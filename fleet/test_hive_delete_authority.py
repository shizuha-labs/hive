"""Regression coverage for Hive-authoritative daemon-managed agent deletion."""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from .models import DaemonRegistry, FleetAgent
from .views import FleetAgentDetailView


class HiveDeleteAuthorityTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.staff = SimpleNamespace(
            id=3, email='test-owner@example.com', username='hritik',
            is_staff=True, is_authenticated=True,
        )

    def test_delete_tombstones_before_removing_daemon_and_k8s_state(self):
        daemon = DaemonRegistry.objects.create(
            daemon_id='rt-fleet-s1', fleet_id='fleet-s1',
            status=DaemonRegistry.Status.CONNECTED,
        )
        agent = FleetAgent.objects.create(
            agent_username='shizuha-3', agent_id='runtime-shizuha-3',
            owner_id=3, managing_daemon=daemon, status=FleetAgent.Status.RUNNING,
        )
        request = self.factory.delete(f'/v1/fleet/agents/{agent.id}/')
        force_authenticate(request, user=self.staff)

        observed_statuses = []

        def runtime_delete(row):
            row.refresh_from_db()
            observed_statuses.append(row.status)
            return True, ''

        with patch('fleet.views._require_staff', return_value=None), \
                patch('fleet.lifecycle.delete_runtime_agent', side_effect=runtime_delete) as delete_runtime, \
                patch('fleet.lifecycle.provisioner.deprovision_agent', return_value=True) as deprovision, \
                patch('fleet.lifecycle.agent_index_cache.invalidate'):
            response = FleetAgentDetailView.as_view()(request, agent_id=str(agent.id))

        self.assertEqual(response.status_code, 204)
        agent.refresh_from_db()
        self.assertEqual(agent.status, FleetAgent.Status.DELETED)
        self.assertEqual(observed_statuses, [FleetAgent.Status.DELETED])
        delete_runtime.assert_called_once()
        deprovision.assert_called_once()
