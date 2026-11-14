"""HIVE-3017: Gemini models on antigravity_server need only provider.api:google.

antigravity_server serves both Claude and Gemini, and its method-level
credential_capabilities list both providers. Admission used to require every
one of them, so a Gemini seat was refused with
CREDENTIAL_CAPABILITY_UNAVAILABLE provider.api:anthropic. The google-antigravity
family now narrows the requirement to provider.api:google; Claude on the same
method is unchanged.
"""
from django.test import SimpleTestCase

from .runtime_lane import _family_for, resolve_runtime_lane


class GeminiCredentialCapabilitiesTest(SimpleTestCase):
    def test_gemini_on_antigravity_requires_google_only(self):
        for model in ("gemini-4-hydrogen-medium", "gemini-4-hydrogen", "gemini-2.5-pro"):
            lane = resolve_runtime_lane({}, {"model": model, "execution_method": "antigravity_server"})
            self.assertEqual(lane["credential_requirements"], ["provider.api:google"], model)
            self.assertEqual(lane["required_capabilities"], ["provider.api:google"], model)

    def test_claude_on_antigravity_unchanged(self):
        lane = resolve_runtime_lane({}, {"model": "claude-opus-4-6", "execution_method": "antigravity_server"})
        self.assertEqual(lane["credential_requirements"], ["provider.api:google", "provider.api:anthropic"])

    def test_families_without_override_keep_method_list(self):
        self.assertIsNone(_family_for("claude-opus-4-6").credential_capabilities)
        self.assertIsNone(_family_for("gpt-5.6-sol").credential_capabilities)
        lane = resolve_runtime_lane({}, {"model": "claude-opus-4-6", "execution_method": "claude_code_server"})
        self.assertEqual(lane["credential_requirements"], ["provider.api:anthropic"])

    def test_family_override_can_only_narrow(self):
        self.assertEqual(_family_for("gemini-4-hydrogen-medium").credential_capabilities, ("provider.api:google",))
