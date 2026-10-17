"""HIVE-602: Connect-DM wake endpoint — token gate + wake-only semantics."""
import os
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

URL = '/hive/api/v1/fleet/internal/connect-wake'
ENV = {'HIVE_CONNECT_WAKE_TOKEN': 'sekrit'}


def _pool_config():
    return [{
        'model_id': 'DeepSeek-V4-Flash',
        'members': ['ichi'],
        'max_holders': 7,
        'enabled': True,
    }]


def _agent(email='ichi@shizuha.com', enabled=False):
    return {'id': 'ichi', 'agent_username': 'ichi', 'email': email, 'enabled': enabled}


class ConnectWakeTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def _post(self, token='sekrit', **body):
        return self.client.post(URL, {'email': 'ichi@shizuha.com', **body},
                                format='json', headers={'X-Hive-Wake-Token': token})

    def test_rejects_bad_or_missing_token(self):
        with mock.patch.dict(os.environ, ENV):
            self.assertEqual(self._post(token='wrong').status_code, 403)
        with mock.patch.dict(os.environ, {'HIVE_CONNECT_WAKE_TOKEN': ''}):
            self.assertEqual(self._post().status_code, 403)

    def test_wakes_stopped_agent(self):
        with mock.patch.dict(os.environ, ENV), \
             mock.patch('fleet.views.list_lifecycle_agents', return_value=[_agent(enabled=False)]), \
             mock.patch('fleet.views.set_lifecycle_agent_enabled', return_value=(True, {'ok': 1})) as toggle:
            resp = self._post(reason='connect_dm:c1')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'started')
        toggle.assert_called_once_with('ichi', True)

    def test_running_agent_is_idempotent_noop(self):
        with mock.patch.dict(os.environ, ENV), \
             mock.patch('fleet.views.list_lifecycle_agents', return_value=[_agent(enabled=True)]), \
             mock.patch('fleet.views.set_lifecycle_agent_enabled') as toggle:
            resp = self._post()
        self.assertEqual(resp.data['status'], 'already_running')
        toggle.assert_not_called()

    def test_unknown_agent_404(self):
        with mock.patch.dict(os.environ, ENV), \
             mock.patch('fleet.views.list_lifecycle_agents', return_value=[]):
            self.assertEqual(self._post().status_code, 404)

    def test_archived_agent_is_never_woken(self):
        from fleet.models import FleetAgentArchive

        FleetAgentArchive.objects.create(agent_username='ichi')
        with mock.patch.dict(os.environ, ENV), \
             mock.patch('fleet.views.list_lifecycle_agents', return_value=[_agent(enabled=False)]), \
             mock.patch('fleet.views.set_lifecycle_agent_enabled') as toggle:
            resp = self._post()
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data['status'], 'archived')
        toggle.assert_not_called()

    def test_wake_deferred_when_lease_pool_full(self):
        """Operator 2026-08-08: never boot a governed agent into a full pool.

        Connect's DM wake must not churn kei/ryo: waking into a 7/7 board makes
        the agent cold-boot, sit lease-gated, and get re-hibernated by the next
        10-min reconcile. The wake is deferred (202) and the 10-min lease
        reconcile grants when a slot frees (the unread DM keeps it worthy).
        """
        agent = {**_agent(enabled=False), 'model': 'DeepSeek-V4-Flash', 'effective_model': 'cortex/DeepSeek-V4-Flash'}
        board = {'max_holders': 7, 'holders': [
            {'principal': 'agent-a'}, {'principal': 'agent-b'}, {'principal': 'agent-c'},
            {'principal': 'agent-d'}, {'principal': 'agent-e'}, {'principal': 'agent-f'},
            {'principal': 'agent-g'},
        ]}
        with mock.patch.dict(os.environ, ENV), \
             mock.patch('fleet.views.list_lifecycle_agents', return_value=[agent]), \
             mock.patch('fleet.views.set_lifecycle_agent_enabled') as toggle, \
             mock.patch('fleet.model_lease.all_pool_configs', return_value=_pool_config()), \
             mock.patch.object(type(leaf_wake_client()), 'board', return_value=board):
            resp = self._post()
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.data['status'], 'lease_waiting')
        toggle.assert_not_called()

    def test_wake_proceeds_when_lease_slot_free(self):
        """A free slot or already-holder should still wake immediately."""
        agent = {**_agent(enabled=False), 'model': 'DeepSeek-V4-Flash', 'effective_model': 'cortex/DeepSeek-V4-Flash'}
        board = {'max_holders': 7, 'holders': [{'principal': 'agent-a'}]}
        with mock.patch.dict(os.environ, ENV), \
             mock.patch('fleet.views.list_lifecycle_agents', return_value=[agent]), \
             mock.patch('fleet.views.set_lifecycle_agent_enabled', return_value=(True, {'ok': 1})) as toggle, \
             mock.patch('fleet.model_lease.all_pool_configs', return_value=_pool_config()), \
             mock.patch.object(type(leaf_wake_client()), 'board', return_value=board):
            resp = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'started')
        toggle.assert_called_once()

    def test_wake_proceeds_when_already_holder(self):
        agent = {**_agent(enabled=False), 'model': 'DeepSeek-V4-Flash', 'effective_model': 'cortex/DeepSeek-V4-Flash'}
        board = {'max_holders': 7, 'holders': [{'principal': 'agent-ichi'}, {'principal': 'agent-a'}]}
        with mock.patch.dict(os.environ, ENV), \
             mock.patch('fleet.views.list_lifecycle_agents', return_value=[agent]), \
             mock.patch('fleet.views.set_lifecycle_agent_enabled', return_value=(True, {'ok': 1})) as toggle, \
             mock.patch('fleet.model_lease.all_pool_configs', return_value=_pool_config()), \
             mock.patch.object(type(leaf_wake_client()), 'board', return_value=board):
            resp = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'started')
        toggle.assert_called_once()

    def test_wake_proceeds_when_not_in_lease_pool(self):
        """Agents whose model has no lease pool are unaffected (no gate)."""
        with mock.patch.dict(os.environ, ENV), \
             mock.patch('fleet.views.list_lifecycle_agents', return_value=[_agent(enabled=False)]), \
             mock.patch('fleet.views.set_lifecycle_agent_enabled', return_value=(True, {'ok': 1})) as toggle:
            resp = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'started')
        toggle.assert_called_once()

    def test_wakes_personal_agent_on_agents_shizuha_io_email(self):
        """Connect stores personal-agent participants as @agents.shizuha.io.

        Native projection used to expose only @shizuha.com, so first-DM wake
        returned unknown_agent and the owner's Shizuha never booted.
        """
        agent = {
            'id': 'personal-uuid',
            'agent_username': 'shizuha-741',
            'username': 'shizuha-741',
            'email': 'shizuha-741@agents.shizuha.io',
            'enabled': False,
            'status': 'hibernated',
        }
        with mock.patch.dict(os.environ, ENV), \
             mock.patch('fleet.views.list_lifecycle_agents', return_value=[agent]), \
             mock.patch('fleet.views.set_lifecycle_agent_enabled', return_value=(True, {'ok': 1})) as toggle:
            resp = self.client.post(
                URL,
                {'email': 'shizuha-741@agents.shizuha.io', 'reason': 'connect_dm:c-personal'},
                format='json',
                headers={'X-Hive-Wake-Token': 'sekrit'},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'started')
        toggle.assert_called_once_with('personal-uuid', True)

    def test_wakes_when_connect_email_domain_differs_from_projection(self):
        """Alias match: Connect @agents.shizuha.io vs projected @shizuha.com."""
        agent = _agent(email='shizuha-115@shizuha.com', enabled=False)
        agent['id'] = 'navin-uuid'
        agent['agent_username'] = 'shizuha-115'
        agent['username'] = 'shizuha-115'
        with mock.patch.dict(os.environ, ENV), \
             mock.patch('fleet.views.list_lifecycle_agents', return_value=[agent]), \
             mock.patch('fleet.views.set_lifecycle_agent_enabled', return_value=(True, {'ok': 1})) as toggle:
            resp = self.client.post(
                URL,
                {'email': 'shizuha-115@agents.shizuha.io'},
                format='json',
                headers={'X-Hive-Wake-Token': 'sekrit'},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'started')
        toggle.assert_called_once_with('navin-uuid', True)


