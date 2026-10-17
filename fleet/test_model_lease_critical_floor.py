"""HIVE-2841 — critical-team lease floor: fair reservation when supply is short.

The operator problem (2026-10-05): with the GLM backend gone, the lease pool
fills with whoever grabs first and critical teams (merge is the first example)
get zero seats — merge seats go dark while cortex admission allows 5. The fix
is a configurable floor: each enabled critical team is guaranteed at least
``min_leases`` holders, fairly across critical teams, editable in Hive (Django
admin) rather than a code constant.

The property that matters: with supply below demand, a merge seat STILL
obtains a lease — and disabling the policy row in Hive stops reserving.
"""
from __future__ import annotations

from django.core.cache import cache
from django.test import TestCase

from .models import LeaseCriticalTeamPolicy
from .model_lease import (
    CRITICAL_TEAM_POLICY_CACHE_KEY,
    HISTORY_CACHE_KEY,
    HOLDINGS_CACHE_KEY,
    PREEMPT_RETRY_CACHE_KEY,
    _reconcile_one_pool,
)

MIN_HOLD = 900
MAX_HOLD = 2700
IDLE = 180


class FloorClient:
    """Multi-holder fake Cortex lease board (mirrors the #1767 guard fakes)."""

    def __init__(self, *, max_holders=2, holders=(), governed=()):
        self.data = {
            'max_holders': max_holders,
            'holders': list(holders),
            'governed_principals': sorted(governed),
            'eligible_principals': [],
            'admission_capacity': max(2, max_holders + 1),
            'residency': {},
        }
        self.grants = []
        self.revokes = []

    def board(self, _model):
        return dict(self.data)

    def set_governed(self, _model, principals):
        self.data['governed_principals'] = sorted(principals)
        return True

    def set_eligible(self, _model, principals):
        self.data['eligible_principals'] = sorted(principals)
        return True

    def grant(self, _model, holder, **_kwargs):
        self.grants.append(holder)
        self.data['holders'] = self.data['holders'] + [{'principal': holder}]
        return True

    def revoke(self, _model, *, holder, **_kwargs):
        self.revokes.append(holder)
        self.data['holders'] = [
            h for h in self.data['holders'] if h.get('principal') != holder
        ]
        return True


def _holder(principal, *, held_for_s=0, idle_for_s=0):
    return {
        'principal': principal,
        'held_for_s': held_for_s,
        'idle_for_s': idle_for_s,
    }


