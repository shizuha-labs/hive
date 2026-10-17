"""Lease scheduling rules — fairness, hold windows, and the starvation trap.

The property that matters most is that WORK PROGRESSES: with one admission slot
and several teams, no eligible team may be starved, and a holder with an empty
queue must give the slot up immediately rather than sit on it.
"""
from __future__ import annotations

import math
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.core.cache import cache

from .model_lease import (
    HISTORY_CACHE_KEY,
    HOLDINGS_CACHE_KEY,
    LEFTOVER_HOME_YIELD_CACHE_KEY,
    POOL_CACHE_KEY,
    PREEMPT_RETRY_CACHE_KEY,
    PoolMember,
    _agent_dict_active,
    _agent_dict_auto_wakeable,
    _agent_dict_running,
    _overflow_revoke_victim,
    _reconcile_one_pool,
    agent_lease_worthiness,
    all_pool_configs,
    decide_lease,
    hive_admission_cap,
    lease_status_by_username,
    lease_wake_available,
    pool_configs_for_agents,
    principal_for,
    release_leases_for_agent,
    reserved_preempt_victim,
    residency_aware_max_holders,
    scale_plan,
    team_has_actionable_work,
)
from .views import FleetPulseSnapshotView, _attach_model_lease_status

MIN_HOLD = 900
MAX_HOLD = 2700
IDLE = 180


def _members(*specs) -> list[PoolMember]:
    """specs: (username, last_held_at)"""
    return [PoolMember(username=u, email=f'{u}@shizuha.com', teams=(u,), last_held_at=t)
            for u, t in specs]


def _holder(principal, held_for_s=0, idle_for_s=0):
    return {'principal': principal, 'held_for_s': held_for_s, 'idle_for_s': idle_for_s}


class GrantSelectionTests(SimpleTestCase):
    def test_no_work_no_grant(self):
        d = decide_lease(now=0, members=_members(('yuki', 0), ('scout', 0)),
                         wants=set(), current=None)
        self.assertEqual(d.action, 'none')

    def test_grants_to_never_held_member_first(self):
        d = decide_lease(now=0, members=_members(('yuki', 500.0), ('scout', 0.0)),
                         wants={'yuki', 'scout'}, current=None)
        self.assertEqual((d.action, d.holder), ('grant', principal_for('scout')))

    def test_grants_to_least_recently_held(self):
        d = decide_lease(now=0, members=_members(('yuki', 900.0), ('scout', 100.0)),
                         wants={'yuki', 'scout'}, current=None)
        self.assertEqual(d.holder, principal_for('scout'))

    def test_only_members_with_work_are_candidates(self):
        d = decide_lease(now=0, members=_members(('yuki', 0.0), ('scout', 999.0)),
                         wants={'scout'}, current=None)
        self.assertEqual(d.holder, principal_for('scout'))

    def test_assigned_tasks_beat_unread_dm_on_a_single_seat(self):
        """Qwen 2026-08-22: reika (unread DM) won the 1-admission seat over
        revi (assigned security WIP) because both were ``personal`` and LRH
        then fell through to username order."""
        d = decide_lease(
            now=0,
            members=_members(('reika', 0.0), ('revi', 0.0)),
            wants={'reika', 'revi'},
            current=None,
            personal={'reika', 'revi'},
            assigned={'revi'},
        )
        self.assertEqual(d.holder, principal_for('revi'))
        self.assertIn('assigned-tasks', d.reason)

    def test_keep_resident_preempt_retry_beats_never_held_personal(self):
        """Saki 2026-08-18: preempt is not a finished sprint; she is next fill."""
        d = decide_lease(
            now=0,
            members=_members(('saki', 900.0), ('nori', 0.0)),
            wants={'saki', 'nori'},
            current=None,
            personal={'saki', 'nori'},
            preempt_retry={'saki'},
        )
        self.assertEqual(d.holder, principal_for('saki'))

    def test_grant_choice_is_deterministic_on_ties(self):
        a = decide_lease(now=0, members=_members(('yuki', 0.0), ('scout', 0.0)),
                         wants={'yuki', 'scout'}, current=None)
        b = decide_lease(now=0, members=_members(('scout', 0.0), ('yuki', 0.0)),
                         wants={'scout', 'yuki'}, current=None)
        self.assertEqual(a.holder, b.holder)

    def test_active_expired_holder_reclaims_free_slot_before_waiter(self):
        """Hard expiry is not permission to hand off during a live turn."""
        d = decide_lease(
            now=0,
            members=_members(('rui', 999.0), ('san', 0.0)),
            wants={'rui', 'san'},
            current=None,
            running={'rui', 'san'},
            personal={'san'},
            active={'rui'},
        )
        self.assertEqual(d.holder, principal_for('rui'))
        self.assertIn('active-turn-continuity', d.reason)


class HoldWindowTests(SimpleTestCase):
    def test_activity_fence_requires_fresh_telemetry(self):
        fresh = {
            'activity_state': 'working',
            'telemetry_ts': 995,
        }
        stale = {
            'activity_state': 'working',
            'telemetry_ts': 700,
        }

        self.assertTrue(_agent_dict_active(fresh, now=1000))
        self.assertFalse(_agent_dict_active(stale, now=1000))
        self.assertFalse(_agent_dict_active({'activity_state': 'working'}, now=1000))
        self.assertTrue(_agent_dict_active({
            'activity_state': 'working',
            'activity_phase': 'thinking',
            'telemetry_ts': 995,
        }, now=1000))
        self.assertFalse(_agent_dict_active({
            'activity_state': 'working',
            'heartbeat_outcome': 'queue_empty',
            'telemetry_ts': 995,
        }, now=1000))
        self.assertTrue(_agent_dict_active({
            'activity_state': 'working',
            'activity_phase': 'tool',
            'heartbeat_outcome': 'queue_empty',
            'telemetry_ts': 995,
        }, now=1000))

    def test_min_hold_protects_against_preemption(self):
        d = decide_lease(now=0, members=_members(('yuki', 0), ('scout', 0)),
                         wants={'yuki', 'scout'},
                         current=_holder(principal_for('yuki'), held_for_s=60, idle_for_s=600),
                         min_hold_seconds=MIN_HOLD, max_hold_seconds=MAX_HOLD,
                         idle_release_seconds=IDLE)
        self.assertEqual(d.action, 'keep')

    def test_idle_past_min_hold_releases_when_someone_waits(self):
        d = decide_lease(now=0, members=_members(('yuki', 0), ('scout', 0)),
                         wants={'yuki', 'scout'},
                         current=_holder(principal_for('yuki'), held_for_s=MIN_HOLD + 1,
                                         idle_for_s=IDLE + 1),
                         min_hold_seconds=MIN_HOLD, max_hold_seconds=MAX_HOLD,
                         idle_release_seconds=IDLE)
        self.assertEqual(d.action, 'revoke')

    def test_idle_but_nobody_waiting_keeps_the_lease(self):
        """Handing the slot back with no taker would only pay a cold prefill later."""
        d = decide_lease(now=0, members=_members(('yuki', 0), ('scout', 0)),
                         wants={'yuki'},
                         current=_holder(principal_for('yuki'), held_for_s=MIN_HOLD + 1,
                                         idle_for_s=IDLE + 999),
                         min_hold_seconds=MIN_HOLD, max_hold_seconds=MAX_HOLD,
                         idle_release_seconds=IDLE)
        self.assertEqual(d.action, 'keep')

    def test_busy_holder_is_not_preempted_at_max_hold(self):
        """Loaded work refreshes the lease. Wall-clock max-hold used to
        revoke a 287k GLM session 74s after its last token (ni 2026-09-09)
        and yield-homes wiped the KV. idle_for=0 is Cortex activity now.
        """
        d = decide_lease(now=0, members=_members(('yuki', 0), ('scout', 0)),
                         wants={'yuki', 'scout'},
                         current=_holder(principal_for('yuki'), held_for_s=MAX_HOLD + 1,
                                         idle_for_s=0),
                         min_hold_seconds=MIN_HOLD, max_hold_seconds=MAX_HOLD,
                         idle_release_seconds=IDLE)
        self.assertEqual(d.action, 'keep')
        self.assertIn('max-hold refresh', d.reason)

    def test_quiet_holder_is_preempted_at_max_hold(self):
        """Parked seats still rotate once Cortex idle is past the window."""
        d = decide_lease(now=0, members=_members(('yuki', 0), ('scout', 0)),
                         wants={'yuki', 'scout'},
                         current=_holder(principal_for('yuki'), held_for_s=MAX_HOLD + 1,
                                         idle_for_s=IDLE + 1),
                         min_hold_seconds=MIN_HOLD, max_hold_seconds=MAX_HOLD,
                         idle_release_seconds=IDLE)
        self.assertEqual(d.action, 'revoke')
        self.assertFalse(d.force)
        self.assertIn('max hold', d.reason)

    def test_runtime_active_holder_is_not_preempted_at_max_hold(self):
        """Fairness waits for a quiet boundary; it never interrupts live work."""
        d = decide_lease(
            now=0,
            members=_members(('yuki', 0), ('scout', 0)),
            wants={'yuki', 'scout'},
            current=_holder(
                principal_for('yuki'),
                held_for_s=MAX_HOLD + 1,
                idle_for_s=IDLE + 1,
            ),
            min_hold_seconds=MIN_HOLD,
            max_hold_seconds=MAX_HOLD,
            idle_release_seconds=IDLE,
            active={'yuki'},
        )
        self.assertEqual(d.action, 'keep')
        self.assertIn('actively working', d.reason)

    def test_busy_holder_below_max_hold_is_kept(self):
        d = decide_lease(now=0, members=_members(('yuki', 0), ('scout', 0)),
                         wants={'yuki', 'scout'},
                         current=_holder(principal_for('yuki'), held_for_s=MAX_HOLD - 1,
                                         idle_for_s=0),
                         min_hold_seconds=MIN_HOLD, max_hold_seconds=MAX_HOLD,
                         idle_release_seconds=IDLE)
        self.assertEqual(d.action, 'keep')


class ReleaseTests(SimpleTestCase):
    def test_drained_queue_releases_immediately_inside_min_hold(self):
        d = decide_lease(now=0, members=_members(('yuki', 0), ('scout', 0)),
                         wants={'scout'},
                         current=_holder(principal_for('yuki'), held_for_s=5, idle_for_s=0),
                         min_hold_seconds=MIN_HOLD)
        self.assertEqual(d.action, 'revoke')
        self.assertTrue(d.voluntary, 'a drained holder releases voluntarily, not by preemption')

    def test_drained_queue_waits_for_active_holder_to_finish(self):
        d = decide_lease(
            now=0,
            members=_members(('yuki', 0), ('scout', 0)),
            wants={'scout'},
            current=_holder(principal_for('yuki'), held_for_s=5, idle_for_s=600),
            min_hold_seconds=MIN_HOLD,
            active={'yuki'},
        )
        self.assertEqual(d.action, 'keep')
        self.assertIn('actively working', d.reason)

    def test_holder_removed_from_pool_is_force_revoked(self):
        d = decide_lease(now=0, members=_members(('scout', 0)),
                         wants={'scout'},
                         current=_holder(principal_for('yuki'), held_for_s=5))
        self.assertEqual(d.action, 'revoke')
        self.assertTrue(d.force)

    def test_stopped_holder_is_force_revoked_inside_min_hold(self):
        """Nami incident: scaled-to-0 holder must not pin the model for min-hold."""
        d = decide_lease(
            now=0, members=_members(('nami', 0), ('scout', 0)),
            wants={'nami', 'scout'},
            current=_holder(principal_for('nami'), held_for_s=5, idle_for_s=0),
            min_hold_seconds=MIN_HOLD, max_hold_seconds=MAX_HOLD,
            idle_release_seconds=IDLE,
            running={'scout'},  # nami is NOT running
        )
        self.assertEqual(d.action, 'revoke')
        self.assertTrue(d.force)
        self.assertIn('not running', d.reason)

    def test_unused_lease_releases_inside_min_hold_when_someone_waits(self):
        d = decide_lease(
            now=0, members=_members(('saki', 0), ('scout', 0)),
            wants={'saki', 'scout'},
            current=_holder(principal_for('saki'), held_for_s=200, idle_for_s=200),
            min_hold_seconds=MIN_HOLD, max_hold_seconds=MAX_HOLD,
            idle_release_seconds=IDLE,
            running={'saki', 'scout'},
        )
        self.assertEqual(d.action, 'revoke')
        self.assertTrue(d.voluntary)
        self.assertTrue(d.force)
        self.assertIn('unused lease', d.reason)

    def test_unused_lease_keeps_when_only_fellow_holders_are_waiting(self):
        """2026-08-22: kai/nagi/sara unused-revoked each other and bounced."""
        d = decide_lease(
            now=0, members=_members(('kai', 0), ('nagi', 0), ('sato', 0)),
            wants={'kai', 'nagi', 'sato'},
            current=_holder(principal_for('nagi'), held_for_s=553, idle_for_s=553),
            min_hold_seconds=MIN_HOLD, max_hold_seconds=MAX_HOLD,
            idle_release_seconds=IDLE,
            running={'kai', 'nagi', 'sato'},
            holder_usernames={'kai', 'nagi', 'sato'},
        )
        self.assertEqual(d.action, 'keep')

    def test_unused_keep_resident_is_not_revoked_for_waiters(self):
        """Yuna unused-revoke + preempt-Saki (2026-08-18): she already holds."""
        d = decide_lease(
            now=0, members=_members(('yuna', 0), ('saki', 0)),
            wants={'yuna', 'saki'},
            current=_holder(principal_for('yuna'), held_for_s=522, idle_for_s=522),
            min_hold_seconds=MIN_HOLD, max_hold_seconds=MAX_HOLD,
            idle_release_seconds=IDLE,
            running={'yuna', 'saki'},
            reserved={'yuna'},
        )
        self.assertEqual(d.action, 'keep')
        self.assertIn('keep-resident', d.reason)

    def test_max_hold_does_not_rotate_keep_resident(self):
        d = decide_lease(
            now=0, members=_members(('yuna', 0), ('saki', 0)),
            wants={'yuna', 'saki'},
            current=_holder(principal_for('yuna'), held_for_s=4000, idle_for_s=10),
            min_hold_seconds=MIN_HOLD, max_hold_seconds=MAX_HOLD,
            idle_release_seconds=IDLE,
            running={'yuna', 'saki'},
            reserved={'yuna'},
        )
        self.assertEqual(d.action, 'keep')
        self.assertIn('keep-resident', d.reason)

    def test_stopped_keep_resident_is_still_force_revoked(self):
        d = decide_lease(
            now=0, members=_members(('yuna', 0), ('saki', 0)),
            wants={'yuna', 'saki'},
            current=_holder(principal_for('yuna'), held_for_s=522, idle_for_s=522),
            min_hold_seconds=MIN_HOLD, max_hold_seconds=MAX_HOLD,
            idle_release_seconds=IDLE,
            running={'saki'},
            reserved={'yuna'},
        )
        self.assertEqual(d.action, 'revoke')
        self.assertTrue(d.force)
        self.assertIn('not running', d.reason)

    def test_used_lease_still_protected_by_min_hold(self):
        d = decide_lease(
            now=0, members=_members(('saki', 0), ('scout', 0)),
            wants={'saki', 'scout'},
            current=_holder(principal_for('saki'), held_for_s=60, idle_for_s=5),
            min_hold_seconds=MIN_HOLD, max_hold_seconds=MAX_HOLD,
            idle_release_seconds=IDLE,
            running={'saki', 'scout'},
        )
        self.assertEqual(d.action, 'keep')

    def test_running_holder_still_protected_by_min_hold(self):
        d = decide_lease(
            now=0, members=_members(('nami', 0), ('scout', 0)),
            wants={'nami', 'scout'},
            current=_holder(principal_for('nami'), held_for_s=5, idle_for_s=0),
            min_hold_seconds=MIN_HOLD, max_hold_seconds=MAX_HOLD,
            idle_release_seconds=IDLE,
            running={'nami', 'scout'},
        )
        self.assertEqual(d.action, 'keep')

    def test_running_none_skips_liveness_check(self):
        """Unit tests / unknown runtime must not force-revoke by default."""
        d = decide_lease(
            now=0, members=_members(('nami', 0), ('scout', 0)),
            wants={'nami', 'scout'},
            current=_holder(principal_for('nami'), held_for_s=5),
            min_hold_seconds=MIN_HOLD,
            running=None,
        )
        self.assertEqual(d.action, 'keep')


