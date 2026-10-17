"""Hive fleet agents must only place as k3s — never bare_metal/host."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase

from fleet.lifecycle_fleet import drop_previous_model_occupancy
from fleet.models import FleetAgent
from fleet.runtime_lane import remap_retired_fleet_model, resolve_runtime_lane
from fleet.runtime_placement import (
    HIVE_FLEET_RUNTIME_ENVIRONMENT,
    coerce_hive_fleet_runtime_environment,
    force_mcp_config_k8s,
    validate_hive_fleet_runtime_environment,
)


class RetiredFleetModelRemapTests(SimpleTestCase):
    def test_aliases_map_to_glm(self):
        for raw in (
            'cortex/Qwen3.8-27B-Q2',
            'qwen3.8-27b-q2',
            'vllm/Qwen3.8-27B-Q2',
            'DeepSeek-V4-Flash-Vision-Metal',
            'cortex/DeepSeek-V4-Flash-Vision-Metal',
            'vllm/DeepSeek-V4-Flash-Vision-Metal',
        ):
            self.assertEqual(
                remap_retired_fleet_model(raw),
                'cortex/GLM-5.3-Flash',
            )

    def test_unrelated_model_is_unchanged(self):
        self.assertEqual(
            remap_retired_fleet_model('cortex/DeepSeek-V4-Flash'),
            'cortex/DeepSeek-V4-Flash',
        )
        self.assertEqual(remap_retired_fleet_model('cortex/GLM-5.3-Flash'), 'cortex/GLM-5.3-Flash')
        self.assertEqual(remap_retired_fleet_model('gpt-5.6-sol'), 'gpt-5.6-sol')
        self.assertEqual(remap_retired_fleet_model('cortex/grok-4.7'), 'cortex/grok-4.7')
        self.assertEqual(
            remap_retired_fleet_model('cortex/grok-voice-think-fast-2.0'),
            'cortex/grok-voice-think-fast-2.0',
        )

    def test_grok_46_canonicalizes_to_47_without_glm(self):
        cases = {
            'cortex/grok-4.6': 'cortex/grok-4.7',
            'xai:grok-4.6': 'xai:grok-4.7',
            'grok-4.6': 'grok-4.7',
            'cortex/xai/grok-4.6': 'cortex/xai/grok-4.7',
            'grok-4.6-latest': 'grok-4.7',
            'cortex/grok-4.6-latest': 'cortex/grok-4.7',
        }
        for raw, expected in cases.items():
            self.assertEqual(remap_retired_fleet_model(raw), expected)
        lane = resolve_runtime_lane(
            {'model': 'cortex/grok-4.6', 'execution_method': 'shizuha'},
            {},
        )
        self.assertEqual(lane['model'], 'cortex/grok-4.7')
        self.assertEqual(lane['lane_id'], 'xai-grok')

    def test_resolve_runtime_lane_remaps_retired_qwen_and_metal(self):
        qwen = resolve_runtime_lane(
            {'model': 'cortex/Qwen3.8-27B-Q2', 'execution_method': 'shizuha'},
            {},
        )
        self.assertEqual(qwen['model'], 'cortex/GLM-5.3-Flash')
        self.assertEqual(qwen['execution_method'], 'shizuha')
        metal = resolve_runtime_lane(
            {'model': 'DeepSeek-V4-Flash-Vision-Metal', 'execution_method': 'shizuha'},
            {},
        )
        self.assertEqual(metal['model'], 'cortex/GLM-5.3-Flash')


class RetiredFleetModelPersistTests(TestCase):
    def test_save_remaps_vision_metal_to_glm(self):
        agent = FleetAgent.objects.create(
            agent_username='nova-metal-remap',
            agent_id='nova-metal-remap',
            model='DeepSeek-V4-Flash-Vision-Metal',
            mcp_config={'execution_method': 'shizuha'},
        )
        agent.refresh_from_db()
        self.assertEqual(agent.model, 'cortex/GLM-5.3-Flash')


class DropPreviousModelOccupancyTests(SimpleTestCase):
    def test_yields_aliases_and_resets_session(self):
        agent = SimpleNamespace(
            agent_username='nova',
            agent_id='nova-id',
            model='cortex/GLM-5.3-Flash',
        )
        client = MagicMock()
        client.yield_homes.return_value = True
        with patch('fleet.model_lease.CortexLeaseClient', return_value=client), \
                patch(
                    'fleet.provisioner.reset_k8s_native_runtime_session',
                    return_value={'status': 'session_reset'},
                ) as reset:
            out = drop_previous_model_occupancy(
                agent, 'DeepSeek-V4-Flash-Vision-Metal',
            )
        self.assertTrue(out['session_reset'])
        reset.assert_called_once()
        yielded = {call.args[0] for call in client.yield_homes.call_args_list}
        self.assertIn('DeepSeek-V4-Flash-Vision-Metal', yielded)
        self.assertIn('cortex/DeepSeek-V4-Flash-Vision-Metal', yielded)

    def test_same_model_is_a_no_op(self):
        agent = SimpleNamespace(
            agent_username='nova',
            agent_id='nova-id',
            model='cortex/GLM-5.3-Flash',
        )
        with patch('fleet.model_lease.CortexLeaseClient') as cls:
            out = drop_previous_model_occupancy(agent, 'cortex/GLM-5.3-Flash')
        cls.assert_not_called()
        self.assertEqual(out['yielded'], [])
        self.assertFalse(out['session_reset'])


class RuntimePlacementHelpersTests(SimpleTestCase):
    def test_coerce_always_k8s(self):
        for value in (None, '', 'bare_metal', 'docker', 'container', 'k8s', 'host'):
            self.assertEqual(coerce_hive_fleet_runtime_environment(value), 'k8s')

    def test_validate_rejects_host_placements(self):
        with self.assertRaises(ValueError):
            validate_hive_fleet_runtime_environment('bare_metal')
        with self.assertRaises(ValueError):
            validate_hive_fleet_runtime_environment('docker')
        self.assertEqual(validate_hive_fleet_runtime_environment('k8s'), 'k8s')
        self.assertEqual(validate_hive_fleet_runtime_environment(None), 'k8s')

    def test_force_mcp_config(self):
        out = force_mcp_config_k8s({'runtime_environment': 'bare_metal', 'execution_method': 'shizuha'})
        self.assertEqual(out['runtime_environment'], HIVE_FLEET_RUNTIME_ENVIRONMENT)
        self.assertEqual(out['execution_method'], 'shizuha')


class FleetAgentSaveForcesK8sTests(TestCase):
    def test_save_coerces_bare_metal_to_k8s(self):
        agent = FleetAgent.objects.create(
            agent_username='k8s-only-probe',
            display_name='K8s Only Probe',
            status=FleetAgent.Status.STOPPED,
            mcp_config={'runtime_environment': 'bare_metal', 'execution_method': 'shizuha'},
        )
        agent.refresh_from_db()
        self.assertEqual(agent.mcp_config.get('runtime_environment'), 'k8s')

    def test_save_remaps_retired_qwen_to_glm(self):
        agent = FleetAgent.objects.create(
            agent_username='qwen-remap-probe',
            display_name='Qwen Remap Probe',
            status=FleetAgent.Status.STOPPED,
            model='cortex/Qwen3.8-27B-Q2',
            mcp_config={'runtime_environment': 'k8s', 'execution_method': 'shizuha'},
        )
        agent.refresh_from_db()
        self.assertEqual(agent.model, 'cortex/GLM-5.3-Flash')

    def test_update_fields_cannot_reintroduce_retired_qwen(self):
        agent = FleetAgent.objects.create(
            agent_username='qwen-remap-probe-2',
            display_name='Qwen Remap Probe 2',
            status=FleetAgent.Status.STOPPED,
            model='cortex/GLM-5.3-Flash',
            mcp_config={'runtime_environment': 'k8s', 'execution_method': 'shizuha'},
        )
        agent.model = 'Qwen3.8-27B-Q2'
        agent.save(update_fields=['model'])
        agent.refresh_from_db()
        self.assertEqual(agent.model, 'cortex/GLM-5.3-Flash')

    def test_update_cannot_reintroduce_bare_metal(self):
        agent = FleetAgent.objects.create(
            agent_username='k8s-only-probe-2',
            display_name='K8s Only Probe 2',
            status=FleetAgent.Status.STOPPED,
            mcp_config={'runtime_environment': 'k8s'},
        )
        agent.mcp_config = {'runtime_environment': 'bare_metal'}
        agent.save(update_fields=['mcp_config'])
        agent.refresh_from_db()
        self.assertEqual(agent.mcp_config.get('runtime_environment'), 'k8s')
