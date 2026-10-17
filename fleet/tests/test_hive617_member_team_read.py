"""HIVE-617: a non-staff org MEMBER may read the team structure of the org(s)
they belong to (multi-org), and ONLY those. Staff read all. Mutations stay
staff-only (covered elsewhere). The verified-membership set is the boundary."""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from fleet.views import FleetTeamRosterView

TEAMS = [
    {'team_name': 'accounting', 'organization_slug': 'shizuha'},
    {'team_name': 'design', 'organization_slug': 'shizuha'},
    {'team_name': 'securities-engineering', 'organization_slug': 'shizuha-trading'},
    {'team_name': 'secret-team', 'organization_slug': 'acme-corp'},  # caller NOT a member
]


def _user(is_staff=False, org_ids=()):
    return SimpleNamespace(
        is_authenticated=True,
        is_staff=is_staff,
        organization_memberships={str(o): 'member' for o in org_ids},
    )


class MemberScopedTeamReadTests(SimpleTestCase):
    def _get(self, user, org_slug=''):
        req = APIRequestFactory().get('/v1/fleet/teams/' + (f'?org_slug={org_slug}' if org_slug else ''))
        force_authenticate(req, user=user)
        return FleetTeamRosterView.as_view()(req)

    @patch('fleet.views._service_roster_teams', return_value=TEAMS)
    @patch('fleet.views._admin_organizations')
    def test_member_of_two_orgs_sees_both_not_others(self, admin_orgs, _teams):
        admin_orgs.return_value = ([
            {'id': 1, 'slug': 'shizuha'},
            {'id': 2, 'slug': 'shizuha-trading'},
            {'id': 9, 'slug': 'acme-corp'},
        ], None)
        resp = self._get(_user(org_ids=[1, 2]))  # member of 1 AND 2, not 9
        orgs = {t['organization_slug'] for t in resp.data['teams']}
        self.assertEqual(orgs, {'shizuha', 'shizuha-trading'})
        self.assertNotIn('acme-corp', orgs)

    @patch('fleet.views._service_roster_teams', return_value=TEAMS)
    @patch('fleet.views._admin_organizations')
    def test_request_foreign_org_is_403(self, admin_orgs, _teams):
        admin_orgs.return_value = ([{'id': 1, 'slug': 'shizuha'}], None)
        resp = self._get(_user(org_ids=[1]), org_slug='acme-corp')
        self.assertEqual(resp.status_code, 403)

    @patch('fleet.views._service_roster_teams', return_value=TEAMS)
    @patch('fleet.views._admin_organizations')
    def test_admin_over_return_is_trimmed_by_jwt(self, admin_orgs, _teams):
        # Defense-in-depth: admin wrongly lists an org the caller is NOT in (9);
        # the JWT membership (only org 1) must trim it out.
        admin_orgs.return_value = ([{'id': 1, 'slug': 'shizuha'}, {'id': 9, 'slug': 'acme-corp'}], None)
        resp = self._get(_user(org_ids=[1]))
        orgs = {t['organization_slug'] for t in resp.data['teams']}
        self.assertEqual(orgs, {'shizuha'})

    @patch('fleet.views._service_roster_teams', return_value=TEAMS)
    @patch('fleet.views._admin_organizations', return_value=([], None))
    def test_no_membership_returns_empty(self, _admin, _teams):
        resp = self._get(_user(org_ids=[]))
        self.assertEqual(resp.data['teams'], [])

    @patch('fleet.views._normalise_team_collection_payload', side_effect=lambda p: p)
    @patch('fleet.views._pulse_request', return_value=({'teams': TEAMS}, None))
    def test_staff_sees_all_orgs(self, _pulse, _norm):
        resp = self._get(_user(is_staff=True))
        self.assertEqual(resp.status_code, 200)
        orgs = {t['organization_slug'] for t in resp.data['teams']}
        self.assertIn('acme-corp', orgs)  # staff is NOT member-scoped