class StarvationTests(SimpleTestCase):
    def test_every_waiting_team_is_eventually_served(self):
        """Round-robin over 4 members must serve all 4 before repeating any."""
        members = _members(('yuki', 0.0), ('scout', 0.0), ('hana', 0.0), ('nao', 0.0))
        wants = {m.username for m in members}
        served, clock = [], 1000.0
        for _ in range(len(members)):
            d = decide_lease(now=clock, members=members, wants=wants, current=None)
            self.assertEqual(d.action, 'grant')
            won = d.holder.removeprefix('agent-')
            served.append(won)
            members = [
                PoolMember(m.username, m.email, m.teams, clock) if m.username == won else m
                for m in members
            ]
            clock += MIN_HOLD
        self.assertCountEqual(served, [m.username for m in members],
                              'a member was served twice before another got a turn')

    def test_eligibility_is_team_level_not_assignment_level(self):
        """A STOPPED agent still qualifies via its team's queue.

        This is the whole anti-deadlock property: Pulse only assigns to started
        agents, so a per-agent signal would leave a stopped agent permanently
        ineligible and its team permanently unserved.
        """
        self.assertTrue(team_has_actionable_work(
            ('documentation',), {'documentation': {'ready': 4}}, ('ready', 'in_progress')))
        self.assertFalse(team_has_actionable_work(
            ('documentation',), {'documentation': {'done': 9}}, ('ready', 'in_progress')))

    def test_work_on_any_of_several_teams_qualifies(self):
        self.assertTrue(team_has_actionable_work(
            ('design', 'documentation'), {'documentation': {'in_progress': 1}},
            ('ready', 'in_progress')))


class ScalePlanTests(SimpleTestCase):
    def test_only_the_holder_runs(self):
        members = _members(('yuki', 0), ('scout', 0), ('hana', 0))
        plan = scale_plan(members=members, holder=principal_for('scout'))
        self.assertEqual(plan, {'yuki': False, 'scout': True, 'hana': False})

    def test_no_holder_stops_everyone(self):
        plan = scale_plan(members=_members(('yuki', 0)), holder=None)
        self.assertEqual(plan, {'yuki': False})

    def test_active_nonholder_stays_running_until_quiet_boundary(self):
        members = _members(('rui', 0), ('san', 0))
        plan = scale_plan(
            members=members,
            holder=principal_for('san'),
            active={'rui'},
        )
        self.assertEqual(plan, {'rui': True, 'san': True})


class LeaseExpiryContinuitySequenceTests(SimpleTestCase):
    """Production order: expiry while active, then handoff after idle."""

    class Client:
        def __init__(self):
            self.data = {
                'max_holders': 1,
                # Cortex filters the just-expired rui lease before Hive reads it.
                'holders': [],
                'governed_principals': ['agent-rui', 'agent-san'],
                'eligible_principals': ['agent-san'],
            }
            self.grants = []
            self.revokes = []

        def board(self, _model):
            return self.data

        def set_governed(self, _model, principals):
            self.data['governed_principals'] = sorted(principals)
            return True

        def set_eligible(self, _model, principals):
            self.data['eligible_principals'] = sorted(principals)
            return True

        def grant(self, _model, holder, **_kwargs):
            self.grants.append(holder)
            self.data['holders'] = [{'principal': holder}]
            return True

        def revoke(self, _model, *, holder, **_kwargs):
            self.revokes.append(holder)
            self.data['holders'] = [
                row for row in self.data.get('holders') or []
                if row.get('principal') != holder
            ]
            return True

    def setUp(self):
        cache.delete(HISTORY_CACHE_KEY)
        cache.delete(HOLDINGS_CACHE_KEY)
        self.client = self.Client()
        self.cfg = {
            'enabled': True,
            'model_id': 'DeepSeek-V4-Flash',
            'members': ['rui', 'san'],
            'min_hold_seconds': MIN_HOLD,
            'max_hold_seconds': MAX_HOLD,
            'idle_release_seconds': IDLE,
            'max_holders': 1,
        }
        self.base_agents = [
            {
                'username': 'rui',
                'email': 'rui@shizuha.com',
                'status': 'running',
                'enabled': True,
            },
            {
                'username': 'san',
                'email': 'san@shizuha.com',
                'status': 'running',
                'enabled': True,
            },
        ]

    def tearDown(self):
        cache.delete(HISTORY_CACHE_KEY)
        cache.delete(HOLDINGS_CACHE_KEY)

    def _tick(self, agents, *, now, can_handoff=None):
        return _reconcile_one_pool(
            cfg=self.cfg,
            agents=agents,
            email_to_teams={},
            workload_by_team={},
            workload_by_email={'san@shizuha.com': {'ready': 1}},
            unread_by_email={},
            alerts_by_email={},
            actionable_buckets=('ready',),
            client=self.client,
            now=now,
            apply=True,
            can_handoff=can_handoff,
        )

    def test_expired_active_holder_wins_then_hands_off_only_after_idle(self):
        active_agents = [
            {
                **self.base_agents[0],
                'activity_state': 'working',
                'telemetry_ts': 1000,
            },
            self.base_agents[1],
        ]
        active_tick = self._tick(active_agents, now=1000)

        self.assertEqual(self.client.grants, ['agent-rui'])
        self.assertEqual(active_tick['scale'], {'rui': True, 'san': False})

        # Next scheduler observation is quiet. Rui's renewed board entry can now
        # be revoked and the waiting agent receives the slot in the same tick.
        self.client.data['holders'] = [{
            'principal': 'agent-rui',
            'held_for_s': MAX_HOLD + 1,
            'idle_for_s': IDLE + 1,
        }]
        quiet_tick = self._tick(self.base_agents, now=1300)

        self.assertEqual(self.client.revokes, ['agent-rui'])
        self.assertEqual(self.client.grants, ['agent-rui', 'agent-san'])
        self.assertEqual(quiet_tick['scale'], {'rui': False, 'san': True})

    def test_last_instant_busy_evidence_defers_lease_revoke(self):
        self.client.data['holders'] = [{
            'principal': 'agent-rui',
            'held_for_s': MAX_HOLD + 1,
            'idle_for_s': IDLE + 1,
        }]

        tick = self._tick(
            self.base_agents,
            now=1300,
            can_handoff=lambda username: (
                False,
                f'{username} began working after planning',
            ),
        )

        self.assertEqual(self.client.revokes, [])
        self.assertEqual(self.client.grants, [])
        self.assertEqual(tick['scale'], {'rui': True, 'san': False})
        self.assertIn('DEFER REVOKE', tick['log'][-1])


@override_settings(CACHES={'default': {
    'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
    'LOCATION': 'lease-grace-interrupt-tests',
}})
class ExpiredNonholderGraceInterruptTests(SimpleTestCase):
    """HIVE-1712 / kei 2026-08-15: a constantly-working non-holder stays up
    for the 900s grace, then scale_plan says stop and interrupt_ok is set so
    the apply-time idle fence cannot defer hibernation forever."""

    class Client:
        def __init__(self):
            self.data = {
                'max_holders': 1,
                'holders': [{'principal': 'agent-kai', 'held_for_s': 100}],
                'governed_principals': ['agent-kai', 'agent-kei'],
                'eligible_principals': ['agent-kai', 'agent-kei'],
            }
            self.grants = []
            self.revokes = []

        def board(self, _model):
            return self.data

        def set_governed(self, _model, principals):
            self.data['governed_principals'] = sorted(principals)
            return True

        def set_eligible(self, _model, principals):
            self.data['eligible_principals'] = sorted(principals)
            return True

        def grant(self, _model, holder, **_kwargs):
            self.grants.append(holder)
            return True

        def revoke(self, _model, *, holder, **_kwargs):
            self.revokes.append(holder)
            return True

    def setUp(self):
        cache.delete(HISTORY_CACHE_KEY)
        cache.delete(HOLDINGS_CACHE_KEY)
        cache.delete('fleet:model-lease-nonholder-active:deepseek-v4-flash')
        self.client = self.Client()
        self.cfg = {
            'enabled': True,
            'model_id': 'DeepSeek-V4-Flash',
            'members': ['kai', 'kei'],
            'min_hold_seconds': MIN_HOLD,
            'max_hold_seconds': MAX_HOLD,
            'idle_release_seconds': IDLE,
            'max_holders': 1,
        }
        self.agents = [
            {
                'username': 'kai',
                'email': 'kai@shizuha.com',
                'status': 'running',
                'enabled': True,
                'activity_state': 'working',
                'telemetry_ts': 1000,
            },
            {
                'username': 'kei',
                'email': 'kei@shizuha.com',
                'status': 'running',
                'enabled': True,
                'activity_state': 'working',
                'telemetry_ts': 1000,
            },
        ]

    def tearDown(self):
        cache.delete(HISTORY_CACHE_KEY)
        cache.delete(HOLDINGS_CACHE_KEY)
        cache.delete('fleet:model-lease-nonholder-active:deepseek-v4-flash')

    def _tick(self, *, now):
        agents = []
        for agent in self.agents:
            row = dict(agent)
            row['telemetry_ts'] = now
            agents.append(row)
        return _reconcile_one_pool(
            cfg=self.cfg,
            agents=agents,
            email_to_teams={},
            workload_by_team={},
            workload_by_email={
                'kai@shizuha.com': {'ready': 1},
                'kei@shizuha.com': {'ready': 1},
            },
            unread_by_email={},
            alerts_by_email={},
            actionable_buckets=('ready',),
            client=self.client,
            now=now,
            apply=True,
        )

    def test_working_nonholder_is_kept_for_grace_then_marked_interrupt_ok(self):
        first = self._tick(now=1000)
        self.assertEqual(first['scale'], {'kai': True, 'kei': True})
        self.assertEqual(first.get('interrupt_ok') or set(), set())

        later = self._tick(now=1000 + 901)
        self.assertEqual(later['scale'], {'kai': True, 'kei': False})
        self.assertIn('kei', later.get('interrupt_ok') or set())
        self.assertTrue(
            any('grace expired' in line for line in later.get('log') or [])
        )

    def test_one_seat_listing_does_not_keep_working_nonholder(self):
        """Vision-Metal admission=1: the 900s fence is a second Running agent."""
        self.client.data['admission_capacity'] = 1
        first = self._tick(now=1000)
        self.assertEqual(first['scale'], {'kai': True, 'kei': False})
        self.assertIn('kei', first.get('interrupt_ok') or set())
        self.assertTrue(
            any('1-seat exclusive' in line for line in first.get('log') or [])
        )


class IdleRevokeDoesNotReelectSameHolderTests(SimpleTestCase):
    """Production order: idle revoke must rotate to another waiter this tick.

    Live 2026-08-12: Hive revoked idle rei/ryo (min-hold elapsed, 15 waiting)
    then immediately re-granted them because last_held_at still reflected an
    hours-old Hive grant (Cortex auto-acquire never updates history).
    """

    class Client:
        def __init__(self):
            self.data = {
                'max_holders': 1,
                'holders': [{
                    'principal': 'agent-rei',
                    'held_for_s': MAX_HOLD + 1,
                    'idle_for_s': IDLE + 1,
                }],
                'governed_principals': ['agent-rei', 'agent-kai'],
                'eligible_principals': ['agent-rei', 'agent-kai'],
            }
            self.grants = []
            self.revokes = []

        def board(self, _model):
            return self.data

        def set_governed(self, _model, principals):
            self.data['governed_principals'] = sorted(principals)
            return True

        def set_eligible(self, _model, principals):
            self.data['eligible_principals'] = sorted(principals)
            return True

        def grant(self, _model, holder, **_kwargs):
            self.grants.append(holder)
            self.data['holders'] = [{'principal': holder}]
            return True

        def revoke(self, _model, *, holder, **_kwargs):
            self.revokes.append(holder)
            self.data['holders'] = [
                row for row in self.data.get('holders') or []
                if row.get('principal') != holder
            ]
            return True

    def setUp(self):
        cache.delete(HISTORY_CACHE_KEY)
        cache.delete(HOLDINGS_CACHE_KEY)
        cache.set(HISTORY_CACHE_KEY, {'rei': 100.0, 'kai': 500.0}, None)
        self.client = self.Client()
        self.cfg = {
            'enabled': True,
            'model_id': 'DeepSeek-V4-Flash',
            'members': ['rei', 'kai'],
            'min_hold_seconds': MIN_HOLD,
            'max_hold_seconds': MAX_HOLD,
            'idle_release_seconds': IDLE,
            'max_holders': 1,
        }
        self.agents = [
            {'username': 'rei', 'email': 'rei@shizuha.com', 'status': 'running', 'enabled': True},
            {'username': 'kai', 'email': 'kai@shizuha.com', 'status': 'hibernated', 'enabled': False},
        ]

    def tearDown(self):
        cache.delete(HISTORY_CACHE_KEY)
        cache.delete(HOLDINGS_CACHE_KEY)

    def test_idle_revoke_grants_the_other_waiter_not_the_same_holder(self):
        tick = _reconcile_one_pool(
            cfg=self.cfg,
            agents=self.agents,
            email_to_teams={},
            workload_by_team={},
            workload_by_email={
                'rei@shizuha.com': {'ready': 2},
                'kai@shizuha.com': {'ready': 2},
            },
            unread_by_email={},
            alerts_by_email={},
            actionable_buckets=('ready',),
            client=self.client,
            now=2000.0,
            apply=True,
            can_handoff=lambda _username: (True, 'runtime reports fresh idle at apply time'),
        )
        self.assertEqual(self.client.revokes, ['agent-rei'])
        self.assertEqual(self.client.grants, ['agent-kai'])
        self.assertEqual(tick['scale'], {'rei': False, 'kai': True})
        self.assertEqual(cache.get(HISTORY_CACHE_KEY).get('rei'), 2000.0)