class CriticalTeamFloorTests(TestCase):
    """End-to-end pool ticks with the floor policy armed."""

    def setUp(self):
        # HIVE-2862: migration 0064 seeds default floor rows (merge/review/
        # devops). These tests exercise the floor from an empty policy table;
        # clear the seed so objects.create(team_slug=...) doesn't hit the
        # unique constraint.
        LeaseCriticalTeamPolicy.objects.all().delete()
        for key in (HISTORY_CACHE_KEY, HOLDINGS_CACHE_KEY, PREEMPT_RETRY_CACHE_KEY,
                    CRITICAL_TEAM_POLICY_CACHE_KEY):
            cache.delete(key)
        self.client = FloorClient()
        self.cfg = {
            'enabled': True,
            'model_id': 'GLM-5.3-Flash',
            'members': ['kei', 'ryo', 'jun', 'sana'],
            'min_hold_seconds': MIN_HOLD,
            'max_hold_seconds': MAX_HOLD,
            'idle_release_seconds': IDLE,
            'max_holders': 2,
        }
        self.agents = [
            {'username': 'kei', 'email': 'kei@shizuha.com', 'status': 'running', 'enabled': True},
            {'username': 'ryo', 'email': 'ryo@shizuha.com', 'status': 'running', 'enabled': True},
            {'username': 'jun', 'email': 'jun@shizuha.com', 'status': 'running', 'enabled': True},
            {'username': 'sana', 'email': 'sana@shizuha.com', 'status': 'running', 'enabled': True},
        ]
        self.email_to_teams = {
            'kei@shizuha.com': {'engineering'},
            'ryo@shizuha.com': {'engineering'},
            'jun@shizuha.com': {'merge'},
            'sana@shizuha.com': {'security'},
        }

    def tearDown(self):
        for key in (HISTORY_CACHE_KEY, HOLDINGS_CACHE_KEY, PREEMPT_RETRY_CACHE_KEY,
                    CRITICAL_TEAM_POLICY_CACHE_KEY):
            cache.delete(key)

    def _tick(self, *, now, workload_by_email=None, workload_by_team=None, agents=None,
              holders=None, client=None):
        return _reconcile_one_pool(
            cfg=self.cfg,
            agents=agents if agents is not None else self.agents,
            email_to_teams=self.email_to_teams,
            workload_by_team=workload_by_team or {},
            workload_by_email=workload_by_email or {},
            unread_by_email={},
            alerts_by_email={},
            actionable_buckets=('ready', 'open', 'todo'),
            client=client or self.client,
            now=now,
            apply=True,
        )

    def test_short_supply_full_pool_still_seats_merge(self):
        """Pool full of productive non-critical holders + worthy merge agent
        (team queue) → the floor preempts a quiet holder and seats merge."""
        LeaseCriticalTeamPolicy.objects.create(team_slug='merge', min_leases=1)
        holders = [
            _holder('agent-kei', held_for_s=300, idle_for_s=290),
            _holder('agent-ryo', held_for_s=300, idle_for_s=290),
        ]
        client = FloorClient(holders=holders, governed=['agent-kei', 'agent-ryo',
                                                        'agent-jun', 'agent-sana'])
        self._tick(
            now=1000,
            # kei/ryo hold their own assigned work → kept by ordinary rules.
            workload_by_email={'kei@shizuha.com': {'ready': 1}, 'ryo@shizuha.com': {'ready': 1}},
            # jun has no personal signal, only the merge team queue → the floor
            # is the ONLY path that seats him.
            workload_by_team={'merge': {'ready': 2}},
            client=client,
        )
        self.assertIn('agent-jun', client.grants)
        self.assertEqual(len(client.grants), 1)
        self.assertEqual(len(client.revokes), 1)

    def test_floor_victim_is_a_quiet_holder_not_the_only_merge_seat(self):
        """The floor never evicts a holder that IS the coverage for another met
        critical team, and never evicts an active holder."""
        LeaseCriticalTeamPolicy.objects.create(team_slug='merge', min_leases=1)
        holders = [
            _holder('agent-ryo', held_for_s=300, idle_for_s=290),
            _holder('agent-kei', held_for_s=300, idle_for_s=290),
        ]
        client = FloorClient(holders=holders, governed=['agent-kei', 'agent-ryo',
                                                        'agent-jun', 'agent-sana'])
        agents = [
            {**a, 'activity_state': 'working', 'telemetry_ts': 1000}
            if a['username'] == 'kei' else a
            for a in self.agents
        ]
        self._tick(
            now=1000,
            workload_by_email={'kei@shizuha.com': {'ready': 1}, 'ryo@shizuha.com': {'ready': 1}},
            workload_by_team={'merge': {'ready': 2}},
            agents=agents,
            client=client,
        )
        # kei is actively working — the quiet ryo is the victim.
        self.assertEqual(client.revokes, ['agent-ryo'])
        self.assertIn('agent-jun', client.grants)

    def test_second_critical_team_also_gets_one(self):
        """A second configured critical team also obtains its seat when supply
        is short (both holders non-critical)."""
        LeaseCriticalTeamPolicy.objects.create(team_slug='merge', min_leases=1)
        LeaseCriticalTeamPolicy.objects.create(team_slug='security', min_leases=1)
        holders = [
            _holder('agent-kei', held_for_s=300, idle_for_s=290),
            _holder('agent-ryo', held_for_s=300, idle_for_s=290),
        ]
        client = FloorClient(holders=holders, governed=['agent-kei', 'agent-ryo',
                                                        'agent-jun', 'agent-sana'])
        self._tick(
            now=1000,
            workload_by_email={'kei@shizuha.com': {'ready': 1}, 'ryo@shizuha.com': {'ready': 1}},
            workload_by_team={'merge': {'ready': 2}, 'security': {'ready': 1}},
            client=client,
        )
        self.assertIn('agent-jun', client.grants)
        self.assertIn('agent-sana', client.grants)
        self.assertEqual(len(client.revokes), 2)
        self.assertEqual(set(client.revokes), {'agent-kei', 'agent-ryo'})

    def test_floor_is_fair_across_critical_teams_when_supply_shorter_than_teams(self):
        """One free floor seat, two unmet critical teams → the stalest team is
        served first and the other rotates in on a later tick (LRH)."""
        LeaseCriticalTeamPolicy.objects.create(team_slug='merge', min_leases=1)
        LeaseCriticalTeamPolicy.objects.create(team_slug='security', min_leases=1)
        holders = [
            _holder('agent-kei', held_for_s=300, idle_for_s=290),
        ]
        client = FloorClient(max_holders=2, holders=holders,
                             governed=['agent-kei', 'agent-ryo',
                                       'agent-jun', 'agent-sana'])
        self._tick(
            now=1000,
            workload_by_email={'kei@shizuha.com': {'ready': 1}},
            workload_by_team={'merge': {'ready': 2}, 'security': {'ready': 1}},
            client=client,
        )
        # One free slot: exactly one floor seat. jun has never held (last=0)
        # so merge (stalest) is served first.
        self.assertEqual(client.grants, ['agent-jun'])
        self.assertEqual(client.revokes, [])

    def test_disabling_the_policy_row_stops_reserving(self):
        """Changing the policy in Hive (not a code edit) changes who is
        reserved: a disabled row leaves the former floor seat to ordinary
        fairness (unused revoke fires for the waiter)."""
        policy = LeaseCriticalTeamPolicy.objects.create(team_slug='merge', min_leases=1)
        holders = [
            _holder('agent-jun', held_for_s=300, idle_for_s=300),
            _holder('agent-kei', held_for_s=300, idle_for_s=290),
        ]
        client = FloorClient(holders=holders, governed=['agent-kei', 'agent-jun',
                                                        'agent-ryo'])
        # Tick 1: floor seats... jun already holds; floor fences him (covered).
        self._tick(
            now=1000,
            workload_by_email={'kei@shizuha.com': {'ready': 1}},
            workload_by_team={'merge': {'ready': 2}},
            client=client,
        )
        self.assertEqual(client.revokes, [])
        # Operator disables the policy row in Hive.
        policy.enabled = False
        policy.save()
        cache.delete(CRITICAL_TEAM_POLICY_CACHE_KEY)
        # Tick 2: jun's seat is unused (idle==held) and a non-holder waiter
        # exists → ordinary unused-revoke fires; no floor fence anymore.
        self._tick(
            now=1000,
            workload_by_email={'kei@shizuha.com': {'ready': 1},
                               'ryo@shizuha.com': {'ready': 1}},
            workload_by_team={'merge': {'ready': 2}},
            client=client,
        )
        self.assertIn('agent-jun', client.revokes)

    def test_no_worthy_critical_member_no_floor_action(self):
        """The floor arbitrates demand; an idle critical team with no work
        signal is not granted a seat."""
        LeaseCriticalTeamPolicy.objects.create(team_slug='merge', min_leases=1)
        holders = [
            _holder('agent-kei', held_for_s=300, idle_for_s=290),
            _holder('agent-ryo', held_for_s=300, idle_for_s=290),
        ]
        client = FloorClient(holders=holders, governed=['agent-kei', 'agent-ryo',
                                                        'agent-jun', 'agent-sana'])
        self._tick(
            now=1000,
            workload_by_email={'kei@shizuha.com': {'ready': 1}, 'ryo@shizuha.com': {'ready': 1}},
            workload_by_team={},  # merge has no queue demand at all
            client=client,
        )
        self.assertEqual(client.grants, [])
        self.assertEqual(client.revokes, [])

    def test_floor_holder_is_keep_fenced_on_the_next_tick(self):
        """The seat the floor just granted is not unused-revoked immediately:
        it is coverage for a met critical team and is fenced like a reserved
        seat (no musical chairs)."""
        LeaseCriticalTeamPolicy.objects.create(team_slug='merge', min_leases=1)
        holders = [
            _holder('agent-jun', held_for_s=310, idle_for_s=310),
            _holder('agent-kei', held_for_s=300, idle_for_s=290),
        ]
        client = FloorClient(holders=holders, governed=['agent-kei', 'agent-jun',
                                                        'agent-ryo'])
        # kei has own work; jun (merge) holds the floor seat but is quiet.
        tick = self._tick(
            now=1000,
            workload_by_email={'kei@shizuha.com': {'ready': 1}},
            workload_by_team={'merge': {'ready': 2}},
            client=client,
        )
        self.assertEqual(client.revokes, [])
        floor_keep = [log for log in tick['log'] if 'keep' in str(log).lower()]
        self.assertTrue(floor_keep, tick['log'])

    def _devops_floor_case(self):
        LeaseCriticalTeamPolicy.objects.create(team_slug='devops', min_leases=1)
        self.cfg.update(members=['taku', 'san'], max_holders=1)
        self.agents = [
            {'username': name, 'email': f'{name}@shizuha.com',
             'status': 'running', 'enabled': True}
            for name in ('taku', 'san')
        ]
        self.email_to_teams = {
            'taku@shizuha.com': {'shizuha-trading::devops'},
            'san@shizuha.com': {'shizuha::devops'},
        }
        return FloorClient(
            max_holders=1,
            holders=[_holder('agent-taku', held_for_s=1000, idle_for_s=1000)],
            governed=['agent-taku', 'agent-san'],
        )

    def test_empty_floor_holder_releases_for_ready_cross_org_teammate(self):
        """A queue-empty Trading holder cannot satisfy Global's DevOps floor."""
        client = self._devops_floor_case()
        self._tick(now=1000, client=client,
                   workload_by_email={'san@shizuha.com': {'ready': 1}})
        self.assertEqual(client.revokes, ['agent-taku'])
        self.assertEqual(client.grants, ['agent-san'])
        # Demand drains after the replacement completes its task. The next
        # reconcile must release it rather than recreate demand from the floor.
        client.data['holders'] = [
            _holder('agent-san', held_for_s=1000, idle_for_s=1000),
        ]
        self._tick(now=2000, client=client)
        self.assertEqual(client.revokes, ['agent-taku', 'agent-san'])
        self.assertEqual(client.data['holders'], [])

    def test_active_floor_holder_keeps_continuity_without_queue_signal(self):
        """Fresh active-turn demand is still valid floor coverage."""
        client = self._devops_floor_case()
        self.agents[0].update(activity_state='working', telemetry_ts=1000)
        self._tick(now=1000, client=client,
                   workload_by_email={'san@shizuha.com': {'ready': 1}})
        self.assertEqual(client.revokes, [])
        self.assertEqual(client.grants, [])
        self.assertEqual(client.data['holders'][0]['principal'], 'agent-taku')
