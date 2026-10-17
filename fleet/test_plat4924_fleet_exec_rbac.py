"""PLAT-4924: fleet-scoped hive-controller pods/exec Role is least-privilege."""
from pathlib import Path

import yaml
from django.test import SimpleTestCase


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "k8s" / "hive-controller-fleet-exec-rbac.yaml"
DEPLOY = ROOT / ".forgejo" / "workflows" / "deploy.yml"


class HiveControllerFleetExecRbacTests(SimpleTestCase):
    def _docs(self):
        return list(yaml.safe_load_all(MANIFEST.read_text()))

    def test_role_is_fleet_pods_exec_create_only(self):
        role = next(d for d in self._docs() if d["kind"] == "Role")
        self.assertEqual(role["metadata"]["namespace"], "shizuha-fleet")
        self.assertEqual(role["metadata"]["name"], "hive-controller-fleet-pods-exec")
        self.assertEqual(
            role["rules"],
            [
                {
                    "apiGroups": [""],
                    "resources": ["pods/exec"],
                    "verbs": ["create", "get"],
                }
            ],
        )

    def test_rolebinding_subjects_hive_controller_only(self):
        rb = next(d for d in self._docs() if d["kind"] == "RoleBinding")
        self.assertEqual(rb["metadata"]["namespace"], "shizuha-fleet")
        self.assertEqual(
            rb["roleRef"],
            {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "Role",
                "name": "hive-controller-fleet-pods-exec",
            },
        )
        self.assertEqual(
            rb["subjects"],
            [
                {
                    "kind": "ServiceAccount",
                    "name": "hive-controller",
                    "namespace": "shizuha-hive",
                }
            ],
        )

    def test_deploy_applies_rbac_before_liveness_manifest(self):
        deploy = DEPLOY.read_text()
        rbac_at = deploy.index("k8s/hive-controller-fleet-exec-rbac.yaml")
        live_at = deploy.index(
            "Reconciling fleet-agent-liveness-reconciler (PLAT-4549 VIP probe)"
        )
        self.assertLess(rbac_at, live_at)
        # origin-ci-deployer cannot impersonate hive-controller; CI proves the
        # live Role/RoleBinding instead of `auth can-i --as`.
        self.assertNotIn("can-i create pods/exec", deploy)
        self.assertNotIn(
            "--as=system:serviceaccount:shizuha-hive:hive-controller",
            deploy,
        )
        self.assertIn('["pods/exec"] ["create","get"]', deploy)
        self.assertIn(
            "shizuha-hive/hive-controller -> hive-controller-fleet-pods-exec",
            deploy,
        )
        self.assertIn(
            "CI cannot grant pods/exec it does not hold",
            deploy,
        )