@override_settings(CACHES={'default': {
    'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
    'LOCATION': 'unused-revoke-no-regrant-tests',
}})
class UnusedRevokeDoesNotRegrantSameHolderTests(SimpleTestCase):
    """Unused assigned-tasks holders must not fill their own vacancy.

    Live 2026-09-15: GLM unused-revoked hiro/nami/mio (idle==held ~10m) then
    the fill loop re-granted them the same tick because assigned_tasks sorts
    ahead of last_held_at while jun/kumo waited on team_queue.
    """

    class Client:
        def __init__(self):
            self.data = {
                'leased': True,
                'max_holders': 1,
                'admission_capacity': 1,
                'residency': {
                    'seats': 1, 'homes': 0, 'protected': 0,
                    'grantable': 1, 'full': False,
                },
                'holders': [{
                    'principal': 'agent-hiro',
                    'held_for_s': 598,
                    'idle_for_s': 598,
                }],
                'governed_principals': ['agent-hiro', 'agent-jun'],
                'eligible_principals': ['agent-hiro', 'agent-jun'],
            }
            self.grants = []
            self.revokes = []

        def board(self, _model):
            return self.data

        def set_governed(self, _model, principals):
            self.data['governed_principals'] = sorted(principals)
            return True

        def set_eligible(self, _model, principals):
            self.data['eligible_principals'] = sorted(principals)
            return True

        def grant(self, _model, holder, **_kwargs):
            self.grants.append(holder)
            self.data['holders'] = [{'principal': holder}]
            return True

        def revoke(self, _model, *, holder, **_kwargs):
            self.revokes.append(holder)
            self.data['holders'] = [
                row for row in self.data.get('holders') or []
                if row.get('principal') != holder
            ]
            return True

        def yield_homes(self, _model, _principal, **_kwargs):
            return False

    def setUp(self):
        cache.delete(HISTORY_CACHE_KEY)
        cache.delete(HOLDINGS_CACHE_KEY)
        self.client = self.Client()
        self.cfg = {
            'enabled': True,
            'model_id': 'GLM-5.3-Flash',
            'members': ['hiro', 'jun'],
            'min_hold_seconds': MIN_HOLD,
            'max_hold_seconds': MAX_HOLD,
            'idle_release_seconds': IDLE,
            'max_holders': 1,
        }

    def tearDown(self):
        cache.delete(HISTORY_CACHE_KEY)
        cache.delete(HOLDINGS_CACHE_KEY)

    def test_unused_assigned_holder_does_not_win_the_same_tick_fill(self):
        tick = _reconcile_one_pool(
            cfg=self.cfg,
            agents=[
                {
                    'username': 'hiro', 'email': 'hiro@shizuha.com',
                    'status': 'running', 'enabled': True,
                    'activity_state': 'idle', 'activity_phase': 'idle',
                    'activity_phase_for_ms': 60_000,
                    'lease_idle_waste': True,
                },
                {
                    'username': 'jun', 'email': 'jun@shizuha.com',
                    'status': 'hibernated', 'enabled': False,
                },
            ],
            email_to_teams={'jun@shizuha.com': {'engineering'}},
            workload_by_team={'engineering': {'ready': 3}},
            workload_by_email={'hiro@shizuha.com': {'ready': 2}},
            unread_by_email={},
            alerts_by_email={},
            actionable_buckets=('ready', 'open', 'todo'),
            client=self.client,
            now=2000.0,
            apply=True,
        )
        self.assertEqual(self.client.revokes, ['agent-hiro'])
        self.assertEqual(self.client.grants, ['agent-jun'])
        self.assertNotIn('agent-hiro', self.client.grants)
        self.assertEqual(tick['scale'].get('hiro'), False)
        self.assertEqual(tick['scale'].get('jun'), True)


class PoolMembershipSoTTests(SimpleTestCase):
    def test_members_follow_effective_model_not_stale_manual_list(self):
        configs = [
            {'model_id': 'DeepSeek-V4-Flash', 'members': ['scout']},
            {'model_id': 'GLM-5.2-NVFP4-AQLM-380K', 'members': ['rui']},
        ]
        agents = [
            {
                'agent_username': 'recon',
                'model': 'cortex/DeepSeek-V4-Flash',
                'effective_model': 'cortex/DeepSeek-V4-Flash',
                'runtime_source': 'rt-fleet',
            },
            {
                'agent_username': 'rui',
                'model': 'cortex/DeepSeek-V4-Flash',
                'runtime_source': 'k3s-native',
            },
            {
                'agent_username': 'scout',
                'model': 'cortex/GLM-5.2-NVFP4-AQLM-380K',
                'runtime_source': 'rt-fleet',
            },
        ]

        resolved = pool_configs_for_agents(configs, agents)

        self.assertEqual(resolved[0]['members'], ['recon', 'rui'])
        self.assertEqual(resolved[1]['members'], ['scout'])

    def test_claw_principals_are_not_added_to_agent_lease_pool(self):
        configs = [{'model_id': 'DeepSeek-V4-Flash', 'members': ['nagi']}]
        agents = [
            {
                'agent_username': 'nagi',
                'model': 'DeepSeek-V4-Flash',
                'runtime_source': 'rt-fleet',
            },
            {
                'agent_username': 'hermes',
                'model': 'cortex/DeepSeek-V4-Flash',
                'runtime_source': 'claw',
                'id': 'claw:123',
            },
        ]

        resolved = pool_configs_for_agents(configs, agents)

        self.assertEqual(resolved[0]['members'], ['nagi'])

    def test_incomplete_inventory_does_not_erase_configured_pool(self):
        configs = [{'model_id': 'DeepSeek-V4-Flash', 'members': ['nagi']}]

        resolved = pool_configs_for_agents(configs, [])

        self.assertEqual(resolved[0]['members'], ['nagi'])

    def test_complete_inventory_clears_stale_members_of_unpinned_model(self):
        configs = [
            {'model_id': 'DeepSeek-V4-Flash', 'members': ['nagi']},
            {'model_id': 'Qwen3.8-27B-Q2', 'members': ['nami', 'reika']},
        ]
        agents = [
            {
                'agent_username': 'nagi',
                'model': 'cortex/DeepSeek-V4-Flash',
                'runtime_source': 'k3s-native',
            },
            {
                'agent_username': 'nami',
                'model': 'cortex/DeepSeek-V4-Flash',
                'runtime_source': 'k3s-native',
            },
            {
                'agent_username': 'reika',
                'model': 'cortex/DeepSeek-V4-Flash',
                'runtime_source': 'k3s-native',
            },
        ]

        resolved = pool_configs_for_agents(configs, agents)

        self.assertEqual(resolved[0]['members'], ['nagi', 'nami', 'reika'])
        self.assertEqual(resolved[1]['members'], [])


class ReleaseLeasesYieldsHomesTests(SimpleTestCase):
    class Client:
        def __init__(self):
            self.data = {
                'max_holders': 4,
                'holders': [{'principal': 'agent-kai'}],
                'holder': {'principal': 'agent-kai'},
            }
            self.yielded = []
            self.revokes = []

        def board(self, _model):
            return self.data

        def yield_homes(self, model, principal, *, reason=''):
            self.yielded.append((model, principal, reason))
            return True

        def revoke(self, model, *, holder, **_kwargs):
            self.revokes.append((model, holder))
            return True

    def test_hibernate_releases_hive_homes_even_without_a_lease(self):
        """Hibernate must free the Cortex KV book, not leave a parked prefix.

        Live 2026-08-22: scout/rei were hibernated with no Hive lease but still
        occupied i7-tp4 / tp4d residency seats.
        """
        with patch('fleet.model_lease.all_pool_configs', return_value=[
            {'model_id': 'DeepSeek-V4-Flash', 'members': ['lina', 'kai']},
        ]):
            client = self.Client()
            out = release_leases_for_agent('lina', client=client, reason='native hibernated lina')
        self.assertEqual(out['yielded'], ['DeepSeek-V4-Flash'])
        self.assertEqual(client.yielded, [
            ('DeepSeek-V4-Flash', 'agent-lina', 'native hibernated lina'),
        ])
        self.assertEqual(out['released'], [])
        self.assertEqual(client.revokes, [])


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class RunningNonholderYieldsHomesTests(SimpleTestCase):
    """A running leftover keeps its Redis home until hibernate.

    Operator 2026-09-01 (21/20 leak): idle leftovers must not occupy seats
    forever — scale them off, then yield on the hibernated tick.
    Operator 2026-09-01 (riku 104f0af7): yielding while the pod is still
    running deleted the session-route book every 10 min and painted a
    155k "cold" turn on a still-warm engine.
    """

    class Client:
        def __init__(self):
            self.data = {
                'max_holders': 1,
                'admission_capacity': 1,
                'residency': {
                    'seats': 1, 'homes': 1, 'protected': 1,
                    'grantable': 0, 'full': True,
                    'principals': ['agent-saki'],
                },
                'holders': [{'principal': 'agent-saki', 'held_for_s': 100, 'idle_for_s': 10}],
                'governed_principals': ['agent-saki', 'agent-hiro'],
                'eligible_principals': ['agent-saki', 'agent-hiro'],
            }
            self.yielded = []

        def board(self, _model):
            return self.data

        def set_governed(self, _model, principals):
            self.data['governed_principals'] = sorted(principals)
            return True

        def set_eligible(self, _model, principals):
            self.data['eligible_principals'] = sorted(principals)
            return True

        def grant(self, _model, holder, **_kwargs):
            return True

        def revoke(self, _model, **_kwargs):
            return True

        def yield_homes(self, model, principal, *, reason=''):
            self.yielded.append((model, principal, reason))
            return True

    def setUp(self):
        cache.delete(HISTORY_CACHE_KEY)
        cache.delete(HOLDINGS_CACHE_KEY)
        cache.delete(LEFTOVER_HOME_YIELD_CACHE_KEY)
        self.client = self.Client()
        self.cfg = {
            'enabled': True,
            'model_id': 'DeepSeek-V4-Flash',
            'members': ['saki', 'hiro'],
            'min_hold_seconds': MIN_HOLD,
            'max_hold_seconds': MAX_HOLD,
            'idle_release_seconds': IDLE,
            'max_holders': 1,
        }

    def tearDown(self):
        cache.delete(HISTORY_CACHE_KEY)
        cache.delete(HOLDINGS_CACHE_KEY)
        cache.delete(LEFTOVER_HOME_YIELD_CACHE_KEY)

    def test_running_idle_nonholder_homes_are_kept_until_hibernate(self):
        """A living leftover must not lose its Redis home between heartbeats.

        riku 2026-09-01: leftover yield while status=running deleted the
        session-route book on the 10-min tick; the next interactive turn
        on 104f0af7/e843d458 painted 0% cache / cold · no warm home.
        Hibernate, then yield.
        """
        agents = [
            {'username': 'saki', 'email': 'saki@shizuha.com', 'status': 'running', 'enabled': True},
            {'username': 'hiro', 'email': 'hiro@shizuha.com', 'status': 'running', 'enabled': True},
        ]
        tick = _reconcile_one_pool(
            cfg=self.cfg,
            agents=agents,
            email_to_teams={},
            workload_by_team={},
            workload_by_email={'saki@shizuha.com': {'ready': 1}},
            unread_by_email={},
            alerts_by_email={},
            actionable_buckets=('ready',),
            client=self.client,
            now=1000.0,
            apply=True,
        )
        yielded_principals = [p for _, p, _ in self.client.yielded]
        self.assertNotIn('agent-hiro', yielded_principals)
        self.assertNotIn('agent-saki', yielded_principals)
        self.assertFalse(tick['scale']['hiro'])

    def test_one_seat_idle_resident_yields_before_fill_when_unleased(self):
        """An idle home on the only seat must not block the worthy waiter.

        Qwen3.8-27B 2026-09-24: Cortex was not leasing the model, Reika's
        protected home made fill return kv_residency_full, and both pins
        were hibernated with the GPU still parked.
        """
        self.client.data = {
            'leased': False,
            'max_holders': 1,
            'admission_capacity': 1,
            'residency': {
                'seats': 1, 'homes': 1, 'protected': 1,
                'grantable': 0, 'full': True,
                'principals': ['agent-reika'],
            },
            'holders': [],
            'governed_principals': [],
            'eligible_principals': [],
        }
        self.cfg['model_id'] = 'Qwen3.8-27B'
        self.cfg['members'] = ['reika', 'zen']
        agents = [
            {
                'username': 'reika',
                'email': 'reika@shizuha.com',
                'status': 'running',
                'enabled': True,
                'heartbeat_outcome': 'queue_empty',
                'activity_phase': 'idle',
                'activity_phase_for_ms': 960_000,
            },
            {
                'username': 'zen',
                'email': 'zen@shizuha.com',
                'status': 'running',
                'enabled': True,
                'heartbeat_outcome': '',
                'activity_phase': 'idle',
                'activity_phase_for_ms': 1_000,
            },
        ]
        tick = _reconcile_one_pool(
            cfg=self.cfg,
            agents=agents,
            email_to_teams={},
            workload_by_team={},
            workload_by_email={'zen@shizuha.com': {'ready': 1}},
            unread_by_email={},
            alerts_by_email={},
            actionable_buckets=('ready',),
            client=self.client,
            now=1000.0,
            apply=True,
        )
        log = ' '.join(tick['log'])
        self.assertIn('YIELD idle-resident principal=agent-reika', log)
        self.assertIn('GRANT local-only holder=agent-zen', log)
        self.assertNotIn('FILL none reason=kv_residency_full', log)
        self.assertIn(('Qwen3.8-27B', 'agent-reika'), [
            (model, principal) for model, principal, _reason in self.client.yielded
        ])
        self.assertTrue(tick['scale'].get('zen'))
        self.assertFalse(tick['scale'].get('reika'))

    def test_running_active_nonholder_home_is_kept(self):
        """In-flight decode still owns the book for the active-turn grace."""
        self.client.data['residency'] = {
            'seats': 2, 'homes': 2, 'protected': 2,
            'grantable': 0, 'full': True,
            'principals': ['agent-saki', 'agent-hiro'],
        }
        self.client.data['admission_capacity'] = 2
        self.client.data['max_holders'] = 1
        agents = [
            {'username': 'saki', 'email': 'saki@shizuha.com', 'status': 'running', 'enabled': True},
            {
                'username': 'hiro',
                'email': 'hiro@shizuha.com',
                'status': 'running',
                'enabled': True,
                'activity_state': 'working',
                'activity_phase': 'responding',
                'telemetry_ts': 1000,
            },
        ]
        tick = _reconcile_one_pool(
            cfg=self.cfg,
            agents=agents,
            email_to_teams={},
            workload_by_team={},
            workload_by_email={'saki@shizuha.com': {'ready': 1}},
            unread_by_email={},
            alerts_by_email={},
            actionable_buckets=('ready',),
            client=self.client,
            now=1000.0,
            apply=True,
        )
        yielded_principals = [p for _, p, _ in self.client.yielded]
        self.assertNotIn('agent-hiro', yielded_principals)
        self.assertTrue(tick['scale']['hiro'])

    def test_hibernated_nonholder_leftover_home_is_yielded(self):
        """Already-hibernated leftovers must free the Redis book.

        Live 2026-08-22: scout had no Hive lease but still occupied i7-tp4
        (186k tokens, 52 min) because hibernate skipped yield when the
        agent was no longer a holder. Reconcile is the backstop.
        """
        agents = [
            {'username': 'saki', 'email': 'saki@shizuha.com', 'status': 'running', 'enabled': True},
            {'username': 'scout', 'email': 'scout@shizuha.com', 'status': 'hibernated', 'enabled': False},
        ]
        self.cfg['members'] = ['saki', 'scout']
        tick = _reconcile_one_pool(
            cfg=self.cfg,
            agents=agents,
            email_to_teams={},
            workload_by_team={},
            workload_by_email={'saki@shizuha.com': {'ready': 1}},
            unread_by_email={},
            alerts_by_email={},
            actionable_buckets=('ready',),
            client=self.client,
            now=1000.0,
            apply=True,
        )
        yielded_principals = [p for _, p, _ in self.client.yielded]
        self.assertIn('agent-scout', yielded_principals)
        self.assertNotIn('agent-saki', yielded_principals)
        self.assertIn('YIELD leftover principal=agent-scout', ' '.join(tick['log']))
        self.assertFalse(tick['scale'].get('scout'))

    def test_cached_zero_yield_retries_when_principal_reappears_on_residents_board(self):
        """Hibernate yield-0 must not park a later re-home for an hour.

        Live 2026-09-16: leftover yield cached count=0 for agent-kumo
        (pod still 1/1), the next heartbeat minted a new GLM book, and
        the 1h cache skipped every later tick while KV Residents still
        listed her. A principal on the residents board is not "no book".
        """
        cache.set(
            LEFTOVER_HOME_YIELD_CACHE_KEY,
            {'agent-kumo': 990.0},
            None,
        )
        self.cfg['members'] = ['saki', 'kumo']
        self.client.data['governed_principals'] = ['agent-saki', 'agent-kumo']
        self.client.data['eligible_principals'] = ['agent-saki']
        self.client.data['residency'] = {
            'seats': 2, 'homes': 2, 'protected': 2,
            'grantable': 0, 'full': False,
            'principals': ['agent-saki', 'agent-kumo'],
        }
        agents = [
            {'username': 'saki', 'email': 'saki@shizuha.com', 'status': 'running', 'enabled': True},
            {'username': 'kumo', 'email': 'kumo@shizuha.com', 'status': 'hibernated', 'enabled': False},
        ]
        tick = _reconcile_one_pool(
            cfg=self.cfg,
            agents=agents,
            email_to_teams={},
            workload_by_team={},
            workload_by_email={'saki@shizuha.com': {'ready': 1}},
            unread_by_email={},
            alerts_by_email={},
            actionable_buckets=('ready',),
            client=self.client,
            now=1000.0,
            apply=True,
        )
        yielded_principals = [p for _, p, _ in self.client.yielded]
        self.assertIn('agent-kumo', yielded_principals)
        self.assertIn('YIELD leftover principal=agent-kumo', ' '.join(tick['log']))

    def test_homed_idle_holder_is_revoked_and_yielded(self):
        """Idle holders must drop the lease AND the Redis home.

        2026-09-02 leftover was revoke without yield. 2026-09-06: skipping
        the revoke because a KV book still existed left 14 silent
        residencies against 4 in-flight GLM admits. Yield on this path
        keeps one-home-one-lease.
        """
        self.client.data['holders'] = [
            {'principal': 'agent-nagi', 'held_for_s': 2000, 'idle_for_s': 400},
        ]
        self.client.data['residency'] = {
            'seats': 2, 'homes': 1, 'protected': 1,
            'grantable': 1, 'full': False,
            'principals': ['agent-nagi'],
        }
        self.client.data['max_holders'] = 1
        self.client.data['admission_capacity'] = 2
        self.cfg['members'] = ['nagi', 'kei']
        agents = [
            {'username': 'nagi', 'email': 'nagi@shizuha.com', 'status': 'running', 'enabled': True},
            {'username': 'kei', 'email': 'kei@shizuha.com', 'status': 'hibernated', 'enabled': False},
        ]
        tick = _reconcile_one_pool(
            cfg=self.cfg,
            agents=agents,
            email_to_teams={},
            workload_by_team={},
            workload_by_email={
                'nagi@shizuha.com': {'ready': 1},
                'kei@shizuha.com': {'ready': 1},
            },
            unread_by_email={},
            alerts_by_email={},
            actionable_buckets=('ready',),
            client=self.client,
            now=1000.0,
            apply=True,
        )
        log = ' '.join(tick['log'])
        self.assertIn('REVOKE holder=agent-nagi', log)
        self.assertNotIn('still has KV home', log)
        self.assertIn('agent-nagi', [p for _, p, _ in self.client.yielded])

    def test_hibernated_nonmember_leftover_home_is_yielded(self):
        """A hibernated DeepSeek agent still occupies a KV seat even off-pool.

        Live 2026-08-25: revi was Hive-hibernated but still on the KV
        Residents board (181K, 29m TTL) while sato ran homeless as the 8th
        DeepSeek agent. Reconcile only scanned pool members, so the leftover
        never yielded.
        """
        agents = [
            {'username': 'saki', 'email': 'saki@shizuha.com', 'status': 'running', 'enabled': True},
            {'username': 'revi', 'email': 'revi@shizuha.com', 'status': 'hibernated', 'enabled': False},
        ]
        self.cfg['members'] = ['saki']
        tick = _reconcile_one_pool(
            cfg=self.cfg,
            agents=agents,
            email_to_teams={},
            workload_by_team={},
            workload_by_email={'saki@shizuha.com': {'ready': 1}},
            unread_by_email={},
            alerts_by_email={},
            actionable_buckets=('ready',),
            client=self.client,
            now=1000.0,
            apply=True,
        )
        yielded_principals = [p for _, p, _ in self.client.yielded]
        self.assertIn('agent-revi', yielded_principals)
        self.assertIn('YIELD leftover principal=agent-revi', ' '.join(tick['log']))

    def test_empty_residency_does_not_yield_unrelated_fleet_members(self):
        """Vision-Metal 2026-09-09: leftover yield iterated the whole fleet.

        Cortex had no KV homes on the unmanaged ds4 listing. Reconcile still
        POSTed yield-homes for every hibernated GLM seat (~70 × 4 pools) and
        the 10-min job took 8 minutes; the Metal holder started only at the
        end. Empty residents board ⇒ only pool members are candidates.
        """
        cache.delete(LEFTOVER_HOME_YIELD_CACHE_KEY)
        self.cfg['model_id'] = 'DeepSeek-V4-Flash-Vision-Metal'
        self.cfg['members'] = ['nova', 'rei', 'yuki']
        self.client.data = {
            'leased': True,
            'max_holders': 1,
            'admission_capacity': 1,
            'residency': {
                'seats': 1, 'homes': 0, 'protected': 0,
                'grantable': 1, 'full': False,
                'principals': [],
            },
            'holders': [{'principal': 'agent-rei', 'held_for_s': 10, 'idle_for_s': 10}],
            'governed_principals': [
                'agent-nova', 'agent-rei', 'agent-yuki',
            ],
            'eligible_principals': ['agent-rei'],
        }
        agents = [
            {'username': 'rei', 'email': 'rei@shizuha.com', 'status': 'running', 'enabled': True},
            {'username': 'nova', 'email': 'nova@shizuha.com', 'status': 'hibernated', 'enabled': False},
            {'username': 'yuki', 'email': 'yuki@shizuha.com', 'status': 'hibernated', 'enabled': False},
            {'username': 'akira', 'email': 'akira@shizuha.com', 'status': 'hibernated', 'enabled': False},
            {'username': 'ichi', 'email': 'ichi@shizuha.com', 'status': 'running', 'enabled': True},
        ]
        tick = _reconcile_one_pool(
            cfg=self.cfg,
            agents=agents,
            email_to_teams={'rei@shizuha.com': {'review'}},
            workload_by_team={'review': {'ready': 3}},
            workload_by_email={},
            unread_by_email={},
            alerts_by_email={},
            actionable_buckets=('ready',),
            client=self.client,
            now=1000.0,
            apply=True,
        )
        yielded = [p for _, p, _ in self.client.yielded]
        self.assertNotIn('agent-akira', yielded)
        self.assertNotIn('agent-ichi', yielded)
        self.assertNotIn('YIELD leftover principal=agent-akira', ' '.join(tick['log']))

    def test_running_agent_converted_off_metal_is_yielded(self):
        """Nova 2026-09-18: running GLM seat must drop the Metal Redis home."""
        cache.delete(LEFTOVER_HOME_YIELD_CACHE_KEY)
        self.cfg['model_id'] = 'DeepSeek-V4-Flash-Vision-Metal'
        self.cfg['members'] = ['nova']
        self.client.data = {
            'max_holders': 1,
            'admission_capacity': 1,
            'residency': {
                'seats': 1, 'homes': 1, 'protected': 1,
                'grantable': 0, 'full': False,
                'principals': ['agent-nova'],
            },
            'holders': [],
            'governed_principals': ['agent-nova'],
            'eligible_principals': [],
        }
        agents = [
            {
                'username': 'nova',
                'email': 'nova@shizuha.com',
                'status': 'running',
                'enabled': True,
                'model': 'cortex/GLM-5.3-Flash',
                'effective_model': 'cortex/GLM-5.3-Flash',
            },
        ]
        tick = _reconcile_one_pool(
            cfg=self.cfg,
            agents=agents,
            email_to_teams={},
            workload_by_team={},
            workload_by_email={'nova@shizuha.com': {'ready': 1}},
            unread_by_email={},
            alerts_by_email={},
            actionable_buckets=('ready',),
            client=self.client,
            now=1000.0,
            apply=True,
        )
        yielded = [p for _, p, _ in self.client.yielded]
        self.assertIn('agent-nova', yielded)
        self.assertIn('YIELD leftover principal=agent-nova', ' '.join(tick['log']))

    def test_empty_metal_pool_still_yields_converted_resident(self):
        """Empty pool after remap used to skip leftover yield entirely."""
        cache.delete(LEFTOVER_HOME_YIELD_CACHE_KEY)
        self.cfg['model_id'] = 'DeepSeek-V4-Flash-Vision-Metal'
        self.cfg['members'] = []
        self.client.data = {
            'max_holders': 1,
            'admission_capacity': 1,
            'residency': {
                'seats': 1, 'homes': 1, 'protected': 1,
                'grantable': 0, 'full': False,
                'principals': ['agent-nova'],
            },
            'holders': [],
            'governed_principals': ['agent-nova'],
            'eligible_principals': [],
        }
        agents = [
            {
                'username': 'nova',
                'email': 'nova@shizuha.com',
                'status': 'running',
                'enabled': True,
                'model': 'cortex/GLM-5.3-Flash',
                'effective_model': 'cortex/GLM-5.3-Flash',
            },
        ]
        tick = _reconcile_one_pool(
            cfg=self.cfg,
            agents=agents,
            email_to_teams={},
            workload_by_team={},
            workload_by_email={},
            unread_by_email={},
            alerts_by_email={},
            actionable_buckets=('ready',),
            client=self.client,
            now=1000.0,
            apply=True,
        )
        yielded = [p for _, p, _ in self.client.yielded]
        self.assertIn('agent-nova', yielded)
        self.assertIn('pool empty; yielded unpinned leftovers', ' '.join(tick['log']))


