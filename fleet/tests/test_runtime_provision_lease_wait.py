"""Cold daemon materialization retains identity while awaiting a real lease."""
from copy import deepcopy
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings

from fleet import personal_agents, runtime_fleet
from fleet.models import DaemonRegistry, FleetAgent
from fleet.on_demand import agent_running


@override_settings(CORTEX_BASE_URL='https://cortex.invalid',
                   HIVE_CORTEX_LEASE_API_KEY='test-only',
                   RUNTIME_FLEET_CONTROL_DAEMON_ID='rt-fleet-s1')
class RuntimeProvisionLeaseWaitTests(TestCase):
    def setUp(self):
        self.daemon = DaemonRegistry.objects.create(
            daemon_id='rt-fleet-s1', fleet_id='test-fleet', owner_subject=63,
            status=DaemonRegistry.Status.CONNECTED,
        )
        self.agent = FleetAgent.objects.create(
            agent_username='shizuha-63', owner_id=63, isolated=False,
            model='cortex/GLM-5.3-Flash', status=FleetAgent.Status.PENDING,
            env={'TEST_CONFIG': 'retained'},
        )
        self.runtime_id = 'local-shizuha-63-fixed'
        self.board = {'leased': True, 'max_holders': 1, 'holders': []}
        self.control_calls = []
        self.enterContext(patch('fleet.model_lease.all_pool_configs', return_value=[{
            'model_id': 'GLM-5.3-Flash', 'max_holders': 1,
        }]))
        self.enterContext(patch('requests.request', side_effect=self.board_request))
        self.enterContext(patch.object(runtime_fleet, 'runtime_control_enabled', return_value=True))
        self.enterContext(patch.object(runtime_fleet, '_post_control', side_effect=self.control))
        self.configure = self.enterContext(patch.object(
            runtime_fleet, 'configure_runtime_agent', return_value=(True, 'saved'),
        ))
        self.native_provision = self.enterContext(patch('fleet.provisioner.provision_agent'))
        self.native_start = self.enterContext(patch('fleet.provisioner.start_agent'))

    def board_request(self, method, url, **kwargs):
        self.assertEqual((method, url),
                         ('GET', 'https://cortex.invalid/v1/admin/model-leases/GLM-5.3-Flash/'))
        if self.board is None:
            return Mock(status_code=503, text='authority unavailable')
        return Mock(status_code=200, json=Mock(return_value=deepcopy(self.board)))

    def control(self, path, body, label):
        self.control_calls.append((path, deepcopy(body)))
        if path == 'agents/provision':
            return True, {'admitted': True, 'agent_id': self.runtime_id}
        self.assertEqual(path, 'agents/toggle')
        self.assertEqual(body['agent_id'], self.runtime_id)
        self.assertTrue(body['enabled'])
        return True, {'status': 'enabled', 'lifecycle_state': 'enabled'}

    def assert_cold_bound_waiter(self):
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.agent_id, self.runtime_id)
        self.assertEqual(self.agent.managing_daemon_id, self.daemon.pk)
        self.assertEqual(self.agent.status, FleetAgent.Status.HIBERNATED)
        self.assertIsNotNone(self.agent.hibernated_at)
        self.assertEqual(self.agent.hibernation_reason, 'awaiting model lease grant')
        self.assertIn('await_reconcile_grant', self.agent.status_message)
        self.assertFalse(agent_running(runtime_fleet._fleet_agent_row_to_runtime(self.agent)))
        self.native_provision.assert_not_called()
        self.native_start.assert_not_called()

    def test_top_down_returns_typed_wait_with_cold_bound_identity(self):
        ok, detail = runtime_fleet.provision_runtime_agent_via_daemon(self.agent, self.daemon)
        self.assertFalse(ok)
        self.assertIsInstance(detail, dict)
        self.assertIs(detail.get('lease_waiting'), True)
        self.assertEqual(detail['reason'], 'await_reconcile_grant')
        self.assert_cold_bound_waiter()
        self.assertEqual([path for path, _ in self.control_calls], ['agents/provision'])
        self.configure.assert_called_once_with(self.runtime_id, {'env': {'TEST_CONFIG': 'retained'}})

    def test_personal_retry_reuses_identity_then_starts_only_after_grant(self):
        personal_agents._actuate_personal_runtime(self.agent, self.daemon)
        self.assert_cold_bound_waiter()
        personal_agents._actuate_personal_runtime(self.agent, self.daemon)
        self.assert_cold_bound_waiter()
        self.assertEqual([path for path, _ in self.control_calls], ['agents/provision'])

        self.board['holders'] = [{'principal': 'agent-shizuha-63', 'lease_id': 'planner-issued'}]
        personal_agents._actuate_personal_runtime(self.agent, self.daemon)
        self.assertEqual([path for path, _ in self.control_calls],
                         ['agents/provision', 'agents/toggle'])
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.agent_id, self.runtime_id)
        self.assertNotEqual(self.agent.status, FleetAgent.Status.FAILED)
        self.assertEqual(FleetAgent.objects.filter(agent_username='shizuha-63').count(), 1)

    def test_authority_failure_remains_failure(self):
        self.board = None
        personal_agents._actuate_personal_runtime(self.agent, self.daemon)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.status, FleetAgent.Status.FAILED)
        self.assertEqual(self.agent.agent_id, self.runtime_id)
        self.assertIn('board_unreadable', self.agent.status_message)
        self.assertEqual([path for path, _ in self.control_calls], ['agents/provision'])

    def test_wait_does_not_rewrite_existing_operator_stop(self):
        self.agent.agent_id = self.runtime_id
        self.agent.managing_daemon = self.daemon
        self.agent.status = FleetAgent.Status.STOPPED
        self.agent.runtime_lifecycle_state = 'operator_stopped'
        self.agent.save()
        personal_agents._actuate_personal_runtime(self.agent, self.daemon)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.status, FleetAgent.Status.STOPPED)
        self.assertEqual(self.agent.runtime_lifecycle_state, 'operator_stopped')
        self.assertEqual(self.control_calls, [])
