import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".forgejo/scripts/wait-hive-connect-cutover-convergence.py"
SPEC = importlib.util.spec_from_file_location("hive725_convergence", SCRIPT)
module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(module)


def deployment():
    return {
        "metadata": {"generation": 4},
        "spec": {
            "replicas": 2,
            "selector": {"matchLabels": {"app": "hive"}},
            "template": {
                "spec": {
                    "containers": [
                        {"name": "hive", "image": "registry/hive:exact"}
                    ]
                }
            },
        },
        "status": {
            "observedGeneration": 4,
            "updatedReplicas": 2,
            "readyReplicas": 2,
            "availableReplicas": 2,
        },
    }


def pod(name, *, enforce="true", deleting=False, ready=True):
    metadata = {
        "name": name,
        "ownerReferences": [{"kind": "ReplicaSet", "name": "hive-current"}],
    }
    if deleting:
        metadata["deletionTimestamp"] = "2026-07-26T18:54:30Z"
    return {
        "metadata": metadata,
        "spec": {
            "containers": [
                {
                    "name": "hive",
                    "image": "registry/hive:exact",
                    "env": [
                        {
                            "name": "HIVE_CONNECT_ENFORCE_DEDICATED_LISTENER",
                            "value": enforce,
                        }
                    ],
                }
            ]
        },
        "status": {
            "conditions": [
                {"type": "Ready", "status": "True" if ready else "False"}
            ]
        },
    }


def endpoint_slice(*names, terminating=()):
    return {
        "items": [
            {
                "endpoints": [
                    {
                        "targetRef": {"kind": "Pod", "name": name},
                        "conditions": {
                            "ready": True,
                            "terminating": name in terminating,
                        },
                    }
                    for name in names
                ]
            }
        ]
    }