class ReservedMustHoldLeaseTests(SimpleTestCase):
    """CEO Office stays up by occupying a real holder slot (operator 2026-08-18)."""

    class Client:
        def __init__(self):
            self.data = {
                'max_holders': 1,
                'admission_capacity': 1,
                'residency': {
                    'seats': 1, 'homes': 1, 'protected': 1,
                    'grantable': 0, 'full': True,
                },
                'holders': [{
                    'principal': 'agent-saki',
                    'held_for_s': 100,
                    'idle_for_s': 40,
                }],
                'governed_principals': ['agent-saki', 'agent-yuna'],
                'eligible_principals': ['agent-saki'],
            }
            self.grants = []
            self.revokes = []
            self.yielded = []

        def board(self, _model):
            return self.data

        def set_governed(self, _model, principals):
            self.data['governed_principals'] = sorted(principals)
            return True

        def set_eligible(self, _model, principals):
            self.data['eligible_principals'] = sorted(principals)
            return True

        def grant(self, _model, holder, **_kwargs):
            self.grants.append(holder)
            holders = self.data.setdefault('holders', [])
            if not any(h.get('principal') == holder for h in holders):
                holders.append({'principal': holder, 'held_for_s': 0, 'idle_for_s': 0})
            return True

        def revoke(self, _model, *, holder, **_kwargs):
            self.revokes.append(holder)
            self.data['holders'] = [
                h for h in (self.data.get('holders') or [])
                if h.get('principal') != holder
            ]
            return True

        def yield_homes(self, model, principal, *, reason=''):
            self.yielded.append((model, principal, reason))
            return True

    def setUp(self):
        cache.delete(HISTORY_CACHE_KEY)
        cache.delete(HOLDINGS_CACHE_KEY)
        cache.delete(PREEMPT_RETRY_CACHE_KEY)
        self.client = self.Client()
        self.cfg = {
            'enabled': True,
            'model_id': 'DeepSeek-V4-Flash',
            'members': ['saki', 'yuna'],
            'min_hold_seconds': MIN_HOLD,
            'max_hold_seconds': MAX_HOLD,
            'idle_release_seconds': IDLE,
            'max_holders': 1,
        }

    def tearDown(self):
        cache.delete(HISTORY_CACHE_KEY)
        cache.delete(HOLDINGS_CACHE_KEY)
        cache.delete(PREEMPT_RETRY_CACHE_KEY)

    def test_reserved_preempts_nonreserved_and_holds_lease(self):
        agents = [
            {'username': 'saki', 'email': 'saki@shizuha.com', 'status': 'running', 'enabled': True},
            {'username': 'yuna', 'email': 'yuna@shizuha.com', 'status': 'running', 'enabled': True,
             'warm_pool_size': 1},
        ]
        tick = _reconcile_one_pool(
            cfg=self.cfg,
            agents=agents,
            email_to_teams={'yuna@shizuha.com': {'ceo-office'}},
            workload_by_team={},
            workload_by_email={'saki@shizuha.com': {'ready': 1}},
            unread_by_email={},
            alerts_by_email={},
            actionable_buckets=('ready',),
            client=self.client,
            now=1000.0,
            apply=True,
        )
        self.assertEqual(self.client.revokes, ['agent-saki'])
        self.assertEqual(self.client.grants, ['agent-yuna'])
        self.assertIn('agent-yuna', tick['holders'])
        self.assertNotIn('agent-saki', tick['holders'])
        self.assertTrue(tick['scale']['yuna'])
        self.assertFalse(tick['scale']['saki'])
        self.assertNotIn(
            ('DeepSeek-V4-Flash', 'agent-yuna', 'running-without-lease'),
            self.client.yielded,
        )
        self.assertIn('GRANT holder=agent-yuna reason=keep-resident', ' '.join(tick['log']))
        self.assertEqual(cache.get(PREEMPT_RETRY_CACHE_KEY), {'saki': 1000.0})

    def test_preempt_retry_wins_the_next_free_slot(self):
        """After Yuna steals the seat, Saki is the next personal fill — not Nori."""
        cache.set(PREEMPT_RETRY_CACHE_KEY, {'saki': 900.0}, None)
        self.client.data = {
            'max_holders': 2,
            'admission_capacity': 3,
            'residency': {
                'seats': 3, 'homes': 2, 'protected': 1,
                'grantable': 1, 'full': False,
            },
            'holders': [{
                'principal': 'agent-yuna',
                'held_for_s': 10,
                'idle_for_s': 0,
            }],
            'governed_principals': ['agent-saki', 'agent-yuna', 'agent-nori'],
            'eligible_principals': ['agent-saki', 'agent-yuna', 'agent-nori'],
        }
        self.cfg['max_holders'] = 2
        self.cfg['members'] = ['saki', 'yuna', 'nori']
        agents = [
            {'username': 'saki', 'email': 'saki@shizuha.com', 'status': 'hibernated', 'enabled': False},
            {'username': 'nori', 'email': 'nori@shizuha.com', 'status': 'hibernated', 'enabled': False},
            {'username': 'yuna', 'email': 'yuna@shizuha.com', 'status': 'running', 'enabled': True,
             'warm_pool_size': 1},
        ]
        cache.set(HISTORY_CACHE_KEY, {'saki': 800.0, 'nori': 0.0}, None)
        tick = _reconcile_one_pool(
            cfg=self.cfg,
            agents=agents,
            email_to_teams={'yuna@shizuha.com': {'ceo-office'}},
            workload_by_team={},
            workload_by_email={
                'saki@shizuha.com': {'ready': 1},
                'nori@shizuha.com': {'ready': 1},
            },
            unread_by_email={},
            alerts_by_email={},
            actionable_buckets=('ready',),
            client=self.client,
            now=2000.0,
            apply=True,
        )
        self.assertEqual(self.client.grants, ['agent-saki'])
        self.assertEqual(set(tick['holders']), {'agent-yuna', 'agent-saki'})
        self.assertNotIn('saki', cache.get(PREEMPT_RETRY_CACHE_KEY) or {})

    def test_preempt_victim_skips_reserved_and_active(self):
        victim = reserved_preempt_victim(
            [
                {'principal': 'agent-yuna', 'idle_for_s': 500, 'held_for_s': 500},
                {'principal': 'agent-saki', 'idle_for_s': 10, 'held_for_s': 100},
                {'principal': 'agent-kai', 'idle_for_s': 80, 'held_for_s': 80},
            ],
            {'yuna'},
            active={'kai'},
        )
        self.assertEqual(victim, 'agent-saki')

    def test_unused_keep_resident_already_holding_does_not_preempt(self):
        """Live 2026-08-18: unused-revoke Yuna then preempt Saki to re-grant Yuna.

        Once she already occupies her honest seat, keep it. Do not shuffle a
        personal-work Engineering holder off the board so Yuna can sit unused
        on a freshly stolen slot.
        """
        self.client.data = {
            'max_holders': 2,
            'admission_capacity': 3,
            'residency': {
                'seats': 3, 'homes': 2, 'protected': 2,
                'grantable': 0, 'full': True,
            },
            'holders': [
                {
                    'principal': 'agent-saki',
                    'held_for_s': 100,
                    'idle_for_s': 40,
                },
                {
                    'principal': 'agent-yuna',
                    'held_for_s': 522,
                    'idle_for_s': 522,
                },
            ],
            'governed_principals': ['agent-saki', 'agent-yuna'],
            'eligible_principals': ['agent-saki', 'agent-yuna'],
        }
        self.cfg['max_holders'] = 2
        agents = [
            {'username': 'saki', 'email': 'saki@shizuha.com', 'status': 'running', 'enabled': True},
            {'username': 'yuna', 'email': 'yuna@shizuha.com', 'status': 'running', 'enabled': True,
             'warm_pool_size': 1},
        ]
        tick = _reconcile_one_pool(
            cfg=self.cfg,
            agents=agents,
            email_to_teams={'yuna@shizuha.com': {'ceo-office'}},
            workload_by_team={},
            workload_by_email={'saki@shizuha.com': {'ready': 1}},
            unread_by_email={},
            alerts_by_email={},
            actionable_buckets=('ready',),
            client=self.client,
            now=1000.0,
            apply=True,
        )
        self.assertEqual(self.client.revokes, [])
        self.assertEqual(self.client.grants, [])
        self.assertEqual(set(tick['holders']), {'agent-saki', 'agent-yuna'})
        self.assertTrue(tick['scale']['saki'])
        self.assertTrue(tick['scale']['yuna'])
        joined = ' '.join(tick['log'])
        self.assertIn('keep-resident occupies an honest seat', joined)
        self.assertNotIn('preempt-for-keep-resident', joined)


