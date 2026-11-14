"""HIVE-3017: gemini-4-hydrogen (+ -low/-medium/-xhigh) admit on antigravity_server.

agy 1.5.8 (shipped in the runtime image) serves these ids; the pinned
runtime-lane registry must resolve them to the google-antigravity lane and
reject them on every other execution method. Additive only: registry_version is
deliberately NOT bumped, because it is part of lane_digest() and a bump would
change desired_config_hash for every existing agent.
"""
from django.test import SimpleTestCase

from .runtime_lane import (
    RUNTIME_CAPABILITY_MANIFEST,
    RuntimeLaneError,
    _family_for,
    resolve_runtime_lane,
)

HYDROGEN = (
    "gemini-4-hydrogen",
    "gemini-4-hydrogen-low",
    "gemini-4-hydrogen-medium",
    "gemini-4-hydrogen-xhigh",
)


class GeminiHydrogenRegistryTests(SimpleTestCase):
    def test_manifest_lists_hydrogen_on_antigravity_server(self):
        models = RUNTIME_CAPABILITY_MANIFEST["methods"]["antigravity_server"]["models"]
        for model in HYDROGEN:
            self.assertIn(model, models)

    def test_hydrogen_resolves_to_google_antigravity_lane(self):
        for model in HYDROGEN:
            family = _family_for(model)
            self.assertEqual(family.lane_id, "google-antigravity")
            self.assertEqual(family.canonical_method, "antigravity_server")

    def test_resolve_runtime_lane_on_antigravity_server(self):
        for model in HYDROGEN:
            lane = resolve_runtime_lane(
                {}, {"model": model, "execution_method": "antigravity_server"},
            )
            self.assertEqual(lane["lane_id"], "google-antigravity")
            self.assertEqual(lane["execution_method"], "antigravity_server")
            self.assertEqual(lane["provider"], "google")
            self.assertIn("provider.api:google", lane["credential_requirements"])

    def test_model_only_patch_derives_canonical_method(self):
        lane = resolve_runtime_lane(
            {"model": "gpt-5.6-sol", "execution_method": "codex_app_server"},
            {"model": "gemini-4-hydrogen"},
        )
        self.assertEqual(lane["execution_method"], "antigravity_server")

    def test_hydrogen_rejected_on_other_methods(self):
        for method in ("codex_app_server", "claude_code_server", "grok_build", "shizuha"):
            with self.subTest(method=method):
                with self.assertRaises(RuntimeLaneError):
                    resolve_runtime_lane(
                        {}, {"model": "gemini-4-hydrogen", "execution_method": method},
                    )

    def test_unknown_hydrogen_variant_still_unregistered(self):
        with self.assertRaises(RuntimeLaneError) as ctx:
            _family_for("gemini-4-hydrogen-ultra")
        self.assertEqual(ctx.exception.code, "UNREGISTERED_MODEL")
