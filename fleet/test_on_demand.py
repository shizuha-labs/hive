import os
from datetime import datetime, timedelta, timezone
from io import StringIO
from unittest.mock import Mock, call, patch

import requests
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from .management.commands.reconcile_on_demand_agents import (
    Command,
    ConnectLifecycleSnapshot,
    _normalize_team_workload,
)
from .on_demand import (
    OnDemandAction,
    agent_eligible_to_start,
    agent_runtime_cannot_turn,
    agent_satisfies_demand,
    decide_on_demand_actions,
    memberships_by_email,
    roster_members_by_team,
)


def _agent(username, *, enabled=True, status='running'):
    return {
        'id': f'id-{username}',
        'username': username,
        'email': f'{username}@shizuha.com',
        'enabled': enabled,
        'status': status,
    }


def _roster(team, members, *, organization_slug=None):
    row = {'team_name': team, 'is_active': True, 'tiers': [[f'{m}@shizuha.com' for m in members]]}
    if organization_slug:
        row['organization_slug'] = organization_slug
    return row


def _recent_ts(minutes_ago=1):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


class OnDemandPolicyTests(SimpleTestCase):
    def test_explicit_operator_stop_is_not_auto_woken_by_any_demand(self):
        actions = decide_on_demand_actions(
            agents=[_agent('scout', enabled=False, status='stopped')],
            rosters=[_roster('qa', ['scout'])],
            workload_by_email={'scout@shizuha.com': {'open': 1}},
            workload_by_team={'qa': {'open': 1}},
            unread_by_email={'scout@shizuha.com': 1},
        )

        self.assertEqual(actions, [])

    def test_provider_blocked_hibernated_agent_is_not_started_into_a_flap(self):
        blocked_signals = (
            {'provider_unavailable': True},
            {'health_token_pool_unavailable': True},
            {'availability_status': 'unavailable'},
            {'availability_status': 'offline'},
            {'availability_status': 'inactive'},
        )
        for signal in blocked_signals:
            with self.subTest(signal=signal):
                scout = _agent('scout', enabled=False, status='hibernated')
                scout.update(signal)
                actions = decide_on_demand_actions(
                    agents=[scout],
                    rosters=[_roster('research-analytics', ['scout'])],
                    workload_by_email={},
                    workload_by_team={'research-analytics': {'open': 1}},
                )
                self.assertEqual(actions, [])
                self.assertFalse(agent_eligible_to_start(scout))

    def test_healthy_hibernated_agent_remains_eligible_for_team_demand(self):
        scout = _agent('scout', enabled=False, status='hibernated')
        actions = decide_on_demand_actions(
            agents=[scout],
            rosters=[_roster('research-analytics', ['scout'])],
            workload_by_email={},
            workload_by_team={'research-analytics': {'open': 1}},
        )

        self.assertTrue(agent_eligible_to_start(scout))
        self.assertEqual([action.action for action in actions], ['start'])

    def test_default_until_first_use_is_not_sweep_started(self):
        parked = _agent('shizuha-115', enabled=False, status='hibernated')
        parked['hibernation_reason'] = 'default_until_first_use'
        actions = decide_on_demand_actions(
            agents=[parked],
            rosters=[_roster('research-analytics', ['shizuha-115'])],
            workload_by_email={'shizuha-115@shizuha.com': {'open': 1}},
            workload_by_team={'research-analytics': {'open': 1}},
        )
        self.assertEqual(actions, [])

    def test_default_until_first_use_wakes_on_unread_dm(self):
        parked = _agent('shizuha-115', enabled=False, status='hibernated')
        parked['hibernation_reason'] = 'default_until_first_use'
        actions = decide_on_demand_actions(
            agents=[parked],
            rosters=[],
            workload_by_email={},
            workload_by_team={},
            unread_by_email={'shizuha-115@shizuha.com': 1},
        )
        self.assertEqual([action.action for action in actions], ['start'])
        self.assertIn('unread Connect', actions[0].reason)

    def test_stops_idle_agent_with_no_work(self):
        actions = decide_on_demand_actions(
            agents=[_agent('scout')],
            rosters=[_roster('customer-support', ['scout'])],
            workload_by_email={},
            workload_by_team={},
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action, 'stop')
        self.assertEqual(actions[0].username, 'scout')

    def test_does_not_stop_agent_without_roster_membership(self):
        actions = decide_on_demand_actions(
            agents=[_agent('unknown')],
            rosters=[],
            workload_by_email={},
            workload_by_team={},
        )

        self.assertEqual(actions, [])

    def test_same_named_org_rosters_keep_distinct_memberships(self):
        rosters = [
            _roster('accounting', ['sato'], organization_slug='personal-3'),
            _roster('accounting', ['banto'], organization_slug='shizuha'),
            _roster('accounting', ['kane'], organization_slug='shizuha-digital'),
        ]

        self.assertEqual(
            roster_members_by_team(rosters),
            {
                'personal-3::accounting': ['sato@shizuha.com'],
                'shizuha::accounting': ['banto@shizuha.com'],
                'shizuha-digital::accounting': ['kane@shizuha.com'],
            },
        )
        self.assertEqual(
            memberships_by_email(rosters),
            {
                'sato@shizuha.com': {'personal-3::accounting'},
                'banto@shizuha.com': {'shizuha::accounting'},
                'kane@shizuha.com': {'shizuha-digital::accounting'},
            },
        )

    def test_idle_agents_in_same_named_org_rosters_all_hibernate(self):
        actions = decide_on_demand_actions(
            agents=[_agent('sato'), _agent('banto'), _agent('kane')],
            rosters=[
                _roster('accounting', ['sato'], organization_slug='personal-3'),
                _roster('accounting', ['banto'], organization_slug='shizuha'),
                _roster('accounting', ['kane'], organization_slug='shizuha-digital'),
            ],
            workload_by_email={},
            workload_by_team={
                'personal-3::accounting': {'blocked': 4},
                'shizuha::accounting': {'blocked': 3, 'scheduled': 2},
                'shizuha-digital::accounting': {'blocked': 2},
            },
        )

        self.assertEqual(
            [(action.action, action.username, action.teams) for action in actions],
            [
                ('stop', 'sato', ('personal-3::accounting',)),
                ('stop', 'banto', ('shizuha::accounting',)),
                ('stop', 'kane', ('shizuha-digital::accounting',)),
            ],
        )

    def test_actionable_work_only_wakes_member_of_matching_org_team(self):
        actions = decide_on_demand_actions(
            agents=[
                _agent('sato', enabled=False, status='hibernated'),
                _agent('banto', enabled=False, status='hibernated'),
            ],
            rosters=[
                _roster('accounting', ['sato'], organization_slug='personal-3'),
                _roster('accounting', ['banto'], organization_slug='shizuha'),
            ],
            workload_by_email={},
            workload_by_team={
                'personal-3::accounting': {'open': 0},
                'shizuha::accounting': {'open': 1},
            },
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action, 'start')
        self.assertEqual(actions[0].username, 'banto')
        self.assertEqual(actions[0].teams, ('shizuha::accounting',))

    def test_no_static_warm_team_idle_agent_hibernates(self):
        # Operator 2026-07-13: 'review' used to be a static warm team exempt from
        # stop. Ordinary compute-saving teams still hibernate when idle. The
        # 2026-08-15 CEO Office exception is explicit and does not revive this
        # list.
        actions = decide_on_demand_actions(
            agents=[_agent('kei')],
            rosters=[_roster('review', ['kei'])],
            workload_by_email={},
            workload_by_team={},
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action, 'stop')
        self.assertEqual(actions[0].username, 'kei')

    def test_ceo_office_team_membership_hibernates_idle_seat(self):
        # Operator 2026-09-22: keep-resident reversed. A new EA without a
        # per-agent warm_pool_size hibernates when idle; the connect-wake
        # preempt path is the fast-wake guarantee.
        actions = decide_on_demand_actions(
            agents=[_agent('hina')],
            rosters=[_roster('ceo-office', ['hina'])],
            workload_by_email={},
            workload_by_team={},
        )

        self.assertTrue(any(a.action == 'stop' and a.username == 'hina' for a in actions))

    def test_scoped_ceo_office_idle_seat_hibernates(self):
        # Operator 2026-09-22: ceo-office left keep-resident — idle voice
        # seats hibernate under the normal on-demand rules (wake is
        # preempt-capable and PVC-resumed).
        actions = decide_on_demand_actions(
            agents=[_agent('aya')],
            rosters=[_roster('ceo-office', ['aya'], organization_slug='shizuha')],
            workload_by_email={},
            workload_by_team={},
        )

        self.assertTrue(any(a.action == 'stop' and a.username == 'aya' for a in actions))

    def test_ceo_office_hibernated_member_stays_hibernated_when_idle(self):
        # Operator 2026-09-22: no keep-resident — a hibernated idle member
        # stays hibernated (no keep-resident start); a connect wake preempts
        # instead (model_lease connect-wake-preempt path).
        actions = decide_on_demand_actions(
            agents=[_agent('hina', enabled=False, status='hibernated')],
            rosters=[_roster('ceo-office', ['hina'])],
            workload_by_email={},
            workload_by_team={},
        )

        self.assertEqual([a for a in actions if a.username == 'hina' and a.action == 'start'], [])

    def test_ceo_office_operator_disable_is_not_auto_started(self):
        actions = decide_on_demand_actions(
            agents=[_agent('hina', enabled=False, status='stopped')],
            rosters=[_roster('ceo-office', ['hina'])],
            workload_by_email={},
            workload_by_team={},
        )
        self.assertEqual(actions, [])

    def test_ceo_office_rate_limited_seat_hibernates(self):
        # Operator 2026-09-22: keep-resident reversed — a rate-limited idle
        # voice seat hibernates (rest off-pool) instead of burning a lease.
        actions = decide_on_demand_actions(
            agents=[{
                **_agent('hina'),
                'provider_unavailable_reason': 'rate_limit: hit your usage',
            }],
            rosters=[_roster('ceo-office', ['hina'])],
            workload_by_email={},
            workload_by_team={},
        )

        self.assertTrue(any(a.action == 'stop' and a.username == 'hina' for a in actions))

    def test_ceo_office_unserviceable_model_hibernates_idempotently(self):
        # 2026-08-17 production failure was a STOP->RESTART->RETRY STORM
        # (Hina stopped every 10m and immediately relaunched). Operator
        # 2026-09-22: idle voice seats hibernate — scale-to-zero with no
        # retry storm — and demand re-wakes them when the model returns.
        # Two consecutive ticks must agree (idempotent hibernation).
        agent = {
            **_agent('hina'),
            'model': 'cortex/grok-voice-think-fast-2.0',
        }
        rosters = [_roster('ceo-office', ['hina'])]
        dead = frozenset({'grok-voice-think-fast-2.0'})
        first = decide_on_demand_actions(
            agents=[agent],
            rosters=rosters,
            workload_by_email={},
            workload_by_team={},
            unserviceable_models=dead,
        )
        second = decide_on_demand_actions(
            agents=[agent],
            rosters=rosters,
            workload_by_email={},
            workload_by_team={},
            unserviceable_models=dead,
        )
        self.assertTrue(any(a.action == 'stop' for a in first))
        self.assertTrue(any(a.action == 'stop' for a in second))

    def test_ceo_office_hibernated_on_dead_model_is_woken(self):
        actions = decide_on_demand_actions(
            agents=[{
                **_agent('hina', enabled=False, status='hibernated'),
                'model': 'cortex/grok-voice-think-fast-2.0',
            }],
            rosters=[_roster('ceo-office', ['hina'])],
            workload_by_email={},
            workload_by_team={},
            unserviceable_models=frozenset({'grok-voice-think-fast-2.0'}),
        )
        # Operator 2026-09-22: no keep-resident — an unserviceable model is
        # not a wake reason; the seat rests until the model returns or a
        # connect wake (preempt-capable) demands it.
        hina_actions = [a for a in actions if a.username == 'hina']
        self.assertEqual([a for a in hina_actions if a.action == 'start'], [])

    def test_ceo_office_hibernated_rate_limited_is_woken(self):
        actions = decide_on_demand_actions(
            agents=[{
                **_agent('ena', enabled=False, status='hibernated'),
                'provider_unavailable_reason': 'rate_limit: hit your usage',
            }],
            rosters=[_roster('ceo-office', ['ena'])],
            workload_by_email={},
            workload_by_team={},
        )
        # Operator 2026-09-22: rate-limited rest happens off-pool; the
        # connect-wake preempt path is the wake guarantee.
        ena_actions = [a for a in actions if a.username == 'ena']
        self.assertEqual([a for a in ena_actions if a.action == 'start'], [])

    def test_warm_pool_size_keeps_idle_reserved_seat_resident(self):
        # Explicit Hive reservation, not a static team. CEO Office / home-voice
        # seats stay up with no Pulse work so a conversation is not a cold start.
        actions = decide_on_demand_actions(
            agents=[{**_agent('hina'), 'warm_pool_size': 1}],
            rosters=[_roster('ceo-office', ['hina'])],
            workload_by_email={},
            workload_by_team={},
        )

        self.assertEqual(actions, [])

    def test_warm_pool_size_from_mcp_config_keeps_idle_seat_resident(self):
        actions = decide_on_demand_actions(
            agents=[{
                **_agent('aya'),
                'mcp_config': {'warm_pool_size': 1},
            }],
            rosters=[_roster('ceo-office', ['aya'])],
            workload_by_email={},
            workload_by_team={},
        )

        self.assertEqual(actions, [])

    def test_warm_pool_size_wakes_hibernated_reserved_seat(self):
        # Per-agent reservation, independent of the CEO Office whitelist.
        actions = decide_on_demand_actions(
            agents=[{
                **_agent('kei', enabled=False, status='hibernated'),
                'warm_pool_size': 1,
            }],
            rosters=[_roster('review', ['kei'])],
            workload_by_email={},
            workload_by_team={},
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action, 'start')
        self.assertEqual(actions[0].username, 'kei')
        self.assertIn('warm-pool', actions[0].reason)

    def test_warm_pool_size_does_not_protect_rate_limited_seat(self):
        # warm_pool_size is a per-agent reservation, not the CEO Office
        # whitelist. Rate-limit rest still applies off that team.
        actions = decide_on_demand_actions(
            agents=[{
                **_agent('kei'),
                'warm_pool_size': 1,
                'provider_unavailable_reason': 'rate_limit: hit your usage',
            }],
            rosters=[_roster('review', ['kei'])],
            workload_by_email={},
            workload_by_team={},
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action, 'stop')
        self.assertIn('rate', actions[0].reason.lower())

    def test_recent_activity_keeps_any_team_agent_warm(self):
        # "Warm" = recently active. The same review agent stays resident when it
        # had activity within the grace window — no team list involved.
        actions = decide_on_demand_actions(
            agents=[{
                **_agent('kei'),
                'last_active_at': _recent_ts(1),
                # Recently active AND reporting work. A bare timestamp with no
                # telemetry no longer holds a seat (banto, 2026-08-05).
                'activity_state': 'working',
            }],
            rosters=[_roster('review', ['kei'])],
            workload_by_email={},
            workload_by_team={},
            activity_grace_minutes=5,
        )

        self.assertEqual(actions, [])

    def test_team_work_wakes_members_of_any_team_to_match_demand(self):
        # Every team scales to actionable demand, bounded by its real agent
        # roster; no hardcoded warm/on-demand subset or one-member cap remains.
        actions = decide_on_demand_actions(
            agents=[
                _agent('ryo', enabled=False, status='hibernated'),
                _agent('jun', enabled=False, status='hibernated'),
            ],
            rosters=[_roster('engineering', ['ryo', 'jun'])],
            workload_by_email={},
            workload_by_team={'engineering': {'open': 2}},
        )

        self.assertEqual(
            [(action.action, action.username) for action in actions],
            [('start', 'ryo'), ('start', 'jun')],
        )

    def test_personal_work_starts_stopped_agent(self):
        actions = decide_on_demand_actions(
            agents=[_agent('scout', enabled=False, status='hibernated')],
            rosters=[_roster('customer-support', ['scout'])],
            workload_by_email={'scout@shizuha.com': {'open': 1}},
            workload_by_team={'customer-support': {'open': 1}},
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action, 'start')
        self.assertEqual(actions[0].username, 'scout')

    def test_team_work_starts_one_stopped_member_when_none_running(self):
        actions = decide_on_demand_actions(
            agents=[
                _agent('scout', enabled=False, status='hibernated'),
                _agent('cora', enabled=False, status='hibernated'),
            ],
            rosters=[_roster('customer-support', ['scout', 'cora'])],
            workload_by_email={},
            workload_by_team={'customer-support': {'open': 1}},
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action, 'start')
        self.assertEqual(actions[0].username, 'scout')

    def test_one_actionable_item_does_not_start_extra_member(self):
        actions = decide_on_demand_actions(
            agents=[
                _agent('scout', enabled=True, status='running'),
                _agent('cora', enabled=False, status='hibernated'),
            ],
            rosters=[_roster('customer-support', ['scout', 'cora'])],
            workload_by_email={},
            workload_by_team={'customer-support': {'open': 1}},
        )

        self.assertEqual(actions, [])

    def test_multiple_actionable_items_start_stopped_peers_when_one_is_running(self):
        actions = decide_on_demand_actions(
            agents=[
                _agent('revi', enabled=True, status='running'),
                _agent('kei', enabled=False, status='hibernated'),
                _agent('ichi', enabled=True, status='running'),
                _agent('san', enabled=True, status='running'),
            ],
            rosters=[_roster('review', ['revi', 'kei', 'ichi', 'san'])],
            workload_by_email={'revi@shizuha.com': {'in_review': 100}},
            workload_by_team={'review': {'in_review': 100}},
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action, 'start')
        self.assertEqual(actions[0].username, 'kei')
        self.assertIn('scaling roster to 4 running member(s)', actions[0].reason)

    def test_personal_start_satisfies_team_demand(self):
        actions = decide_on_demand_actions(
            agents=[
                _agent('cora', enabled=False, status='hibernated'),
                _agent('scout', enabled=False, status='hibernated'),
            ],
            rosters=[_roster('customer-support', ['cora', 'scout'])],
            workload_by_email={'cora@shizuha.com': {'open': 1}},
            workload_by_team={'customer-support': {'open': 1}},
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action, 'start')
        self.assertEqual(actions[0].username, 'cora')

    def test_team_demand_downscales_idle_excess_member(self):
        """Demand falling from three to two must not retain an idle third seat."""
        actions = decide_on_demand_actions(
            agents=[
                _agent('ichi'),
                _agent('ni'),
                _agent('san'),
            ],
            rosters=[_roster('devops', ['ichi', 'ni', 'san'])],
            workload_by_email={
                'ichi@shizuha.com': {'in_progress': 1},
                'ni@shizuha.com': {'open': 1},
                'san@shizuha.com': {'blocked': 3, 'scheduled': 2},
            },
            workload_by_team={'devops': {'open': 1, 'in_progress': 1}},
        )

        self.assertEqual(
            [(action.action, action.username) for action in actions],
            [('stop', 'san')],
        )
        self.assertIn('devops=2', actions[0].reason)

    def test_team_demand_does_not_downscale_below_required_capacity(self):
        actions = decide_on_demand_actions(
            agents=[_agent('ichi'), _agent('san')],
            rosters=[_roster('devops', ['ichi', 'san'])],
            workload_by_email={},
            workload_by_team={'devops': {'open': 2}},
        )

        self.assertEqual(actions, [])

    def test_assigned_work_protects_member_even_when_team_has_excess_capacity(self):
        actions = decide_on_demand_actions(
            agents=[_agent('ichi'), _agent('ni'), _agent('san')],
            rosters=[_roster('devops', ['ichi', 'ni', 'san'])],
            workload_by_email={
                'ichi@shizuha.com': {'in_progress': 1},
                'ni@shizuha.com': {'blocked': 1},
                'san@shizuha.com': {'open': 1},
            },
            workload_by_team={'devops': {'open': 1, 'in_progress': 1}},
        )

        self.assertEqual(
            [(action.action, action.username) for action in actions],
            [('stop', 'ni')],
        )

    def test_multi_team_member_is_not_stopped_when_other_team_needs_it(self):
        actions = decide_on_demand_actions(
            agents=[_agent('ichi'), _agent('san')],
            rosters=[
                _roster('devops', ['ichi', 'san']),
                _roster('review', ['ichi']),
            ],
            workload_by_email={},
            workload_by_team={
                'devops': {'open': 1},
                'review': {'in_review': 1},
            },
        )

        self.assertEqual(
            [(action.action, action.username) for action in actions],
            [('stop', 'san')],
        )

    def test_recent_idle_excess_member_null_telemetry_does_not_get_grace(self):
        """Idle + no heartbeat_outcome must not hold an excess seat via last_active."""
        actions = decide_on_demand_actions(
            agents=[
                _agent('ichi'),
                _agent('ni'),
                {
                    **_agent('san'),
                    'last_active_at': _recent_ts(),
                    'activity_state': 'idle',
                },
            ],
            rosters=[_roster('devops', ['ichi', 'ni', 'san'])],
            workload_by_email={
                'ichi@shizuha.com': {'open': 1},
                'ni@shizuha.com': {'open': 1},
            },
            workload_by_team={'devops': {'open': 2}},
            activity_grace_minutes=5,
        )

        self.assertEqual(
            [(action.action, action.username) for action in actions],
            [('stop', 'san')],
        )

    def test_recent_working_null_telemetry_still_gets_activity_grace(self):
        """Mid-turn / working without HB envelope still fail-closed keep-warm."""
        actions = decide_on_demand_actions(
            agents=[
                _agent('ichi'),
                _agent('ni'),
                {
                    **_agent('san'),
                    'last_active_at': _recent_ts(),
                    'activity_state': 'working',
                },
            ],
            rosters=[_roster('devops', ['ichi', 'ni', 'san'])],
            workload_by_email={
                'ichi@shizuha.com': {'open': 1},
                'ni@shizuha.com': {'open': 1},
            },
            workload_by_team={'devops': {'open': 2}},
            activity_grace_minutes=5,
        )

        self.assertEqual(actions, [])

    def test_idle_null_heartbeat_cannot_self_refresh_activity_grace(self):
        """Sole idle seat with null telemetry and no work must hibernate."""
        actions = decide_on_demand_actions(
            agents=[{
                **_agent('kai'),
                'last_active_at': _recent_ts(),
                'activity_state': 'idle',
            }],
            rosters=[_roster('engineering', ['kai'])],
            workload_by_email={},
            workload_by_team={},
            activity_grace_minutes=5,
        )

        self.assertEqual(
            [(action.action, action.username) for action in actions],
            [('stop', 'kai')],
        )

    def test_queue_empty_heartbeat_cannot_self_refresh_activity_grace(self):
        """An empty paid heartbeat is not activity that justifies another one."""
        heartbeat_at = _recent_ts()
        actions = decide_on_demand_actions(
            agents=[{
                **_agent('akira'),
                'last_active_at': heartbeat_at,
                'heartbeat_outcome': 'queue_empty',
                'heartbeat_observed_at': heartbeat_at,
            }],
            rosters=[_roster('security', ['akira'])],
            workload_by_email={},
            workload_by_team={'security': {'blocked': 19}},
            activity_grace_minutes=5,
        )

        self.assertEqual(
            [(action.action, action.username) for action in actions],
            [('stop', 'akira')],
        )

    def test_all_blocked_heartbeat_cannot_self_refresh_activity_grace(self):
        heartbeat_at = _recent_ts()
        actions = decide_on_demand_actions(
            agents=[{
                **_agent('mika'),
                'last_active_at': heartbeat_at,
                'heartbeat_outcome': 'all_blocked',
                'heartbeat_observed_at': heartbeat_at,
            }],
            rosters=[_roster('qa', ['mika'])],
            workload_by_email={'mika@shizuha.com': {'blocked': 36}},
            workload_by_team={'qa': {'blocked': 36}},
            activity_grace_minutes=5,
        )

        self.assertEqual(
            [(action.action, action.username) for action in actions],
            [('stop', 'mika')],
        )

    def test_needs_help_ready_zero_cannot_self_refresh_activity_grace(self):
        """Empty-queue needs_help storms must not hold a paid seat via grace."""
        heartbeat_at = _recent_ts()
        actions = decide_on_demand_actions(
            agents=[{
                **_agent('san'),
                'last_active_at': heartbeat_at,
                'heartbeat_outcome': 'needs_help',
                'heartbeat_observed_at': heartbeat_at,
                'heartbeat_ready_task_count': 0,
            }],
            rosters=[_roster('devops', ['san'])],
            workload_by_email={},
            workload_by_team={'devops': {'blocked': 12}},
            activity_grace_minutes=5,
        )

        self.assertEqual(
            [(action.action, action.username) for action in actions],
            [('stop', 'san')],
        )

    def test_not_observed_heartbeat_cannot_self_refresh_activity_grace(self):
        heartbeat_at = _recent_ts()
        actions = decide_on_demand_actions(
            agents=[{
                **_agent('sara'),
                'last_active_at': heartbeat_at,
                'heartbeat_outcome': 'not_observed',
                'heartbeat_observed_at': heartbeat_at,
                'heartbeat_ready_task_count': 0,
            }],
            rosters=[_roster('engineering', ['sara'])],
            workload_by_email={},
            workload_by_team={},
            activity_grace_minutes=5,
        )

        self.assertEqual(
            [(action.action, action.username) for action in actions],
            [('stop', 'sara')],
        )

    def test_needs_help_with_ready_tasks_keeps_activity_grace(self):
        """Stuck-on-ready needs_help is real work — do not hibernate mid-struggle."""
        heartbeat_at = _recent_ts()
        actions = decide_on_demand_actions(
            agents=[{
                **_agent('jun'),
                'last_active_at': heartbeat_at,
                'heartbeat_outcome': 'needs_help',
                'heartbeat_observed_at': heartbeat_at,
                'heartbeat_ready_task_count': 12,
            }],
            rosters=[_roster('engineering', ['jun'])],
            workload_by_email={},
            workload_by_team={},
            activity_grace_minutes=5,
        )

        self.assertEqual(actions, [])

    def test_real_activity_after_stale_empty_heartbeat_keeps_grace(self):
        actions = decide_on_demand_actions(
            agents=[{
                **_agent('akira'),
                'last_active_at': _recent_ts(1),
                'heartbeat_outcome': 'queue_empty',
                'heartbeat_observed_at': _recent_ts(10),
            }],
            rosters=[_roster('security', ['akira'])],
            workload_by_email={},
            workload_by_team={},
            activity_grace_minutes=5,
        )

        self.assertEqual(actions, [])

    def test_rate_limited_running_seat_hibernates_and_wakes_healthy_peer(self):
        """Rate-limit is rest-until-workable: stop the limited seat, start a peer."""
        import time as _time
        now_ms = int(_time.time() * 1000)
        limited = {
            **_agent('aoi'),
            'recent_errors': [{
                'ts': now_ms,
                'level': 'error',
                'msg': "rate_limit: You've hit your usage limit. Try again at Aug 1st.",
            }],
            'last_active_at': _recent_ts(),
            'activity_state': 'idle',
        }
        peer = {**_agent('sora', enabled=False, status='hibernated'), 'enabled': False}
        actions = decide_on_demand_actions(
            agents=[limited, peer],
            rosters=[_roster('architecture', ['aoi', 'sora'])],
            workload_by_email={},
            workload_by_team={'architecture': {'open': 2}},
        )
        by_action = {}
        for action in actions:
            by_action.setdefault(action.action, set()).add(action.username)
        self.assertIn('aoi', by_action.get('stop', set()))
        self.assertIn('sora', by_action.get('start', set()))
        stop = next(a for a in actions if a.action == 'stop' and a.username == 'aoi')
        self.assertIn('rate/usage limited', stop.reason)

    def test_rate_limited_with_personal_work_still_hibernates(self):
        """Assigned work cannot be processed while limited — do not keep the seat warm."""
        import time as _time
        now_ms = int(_time.time() * 1000)
        actions = decide_on_demand_actions(
            agents=[{
                **_agent('san'),
                'recent_errors': [{
                    'ts': now_ms,
                    'msg': 'rate_limit: usage limit',
                }],
                'activity_state': 'idle',
            }],
            rosters=[_roster('devops', ['san'])],
            workload_by_email={'san@shizuha.com': {'open': 3}},
            workload_by_team={'devops': {'open': 3}},
        )
        self.assertEqual(
            [(a.action, a.username) for a in actions],
            [('stop', 'san')],
        )

    def test_rate_limited_stopped_agent_not_woken_for_personal_work(self):
        """Do not thrash START on a still-limited seat; wait for cooldown age-out."""
        import time as _time
        now_ms = int(_time.time() * 1000)
        stopped = {
            **_agent('san', enabled=False, status='hibernated'),
            'enabled': False,
            'recent_errors': [{
                'ts': now_ms,
                'msg': 'rate_limit: usage limit',
            }],
        }
        actions = decide_on_demand_actions(
            agents=[stopped],
            rosters=[_roster('devops', ['san'])],
            workload_by_email={'san@shizuha.com': {'open': 2}},
            workload_by_team={},
        )
        self.assertEqual(actions, [])

    def test_stale_rate_limit_allows_wake(self):
        """After the cooldown window, personal work may START the agent again."""
        import time as _time
        old_ms = int(_time.time() * 1000) - (7 * 60 * 60 * 1000)  # 7h ago
        stopped = {
            **_agent('san', enabled=False, status='hibernated'),
            'enabled': False,
            'recent_errors': [{
                'ts': old_ms,
                'msg': 'rate_limit: usage limit',
            }],
        }
        actions = decide_on_demand_actions(
            agents=[stopped],
            rosters=[_roster('devops', ['san'])],
            workload_by_email={'san@shizuha.com': {'open': 1}},
            workload_by_team={},
        )
        self.assertEqual(
            [(a.action, a.username) for a in actions],
            [('start', 'san')],
        )

    def test_provider_dead_excess_member_cannot_self_refresh_activity_grace(self):
        """Empty unhealthy heartbeats must not keep an excess paid seat alive."""
        actions = decide_on_demand_actions(
            agents=[
                _agent('ni'),
                {
                    **_agent('san'),
                    'provider_unavailable': True,
                    'last_active_at': _recent_ts(),
                },
            ],
            rosters=[_roster('devops', ['ni', 'san'])],
            workload_by_email={
                'ni@shizuha.com': {'open': 1},
                'san@shizuha.com': {'blocked': 8},
            },
            # Demand exceeds the only healthy seat. The provider-dead member
            # still contributes zero usable capacity, so stopping it cannot
            # make the deficit worse and must not be blocked by the target.
            workload_by_team={'devops': {'open': 2}},
            activity_grace_minutes=5,
        )

        self.assertEqual(
            [(action.action, action.username) for action in actions],
            [('stop', 'san')],
        )