class ResidencyAwareMaxHoldersTests(SimpleTestCase):
    def test_hive_admission_cap_is_ninety_five_percent(self):
        self.assertEqual(hive_admission_cap(21), 19)  # floor, not round(19.95)=20
        self.assertEqual(hive_admission_cap(20), 19)
        self.assertEqual(hive_admission_cap(16), 15)
        self.assertEqual(hive_admission_cap(8), 7)
        self.assertEqual(hive_admission_cap(6), 5)
        self.assertEqual(hive_admission_cap(2), 1)
        self.assertEqual(hive_admission_cap(1), 1)
        self.assertEqual(hive_admission_cap(0), 0)

    def test_hive_admission_cap_leaves_tui_seat_when_fleet_has_room(self):
        """floor(95%) never consumes every seat on a ≥2-seat GLM fleet."""
        for seats in range(2, 33):
            cap = hive_admission_cap(seats)
            self.assertLess(cap, seats, seats)
            self.assertEqual(
                cap,
                min(math.floor(seats * 0.95), seats - 1),
                seats,
            )

    def test_raises_toward_ninety_five_percent_when_waiters_exist(self):
        board = {
            'max_holders': 4,
            'admission_capacity': 8,
            'residency': {'seats': 8, 'protected': 3, 'grantable': 5, 'full': False},
        }
        self.assertEqual(
            residency_aware_max_holders(board, waiter_count=13, holder_count=4),
            7,
        )

    def test_cannot_raise_to_full_cortex_admission(self):
        board = {
            'max_holders': 18,
            'admission_capacity': 20,
            'residency': {'seats': 20, 'protected': 18, 'grantable': 2, 'full': False},
        }
        self.assertEqual(
            residency_aware_max_holders(board, waiter_count=13, holder_count=18),
            19,
        )

    def test_shrinks_leftover_hundred_percent_board(self):
        board = {
            'max_holders': 20,
            'admission_capacity': 20,
            'residency': {'seats': 20, 'grantable': 0, 'full': True},
        }
        self.assertEqual(
            residency_aware_max_holders(board, waiter_count=0, holder_count=20),
            19,
        )

    def test_stops_when_every_seat_is_protected(self):
        board = {
            'max_holders': 4,
            'admission_capacity': 8,
            'residency': {'seats': 8, 'protected': 8, 'grantable': 0, 'full': True},
        }
        self.assertEqual(
            residency_aware_max_holders(board, waiter_count=13, holder_count=4),
            4,
        )

    def test_does_not_raise_without_waiters(self):
        board = {
            'max_holders': 4,
            'admission_capacity': 8,
            'residency': {'grantable': 5, 'full': False},
        }
        self.assertEqual(
            residency_aware_max_holders(board, waiter_count=0, holder_count=4),
            4,
        )

    def test_overflow_victim_is_idlest_non_active(self):
        holders = [
            {'principal': 'agent-rei', 'idle_for_s': 10},
            {'principal': 'agent-sato', 'idle_for_s': 90},
            {'principal': 'agent-zen', 'idle_for_s': 5},
        ]
        self.assertEqual(
            _overflow_revoke_victim(holders, active={'sato'}, reserved=set()),
            'agent-rei',
        )

    def test_overflow_victim_falls_back_when_every_holder_is_active(self):
        holders = [
            {'principal': 'agent-ichi', 'idle_for_s': 16},
            {'principal': 'agent-nagi', 'idle_for_s': 290},
            {'principal': 'agent-kumo', 'idle_for_s': 1},
        ]
        self.assertEqual(
            _overflow_revoke_victim(
                holders,
                active={'ichi', 'nagi', 'kumo'},
                reserved=set(),
            ),
            'agent-nagi',
        )

    def test_overflow_victim_prefers_homeless_over_idlest_homed(self):
        holders = [
            {'principal': 'agent-nagi', 'idle_for_s': 400},
            {'principal': 'agent-kei', 'idle_for_s': 10},
        ]
        self.assertEqual(
            _overflow_revoke_victim(
                holders,
                active=set(),
                reserved=set(),
                residents={'agent-nagi'},
            ),
            'agent-kei',
        )


_LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}


@override_settings(CACHES=_LOCMEM)
class FullResidencyFillTests(SimpleTestCase):
    class Client:
        def __init__(self):
            self.data = {
                'max_holders': 8,
                'admission_capacity': 8,
                'residency': {
                    'seats': 8, 'homes': 8, 'protected': 8,
                    'grantable': 0, 'full': True,
                    'principals': ['agent-kai', 'agent-nagi'],
                },
                'holders': [
                    {'principal': 'agent-kai', 'held_for_s': 10, 'idle_for_s': 0},
                    {'principal': 'agent-nagi', 'held_for_s': 10, 'idle_for_s': 0},
                    {'principal': 'agent-ichi', 'held_for_s': 10, 'idle_for_s': 0},
                    {'principal': 'agent-mio', 'held_for_s': 10, 'idle_for_s': 0},
                ],
                'governed_principals': [
                    'agent-kai', 'agent-nagi', 'agent-ichi', 'agent-mio', 'agent-haru',
                ],
                'eligible_principals': [
                    'agent-kai', 'agent-nagi', 'agent-ichi', 'agent-mio', 'agent-haru',
                ],
            }
            self.grants = []
            self.yielded = []

        def board(self, _model):
            return self.data

        def set_governed(self, _model, principals):
            self.data['governed_principals'] = sorted(principals)
            return True

        def set_eligible(self, _model, principals):
            self.data['eligible_principals'] = sorted(principals)
            return True

        def grant(self, _model, holder, **_kwargs):
            self.grants.append(holder)
            return True

        def revoke(self, _model, **_kwargs):
            return True

        def yield_homes(self, model, principal, *, reason=''):
            self.yielded.append((model, principal, reason))
            return True

    def setUp(self):
        cache.delete(HISTORY_CACHE_KEY)
        cache.delete(HOLDINGS_CACHE_KEY)
        self.client = self.Client()
        self.cfg = {
            'enabled': True,
            'model_id': 'DeepSeek-V4-Flash',
            'members': ['kai', 'nagi', 'ichi', 'mio', 'haru'],
            'min_hold_seconds': MIN_HOLD,
            'max_hold_seconds': MAX_HOLD,
            'idle_release_seconds': IDLE,
            'max_holders': 8,
        }

    def tearDown(self):
        cache.delete(HISTORY_CACHE_KEY)
        cache.delete(HOLDINGS_CACHE_KEY)

    def test_full_residency_does_not_fill_nonresident_waiters(self):
        """8 protected homes + 4 leases must not wake haru into residency_full."""
        agents = [
            {'username': u, 'email': f'{u}@shizuha.com', 'status': 'hibernated', 'enabled': False}
            for u in ('kai', 'nagi', 'ichi', 'mio', 'haru')
        ]
        tick = _reconcile_one_pool(
            cfg=self.cfg,
            agents=agents,
            email_to_teams={},
            workload_by_team={},
            workload_by_email={'haru@shizuha.com': {'ready': 1}},
            unread_by_email={},
            alerts_by_email={},
            actionable_buckets=('ready',),
            client=self.client,
            now=1000.0,
            apply=True,
        )
        self.assertIn('FILL none reason=kv_residency_full', ' '.join(tick['log']))
        self.assertEqual(self.client.grants, [])

    def test_running_homeless_waiter_is_scaled_off_when_residency_full(self):
        """Active-grace must not keep haru up while 8/8 homes are protected."""
        cache.delete('fleet:model-lease-nonholder-active:deepseek-v4-flash')
        agents = [
            {
                'username': u,
                'email': f'{u}@shizuha.com',
                'status': 'running',
                'enabled': True,
                'activity_state': 'working',
                'telemetry_ts': 1000,
            }
            for u in ('kai', 'nagi', 'ichi', 'mio', 'haru')
        ]
        tick = _reconcile_one_pool(
            cfg=self.cfg,
            agents=agents,
            email_to_teams={},
            workload_by_team={},
            workload_by_email={'haru@shizuha.com': {'ready': 1}},
            unread_by_email={},
            alerts_by_email={},
            actionable_buckets=('ready',),
            client=self.client,
            now=1000.0,
            apply=True,
        )
        log = ' '.join(tick['log'])
        self.assertIn('SCALE off principal=agent-haru reason=kv_residency_full', log)
        self.assertFalse(tick['scale'].get('haru'))
        self.assertTrue(tick['scale'].get('kai'))
        self.assertEqual(self.client.grants, [])
        cache.delete('fleet:model-lease-nonholder-active:deepseek-v4-flash')

    def test_running_idle_leftover_resident_home_is_yielded_when_residency_full(self):
        """Idle leftover residents occupy the unleased 10% without a holder.

        yield-homes HTTP 200 with count=0 used to cache success and leave
        riku/yuma on the KV board (2026-09-01). Idle leftovers must still
        POST yield; in-flight leftovers keep the book.
        """
        cache.delete('fleet:model-lease-nonholder-active:deepseek-v4-flash')
        self.cfg['members'] = ['kai', 'nagi', 'ichi', 'mio', 'riku']
        self.client.data['residency']['principals'] = [
            'agent-kai', 'agent-nagi', 'agent-ichi', 'agent-mio', 'agent-riku',
        ]
        self.client.data['governed_principals'] = [
            'agent-kai', 'agent-nagi', 'agent-ichi', 'agent-mio', 'agent-riku',
        ]
        self.client.data['eligible_principals'] = list(
            self.client.data['governed_principals'],
        )
        agents = [
            {
                'username': u,
                'email': f'{u}@shizuha.com',
                'status': 'running',
                'enabled': True,
                'activity_state': 'idle' if u == 'riku' else 'working',
                'telemetry_ts': 1000,
            }
            for u in ('kai', 'nagi', 'ichi', 'mio', 'riku')
        ]
        tick = _reconcile_one_pool(
            cfg=self.cfg,
            agents=agents,
            email_to_teams={},
            workload_by_team={},
            workload_by_email={'kai@shizuha.com': {'ready': 1}},
            unread_by_email={},
            alerts_by_email={},
            actionable_buckets=('ready',),
            client=self.client,
            now=1000.0,
            apply=True,
        )
        self.assertFalse(tick['scale'].get('riku'))
        yielded = [p for _, p, _ in self.client.yielded]
        self.assertNotIn('agent-riku', yielded)
        cache.delete('fleet:model-lease-nonholder-active:deepseek-v4-flash')


class RaiseMaxHoldersReconcileTests(SimpleTestCase):
    class Client:
        def __init__(self):
            self.data = {
                'max_holders': 1,
                'admission_capacity': 4,
                'residency': {
                    'seats': 4, 'homes': 1, 'protected': 0,
                    'grantable': 4, 'full': False,
                },
                'holders': [{'principal': 'agent-kai', 'held_for_s': 10, 'idle_for_s': 0}],
                'governed_principals': ['agent-kai', 'agent-nagi', 'agent-sara'],
                'eligible_principals': ['agent-kai', 'agent-nagi', 'agent-sara'],
            }
            self.grants = []
            self.grant_max_holders = []

        def board(self, _model):
            return self.data

        def set_governed(self, _model, principals):
            self.data['governed_principals'] = sorted(principals)
            return True

        def set_eligible(self, _model, principals):
            self.data['eligible_principals'] = sorted(principals)
            return True

        def grant(self, _model, holder, **kwargs):
            self.grants.append(holder)
            self.grant_max_holders.append(kwargs.get('max_holders'))
            self.data.setdefault('holders', []).append({'principal': holder})
            return True

        def revoke(self, _model, **_kwargs):
            return True

    def setUp(self):
        cache.delete(HISTORY_CACHE_KEY)
        cache.delete(HOLDINGS_CACHE_KEY)
        self.client = self.Client()
        self.cfg = {
            'enabled': True,
            'model_id': 'DeepSeek-V4-Flash',
            'members': ['kai', 'nagi', 'sara'],
            'min_hold_seconds': MIN_HOLD,
            'max_hold_seconds': MAX_HOLD,
            'idle_release_seconds': IDLE,
            'max_holders': 1,
        }

    def tearDown(self):
        cache.delete(HISTORY_CACHE_KEY)
        cache.delete(HOLDINGS_CACHE_KEY)

    def test_reconcile_raises_and_fills_waiters(self):
        agents = [
            {'username': 'kai', 'email': 'kai@shizuha.com', 'status': 'running', 'enabled': True,
             'activity_state': 'responding', 'telemetry_ts': 1000},
            {'username': 'nagi', 'email': 'nagi@shizuha.com', 'status': 'hibernated', 'enabled': False},
            {'username': 'sara', 'email': 'sara@shizuha.com', 'status': 'hibernated', 'enabled': False},
        ]
        tick = _reconcile_one_pool(
            cfg=self.cfg,
            agents=agents,
            email_to_teams={},
            workload_by_team={},
            workload_by_email={
                'kai@shizuha.com': {'ready': 1},
                'nagi@shizuha.com': {'ready': 1},
                'sara@shizuha.com': {'ready': 1},
            },
            unread_by_email={},
            alerts_by_email={},
            actionable_buckets=('ready',),
            client=self.client,
            now=1000.0,
            apply=True,
        )
        self.assertIn('RAISE max_holders 1->3', ' '.join(tick['log']))
        self.assertCountEqual(self.client.grants, ['agent-nagi', 'agent-sara'])
        self.assertTrue(all(n == 3 for n in self.client.grant_max_holders))

    def test_unleased_cortex_model_still_starts_one_worthy_holder(self):
        """Qwen 2026-08-22: Cortex CORTEX_LEASED_MODELS omitted the model so
        every GRANT 400'd and Hive hibernated documentation/review/QA/security
        as 'does not hold the model lease' while the 1-admission backend sat
        idle. The request gate still serves unleased models; Hive must start
        the worthy holder without a board write."""
        self.client.data = {
            'leased': False,
            'max_holders': 1,
            'admission_capacity': 1,
            'residency': {
                'seats': 1, 'homes': 0, 'protected': 0,
                'grantable': 1, 'full': False,
            },
            'holders': [],
            'governed_principals': [],
            'eligible_principals': [],
        }
        self.cfg['model_id'] = 'Qwen3.8-27B-Q2'
        self.cfg['members'] = ['revi', 'yuki', 'nami']
        agents = [
            {'username': 'revi', 'email': 'revi@shizuha.com', 'status': 'hibernated', 'enabled': False},
            {'username': 'yuki', 'email': 'yuki@shizuha.com', 'status': 'hibernated', 'enabled': False},
            {'username': 'nami', 'email': 'nami@shizuha.com', 'status': 'hibernated', 'enabled': False},
        ]
        tick = _reconcile_one_pool(
            cfg=self.cfg,
            agents=agents,
            email_to_teams={'revi@shizuha.com': {'security'}},
            workload_by_team={},
            workload_by_email={'revi@shizuha.com': {'ready': 2}, 'yuki@shizuha.com': {'ready': 1}},
            unread_by_email={},
            alerts_by_email={},
            actionable_buckets=('ready',),
            client=self.client,
            now=1000.0,
            apply=True,
        )
        joined = ' '.join(tick['log'] + [str(tick.get('holders'))])
        self.assertIn('cortex is not leasing this model', joined)
        self.assertEqual(self.client.grants, [])
        self.assertTrue(tick['scale'].get('revi'))
        self.assertFalse(tick['scale'].get('yuki'))
        self.assertIn('agent-revi', tick['holders'])

    def test_queue_empty_working_telemetry_does_not_keep_the_qwen_seat(self):
        """Skip-model heartbeats stamp activity_state=working. That used to
        KEEP-active the 1 Qwen holder (reika) while revi sat hibernated with
        assigned security work (2026-08-22)."""
        self.client.data = {
            'leased': True,
            'max_holders': 1,
            'admission_capacity': 1,
            'residency': {
                'seats': 1, 'homes': 1, 'protected': 0,
                'grantable': 1, 'full': False,
            },
            'holders': [{
                'principal': 'agent-reika',
                'held_for_s': 400,
                'idle_for_s': 400,
            }],
            'governed_principals': ['agent-reika', 'agent-revi'],
            'eligible_principals': ['agent-reika', 'agent-revi'],
        }
        self.cfg['model_id'] = 'Qwen3.8-27B-Q2'
        self.cfg['members'] = ['reika', 'revi']
        self.cfg['max_holders'] = 1
        orig_revoke = self.client.revoke

        def _revoke(_model, **kwargs):
            holder = kwargs.get('holder')
            self.client.data['holders'] = [
                h for h in (self.client.data.get('holders') or [])
                if h.get('principal') != holder
            ]
            return orig_revoke(_model, **kwargs)

        self.client.revoke = _revoke
        agents = [
            {
                'username': 'reika', 'email': 'reika@shizuha.com',
                'status': 'running', 'enabled': True,
                'heartbeat_outcome': 'queue_empty',
                'activity_state': 'working', 'telemetry_ts': 1000,
            },
            {
                'username': 'revi', 'email': 'revi@shizuha.com',
                'status': 'hibernated', 'enabled': False,
            },
        ]
        tick = _reconcile_one_pool(
            cfg=self.cfg,
            agents=agents,
            email_to_teams={'revi@shizuha.com': {'security'}},
            workload_by_team={},
            workload_by_email={'revi@shizuha.com': {'ready': 4}},
            unread_by_email={},
            alerts_by_email={},
            actionable_buckets=('ready',),
            client=self.client,
            now=1000.0,
            apply=True,
        )
        joined = ' '.join(tick['log'])
        self.assertIn('REVOKE holder=agent-reika', joined)
        self.assertIn('GRANT holder=agent-revi', joined)
        self.assertIn('agent-revi', tick['holders'])
        self.assertNotIn('agent-reika', tick['holders'])
        self.assertTrue(tick['scale'].get('revi'))
        self.assertFalse(tick['scale'].get('reika'))


