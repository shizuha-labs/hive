"""PLAT-4000: Hive effective-capabilities HMAC signer -- cross-implementation test.

The daemon (`shizuha-beta src/platform/effective-capabilities.ts`) fails closed on
unsigned payloads. Hive must sign the payload it emits, byte-for-byte compatibly
with the daemon's `signEffectiveCapabilitiesPayload`. The `EXPECTED_HEX` below is
the GROUND TRUTH produced by running the daemon's EXACT `stableJson` + Node
`crypto.createHmac('sha256')` over the fixed PAYLOAD/SECRET -- so this test proves
the Python and TS signers agree across the trust boundary. If the daemon's
canonicalization ever changes, this vector fails loud.
"""
import json
import os
from unittest import mock

from django.test import SimpleTestCase

from fleet.capability_signing import (
    HMAC_SECRET_ENV,
    SIGNATURE_PREFIX,
    attach_effective_capabilities_signature,
    resolve_hmac_secret,
    sign_effective_capabilities_payload,
    stable_json,
)

# The exact payload signed by the daemon-algorithm generator (Node).
PAYLOAD = {
    "signature": "hmac_sha256:SHOULD_BE_STRIPPED",
    "agent": {"id": "a1", "username": "ryo", "email": "ryo@shizuha.com"},
    "source_teams": ["engineering", "devops"],
    "capabilities": [
        {"slug": "engineering", "risk_tier": "high", "version": 3},
        {"slug": "devops", "risk_tier": "high", "version": 1},
    ],
    "credential_grant_scopes": ["kubeconfig", "github:review"],
    "runtime_flags": {"trust": True, "retries": 0, "note": None},
    "display_title": 'Ryo "the Engineer"\n\t\\path/here',
    "unicode": "café ☕ 日本語",
    "computed_at": "2026-07-06T00:00:00Z",
}
SECRET = "test-secret-plat-4000"
# Ground truth from the daemon's verbatim stableJson + node crypto HMAC-SHA256.
EXPECTED_HEX = "e0474d62198314c2a5241ba5468f0b9e29d8ea2c9be8b42d5b896c6ee38fff04"


def _daemon_normalize_signature(sig):
    """Mirror the daemon's normalizeSignature: strip the hmac_sha256: prefix."""
    s = (sig or "").strip()
    return s[len(SIGNATURE_PREFIX):] if s.startswith(SIGNATURE_PREFIX) else s


class CrossImplSignatureTests(SimpleTestCase):
    def test_hex_matches_daemon_ground_truth(self):
        # THE cross-implementation assertion: identical HMAC to the daemon's TS signer.
        self.assertEqual(sign_effective_capabilities_payload(PAYLOAD, SECRET), EXPECTED_HEX)

    def test_signature_key_is_excluded_from_the_mac(self):
        without = {k: v for k, v in PAYLOAD.items() if k != "signature"}
        self.assertEqual(
            sign_effective_capabilities_payload(PAYLOAD, SECRET),
            sign_effective_capabilities_payload(without, SECRET),
        )
        tampered = dict(PAYLOAD, signature="hmac_sha256:deadbeef")
        self.assertEqual(sign_effective_capabilities_payload(tampered, SECRET), EXPECTED_HEX)

    def test_attach_produces_a_daemon_verifiable_signature(self):
        signed = attach_effective_capabilities_signature(PAYLOAD, SECRET)
        self.assertTrue(signed["signature"].startswith(SIGNATURE_PREFIX))
        self.assertEqual(signed["signature"], f"{SIGNATURE_PREFIX}{EXPECTED_HEX}")
        provided = _daemon_normalize_signature(signed["signature"])
        expected = sign_effective_capabilities_payload(signed, SECRET)
        self.assertEqual(provided, expected)

    def test_attach_is_noop_without_a_secret(self):
        os.environ.pop(HMAC_SECRET_ENV, None)
        out = attach_effective_capabilities_signature(dict(PAYLOAD))
        self.assertEqual(out, PAYLOAD)

    def test_env_secret_is_used_when_no_explicit_secret(self):
        with mock.patch.dict(os.environ, {HMAC_SECRET_ENV: SECRET}):
            self.assertEqual(resolve_hmac_secret(), SECRET)
            signed = attach_effective_capabilities_signature(
                {k: v for k, v in PAYLOAD.items() if k != "signature"}
            )
            self.assertEqual(signed["signature"], f"{SIGNATURE_PREFIX}{EXPECTED_HEX}")


class StableJsonTests(SimpleTestCase):
    def test_array_order_is_preserved(self):
        self.assertEqual(stable_json(["b", "a", "c"]), '["b","a","c"]')

    def test_object_keys_are_sorted(self):
        self.assertEqual(stable_json({"z": 1, "a": 2, "m": 3}), '{"a":2,"m":3,"z":1}')

    def test_scalars(self):
        self.assertEqual(stable_json(True), "true")
        self.assertEqual(stable_json(False), "false")
        self.assertEqual(stable_json(None), "null")
        self.assertEqual(stable_json(0), "0")
        self.assertEqual(stable_json(42), "42")
        self.assertEqual(stable_json(""), '""')

    def test_string_escaping_matches_json_stringify(self):
        s = 'a"b\\c\nd\te/f'
        self.assertEqual(stable_json(s), json.dumps(s, ensure_ascii=False))
        self.assertIn("e/f", stable_json(s))

    def test_unicode_is_not_ascii_escaped(self):
        self.assertEqual(stable_json("café ☕"), '"café ☕"')

    def test_nested_structure(self):
        self.assertEqual(
            stable_json({"b": [1, {"y": 2, "x": 3}], "a": "s"}),
            '{"a":"s","b":[1,{"x":3,"y":2}]}',
        )

    def test_float_is_rejected(self):
        with self.assertRaises(ValueError):
            stable_json({"x": 1.5})
        with self.assertRaises(ValueError):
            sign_effective_capabilities_payload({"x": 1.0}, SECRET)