class UnreadWakeTests(SimpleTestCase):
    """HIVE-628: unread Connect DMs wake stopped agents and block stops."""

    def test_unread_messages_start_stopped_agent(self):
        actions = decide_on_demand_actions(
            agents=[_agent('scout', enabled=False, status='hibernated')],
            rosters=[_roster('customer-support', ['scout'])],
            workload_by_email={},
            workload_by_team={},
            unread_by_email={'scout@shizuha.com': 3},
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action, 'start')
        self.assertIn('unread Connect message', actions[0].reason)

    def test_unread_messages_block_idle_stop(self):
        actions = decide_on_demand_actions(
            agents=[_agent('scout')],
            rosters=[_roster('customer-support', ['scout'])],
            workload_by_email={},
            workload_by_team={},
            unread_by_email={'scout@shizuha.com': 1},
        )
        self.assertEqual(actions, [])

    def test_no_unread_keeps_existing_behaviour(self):
        actions = decide_on_demand_actions(
            agents=[_agent('scout')],
            rosters=[_roster('customer-support', ['scout'])],
            workload_by_email={},
            workload_by_team={},
            unread_by_email={},
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action, 'stop')

    def test_unread_start_not_duplicated_when_agent_has_work(self):
        actions = decide_on_demand_actions(
            agents=[_agent('scout', enabled=False, status='hibernated')],
            rosters=[_roster('customer-support', ['scout'])],
            workload_by_email={'scout@shizuha.com': {'in_progress': 1}},
            workload_by_team={},
            unread_by_email={'scout@shizuha.com': 2},
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].reason, 'stopped agent has assigned work')


