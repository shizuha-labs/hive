"""POSIX-quoted kubectl merge patches are not JSON.

Live 2026-08-27: every Hive canary died in 3s on
`error decoding patch: invalid character '\\'` while repairing
hive-capability-evidence-signer.keyring.json. Fleet stayed 48 commits
behind df5b536.
"""
import json
import subprocess
import unittest


class CapabilityEvidenceKeyPatchTests(unittest.TestCase):
    def test_posix_backslash_printf_is_not_json(self):
        encoded = "eyJ0ZXN0IjogMX0="
        old = subprocess.check_output(
            [
                "sh", "-c",
                r"""printf '{\"data\":{\"keyring.json\":\"%s\"}}' "$1" """,
                "x", encoded,
            ]
        )
        with self.assertRaises(json.JSONDecodeError):
            json.loads(old)

    def test_python_dump_is_valid_merge_patch(self):
        encoded = "eyJ0ZXN0IjogMX0="
        payload = json.dumps({"data": {"keyring.json": encoded}})
        parsed = json.loads(payload)
        self.assertEqual(parsed["data"]["keyring.json"], encoded)
