"""HIVE-1878: pin the producer and create-once credential deploy contract."""

import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


class CompanyStateDeployContractTests(unittest.TestCase):
    def test_every_hive_release_manifest_uses_the_dedicated_required_secret(self):
        paths = (
            ROOT / "k8s/shizuha-hive.yaml",
            ROOT / "k8s/hive-backend-live.yaml",
            ROOT / "k8s/rollout/hive-canary.yaml.tpl",
        )
        for path in paths:
            documents = list(yaml.safe_load_all(path.read_text()))
            deployments = [doc for doc in documents if doc and doc.get("kind") == "Deployment"]
            self.assertTrue(deployments, path)
            for deployment in deployments:
                containers = deployment["spec"]["template"]["spec"]["containers"]
                matching = [container for container in containers if container["name"] == "hive"]
                if not matching:
                    continue
                hive = matching[0]
                env = {entry["name"]: entry for entry in hive.get("env", [])}
                ref = env["COMPANY_STATE_READ_TOKEN"]["valueFrom"]["secretKeyRef"]
                self.assertEqual(ref["name"], "hive-company-state-read", path)
                self.assertEqual(ref["key"], "token", path)
                self.assertFalse(ref.get("optional", False), path)

    def test_create_once_script_never_prints_or_rotates_the_token(self):
        script = (ROOT / ".forgejo/scripts/ensure-company-state-read-token.sh").read_text()
        self.assertIn("refusing implicit rotation", script)
        self.assertIn("openssl rand -hex 32", script)
        self.assertNotIn('echo "$token"', script)
        self.assertNotIn("printf '%s' \"$token\"", script)


if __name__ == "__main__":
    unittest.main()