class GracePeriodTests(SimpleTestCase):
    """PLAT-4490: DM grace timer + recent-activity grace window."""

    def test_recent_dm_blocks_stop(self):
        """Agent with a DM within the grace window should not be stopped."""
        actions = decide_on_demand_actions(
            agents=[_agent('scout')],
            rosters=[_roster('customer-support', ['scout'])],
            workload_by_email={},
            workload_by_team={},
            last_dm_at_by_email={'SCOUT@SHIZUHA.COM': _recent_ts()},
            dm_grace_minutes=30,
        )
        self.assertEqual(actions, [])

    def test_old_dm_does_not_block_stop(self):
        """Agent with a DM outside the grace window should still be stopped."""
        actions = decide_on_demand_actions(
            agents=[_agent('scout')],
            rosters=[_roster('customer-support', ['scout'])],
            workload_by_email={},
            workload_by_team={},
            last_dm_at_by_email={'scout@shizuha.com': '2020-01-01T00:00:00Z'},
            dm_grace_minutes=5,
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action, 'stop')

    def test_recent_activity_blocks_stop(self):
        """A recently-active agent that is actually WORKING is not stopped.

        The fixture carries `activity_state='working'` deliberately. PLAT-4490's
        subject is "do not stop an agent mid-turn", and a mid-turn agent says so.
        This test used to omit any telemetry, which meant it also asserted that a
        bare refreshed timestamp is enough — see the companion test below for why
        that is not true.
        """
        actions = decide_on_demand_actions(
            agents=[{
                **_agent('scout'),
                'last_active_at': _recent_ts(),
                'activity_state': 'working',
            }],
            rosters=[_roster('customer-support', ['scout'])],
            workload_by_email={},
            workload_by_team={},
            activity_grace_minutes=30,
        )
        self.assertEqual(actions, [])

    def test_recent_activity_with_no_telemetry_does_not_block_stop(self):
        """A refreshed clock alone is not evidence of work.

        Operator 2026-08-05 on banto: Running with an empty queue, held resident
        because `last_active_at` was two minutes old while `heartbeat_outcome`
        and `activity_state` were both null. A heartbeat refreshes
        last_active_at whether or not the turn did anything, so grace granted on
        that signal alone renews forever and the seat never hibernates.

        Reaching this point already means: no actionable personal work, no
        unread DM, no recent DM. Stopping is the intended outcome — rest, not
        cull; the sweep restarts it the moment work or a message arrives.
        """
        actions = decide_on_demand_actions(
            agents=[{**_agent('scout'), 'last_active_at': _recent_ts()}],
            rosters=[_roster('customer-support', ['scout'])],
            workload_by_email={},
            workload_by_team={},
            activity_grace_minutes=30,
        )
        self.assertEqual([a.action for a in actions], ['stop'])

    def test_old_activity_does_not_block_stop(self):
        """Agent with old last_active_at should still be stopped."""
        actions = decide_on_demand_actions(
            agents=[{**_agent('scout'), 'last_active_at': '2020-01-01T00:00:00Z'}],
            rosters=[_roster('customer-support', ['scout'])],
            workload_by_email={},
            workload_by_team={},
            activity_grace_minutes=5,
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action, 'stop')

    def test_zero_grace_disables_safeguard(self):
        """Setting grace to 0 should not block stop regardless of recent DM."""
        actions = decide_on_demand_actions(
            agents=[_agent('scout')],
            rosters=[_roster('customer-support', ['scout'])],
            workload_by_email={},
            workload_by_team={},
            last_dm_at_by_email={'scout@shizuha.com': _recent_ts()},
            dm_grace_minutes=0,
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action, 'stop')

    def test_missing_last_dm_at_does_not_block(self):
        """No last_dm_at entry should not block stop."""
        actions = decide_on_demand_actions(
            agents=[_agent('scout')],
            rosters=[_roster('customer-support', ['scout'])],
            workload_by_email={},
            workload_by_team={},
            last_dm_at_by_email={},
            dm_grace_minutes=5,
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action, 'stop')


