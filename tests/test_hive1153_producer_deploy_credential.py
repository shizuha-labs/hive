"""HIVE-1153 (independent Security review P1): the authoritative fleet deploy
patch MUST inject the dedicated Pulse->Hive service credential
(HIVE_CAPABILITY_EVIDENCE_AUTH_TOKEN) into the producer container, alongside the
Ed25519 signer key/kid and service account.

The checked-in manifests declare the variable, but the fleet release path builds
its patch from the live Deployment and applies THAT, not the manifests. If the
patch omits the token, a clean deploy leaves the live producer with an empty
expected token and every Pulse capability-evidence request 403s. This regression
pins the token injection into the same fleet patch that rolls the producer pods.
"""
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".forgejo" / "workflows" / "deploy.yml"
FLEET_RELEASE = ROOT / ".forgejo" / "scripts" / "ring-release-fleet.sh"


def _fleet_patch_step_run():
    run = FLEET_RELEASE.read_text()
    if (
        "kubectl patch -n shizuha-hive deploy/hive" in run
        and "codex-refresh-deploy-patch" in run
    ):
        return run
    raise AssertionError("authoritative fleet deploy-patch step not found")


class Hive1153ProducerDeployCredentialTests(unittest.TestCase):
    def test_fleet_patch_injects_dedicated_auth_token_from_client_auth_secret(self):
        compact = re.sub(r"\s+", " ", _fleet_patch_step_run())
        # The dedicated Pulse->Hive credential (the Security P1 gap) is injected
        # from the client-auth secret's `token` key into the producer container.
        self.assertIn(
            'container, "HIVE_CAPABILITY_EVIDENCE_AUTH_TOKEN", '
            '"hive-capability-evidence-client-auth", "token"',
            compact,
        )

    def test_fleet_patch_still_injects_signer_material(self):
        compact = re.sub(r"\s+", " ", _fleet_patch_step_run())
        self.assertIn(
            'container, "HIVE_CAPABILITY_EVIDENCE_PRIVATE_KEY_PEM", '
            '"hive-capability-evidence-signer", "private-key.pem"',
            compact,
        )
        self.assertIn(
            'container, "HIVE_CAPABILITY_EVIDENCE_KID", '
            '"hive-capability-evidence-signer", "kid"',
            compact,
        )

    def test_client_auth_secret_is_provisioned_by_ci(self):
        self.assertIn(
            "ensure-capability-evidence-client-auth.sh",
            WORKFLOW.read_text(),
        )

    def test_company_state_read_credential_reaches_authoritative_fleet_patch(self):
        compact = re.sub(r"\s+", " ", _fleet_patch_step_run())
        self.assertIn(
            'container, "COMPANY_STATE_READ_TOKEN", '
            '"hive-company-state-read", "token"',
            compact,
        )
        self.assertIn(
            "ensure-company-state-read-token.sh",
            WORKFLOW.read_text(),
        )


if __name__ == "__main__":
    unittest.main()