class Hive725CutoverConvergenceTests(unittest.TestCase):
    def test_native_ci_runs_cutover_observer_regressions(self):
        for name in ("deploy.yml", "premerge.yml"):
            with self.subTest(workflow=name):
                workflow = (ROOT / ".forgejo/workflows" / name).read_text()
                self.assertIn("/app/.venv/bin/python tests/test_hive725_cutover_convergence.py", workflow)

    def test_exact_enforcing_pods_and_endpoints_pass(self):
        result = module.evaluate_snapshot(
            deployment(),
            {"items": [pod("hive-a"), pod("hive-b")]},
            endpoint_slice("hive-a", "hive-b"),
        )
        self.assertEqual(result[0], True, result[1])

    def test_terminal_owned_pods_do_not_count_as_live_cutover_members(self):
        for phase in ("Succeeded", "Failed"):
            for deleting in (False, True):
                with self.subTest(phase=phase, deleting=deleting):
                    old = pod("hive-history", enforce="false", ready=False, deleting=deleting)
                    old["status"]["phase"] = phase
                    old["spec"]["containers"][0]["image"] = "registry/hive:previous"
                    result = module.evaluate_snapshot(
                        deployment(), {"items": [pod("hive-a"), pod("hive-b"), old]},
                        endpoint_slice("hive-a", "hive-b"),
                    )
                    self.assertTrue(result[0], result[1])
                    self.assertEqual(module.orphaned_image_mismatch_pods(
                        deployment(), {"items": [pod("hive-a"), pod("hive-b"), old]},
                    ), [])

    def test_terminal_pod_still_present_in_service_endpoints_is_refused(self):
        old = pod("hive-history", ready=False)
        old["status"]["phase"] = "Succeeded"
        result = module.evaluate_snapshot(
            deployment(), {"items": [pod("hive-a"), pod("hive-b"), old]},
            endpoint_slice("hive-a", "hive-b", "hive-history"),
        )
        self.assertFalse(result[0])
        self.assertIn("do not equal", result[1])

    def test_terminal_members_never_substitute_for_missing_live_replica(self):
        old = pod("hive-history")
        old["status"]["phase"] = "Failed"
        result = module.evaluate_snapshot(
            deployment(), {"items": [pod("hive-a"), old]}, endpoint_slice("hive-a"),
        )
        self.assertFalse(result[0])
        self.assertIn("pods=1 desired=2", result[1])

    def test_nonterminal_old_image_remains_blocking(self):
        for phase in ("Pending", "Running", "Unknown", None):
            with self.subTest(phase=phase):
                old = pod("hive-old")
                if phase is not None:
                    old["status"]["phase"] = phase
                old["spec"]["containers"][0]["image"] = "registry/hive:previous"
                result = module.evaluate_snapshot(
                    deployment(), {"items": [pod("hive-a"), pod("hive-b"), old]},
                    endpoint_slice("hive-a", "hive-b"),
                )
                self.assertFalse(result[0])
                self.assertIn("image does not match", result[1])
                self.assertEqual(module.orphaned_image_mismatch_pods(
                    deployment(), {"items": [pod("hive-a"), pod("hive-b"), old]},
                ), ["hive-old"])

    def test_retiring_phase_one_pod_holds_cutover(self):
        result = module.evaluate_snapshot(
            deployment(),
            {
                "items": [
                    pod("hive-a"),
                    pod("hive-b"),
                    pod("hive-old", enforce="false", deleting=True),
                ]
            },
            endpoint_slice("hive-a", "hive-b", "hive-old", terminating=("hive-old",)),
        )
        self.assertEqual(result[0], False)
        self.assertIn("terminating", result[1])

    def test_non_enforcing_current_pod_holds_cutover(self):
        result = module.evaluate_snapshot(
            deployment(),
            {"items": [pod("hive-a"), pod("hive-b", enforce="false")]},
            endpoint_slice("hive-a", "hive-b"),
        )
        self.assertEqual(result[0], False)
        self.assertIn("does not enforce", result[1])

    def test_foreign_service_endpoint_holds_cutover(self):
        result = module.evaluate_snapshot(
            deployment(),
            {"items": [pod("hive-a"), pod("hive-b")]},
            endpoint_slice("hive-a", "hive-b", "hive-old"),
        )
        self.assertEqual(result[0], False)
        self.assertIn("do not equal", result[1])

    def test_orphan_helper_flags_live_old_image_pod_when_settled(self):
        old = pod("hive-old")
        old["spec"]["containers"][0]["image"] = "registry/hive:previous"
        result = module.orphaned_image_mismatch_pods(
            deployment(),
            {"items": [pod("hive-a"), pod("hive-b"), old]},
        )
        self.assertEqual(result, ["hive-old"])

    def test_orphan_helper_ignores_terminating_old_image_pod(self):
        old = pod("hive-old", deleting=True)
        old["spec"]["containers"][0]["image"] = "registry/hive:previous"
        result = module.orphaned_image_mismatch_pods(
            deployment(), {"items": [pod("hive-a"), pod("hive-b"), old]}
        )
        self.assertEqual(result, [])

    def test_orphan_helper_never_reconciles_unsettled_rollout(self):
        old = pod("hive-old")
        old["spec"]["containers"][0]["image"] = "registry/hive:previous"
        unsettled = deployment()
        unsettled["status"]["updatedReplicas"] = 1
        result = module.orphaned_image_mismatch_pods(
            unsettled, {"items": [pod("hive-a"), old]}
        )
        self.assertEqual(result, [])

    def test_orphan_helper_ignores_current_image_and_non_rs_pods(self):
        stray = {
            "metadata": {"name": "stray", "ownerReferences": [{"kind": "Job"}]},
            "spec": {"containers": [{"name": "hive", "image": "registry/hive:previous"}]},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        }
        result = module.orphaned_image_mismatch_pods(
            deployment(), {"items": [pod("hive-a"), pod("hive-b"), stray]}
        )
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