class ConnectLifecycleStateTests(SimpleTestCase):
    @patch.dict(os.environ, {'CONNECT_INTERNAL_API_URL': 'http://connect.test/api'})
    @patch('fleet.management.commands.reconcile_on_demand_agents.requests.get')
    def test_parses_unread_and_latest_inbound_dm_from_one_snapshot(self, get):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            'complete': True,
            'lifecycle_delivery_complete': True,
            'recipients': [
                {
                    'user_email': 'SCOUT@SHIZUHA.COM',
                    'unread_total': 2,
                    'pending_delivery_total': 1,
                    'last_dm_at': '2026-07-13T02:00:00Z',
                },
                {
                    'user_email': 'cora@shizuha.com',
                    'unread_total': 0,
                    'pending_delivery_total': 0,
                    'last_dm_at': None,
                },
            ],
        }
        get.return_value = response

        snapshot = Command()._fetch_connect_state()

        self.assertTrue(snapshot.available)
        self.assertEqual(snapshot.unread_by_email, {'scout@shizuha.com': 1, 'cora@shizuha.com': 0})
        self.assertEqual(snapshot.last_dm_at_by_email, {'scout@shizuha.com': '2026-07-13T02:00:00Z'})
        get.assert_called_once_with(
            'http://connect.test/api/internal/unread-recipients/',
            headers={'X-Internal-Service': 'shizuha-hive'},
            timeout=10,
        )

    @patch('fleet.management.commands.reconcile_on_demand_agents.requests.get')
    def test_connect_failure_fails_closed_instead_of_stopping_agents(self, get):
        get.side_effect = RuntimeError('connect unavailable')

        snapshot = Command()._fetch_connect_state()

        self.assertFalse(snapshot.available)
        self.assertEqual(snapshot.unread_by_email, {})
        self.assertEqual(snapshot.last_dm_at_by_email, {})
        self.assertIn('connect unavailable', snapshot.error)

    @patch.dict(os.environ, {'CONNECT_INTERNAL_API_URL': 'http://connect.test/api'})
    @patch('fleet.management.commands.reconcile_on_demand_agents.requests.get')
    def test_incomplete_connect_snapshot_is_unavailable_for_stops(self, get):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            'complete': False,
            'lifecycle_delivery_complete': False,
            'recipients': [{
                'user_email': 'first-500-only@shizuha.com',
                'unread_total': 0,
                'last_dm_at': None,
            }],
        }
        get.return_value = response

        snapshot = Command()._fetch_connect_state()

        self.assertFalse(snapshot.available)
        self.assertEqual(snapshot.unread_by_email, {})
        self.assertEqual(snapshot.last_dm_at_by_email, {})
        self.assertIn('incomplete', snapshot.error)

    @patch('fleet.models.archived_usernames', return_value=set())
    @patch('fleet.management.commands.reconcile_on_demand_agents.set_lifecycle_agent_enabled')
    @patch('fleet.management.commands.reconcile_on_demand_agents.list_lifecycle_agents')
    @patch.object(Command, '_fetch_connect_state')
    @patch.object(Command, '_fetch_pulse_snapshot')
    @patch.object(Command, '_fetch_pulse_rosters')
    def test_connect_failure_applies_workload_start_but_skips_idle_stop(
        self,
        rosters,
        pulse_snapshot,
        connect_snapshot,
        runtime_agents,
        set_enabled,
        _archived,
    ):
        rosters.return_value = [
            _roster('customer-support', ['scout']),
            _roster('product', ['cora']),
        ]
        pulse_snapshot.return_value = {
            'workload': {'scout@shizuha.com': {'open': 1}},
            'team_workload': {'customer-support': {'open': 1}},
        }
        runtime_agents.return_value = [
            _agent('scout', enabled=False, status='hibernated'),
            _agent('cora', enabled=True, status='running'),
        ]
        connect_snapshot.return_value = ConnectLifecycleSnapshot(
            available=False,
            unread_by_email={},
            last_dm_at_by_email={},
            error='Timeout: connect unavailable',
        )
        set_enabled.return_value = (True, 'started')
        stdout = StringIO()

        with self.assertRaisesMessage(CommandError, 'PARTIAL reconcile'):
            call_command('reconcile_on_demand_agents', '--apply', stdout=stdout)

        set_enabled.assert_called_once_with('id-scout', True)
        self.assertIn('START scout', stdout.getvalue())
        self.assertNotIn('STOP cora', stdout.getvalue())


class TeamDemandCommandTests(SimpleTestCase):
    """Exact group-queue wake path for a stopped Hive-native team seat."""

    @patch('fleet.models.archived_usernames', return_value=set())
    @patch('fleet.management.commands.reconcile_on_demand_agents.hibernate_lifecycle_agent')
    @patch('fleet.management.commands.reconcile_on_demand_agents.set_lifecycle_agent_enabled')
    @patch('fleet.management.commands.reconcile_on_demand_agents.list_lifecycle_agents')
    @patch.object(Command, '_fetch_connect_state')
    @patch.object(Command, '_fetch_pulse_snapshot')
    @patch.object(Command, '_fetch_pulse_rosters')
    def test_runtime_hibernation_sync_then_personal_work_wakes_normally(
        self,
        rosters,
        pulse_snapshot,
        connect_snapshot,
        lifecycle_agents,
        set_enabled,
        hibernate,
        _archived,
    ):
        """Production order: repair typed export, then assigned work -> START."""
        rosters.return_value = [_roster('architecture', ['aoi'])]
        pulse_snapshot.side_effect = [
            {'workload': {}, 'team_workload': {}},
            {
                'workload': {'aoi@shizuha.com': {'open': 1}},
                'team_workload': {'architecture': {'open': 1}},
            },
        ]
        lifecycle_agents.side_effect = [
            [{
                **_agent('aoi', enabled=False, status='hibernated'),
                'lifecycle_plane': 'runtime',
                'lifecycle_state': 'operator_stopped',
            }],
            [{
                **_agent('aoi', enabled=False, status='hibernated'),
                'lifecycle_plane': 'runtime',
                'lifecycle_state': 'hibernated',
            }],
        ]
        connect_snapshot.return_value = ConnectLifecycleSnapshot(
            available=True,
            unread_by_email={},
            last_dm_at_by_email={},
        )
        hibernate.return_value = (
            True,
            {'status': 'hibernated', 'lifecycle_state': 'hibernated'},
        )
        set_enabled.return_value = (
            True,
            {'status': 'enabled', 'lifecycle_state': 'enabled'},
        )

        stdout = StringIO()
        with patch(
            'fleet.external_agents.fresh_agent_telemetry',
            return_value={},
        ):
            call_command('reconcile_on_demand_agents', '--apply', stdout=stdout)
            call_command('reconcile_on_demand_agents', '--apply', stdout=stdout)

        hibernate.assert_called_once_with(
            'id-aoi',
            reason='reassert Hive hibernation intent to runtime exporter',
        )
        set_enabled.assert_called_once_with('id-aoi', True)
        self.assertIn('SYNC_HIBERNATION aoi', stdout.getvalue())
        self.assertIn('START aoi', stdout.getvalue())

    @patch('fleet.models.archived_usernames', return_value=set())
    @patch('fleet.management.commands.reconcile_on_demand_agents.hibernate_lifecycle_agent')
    @patch('fleet.management.commands.reconcile_on_demand_agents.list_lifecycle_agents')
    @patch.object(Command, '_fetch_connect_state')
    @patch.object(Command, '_fetch_pulse_snapshot')
    @patch.object(Command, '_fetch_pulse_rosters')
    def test_failed_stop_retry_refences_and_defers_if_turn_started(
        self,
        rosters,
        pulse_snapshot,
        connect_snapshot,
        lifecycle_agents,
        hibernate,
        _archived,
    ):
        """Real command order: plan idle, stop fails, retry observes working."""
        import time

        rosters.return_value = [_roster('devops', ['san'])]
        pulse_snapshot.return_value = {
            'workload': {},
            'team_workload': {},
        }
        lifecycle_agents.return_value = [_agent('san')]
        connect_snapshot.return_value = ConnectLifecycleSnapshot(
            available=True,
            unread_by_email={},
            last_dm_at_by_email={},
        )
        hibernate.return_value = (False, 'runtime control timed out')
        idle = {
            'san': {
                'activity': {'state': 'idle'},
                'received_at': time.time(),
            },
        }
        working = {
            'san': {
                'activity': {'state': 'working'},
                'received_at': time.time(),
            },
        }

        stdout = StringIO()
        with patch.object(Command, '_apply_health_signals'), patch(
            'fleet.external_agents.fresh_agent_telemetry',
            side_effect=[idle, idle, working],
        ) as telemetry:
            call_command('reconcile_on_demand_agents', '--apply', stdout=stdout)

        hibernate.assert_called_once_with(
            'id-san',
            reason='idle on-demand agent has no personal or team work',
        )
        self.assertEqual(telemetry.call_count, 3)
        self.assertIn('DEFER RETRY STOP san', stdout.getvalue())
        self.assertIn('runtime reports working', stdout.getvalue())

    @patch('fleet.models.archived_usernames', return_value=set())
    @patch('fleet.management.commands.reconcile_on_demand_agents.hibernate_lifecycle_agent')
    @patch('fleet.management.commands.reconcile_on_demand_agents.set_lifecycle_agent_enabled')
    @patch('fleet.management.commands.reconcile_on_demand_agents.list_lifecycle_agents')
    @patch.object(Command, '_fetch_connect_state')
    @patch.object(Command, '_fetch_pulse_snapshot')
    @patch.object(Command, '_fetch_pulse_rosters')
    def test_consecutive_sweeps_scale_down_when_team_demand_falls(
        self,
        rosters,
        pulse_snapshot,
        connect_snapshot,
        lifecycle_agents,
        set_enabled,
        hibernate,
        _archived,
    ):
        """Production order: 3-item sweep -> 2-item sweep -> one durable stop."""
        rosters.return_value = [_roster('devops', ['ichi', 'ni', 'san'])]
        pulse_snapshot.side_effect = [
            {
                'workload': {
                    'ichi@shizuha.com': {'in_progress': 1},
                    'ni@shizuha.com': {'in_progress': 1},
                    'san@shizuha.com': {'in_progress': 1},
                },
                'team_workload': {'devops': {'in_progress': 3}},
            },
            {
                'workload': {
                    'ichi@shizuha.com': {'in_progress': 1},
                    'ni@shizuha.com': {'in_progress': 1},
                    'san@shizuha.com': {'blocked': 3},
                },
                'team_workload': {'devops': {'in_progress': 2}},
            },
        ]
        lifecycle_agents.return_value = [
            _agent('ichi'),
            _agent('ni'),
            _agent('san'),
        ]
        connect_snapshot.return_value = ConnectLifecycleSnapshot(
            available=True,
            unread_by_email={},
            last_dm_at_by_email={},
        )
        hibernate.return_value = (True, {'status': 'hibernated'})

        import time
        with patch(
            'fleet.external_agents.fresh_agent_telemetry',
            return_value={
                'san': {
                    'activity': {'state': 'idle'},
                    'received_at': time.time(),
                },
            },
        ):
            call_command('reconcile_on_demand_agents', '--apply')
            call_command('reconcile_on_demand_agents', '--apply')

        set_enabled.assert_not_called()
        hibernate.assert_called_once_with(
            'id-san',
            reason='idle running seat exceeds current actionable team demand: devops=2',
        )

    @patch('fleet.models.archived_usernames', return_value=set())
    @patch('fleet.management.commands.reconcile_on_demand_agents.set_lifecycle_agent_enabled')
    @patch('fleet.management.commands.reconcile_on_demand_agents.list_lifecycle_agents')
    @patch.object(Command, '_fetch_connect_state')
    @patch.object(Command, '_fetch_pulse_snapshot')
    @patch.object(Command, '_fetch_pulse_rosters')
    def test_actionable_team_queue_wakes_sole_stopped_native_member(
        self,
        rosters,
        pulse_snapshot,
        connect_snapshot,
        lifecycle_agents,
        set_enabled,
        _archived,
    ):
        """Pulse team work -> policy -> canonical Hive lifecycle actuation."""
        tomo_id = '421bd9a2-a484-4a92-bf26-0d42731cfaf0'
        rosters.return_value = [_roster('research-analytics', ['tomo'])]
        pulse_snapshot.return_value = {
            'workload': {'tomo@shizuha.com': {'open': 0}},
            'team_workload': {
                'roster:research-analytics': {'open': 2, 'blocked': 4},
            },
        }
        lifecycle_agents.return_value = [{
            **_agent('tomo', enabled=False, status='hibernated'),
            'id': tomo_id,
            'lifecycle_plane': 'native',
        }]
        connect_snapshot.return_value = ConnectLifecycleSnapshot(
            available=True,
            unread_by_email={},
            last_dm_at_by_email={},
        )
        set_enabled.return_value = (True, {'plane': 'native', 'status': 'running'})

        stdout = StringIO()
        call_command('reconcile_on_demand_agents', '--apply', stdout=stdout)

        set_enabled.assert_called_once_with(tomo_id, True)
        self.assertIn('START tomo', stdout.getvalue())
        self.assertIn(
            'on-demand team research-analytics has 2 actionable item(s); '
            'scaling roster to 1 running member(s)',
            stdout.getvalue(),
        )

    @patch('fleet.models.archived_usernames', return_value=set())
    @patch('fleet.management.commands.reconcile_on_demand_agents.set_lifecycle_agent_enabled')
    @patch('fleet.management.commands.reconcile_on_demand_agents.list_lifecycle_agents')
    @patch.object(Command, '_fetch_connect_state')
    @patch.object(Command, '_fetch_pulse_snapshot')
    @patch.object(Command, '_fetch_pulse_rosters')
    def test_scoped_research_snapshot_wakes_research_analytics_roster(
        self,
        rosters,
        pulse_snapshot,
        connect_snapshot,
        lifecycle_agents,
        set_enabled,
        _archived,
    ):
        """Admin `Research` demand must wake the canonical research-analytics seats."""
        rosters.return_value = [
            _roster(
                'research-analytics',
                ['scout', 'hana', 'tomo'],
                organization_slug='shizuha',
            ),
        ]
        pulse_snapshot.return_value = {
            'workload': {},
            'team_workload': {'shizuha::research': {'open': 36}},
            'teams': [{
                'id': 30,
                'name': 'Research',
                'organization_slug': 'shizuha',
            }],
        }
        lifecycle_agents.return_value = [
            {
                **_agent(username, enabled=False, status='hibernated'),
                'id': f'native-{username}',
                'lifecycle_plane': 'native',
            }
            for username in ('scout', 'hana', 'tomo')
        ]
        connect_snapshot.return_value = ConnectLifecycleSnapshot(
            available=True,
            unread_by_email={},
            last_dm_at_by_email={},
        )
        set_enabled.return_value = (True, {'plane': 'native', 'status': 'running'})

        stdout = StringIO()
        call_command('reconcile_on_demand_agents', '--apply', stdout=stdout)

        self.assertCountEqual(
            [item.args for item in set_enabled.call_args_list],
            [
                ('native-scout', True),
                ('native-hana', True),
                ('native-tomo', True),
            ],
        )
        self.assertIn(
            'on-demand team shizuha::research-analytics has 36 actionable item(s); '
            'scaling roster to 3 running member(s)',
            stdout.getvalue(),
        )


