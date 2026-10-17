"""HIVE-2579 (run 3071): the cutover self-heal must clear the fence.

Run 3071 proved the RBAC leg (pods/delete) but the fenced kubectl still
rejected the orphan delete: pods were not on the protected-target allowlist,
raw_delete had no pod group path, and an orphan pod carries no fence
generation so the strict ownership test would hold fail-closed forever.
"""
import importlib.machinery
import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".forgejo" / "scripts"


def load_module(name: str, path: Path, **mock_modules):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    # Register before exec: dataclass resolution reads sys.modules[cls.__module__].
    with mock.patch.dict(sys.modules, {name: module, **mock_modules}):
        loader.exec_module(module)
    return module


def load_ring_executor():
    """Load ring_executor with the real ring_fence (Holder is exercised)."""
    fence = load_module("ring_fence_hive2579", SCRIPTS / "ring_fence.py")
    return load_module(
        "ring_executor_hive2579", SCRIPTS / "ring_executor.py",
        ring_fence=fence,
    )


def make_holder(ring_executor):
    return ring_executor.Holder(
        repo="shizuha-labs/hive",
        workflow="deploy-hive",
        run_id="47100",
        run_number="3071",
        job="deploy-fleet",
        ring="fleet",
        release_sha="a" * 40,
        restored_sha="b" * 40,
    )


class OrphanPodAllowlistTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ring_executor = load_ring_executor()

    def test_hive_rs_pod_shape_is_allowlisted(self):
        holder = make_holder(self.ring_executor)
        for name in ("hive-6469d84f54-6lltt", "hive-67785b7d48-p8rvp"):
            self.assertTrue(
                self.ring_executor.target_is_protected(holder, "shizuha-hive", "pod", name),
                name,
            )

    def test_non_orphan_pods_stay_outside_the_allowlist(self):
        holder = make_holder(self.ring_executor)
        for ns, kind, name in (
            ("shizuha-hive", "pod", "hive-canary-6469d84f54-6lltt"),
            ("shizuha-hive", "pod", "hive-codex-refresh-6469d84f54-6lltt"),
            ("shizuha-hive", "pod", "hive-notahexhash-6lltt"),
            ("shizuha-hive", "pod", "hive-6469d84f54"),
            ("origin", "pod", "hive-6469d84f54-6lltt"),
            ("shizuha-hive", "deployment", "hive-6469d84f54-6lltt"),
        ):
            self.assertFalse(
                self.ring_executor.target_is_protected(holder, ns, kind, name),
                f"{ns}/{kind}/{name}",
            )


class AdoptGenerationlessPodTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kubectl = load_module(
            "kubectl_hive2579", SCRIPTS / "kubectl",
            ring_fence=mock.Mock(FenceError=RuntimeError, Conflict=RuntimeError),
            ring_executor=mock.Mock(target_is_protected=lambda *a: True),
        )

    def _fence(self, generation="gen-1", token=7):
        fence = mock.Mock()
        fence.holder.generation = generation
        fence.token = token
        return fence

    def test_unmarked_pod_is_stamped_under_resourceversion_cas(self):
        fence = self._fence()
        value = {"metadata": {"name": "hive-6469d84f54-6lltt", "resourceVersion": "12345"}}
        marked = {
            "metadata": {
                "name": "hive-6469d84f54-6lltt",
                "resourceVersion": "12346",
                "annotations": {
                    "shizuha.io/ring-generation": fence.holder.generation,
                    "shizuha.io/fence-token": str(fence.token),
                },
            }
        }
        fence.api.json_patch.return_value = marked

        self.kubectl.adopt_generationless_pod(
            fence, "shizuha-hive", "pod", "hive-6469d84f54-6lltt", value
        )

        ops = fence.api.json_patch.call_args[0][3]
        self.assertEqual(ops[0], {"op": "test", "path": "/metadata/resourceVersion", "value": "12345"})
        self.assertEqual(
            ops[1],
            {"op": "add", "path": "/metadata/annotations", "value": {
                "shizuha.io/ring-generation": fence.holder.generation,
                "shizuha.io/fence-token": str(fence.token),
            }},
        )
        self.assertEqual(value, marked)
        fence.validate.assert_called_once_with()

    def test_pod_with_existing_annotations_gets_per_key_adds(self):
        fence = self._fence()
        value = {
            "metadata": {
                "name": "hive-6469d84f54-6lltt",
                "resourceVersion": "99",
                "annotations": {"some/other": "annotation"},
            }
        }
        fence.api.json_patch.return_value = {
            "metadata": {
                "name": "hive-6469d84f54-6lltt",
                "resourceVersion": "100",
                "annotations": {
                    "some/other": "annotation",
                    "shizuha.io/ring-generation": fence.holder.generation,
                    "shizuha.io/fence-token": str(fence.token),
                },
            }
        }

        self.kubectl.adopt_generationless_pod(
            fence, "shizuha-hive", "pod", "hive-6469d84f54-6lltt", value
        )

        ops = fence.api.json_patch.call_args[0][3]
        self.assertEqual(
            ops[1:],
            [
                {"op": "add", "path": "/metadata/annotations/shizuha.io~1ring-generation", "value": fence.holder.generation},
                {"op": "add", "path": "/metadata/annotations/shizuha.io~1fence-token", "value": str(fence.token)},
            ],
        )

    def test_foreign_generation_pod_is_left_strict(self):
        fence = self._fence()
        value = {
            "metadata": {
                "name": "hive-6469d84f54-6lltt",
                "annotations": {"shizuha.io/ring-generation": "gen-0"},
            }
        }
        self.kubectl.adopt_generationless_pod(
            fence, "shizuha-hive", "pod", "hive-6469d84f54-6lltt", value
        )
        fence.api.json_patch.assert_not_called()

    def test_non_pod_kinds_keep_strict_semantics(self):
        fence = self._fence()
        value = {"metadata": {"name": "hive", "resourceVersion": "1"}}
        self.kubectl.adopt_generationless_pod(fence, "shizuha-hive", "deployment", "hive", value)
        fence.api.json_patch.assert_not_called()


class DeleteMechanicsTests(unittest.TestCase):
    def test_raw_delete_has_the_pod_group_path(self):
        source = (SCRIPTS / "kubectl").read_text()
        self.assertIn('"pod": ("/api/v1", "pods"),', source)


class ExecutorRbacTests(unittest.TestCase):
    def test_executor_role_grants_pods_patch_and_delete(self):
        documents = [
            item for item in yaml.safe_load_all(
                (ROOT / "k8s" / "hive-ring-executor-rbac.yaml").read_text()
            ) if item
        ]
        roles = [
            item for item in documents
            if item.get("kind") == "Role"
            and item["metadata"]["name"] == "hive-ring-execute"
            and item["metadata"]["namespace"] == "shizuha-hive"
        ]
        self.assertEqual(len(roles), 1)
        verbs: set[str] = set()
        for rule in roles[0]["rules"]:
            if "pods" in rule.get("resources", []):
                verbs.update(rule.get("verbs", []))
        self.assertLessEqual({"delete", "patch"}, verbs)


class GenericK3s422IsConflict(unittest.TestCase):
    def test_commit_422_without_cas_words_is_conflict(self):
        fence = load_module("ring_fence_plat10910", SCRIPTS / "ring_fence.py")
        api = fence.Kubectl("kubectl")
        stderr = (
            "The request is invalid: the server rejected our request "
            "due to an error in our request\n"
        )
        completed = subprocess.CompletedProcess(["kubectl"], 1, "", stderr)
        with mock.patch.object(fence.subprocess, "run", return_value=completed):
            with self.assertRaises(fence.Conflict):
                api.run(["-n", "origin", "patch", "lease", "hive-ring-fleet"])

    def test_other_invalid_request_stays_fence_error(self):
        fence = load_module("ring_fence_plat10910b", SCRIPTS / "ring_fence.py")
        api = fence.Kubectl("kubectl")
        stderr = "The request is invalid: spec.holderIdentity: Invalid value\n"
        completed = subprocess.CompletedProcess(["kubectl"], 1, "", stderr)
        with mock.patch.object(fence.subprocess, "run", return_value=completed):
            with self.assertRaises(fence.FenceError) as caught:
                api.run(["-n", "origin", "patch", "lease", "hive-ring-fleet"])
        self.assertNotIsInstance(caught.exception, fence.Conflict)


if __name__ == "__main__":
    unittest.main()
