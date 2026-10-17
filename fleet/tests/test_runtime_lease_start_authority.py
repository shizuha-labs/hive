"""Real daemon enable boundary refuses nonholders before the control POST."""
from copy import deepcopy
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings

from fleet.models import FleetAgent
from fleet.runtime_fleet import set_runtime_agent_enabled


@override_settings(CORTEX_BASE_URL='https://cortex.invalid', HIVE_CORTEX_LEASE_API_KEY='test-only')
class RuntimeLeaseStartAuthorityTests(TestCase):
    def setUp(self):
        self.agents = [FleetAgent.objects.create(
            agent_username=name, agent_id=f'runtime-{name}',
            status=FleetAgent.Status.HIBERNATED, model='cortex/GLM-5.3-Flash',
        ) for name in ('sara', 'kumo')]
        self.board = {'leased': True, 'max_holders': 1, 'holders': []}
        self.requests = []
        self.enterContext(patch('fleet.model_lease.all_pool_configs', return_value=[{
            'model_id': 'GLM-5.3-Flash', 'max_holders': 1,
        }]))
        self.enterContext(patch('requests.request', side_effect=self.board_request))
        self.control = self.enterContext(patch('fleet.runtime_fleet._post_control', return_value=(True, {'enabled': True})))

    def board_request(self, method, url, **kwargs):
        self.requests.append((method, url))
        self.assertEqual((method, url), ('GET', 'https://cortex.invalid/v1/admin/model-leases/GLM-5.3-Flash/'))
        return Mock(status_code=200, json=Mock(return_value=deepcopy(self.board)))

    def test_two_candidates_cannot_start_on_one_ungranted_slot_then_holder_can(self):
        for _ in range(2):
            for agent in self.agents:
                ok, detail = set_runtime_agent_enabled(agent.agent_id, True)
                self.assertFalse(ok)
                self.assertIs(detail['lease_waiting'], True)
                self.assertEqual(detail['reason'], 'await_reconcile_grant')
        self.control.assert_not_called()
        self.board['holders'] = [{'principal': 'agent-sara', 'lease_id': 'planner-issued'}]
        self.assertTrue(set_runtime_agent_enabled('runtime-sara', True)[0])
        self.assertFalse(set_runtime_agent_enabled('runtime-kumo', True)[0])
        self.control.assert_called_once_with('agents/toggle', {
            'agent_id': 'runtime-sara', 'enabled': True, 'overrideKillSwitch': True,
        }, 'enable')
        self.assertEqual(len(self.requests), 6)

    def test_unreadable_authority_is_failure_and_disable_still_works(self):
        with patch('fleet.model_lease.lease_wake_available', side_effect=TimeoutError('synthetic')):
            ok, detail = set_runtime_agent_enabled('runtime-sara', True)
            self.assertFalse(ok)
            self.assertNotEqual(detail.get('lease_waiting'), True)
            self.assertEqual(detail['reason'], 'gate_error_fail_closed')
            self.control.assert_not_called()
            self.assertTrue(set_runtime_agent_enabled('runtime-sara', False)[0])
        self.control.assert_called_once_with('agents/toggle', {
            'agent_id': 'runtime-sara', 'enabled': False, 'overrideKillSwitch': True,
            'lifecycle_state': 'operator_stopped',
        }, 'disable')

    def test_legacy_inventory_is_checked_and_missing_identity_is_not_free_capacity(self):
        with patch('fleet.runtime_fleet.get_runtime_agent', return_value={
            'id': 'legacy', 'agent_username': 'sara', 'effective_model': 'cortex/GLM-5.3-Flash',
        }):
            ok, detail = set_runtime_agent_enabled('legacy', True)
            self.assertFalse(ok)
            self.assertTrue(detail['lease_waiting'])
        with patch('fleet.runtime_fleet.get_runtime_agent', return_value=None):
            ok, detail = set_runtime_agent_enabled('missing', True)
            self.assertFalse(ok)
            self.assertIn('identity unavailable', detail['error'])
        self.control.assert_not_called()

    def test_ungoverned_model_keeps_normal_enable(self):
        agent = self.agents[0]
        agent.model = 'unpooled-model'
        agent.save(update_fields=['model'])
        self.assertTrue(set_runtime_agent_enabled(agent.agent_id, True)[0])
        self.control.assert_called_once()
        self.assertEqual(self.requests, [])

    def test_blank_row_model_uses_real_runtime_fallback_and_override(self):
        agent = self.agents[0]
        # Seed a historical daemon row. Normal save deliberately clears old
        # fallback chains; using save would erase the condition under test.
        FleetAgent.objects.filter(pk=agent.pk).update(
            model='', model_fallbacks=[{'method': 'shizuha', 'model': 'cortex/GLM-5.3-Flash'}],
        )
        self.assertTrue(set_runtime_agent_enabled(agent.agent_id, True)[1]['lease_waiting'])
        FleetAgent.objects.filter(pk=agent.pk).update(
            model_fallbacks=[{'method': 'shizuha', 'model': 'unpooled-model'}],
            model_overrides={'shizuha': 'cortex/GLM-5.3-Flash'},
        )
        self.assertTrue(set_runtime_agent_enabled(agent.agent_id, True)[1]['lease_waiting'])
        self.control.assert_not_called()

    def test_unknown_native_default_cannot_bypass_but_claude_default_is_ungoverned(self):
        agent = self.agents[0]
        agent.model = ''
        agent.save(update_fields=['model'])
        ok, detail = set_runtime_agent_enabled(agent.agent_id, True)
        self.assertFalse(ok)
        self.assertEqual(detail['reason'], 'model_unresolved')
        self.control.assert_not_called()
        agent.mcp_config = {'execution_method': 'claude_code_server'}
        agent.save(update_fields=['mcp_config'])
        self.assertTrue(set_runtime_agent_enabled(agent.agent_id, True)[0])
        self.control.assert_called_once()
