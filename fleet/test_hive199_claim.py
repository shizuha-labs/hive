from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from fleet import external_agents
from fleet.views import FleetAgentClaimView


class ExternalAgentClaimHelperTests(SimpleTestCase):
    def test_claim_external_agent_posts_actor_not_body_owner(self):
        captured = {}

        def fake_patch(url, json=None, timeout=None):
            captured.update({'url': url, 'json': json, 'timeout': timeout})
            return mock.Mock(status_code=200, json=lambda: {'action': 'claimed', 'owner_id': 42})

        with mock.patch.object(external_agents, '_id_internal_base', return_value='http://id'), \
                mock.patch.object(external_agents.requests, 'patch', side_effect=fake_patch):
            ok, detail = external_agents.claim_external_agent(
                74, actor_user_id=42, actor_email='human@example.com', is_staff=False)

        self.assertTrue(ok, detail)
        self.assertEqual(captured['url'], 'http://id/api/internal/agents/')
        self.assertEqual(captured['json']['agent_user_id'], 74)
        self.assertEqual(captured['json']['actor_user_id'], 42)
        self.assertNotIn('owner_id', captured['json'])


class FleetAgentClaimViewTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.view = FleetAgentClaimView.as_view()
        self.human = SimpleNamespace(id=42, email='human@example.com', username='human', is_staff=False, is_authenticated=True)

    def _request(self, data=None, user=None, token=None):
        req = self.factory.post('/v1/fleet/agents/ext:74/claim/', data or {}, format='json')
        force_authenticate(req, user=user or self.human, token=token or {'email': 'human@example.com'})
        return req

    def test_claim_rejects_agent_token(self):
        req = self._request(token={'email': 'scout@agents.shizuha.io', 'account_type': 'agent'})
        resp = self.view(req, agent_id='ext:74')
        self.assertEqual(resp.status_code, 403)

    @mock.patch('fleet.views._nondb_agent', return_value={
        'id': 'ext:74', 'agent_id': '74', 'agent_username': 'scout',
        'is_external_byo': True, 'owner_id': None,
    })
    @mock.patch('fleet.external_agents.claim_external_agent', return_value=(True, {'action': 'claimed', 'owner_id': 42}))
    def test_claim_uses_authenticated_human_actor(self, claim, _agent):
        resp = self.view(self._request(), agent_id='ext:74')
        self.assertEqual(resp.status_code, 200, resp.data)
        claim.assert_called_once()
        kwargs = claim.call_args.kwargs
        self.assertEqual(kwargs['actor_user_id'], 42)
        self.assertEqual(kwargs['actor_email'], 'human@example.com')
        self.assertFalse(kwargs['is_staff'])