class TeamWorkloadNormalizationTests(SimpleTestCase):
    def test_maps_numeric_snapshot_team_id_to_roster_slug(self):
        normalized = _normalize_team_workload(
            {'17': {'open': 2, 'blocked': 1}},
            [{'id': 17, 'team_name': 'Accounting'}],
        )
        self.assertEqual(normalized, {'accounting': {'open': 2, 'blocked': 1}})

    def test_merges_id_and_slug_aliases_without_losing_counts(self):
        normalized = _normalize_team_workload(
            {'17': {'open': 2}, 'accounting': {'open': 1}},
            [{'id': 17, 'team_name': 'Accounting'}],
        )
        self.assertEqual(normalized, {'accounting': {'open': 3}})

    def test_maps_numeric_ids_via_snapshot_teams_when_roster_lacks_ids(self):
        """Production Pulse team-roster rows often omit id; snapshot teams have them.

        Regression: without this path every team_workload key stays numeric,
        memberships use slugs, and on-demand false-stops agents with real team work.
        """
        normalized = _normalize_team_workload(
            {
                '15': {'open': 0, 'in_progress': 2, 'blocked': 34},
                '12': {'in_progress': 7},
                'roster:research-analytics': {'scheduled': 4},
            },
            # Roster has slug only — matches live /admin/team-roster/ shape.
            [
                {'team_name': 'devops', 'is_active': True},
                {'team_name': 'admin-ops', 'is_active': True},
                {'team_name': 'research-analytics', 'is_active': True},
            ],
            snapshot_teams=[
                {'id': 15, 'name': 'DevOps'},
                {'id': 12, 'name': 'Admin Ops'},
            ],
        )
        self.assertEqual(normalized['devops']['in_progress'], 2)
        self.assertEqual(normalized['admin-ops']['in_progress'], 7)
        self.assertEqual(normalized['research-analytics']['scheduled'], 4)
        self.assertNotIn('15', normalized)
        self.assertNotIn('12', normalized)

    def test_same_named_org_team_ids_remain_distinct(self):
        normalized = _normalize_team_workload(
            {
                '1': {'open': 2, 'blocked': 3},
                '26': {'open': 0, 'blocked': 2},
                '28': {'open': 0, 'blocked': 4},
            },
            [
                _roster('accounting', ['banto'], organization_slug='shizuha'),
                _roster('accounting', ['kane'], organization_slug='shizuha-digital'),
                _roster('accounting', ['sato'], organization_slug='personal-3'),
            ],
            snapshot_teams=[
                {'id': 1, 'name': 'Accounting', 'organization_slug': 'shizuha'},
                {'id': 26, 'name': 'Accounting', 'organization_slug': 'shizuha-digital'},
                {'id': 28, 'name': 'Accounting', 'organization_slug': 'personal-3'},
            ],
        )

        self.assertEqual(
            normalized,
            {
                'shizuha::accounting': {'open': 2, 'blocked': 3},
                'shizuha-digital::accounting': {'open': 0, 'blocked': 2},
                'personal-3::accounting': {'open': 0, 'blocked': 4},
            },
        )

    def test_scoped_research_alias_maps_to_research_analytics_roster(self):
        normalized = _normalize_team_workload(
            {'shizuha::research': {'open': 36}},
            [_roster('research-analytics', ['scout'], organization_slug='shizuha')],
            snapshot_teams=[{
                'id': 30,
                'name': 'Research',
                'organization_slug': 'shizuha',
            }],
        )

        self.assertEqual(
            normalized,
            {'shizuha::research-analytics': {'open': 36}},
        )


class PulseSnapshotScopeTests(SimpleTestCase):
    @patch.object(Command, '_pulse_get')
    def test_on_demand_snapshot_fans_out_across_all_organizations(self, pulse_get):
        """A Digital/Trading queue must be visible to the lifecycle reconciler."""
        pulse_get.return_value = {'team_workload': {}}

        snapshot = Command()._fetch_pulse_snapshot()

        self.assertEqual(snapshot, {'team_workload': {}})
        pulse_get.assert_called_once_with(
            '/admin/org-chart/snapshot/?organization_slug=all'
        )

    @patch(
        'fleet.pulse_auth.time.sleep'
    )
    @patch(
        'fleet.pulse_auth.requests.get'
    )
    @patch('fleet.pulse_auth.pulse_fleet_headers', return_value={'Authorization': 'Bearer test'})
    @override_settings(HIVE_PULSE_API_URL='http://pulse/api')
    def test_pulse_get_retries_transient_timeout_without_using_stale_state(
        self,
        _headers,
        get,
        sleep,
    ):
        response = Mock(status_code=200)
        response.json.return_value = {'teams': [{'team_name': 'devops'}]}
        get.side_effect = [
            requests.Timeout('rolling deployment'),
            response,
        ]

        payload = Command()._pulse_get('/admin/team-roster/')

        self.assertEqual(payload, {'teams': [{'team_name': 'devops'}]})
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(1)

    @patch(
        'fleet.pulse_auth.time.sleep'
    )
    @patch(
        'fleet.pulse_auth.requests.get'
    )
    @patch('fleet.pulse_auth.pulse_fleet_headers', return_value={'Authorization': 'Bearer test'})
    @override_settings(HIVE_PULSE_API_URL='http://pulse/api')
    def test_pulse_get_fails_closed_after_bounded_retries(
        self,
        _headers,
        get,
        sleep,
    ):
        get.side_effect = requests.Timeout('still unavailable')

        with self.assertRaisesMessage(CommandError, 'after 3 attempts'):
            Command()._pulse_get('/admin/team-roster/')

        self.assertEqual(get.call_count, 3)
        self.assertEqual(
            sleep.call_args_list,
            [call(1), call(2)],
        )


class ArchivedDesiredStateTests(SimpleTestCase):
    @patch('fleet.models.archived_usernames', return_value={'misaki'})
    @patch('fleet.management.commands.reconcile_on_demand_agents.set_lifecycle_agent_enabled')
    @patch('fleet.management.commands.reconcile_on_demand_agents.list_lifecycle_agents')
    @patch.object(Command, '_fetch_connect_state')
    @patch.object(Command, '_fetch_pulse_snapshot')
    @patch.object(Command, '_fetch_pulse_rosters')
    def test_running_archived_agent_is_stopped_through_hive_lifecycle(
        self,
        rosters,
        pulse_snapshot,
        connect_snapshot,
        runtime_agents,
        set_enabled,
        _archived,
    ):
        rosters.return_value = [_roster('accounting', ['misaki'])]
        pulse_snapshot.return_value = {'workload': {}, 'team_workload': {}}
        runtime_agents.return_value = [_agent('misaki')]
        connect_snapshot.return_value = ConnectLifecycleSnapshot(
            available=True,
            unread_by_email={'misaki@shizuha.com': 99},
            last_dm_at_by_email={},
        )
        set_enabled.return_value = (True, 'stopped')

        call_command('reconcile_on_demand_agents', '--apply')

        set_enabled.assert_called_once_with('id-misaki', False)

    @patch('fleet.models.archived_usernames', return_value={'misaki'})
    @patch('fleet.management.commands.reconcile_on_demand_agents.set_lifecycle_agent_enabled')
    @patch('fleet.management.commands.reconcile_on_demand_agents.list_lifecycle_agents')
    @patch.object(Command, '_fetch_connect_state')
    @patch.object(Command, '_fetch_pulse_snapshot')
    @patch.object(Command, '_fetch_pulse_rosters')
    def test_transient_lifecycle_failure_retries_after_initial_batch(
        self,
        rosters,
        pulse_snapshot,
        connect_snapshot,
        runtime_agents,
        set_enabled,
        _archived,
    ):
        rosters.return_value = [_roster('accounting', ['misaki'])]
        pulse_snapshot.return_value = {'workload': {}, 'team_workload': {}}
        runtime_agents.return_value = [_agent('misaki')]
        connect_snapshot.return_value = ConnectLifecycleSnapshot(
            available=True,
            unread_by_email={},
            last_dm_at_by_email={},
        )
        set_enabled.side_effect = [
            (False, 'runtime control timed out'),
            (True, 'stopped on retry'),
        ]

        stdout = StringIO()
        call_command('reconcile_on_demand_agents', '--apply', stdout=stdout)

        self.assertEqual(set_enabled.call_count, 2)
        self.assertIn('RETRY STOP misaki', stdout.getvalue())

    @patch('fleet.models.archived_usernames', return_value={'misaki'})
    @patch('fleet.management.commands.reconcile_on_demand_agents.set_lifecycle_agent_enabled')
    @patch('fleet.management.commands.reconcile_on_demand_agents.list_lifecycle_agents')
    @patch.object(Command, '_fetch_connect_state')
    @patch.object(Command, '_fetch_pulse_snapshot')
    @patch.object(Command, '_fetch_pulse_rosters')
    def test_persistent_lifecycle_failure_fails_job_visibly(
        self,
        rosters,
        pulse_snapshot,
        connect_snapshot,
        runtime_agents,
        set_enabled,
        _archived,
    ):
        rosters.return_value = [_roster('accounting', ['misaki'])]
        pulse_snapshot.return_value = {'workload': {}, 'team_workload': {}}
        runtime_agents.return_value = [_agent('misaki')]
        connect_snapshot.return_value = ConnectLifecycleSnapshot(
            available=True,
            unread_by_email={},
            last_dm_at_by_email={},
        )
        set_enabled.return_value = (False, 'runtime control unavailable')

        with self.assertRaisesMessage(CommandError, 'incomplete after retry: misaki'):
            call_command('reconcile_on_demand_agents', '--apply')

        self.assertEqual(set_enabled.call_count, 2)


