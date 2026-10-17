"""
HIVE-238: Hive teams proxy → Pulse TeamRoster API.

Tests for FleetTeamRosterView (GET list + POST create),
FleetTeamDetailView (GET/PATCH/DELETE), and FleetTeamMembersView (GET/POST).
All Pulse outbound calls are mocked at `requests.request`.
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from fleet.views import (
    FleetTeamDetailView,
    FleetTeamMembersView,
    FleetTeamRosterView,
)


def _staff_user():
    return SimpleNamespace(is_authenticated=True, is_staff=True, organization_memberships={})


def _member_user(org_ids=()):
    return SimpleNamespace(
        is_authenticated=True, is_staff=False,
        organization_memberships={str(o): 'member' for o in org_ids},
    )

PULSE_URL = 'http://pulse.internal'
_BEARER = 'Bearer test-token'

_TEAM_DETAIL = {
    'team_name': 'engineering',
    'is_active': True,
    'note': '',
    'tiers': [['kai@shizuha.com']],
    'junior_emails': [],
}

_TEAM_LIST = {
    'teams': [_TEAM_DETAIL, {
        'team_name': 'admin-ops',
        'is_active': True,
        'note': '',
        'tiers': [['shizuha@shizuha.com'], ['nagi@shizuha.com']],
        'junior_emails': [],
    }],
    'count': 2,
}

_MEMBERS_PAYLOAD = {
    'team_name': 'engineering',
    'memberships': [{'email': 'ryo@shizuha.com', 'tier': 0}],
    'rosters': [],
}

_WRITE_OK = {'ok': True, 'action': 'add', 'team_name': 'engineering', 'email': 'ryo@shizuha.com'}


def _mock_resp(data, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = data
    resp.text = json.dumps(data)
    return resp


def _mock_resp_204():
    resp = MagicMock()
    resp.status_code = 204
    resp.json.side_effect = ValueError('no content')
    resp.text = ''
    return resp


# ── Subclasses with auth/perm stripped; _require_staff kept as real code ──

class _OpenTeamRosterView(FleetTeamRosterView):
    """Auth-free version for unit testing."""
    permission_classes = []
    authentication_classes = []


class _OpenTeamDetailView(FleetTeamDetailView):
    permission_classes = []
    authentication_classes = []


class _OpenTeamMembersView(FleetTeamMembersView):
    permission_classes = []
    authentication_classes = []


@override_settings(HIVE_PULSE_API_URL=PULSE_URL)
class FleetTeamRosterListTests(SimpleTestCase):

    def setUp(self):
        self.factory = APIRequestFactory()
        self.view = _OpenTeamRosterView.as_view()

    @patch('requests.request')
    def test_list_teams_proxies_to_pulse(self, mock_req):
        mock_req.return_value = _mock_resp(_TEAM_LIST)
        req = self.factory.get('/v1/fleet/teams/', HTTP_AUTHORIZATION=_BEARER)
        force_authenticate(req, user=_staff_user())
        with patch('fleet.views.ensure_team_required_capability', return_value=None):
            resp = self.view(req)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['teams'][0]['display_name'], 'Engineering')
        mock_req.assert_called_once()
        call_url = mock_req.call_args[0][1]
        self.assertIn('/admin/team-roster/', call_url)
        self.assertNotIn('<team', call_url)
        self.assertEqual(resp.data['teams'][0]['display_name'], 'Engineering')
        self.assertEqual(resp.data['teams'][0]['lead_email'], 'kai@shizuha.com')
        self.assertEqual(resp.data['teams'][1]['display_name'], 'Admin Ops')
        self.assertEqual(resp.data['teams'][1]['slug'], 'admin-ops')

    @patch('requests.request')
    def test_list_teams_normalises_legacy_rosters_shape(self, mock_req):
        mock_req.return_value = _mock_resp({
            'rosters': [{'team_name': 'customer-support', 'tiers': [[]]}],
        })
        req = self.factory.get('/v1/fleet/teams/', HTTP_AUTHORIZATION=_BEARER)
        force_authenticate(req, user=_staff_user())
        with patch('fleet.views.ensure_team_required_capability', return_value=None):
            resp = self.view(req)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['teams'][0]['team_name'], 'customer-support')
        self.assertEqual(resp.data['teams'][0]['display_name'], 'Customer Support')
        self.assertEqual(resp.data['rosters'][0]['display_name'], 'Customer Support')

    @patch('requests.request')
    def test_create_team_proxies_to_pulse(self, mock_req):
        mock_req.return_value = _mock_resp({'ok': True, 'action': 'create', 'team_name': 'devops'})
        req = self.factory.post(
            '/v1/fleet/teams/', {'team_name': 'devops'}, format='json', HTTP_AUTHORIZATION=_BEARER,
        )
        with patch('fleet.views._require_staff', return_value=None), \
             patch('fleet.views.ensure_team_required_capability', return_value=None):
            resp = self.view(req)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data['team']['slug'], 'devops')

    def test_create_team_missing_name_returns_400(self):
        req = self.factory.post('/v1/fleet/teams/', {}, format='json', HTTP_AUTHORIZATION=_BEARER)
        with patch('fleet.views._require_staff', return_value=None):
            resp = self.view(req)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('error', resp.data)

    def test_list_returns_401_without_bearer(self):
        req = self.factory.get('/v1/fleet/teams/')
        with patch('fleet.views._require_staff', return_value=None):
            resp = self.view(req)
        self.assertEqual(resp.status_code, 401)

    @patch('requests.request')
    def test_list_forwards_pulse_error(self, mock_req):
        mock_req.return_value = _mock_resp({'detail': 'internal'}, 500)
        req = self.factory.get('/v1/fleet/teams/', HTTP_AUTHORIZATION=_BEARER)
        force_authenticate(req, user=_staff_user())
        resp = self.view(req)
        self.assertEqual(resp.status_code, 500)

    def test_list_non_staff_member_scoped_no_membership_empty(self):
        # HIVE-617: non-staff no longer gets a blanket 403 — team STRUCTURE is
        # member-visible. A caller with no org membership sees an empty list;
        # the member-of-org(s) behaviour is covered in
        # test_hive617_member_team_read (incl. the cross-org 403 boundary).
        req = self.factory.get('/v1/fleet/teams/', HTTP_AUTHORIZATION=_BEARER)
        force_authenticate(req, user=_member_user(org_ids=[]))
        resp = self.view(req)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['teams'], [])


@override_settings(HIVE_PULSE_API_URL=PULSE_URL)
class FleetTeamDetailTests(SimpleTestCase):

    def setUp(self):
        self.factory = APIRequestFactory()
        self.view = _OpenTeamDetailView.as_view()

    @patch('requests.request')
    def test_get_team_detail(self, mock_req):
        mock_req.return_value = _mock_resp(_TEAM_DETAIL)
        req = self.factory.get('/v1/fleet/teams/engineering/', HTTP_AUTHORIZATION=_BEARER)
        with patch('fleet.views._require_staff', return_value=None):
            resp = self.view(req, team_name='engineering')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['team_name'], 'engineering')
        self.assertEqual(resp.data['display_name'], 'Engineering')
        self.assertEqual(resp.data['lead_email'], 'kai@shizuha.com')
        call_url = mock_req.call_args[0][1]
        self.assertIn('/admin/team-roster/engineering/', call_url)

    @patch('requests.request')
    def test_get_team_normalises_uppercase_name(self, mock_req):
        mock_req.return_value = _mock_resp(_TEAM_DETAIL)
        req = self.factory.get('/v1/fleet/teams/Engineering/', HTTP_AUTHORIZATION=_BEARER)
        with patch('fleet.views._require_staff', return_value=None):
            resp = self.view(req, team_name='Engineering')
        self.assertEqual(resp.status_code, 200)
        call_url = mock_req.call_args[0][1]
        self.assertIn('/admin/team-roster/engineering/', call_url)

    @patch('requests.request')
    def test_patch_team_forwards_body(self, mock_req):
        updated = {**_TEAM_DETAIL, 'lead_email': 'sara@shizuha.com'}
        mock_req.return_value = _mock_resp(updated)
        req = self.factory.patch(
            '/v1/fleet/teams/engineering/',
            {'lead_email': 'sara@shizuha.com'},
            format='json',
            HTTP_AUTHORIZATION=_BEARER,
        )
        with patch('fleet.views._require_staff', return_value=None):
            resp = self.view(req, team_name='engineering')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['lead_email'], 'sara@shizuha.com')
        self.assertEqual(mock_req.call_args[0][0], 'PATCH')

    @patch('requests.request')
    def test_delete_team_204_from_pulse(self, mock_req):
        mock_req.return_value = _mock_resp_204()
        req = self.factory.delete('/v1/fleet/teams/engineering/', HTTP_AUTHORIZATION=_BEARER)
        with patch('fleet.views._require_staff', return_value=None):
            resp = self.view(req, team_name='engineering')
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(mock_req.call_args[0][0], 'DELETE')

    @patch('requests.request')
    def test_delete_team_200_body_forwarded(self, mock_req):
        deactivated = {**_TEAM_DETAIL, 'is_active': False}
        mock_req.return_value = _mock_resp(deactivated)
        req = self.factory.delete('/v1/fleet/teams/engineering/', HTTP_AUTHORIZATION=_BEARER)
        with patch('fleet.views._require_staff', return_value=None):
            resp = self.view(req, team_name='engineering')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data['is_active'])

    @patch('requests.request')
    def test_pulse_404_forwarded(self, mock_req):
        mock_req.return_value = _mock_resp({'detail': 'Team not found'}, 404)
        req = self.factory.get('/v1/fleet/teams/nonexistent/', HTTP_AUTHORIZATION=_BEARER)
        with patch('fleet.views._require_staff', return_value=None):
            resp = self.view(req, team_name='nonexistent')
        self.assertEqual(resp.status_code, 404)

    def test_get_returns_401_without_bearer(self):
        req = self.factory.get('/v1/fleet/teams/engineering/')
        with patch('fleet.views._require_staff', return_value=None):
            resp = self.view(req, team_name='engineering')
        self.assertEqual(resp.status_code, 401)


@override_settings(HIVE_PULSE_API_URL=PULSE_URL)
class FleetTeamMembersTests(SimpleTestCase):

    def setUp(self):
        self.factory = APIRequestFactory()
        self.view = _OpenTeamMembersView.as_view()

    @patch('requests.request')
    def test_list_members_passes_team_filter(self, mock_req):
        mock_req.return_value = _mock_resp(_MEMBERS_PAYLOAD)
        req = self.factory.get(
            '/v1/fleet/teams/engineering/members/', HTTP_AUTHORIZATION=_BEARER,
        )
        with patch('fleet.views._require_staff', return_value=None):
            resp = self.view(req, team_name='engineering')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['memberships'][0]['email'], 'ryo@shizuha.com')
        params = mock_req.call_args[1].get('params', {})
        self.assertEqual(params.get('team_name'), 'engineering')

    @patch('requests.request')
    def test_list_members_flattens_team_roster_tiers(self, mock_req):
        mock_req.return_value = _mock_resp({
            'rosters': [{
                'team_name': 'admin-ops',
                'tiers': [['shizuha@shizuha.com'], ['nagi@shizuha.com']],
                'junior_emails': [],
            }],
            'memberships': [],
        })
        req = self.factory.get(
            '/v1/fleet/teams/admin-ops/members/', HTTP_AUTHORIZATION=_BEARER,
        )
        with patch('fleet.views._require_staff', return_value=None):
            resp = self.view(req, team_name='admin-ops')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['team_name'], 'admin-ops')
        self.assertEqual(resp.data['display_name'], 'Admin Ops')
        self.assertEqual(resp.data['lead_email'], 'shizuha@shizuha.com')
        self.assertEqual(
            resp.data['memberships'],
            [
                {
                    'email': 'shizuha@shizuha.com',
                    'team_name': 'admin-ops',
                    'tier': 0,
                    'tier_index': 0,
                    'role': 'member',
                    'is_junior': False,
                    'routing_health': {'ok': True, 'warnings': [], 'blocking_services': []},
                },
                {
                    'email': 'nagi@shizuha.com',
                    'team_name': 'admin-ops',
                    'tier': 1,
                    'tier_index': 1,
                    'role': 'member',
                    'is_junior': False,
                    'routing_health': {'ok': True, 'warnings': [], 'blocking_services': []},
                },
            ],
        )
        self.assertEqual(resp.data['membership_source'], 'team_roster_tiers')

    @patch('requests.request')
    def test_list_members_uses_roster_edges_not_lead_metadata(self, mock_req):
        mock_req.return_value = _mock_resp({
            'rosters': [{
                'team_name': 'engineering',
                'lead_email': 'kai@shizuha.com',
                'tiers': [['ryo@shizuha.com'], ['nagi@shizuha.com']],
                'junior_emails': ['saki@shizuha.com'],
            }],
        })
        req = self.factory.get(
            '/v1/fleet/teams/engineering/members/', HTTP_AUTHORIZATION=_BEARER,
        )
        with patch('fleet.views._require_staff', return_value=None):
            resp = self.view(req, team_name='engineering')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            [m['email'] for m in resp.data['memberships']],
            ['ryo@shizuha.com', 'nagi@shizuha.com', 'saki@shizuha.com'],
        )
        self.assertEqual(resp.data['memberships'][0]['tier'], 0)
        self.assertEqual(resp.data['memberships'][1]['tier'], 1)


    @patch('requests.request')
    def test_add_member(self, mock_req):
        mock_req.return_value = _mock_resp(_WRITE_OK)
        req = self.factory.post(
            '/v1/fleet/teams/engineering/members/',
            {'action': 'add', 'email': 'ryo@shizuha.com'},
            format='json',
            HTTP_AUTHORIZATION=_BEARER,
        )
        with patch('fleet.views._require_staff', return_value=None):
            resp = self.view(req, team_name='engineering')
        self.assertEqual(resp.status_code, 200)
        mutation = next(call for call in mock_req.call_args_list if call.args[0] == 'POST')
        body = mutation.kwargs.get('json', {})
        self.assertEqual(body['action'], 'add')
        self.assertEqual(body['team_name'], 'engineering')
        self.assertEqual(body['email'], 'ryo@shizuha.com')

    @patch('requests.request')
    def test_remove_member(self, mock_req):
        mock_req.return_value = _mock_resp({**_WRITE_OK, 'action': 'remove'})
        req = self.factory.post(
            '/v1/fleet/teams/engineering/members/',
            {'action': 'remove', 'email': 'ryo@shizuha.com'},
            format='json',
            HTTP_AUTHORIZATION=_BEARER,
        )
        with patch('fleet.views._require_staff', return_value=None):
            resp = self.view(req, team_name='engineering')
        self.assertEqual(resp.status_code, 200)
        mutation = next(call for call in mock_req.call_args_list if call.args[0] == 'POST')
        self.assertEqual(mutation.kwargs.get('json', {})['action'], 'remove')

    def test_add_member_missing_email_returns_400(self):
        req = self.factory.post(
            '/v1/fleet/teams/engineering/members/',
            {'action': 'add'},
            format='json',
            HTTP_AUTHORIZATION=_BEARER,
        )
        with patch('fleet.views._require_staff', return_value=None):
            resp = self.view(req, team_name='engineering')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('error', resp.data)

    def test_invalid_action_returns_400(self):
        req = self.factory.post(
            '/v1/fleet/teams/engineering/members/',
            {'action': 'destroy', 'email': 'ryo@shizuha.com'},
            format='json',
            HTTP_AUTHORIZATION=_BEARER,
        )
        with patch('fleet.views._require_staff', return_value=None):
            resp = self.view(req, team_name='engineering')
        self.assertEqual(resp.status_code, 400)

    def test_list_returns_401_without_bearer(self):
        req = self.factory.get('/v1/fleet/teams/engineering/members/')
        with patch('fleet.views._require_staff', return_value=None):
            resp = self.view(req, team_name='engineering')
        self.assertEqual(resp.status_code, 401)
