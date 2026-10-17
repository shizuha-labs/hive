"""Regression coverage for the fleet list/detail read contract.

The list includes live daemon/BYO agents before (or without) a native
FleetAgent row. Their drawer endpoints must resolve the same authorized
projection instead of returning 404 and leaving the UI in a loading loop.
"""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from rest_framework import status
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate

from .views import FleetAgentDetailView, FleetAgentEventsView


class FleetAgentReadContractTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.staff = SimpleNamespace(
            id=1,
            email='operator@example.com',
            username='operator',
            is_staff=True,
            is_authenticated=True,
        )
        self.runtime_agent = {
            'id': 'runtime-agent-id',
            'agent_username': 'jun',
            'display_name': 'Jun',
            'status': 'running',
            'runtime_source': 'daemon-link',
        }

    def _get(self, path):
        request = self.factory.get(path)
        force_authenticate(request, user=self.staff)
        return request

    @patch('fleet.views._attach_health_summaries')
    @patch('fleet.views._attach_identity_routing_health')
    @patch('fleet.views._with_telemetry', side_effect=lambda payload: payload)
    @patch('fleet.views._owned_external_agent')
    @patch('fleet.views._get_agent')
    def test_detail_resolves_authorized_runtime_agent_without_native_row(
        self,
        get_agent,
        owned_external_agent,
        _with_telemetry,
        _attach_identity,
        _attach_health,
    ):
        get_agent.return_value = Response(
            {'error': 'Agent not found'},
            status=status.HTTP_404_NOT_FOUND,
        )
        owned_external_agent.return_value = self.runtime_agent

        response = FleetAgentDetailView.as_view()(
            self._get('/v1/fleet/agents/runtime-agent-id/'),
            agent_id='runtime-agent-id',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['agent_username'], 'jun')
        _attach_identity.assert_called_once()
        _attach_health.assert_called_once()

    @patch('fleet.views._owned_external_agent')
    @patch('fleet.views._get_agent')
    def test_events_for_runtime_agent_are_empty_instead_of_404(
        self,
        get_agent,
        owned_external_agent,
    ):
        get_agent.return_value = Response(
            {'error': 'Agent not found'},
            status=status.HTTP_404_NOT_FOUND,
        )
        owned_external_agent.return_value = self.runtime_agent

        response = FleetAgentEventsView.as_view()(
            self._get('/v1/fleet/agents/runtime-agent-id/events/'),
            agent_id='runtime-agent-id',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {'events': []})

    @patch('fleet.views._owned_personal_agent', return_value=None)
    @patch('fleet.views._owned_external_agent', return_value=None)
    @patch('fleet.views._get_agent')
    def test_unknown_agent_remains_404(
        self,
        get_agent,
        _owned_external_agent,
        _owned_personal_agent,
    ):
        get_agent.return_value = Response(
            {'error': 'Agent not found'},
            status=status.HTTP_404_NOT_FOUND,
        )

        response = FleetAgentDetailView.as_view()(
            self._get('/v1/fleet/agents/missing/'),
            agent_id='missing',
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