class ProviderDeadSeatDemandTests(SimpleTestCase):
    """PLAT-5265 / PLAT-5154: a process-running but provider-dead seat is not capacity.

    Security starved twice on this. Ren was enabled and status=running with
    provider_unavailable=true, so it could not take a turn — but the old
    demand-capacity predicate (agent_running) counted it, so the healthy STOPPED
    fallback never woke and two items sat unrouted (oldest MAIL-62 at 400h).

    The production order is reproduced exactly: provider-dead running seat +
    healthy stopped fallback + open team demand => the fallback must be started.
    """

    def _security(self, dead_health):
        """Ren running-but-dead, Akira healthy-but-stopped, one open item."""
        ren = _agent('ren', enabled=True, status='running')
        ren.update(dead_health)
        # A hibernated fallback is enabled=False AND status=stopped — the state
        # Akira was actually in. enabled=True/status=stopped is a different
        # (mid-transition) state that the start path does not consider, so
        # using it here would make every assertion below vacuous.
        akira = _agent('akira', enabled=False, status='hibernated')
        return decide_on_demand_actions(
            agents=[ren, akira],
            rosters=[_roster('security', ['ren', 'akira'])],
            workload_by_email={},
            workload_by_team={'security': {'open': 1}},
        )

    def _started(self, actions):
        return {a.username for a in actions if a.action == 'start'}

    def test_provider_unavailable_seat_does_not_suppress_the_healthy_fallback(self):
        actions = self._security({'provider_unavailable': True})
        self.assertIn('akira', self._started(actions),
                      'healthy stopped fallback must wake when the running seat is provider-dead')

    def test_token_pool_unavailable_seat_does_not_suppress_the_fallback(self):
        actions = self._security({'health_token_pool_unavailable': True})
        self.assertIn('akira', self._started(actions))

    def test_availability_status_unavailable_does_not_suppress_the_fallback(self):
        for status in ('unavailable', 'offline', 'inactive', 'UNAVAILABLE', ' Offline '):
            with self.subTest(status=status):
                actions = self._security({'availability_status': status})
                self.assertIn('akira', self._started(actions), status)

    def test_a_genuinely_healthy_running_seat_still_satisfies_demand(self):
        """The counter-case: without this, the fix would over-start every team."""
        actions = self._security({})
        self.assertNotIn('akira', self._started(actions),
                         'a healthy running seat must still count as capacity')

    def test_absent_health_is_treated_as_capacity_for_compatibility(self):
        """No telemetry yet must not read as dead, or we over-start seats."""
        for health in ({'provider_unavailable': None},
                       {'health_token_pool_unavailable': None},
                       {'availability_status': None},
                       {'availability_status': ''},
                       {'availability_status': 'available'}):
            with self.subTest(health=health):
                actions = self._security(health)
                self.assertNotIn('akira', self._started(actions), health)

    def test_predicate_is_falsey_safe_and_only_explicit_true_disqualifies(self):
        from .on_demand import agent_satisfies_demand
        base = _agent('x', enabled=True, status='running')
        self.assertTrue(agent_satisfies_demand(base))
        self.assertTrue(agent_satisfies_demand({**base, 'provider_unavailable': False}))
        self.assertTrue(agent_satisfies_demand({**base, 'provider_unavailable': 0}))
        self.assertFalse(agent_satisfies_demand({**base, 'provider_unavailable': True}))
        # A stopped agent never satisfies demand regardless of health.
        self.assertFalse(agent_satisfies_demand(_agent('y', status='stopped')))


class HealthSignalsReachTheDecisionTests(SimpleTestCase):
    """PLAT-5265 follow-up: the predicate's keys must exist on its REAL input.

    The first fix added agent_satisfies_demand(), which reads
    provider_unavailable / health_token_pool_unavailable / availability_status.
    Its tests supplied those by hand. The reconciler's actual dicts come from
    list_lifecycle_agents() -> _native_lifecycle_agent / normalize_runtime_agent,
    both closed literal projections that emitted none of them, and the health
    enrichment ran only on the API/UI read paths. So in production every check
    read absent, the predicate returned exactly what agent_running() returned,
    and the fix was inert while the suite stayed green (found in review by
    reika).

    These tests therefore build fixtures from the PROJECTIONS THEMSELVES rather
    than from literals, so the suite cannot stay green through that drift again.
    """

    ANTHROPIC_MODEL = 'claude-opus-4'

    def _poisoned_telemetry(self, username):
        """A fresh bridge report that the shared anthropic pool is unavailable."""
        import time as _time
        return {
            username: {
                'health': {'token_pool_unavailable': True},
                'ts': int(_time.time() * 1000),
                'runtime': {'provider': 'anthropic', 'model': self.ANTHROPIC_MODEL},
            },
        }

    def _projected_native(self, username, *, status=None):
        """A native agent dict produced by the real projection, not a literal."""
        from .lifecycle_fleet import _native_lifecycle_agent
        from .models import FleetAgent
        row = FleetAgent(
            agent_username=username,
            status=status or FleetAgent.Status.RUNNING,
            model=self.ANTHROPIC_MODEL,
        )
        return _native_lifecycle_agent(row)

    def test_native_projection_carries_the_provider_key_the_enrichment_needs(self):
        """Without a provider key the enrichment silently matches nothing."""
        from .agent_health import agent_provider_key
        projected = self._projected_native('ren')
        self.assertEqual(
            agent_provider_key(projected), 'anthropic',
            'native projection must expose enough model identity for the health '
            'enrichment to match a poisoned provider; otherwise enrichment is a '
            'no-op on the plane the incident happened on',
        )

    def test_enrichment_marks_a_projected_native_seat_and_predicate_agrees(self):
        """End-to-end on real shapes: projection -> enrichment -> predicate."""
        from .agent_health import apply_provider_unavailable_health
        from .on_demand import agent_satisfies_demand
        ren = self._projected_native('ren')
        self.assertTrue(
            agent_satisfies_demand(ren),
            'precondition: before enrichment the seat looks like capacity',
        )
        apply_provider_unavailable_health([ren], self._poisoned_telemetry('ren'))
        self.assertFalse(
            agent_satisfies_demand(ren),
            'after the same enrichment the read paths apply, a provider-dead '
            'seat must not count as demand capacity',
        )

    def test_enriched_projection_wakes_the_healthy_fallback(self):
        """The incident, reproduced on projected dicts rather than literals."""
        import time as _time
        from .agent_health import apply_provider_unavailable_health
        from .models import FleetAgent
        ren = self._projected_native('ren')
        akira = self._projected_native('akira', status=FleetAgent.Status.HIBERNATED)
        akira['enabled'] = False
        # Shared-provider poison deliberately reaches every seat unless that
        # seat has a fresh explicit healthy observation. Make the fixture's
        # claimed "healthy fallback" real rather than relying on absent health.
        akira['health_ok'] = True
        akira['telemetry_ts'] = int(_time.time() * 1000)
        apply_provider_unavailable_health([ren, akira], self._poisoned_telemetry('ren'))
        actions = decide_on_demand_actions(
            agents=[ren, akira],
            rosters=[_roster('security', ['ren', 'akira'])],
            workload_by_email={},
            workload_by_team={'security': {'open': 1}},
        )
        self.assertIn(
            'akira', {a.username for a in actions if a.action == 'start'},
            'healthy stopped fallback must wake when the running seat is provider-dead',
        )

    def test_shared_provider_outage_does_not_wake_an_equally_poisoned_fallback(self):
        """No seat should oscillate when the provider pool is unavailable."""
        from .agent_health import apply_provider_unavailable_health
        from .models import FleetAgent
        ren = self._projected_native('ren')
        akira = self._projected_native('akira', status=FleetAgent.Status.HIBERNATED)
        akira['enabled'] = False
        apply_provider_unavailable_health([ren, akira], self._poisoned_telemetry('ren'))

        actions = decide_on_demand_actions(
            agents=[ren, akira],
            rosters=[_roster('security', ['ren', 'akira'])],
            workload_by_email={},
            workload_by_team={'security': {'open': 1}},
        )

        self.assertNotIn(
            'akira', {a.username for a in actions if a.action == 'start'},
            'a same-provider poisoned fallback cannot satisfy the demand that wakes it',
        )

    def test_control_without_enrichment_the_dead_seat_still_counts(self):
        """Negative control — this is the bug, and it must still reproduce.

        Same projected inputs, enrichment NOT applied (the pre-fix reconciler).
        If this ever starts waking the fallback, the enrichment wiring is no
        longer what makes the fix work and these tests have stopped proving it.
        """
        from .models import FleetAgent
        ren = self._projected_native('ren')
        akira = self._projected_native('akira', status=FleetAgent.Status.HIBERNATED)
        akira['enabled'] = False
        actions = decide_on_demand_actions(
            agents=[ren, akira],
            rosters=[_roster('security', ['ren', 'akira'])],
            workload_by_email={},
            workload_by_team={'security': {'open': 1}},
        )
        self.assertNotIn(
            'akira', {a.username for a in actions if a.action == 'start'},
            'un-enriched projections must still reproduce the starvation; if they '
            'do not, the enrichment step is not what is carrying the fix',
        )

    @patch('fleet.external_agents.agent_telemetry')
    def test_reconciler_projects_heartbeat_outcome_from_live_telemetry(
        self,
        telemetry,
    ):
        import time as _time

        observed_at = _recent_ts()
        telemetry.return_value = {
            'akira': {
                'received_at': _time.time(),
                'runtime': {
                    'provider': 'openai',
                    'harness': 'Codex',
                    'model': 'gpt-5.6-sol',
                },
                'activity': {'last_activity_ms_ago': 0},
                'health': {'ok': True},
                'heartbeat': {
                    'outcome': 'queue_empty',
                    'observed_at': observed_at,
                    'ready_task_count': 0,
                    'blocked_task_count': 0,
                    'progress_event_count': 0,
                },
            },
        }
        agent = _agent('akira')

        Command()._apply_health_signals([agent])

        self.assertEqual(agent['heartbeat_outcome'], 'queue_empty')
        self.assertEqual(agent['heartbeat_observed_at'], observed_at)
        self.assertEqual(agent['heartbeat_ready_task_count'], 0)
        self.assertIsNotNone(agent['last_active_at'])


class ModelServiceabilityHibernationTests(SimpleTestCase):
    """Agents on a model with zero serviceable capacity hibernate and resume.

    Operator 2026-08-06 (grok credit exhaustion, goro/nagi provider_pool_dry
    loop): "the agents should stay as is .. but hive can hibernate them to
    save resources if needed .. whenever grok capacity is back the agents
    should resume back to working".
    """

    def _grok_agent(self, username, *, status='running', enabled=True):
        agent = _agent(username, status=status, enabled=enabled)
        agent['model'] = 'cortex/grok-4.5'
        return agent

    def _work(self, username):
        return {f'{username}@shizuha.com': {'doing': 2}}

    def test_running_agent_on_dead_model_hibernates_despite_work(self):
        agent = self._grok_agent('goro')
        actions = decide_on_demand_actions(
            agents=[agent],
            rosters=[_roster('engineering', ['goro'])],
            workload_by_email=self._work('goro'),
            workload_by_team={'engineering': {'doing': 2}},
            unserviceable_models=frozenset({'grok-4.5'}),
        )
        stops = [a for a in actions if a.action == 'stop']
        self.assertEqual(len(stops), 1)
        self.assertIn('no serviceable capacity', stops[0].reason)
        self.assertIn('grok-4.5', stops[0].reason)

    def test_hibernated_agent_on_dead_model_is_not_woken_by_its_work(self):
        agent = self._grok_agent('goro', status='hibernated', enabled=False)
        actions = decide_on_demand_actions(
            agents=[agent],
            rosters=[_roster('engineering', ['goro'])],
            workload_by_email=self._work('goro'),
            workload_by_team={'engineering': {'doing': 2}},
            unserviceable_models=frozenset({'grok-4.5'}),
        )
        self.assertEqual([a for a in actions if a.action == 'start'], [])

    def test_capacity_return_resumes_the_agent_automatically(self):
        """The whole point: same inputs, empty unserviceable set => start."""
        agent = self._grok_agent('goro', status='hibernated', enabled=False)
        actions = decide_on_demand_actions(
            agents=[agent],
            rosters=[_roster('engineering', ['goro'])],
            workload_by_email=self._work('goro'),
            workload_by_team={'engineering': {'doing': 2}},
            unserviceable_models=frozenset(),
        )
        starts = [a for a in actions if a.action == 'start']
        self.assertEqual(len(starts), 1)

    def test_other_models_are_untouched(self):
        agent = _agent('rei')
        agent['model'] = 'cortex/DeepSeek-V4-Flash'
        actions = decide_on_demand_actions(
            agents=[agent],
            rosters=[_roster('engineering', ['rei'])],
            workload_by_email=self._work('rei'),
            workload_by_team={'engineering': {'doing': 2}},
            unserviceable_models=frozenset({'grok-4.5'}),
        )
        self.assertEqual([a for a in actions if a.action == 'stop'], [])

    def test_fail_open_none_changes_nothing(self):
        agent = self._grok_agent('goro')
        actions = decide_on_demand_actions(
            agents=[agent],
            rosters=[_roster('engineering', ['goro'])],
            workload_by_email=self._work('goro'),
            workload_by_team={'engineering': {'doing': 2}},
            unserviceable_models=None,
        )
        self.assertEqual([a for a in actions if a.action == 'stop'], [])