class PoolMembershipDbUnionTests(TestCase):
    """HIVE-1714: FleetAgent DB rows are the membership floor.

    While the rt-fleet daemon flapped (liveness-kill storm 2026-08-11) the
    lifecycle inventory shrank 44 -> 21 and the governed set at Cortex was
    synced down to the surviving subset each tick — 30 DeepSeek agents
    (including 4 admitted at that moment) escaped lease governance.  The DB
    is the config SoT for who runs which model; a partial runtime inventory
    may only ADD members, never subtract them.
    """

    def test_db_rows_survive_partial_runtime_inventory(self):
        from .models import FleetAgent
        FleetAgent.objects.create(
            agent_username='hiro', model='cortex/DeepSeek-V4-Flash',
            status=FleetAgent.Status.HIBERNATED,
        )
        FleetAgent.objects.create(
            agent_username='gone', model='cortex/DeepSeek-V4-Flash',
            status=FleetAgent.Status.DELETED,
        )
        configs = [{'model_id': 'DeepSeek-V4-Flash', 'members': []}]
        # Partial inventory: the daemon merge only returned nami this tick.
        agents = [{
            'agent_username': 'nami',
            'model': 'DeepSeek-V4-Flash',
            'runtime_source': 'rt-fleet',
        }]

        resolved = pool_configs_for_agents(configs, agents)

        self.assertEqual(resolved[0]['members'], ['hiro', 'nami'])


class LeasePresentationTests(SimpleTestCase):
    class Client:
        def __init__(self, board):
            self._board = board

        def board(self, _model):
            return self._board

    def test_projects_holder_and_waiter_from_cortex_board(self):
        agents = [
            {'agent_username': 'nagi', 'model': 'DeepSeek-V4-Flash'},
            {'agent_username': 'recon', 'model': 'cortex/DeepSeek-V4-Flash'},
        ]
        board = {
            'holder': {
                'principal': 'agent-nagi',
                'reason': 'personal-work',
                'held_for_s': 17,
                'expires_in_s': 88,
                'idle_for_s': 2,
            },
            'governed_principals': ['agent-nagi', 'agent-recon'],
            'eligible_principals': ['agent-nagi', 'agent-recon'],
        }

        projected = lease_status_by_username(
            agents=agents,
            client=self.Client(board),
            configs=[{
                'model_id': 'DeepSeek-V4-Flash',
                'members': ['stale-entry'],
            }],
        )

        self.assertEqual(projected['nagi']['state'], 'holder')
        self.assertTrue(projected['nagi']['is_holder'])
        self.assertEqual(projected['recon']['state'], 'waiting')
        self.assertEqual(projected['recon']['holder'], 'agent-nagi')
        self.assertEqual(projected['recon']['slots_free'], 0)

    def test_free_slots_eligible_is_not_waiting_behind_holder(self):
        """Worthy agents with free board slots are eligible, not queued."""
        agents = [
            {'agent_username': 'nagi', 'model': 'DeepSeek-V4-Flash'},
            {'agent_username': 'recon', 'model': 'DeepSeek-V4-Flash'},
        ]
        board = {
            'max_holders': 4,
            'holders': [{'principal': 'agent-nagi', 'held_for_s': 100}],
            'governed_principals': ['agent-nagi', 'agent-recon'],
            'eligible_principals': ['agent-nagi', 'agent-recon'],
        }
        projected = lease_status_by_username(
            agents=agents,
            client=self.Client(board),
            configs=[{
                'model_id': 'DeepSeek-V4-Flash',
                'members': ['stale-entry'],
            }],
        )
        self.assertEqual(projected['nagi']['state'], 'holder')
        self.assertEqual(projected['recon']['state'], 'eligible')
        self.assertIsNone(projected['recon']['holder'])
        self.assertEqual(projected['recon']['slots_free'], 3)

    def test_ineligible_with_free_slots_is_idle_not_waiting(self):
        """Blocked/scheduled-only agents are not worthy — idle, not behind @X."""
        agents = [
            {'agent_username': 'nagi', 'model': 'DeepSeek-V4-Flash'},
            {'agent_username': 'banto', 'model': 'DeepSeek-V4-Flash'},
        ]
        board = {
            'max_holders': 14,
            'holders': [{'principal': 'agent-nagi', 'held_for_s': 50}],
            'governed_principals': ['agent-nagi', 'agent-banto'],
            'eligible_principals': ['agent-nagi'],  # banto not worthy
        }
        projected = lease_status_by_username(
            agents=agents,
            client=self.Client(board),
            configs=[{
                'model_id': 'DeepSeek-V4-Flash',
                'members': ['stale-entry'],
            }],
        )
        self.assertEqual(projected['banto']['state'], 'idle')
        self.assertFalse(projected['banto']['eligible'])
        self.assertIsNone(projected['banto']['holder'])
        self.assertEqual(projected['banto']['slots_free'], 13)

    def test_board_failure_is_explicit_unknown(self):
        agents = [{'agent_username': 'nagi', 'model': 'DeepSeek-V4-Flash'}]

        projected = lease_status_by_username(
            agents=agents,
            client=self.Client(None),
            configs=[{
                'model_id': 'DeepSeek-V4-Flash',
                'members': ['stale-entry'],
            }],
        )

        self.assertEqual(projected['nagi']['state'], 'unknown')
        self.assertIsNone(projected['nagi']['governed'])

    def test_explicit_stop_is_presented_as_disabled_not_waiting(self):
        projected = lease_status_by_username(
            agents=[{
                'agent_username': 'recon',
                'model': 'DeepSeek-V4-Flash',
                'status': 'stopped',
            }],
            client=self.Client({
                'holder': {'principal': 'agent-nagi'},
                'governed_principals': ['agent-recon'],
                'eligible_principals': [],
            }),
            configs=[{
                'model_id': 'DeepSeek-V4-Flash',
                'members': ['recon'],
            }],
        )

        self.assertEqual(projected['recon']['state'], 'disabled')

    def test_zero_admission_pool_does_not_overwrite_deepseek_holder(self):
        class MultiClient:
            def board(self, model):
                if 'Qwen' in str(model):
                    return {
                        'admission_capacity': 0,
                        'max_holders': 1,
                        'holders': [],
                        'governed_principals': ['agent-nami'],
                        'eligible_principals': ['agent-nami'],
                    }
                return {
                    'admission_capacity': 8,
                    'max_holders': 8,
                    'holders': [{'principal': 'agent-nami', 'held_for_s': 40}],
                    'governed_principals': ['agent-nami'],
                    'eligible_principals': ['agent-nami'],
                }

        projected = lease_status_by_username(
            agents=[],
            client=MultiClient(),
            configs=[
                {'model_id': 'DeepSeek-V4-Flash', 'members': ['nami']},
                {'model_id': 'Qwen3.8-27B-Q2', 'members': ['nami']},
            ],
        )
        self.assertEqual(projected['nami']['model'], 'DeepSeek-V4-Flash')
        self.assertEqual(projected['nami']['state'], 'holder')
        self.assertTrue(projected['nami']['is_holder'])

    @patch('fleet.model_lease.lease_status_by_username')
    def test_staff_read_model_gets_lease_projection(self, lease_status):
        lease_status.return_value = {
            'nagi': {'managed': True, 'state': 'holder', 'is_holder': True},
        }
        agents = [{'agent_username': 'nagi'}, {'agent_username': 'kai'}]

        attached = _attach_model_lease_status(agents)

        self.assertEqual(attached[0]['model_lease']['state'], 'holder')
        self.assertNotIn('model_lease', attached[1])

    @patch('fleet.model_lease.lease_status_by_username', side_effect=RuntimeError('offline'))
    def test_staff_read_model_survives_cortex_observation_failure(self, _lease_status):
        agents = [{'agent_username': 'nagi'}]

        self.assertEqual(_attach_model_lease_status(agents), agents)


