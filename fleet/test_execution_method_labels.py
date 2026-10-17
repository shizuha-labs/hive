"""Effective execution-method derivation for provisioner FleetAgent rows.

The serializer previously hardcoded execution_method='shizuha' and labeled the
harness from the image name — so every provisioner agent on the multi-bridge
shizuha-agent-runtime image showed "Shizuha CLI" in the Agents UI even while
its pod's entrypoint method-selected codex-bridge for gpt models
(gin/kane/sato/tora, 2026-07-14). The serializer must mirror the runtime's
method selection: explicit model_fallbacks[0].method wins, else model family.
"""
from django.test import SimpleTestCase

from .models import FleetAgent
from .serializers import FleetAgentSerializer

HARNESS_IMAGE = 'localhost:30500/shizuha-agent-runtime:harness-202607130430'


def _fields(**kwargs):
    agent = FleetAgent(agent_username=kwargs.pop('username', 'sato'), **kwargs)
    return FleetAgentSerializer(agent).data


class ExecutionMethodDerivationTests(SimpleTestCase):
    def test_gpt_model_without_fallbacks_is_codex(self):
        data = _fields(model='gpt-5.6-sol', image=HARNESS_IMAGE)
        self.assertEqual(data['execution_method'], 'codex_app_server')
        self.assertEqual(data['runtime_harness'], 'Codex')

    def test_explicit_fallback_method_wins(self):
        data = _fields(
            model='gpt-5.6-sol',
            image=HARNESS_IMAGE,
            model_fallbacks=[{'method': 'codex_app_server', 'model': 'gpt-5.6-sol'}],
        )
        self.assertEqual(data['execution_method'], 'codex_app_server')
        self.assertEqual(data['runtime_harness'], 'Codex')

    def test_cortex_model_stays_shizuha(self):
        data = _fields(model='cortex/DeepSeek-V4-Flash', image=HARNESS_IMAGE)
        self.assertEqual(data['execution_method'], 'shizuha')
        self.assertEqual(data['runtime_harness'], 'Shizuha CLI')

    def test_claude_model_is_claude_code(self):
        data = _fields(model='claude-fable-5', image=HARNESS_IMAGE)
        self.assertEqual(data['execution_method'], 'claude_code_server')
        self.assertEqual(data['runtime_harness'], 'Claude Code')

    def test_explicit_shizuha_pin_beats_model_family(self):
        # An operator may deliberately run a gpt model through the native
        # gateway; an explicit pin must not be overridden by inference.
        data = _fields(
            model='gpt-5.5',
            image=HARNESS_IMAGE,
            model_fallbacks=[{'method': 'shizuha', 'model': 'gpt-5.5'}],
        )
        self.assertEqual(data['execution_method'], 'shizuha')
        self.assertEqual(data['runtime_harness'], 'Shizuha CLI')

    def test_dedicated_image_labels_unchanged(self):
        data = _fields(model='', image='registry/openclaw:2026.6.11')
        self.assertEqual(data['runtime_harness'], 'OpenClaw')

    def test_mcp_config_execution_method_is_authoritative(self):
        # A grok_build agent runs model 'cortex/grok-4.5' with no fallbacks; the
        # runtime harness reads execution_method from mcp_config. Without honoring
        # it the model family ('cortex/...') mislabels the harness as Shizuha CLI
        # (goro/banto, 2026-07-16). mcp_config must win over family inference.
        data = _fields(
            model='cortex/grok-4.5',
            image=HARNESS_IMAGE,
            mcp_config={'execution_method': 'grok_build'},
        )
        self.assertEqual(data['execution_method'], 'grok_build')
        self.assertEqual(data['runtime_harness'], 'Grok')

    def test_mcp_config_method_beats_model_family(self):
        # Even for a gpt model, an explicit mcp_config method reflects the pod's
        # real bridge and must be surfaced rather than family-inferred codex.
        data = _fields(
            model='gpt-5.6-sol',
            image=HARNESS_IMAGE,
            mcp_config={'execution_method': 'claude_code_server'},
        )
        self.assertEqual(data['execution_method'], 'claude_code_server')


class EffectiveReasoningEffortTests(SimpleTestCase):
    """The Agents UI shows an effort for EVERY agent — explicit override or the
    per-harness default — so the setting is never blank/invisible."""

    def test_explicit_effort_is_surfaced_and_not_default(self):
        data = _fields(model='gpt-5.6-sol', image=HARNESS_IMAGE, reasoning_effort='max')
        self.assertEqual(data['reasoning_effort'], 'max')
        self.assertEqual(data['effective_reasoning_effort'], 'max')
        self.assertFalse(data['reasoning_effort_is_default'])

    def test_codex_without_explicit_effort_shows_high_default(self):
        data = _fields(model='gpt-5.6-sol', image=HARNESS_IMAGE)
        self.assertEqual(data['reasoning_effort'], '')
        self.assertEqual(data['effective_reasoning_effort'], 'high')
        self.assertTrue(data['reasoning_effort_is_default'])

    def test_claude_without_explicit_effort_shows_model_default(self):
        data = _fields(model='claude-opus-4-8', image=HARNESS_IMAGE)
        self.assertEqual(data['effective_reasoning_effort'], 'default')
        self.assertTrue(data['reasoning_effort_is_default'])

    def test_grok_without_explicit_effort_shows_high_default(self):
        data = _fields(
            model='grok-4.5', image=HARNESS_IMAGE,
            model_fallbacks=[{'method': 'grok_build', 'model': 'grok-4.5'}],
        )
        self.assertEqual(data['effective_reasoning_effort'], 'high')
        self.assertTrue(data['reasoning_effort_is_default'])

    def test_grok_via_mcp_config_shows_high_default(self):
        # Real goro/banto shape: cortex/grok-4.5 model, method only in mcp_config.
        data = _fields(
            model='cortex/grok-4.5', image=HARNESS_IMAGE,
            mcp_config={'execution_method': 'grok_build'},
        )
        self.assertEqual(data['effective_reasoning_effort'], 'high')
        self.assertTrue(data['reasoning_effort_is_default'])
