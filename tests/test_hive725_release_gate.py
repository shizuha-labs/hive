import importlib.util
import json
import subprocess
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".forgejo" / "scripts"
WORKFLOW = (ROOT / ".forgejo" / "workflows" / "deploy.yml").read_text()
FAILURE_REPORTER = (
    ROOT / ".forgejo" / "scripts" / "record-rollout-failure.sh"
).read_text()


def load_script(name):
    path = SCRIPTS / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class IdentityReconcileLogsAreBestEffortTests(unittest.TestCase):
    def test_completed_identity_job_log_fetch_does_not_fail_the_ring(self):
        source = (SCRIPTS / "ring-release-fleet.sh").read_text()
        lines = [
            line
            for line in source.splitlines()
            if 'logs "job/${IDENTITY_RECONCILE_JOB}"' in line
        ]
        self.assertGreaterEqual(len(lines), 1, source)
        for line in lines:
            self.assertTrue(
                line.rstrip().endswith("|| true")
                or "if ! " in line,
                msg=f"identity job logs must be best-effort, got: {line}",
            )
        self.assertIn(
            "do not treat as release regression",
            source,
        )
        neg_lines = [
            line
            for line in source.splitlines()
            if 'logs "job/${NEG_JOB}"' in line
        ]
        self.assertGreaterEqual(len(neg_lines), 1, source)
        for line in neg_lines:
            self.assertTrue(
                line.rstrip().endswith("|| true")
                or "if ! " in line,
                msg=f"NEG_JOB logs must be best-effort, got: {line}",
            )


class ReadyPodSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.selector = load_script("select-ready-pod.py")

    @staticmethod
    def pod(name, *, phase="Running", ready=True, container="hive", deleting=False, node="gx10-1"):
        metadata = {"name": name}
        if deleting:
            metadata["deletionTimestamp"] = "2026-07-26T00:00:00Z"
        return {
            "metadata": metadata,
            "spec": {"nodeName": node},
            "status": {
                "phase": phase,
                "conditions": [
                    {"type": "Ready", "status": "True" if ready else "False"}
                ],
                "containerStatuses": [
                    {"name": container, "ready": ready},
                ],
            },
        }

    def test_excludes_completed_failed_unready_and_wrong_container_pods(self):
        payload = {
            "items": [
                self.pod("cron-complete", phase="Succeeded"),
                self.pod("cron-failed", phase="Failed"),
                self.pod("hive-unready", ready=False),
                self.pod("controller-running", container="controller"),
                self.pod("hive-deleting", deleting=True),
                self.pod("hive-ready-b"),
                self.pod("hive-ready-a"),
            ]
        }
        self.assertEqual(
            self.selector.select_ready_pod(payload, "hive"),
            "hive-ready-a",
        )

    def test_prefers_non_workstation_nodes_for_exec(self):
        payload = {
            "items": [
                self.pod("hive-on-i9", node="i9-ws"),
                self.pod("hive-on-gx", node="gx10-3"),
            ]
        }
        self.assertEqual(
            self.selector.select_ready_pod(payload, "hive"),
            "hive-on-gx",
        )

    def test_falls_back_to_workstation_when_it_is_the_only_ready_pod(self):
        payload = {"items": [self.pod("hive-only-i9", node="i9-ws")]}
        self.assertEqual(
            self.selector.select_ready_pod(payload, "hive"),
            "hive-only-i9",
        )

    def test_fails_closed_without_a_ready_named_container(self):
        with self.assertRaisesRegex(ValueError, "no Running Ready pod"):
            self.selector.select_ready_pod(
                {"items": [self.pod("failed", phase="Failed")]},
                "hive",
            )


class RollbackSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sanitizer = load_script("sanitize-rollback-resource.py")

    def test_removes_server_owned_identity_and_status_before_apply(self):
        resource = {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {
                "name": "allow-node-healthchecks",
                "namespace": "shizuha-hive",
                "resourceVersion": "123",
                "uid": "uuid",
                "generation": 7,
                "managedFields": [{"manager": "other"}],
                "creationTimestamp": "2026-07-26T00:00:00Z",
                "labels": {"owned": "true"},
            },
            "spec": {"podSelector": {}},
            "status": {"ignored": True},
        }
        sanitized = self.sanitizer.sanitize(resource)
        self.assertEqual(sanitized["metadata"]["labels"], {"owned": "true"})
        for field in self.sanitizer.SERVER_OWNED_METADATA:
            self.assertNotIn(field, sanitized["metadata"])
        self.assertNotIn("status", sanitized)

    def test_cli_rejects_non_resource_input(self):
        result = subprocess.run(
            ["python3", SCRIPTS / "sanitize-rollback-resource.py"],
            input=json.dumps([]),
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("snapshot must be a JSON object", result.stderr)


class WorkflowContractTests(unittest.TestCase):
    def test_exact_sha_reruns_reuse_source_bound_immutable_release_index(self):
        build = WORKFLOW.split(
            "- name: Multi-arch build + guarded deploy + verify", 1
        )[1].split(
            "# ── R0: isolated staging smoke", 1
        )[0]
        immutable_probe = build.index("verify_release_index || VERIFY_RC=$?")
        builder_cleanup = build.index(
            "# Forgejo cancellation cannot stop external Kubernetes Jobs"
        )
        self.assertLess(immutable_probe, builder_cleanup)
        self.assertIn("RELEASE_INDEX_ABSENT", build)
        self.assertIn('10)\n              echo "Release index', build)
        self.assertIn(
            'echo "Reusing immutable multi-arch release ${TAG}; no rebuild"',
            build,
        )
        self.assertEqual(
            build.count(
                "--label=org.opencontainers.image.revision=${GITHUB_SHA}"
            ),
            1,
        )
        self.assertIn(
            'revision != expected_revision',
            build,
        )
        self.assertIn("for attempt in range(1, 7):", build)
        self.assertIn(
            "except (urllib.error.URLError, TimeoutError) as exc:", build,
        )
        self.assertIn("except urllib.error.HTTPError:", build)
        self.assertIn("registry transport retry", build)
        self.assertEqual(build.count("verify_release_index"), 3)

    def test_listener_enforcement_mutates_only_the_hive_container(self):
        scoped = (
            "kubectl -n shizuha-hive set env deploy/hive --containers=hive"
        )
        self.assertEqual(WORKFLOW.count(scoped), 2)
        self.assertNotIn(
            "kubectl -n shizuha-hive set env deploy/hive \\\n",
            WORKFLOW,
        )

    def test_hive725_exec_targets_use_ready_container_aware_selection(self):
        self.assertIn("h725_ready_pod()", WORKFLOW)
        self.assertEqual(WORKFLOW.count("h725_ready_pod shizuha "), 2)
        self.assertEqual(
            WORKFLOW.count("h725_ready_pod shizuha-hive app=hive hive"),
            1,
        )
        self.assertNotIn(
            "-o jsonpath='{.items[0].metadata.name}'",
            WORKFLOW,
        )

    def test_boundary_snapshot_is_sanitized_and_restore_owns_conflicts(self):
        self.assertIn(
            "python3 .forgejo/scripts/sanitize-rollback-resource.py",
            WORKFLOW,
        )
        self.assertIn(
            "kubectl apply --server-side --force-conflicts",
            WORKFLOW,
        )
        self.assertIn(
            "--field-manager=hive-release-ring-rollback",
            WORKFLOW,
        )

    def test_production_dispatch_probe_retries_with_failure_diagnostics(self):
        probe = WORKFLOW.split(
            "# Exercise Connect's production dispatcher", 1
        )[1].split(
            "printf '%s' \"$H725_FIXTURE\" | kubectl -n shizuha exec -i "
            "\"$CONNECT_POD\" -- python -c", 1
        )[0]
        self.assertIn("max_attempts = 6", probe)
        self.assertIn("for attempt in range(1, max_attempts + 1):", probe)
        self.assertIn("failures = list(", probe)
        self.assertIn('diagnostics = failures or ["no diagnostic"]', probe)
        self.assertIn("transaction.set_rollback(True)", probe)
        self.assertIn("delay = min(2 ** attempt, 8)", probe)
        self.assertIn("if attempt < max_attempts:", probe)
        self.assertIn("time.sleep(delay)", probe)
        self.assertIn("2>&1 | tee -a /tmp/fleet-gate.log", probe)
        self.assertIn(
            "production Connect dispatcher/authority probe failed ",
            probe,
        )
        source = probe.split(
            "-- python manage.py shell -c '\n", 1
        )[1].split(
            "\n          ' 2>&1 | tee -a /tmp/fleet-gate.log", 1
        )[0]
        rendered = subprocess.run(
            ["bash", "-c", "printf %s '" + source + "'"],
            text=True,
            capture_output=True,
        )
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self.assertEqual(rendered.stdout, source)
        compile(
            textwrap.dedent(rendered.stdout),
            "<production-connect-probe>",
            "exec",
        )

    def test_listener_cutover_and_phase_local_rollback_allow_serial_soaks(self):
        finalize = WORKFLOW.split(
            ": >/tmp/h725-phase1-proven", 1
        )[1].split(
            'printf \'%s\' "$H725_FIXTURE"', 1
        )[0]
        self.assertIn(
            "kubectl -n shizuha-hive rollout status deploy/hive "
            "--timeout=1800s",
            finalize,
        )

        rollback = WORKFLOW.split(
            "# Phase-local rollback:", 1
        )[1].split(
            'evidence="phase=hive725-phase2', 1
        )[0]
        self.assertIn(
            "kubectl -n shizuha-hive rollout status deploy/hive "
            "--timeout=1800s",
            rollback,
        )

    def test_fleet_release_does_not_require_running_claws_after_retirement(self):
        """HIVE-2007/HIVE-2029: zero running Claws must not roll fleet back."""
        script = (ROOT / ".forgejo" / "scripts" / "ring-release-fleet.sh").read_text()
        self.assertIn("if not seen:", script)
        self.assertIn("Claw retired HIVE-2007", script)
        self.assertIn("print(\\\"SKIP\\\")", script)
        self.assertNotIn(
            "missing running dashboard target(s)",
            script,
        )
        self.assertIn(
            "No Hive-managed claw namespaces remain (Claw retired HIVE-2007)",
            script,
        )
        self.assertNotIn(
            "::error::no Hive-managed claw namespaces found for dashboard NetworkPolicy verification",
            script,
        )

    def test_rollback_reporter_retries_read_only_pulse_project_lookup(self):
        projects_lookup = FAILURE_REPORTER.split(
            'projects_response="$(', 1
        )[1].split(')" || {', 1)[0]
        self.assertIn("--retry 4", projects_lookup)
        self.assertIn("--retry-all-errors", projects_lookup)
        self.assertIn("--retry-max-time 75", projects_lookup)


if __name__ == "__main__":
    unittest.main()
