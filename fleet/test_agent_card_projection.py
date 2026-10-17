import copy
import gzip
import json
from unittest import mock

from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from claws.middleware import AgentCardGZipMiddleware
from fleet.card_projection import project_agent_card
from fleet.views import FleetAgentCardListView, FleetAgentListView


class AgentCardProjectionTests(SimpleTestCase):
    def setUp(self):
        self.full = {
            'id': 'agent-id',
            'agent_username': 'mio',
            'display_name': 'Mio',
            'status': 'running',
            'health_summary': {'ok': True, 'issues': []},
            'team_memberships': [{'team_name': 'devops'}],
            'context_prompt': 'drawer only',
            'mcp_config': {'servers': ['pulse']},
            'env': {'EXAMPLE': 'drawer-only-value'},
            'identity_health': {'diagnostics': ['drawer only']},
            'context_budget': {
                'total_tokens': 12000,
                'headroom_tokens': 116000,
                'context_window_tokens': 128000,
                'utilization_percent': 9.38,
                'outlier': False,
                'severity': 'ok',
                'measured_at': '2026-08-10T00:00:00Z',
                'thresholds': {'warning_tokens': 100000},
                'components': [{
                    'component': 'agents_md',
                    'tokens': 2130,
                    'source': 'AGENTS.md',
                    'status': 'estimated',
                    'description': 'drawer-only explanation',
                }],
            },
        }

    def test_projection_strips_detail_without_mutating_cached_row(self):
        original = copy.deepcopy(self.full)
        card = project_agent_card(self.full)

        self.assertEqual(self.full, original)
        self.assertEqual(card['id'], 'agent-id')
        self.assertEqual(card['health_summary'], {'ok': True, 'issues': []})
        self.assertEqual(card['team_memberships'], [{'team_name': 'devops'}])
        self.assertNotIn('context_prompt', card)
        self.assertNotIn('mcp_config', card)
        self.assertNotIn('env', card)
        self.assertNotIn('identity_health', card)
        self.assertEqual(card['context_budget']['total_tokens'], 12000)
        self.assertEqual(
            card['context_budget']['components'],
            [{'component': 'agents_md', 'tokens': 2130}],
        )
        self.assertNotIn('thresholds', card['context_budget'])

    def _request(self, view):
        request = APIRequestFactory().get('/v1/fleet/agent-cards/', {'page_size': 100})
        force_authenticate(request, user=type('User', (), {
            'is_authenticated': True,
            'is_staff': True,
            'id': 1,
        })())
        with mock.patch(
            'fleet.views.agent_index_cache.get_or_build',
            return_value=([self.full], 'hit'),
        ), mock.patch('fleet.models.archived_usernames', return_value=set()), mock.patch(
            'fleet.views.FleetAgent.objects.filter'
        ) as deleted, mock.patch(
            'fleet.views.pulse_roster_index_degradation', return_value=(False, None)
        ):
            deleted.return_value.values_list.return_value = []
            return view.as_view()(request)

    def test_card_endpoint_projects_but_full_list_contract_stays_intact(self):
        cards = self._request(FleetAgentCardListView)
        full = self._request(FleetAgentListView)

        self.assertNotIn('context_prompt', cards.data['results'][0])
        self.assertEqual(full.data['results'][0]['context_prompt'], 'drawer only')
        self.assertEqual(self.full['context_prompt'], 'drawer only')


class AgentCardGZipMiddlewareTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.body = json.dumps({'results': [{'agent': 'mio', 'status': 'running'}] * 80}).encode()
        self.middleware = AgentCardGZipMiddleware(
            lambda request: HttpResponse(self.body, content_type='application/json')
        )

    def test_card_projection_is_gzipped_for_capable_browser(self):
        request = self.factory.get(
            '/hive/api/v1/fleet/agent-cards/',
            HTTP_ACCEPT_ENCODING='gzip, deflate, br',
        )
        response = self.middleware(request)

        self.assertEqual(response['Content-Encoding'], 'gzip')
        self.assertEqual(json.loads(gzip.decompress(response.content)), json.loads(self.body))
        self.assertIn('Accept-Encoding', response['Vary'])

    def test_full_agent_detail_contract_is_not_compressed(self):
        request = self.factory.get(
            '/hive/api/v1/fleet/agents/',
            HTTP_ACCEPT_ENCODING='gzip',
        )
        response = self.middleware(request)

        self.assertNotIn('Content-Encoding', response)
        self.assertEqual(response.content, self.body)
