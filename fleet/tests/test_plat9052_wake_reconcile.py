"""PLAT-9052 direction-1: deferred-wake reconciliation.

The connect-wake DEFER promises "reconcile grants when a slot frees". The
durable registry (fleet.wake_defers) records the starvation; this module's
`reconcile_deferred_wakes` honors the promise: event-driven from the
stop/hibernate lease-release boundaries, it grants the OLDEST outstanding
deferral once lease capacity exists, with the view's exact fences failing
closed (archived / operator-stopped / already-running drop the entry;
lease-unavailable keeps it and the starvation clock runs).
"""

from __future__ import annotations

from unittest import mock

from django.test import TestCase

from fleet import wake_defers
from fleet.wake_defers import (
    MAX_DEFER_AGE_SECONDS,
    outstanding_wake_defers,
    reconcile_deferred_wakes,
)

NOW = 1_000_000.0


def _agent(username='kai', email='kai@shizuha.com', enabled=False, eligible=True):
    return {
        'id': username, 'agent_username': username, 'username': username,
        'email': email, 'enabled': enabled,
        'eligible_to_start': eligible,
    }


class ReconcileDeferredWakesTests(TestCase):
    def setUp(self):
        wake_defers._save({'defers': {}})

    def tearDown(self):
        wake_defers._save({'defers': {}})

    def test_empty_registry_is_a_noop(self):
        result = reconcile_deferred_wakes(now=NOW)
        self.assertEqual(result, {'considered': 0, 'granted': [], 'skipped': {}})

    def test_grants_oldest_deferral_when_capacity_exists(self):
        wake_defers.record_wake_defer('kai@shizuha.com', reason='connect_dm', now=NOW - 600)
        agent = _agent()
        with mock.patch('fleet.lifecycle_fleet.list_lifecycle_agents',
                               return_value=[agent]), \
             mock.patch('fleet.lifecycle_fleet.set_lifecycle_agent_enabled',
                               return_value=(True, 'started')) as start, \
             mock.patch('fleet.model_lease.lease_wake_available',
                        return_value=(True, 'ok')):
            result = reconcile_deferred_wakes(now=NOW)
        self.assertEqual(result['granted'], ['kai@shizuha.com'])
        start.assert_called_once_with('kai', True)
        self.assertEqual(outstanding_wake_defers(now=NOW), {})

    def test_oldest_first_when_two_deferrals_and_one_grant_budget(self):
        wake_defers.record_wake_defer('old@shizuha.com', now=NOW - 900)
        wake_defers.record_wake_defer('new@shizuha.com', now=NOW - 60)
        agents = [_agent('newseat', 'new@shizuha.com'),
                  _agent('oldseat', 'old@shizuha.com')]
        started = []
        with mock.patch('fleet.lifecycle_fleet.list_lifecycle_agents',
                               return_value=agents), \
             mock.patch('fleet.lifecycle_fleet.set_lifecycle_agent_enabled',
                               side_effect=lambda aid, en: (started.append(aid) or (True, 'ok'))), \
             mock.patch('fleet.model_lease.lease_wake_available',
                        return_value=(True, 'ok')):
            result = reconcile_deferred_wakes(max_grants=1, now=NOW)
        self.assertEqual(result['granted'], ['old@shizuha.com'])
        self.assertEqual(started, ['oldseat'])
        self.assertEqual(result['skipped']['new@shizuha.com'],
                         'grant budget exhausted this pass')

    def test_lease_unavailable_keeps_entry_and_clock(self):
        wake_defers.record_wake_defer('kai@shizuha.com', now=NOW - 600)
        with mock.patch('fleet.lifecycle_fleet.list_lifecycle_agents',
                               return_value=[_agent()]), \
             mock.patch('fleet.model_lease.lease_wake_available',
                        return_value=(False, 'lease_pool_full')):
            result = reconcile_deferred_wakes(now=NOW)
        self.assertEqual(result['granted'], [])
        self.assertIn('lease unavailable', result['skipped']['kai@shizuha.com'])
        # The starvation clock started at the first defer and survives.
        self.assertEqual(outstanding_wake_defers(now=NOW),
                         {'kai@shizuha.com': NOW - 600})

    # --- PLAT-10630: periodic sweep + bounded multi-grant drain ------------

    def test_default_pass_drains_to_lease_capacity_not_budget(self):
        """A 38-deep backlog with a 7-slot pool grants exactly 7 in one pass:
        the lease board is the real limiter; the rest keep their entries and
        the periodic sweep drains them on later passes."""
        emails = [f'seat{i:02d}@shizuha.com' for i in range(38)]
        for i, email in enumerate(emails):
            wake_defers.record_wake_defer(email, now=NOW - 3600 + i)
        agents = [_agent(f'seat{i:02d}', email) for i, email in enumerate(emails)]
        pool = {'free': 7}

        def lease_board(agent, **kwargs):
            if pool['free'] > 0:
                pool['free'] -= 1
                return (True, 'ok')
            return (False, 'lease_pool_full')

        with mock.patch('fleet.lifecycle_fleet.list_lifecycle_agents',
                               return_value=agents), \
             mock.patch('fleet.lifecycle_fleet.set_lifecycle_agent_enabled',
                               return_value=(True, 'started')), \
             mock.patch('fleet.model_lease.lease_wake_available',
                        side_effect=lease_board):
            result = reconcile_deferred_wakes(now=NOW)
        self.assertEqual(len(result['granted']), 7)
        # Oldest-first ordering across the drained set.
        self.assertEqual(result['granted'], emails[:7])
        self.assertEqual(len(outstanding_wake_defers(now=NOW)), 31)

    def test_default_pass_bounds_fanout_when_pool_stays_open(self):
        """With unlimited board capacity the per-pass budget still bounds the
        scale-up fan-out (RECONCILE_MAX_GRANTS_PER_PASS); the remainder drain
        on successive periodic passes instead of one thundering herd."""
        emails = [f'seat{i:02d}@shizuha.com' for i in range(12)]
        for i, email in enumerate(emails):
            wake_defers.record_wake_defer(email, now=NOW - 3600 + i)
        agents = [_agent(f'seat{i:02d}', email) for i, email in enumerate(emails)]
        with mock.patch('fleet.lifecycle_fleet.list_lifecycle_agents',
                               return_value=agents), \
             mock.patch('fleet.lifecycle_fleet.set_lifecycle_agent_enabled',
                               return_value=(True, 'started')), \
             mock.patch('fleet.model_lease.lease_wake_available',
                        return_value=(True, 'ok')):
            result = reconcile_deferred_wakes(now=NOW)
        self.assertEqual(len(result['granted']),
                         wake_defers.RECONCILE_MAX_GRANTS_PER_PASS)
        budget_skipped = [e for e, reason in result['skipped'].items()
                          if reason == 'grant budget exhausted this pass']
        self.assertEqual(len(budget_skipped),
                         12 - wake_defers.RECONCILE_MAX_GRANTS_PER_PASS)

    def test_explicit_max_grants_one_keeps_legacy_shape(self):
        for i in range(4):
            wake_defers.record_wake_defer(f'seat{i}@shizuha.com', now=NOW - 900 + i)
        agents = [_agent(f'seat{i}', f'seat{i}@shizuha.com') for i in range(4)]
        started = []
        with mock.patch('fleet.lifecycle_fleet.list_lifecycle_agents',
                               return_value=agents), \
             mock.patch('fleet.lifecycle_fleet.set_lifecycle_agent_enabled',
                        side_effect=lambda aid, en: (started.append(aid) or (True, 'ok'))), \
             mock.patch('fleet.model_lease.lease_wake_available',
                        return_value=(True, 'ok')):
            result = reconcile_deferred_wakes(max_grants=1, now=NOW)
        self.assertEqual(result['granted'], ['seat0@shizuha.com'])
        self.assertEqual(started, ['seat0'])

    def test_invalid_max_grants_falls_back_to_default_budget(self):
        for i in range(3):
            wake_defers.record_wake_defer(f'seat{i}@shizuha.com', now=NOW - 900 + i)
        agents = [_agent(f'seat{i}', f'seat{i}@shizuha.com') for i in range(3)]
        with mock.patch('fleet.lifecycle_fleet.list_lifecycle_agents',
                               return_value=agents), \
             mock.patch('fleet.lifecycle_fleet.set_lifecycle_agent_enabled',
                               return_value=(True, 'started')), \
             mock.patch('fleet.model_lease.lease_wake_available',
                        return_value=(True, 'ok')):
            result = reconcile_deferred_wakes(max_grants='bogus', now=NOW)
        self.assertEqual(len(result['granted']), 3)

    def test_archived_and_operator_stopped_drop_their_entries(self):
        wake_defers.record_wake_defer('arch@shizuha.com', now=NOW - 300)
        wake_defers.record_wake_defer('stopped@shizuha.com', now=NOW - 300)
        agents = [_agent('archseat', 'arch@shizuha.com'),
                  _agent('stopseat', 'stopped@shizuha.com')]
        with mock.patch('fleet.lifecycle_fleet.list_lifecycle_agents',
                               return_value=agents), \
             mock.patch('fleet.views._is_archived_username',
                        side_effect=lambda u: u == 'archseat'), \
             mock.patch('fleet.on_demand.agent_eligible_to_start',
                        side_effect=lambda a: a['username'] != 'stopseat'), \
             mock.patch('fleet.model_lease.lease_wake_available',
                        return_value=(True, 'ok')):
            result = reconcile_deferred_wakes(max_grants=2, now=NOW)
        self.assertEqual(result['skipped']['arch@shizuha.com'], 'archived')
        self.assertEqual(result['skipped']['stopped@shizuha.com'], 'operator_stopped')
        self.assertEqual(outstanding_wake_defers(now=NOW), {})

    def test_already_running_seat_clears_stale_entry(self):
        wake_defers.record_wake_defer('kai@shizuha.com', now=NOW - 300)
        with mock.patch('fleet.lifecycle_fleet.list_lifecycle_agents',
                               return_value=[_agent(enabled=True)]), \
             mock.patch('fleet.model_lease.lease_wake_available',
                        return_value=(True, 'ok')):
            result = reconcile_deferred_wakes(now=NOW)
        self.assertEqual(result['skipped']['kai@shizuha.com'], 'already_running')
        self.assertEqual(outstanding_wake_defers(now=NOW), {})

    def test_start_failure_fails_closed_and_keeps_entry(self):
        wake_defers.record_wake_defer('kai@shizuha.com', now=NOW - 300)
        with mock.patch('fleet.lifecycle_fleet.list_lifecycle_agents',
                               return_value=[_agent()]), \
             mock.patch('fleet.lifecycle_fleet.set_lifecycle_agent_enabled',
                               return_value=(False, {'detail': 'lease wait'})), \
             mock.patch('fleet.model_lease.lease_wake_available',
                        return_value=(True, 'ok')):
            result = reconcile_deferred_wakes(now=NOW)
        self.assertEqual(result['granted'], [])
        self.assertIn('start failed', result['skipped']['kai@shizuha.com'])
        self.assertEqual(outstanding_wake_defers(now=NOW),
                         {'kai@shizuha.com': NOW - 300})

    def test_resolver_fault_fails_closed_without_raising(self):
        wake_defers.record_wake_defer('kai@shizuha.com', now=NOW - 300)
        with mock.patch('fleet.lifecycle_fleet.list_lifecycle_agents',
                        side_effect=RuntimeError('offline')):
            result = reconcile_deferred_wakes(now=NOW)
        self.assertEqual(result['granted'], [])
        self.assertIn('resolve failed', result['skipped']['kai@shizuha.com'])
        self.assertEqual(outstanding_wake_defers(now=NOW),
                         {'kai@shizuha.com': NOW - 300})

    def test_rotted_entries_beyond_max_age_are_not_granted(self):
        wake_defers.record_wake_defer('kai@shizuha.com', now=NOW - MAX_DEFER_AGE_SECONDS - 10)
        result = reconcile_deferred_wakes(now=NOW)
        self.assertEqual(result['considered'], 0)
