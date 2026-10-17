"""Regression tests for rt-fleet agent normalization."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from .consumers import _lifecycle_status_from_observation
from .runtime_fleet import delete_runtime_agent, normalize_runtime_agent, normalize_runtime_config_updates
from .runtime_lane import RuntimeLaneError, resolve_runtime_lane


if not settings.configured:
    settings.configure(DEFAULT_CHARSET='utf-8')


class RuntimeFleetNormalizerTests(SimpleTestCase):
    def test_daemon_stopped_observation_preserves_hive_hibernation_intent(self):
        self.assertEqual(
            _lifecycle_status_from_observation(
                current_status='hibernated',
                hibernation_reason='waiting for lease',
                observed_status='stopped',
            ),
            'hibernated',
        )
        self.assertEqual(
            _lifecycle_status_from_observation(
                current_status='stopped',
                hibernation_reason='',
                observed_status='stopped',
            ),
            'stopped',
        )
        self.assertEqual(
            _lifecycle_status_from_observation(
                current_status='running',
                hibernation_reason='',
                observed_status='stopped',
            ),
            'hibernated',
        )

    def test_hibernated_runtime_state_is_not_collapsed_into_stopped(self):
        normalized = normalize_runtime_agent({
            'id': 'agent-1',
            'username': 'ren',
            'enabled': False,
            'status': 'hibernated',
            'hibernationReason': 'waiting for DeepSeek lease',
            'hibernatedAt': '2026-07-29T03:00:00',
        })

        self.assertEqual(normalized['status'], 'hibernated')
        self.assertEqual(
            normalized['hibernation_reason'],
            'waiting for DeepSeek lease',
        )

    def test_model_only_deepseek_to_gpt_derives_canonical_codex_lane(self):
        lane = resolve_runtime_lane(
            {'model': 'cortex/DeepSeek-V4-Flash', 'execution_method': 'shizuha'},
            {'model': 'gpt-5.6-sol'},
        )
        self.assertEqual(lane['model'], 'gpt-5.6-sol')
        self.assertEqual(lane['execution_method'], 'codex_app_server')
        self.assertEqual(lane['derivations'][0]['field'], 'execution_method')

    def test_model_only_gpt_to_deepseek_derives_canonical_shizuha_lane(self):
        lane = resolve_runtime_lane(
            {'model': 'gpt-5.6-sol', 'execution_method': 'codex_app_server'},
            {'model': 'cortex/DeepSeek-V4-Flash'},
        )
        self.assertEqual(lane['execution_method'], 'shizuha')

    def test_shizuha_cli_can_run_gpt6_astra(self):
        lane = resolve_runtime_lane(
            {'model': 'cortex/GLM-5.3-Flash', 'execution_method': 'shizuha'},
            {'model': 'gpt-6-astra', 'execution_method': 'shizuha', 'reasoning_effort': 'medium'},
        )
        self.assertEqual(lane['model'], 'gpt-6-astra')
        self.assertEqual(lane['execution_method'], 'shizuha')
        self.assertEqual(lane['lane_id'], 'openai-astra')

    def test_codex_lane_still_runs_gpt6_astra(self):
        lane = resolve_runtime_lane(
            {},
            {'model': 'gpt-6-astra', 'execution_method': 'codex_app_server'},
        )
        self.assertEqual(lane['execution_method'], 'codex_app_server')
        self.assertEqual(lane['lane_id'], 'openai-astra')

    def test_explicit_impossible_tuple_fails_closed(self):
        with self.assertRaises(RuntimeLaneError) as raised:
            resolve_runtime_lane(
                {'model': 'cortex/DeepSeek-V4-Flash', 'execution_method': 'shizuha'},
                {'model': 'gpt-5.6-sol', 'execution_method': 'shizuha'},
            )
        self.assertEqual(raised.exception.code, 'INCOMPATIBLE_RUNTIME_LANE')

    def test_grok_voice_omni_is_shizuha_only(self):
        lane = resolve_runtime_lane(
            {},
            {'model': 'cortex/grok-voice-think-fast-2.0', 'execution_method': 'shizuha'},
        )
        self.assertEqual(lane['lane_id'], 'xai-grok-voice')
        self.assertEqual(lane['execution_method'], 'shizuha')
        self.assertEqual(lane['model'], 'cortex/grok-voice-think-fast-2.0')
        with self.assertRaises(RuntimeLaneError) as raised:
            resolve_runtime_lane(
                {},
                {'model': 'cortex/grok-voice-think-fast-2.0', 'execution_method': 'grok_build'},
            )
        self.assertEqual(raised.exception.code, 'INCOMPATIBLE_RUNTIME_LANE')

    def test_registry_covers_every_published_method_model_option(self):
        from .views import (
            CLAUDE_MODEL_OPTIONS,
            CODEX_MODEL_OPTIONS,
            GROK_BUILD_MODEL_OPTIONS,
            MANAGED_FLEET_CORTEX_MODELS,
        )

        catalogs = {
            'claude_code_server': CLAUDE_MODEL_OPTIONS,
            'codex_app_server': CODEX_MODEL_OPTIONS,
            'openclaw_app_server': CODEX_MODEL_OPTIONS,
            'grok_build': GROK_BUILD_MODEL_OPTIONS,
            'shizuha': MANAGED_FLEET_CORTEX_MODELS,
        }
        for method, models in catalogs.items():
            for model in models:
                with self.subTest(method=method, model=model['id']):
                    lane = resolve_runtime_lane({}, {
                        'model': model['id'],
                        'execution_method': method,
                    })
                    self.assertEqual(lane['execution_method'], method)
                    self.assertTrue(lane['runtime_release_id'])

    def test_ui_method_catalog_is_derived_from_pinned_runtime_manifest(self):
        from .runtime_lane import (
            RUNTIME_CAPABILITY_MANIFEST, runtime_registry_method_models,
        )

        catalogs = runtime_registry_method_models([
            {'id': 'cortex/DeepSeek-V4-Flash'},
        ])
        self.assertEqual(set(catalogs), set(RUNTIME_CAPABILITY_MANIFEST['methods']))
        self.assertIn(
            'gpt-5.6-sol',
            {item['id'] for item in catalogs['codex_app_server']},
        )

    def test_running_agent_suppresses_stale_exit_message(self):
        normalized = normalize_runtime_agent({
            'id': 'agent-1',
            'username': 'jun',
            'enabled': True,
            'status': 'running',
            'executionMethod': 'codex_app_server',
            'error': 'Exited with code 137',
            'lastError': 'Exited with code 137',
        })

        self.assertEqual(normalized['status'], 'running')
        self.assertEqual(normalized['status_message'], '')

    def test_failed_agent_preserves_exit_message(self):
        normalized = normalize_runtime_agent({
            'id': 'agent-1',
            'username': 'jun',
            'enabled': True,
            'status': 'error',
            'executionMethod': 'codex_app_server',
            'error': 'Exited with code 137',
            'lastError': 'Exited with code 137',
        })

        self.assertEqual(normalized['status'], 'failed')
        self.assertEqual(normalized['status_message'], 'Exited with code 137')

    def test_kubectl_auth_dump_is_not_an_agent_fault(self):
        dump = (
            'k8s reconcile start failed: Command failed: /usr/local/bin/kubectl '
            'get -n shizuha-fleet deployment/agent-san -o json\n'
            "E0814 06:41:26.460865 2132538 memcache.go:265] couldn't get current "
            'server API group list: the server has asked for the client to provide '
            'credentials\n'
            'error: You must be logged in to the server (the server has asked for '
            'the client to provide credentials)'
        )
        normalized = normalize_runtime_agent({
            'id': 'agent-san-id',
            'username': 'san',
            'enabled': True,
            'status': 'error',
            'executionMethod': 'shizuha',
            'error': dump,
            'lastError': dump,
        })
        self.assertEqual(normalized['status'], 'running')
        self.assertEqual(normalized['status_message'], '')

    def test_stale_fallback_only_update_cannot_derive_primary_model(self):
        """Disabled legacy maps cannot overwrite Hive's explicit primary."""
        out = normalize_runtime_config_updates({
            'model_fallbacks': [
                {'method': 'grok_build', 'model': 'cortex/grok-4.5', 'reasoningEffort': 'high'},
            ],
            'model_overrides': {'grok_build': 'cortex/grok-4.5'},
        })
        self.assertNotIn('model', out)
        self.assertNotIn('execution_method', out)
        self.assertEqual(out['model_fallbacks'], [])
        self.assertEqual(out['model_overrides'], {})

    def test_config_updates_accept_simple_primary_model_without_fallback_chain(self):
        """Normal Hive edits are one execution method + model, not a chain."""
        out = normalize_runtime_config_updates({
            'execution_method': 'shizuha',
            'model': 'cortex/DeepSeek-V4-Flash',
            'reasoning_effort': 'high',
        })
        self.assertEqual(out['model'], 'cortex/DeepSeek-V4-Flash')
        self.assertEqual(out['execution_method'], 'shizuha')
        self.assertEqual(out['model_fallbacks'], [])
        self.assertEqual(out['model_overrides'], {})

    def test_reasoning_edit_does_not_clear_platform_managed_fallback(self):
        out = normalize_runtime_config_updates({'reasoning_effort': 'medium'})
        self.assertEqual(out, {
            'reasoning_effort': 'medium',
            'model_overrides': {},
        })

    @override_settings(
        RUNTIME_FLEET_CONTROL_URL='http://runtime-control:8016',
        RUNTIME_FLEET_CONTROL_SECRET='secret',
    )
    @patch('fleet.runtime_fleet.requests.delete')
    def test_delete_runtime_agent_removes_daemon_desired_state(self, request_delete):
        request_delete.return_value = MagicMock(status_code=200, text='{"ok":true}')
        agent = SimpleNamespace(
            id='hive-row-id', agent_id='runtime-agent-id',
            agent_username='shizuha-3', managing_daemon=None,
        )

        ok, detail = delete_runtime_agent(agent)

        self.assertTrue(ok, detail)
        request_delete.assert_called_once_with(
            'http://runtime-control:8016/v1/agents/runtime-agent-id',
            headers={'X-Fleet-Control-Secret': 'secret'},
            json={},
            timeout=3.0,
        )