class RuntimeCannotTurnHibernationTests(SimpleTestCase):
    """Operator 2026-08-22: a seat that cannot take a turn must not pin WIP.

    Jun on Codex/gpt-5.6-sol failed every heartbeat with "model is not
    supported when using Codex with a ChatGPT account" while holding 8
    in_progress Engineering items. Personal WIP used to block hibernate,
    so Pulse never drained onto DeepSeek peers.
    """

    def test_unsupported_model_error_is_cannot_turn(self):
        jun = {
            **_agent('jun'),
            'recent_errors': [{
                'msg': "The 'gpt-5.6-sol' model is not supported when using Codex with a ChatGPT account.",
            }],
        }
        self.assertTrue(agent_runtime_cannot_turn(jun))
        self.assertFalse(agent_satisfies_demand(jun))

    def test_crashloop_unready_pod_is_cannot_turn(self):
        sara = {**_agent('sara'), 'runtime_ready': False}
        self.assertTrue(agent_runtime_cannot_turn(sara))
        self.assertFalse(agent_satisfies_demand(sara))

    def test_unsupported_codex_model_hibernates_despite_personal_wip(self):
        jun = {
            **_agent('jun'),
            'recent_errors': [{
                'msg': "The 'gpt-5.6-sol' model is not supported when using Codex with a ChatGPT account.",
            }],
        }
        kai = _agent('kai', enabled=False, status='hibernated')
        actions = decide_on_demand_actions(
            agents=[jun, kai],
            rosters=[_roster('engineering', ['jun', 'kai'])],
            workload_by_email={
                'jun@shizuha.com': {'doing': 8, 'open': 3},
            },
            workload_by_team={'engineering': {'doing': 8, 'open': 3, 'needs_triage': 4}},
        )
        stops = [a for a in actions if a.action == 'stop']
        starts = [a for a in actions if a.action == 'start']
        self.assertEqual([a.username for a in stops], ['jun'])
        self.assertIn('rebalance assigned work', stops[0].reason)
        self.assertIn('kai', [a.username for a in starts])

    def test_healthy_seat_with_wip_is_not_stopped(self):
        jun = _agent('jun')
        actions = decide_on_demand_actions(
            agents=[jun],
            rosters=[_roster('engineering', ['jun'])],
            workload_by_email={'jun@shizuha.com': {'doing': 8}},
            workload_by_team={'engineering': {'doing': 8}},
        )
        self.assertEqual([a for a in actions if a.action == 'stop'], [])

    def test_hibernated_cannot_turn_codex_is_not_restarted_for_assigned_work(self):
        """Live 2026-08-22 08:40: hibernated Codex was START'd because poison
        was only detected while running, so aoi/ichi/mika/ni/zen/shizuha
        flapped and DeepSeek FILL stayed at 4/8."""
        jun = {
            **_agent('jun', enabled=False, status='hibernated'),
            'execution_method': 'codex_app_server',
            'model': 'gpt-5.6-sol',
            'hibernation_reason': (
                'runtime cannot take a model turn; hibernating so Pulse can '
                'rebalance assigned work'
            ),
        }
        kai = _agent('kai', enabled=False, status='hibernated')
        actions = decide_on_demand_actions(
            agents=[jun, kai],
            rosters=[_roster('engineering', ['jun', 'kai'])],
            workload_by_email={
                'jun@shizuha.com': {'doing': 8, 'open': 3},
            },
            workload_by_team={'engineering': {'doing': 8, 'open': 3}},
        )
        self.assertTrue(agent_runtime_cannot_turn(jun))
        self.assertFalse(agent_eligible_to_start(jun))
        self.assertNotIn('jun', [a.username for a in actions if a.action == 'start'])
        self.assertIn('kai', [a.username for a in actions if a.action == 'start'])

    def test_hibernated_undated_codex_error_stays_cannot_turn(self):
        jun = {
            **_agent('jun', enabled=False, status='hibernated'),
            'execution_method': 'codex_app_server',
            'model': 'gpt-5.6-sol',
            'recent_errors': [{
                'msg': (
                    "The 'gpt-5.6-sol' model is not supported when using "
                    'Codex with a ChatGPT account.'
                ),
            }],
        }
        self.assertTrue(agent_runtime_cannot_turn(jun))
        self.assertFalse(agent_eligible_to_start(jun))

    def test_model_switch_off_codex_can_start_after_cannot_turn_hibernate(self):
        jun = {
            **_agent('jun', enabled=False, status='hibernated'),
            'execution_method': 'shizuha',
            'model': 'cortex/DeepSeek-V4-Flash',
            'hibernation_reason': (
                'runtime cannot take a model turn; hibernating so Pulse can '
                'rebalance assigned work'
            ),
        }
        self.assertFalse(agent_runtime_cannot_turn(jun))
        self.assertTrue(agent_eligible_to_start(jun))


class ModelServiceabilityFetchTests(SimpleTestCase):
    def setUp(self):
        from fleet import model_serviceability
        model_serviceability._cache['at'] = 0.0
        model_serviceability._cache['ids'] = None

    def test_only_cortex_models_are_classified(self):
        from unittest import mock
        from fleet.model_serviceability import unserviceable_cortex_models
        with mock.patch(
            'fleet.model_serviceability.served_cortex_model_ids',
            return_value={'deepseek-v4-flash'},
        ):
            out = unserviceable_cortex_models([
                'cortex/DeepSeek-V4-Flash', 'cortex/grok-4.5',
                'gpt-5.6-sol', 'claude-opus-5',
            ])
        self.assertEqual(out, frozenset({'grok-4.5'}))

    def test_grok_voice_omni_is_not_classified_by_chat_catalog(self):
        from unittest import mock
        from fleet.model_serviceability import unserviceable_cortex_models
        with mock.patch(
            'fleet.model_serviceability.served_cortex_model_ids',
            return_value={'deepseek-v4-flash'},
        ):
            out = unserviceable_cortex_models([
                'cortex/grok-voice-think-fast-2.0',
                'cortex/grok-voice-latest',
                'cortex/grok-4.6',
            ])
        self.assertEqual(out, frozenset({'grok-4.6'}))

    def test_fetch_failure_fails_open(self):
        from unittest import mock
        from fleet.model_serviceability import unserviceable_cortex_models
        with mock.patch(
            'fleet.model_serviceability.served_cortex_model_ids',
            return_value=None,
        ):
            out = unserviceable_cortex_models(['cortex/grok-4.5'])
        self.assertEqual(out, frozenset())

    def test_empty_catalog_is_treated_as_broken(self):
        from unittest import mock
        from fleet import model_serviceability
        with mock.patch.object(model_serviceability, '_fetch_served_model_ids', return_value=None):
            self.assertIsNone(model_serviceability.served_cortex_model_ids())


class StopFenceFreshActivityTests(SimpleTestCase):
    """agent-rui 2026-08-08: a lease-driven hibernate applied 7s before the
    holder's first post-roll turn hit cortex, killing a 272K rebuild
    mid-stream. Scheduler stops must re-check LIVE telemetry at apply time."""

    def _action(self, action='stop', username='rui'):
        return OnDemandAction(
            action=action, agent_id='aid-1', username=username,
            reason='does not hold the model lease',
        )

    @patch(
        'fleet.external_agents.fresh_agent_telemetry',
    )
    def test_stop_defers_when_fresh_telemetry_shows_working(self, telemetry):
        from fleet.management.commands.reconcile_on_demand_agents import (
            stop_deferred_by_fresh_activity,
        )
        import time
        telemetry.return_value = {'rui': {
            'activity': {'state': 'working'},
            'received_at': time.time(),
        }}
        deferral = stop_deferred_by_fresh_activity(self._action())
        self.assertIsNotNone(deferral)
        self.assertIn('working', deferral)

    @patch(
        'fleet.external_agents.fresh_agent_telemetry',
    )
    def test_only_fresh_explicit_idle_allows_stop(self, telemetry):
        from fleet.management.commands.reconcile_on_demand_agents import (
            stop_deferred_by_fresh_activity,
        )
        import time
        telemetry.return_value = {'rui': {
            'activity': {'state': 'idle'}, 'received_at': time.time(),
        }}
        self.assertIsNone(stop_deferred_by_fresh_activity(self._action()))

        telemetry.return_value = {'rui': {
            'activity': {'state': 'idle'}, 'received_at': time.time() - 3600,
        }}
        self.assertIn('stale', stop_deferred_by_fresh_activity(self._action()))

        telemetry.return_value = {}
        with patch(
            'fleet.management.commands.reconcile_on_demand_agents.observed_native_replicas',
            return_value=None,
        ):
            # Non-holder leftover + missing telemetry must NOT defer: that
            # parked lex/lina/hana/banto KV homes until residency_full.
            self.assertIsNone(stop_deferred_by_fresh_activity(self._action()))

    @patch(
        'fleet.external_agents.fresh_agent_telemetry',
    )
    def test_stop_proceeds_when_native_deploy_already_at_zero(self, telemetry):
        from fleet.management.commands.reconcile_on_demand_agents import (
            stop_deferred_by_fresh_activity,
        )
        telemetry.return_value = {}
        with patch(
            'fleet.management.commands.reconcile_on_demand_agents.observed_native_replicas',
            return_value=0,
        ):
            self.assertIsNone(stop_deferred_by_fresh_activity(self._action()))

    @patch(
        'fleet.external_agents.fresh_agent_telemetry',
    )
    def test_stop_proceeds_when_native_deploy_is_unready(self, telemetry):
        """recon/tomo 2026-08-31: CreateContainerConfigError never emits
        telemetry. replicas=1 + ready=0 is not an in-flight stream."""
        from fleet.management.commands.reconcile_on_demand_agents import (
            stop_deferred_by_fresh_activity,
        )
        telemetry.return_value = {}
        with patch(
            'fleet.management.commands.reconcile_on_demand_agents.observed_native_replicas',
            return_value=1,
        ), patch(
            'fleet.management.commands.reconcile_on_demand_agents.observed_native_ready',
            return_value=False,
        ):
            self.assertIsNone(stop_deferred_by_fresh_activity(self._action()))

    @patch(
        'fleet.external_agents.fresh_agent_telemetry',
    )
    def test_stop_still_defers_when_ready_but_telemetry_missing(self, telemetry):
        from fleet.management.commands.reconcile_on_demand_agents import (
            stop_deferred_by_fresh_activity,
        )
        telemetry.return_value = {}
        with patch(
            'fleet.management.commands.reconcile_on_demand_agents.observed_native_replicas',
            return_value=1,
        ), patch(
            'fleet.management.commands.reconcile_on_demand_agents.observed_native_ready',
            return_value=True,
        ):
            # Holder-protecting stops (not leftover-lease) still fail closed.
            action = OnDemandAction(
                action='stop', agent_id='aid-1', username='rui',
                reason='no actionable work',
            )
            self.assertIn(
                'unavailable',
                stop_deferred_by_fresh_activity(action),
            )

    @patch(
        'fleet.external_agents.fresh_agent_telemetry',
    )
    def test_lease_leftover_proceeds_when_telemetry_unavailable(self, telemetry):
        """Operator 2026-09-01: missing telemetry must not park unleased KV homes."""
        from fleet.management.commands.reconcile_on_demand_agents import (
            stop_deferred_by_fresh_activity,
        )
        telemetry.return_value = {}
        with patch(
            'fleet.management.commands.reconcile_on_demand_agents.observed_native_replicas',
            return_value=1,
        ), patch(
            'fleet.management.commands.reconcile_on_demand_agents.observed_native_ready',
            return_value=True,
        ):
            self.assertIsNone(stop_deferred_by_fresh_activity(self._action()))

    @patch(
        'fleet.external_agents.fresh_agent_telemetry',
    )
    def test_every_attempt_reads_uncached_state_again(self, telemetry):
        """Production retry order: idle first attempt, working before retry."""
        from fleet.management.commands.reconcile_on_demand_agents import (
            stop_deferred_by_fresh_activity,
        )
        import time
        telemetry.side_effect = [
            {'rui': {
                'activity': {'state': 'idle'}, 'received_at': time.time(),
            }},
            {'rui': {
                'activity': {'state': 'working'}, 'received_at': time.time(),
            }},
        ]

        self.assertIsNone(stop_deferred_by_fresh_activity(self._action()))
        self.assertIn('working', stop_deferred_by_fresh_activity(self._action()))
        self.assertEqual(telemetry.call_count, 2)

    @patch(
        'fleet.external_agents.fresh_agent_telemetry',
    )
    def test_start_actions_never_consult_the_fence(self, telemetry):
        from fleet.management.commands.reconcile_on_demand_agents import (
            stop_deferred_by_fresh_activity,
        )
        self.assertIsNone(
            stop_deferred_by_fresh_activity(self._action(action='start'))
        )
        telemetry.assert_not_called()

    @patch(
        'fleet.external_agents.fresh_agent_telemetry',
    )
    def test_interrupt_ok_does_not_defer_a_working_expired_nonholder(self, telemetry):
        """HIVE-1712 / kei 2026-08-15: after the 900s active-turn grace the
        lease planner still said stop. The apply-time fence must not keep a
        non-holder DeepSeek runtime up forever just because it never idles."""
        from fleet.management.commands.reconcile_on_demand_agents import (
            stop_deferred_by_fresh_activity,
        )
        import time
        telemetry.return_value = {'kei': {
            'activity': {'state': 'working'},
            'received_at': time.time(),
        }}
        action = OnDemandAction(
            action='stop', agent_id='aid-kei', username='kei',
            reason='does not hold the model lease',
            interrupt_ok=True,
        )
        self.assertIsNone(stop_deferred_by_fresh_activity(action))
        telemetry.assert_not_called()


