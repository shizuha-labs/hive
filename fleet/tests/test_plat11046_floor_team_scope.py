"""PLAT-11046 / HIVE-2841 floor regression: the critical-team floor must match
its own policy rows against real rosters.

``fleet.on_demand.canon_team`` PRESERVES the org scope separator, so Pulse
rosters deliver org-qualified team keys ("shizuha::merge") while the
``LeaseCriticalTeamPolicy`` surface's documented contract is the unqualified
slug ("merge" — see the model docstring). Before the fix the floor compared
"merge" (policy) against "shizuha::merge" (members) and could never seat its
own critical teams — the merge seat starved in ``waiting`` for hours after the
policy row landed (kei, 2026-10-07).

Seam map: the POLICY-side tail normalization lives in
``critical_team_policy()`` (the DB reader); the MEMBER-side normalization
lives in ``critical_team_floor_state._team_slugs``. Both are tested here."""

from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from fleet.model_lease import (
    PoolMember,
    critical_team_floor_state,
    critical_team_policy,
)


class FloorTeamScopeTests(SimpleTestCase):
    def test_unqualified_policy_row_matches_org_qualified_roster(self):
        policy = {'merge': 1, 'review': 1}
        members = [
            PoolMember(username='kei', email='kei@shizuha.com',
                       teams=('shizuha::merge',)),
            PoolMember(username='sara', email='sara@shizuha.com',
                       teams=('shizuha::review',)),
            PoolMember(username='ichi', email='ichi@shizuha.com', teams=()),
        ]
        state = critical_team_floor_state(
            members=members,
            wants={'kei', 'sara'},
            holder_usernames={'ichi', 'san'},
            policy=policy,
        )
        # LRH order: all last_held_at default 0.0 → teams sort by slug.
        self.assertEqual(state['targets'],
                         [('merge', ['kei']), ('review', ['sara'])])
        # Neither ichi nor san belongs to a critical team → nothing reserved.
        self.assertEqual(state['reserved'], set())

    def test_met_team_is_keep_fenced(self):
        policy = {'merge': 1}
        members = [
            PoolMember(username='kei', email='kei@shizuha.com',
                       teams=('shizuha::merge',)),
        ]
        state = critical_team_floor_state(
            members=members,
            wants={'kei'},
            holder_usernames={'kei'},
            policy=policy,
        )
        self.assertEqual(state['targets'], [])
        self.assertEqual(state['reserved'], {'kei'})

    def test_policy_reader_tail_normalizes_qualified_row(self):
        """An org-qualified policy row ("shizuha::merge") must read back as the
        unqualified slug the member side compares ("merge")."""
        fake_qs = [Mock(team_slug='shizuha::merge', min_leases=1)]
        with patch('fleet.model_lease.cache') as cache_mock, \
             patch('fleet.models.LeaseCriticalTeamPolicy') as policy_mock:
            cache_mock.get.return_value = None
            policy_mock.objects.filter.return_value.only.return_value = fake_qs
            with patch('fleet.model_lease.logger'):
                self.assertEqual(critical_team_policy(), {'merge': 1})