def leaf_wake_client():
    """Return a real CortexLeaseClient so it can be patched for board reads."""
    from fleet.model_lease import CortexLeaseClient
    return CortexLeaseClient(timeout=1.0)


def _personal_binding(owner_id):
    """Exact immutable ID rows for one personal Shizuha owner (HIVE-2009)."""
    from fleet.provisioner import FleetIdentityBinding

    return FleetIdentityBinding(
        user_id=10_000 + owner_id,
        profile_id=20_000 + owner_id,
        owner_user_id=owner_id,
        owner_profile_id=30_000 + owner_id,
        ownership_class='personal_default_member',
    )


class DefaultUntilFirstUseWakeTests(TestCase):
    """ADR-0080 / HIVE-2009: a new human's default personal FleetAgent stays
    hibernated (default_until_first_use) until first use, then connect-wake
    boots it on the first DM — the Navin (shizuha-115) / Ganesh (shizuha-741)
    wake path, end to end."""

    def test_new_personal_agent_stays_hibernated_then_wakes_on_first_use(self):
        from fleet.models import FleetAgent
        from fleet.personal_agents import ensure_personal_shizuha_agent

        with mock.patch('fleet.personal_agents.wire_personal_agent_connectivity'), \
             mock.patch('fleet.personal_agents._provision_platform_identity_details',
                        return_value=_personal_binding(741)), \
             mock.patch('fleet.personal_agents.provisioner.provision_agent', return_value=True), \
             mock.patch('fleet.personal_agents._generate_agent_password',
                        return_value='generated!A1'):
            result = ensure_personal_shizuha_agent(owner_id=741, owner_username='ganesh')
        agent = result.agent
        agent.refresh_from_db()

        # Starts hibernated — no pod before first real use.
        self.assertEqual(agent.status, FleetAgent.Status.HIBERNATED)
        self.assertEqual(agent.hibernation_reason, 'default_until_first_use')
        self.assertTrue(str(agent.agent_id or '').strip())

        # First use (a DM) wakes it through the connect-wake path.
        projected = {
            'id': str(agent.agent_id),
            'agent_username': agent.agent_username,
            'username': agent.agent_username,
            'email': f'{agent.agent_username}@agents.shizuha.io',
            'enabled': False,
            'status': 'hibernated',
        }
        with mock.patch.dict(os.environ, ENV), \
             mock.patch('fleet.views.list_lifecycle_agents', return_value=[projected]), \
             mock.patch('fleet.views.set_lifecycle_agent_enabled',
                        return_value=(True, {'ok': 1})) as toggle:
            resp = self.client.post(
                URL,
                {'email': f'{agent.agent_username}@agents.shizuha.io',
                 'reason': 'connect_dm:c-personal'},
                format='json',
                headers={'X-Hive-Wake-Token': 'sekrit'},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'started')
        toggle.assert_called_once_with(str(agent.agent_id), True)

    def test_hibernated_personal_agent_is_not_auto_started_by_ensure(self):
        """Re-ensure on later page loads must NOT boot the parked agent — it
        stays hibernated until a real first use (no idle Running seats)."""
        from fleet.models import FleetAgent
        from fleet.personal_agents import ensure_personal_shizuha_agent

        with mock.patch('fleet.personal_agents.wire_personal_agent_connectivity'), \
             mock.patch('fleet.personal_agents._provision_platform_identity_details',
                        return_value=_personal_binding(115)), \
             mock.patch('fleet.personal_agents.provisioner.provision_agent', return_value=True), \
             mock.patch('fleet.personal_agents._generate_agent_password',
                        return_value='generated!A1'):
            first = ensure_personal_shizuha_agent(owner_id=115, owner_username='navin')
            second = ensure_personal_shizuha_agent(owner_id=115, owner_username='navin')
        self.assertFalse(second.created)
        self.assertEqual(first.agent.pk, second.agent.pk)
        second.agent.refresh_from_db()
        self.assertEqual(second.agent.status, FleetAgent.Status.HIBERNATED)
        self.assertEqual(second.agent.hibernation_reason, 'default_until_first_use')
