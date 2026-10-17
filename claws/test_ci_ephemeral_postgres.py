"""Discovered with the full backend suite; render real Jobs without Kubernetes."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
POSTGRES_ARGS = ["postgres", "-c", "fsync=off", "-c", "synchronous_commit=off",
                 "-c", "full_page_writes=off"]


def rendered_test_job(workflow_path):
    workflow = yaml.safe_load(workflow_path.read_text())
    steps = [step for job in workflow["jobs"].values() for step in job.get("steps", [])
             if step.get("name") == "Run backend test suite before deploy"]
    if len(steps) != 1:
        raise AssertionError("Expected one real backend test Job caller")
    source = steps[0]["run"].replace("${{ github.sha }}", "a" * 40)
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        capture = directory / "job.yaml"
        calls = directory / "calls"
        executable = directory / "kubectl"
        executable.write_text(
            '#!/bin/bash\nset -euo pipefail\n'
            'printf "%s\\n" "$*" >> "$CALLS"\n'
            'case "$*" in\n'
            '  "get deployment hive -n shizuha-hive "*) printf "%s\\n" "localhost:30500/shizuha-hive:hive-reviewed" ;;\n'
            '  "delete job -n build ci-test-hive-aaaaaaa --ignore-not-found") exit 0 ;;\n'
            '  "apply -f -") cat > "$CAPTURE"; exit 73 ;;\n'
            '  *) exit 74 ;;\n'
            'esac\n'
        )
        executable.chmod(0o700)
        result = subprocess.run(["bash", "-c", source], cwd=ROOT, capture_output=True,
                                text=True, timeout=10,
                                env={**os.environ, "PATH": str(directory) + ":" + os.environ["PATH"],
                                     "GITHUB_SHA": "a" * 40, "CAPTURE": str(capture), "CALLS": str(calls)})
        if result.returncode != 73 or not capture.is_file():
            raise AssertionError("Real Job renderer did not stop at the intercepted apply: " + result.stderr)
        observed_calls = calls.read_text().splitlines()
        if len(observed_calls) != 3 or observed_calls[-1] != "apply -f -":
            raise AssertionError("Unexpected control-plane call sequence")
        return yaml.safe_load(capture.read_text()), source


class CiEphemeralPostgresTests(unittest.TestCase):
    def test_actual_deploy_and_premerge_jobs_disable_only_disposable_durability(self):
        for name in ("deploy.yml", "premerge.yml"):
            with self.subTest(workflow=name):
                job, _source = rendered_test_job(ROOT / ".forgejo/workflows" / name)
                self.assertEqual(job["kind"], "Job")
                self.assertEqual(job["metadata"]["namespace"], "build")
                self.assertEqual(job["spec"]["backoffLimit"], 0)
                self.assertEqual(job["spec"]["ttlSecondsAfterFinished"], 3600)
                pod = job["spec"]["template"]["spec"]
                self.assertEqual(pod["restartPolicy"], "Never")
                postgres, = [container for container in pod["initContainers"] if container["name"] == "postgres"]
                self.assertEqual(postgres["image"], "localhost:30500/hive-ci-postgres:16-alpine")
                self.assertEqual(postgres["args"], POSTGRES_ARGS)
                self.assertNotIn("command", postgres)
                self.assertEqual(postgres["restartPolicy"], "Always")
                self.assertEqual(postgres.get("volumeMounts", []), [])
                self.assertEqual(pod["volumes"], [{"emptyDir": {}, "name": "workspace"}])
                self.assertNotIn("autovacuum=off", postgres["args"])

    def test_actual_jobs_retain_test_selection_connection_and_wait_contract(self):
        for name in ("deploy.yml", "premerge.yml"):
            with self.subTest(workflow=name):
                job, source = rendered_test_job(ROOT / ".forgejo/workflows" / name)
                container, = job["spec"]["template"]["spec"]["containers"]
                self.assertEqual(container["name"], "test")
                command, = container["args"]
                self.assertIn("export DB_HOST=127.0.0.1 DB_PORT=5432", command)
                self.assertIn("/app/.venv/bin/python manage.py test --noinput --keepdb", command)
                self.assertNotIn("--parallel", command)
                self.assertNotIn("--failfast", command)
                self.assertIn('"job/${JOB}" --timeout=1800s', source)
                self.assertNotIn("resources", container)

    def test_persistent_local_gate_and_non_test_manifests_keep_durable_defaults(self):
        local_gate = (ROOT / ".ci-local-gate.sh").read_text()
        self.assertIn("-v hive-gate-pgdata:/var/lib/postgresql/data", local_gate)
        for setting in ("fsync=off", "synchronous_commit=off", "full_page_writes=off"):
            self.assertNotIn(setting, local_gate)
        for path in (ROOT / "k8s").rglob("*"):
            if path.is_file() and path.suffix in {".yaml", ".yml", ".tpl"}:
                with self.subTest(path=str(path.relative_to(ROOT))):
                    self.assertNotIn("fsync=off", path.read_text())


if __name__ == "__main__":
    unittest.main()
