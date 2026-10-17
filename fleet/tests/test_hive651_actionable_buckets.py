"""
HIVE-651 (operator 2026-07-11): the on-demand keep-warm/start decision must
count only ACTIONABLE work. An agent whose whole queue is blocked/scheduled/
backlog has nothing to do NOW and should hibernate; the 10-min sweep restarts
it when a blocker clears / a scheduled task comes due / a DM arrives.
"""
from django.test import SimpleTestCase

from fleet.on_demand import decide_on_demand_actions


ROSTER = [{'team_name': 'design', 'is_active': True, 'tiers': [['dee@shizuha.com']]}]


def _agent(running=True):
    # HIVE-1401: the fixture originally used status='stopped', which the current
    # on_demand policy treats as an OPERATOR KILL-SWITCH that demand must never
    # override (agent_eligible_to_start -> False). A cold-startable seat is
    # represented as status='hibernated' (enabled=False), matching the canonical
    # fixtures in fleet/test_on_demand.py.
    return {
        'id': 'a1', 'agent_username': 'dee', 'email': 'dee@shizuha.com',
        'enabled': running, 'status': 'running' if running else 'hibernated',
    }


class Hive651ActionableBucketsTests(SimpleTestCase):
    def _decide(self, workload, running=True):
        return decide_on_demand_actions(
            agents=[_agent(running)], rosters=ROSTER,
            workload_by_email={'dee@shizuha.com': workload},
            workload_by_team={'design': workload},
        )

    def test_only_blocked_scheduled_backlog_triggers_STOP(self):
        # Non-actionable-only queue → idle → stop
        actions = self._decide({'blocked': 4, 'scheduled': 5, 'backlog': 2}, running=True)
        self.assertEqual([a.action for a in actions], ['stop'])

    def test_actionable_work_keeps_agent_running(self):
        actions = self._decide({'in_progress': 1, 'blocked': 3}, running=True)
        self.assertEqual(actions, [])  # has actionable work → no stop

    def test_stopped_agent_with_only_blocked_is_NOT_started(self):
        actions = self._decide({'blocked': 2, 'scheduled': 1}, running=False)
        self.assertEqual(actions, [])  # nothing actionable → don't wake

    def test_stopped_agent_with_actionable_is_started(self):
        actions = self._decide({'todo': 1}, running=False)
        self.assertEqual([a.action for a in actions], ['start'])