class WorthinessTests(SimpleTestCase):
    """Hive must not grant sprints to idle agents (operator 2026-07-28+)."""

    def test_awaiting_deploy_is_not_an_open_workload_bucket(self):
        """Kei 2026-09-15: awaiting_deploy was falling through to open, so
        merge seats looked assigned_tasks while heartbeats saw 0 ready."""
        view = FleetPulseSnapshotView()
        self.assertEqual(view._bucket({'status': 'awaiting_deploy'}), 'in_review')
        self.assertEqual(view._bucket({'status': 'awaiting_merge'}), 'in_review')
        self.assertEqual(view._bucket({'status': 'open'}), 'open')

    def test_assigned_tasks_are_personal_worthy(self):
        ok, reason, personal = agent_lease_worthiness(
            email='nami@shizuha.com', teams=('qa',),
            workload_by_team={}, workload_by_email={'nami@shizuha.com': {'ready': 2}},
            unread_by_email={}, alerts_by_email={},
            actionable_buckets=('ready', 'in_progress'),
        )
        self.assertEqual((ok, reason, personal), (True, 'assigned_tasks', True))

    def test_unread_dm_is_personal_worthy(self):
        ok, reason, personal = agent_lease_worthiness(
            email='rui@shizuha.com', teams=('review',),
            workload_by_team={}, workload_by_email={},
            unread_by_email={'rui@shizuha.com': 1}, alerts_by_email={},
            actionable_buckets=('ready',),
        )
        self.assertEqual((ok, reason, personal), (True, 'unread_dm', True))

    def test_assigned_alert_is_personal_worthy(self):
        ok, reason, personal = agent_lease_worthiness(
            email='kai@shizuha.com', teams=('engineering',),
            workload_by_team={}, workload_by_email={},
            unread_by_email={}, alerts_by_email={'kai@shizuha.com': 3},
            actionable_buckets=('ready',),
            heartbeat_outcome='queue_empty',
            agent_status='hibernated',
        )
        self.assertEqual((ok, reason, personal), (True, 'assigned_alert', True))

    def test_assigned_alert_matches_agent_email_alias(self):
        ok, reason, personal = agent_lease_worthiness(
            email='kai@shizuha.com', teams=('engineering',),
            workload_by_team={}, workload_by_email={},
            unread_by_email={},
            alerts_by_email={'kai@agents.shizuha.io': 2},
            actionable_buckets=('ready',),
            heartbeat_outcome='queue_empty',
            agent_status='hibernated',
        )
        self.assertEqual((ok, reason, personal), (True, 'assigned_alert', True))

    def test_team_queue_still_wakes_without_personal_work(self):
        ok, reason, personal = agent_lease_worthiness(
            email='stopped@shizuha.com', teams=('qa',),
            workload_by_team={'qa': {'ready': 3}}, workload_by_email={},
            unread_by_email={}, alerts_by_email={},
            actionable_buckets=('ready',),
        )
        self.assertEqual((ok, reason, personal), (True, 'team_queue', False))

    def test_running_idle_waste_is_not_team_queue_worthy(self):
        ok, reason, personal = agent_lease_worthiness(
            email='sara@shizuha.com', teams=('engineering',),
            workload_by_team={'engineering': {'ready': 4}},
            workload_by_email={},
            unread_by_email={}, alerts_by_email={},
            actionable_buckets=('ready', 'in_progress'),
            heartbeat_outcome='',
            agent_status='running',
            activity_phase='idle',
            activity_phase_for_ms=270_000,
            lease_idle_waste=True,
        )
        self.assertEqual((ok, reason, personal), (False, 'nonproductive_heartbeat', False))

    def test_running_queue_empty_heartbeat_is_not_team_queue_worthy(self):
        ok, reason, personal = agent_lease_worthiness(
            email='hiro@shizuha.com', teams=('architecture',),
            workload_by_team={'architecture': {'in_progress': 6}},
            workload_by_email={},
            unread_by_email={}, alerts_by_email={},
            actionable_buckets=('ready', 'in_progress'),
            heartbeat_outcome='queue_empty',
            agent_status='running',
        )
        self.assertEqual((ok, reason, personal), (False, 'nonproductive_heartbeat', False))

    def test_running_all_blocked_heartbeat_is_not_team_queue_worthy(self):
        ok, reason, personal = agent_lease_worthiness(
            email='kai@shizuha.com', teams=('engineering',),
            workload_by_team={'engineering': {'in_progress': 4}},
            workload_by_email={},
            unread_by_email={}, alerts_by_email={},
            actionable_buckets=('ready', 'in_progress'),
            heartbeat_outcome='all_blocked',
            agent_status='running',
        )
        self.assertFalse(ok)
        self.assertEqual(reason, 'nonproductive_heartbeat')

    def test_hibernated_queue_empty_still_wakes_on_team_queue(self):
        """Starvation trap: a scaled-to-zero seat must still be grantable."""
        ok, reason, personal = agent_lease_worthiness(
            email='hiro@shizuha.com', teams=('architecture',),
            workload_by_team={'architecture': {'ready': 2}},
            workload_by_email={},
            unread_by_email={}, alerts_by_email={},
            actionable_buckets=('ready', 'in_progress'),
            heartbeat_outcome='queue_empty',
            agent_status='hibernated',
        )
        self.assertEqual((ok, reason, personal), (True, 'team_queue', False))

    def test_hibernated_wakes_on_unseated_work_of_dead_teammate(self):
        ok, reason, personal = agent_lease_worthiness(
            email='rei@shizuha.com', teams=('review',),
            workload_by_team={'review': {'in_progress': 4}},
            workload_by_email={},
            unread_by_email={}, alerts_by_email={},
            actionable_buckets=('ready', 'open', 'todo', 'doing', 'in_progress'),
            agent_status='hibernated',
            unseated_workload_by_team={'review': {'in_progress': 4}},
        )
        self.assertEqual((ok, reason, personal), (True, 'team_queue', False))

    def test_hibernated_wakes_on_team_needs_triage(self):
        ok, reason, personal = agent_lease_worthiness(
            email='tora@shizuha.com', teams=('engineering',),
            workload_by_team={'engineering': {'needs_triage': 4}},
            workload_by_email={},
            unread_by_email={}, alerts_by_email={},
            actionable_buckets=('ready', 'open', 'todo', 'doing', 'needs_triage', 'in_progress'),
            heartbeat_outcome='queue_empty',
            agent_status='hibernated',
        )
        self.assertEqual((ok, reason, personal), (True, 'team_queue', False))

    def test_crashloop_holder_is_not_running(self):
        self.assertFalse(_agent_dict_running({
            'status': 'running', 'enabled': True, 'runtime_ready': False,
        }))
        self.assertTrue(_agent_dict_running({
            'status': 'running', 'enabled': True,
        }))

    def test_hibernated_does_not_wake_on_teammate_in_progress_only(self):
        ok, reason, personal = agent_lease_worthiness(
            email='nova@shizuha.com', teams=('qa',),
            workload_by_team={'qa': {'in_progress': 4}},
            workload_by_email={},
            unread_by_email={}, alerts_by_email={},
            actionable_buckets=('ready', 'in_progress'),
            heartbeat_outcome='queue_empty',
            agent_status='hibernated',
        )
        self.assertFalse(ok)
        self.assertEqual((reason, personal), ('', False))

    def test_running_needs_help_no_snapshot_yields_even_with_open(self):
        """Kumo 2026-09-15: 167k silent/echo-done heartbeats, outcome needs_help
        ready=0, Pulse still listed EVOL-1 open so assigned_tasks KEEP."""
        ok, reason, personal = agent_lease_worthiness(
            email='kumo@shizuha.com', teams=('architecture',),
            workload_by_team={},
            workload_by_email={'kumo@shizuha.com': {'open': 1, 'in_progress': 3}},
            unread_by_email={}, alerts_by_email={},
            actionable_buckets=('ready', 'open', 'todo', 'in_progress'),
            heartbeat_outcome='needs_help',
            agent_status='running',
        )
        self.assertEqual((ok, reason, personal), (False, 'nonproductive_heartbeat', False))

    def test_running_queue_empty_yields_even_with_pulse_ready(self):
        """Kei 2026-09-15: Hive assigned_tasks from a Pulse snapshot while
        pulse_get_my_work returned readyTaskCount=0. The in-pod look wins."""
        ok, reason, personal = agent_lease_worthiness(
            email='kei@shizuha.com', teams=('merge',),
            workload_by_team={},
            workload_by_email={'kei@shizuha.com': {'open': 6, 'ready': 1}},
            unread_by_email={}, alerts_by_email={},
            actionable_buckets=('ready', 'open', 'todo', 'in_progress'),
            heartbeat_outcome='queue_empty',
            agent_status='running',
        )
        self.assertEqual((ok, reason, personal), (False, 'nonproductive_heartbeat', False))

    def test_hibernated_ready_still_wakes_with_stale_queue_empty(self):
        """A scaled-to-zero seat has not looked this tenure — Pulse ready wakes."""
        ok, reason, personal = agent_lease_worthiness(
            email='sato@shizuha.com', teams=('platform',),
            workload_by_team={},
            workload_by_email={'sato@shizuha.com': {'ready': 1}},
            unread_by_email={}, alerts_by_email={},
            actionable_buckets=('ready', 'in_progress'),
            heartbeat_outcome='queue_empty',
            agent_status='hibernated',
        )
        self.assertEqual((ok, reason, personal), (True, 'assigned_tasks', True))

    def test_running_idle_doing_assignment_is_not_lease_worthy(self):
        """Yuki 2026-09-02: Hive '1 doing' + idle 'No ready work' must yield.

        `doing` used to short-circuit idle-waste KEEP (assigned_tasks) so a
        documentation seat held GLM while pulse_get_my_tasks was absent and
        every heartbeat burned a 90k–180k turn with 0 tools.
        """
        ok, reason, personal = agent_lease_worthiness(
            email='yuki@shizuha.com', teams=('documentation',),
            workload_by_team={},
            workload_by_email={'yuki@shizuha.com': {'doing': 1}},
            unread_by_email={}, alerts_by_email={},
            actionable_buckets=('ready', 'open', 'todo', 'doing', 'in_progress'),
            heartbeat_outcome='queue_empty',
            agent_status='running',
            activity_phase='idle',
            activity_phase_for_ms=270_000,
            lease_idle_waste=True,
        )
        self.assertEqual((ok, reason, personal), (False, 'nonproductive_heartbeat', False))

    def test_hibernated_doing_assignment_does_not_wake(self):
        """A stale Pulse 'doing' row must not re-wake a yielded seat."""
        ok, reason, personal = agent_lease_worthiness(
            email='yuki@shizuha.com', teams=('documentation',),
            workload_by_team={},
            workload_by_email={'yuki@shizuha.com': {'doing': 1}},
            unread_by_email={}, alerts_by_email={},
            actionable_buckets=('ready', 'open', 'todo', 'doing', 'in_progress'),
            heartbeat_outcome='queue_empty',
            agent_status='hibernated',
        )
        self.assertEqual((ok, reason, personal), (False, '', False))

    def test_running_queue_empty_does_not_keep_in_progress_assignment(self):
        """Hiro/rei 2026-08-21: in_progress on the snapshot is not runnable work."""
        ok, reason, personal = agent_lease_worthiness(
            email='hiro@shizuha.com', teams=('architecture',),
            workload_by_team={},
            workload_by_email={'hiro@shizuha.com': {'in_progress': 2}},
            unread_by_email={}, alerts_by_email={},
            actionable_buckets=('ready', 'in_progress'),
            heartbeat_outcome='queue_empty',
            agent_status='running',
        )
        self.assertEqual((ok, reason, personal), (False, 'nonproductive_heartbeat', False))

    def test_running_queue_empty_keeps_in_review_assignment(self):
        """PLAT-7032: a review seat that owns in_review work is productive even
        with a queue_empty heartbeat — it must NOT be hibernated (the old
        ordering hibernated it on nonproductive_heartbeat, then the next tick
        woke it again → recurring review-lane starvation / wake-and-forget)."""
        ok, reason, personal = agent_lease_worthiness(
            email='kei@shizuha.com', teams=('review',),
            workload_by_team={},
            workload_by_email={'kei@shizuha.com': {'in_review': 1}},
            unread_by_email={}, alerts_by_email={},
            actionable_buckets=('ready', 'in_progress', 'in_review'),
            heartbeat_outcome='queue_empty',
            agent_status='running',
        )
        self.assertEqual((ok, reason, personal), (True, 'assigned_tasks', True))

    def test_running_idle_waste_yields_even_with_in_review(self):
        """Scout 2026-08-31: in_review KEEP pinned a GLM TP4 while the holder
        was queue-blind / idle. Idle-waste outranks PLAT-7032 KEEP."""
        ok, reason, personal = agent_lease_worthiness(
            email='scout@shizuha.com', teams=('research-analytics',),
            workload_by_team={},
            workload_by_email={'scout@shizuha.com': {'in_review': 2}},
            unread_by_email={}, alerts_by_email={},
            actionable_buckets=('ready', 'in_progress', 'in_review'),
            heartbeat_outcome='needs_help',
            agent_status='running',
            activity_phase='idle',
            activity_phase_for_ms=60_000,
            lease_idle_waste=True,
        )
        self.assertEqual((ok, reason, personal), (False, 'nonproductive_heartbeat', False))

    def test_non_review_in_review_does_not_keep_or_wake(self):
        """in_review on a research seat is a handoff, not GLM-worthy work."""
        ok, reason, personal = agent_lease_worthiness(
            email='scout@shizuha.com', teams=('research-analytics',),
            workload_by_team={},
            workload_by_email={'scout@shizuha.com': {'in_review': 2}},
            unread_by_email={}, alerts_by_email={},
            actionable_buckets=('ready', 'in_progress', 'in_review'),
            heartbeat_outcome='needs_help',
            agent_status='running',
        )
        # needs_help with no ready-count is a nonproductive heartbeat, so a
        # research in_review handoff still yields (it is not review-lane KEEP).
        self.assertEqual((ok, reason, personal), (False, 'nonproductive_heartbeat', False))
        ok, reason, personal = agent_lease_worthiness(
            email='scout@shizuha.com', teams=('research-analytics',),
            workload_by_team={},
            workload_by_email={'scout@shizuha.com': {'in_review': 2}},
            unread_by_email={}, alerts_by_email={},
            actionable_buckets=('ready', 'in_progress', 'in_review'),
            heartbeat_outcome='',
            agent_status='hibernated',
        )
        self.assertEqual((ok, reason, personal), (False, '', False))

    def test_review_lane_idle_waste_yields_in_review_keep(self):
        ok, reason, personal = agent_lease_worthiness(
            email='kei@shizuha.com', teams=('review',),
            workload_by_team={},
            workload_by_email={'kei@shizuha.com': {'in_review': 1}},
            unread_by_email={}, alerts_by_email={},
            actionable_buckets=('ready', 'in_progress', 'in_review'),
            heartbeat_outcome='queue_empty',
            agent_status='running',
            activity_phase='idle',
            activity_phase_for_ms=120_000,
            lease_idle_waste=True,
        )
        self.assertEqual((ok, reason, personal), (False, 'nonproductive_heartbeat', False))

    def test_running_queue_empty_keeps_review_bucket_assignment(self):
        """PLAT-7032: the 'review' bucket (alias of in_review) also keeps the
        seat — the review lane must stay routable while review work is owned."""
        ok, reason, personal = agent_lease_worthiness(
            email='rei@shizuha.com', teams=('review',),
            workload_by_team={},
            workload_by_email={'rei@shizuha.com': {'review': 2}},
            unread_by_email={}, alerts_by_email={},
            actionable_buckets=('ready', 'in_progress', 'review'),
            heartbeat_outcome='queue_empty',
            agent_status='running',
        )
        self.assertEqual((ok, reason, personal), (True, 'assigned_tasks', True))

    def test_hibernated_in_progress_assignment_still_wakes(self):
        ok, reason, personal = agent_lease_worthiness(
            email='sato@shizuha.com', teams=('accounting',),
            workload_by_team={},
            workload_by_email={'sato@shizuha.com': {'in_progress': 1}},
            unread_by_email={}, alerts_by_email={},
            actionable_buckets=('ready', 'in_progress'),
            heartbeat_outcome='queue_empty',
            agent_status='hibernated',
        )
        self.assertEqual((ok, reason, personal), (True, 'assigned_tasks', True))

    def test_idle_agent_is_not_worthy(self):
        ok, reason, personal = agent_lease_worthiness(
            email='idle@shizuha.com', teams=('qa',),
            workload_by_team={'qa': {'blocked': 9}}, workload_by_email={},
            unread_by_email={}, alerts_by_email={},
            actionable_buckets=('ready', 'in_progress'),
        )
        self.assertFalse(ok)
        self.assertEqual((reason, personal), ('', False))

    def test_hibernated_is_schedulable_but_operator_stopped_is_not(self):
        self.assertTrue(_agent_dict_auto_wakeable({'status': 'hibernated'}))
        self.assertFalse(_agent_dict_auto_wakeable({'status': 'stopped'}))
        self.assertFalse(_agent_dict_auto_wakeable({'status': 'disabled'}))

    def test_unready_running_seat_is_not_wakeable(self):
        """mio/ryo/sara 2026-08-26: broker never Ready, still RUNNING, held leases."""
        self.assertFalse(_agent_dict_auto_wakeable({
            'status': 'running', 'enabled': True, 'runtime_ready': False,
        }))
        self.assertTrue(_agent_dict_auto_wakeable({'status': 'hibernated'}))

    def test_grant_prefers_personal_over_team_only(self):
        d = decide_lease(
            now=0,
            members=_members(('nami', 100.0), ('scout', 0.0)),
            wants={'nami', 'scout'},
            personal={'nami'},  # nami has assigned work; scout only team queue
            current=None,
            running={'nami', 'scout'},
        )
        self.assertEqual(d.holder, principal_for('nami'))
        self.assertIn('personal', d.reason)


