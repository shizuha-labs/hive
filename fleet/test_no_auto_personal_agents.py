"""Default personal agents: humans only, hibernated, never test/agent/staff.

Operator 2026-08-05 banned garbage auto-spawns (testuser, scli-qa, kai, a
duplicate Shizuha for Hritik). Operator 2026-08-19 restored a default for
every *human* — parked hibernated and isolated so User A cannot see User B.
Users who already own fleet agents are left alone.
"""
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from .models import FleetAgent
from .personal_agents import (
    connect_agent_connection_url,
    eligible_for_default_personal_agent,
    owner_record,
)


def _user(**kwargs):
    defaults = dict(
        id=115, is_authenticated=True, is_staff=False, is_superuser=False,
        is_agent=False, username='navin', email='navin@gmail.com',
        profile=SimpleNamespace(account_type='human'),
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class EligibilityTests(TestCase):
    def test_human_is_eligible(self):
        self.assertTrue(eligible_for_default_personal_agent(_user()))

    def test_staff_is_not_eligible(self):
        self.assertFalse(eligible_for_default_personal_agent(
            _user(id=3, is_staff=True, username='hritik', email='test-owner@example.com')))

    def test_agent_claim_is_not_eligible(self):
        self.assertFalse(eligible_for_default_personal_agent(_user(is_agent=True, username='kai')))

    def test_testuser_and_scli_qa_are_not_eligible(self):
        self.assertFalse(eligible_for_default_personal_agent(
            _user(username='testuser', email='testuser@example.com')))
        self.assertFalse(eligible_for_default_personal_agent(
            _user(username='scli-qa', email='scli-qa@shizuha.com')))

    def test_qa_and_service_accounts_are_not_eligible(self):
        self.assertFalse(eligible_for_default_personal_agent(
            _user(id=739, username='liveqa', email='liveqa@shizuha.com')))
        self.assertFalse(eligible_for_default_personal_agent(
            _user(id=295, username='origin-ci', email='origin-ci@shizuha.com')))
        self.assertFalse(eligible_for_default_personal_agent(
            _user(id=252, username='qa-hive-fixture', email='qa-hive-fixture@shizuha.com')))
        self.assertFalse(eligible_for_default_personal_agent(
            _user(id=492, username='qat10598230', email='qat10598230@qa.invalid')))

    def test_existing_fleet_owner_is_not_eligible(self):
        FleetAgent.objects.create(
            agent_username='hina', owner_id=3, status=FleetAgent.Status.RUNNING)
        self.assertFalse(eligible_for_default_personal_agent(
            _user(id=3, username='hritik', email='test-owner@example.com')))

    def test_fleet_agent_username_is_not_eligible(self):
        FleetAgent.objects.create(agent_username='kai', status=FleetAgent.Status.RUNNING)
        self.assertFalse(eligible_for_default_personal_agent(_user(username='kai', email='kai@shizuha.com')))


class ConnectWireUrlTests(SimpleTestCase):
    def test_cluster_service_drops_nginx_connect_prefix(self):
        self.assertEqual(
            connect_agent_connection_url('http://shizuha-connect.shizuha.svc.cluster.local:8019'),
            'http://shizuha-connect.shizuha.svc.cluster.local:8019/api/internal/agent-connection/',
        )

    def test_platform_nginx_keeps_connect_prefix(self):
        self.assertEqual(
            connect_agent_connection_url('http://shizuha-nginx.shizuha.svc.cluster.local'),
            'http://shizuha-nginx.shizuha.svc.cluster.local/connect/api/internal/agent-connection/',
        )


class PersonalAgentEnsureViewTests(SimpleTestCase):
    def test_endpoint_creates_for_an_eligible_human(self):
        from pathlib import Path
        views = Path(__file__).resolve().parent / 'views.py'
        text = views.read_text()
        self.assertIn('eligible_for_default_personal_agent', text)
        start = text.index('class PersonalAgentEnsureView')
        body = text[start:text.index('\nclass ', start + 1)]
        self.assertIn('ensure_personal_shizuha_agent', body)
        self.assertIn('created', body)
        self.assertIn('identity_user_id', body)


class InternalEnsurePersonalAgentViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.url = '/hive/api/v1/fleet/internal/ensure-personal-agent/'

    def test_staff_is_skipped(self):
        with patch('fleet.personal_agents.ensure_personal_shizuha_agent') as ensure:
            resp = self.client.post(self.url, {
                'owner_id': 3, 'username': 'hritik', 'email': 'test-owner@example.com',
                'is_staff': True, 'account_type': 'human',
            }, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['skipped'])
        ensure.assert_not_called()

    def test_existing_owner_is_skipped(self):
        FleetAgent.objects.create(
            agent_username='hina', owner_id=3, status=FleetAgent.Status.RUNNING)
        with patch('fleet.personal_agents.ensure_personal_shizuha_agent') as ensure:
            resp = self.client.post(self.url, {
                'owner_id': 3, 'username': 'hritik', 'email': 'test-owner@example.com',
                'is_staff': False, 'account_type': 'human',
            }, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['skipped'])
        ensure.assert_not_called()

    def test_eligible_human_is_ensured_hibernated(self):
        agent = SimpleNamespace(
            agent_username='shizuha-95', status=FleetAgent.Status.HIBERNATED,
            isolated=True, hibernation_reason='default_until_first_use',
        )
        result = SimpleNamespace(agent=agent, created=True)
        with patch('fleet.personal_agents.ensure_personal_shizuha_agent', return_value=result) as ensure:
            resp = self.client.post(self.url, {
                'owner_id': 95, 'username': 'komal', 'email': 'komal@gmail.com',
                'account_type': 'human',
            }, format='json')
        self.assertEqual(resp.status_code, 201)
        self.assertFalse(resp.data['skipped'])
        self.assertEqual(resp.data['agent_username'], 'shizuha-95')
        self.assertEqual(resp.data['status'], FleetAgent.Status.HIBERNATED)
        ensure.assert_called_once()

    def test_missing_owner_id_is_400(self):
        resp = self.client.post(self.url, {'username': 'komal'}, format='json')
        self.assertEqual(resp.status_code, 400)


class EnsureDefaultCommandTests(TestCase):
    @patch('fleet.management.commands.ensure_default_personal_agents.ensure_personal_shizuha_agent')
    @patch('fleet.management.commands.ensure_default_personal_agents.iter_id_human_directory')
    def test_skips_staff_and_existing_owners(self, humans, ensure):
        FleetAgent.objects.create(
            agent_username='hina', owner_id=3, status=FleetAgent.Status.RUNNING)
        humans.return_value = [
            owner_record(owner_id=3, username='hritik', email='test-owner@example.com', is_staff=True),
            owner_record(owner_id=95, username='komal', email='komal@gmail.com'),
        ]
        created = SimpleNamespace(
            agent=SimpleNamespace(
                agent_username='shizuha-95', status='hibernated', isolated=True),
            created=True,
        )
        ensure.return_value = created
        call_command('ensure_default_personal_agents')
        ensure.assert_called_once()
        self.assertEqual(ensure.call_args.kwargs['owner_id'], 95)

    @patch('fleet.management.commands.ensure_default_personal_agents.ensure_personal_shizuha_agent')
    @patch('fleet.management.commands.ensure_default_personal_agents.iter_id_human_directory')
    def test_aligns_existing_personal_agent(self, humans, ensure):
        FleetAgent.objects.create(
            agent_username='shizuha-279', owner_id=279,
            status=FleetAgent.Status.HIBERNATED,
            hibernation_reason='default_until_first_use',
        )
        humans.return_value = [
            owner_record(owner_id=279, username='mihirgates', email='mihirgates@hotmail.com'),
        ]
        ensure.return_value = SimpleNamespace(
            agent=SimpleNamespace(
                agent_username='shizuha-279', status='hibernated', isolated=True),
            created=False,
        )
        call_command('ensure_default_personal_agents')
        ensure.assert_called_once()
        self.assertEqual(ensure.call_args.kwargs['owner_id'], 279)
