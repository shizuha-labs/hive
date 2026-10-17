from types import SimpleNamespace
from unittest import mock

from django.test import TestCase

from claws.models import Claw
from fleet.identity_health import annotate_agents
from fleet.models import FleetAgent
from fleet.views import FleetAgentListView


def user(user_id, username, email=None, *, is_staff=False):
    return SimpleNamespace(
        id=user_id,
        username=username,
        email=email or f'{username}@example.com',
        is_staff=is_staff,
        is_authenticated=True,
    )


class FleetClawAgentListTests(TestCase):
    def setUp(self):
        self.hritik = user(3, 'hritik', 'test-owner@example.com', is_staff=True)
        self.other = user(116, 'harshita', 'harshita@shizuha.com')
        self.claw = Claw.objects.create(
            owner_id=self.hritik.id,
            owner_username=self.hritik.username,
            owner_email=self.hritik.email,
            name='claw',
            tier_id='standard',
            agent_type=Claw.AgentType.CLAW,
            model='openai/gpt-5.5',
            status=Claw.Status.RUNNING,
            namespace='claw-3-test',
            pod_name='claw-pod',
        )
        Claw.objects.create(
            owner_id=self.other.id,
            owner_username=self.other.username,
            owner_email=self.other.email,
            name='Hermes',
            tier_id='standard',
            agent_type=Claw.AgentType.HERMES,
            model='DeepSeek-V4-Flash',
            status=Claw.Status.RUNNING,
        )

    def test_staff_fleet_list_excludes_retired_claw_agents(self):
        request = SimpleNamespace(user=self.hritik)
        view = FleetAgentListView()

        with mock.patch('fleet.views.list_runtime_agents', return_value=[]), \
             mock.patch('fleet.views.list_external_agents', return_value=[]), \
             mock.patch('fleet.views.get_runtime_activity_rates', return_value={}), \
             mock.patch('fleet.views._attach_pulse_team_memberships', side_effect=lambda _request, agents, org_slug='': agents), \
             mock.patch('fleet.views._attach_identity_routing_health', side_effect=lambda agents: agents):
            agents = view._build_agents_index(request, include_external=True, owner_only=False)

        self.assertEqual(agents, [])

    def test_claw_account_agent_skips_per_agent_identity_health(self):
        agents = [{
            'kind': 'claw',
            'runtime_source': 'claw',
            'agent_username': 'claw',
            'agent_id': f'claw:{self.claw.id}',
            'team_memberships': [],
        }]

        annotate_agents(agents, [{
            'id': 70,
            'username': 'claw',
            'email': 'claw@shizuha.com',
            'account_type': 'agent',
            'active': True,
        }])

        self.assertTrue(agents[0]['identity_health']['ok'])
        self.assertTrue(agents[0]['identity_health']['not_applicable'])
        self.assertTrue(agents[0]['health_summary']['ok'])


class IsolatedPersonalShizuhaListTests(TestCase):
    def test_staff_fleet_list_excludes_isolated_personal_shizuha(self):
        FleetAgent.objects.create(
            agent_username='riko', role='Engineer', status=FleetAgent.Status.HIBERNATED)
        FleetAgent.objects.create(
            agent_username='shizuha-115',
            display_name='Shizuha',
            role='Personal Shizuha',
            owner_id=115,
            isolated=True,
            status=FleetAgent.Status.HIBERNATED,
        )
        request = SimpleNamespace(user=user(3, 'hritik', 'test-owner@example.com', is_staff=True))
        view = FleetAgentListView()
        with mock.patch('fleet.views.list_runtime_agents', return_value=[]), \
             mock.patch('fleet.views.list_external_agents', return_value=[]), \
             mock.patch('fleet.views.get_runtime_activity_rates', return_value={}), \
             mock.patch('fleet.views._attach_pulse_team_memberships', side_effect=lambda _request, agents, org_slug='': agents), \
             mock.patch('fleet.views._attach_identity_routing_health', side_effect=lambda agents: agents), \
             mock.patch('fleet.views._attach_health_summaries', side_effect=lambda agents: agents):
            agents = view._build_agents_index(request, include_external=True, owner_only=False)
        names = {(a.get('agent_username') or '').lower() for a in agents}
        self.assertIn('riko', names)
        self.assertNotIn('shizuha-115', names)
