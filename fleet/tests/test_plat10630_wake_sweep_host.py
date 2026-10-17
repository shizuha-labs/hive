"""PLAT-10630: the periodic deferred-wake grant sweep runs on the 10-min
lease reconcile command.

The stop/hibernate paths reconcile event-driven; the 2026-10-01 cascade (38
outstanding seats, zero grant-release events) proved a backlog cannot drain
when no seat happens to stop. `reconcile_on_demand_agents` is the sweep host:
after the lease tick, apply-mode runs `reconcile_deferred_wakes()` so slots
the planner just granted or freed are visible to the grant pass. Contracts
pinned here:

  * apply-mode runs the sweep exactly once per pass;
  * dry-run does NOT run it (grants are real lifecycle writes);
  * a reconcile fault degrades to a warning line and never breaks the
    command's own hibernation duty (exit stays clean).
"""
from contextlib import ExitStack
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from fleet.management.commands.reconcile_on_demand_agents import (
    ConnectLifecycleSnapshot,
)

CMD = 'fleet.management.commands.reconcile_on_demand_agents.'

EMPTY_ROSTERS = [{'team_name': 'existing-team', 'is_active': True,
                  'tiers': [['other@shizuha.com']]}]


class WakeDeferredSweepHostTests(TestCase):
    """Command-level contract: apply runs the sweep, dry-run does not."""

    def _run(self, *, apply: bool, sweep_side_effect=None):
        with ExitStack() as stack:
            for target, value in {
                CMD + 'Command._fetch_pulse_rosters': EMPTY_ROSTERS,
                CMD + 'Command._fetch_pulse_snapshot': {'workload': {}, 'team_workload': {}},
                CMD + 'Command._fetch_connect_state': ConnectLifecycleSnapshot(
                    available=True, unread_by_email={}, last_dm_at_by_email={}),
                CMD + 'Command._apply_health_signals': None,
                'fleet.external_agents.fresh_agent_telemetry': {},
                'fleet.models.archived_usernames': set(),
                CMD + 'reconcile_model_lease': {'scale': {}, 'log': []},
            }.items():
                stack.enter_context(patch(target, return_value=value))
            sweep = stack.enter_context(patch(
                'fleet.wake_defers.reconcile_deferred_wakes',
                side_effect=sweep_side_effect,
                return_value={'considered': 0, 'granted': [], 'skipped': {}},
            ))
            output = StringIO()
            if apply:
                call_command('reconcile_on_demand_agents', '--apply', stdout=output)
            else:
                call_command('reconcile_on_demand_agents', stdout=output)
            return sweep, output.getvalue()

    def test_apply_mode_runs_the_periodic_sweep_once(self):
        sweep, out = self._run(apply=True)
        self.assertEqual(sweep.call_count, 1)
        self.assertIn('wake-defer: periodic reconcile pass', out)

    def test_dry_run_does_not_grant(self):
        sweep, out = self._run(apply=False)
        self.assertEqual(sweep.call_count, 0)
        self.assertNotIn('wake-defer: periodic reconcile pass', out)

    def test_sweep_fault_degrades_to_warning_and_does_not_break_reconcile(self):
        sweep, out = self._run(apply=True,
                               sweep_side_effect=RuntimeError('synthetic fence fault'))
        self.assertEqual(sweep.call_count, 1)
        self.assertIn('wake-defer: periodic reconcile skipped', out)
        self.assertIn('synthetic fence fault', out)
