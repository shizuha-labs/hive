"""HIVE-1255: unarchive resolves archived/deleted agents; Admin JIT contract shape."""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from fleet import views
from fleet.models import FleetAgent, FleetAgentArchive
from fleet.views import FleetAgentUnarchiveView


class UnarchiveResolveHelperTests(SimpleTestCase):
    @patch('fleet.views._agent_username_for_id', return_value=('', None))
    @patch('fleet.views.FleetAgent.objects')
    def test_includes_deleted_native_row(self, objects, _base):
        agent = SimpleNamespace(id='uuid-1', agent_username='kenta')
        objects.get.return_value = agent
        username, native = views._agent_username_for_unarchive('uuid-1')
        self.assertEqual(username, 'kenta')
        self.assertIs(native, agent)
        objects.get.assert_called_once()

    @patch('fleet.views._agent_username_for_id', return_value=('', None))
    @patch('fleet.views.FleetAgentArchive.objects')
    @patch('fleet.views.FleetAgent.objects')
    def test_archive_marker_by_username(self, fa_objects, archive_objects, _base):
        from django.core.exceptions import ValidationError
        fa_objects.get.side_effect = FleetAgent.DoesNotExist
        fa_objects.filter.return_value.first.return_value = None
        archive_objects.filter.return_value.exists.return_value = True
        username, native = views._agent_username_for_unarchive('kenta')
        self.assertEqual(username, 'kenta')
        self.assertIsNone(native)


class UnarchiveDeletedAgentDBTests(TestCase):
    def test_unarchive_lifts_marker_for_deleted_agent_uuid(self):
        agent = FleetAgent(
            agent_username='kenta-hive1255',
            display_name='Kenta',
            status=FleetAgent.Status.DELETED,
        )
        # Bypass full clean if DNS validator is fine with this name
        agent.save()
        FleetAgentArchive.objects.create(agent_username='kenta-hive1255', reason='parked')
        factory = APIRequestFactory()
        user = SimpleNamespace(
            is_staff=True, is_authenticated=True, id=1, email='staff@shizuha.com',
        )
        request = factory.post(
            f'/v1/fleet/agents/{agent.id}/unarchive/', {}, format='json',
        )
        force_authenticate(request, user=user)
        request.user = user
        with patch('fleet.views.agent_index_cache.invalidate'):
            response = FleetAgentUnarchiveView.as_view()(request, agent_id=str(agent.id))
        self.assertEqual(response.status_code, 200, getattr(response, 'data', response))
        self.assertEqual(response.data.get('archived'), False)
        self.assertFalse(
            FleetAgentArchive.objects.filter(agent_username__iexact='kenta-hive1255').exists()
        )


@override_settings(HIVE_ADMIN_API_URL='http://admin.test/api')
class ActiveJitGrantContractShapeTests(SimpleTestCase):
    def _request(self):
        factory = APIRequestFactory()
        req = factory.post('/v1/fleet/agents/x/unarchive/')
        req.user = SimpleNamespace(
            is_staff=False, id=9, email='mika@shizuha.com',
            organization_memberships={'1': 'member'},
        )
        req.META = {'HTTP_AUTHORIZATION': 'Bearer test-token'}
        return req

    @patch('fleet.views.requests.get')
    def test_incomplete_page_is_dependency(self, get):
        get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                'count': 2,
                'results': [{
                    'id': 1, 'organization': 1, 'requester_user_id': 9,
                    'requester_email': 'mika@shizuha.com', 'status': 'approved',
                    'expires_at': '2099-01-01T00:00:00Z', 'revoked_at': None,
                    'active': True, 'scope': 'hive.fleet.agent.unarchive:kenta',
                }],
            },
        )
        grant, decision, reason = views._active_jit_grant_for_scope(
            self._request(), 'hive.fleet.agent.unarchive:kenta',
        )
        self.assertIsNone(grant)
        self.assertEqual(decision, 'dependency')
        self.assertIn('incomplete', reason)


if os.environ.get('HIVE_ADMIN_JIT_IT') == '1':
    class LiveAdminJitContractIT(TestCase):
        def test_list_approved_shape(self):
            import requests
            base = os.environ['HIVE_ADMIN_API_URL'].rstrip('/')
            bearer = os.environ['HIVE_ADMIN_JIT_IT_BEARER']
            resp = requests.get(
                f'{base}/jit-elevations/',
                headers={'Authorization': f'Bearer {bearer}'},
                params={'status': 'approved'},
                timeout=10,
            )
            self.assertEqual(resp.status_code, 200)
            payload = resp.json()
            self.assertEqual(int(payload['count']), len(payload['results']))
            required = {
                'id', 'organization', 'requester_user_id', 'requester_email',
                'status', 'expires_at', 'revoked_at', 'active',
            }
            for grant in payload['results']:
                self.assertTrue(required.issubset(grant.keys()), grant)
                self.assertTrue(str(grant.get('requester_user_id') or '').strip())
