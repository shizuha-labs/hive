"""HIVE-1937 regression: Hive must reject a model whose explicit provider the
execution method cannot serve, so an unsupported provider/model tuple can never
reach a pod env that exits at startup.

Incident (2026-08-14): Hive rendered agent-aya revision 3 with MODEL=xai:grok-4.5.
The runtime image exposed providers openai/google/cortex/ollama, so the agent
exited during initialization with `Error: Provider "xai" is not
configured/available` and crash-looped (ShizuhaPodCrashLooping). The source-owned
repair converged to cortex/grok-4.5. This gate keeps the invalid tuple off any
pod env regardless of the write path (FleetAgent.save() chokepoint and the
runtime-lane admission path)."""
from django.core.exceptions import ValidationError
from django.test import TestCase

from .models import FleetAgent
from .runtime_lane import RuntimeLaneError, resolve_runtime_lane


class ProviderServableValidationTests(TestCase):
    def test_xai_colon_model_allowed_on_shizuha_gateway(self):
        """CTX-5382: the shizuha gateway now serves `xai:grok-*` through the Hive
        TokenPool broker lease (same gateway binary as grok_build; lease path
        added 2026-09-26, after the 2026-08-14 Aya incident)."""
        agent = FleetAgent.objects.create(
            agent_username='scli-xai-direct-valid',
            model='xai:grok-4.7',
            mcp_config={'execution_method': 'shizuha'},
        )
        self.assertEqual(agent.model, 'xai:grok-4.7')

    def test_unserved_provider_still_rejected_on_shizuha_gateway(self):
        """The HIVE-1937 gate still rejects providers the gateway cannot serve
        (the shizuha gateway has no anthropic provider)."""
        agent = FleetAgent(
            agent_username='scli-anthropic-repro',
            model='anthropic:claude-opus-4',
            mcp_config={'execution_method': 'shizuha'},
        )
        with self.assertRaisesMessage(ValidationError, 'cannot serve'):
            agent.save()

    def test_unserved_provider_rejected_on_shizuha_update(self):
        """Changing an existing shizuha-gateway agent to an unserved provider is
        rejected; the last-known healthy model is not overwritten."""
        agent = FleetAgent.objects.create(
            agent_username='scli-update-repro',
            model='cortex/grok-4.5',
            mcp_config={'execution_method': 'shizuha'},
        )
        agent.model = 'anthropic:claude-opus-4'
        with self.assertRaisesMessage(ValidationError, 'cannot serve'):
            agent.save(update_fields=['model', 'updated_at'])
        agent.refresh_from_db()
        self.assertEqual(agent.model, 'cortex/grok-4.5')

    def test_xai_colon_model_allowed_on_grok_build(self):
        """The xAI TokenPool Grok Build lane CAN serve xai — the same model is
        valid there."""
        agent = FleetAgent.objects.create(
            agent_username='aya-grok-build-valid',
            model='xai:grok-4.5',
            mcp_config={'execution_method': 'grok_build'},
        )
        self.assertEqual(agent.model, 'xai:grok-4.5')

    def test_cortex_routed_grok_valid_on_shizuha(self):
        """The reviewed cortex/grok-4.5 route (Aya's recovery) is valid on the
        shizuha gateway."""
        agent = FleetAgent.objects.create(
            agent_username='aya-cortex-route-valid',
            model='cortex/grok-4.5',
            mcp_config={'execution_method': 'shizuha'},
        )
        self.assertEqual(agent.model, 'cortex/grok-4.5')

    def test_bare_models_are_not_gated(self):
        """Bare model names (no explicit provider prefix) resolve implicitly and
        must not be rejected by the provider gate."""
        for model in ('grok-4.5', 'gpt-5.6-sol', 'DeepSeek-V4-Flash'):
            slug = ''.join(ch for ch in model.lower() if ch.isalnum() or ch == '-')[:40]
            agent = FleetAgent.objects.create(
                agent_username=f'bare-{slug}',
                model=model,
                mcp_config={'execution_method': 'shizuha'},
            )
            self.assertEqual(agent.model, model)

    def test_openai_prefix_valid_on_shizuha_gateway(self):
        """The shizuha gateway serves the openai provider — openai/gpt-5.5 must
        not be a false positive."""
        agent = FleetAgent.objects.create(
            agent_username='openai-gateway-valid',
            model='openai/gpt-5.5',
            mcp_config={'execution_method': 'shizuha'},
        )
        self.assertEqual(agent.model, 'openai/gpt-5.5')

    def test_admission_path_rejects_unsupported_provider(self):
        """The runtime-lane admission path (daemon) also rejects a registered
        model whose explicit provider the method cannot serve (xai on the
        codex app server)."""
        with self.assertRaises(RuntimeLaneError) as raised:
            resolve_runtime_lane(
                {'model': 'gpt-5.5', 'execution_method': 'codex_app_server'},
                {'model': 'xai:grok-4.7', 'execution_method': 'codex_app_server'},
            )
        self.assertEqual(raised.exception.code, 'PROVIDER_NOT_SERVABLE')

    def test_admission_path_accepts_xai_direct_on_shizuha(self):
        """CTX-5382: xai:grok-4.7 on the shizuha gateway resolves."""
        lane = resolve_runtime_lane(
            {'model': 'cortex/GLM-5.3-Flash', 'execution_method': 'shizuha'},
            {'model': 'xai:grok-4.7', 'execution_method': 'shizuha'},
        )
        self.assertEqual(lane['model'], 'xai:grok-4.7')
        self.assertEqual(lane['execution_method'], 'shizuha')

    def test_admission_path_accepts_valid_tuples(self):
        """Valid Cortex Grok + xAI Grok Build tuples still resolve."""
        lane = resolve_runtime_lane(
            {'model': 'cortex/grok-4.5', 'execution_method': 'shizuha'},
            {'model': 'cortex/grok-4.5', 'execution_method': 'shizuha'},
        )
        self.assertEqual(lane['model'], 'cortex/grok-4.5')
        lane = resolve_runtime_lane(
            {'model': 'xai:grok-4.5', 'execution_method': 'grok_build'},
            {'model': 'xai:grok-4.5', 'execution_method': 'grok_build'},
        )
        self.assertEqual(lane['model'], 'xai:grok-4.5')
