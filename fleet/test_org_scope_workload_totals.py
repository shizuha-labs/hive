"""Selecting an org must not shrink an agent's own workload.

Operator 2026-08-05, after two rounds of "the card still shows 0 open":

    still don't see the active tasks assigned to banto nor on the team header ..
    and it's still in needs help .. really poor user experience here by you
    ...
    note that the card and the teams headers should never under-report

The client-side floor landed first and was too shallow: it floors the team-scoped
slice at the agent's own totals, but in the ORG-SCOPED payload those totals were
themselves already zero, so there was nothing left to floor to.

    GET /v1/fleet/pulse-snapshot/                 banto -> open 1, blocked 2
    GET /v1/fleet/pulse-snapshot/?org_slug=shizuha banto -> open 0, blocked 0

banto is placed in Shizuha Global > Accounting, but BKSD-10 (todo, high) and
BKSD-8/BKSD-9 (blocked) are Digital LLP filings. Pulse's org projection counts
only that org's projects, so viewing Shizuha Global erased them.

The org lens chooses WHICH agents and teams are displayed. It must not misreport
the workload of one it displays.
"""
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from fleet.views import _restore_assignee_workload_totals


# The live numbers, both lenses.
GLOBAL = {
    'banto@shizuha.com': {
        'open': 1, 'in_progress': 0, 'blocked': 2, 'in_review': 0,
        'scheduled': 2, 'backlog': 0, 'completed': 5, 'overdue': 2,
    },
}
ORG_SCOPED = {
    'banto@shizuha.com': {
        'open': 0, 'in_progress': 0, 'blocked': 0, 'in_review': 0,
        'scheduled': 2, 'backlog': 0, 'completed': 4, 'overdue': 0,
    },
}


def _apply(payload, global_workload):
    view = MagicMock()
    view._global_assignee_workload.return_value = global_workload
    _restore_assignee_workload_totals(view, MagicMock(), payload)
    return payload


class OrgScopeNeverHidesAssignedWorkTests(SimpleTestCase):
    def test_the_banto_case(self):
        payload = {'workload': {k: dict(v) for k, v in ORG_SCOPED.items()}}
        _apply(payload, GLOBAL)
        got = payload['workload']['banto@shizuha.com']
        self.assertEqual(got['open'], 1, 'BKSD-10 is assigned to banto and must be counted')
        self.assertEqual(got['blocked'], 2, 'BKSD-8 and BKSD-9 are assigned to banto')
        self.assertEqual(got['overdue'], 2)

    def test_no_bucket_is_ever_lowered(self):
        payload = {'workload': {'a@x.com': {'open': 9, 'blocked': 1}}}
        _apply(payload, {'a@x.com': {'open': 2, 'blocked': 5}})
        self.assertEqual(payload['workload']['a@x.com'], {'open': 9, 'blocked': 5})

    def test_an_assignee_missing_from_the_org_payload_is_added(self):
        payload = {'workload': {}}
        _apply(payload, GLOBAL)
        self.assertEqual(payload['workload']['banto@shizuha.com']['open'], 1)

    def test_it_does_not_mutate_the_global_source(self):
        global_copy = {k: dict(v) for k, v in GLOBAL.items()}
        payload = {'workload': {}}
        _apply(payload, global_copy)
        payload['workload']['banto@shizuha.com']['open'] = 99
        self.assertEqual(global_copy['banto@shizuha.com']['open'], 1)


class FailsSoftTests(SimpleTestCase):
    """A degraded global fetch must never blank the org payload."""

    def test_an_unavailable_global_snapshot_leaves_the_payload_alone(self):
        payload = {'workload': {k: dict(v) for k, v in ORG_SCOPED.items()}}
        _apply(payload, None)
        self.assertEqual(payload['workload'], ORG_SCOPED)

    def test_an_exception_is_swallowed(self):
        payload = {'workload': {k: dict(v) for k, v in ORG_SCOPED.items()}}
        view = MagicMock()
        view._global_assignee_workload.side_effect = RuntimeError('pulse down')
        _restore_assignee_workload_totals(view, MagicMock(), payload)
        self.assertEqual(payload['workload'], ORG_SCOPED)

    def test_a_payload_without_workload_is_untouched(self):
        payload = {'teams': []}
        _apply(payload, GLOBAL)
        self.assertNotIn('workload', payload)

    def test_malformed_bucket_values_are_skipped(self):
        payload = {'workload': {'a@x.com': {'open': 0}}}
        _apply(payload, {'a@x.com': {'open': 'not-a-number', 'blocked': 3}})
        self.assertEqual(payload['workload']['a@x.com']['open'], 0)
        self.assertEqual(payload['workload']['a@x.com']['blocked'], 3)


class TeamCountsStayOrgScopedTests(SimpleTestCase):
    def test_team_workload_is_not_touched(self):
        """A team genuinely belongs to one org; only per-assignee totals merge."""
        payload = {
            'workload': {},
            'team_workload': {'accounting': {'open': 0}},
        }
        _apply(payload, GLOBAL)
        self.assertEqual(payload['team_workload'], {'accounting': {'open': 0}})