class UnregisteredCurrentModelTests(SimpleTestCase):
    """An agent left on a model the registry no longer lists must stay movable.

    Registry 2026-07-25.1 dropped `gpt-5.6-terra` while three live agents still
    ran it. `admit_runtime_lane_update` resolves the CURRENT model to build a
    rollback anchor, so that raise stranded those agents: every attempt to move
    them to a valid model failed with UNREGISTERED_MODEL naming the OLD model,
    which reads like the caller's request was bad.
    """

    def test_retired_model_is_not_resolvable(self):
        # Guards the premise: if this ever starts resolving, the trap below is moot.
        with self.assertRaises(RuntimeLaneError) as ctx:
            resolve_runtime_lane({}, {'model': 'gpt-5.6-terra'})
        self.assertEqual(ctx.exception.code, 'UNREGISTERED_MODEL')

    def test_patch_to_valid_model_resolves_from_unregistered_anchor(self):
        # The recovery path admit_runtime_lane_update now takes: anchor the
        # rollback lane on the REQUESTED model when the current one is retired.
        lane = resolve_runtime_lane({}, {'model': 'cortex/DeepSeek-V4-Flash'})
        self.assertEqual(lane['model'], 'cortex/DeepSeek-V4-Flash')
        merged = resolve_runtime_lane(lane, {'model': 'cortex/DeepSeek-V4-Flash'})
        self.assertEqual(merged['model'], 'cortex/DeepSeek-V4-Flash')

    def test_unregistered_current_and_no_patch_model_still_raises(self):
        # Without a valid target there is nothing to recover to — must not
        # silently invent a lane.
        with self.assertRaises(RuntimeLaneError):
            resolve_runtime_lane(
                {'model': 'gpt-5.6-terra', 'execution_method': 'shizuha'},
                {'model': 'gpt-5.6-terra'},
            )