class LeaseWakeGateTests(SimpleTestCase):
    """Operator 2026-08-08: event-driven wakes must respect lease capacity.

    Connect's DM fanout woke kei/ryo into a full 7/7 DeepSeek pool; the agent
    cold-booted, sat lease-gated (Cortex is the admission authority), and the
    next 10-min reconcile hibernated it again ("does not hold the model lease").
    ``lease_wake_available`` is the shared gate both the event-driven wake and
    the reconcile fallback use: never boot a governed agent when its pool is
    full (or its board is unreadable).
    """

    def _agent(self, username='kei', model='cortex/DeepSeek-V4-Flash'):
        return {
            'agent_username': username,
            'username': username,
            'model': model,
            'effective_model': model,
        }

    def _config(self, max_holders=7, members=('a', 'b', 'c', 'd', 'e', 'f', 'g')):
        return [{
            'model_id': 'DeepSeek-V4-Flash',
            'max_holders': max_holders,
            'members': list(members),
            'enabled': True,
        }]

    def _board(self, principals):
        return {'max_holders': 7, 'holders': [{'principal': p} for p in principals]}

    def test_not_governed_wakes_freely(self):
        from .model_lease import lease_wake_available
        ok, reason = lease_wake_available(
            agent=self._agent(model='cortex/SomeOther'),
            configs=self._config(),
        )
        self.assertTrue(ok)
        self.assertEqual(reason, 'no_lease_pool')

    def test_free_slot_does_not_wake_without_grant(self):
        from .model_lease import lease_wake_available
        ok, reason = lease_wake_available(
            agent=self._agent(),
            configs=self._config(),
            client=FakeLeaseClient(self._board(['agent-a'])),
        )
        self.assertFalse(ok)
        self.assertEqual(reason, 'await_reconcile_grant')

    def test_full_pool_defers_wake(self):
        from .model_lease import lease_wake_available
        board = self._board(['agent-a', 'agent-b', 'agent-c', 'agent-d', 'agent-e', 'agent-f', 'agent-g'])
        ok, reason = lease_wake_available(
            agent=self._agent(),
            configs=self._config(),
            client=FakeLeaseClient(board),
        )
        self.assertFalse(ok)
        self.assertEqual(reason, 'lease_pool_full')

    def test_keep_resident_does_not_bypass_a_full_pool(self):
        from .model_lease import lease_wake_available
        board = self._board(['agent-a', 'agent-b', 'agent-c', 'agent-d', 'agent-e', 'agent-f', 'agent-g'])
        agent = self._agent(username='yuna')
        agent['team_names'] = ['ceo-office']
        agent['warm_pool_size'] = 1
        ok, reason = lease_wake_available(
            agent=agent,
            configs=self._config(),
            client=FakeLeaseClient(board),
        )
        self.assertFalse(ok)
        self.assertEqual(reason, 'lease_pool_full')

    def test_already_holder_wakes_even_when_full(self):
        from .model_lease import lease_wake_available
        ok, reason = lease_wake_available(
            agent=self._agent(username='kei'),
            configs=self._config(),
            client=FakeLeaseClient(self._board(['agent-kei', 'agent-b', 'agent-c', 'agent-d', 'agent-e', 'agent-f', 'agent-g'])),
        )
        self.assertTrue(ok)
        self.assertEqual(reason, 'already_holder')

    def test_board_unreadable_defers(self):
        from .model_lease import lease_wake_available
        ok, reason = lease_wake_available(
            agent=self._agent(),
            configs=self._config(),
            client=FakeLeaseClient(None),
        )
        self.assertFalse(ok)
        self.assertEqual(reason, 'board_unreadable')

    def test_kv_residency_full_defers_even_with_a_free_lease_slot(self):
        """Hive must not boot a 9th DeepSeek agent onto a full KV board.

        Live 2026-08-25: 8 TTL homes (including hibernated revi) filled every
        DeepSeek seat while the lease board still had room, so sato woke
        homeless and mio admission_timeout'd waiting on a 0/1 home.
        """
        from .model_lease import lease_wake_available
        board = {
            'max_holders': 8,
            'holders': [{'principal': 'agent-a'}],
            'residency': {
                'seats': 8, 'homes': 8, 'protected': 8,
                'grantable': 0, 'full': True,
                'principals': ['agent-haru', 'agent-kai', 'agent-mio'],
            },
        }
        ok, reason = lease_wake_available(
            agent=self._agent(username='sato'),
            configs=self._config(max_holders=8),
            client=FakeLeaseClient(board),
        )
        self.assertFalse(ok)
        self.assertEqual(reason, 'kv_residency_full')

    def test_kv_resident_still_needs_a_lease_when_residency_is_full(self):
        from .model_lease import lease_wake_available
        board = {
            'max_holders': 8,
            'holders': [{'principal': 'agent-a'}],
            'residency': {
                'seats': 8, 'homes': 8, 'protected': 8,
                'grantable': 0, 'full': True,
                'principals': ['agent-mio'],
            },
        }
        ok, reason = lease_wake_available(
            agent=self._agent(username='mio'),
            configs=self._config(max_holders=8),
            client=FakeLeaseClient(board),
        )
        self.assertFalse(ok)
        self.assertEqual(reason, 'await_reconcile_grant')

    def test_leftover_nonholder_resident_full_lease_pool_does_not_wake(self):
        """A leftover KV home is not a Hive lease.

        ryo 2026-08-28: connect-wake treated already_resident as permission to
        boot a non-holder into an 8/8 board, so a hibernated agent came back
        living and hit Cortex.
        """
        from .model_lease import lease_wake_available
        board = {
            'max_holders': 8,
            'holders': [
                {'principal': f'agent-{name}'}
                for name in ('aoi', 'haru', 'ichi', 'jun', 'kei', 'mio', 'nagi', 'sora')
            ],
            'residency': {
                'seats': 8, 'homes': 8, 'protected': 8,
                'grantable': 0, 'full': True,
                'principals': ['agent-ryo'],
            },
        }
        ok, reason = lease_wake_available(
            agent=self._agent(username='ryo'),
            configs=self._config(max_holders=8),
            client=FakeLeaseClient(board),
        )
        self.assertFalse(ok)
        self.assertEqual(reason, 'lease_pool_full')


class FakeLeaseClient:
    """Minimal stand-in for CortexLeaseClient exposing only ``board``."""

    def __init__(self, board):
        self._board_value = board

    def board(self, model):
        return self._board_value


class CortexLeasedPoolOverlayTests(SimpleTestCase):
    """Hive wake cap = Cortex admission. A stale DeepSeek-only cache must
    not leave GLM-5.3-Flash ungoverned after a fleet pin."""

    def tearDown(self):
        cache.delete(POOL_CACHE_KEY)

    def test_glm_pin_adds_pool_when_cache_still_lists_deepseek(self):
        cache.set(POOL_CACHE_KEY, {
            'pools': [{
                'model_id': 'DeepSeek-V4-Flash',
                'members': ['ichi'],
                'enabled': True,
                'max_holders': 8,
            }],
        }, None)
        with patch(
            'fleet.model_lease._fleet_pins_by_canonical',
            return_value={
                'glm-5.3-flash': {'ichi', 'haru'},
                'deepseek-v4-flash': {'ichi'},
            },
        ):
            pools = {p['model_id']: p for p in all_pool_configs()}
        self.assertIn('DeepSeek-V4-Flash', pools)
        self.assertIn('GLM-5.3-Flash', pools)
        self.assertEqual(sorted(pools['GLM-5.3-Flash']['members']), ['haru', 'ichi'])

    def test_retired_vision_metal_pin_does_not_add_pool(self):
        """Metal is deprecated: stale pins must not recreate a 1-seat pool."""
        cache.set(POOL_CACHE_KEY, {
            'pools': [{
                'model_id': 'GLM-5.3-Flash',
                'members': ['ichi'],
                'enabled': True,
                'max_holders': 11,
            }],
        }, None)
        with patch(
            'fleet.model_lease._fleet_pins_by_canonical',
            return_value={
                'glm-5.3-flash': {'ichi'},
                'deepseek-v4-flash-vision-metal': {'rei', 'yuki', 'nova'},
            },
        ):
            pools = {p['model_id']: p for p in all_pool_configs()}
        self.assertNotIn('DeepSeek-V4-Flash-Vision-Metal', pools)
        self.assertIn('GLM-5.3-Flash', pools)
        self.assertNotIn('GLM-5.3-Flash-Metal', pools)

    def test_stale_cache_still_blocks_homeless_glm_wake(self):
        """Production path: all_pool_configs() overlay, no explicit configs=."""
        cache.set(POOL_CACHE_KEY, {
            'pools': [{
                'model_id': 'DeepSeek-V4-Flash',
                'members': ['ichi'],
                'enabled': True,
                'max_holders': 8,
            }],
        }, None)
        board = {
            'max_holders': 4,
            'holders': [],
            'residency': {
                'seats': 4, 'homes': 4, 'protected': 4,
                'grantable': 0, 'full': True,
                'principals': [
                    'agent-sora', 'agent-ichi', 'agent-mika', 'agent-shion',
                ],
            },
        }
        with patch(
            'fleet.model_lease._fleet_pins_by_canonical',
            return_value={
                'glm-5.3-flash': {'ichi', 'sora', 'mika', 'shion', 'haru'},
                'deepseek-v4-flash': {'ichi'},
            },
        ):
            ok, reason = lease_wake_available(
                agent={
                    'agent_username': 'haru',
                    'username': 'haru',
                    'model': 'cortex/GLM-5.3-Flash',
                    'effective_model': 'cortex/GLM-5.3-Flash',
                },
                client=FakeLeaseClient(board),
            )
        self.assertFalse(ok)
        self.assertEqual(reason, 'kv_residency_full')

    def test_glm_residency_full_blocks_homeless_wake(self):
        cfg = [{
            'model_id': 'GLM-5.3-Flash',
            'max_holders': 4,
            'members': ['ichi', 'sora', 'mika', 'shion', 'haru'],
            'enabled': True,
        }]
        board = {
            'max_holders': 4,
            'holders': [],
            'residency': {
                'seats': 4, 'homes': 4, 'protected': 4,
                'grantable': 0, 'full': True,
                'principals': [
                    'agent-sora', 'agent-ichi', 'agent-mika', 'agent-shion',
                ],
            },
        }
        ok, reason = lease_wake_available(
            agent={
                'agent_username': 'haru',
                'username': 'haru',
                'model': 'cortex/GLM-5.3-Flash',
                'effective_model': 'cortex/GLM-5.3-Flash',
            },
            configs=cfg,
            client=FakeLeaseClient(board),
        )
        self.assertFalse(ok)
        self.assertEqual(reason, 'kv_residency_full')

    def test_glm_resident_waits_for_grant_on_empty_lease_board(self):
        cfg = [{
            'model_id': 'GLM-5.3-Flash',
            'max_holders': 4,
            'members': ['ichi', 'sora', 'mika', 'shion', 'haru'],
            'enabled': True,
        }]
        board = {
            'max_holders': 4,
            'holders': [],
            'residency': {
                'seats': 4, 'homes': 4, 'protected': 4,
                'grantable': 0, 'full': True,
                'principals': [
                    'agent-sora', 'agent-ichi', 'agent-mika', 'agent-shion',
                ],
            },
        }
        ok, reason = lease_wake_available(
            agent={
                'agent_username': 'ichi',
                'username': 'ichi',
                'model': 'cortex/GLM-5.3-Flash',
                'effective_model': 'cortex/GLM-5.3-Flash',
            },
            configs=cfg,
            client=FakeLeaseClient(board),
        )
        self.assertFalse(ok)
        self.assertEqual(reason, 'await_reconcile_grant')


class ZeroAdmissionRevokesHoldersTests(SimpleTestCase):
    """Qwen after MiniMax H3 took i9-ws: admission_capacity=0 must drop holders."""

    class Client:
        def __init__(self):
            self.data = {
                'max_holders': 1,
                'admission_capacity': 0,
                'residency': {
                    'seats': 0, 'homes': 0, 'protected': 0,
                    'grantable': 0, 'full': False,
                },
                'holders': [{'principal': 'agent-nami', 'held_for_s': 600, 'idle_for_s': 0}],
                'governed_principals': ['agent-nami', 'agent-yuki'],
                'eligible_principals': ['agent-nami', 'agent-yuki'],
            }
            self.revokes = []
            self.grants = []

        def board(self, _model):
            return self.data

        def set_governed(self, _model, principals):
            self.data['governed_principals'] = sorted(principals)
            return True

        def set_eligible(self, _model, principals):
            self.data['eligible_principals'] = sorted(principals)
            return True

        def grant(self, _model, holder, **_kwargs):
            self.grants.append(holder)
            return True

        def revoke(self, _model, **kwargs):
            self.revokes.append(kwargs)
            holder = kwargs.get('holder')
            self.data['holders'] = [
                h for h in self.data.get('holders') or []
                if str(h.get('principal') or '') != str(holder or '')
            ]
            return True

    def setUp(self):
        cache.delete(HISTORY_CACHE_KEY)
        cache.delete(HOLDINGS_CACHE_KEY)
        self.client = self.Client()

    def test_zero_admission_force_revokes_even_active_holder(self):
        tick = _reconcile_one_pool(
            cfg={
                'enabled': True,
                'model_id': 'Qwen3.8-27B-Q2',
                'members': ['nami', 'yuki'],
                'min_hold_seconds': MIN_HOLD,
                'max_hold_seconds': MAX_HOLD,
                'idle_release_seconds': IDLE,
                'max_holders': 1,
            },
            agents=[
                {
                    'username': 'nami', 'email': 'nami@shizuha.com',
                    'status': 'running', 'enabled': True,
                    'activity_state': 'responding', 'telemetry_ts': 1000,
                },
                {'username': 'yuki', 'email': 'yuki@shizuha.com', 'status': 'hibernated', 'enabled': False},
            ],
            email_to_teams={},
            workload_by_team={},
            workload_by_email={'nami@shizuha.com': {'ready': 1}, 'yuki@shizuha.com': {'ready': 1}},
            unread_by_email={},
            alerts_by_email={},
            actionable_buckets=('ready',),
            client=self.client,
            now=1000.0,
            apply=True,
        )
        self.assertTrue(self.client.revokes)
        self.assertTrue(self.client.revokes[0].get('force'))
        self.assertEqual(self.client.grants, [])
        self.assertIn('admission_capacity=0', ' '.join(tick['log']))
        self.assertFalse(tick['scale'].get('nami'))


class UnmetFloorSeaterTests(SimpleTestCase):
    """Grant-path floor arbitration (PLAT-6618 verify leg, PLAT-302 wave).

    Property: a slot freed by lifecycle hibernate seats UNMET critical-team
    coverage ahead of ordinary fairness signals among the waiters.  2026-10-07
    receipt: two slots freed at 13:50Z seated fellow-team engineering waiters
    while merge seats kei/ryo/aoi stayed eligible+waiting, so the floor never
    saw a seat and start-defer kept returning lease_pool_full.
    """

    POLICY = {'merge': 2, 'engineering': 1}

    def _members(self, *specs):
        """specs: (username, last_held_at, teams)"""
        return [
            PoolMember(username=u, email=f'{u}@shizuha.com', teams=teams, last_held_at=t)
            for u, t, teams in specs
        ]

    def test_empty_policy_no_seaters(self):
        from .model_lease import unmet_floor_seaters

        self.assertEqual(
            unmet_floor_seaters(
                candidates=self._members(('kai', 0.0, ('merge',))),
                holder_usernames=set(),
                policy={},
            ),
            set(),
        )

    def test_deficit_seater_selected(self):
        from .model_lease import unmet_floor_seaters

        members = self._members(
            ('kai', 0.0, ('merge',)),
            ('hiro', 100.0, ('engineering',)),
        )
        self.assertEqual(
            unmet_floor_seaters(
                candidates=members,
                holder_usernames={'hiro'},
                policy=self.POLICY,
            ),
            {'kai'},
        )

    def test_met_floor_not_a_seater(self):
        from .model_lease import unmet_floor_seaters

        members = self._members(
            ('kai', 0.0, ('merge',)),
            ('ryo', 100.0, ('merge',)),
        )
        # merge already fields 2 holders — no deficit, so nobody seats it.
        self.assertEqual(
            unmet_floor_seaters(
                candidates=members,
                holder_usernames={'agent-kai', 'agent-ryo'},
                policy=self.POLICY,
            ),
            set(),
        )

    def test_fellow_teams_counted_per_membership(self):
        from .model_lease import unmet_floor_seaters

        members = self._members(
            ('kai', 0.0, ('merge', 'engineering')),
            ('nagi', 100.0, ('engineering',)),
        )
        # nagi's lease covers merge too (same agent, two teams): merge fields
        # 1 holder, deficit 1 — kai seats it; engineering met by nagi.
        self.assertEqual(
            unmet_floor_seaters(
                candidates=members,
                holder_usernames={'agent-nagi'},
                policy=self.POLICY,
            ),
            {'kai'},
        )

    def test_grant_path_seats_floor_coverage_ahead(self):
        members = self._members(
            ('kai', 100.0, ('merge',)),        # critical team, running deficit
            ('scout', 0.0, ('engineering',)),  # running, personal, assigned —
            ('rookie', 0.0, ('engineering',)), # but seats no unmet floor
        )
        d = decide_lease(
            now=0, members=members,
            wants={'kai', 'scout', 'rookie'},
            current=None,
            running={'kai', 'scout', 'rookie'},
            active={'scout', 'rookie'},
            assigned={'scout'},
            personal={'scout'},
            holder_usernames={'agent-rookie'},
            critical_floor=self.POLICY,
        )
        self.assertEqual(d.action, 'grant')
        self.assertEqual(d.holder, 'agent-kai')

    def test_grant_path_no_policy_unchanged(self):
        members = self._members(
            ('kai', 100.0, ('merge',)),
            ('scout', 0.0, ('engineering',)),
        )
        d = decide_lease(
            now=0, members=members,
            wants={'kai', 'scout'},
            current=None,
            running={'kai', 'scout'},
            active={'scout'},
            assigned={'scout'},
            personal={'scout'},
            holder_usernames=set(),
        )
        self.assertEqual(d.action, 'grant')
        self.assertEqual(d.holder, 'agent-scout')