class ExpiredLeaseNonholderStopAppliesTests(SimpleTestCase):
    """Production caller sequence for HIVE-1712 / kei 2026-08-15.

    reconcile_model_lease already spent the 900s grace and returned
    scale[kei]=False plus interrupt_ok. The command must hibernate even
    though uncached telemetry still says working.
    """

    @patch('fleet.models.archived_usernames', return_value=set())
    @patch('fleet.management.commands.reconcile_on_demand_agents.hibernate_lifecycle_agent')
    @patch('fleet.management.commands.reconcile_on_demand_agents.set_lifecycle_agent_enabled')
    @patch('fleet.management.commands.reconcile_on_demand_agents.list_lifecycle_agents')
    @patch.object(Command, '_fetch_connect_state')
    @patch.object(Command, '_fetch_pulse_snapshot')
    @patch.object(Command, '_fetch_pulse_rosters')
    @patch.object(Command, '_apply_health_signals')
    @patch('fleet.model_serviceability.unserviceable_cortex_models', return_value=frozenset())
    @patch('fleet.management.commands.reconcile_on_demand_agents.reconcile_model_lease')
    @patch('fleet.external_agents.fresh_agent_telemetry')
    def test_apply_hibernates_working_nonholder_after_grace(
        self,
        telemetry,
        reconcile_lease,
        _unserviceable,
        _health,
        rosters,
        pulse_snapshot,
        connect_snapshot,
        lifecycle_agents,
        set_enabled,
        hibernate,
        _archived,
    ):
        import time

        rosters.return_value = [_roster('merge', ['kei'])]
        pulse_snapshot.return_value = {
            'workload': {'kei@shizuha.com': {'in_progress': 1}},
            'team_workload': {'merge': {'in_progress': 1}},
        }
        lifecycle_agents.return_value = [_agent('kei')]
        connect_snapshot.return_value = ConnectLifecycleSnapshot(
            available=True,
            unread_by_email={},
            last_dm_at_by_email={},
        )
        telemetry.return_value = {
            'kei': {
                'activity': {'state': 'working'},
                'received_at': time.time(),
            },
        }
        reconcile_lease.return_value = {
            'scale': {'kei': False},
            'interrupt_ok': {'kei'},
            'log': ['active-fence grace expired (>900s): kei'],
        }
        hibernate.return_value = (True, 'hibernated')

        stdout = StringIO()
        call_command('reconcile_on_demand_agents', '--apply', stdout=stdout)

        hibernate.assert_called_once_with(
            'id-kei',
            reason='does not hold the model lease',
        )
        self.assertNotIn('DEFER STOP kei', stdout.getvalue())
        self.assertIn('STOP kei', stdout.getvalue())


class GhostLivingHibernatedNonholderStopTests(SimpleTestCase):
    """Production caller sequence for ryo 2026-08-28.

    Hive status=hibernated and the lease planner already said should_run=False,
    so claimed_running == should_run and the command skipped STOP without
    observing Deployment replicas. The pod stayed Ready and hit Cortex.
    """

    @patch('fleet.models.archived_usernames', return_value=set())
    @patch('fleet.management.commands.reconcile_on_demand_agents.hibernate_lifecycle_agent')
    @patch('fleet.management.commands.reconcile_on_demand_agents.set_lifecycle_agent_enabled')
    @patch('fleet.management.commands.reconcile_on_demand_agents.list_lifecycle_agents')
    @patch.object(Command, '_fetch_connect_state')
    @patch.object(Command, '_fetch_pulse_snapshot')
    @patch.object(Command, '_fetch_pulse_rosters')
    @patch.object(Command, '_apply_health_signals')
    @patch('fleet.model_serviceability.unserviceable_cortex_models', return_value=frozenset())
    @patch('fleet.management.commands.reconcile_on_demand_agents.reconcile_model_lease')
    @patch('fleet.external_agents.fresh_agent_telemetry')
    @patch(
        'fleet.management.commands.reconcile_on_demand_agents.observed_native_replicas',
        return_value=1,
    )
    def test_apply_hibernates_hibernated_nonholder_with_live_replicas(
        self,
        _replicas,
        telemetry,
        reconcile_lease,
        _unserviceable,
        _health,
        rosters,
        pulse_snapshot,
        connect_snapshot,
        lifecycle_agents,
        set_enabled,
        hibernate,
        _archived,
    ):
        import time

        rosters.return_value = [_roster('merge', ['ryo'])]
        pulse_snapshot.return_value = {
            'workload': {},
            'team_workload': {'merge': {'in_progress': 1}},
        }
        lifecycle_agents.return_value = [
            _agent('ryo', enabled=False, status='hibernated'),
        ]
        connect_snapshot.return_value = ConnectLifecycleSnapshot(
            available=True,
            unread_by_email={},
            last_dm_at_by_email={},
        )
        telemetry.return_value = {
            'ryo': {
                'activity': {'state': 'working'},
                'received_at': time.time(),
            },
        }
        reconcile_lease.return_value = {
            'scale': {'ryo': False},
            'log': ['ryo does not hold DeepSeek'],
        }
        hibernate.return_value = (True, 'hibernated')

        stdout = StringIO()
        call_command('reconcile_on_demand_agents', '--apply', stdout=stdout)

        hibernate.assert_called_once_with(
            'id-ryo',
            reason='does not hold the model lease',
        )
        self.assertNotIn('DEFER STOP ryo', stdout.getvalue())
        self.assertIn('STOP ryo', stdout.getvalue())

    @patch('fleet.models.archived_usernames', return_value=set())
    @patch('fleet.management.commands.reconcile_on_demand_agents.hibernate_lifecycle_agent')
    @patch('fleet.management.commands.reconcile_on_demand_agents.set_lifecycle_agent_enabled')
    @patch('fleet.management.commands.reconcile_on_demand_agents.list_lifecycle_agents')
    @patch.object(Command, '_fetch_connect_state')
    @patch.object(Command, '_fetch_pulse_snapshot')
    @patch.object(Command, '_fetch_pulse_rosters')
    @patch.object(Command, '_apply_health_signals')
    @patch('fleet.model_serviceability.unserviceable_cortex_models', return_value=frozenset())
    @patch('fleet.management.commands.reconcile_on_demand_agents.reconcile_model_lease')
    @patch('fleet.external_agents.fresh_agent_telemetry')
    @patch(
        'fleet.management.commands.reconcile_on_demand_agents.observed_native_replicas',
        return_value=0,
    )
    def test_apply_skips_hibernated_nonholder_already_at_zero(
        self,
        _replicas,
        telemetry,
        reconcile_lease,
        _unserviceable,
        _health,
        rosters,
        pulse_snapshot,
        connect_snapshot,
        lifecycle_agents,
        set_enabled,
        hibernate,
        _archived,
    ):
        rosters.return_value = [_roster('merge', ['ryo'])]
        pulse_snapshot.return_value = {'workload': {}, 'team_workload': {}}
        lifecycle_agents.return_value = [
            _agent('ryo', enabled=False, status='hibernated'),
        ]
        connect_snapshot.return_value = ConnectLifecycleSnapshot(
            available=True,
            unread_by_email={},
            last_dm_at_by_email={},
        )
        telemetry.return_value = {}
        reconcile_lease.return_value = {
            'scale': {'ryo': False},
            'log': ['ryo does not hold DeepSeek'],
        }

        stdout = StringIO()
        call_command('reconcile_on_demand_agents', '--apply', stdout=stdout)

        hibernate.assert_not_called()
        self.assertNotIn('STOP ryo', stdout.getvalue())


class KeepResidentLeaseStopExemptionTests(SimpleTestCase):
    """Operator 2026-08-18: CEO Office stays up by holding a lease.

    lease_scale=False means the reserved seat did not get a slot this tick
    (every other holder was in-flight). Honest math lease-stops her instead
    of leaving a running unleased talk seat that occupies a KV home.
    """

    @patch('fleet.models.archived_usernames', return_value=set())
    @patch('fleet.management.commands.reconcile_on_demand_agents.hibernate_lifecycle_agent')
    @patch('fleet.management.commands.reconcile_on_demand_agents.set_lifecycle_agent_enabled')
    @patch('fleet.management.commands.reconcile_on_demand_agents.list_lifecycle_agents')
    @patch.object(Command, '_fetch_connect_state')
    @patch.object(Command, '_fetch_pulse_snapshot')
    @patch.object(Command, '_fetch_pulse_rosters')
    @patch.object(Command, '_apply_health_signals')
    @patch('fleet.model_serviceability.unserviceable_cortex_models', return_value=frozenset())
    @patch('fleet.management.commands.reconcile_on_demand_agents.reconcile_model_lease')
    @patch('fleet.external_agents.fresh_agent_telemetry')
    def test_apply_lease_stops_ceo_office_seat_without_a_holder_slot(
        self,
        telemetry,
        reconcile_lease,
        _unserviceable,
        _health,
        rosters,
        pulse_snapshot,
        connect_snapshot,
        lifecycle_agents,
        set_enabled,
        hibernate,
        _archived,
    ):
        rosters.return_value = [_roster('ceo-office', ['yuna'])]
        pulse_snapshot.return_value = {'workload': {}, 'team_workload': {}}
        yuna = _agent('yuna')
        yuna['warm_pool_size'] = 1
        lifecycle_agents.return_value = [yuna]
        connect_snapshot.return_value = ConnectLifecycleSnapshot(
            available=True,
            unread_by_email={},
            last_dm_at_by_email={},
        )
        telemetry.return_value = {}
        hibernate.return_value = (True, 'hibernated')
        reconcile_lease.return_value = {
            'scale': {'yuna': False},
            'interrupt_ok': {'yuna'},
            'log': ['yuna does not hold DeepSeek'],
        }

        stdout = StringIO()
        call_command('reconcile_on_demand_agents', '--apply', stdout=stdout)

        hibernate.assert_called_once_with(
            'id-yuna',
            reason='does not hold the model lease',
        )
        self.assertIn('STOP yuna', stdout.getvalue())
        self.assertNotIn('KEEP yuna — reserved conversational seat', stdout.getvalue())
