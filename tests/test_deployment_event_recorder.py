import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
RECORDER = ROOT / ".forgejo" / "scripts" / "record-deployment-event.sh"


class DeploymentEventRecorderTests(unittest.TestCase):
    def deployment(self, service="hive"):
        return {
            "metadata": {"name": service, "namespace": "shizuha-hive",
                         "uid": "deployment-uid", "resourceVersion": "41", "generation": 7},
            "spec": {"replicas": 2, "template": {"spec": {"containers": [
                {"name": service, "image": "registry/hive:test"},
            ]}}},
            "status": {"replicas": 2, "readyReplicas": 2, "updatedReplicas": 2,
                       "availableReplicas": 2, "observedGeneration": 7},
        }

    def run_recorder_boundary(self, deployment=None, *, caller="direct", read_error=False, digest=""):
        """Execute the shipped caller/recorder; only external CLI boundaries are fixtures."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "kubectl").write_text(textwrap.dedent('''\
                #!/usr/bin/env python3
                import json, os, sys
                with open(os.environ["READS"], "a") as out:
                    out.write(json.dumps(sys.argv[1:]) + "\\n")
                assert sys.argv[1:] == ["-n", "shizuha-hive", "get", "deployment/" + os.environ["SERVICE_FIXTURE"], "-o", "json"]
                if os.environ["READ_ERROR"] == "1":
                    raise SystemExit(1)
                print(os.environ["DEPLOYMENT_FIXTURE"])
            '''))
            (path / "curl").write_text(textwrap.dedent('''\
                #!/usr/bin/env python3
                import json, os, pathlib, sys
                args = sys.argv[1:]
                assert args[-1].endswith("/api/deployment-events/ingest/")
                pathlib.Path(os.environ["POSTED"]).write_text(args[args.index("-d") + 1])
                print(json.dumps({"id": "event", "state": "smoke_passed"}))
            '''))
            for name in ("kubectl", "curl"):
                (path / name).chmod(0o755)
            service = "hive-frontend" if caller == "frontend" else "hive"
            deployment = self.deployment(service) if deployment is None else deployment
            env = {**os.environ, "PATH": f"{path}:{os.environ['PATH']}",
                   "PULSE_USERNAME": "", "PULSE_PASSWORD": "", "PULSE_TOKEN": "fixture-token",
                   "DEPLOYMENT_EVENT_DRY_RUN": "0", "GITHUB_SHA": "a" * 40,
                   "DEPLOYMENT_EVENT_IMAGE_DIGEST": digest,
                   "DEPLOYMENT_ENVIRONMENT": "live", "READS": str(path / "reads"),
                   "POSTED": str(path / "posted"), "SERVICE_FIXTURE": service,
                   "READ_ERROR": "1" if read_error else "0",
                   "DEPLOYMENT_FIXTURE": json.dumps(deployment),
                   "TAG": "test", "FTAG": "test", "IMAGE_INDEX_DIGEST": "sha256:" + "b" * 64}
            # Avoid secret discovery: the renewable credentials here are explicit
            # fixtures; the login stub emits a dummy token without exposing it.
            env.update(PULSE_USERNAME="fixture-user", PULSE_PASSWORD="fixture-password")
            curl = path / "curl"
            curl.write_text(curl.read_text().replace(
                'assert args[-1].endswith',
                'if args[-1].endswith("/api/auth/login/"):\n'
                '    print(json.dumps({"tokens": {"access": "fixture-token"}})); raise SystemExit(0)\n'
                'assert args[-1].endswith'))
            if caller == "fleet":
                release = (ROOT / ".forgejo/scripts/ring-release-fleet.sh").read_text()
                command = "DEPLOYMENT_EVENT_IMAGE_DIGEST=" + release.split(
                    "if ! DEPLOYMENT_EVENT_IMAGE_DIGEST=", 1)[1].split("; then", 1)[0]
                deployment["spec"]["template"]["spec"]["containers"][0]["image"] = "localhost:30500/shizuha-hive:test"
                env["DEPLOYMENT_FIXTURE"] = json.dumps(deployment)
            elif caller == "frontend":
                workflow = yaml.safe_load((ROOT / ".forgejo/workflows/deploy-frontend.yml").read_text())
                block = next(step["run"] for job in workflow["jobs"].values()
                             for step in job.get("steps", [])
                             if "if ! .forgejo/scripts/record-deployment-event.sh" in step.get("run", ""))
                command = block.split("if ! ", 1)[1].split("; then", 1)[0]
                deployment["spec"]["template"]["spec"]["containers"][0]["image"] = "localhost:30500/shizuha-hive-frontend:test"
                env["DEPLOYMENT_FIXTURE"] = json.dumps(deployment)
            else:
                command = '.forgejo/scripts/record-deployment-event.sh hive hive-backend registry/hive:test'
            result = subprocess.run(["bash", "-c", command], cwd=ROOT, env=env,
                                    capture_output=True, text=True, timeout=10)
            posted = json.loads((path / "posted").read_text()) if (path / "posted").exists() else None
            reads = (path / "reads").read_text().splitlines() if (path / "reads").exists() else []
            return result, posted, reads

    def test_actual_fleet_and_frontend_callers_record_converged_image(self):
        for caller in ("fleet", "frontend"):
            with self.subTest(caller=caller):
                result, payload, reads = self.run_recorder_boundary(caller=caller)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(len(reads), 1)
                self.assertIs(payload["metadata"]["replicas_ready"], True)
                self.assertEqual(payload["metadata"]["deployment_uid"], "deployment-uid")
                self.assertEqual(payload["metadata"]["observed_generation"], 7)
                self.assertEqual(payload["state"], "smoke_passed")
                self.assertNotIn("live_probe_passed", payload["metadata"])
                self.assertEqual(payload["image_digest"], "sha256:" + "b" * 64 if caller == "fleet" else "")

    def test_invalid_digest_is_refused_before_read_or_ingest(self):
        result, posted, reads = self.run_recorder_boundary(digest="sha256:partial")
        self.assertEqual(result.returncode, 2)
        self.assertIsNone(posted)
        self.assertEqual(reads, [])

    def test_recorder_refuses_wrong_image_identity_or_unconverged_generation(self):
        changes = [
            ("metadata", "uid", ""), ("metadata", "deletionTimestamp", "2026-09-22T00:00:00Z"),
            ("metadata", "name", "another-deployment"), ("metadata", "namespace", "another-namespace"),
            ("spec", "replicas", 0), ("status", "replicas", 3),
            ("status", "readyReplicas", 1), ("status", "updatedReplicas", 1),
            ("status", "availableReplicas", 1), ("status", "unavailableReplicas", 1),
            ("status", "observedGeneration", 6), ("status", "observedGeneration", 8),
        ]
        fixtures = []
        for section, key, value in changes:
            fixture = self.deployment(); fixture[section][key] = value
            fixtures.append((f"{section}.{key}={value}", fixture))
        wrong_image = self.deployment()
        wrong_image["spec"]["template"]["spec"]["containers"][0]["image"] = "registry/hive:other"
        fixtures.append(("wrong-image", wrong_image))
        fixtures.append(("missing-status", {"metadata": self.deployment()["metadata"]}))
        for name, fixture in fixtures:
            with self.subTest(case=name):
                result, posted, _ = self.run_recorder_boundary(fixture)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIsNone(posted)

    def test_deployment_read_failure_cannot_publish_ready_evidence(self):
        result, posted, _ = self.run_recorder_boundary(read_error=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(posted)

    def run_terminal_check(self, event, ring, build, staging, canary, fleet):
        workflow = yaml.safe_load(
            (ROOT / ".forgejo" / "workflows" / "deploy.yml").read_text()
        )
        command = workflow["jobs"]["verify-production-deploy"]["steps"][0]["run"]
        return subprocess.run(
            ["bash", "-c", command], cwd=ROOT, capture_output=True, text=True,
            env={**os.environ, "EVENT_NAME": event, "RING": ring,
                 "BUILD_RESULT": build, "STAGING_RESULT": staging,
                 "CANARY_RESULT": canary, "FLEET_RESULT": fleet},
        )

    def test_actual_terminal_caller_accepts_completed_fleet_dispatch(self):
        # Origin 39353: both skipped jobs are intentional in the ring=fleet
        # graph; the fenced fleet job itself completed and recorded production.
        result = self.run_terminal_check(
            "workflow_dispatch", "fleet", "success", "skipped", "skipped", "success"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("production deploy receipt accepted", result.stdout)

    def test_partial_dispatch_reports_only_the_requested_ring(self):
        for ring, canary in (("staging", "skipped"), ("canary", "success")):
            with self.subTest(ring=ring):
                result = self.run_terminal_check(
                    "workflow_dispatch", ring, "success", "success", canary, "skipped"
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("no production deployment", result.stdout)
                self.assertNotIn("production deploy receipt accepted", result.stdout)

    def test_required_ring_outcomes_cannot_be_skipped_or_failed(self):
        good = self.run_terminal_check("push", "", *(["success"] * 4))
        self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
        for bad in ("skipped", "failure", "cancelled", ""):
            for index in range(4):
                outcomes = ["success"] * 4
                outcomes[index] = bad
                with self.subTest(event="push", index=index, bad=bad):
                    self.assertNotEqual(self.run_terminal_check("push", "", *outcomes).returncode, 0)
            for ring, index, defaults in (
                ("fleet", 3, ["success", "skipped", "skipped", "success"]),
                ("canary", 2, ["success", "success", "success", "skipped"]),
                ("staging", 1, ["success", "success", "skipped", "skipped"]),
            ):
                defaults[index] = bad
                with self.subTest(ring=ring, bad=bad):
                    self.assertNotEqual(self.run_terminal_check("workflow_dispatch", ring, *defaults).returncode, 0)
        for field in (1, 2):
            outcomes = ["success", "skipped", "skipped", "success"]
            outcomes[field] = "failure"
            self.assertNotEqual(self.run_terminal_check("workflow_dispatch", "fleet", *outcomes).returncode, 0)
        self.assertNotEqual(self.run_terminal_check("workflow_dispatch", "unknown", *(["success"] * 4)).returncode, 0)

    def test_only_production_fleet_convergence_can_emit_backend_event(self):
        workflow = yaml.safe_load(
            (ROOT / ".forgejo" / "workflows" / "deploy.yml").read_text()
        )
        jobs = workflow["jobs"]

        receipt = jobs["verify-production-deploy"]
        self.assertEqual(receipt["if"], "always()")
        self.assertEqual(
            receipt["needs"],
            ["build", "deploy-staging", "deploy-canary", "deploy-fleet"],
        )
        command = receipt["steps"][0]["run"]
        env = receipt["steps"][0]["env"]
        self.assertIn("RING", env)
        self.assertIn("EVENT_NAME", env)
        self.assertIn('[ "$FLEET_RESULT" = success ]', command)
        self.assertIn("production deploy absent", command)
        self.assertIn("no DeploymentEvent may be inferred", command)
        # Execute the actual terminal shell above: source substrings did not
        # detect the staging-skipped rejection or false partial-ring receipt.
        self.assertNotIn("record-deployment-event.sh", command)

        emitters = []
        for job_name, job in jobs.items():
            for step in job.get("steps", []):
                if "record-deployment-event.sh" in step.get("run", ""):
                    emitters.append(job_name)
        # The backend workflow delegates the only event write to the fenced R2
        # executor.  R0/staging and R1/canary must never mint production truth.
        self.assertEqual(emitters, [])
        release = (
            ROOT / ".forgejo" / "scripts" / "ring-release-fleet.sh"
        ).read_text()
        self.assertIn("ring-executor.py commit", release)
        self.assertIn("record-deployment-event.sh", release)
        self.assertEqual(release.count("record-deployment-event.sh"), 1)
        self.assertLess(
            release.index("ring-executor.py commit"),
            release.index("record-deployment-event.sh"),
        )
        self.assertIn("if ! DEPLOYMENT_EVENT_IMAGE_DIGEST=", release)
        self.assertIn(
            ".forgejo/scripts/record-deployment-event.sh",
            release,
        )
        self.assertIn(
            "rollout succeeded but DeploymentEvent recording failed",
            release,
        )

    def test_non_production_environment_is_rejected(self):
        env = {
            **os.environ,
            "DEPLOYMENT_EVENT_DRY_RUN": "1",
            "GITHUB_SHA": "c" * 40,
            "DEPLOYMENT_ENVIRONMENT": "staging",
        }
        result = subprocess.run(
            [RECORDER, "hive", "hive-backend", "registry/hive:test"],
            cwd=ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("rejected environment=staging", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_dry_run_emits_hive_truth_contract(self):
        env = {
            **os.environ,
            "DEPLOYMENT_EVENT_DRY_RUN": "1",
            "GITHUB_REPOSITORY": "shizuha-labs/hive",
            "GITHUB_SHA": "a" * 40,
            "GITHUB_REF_NAME": "main",
            "GITHUB_RUN_ID": "123",
        }
        result = subprocess.run(
            [
                RECORDER,
                "hive-frontend",
                "hive-frontend",
                "registry/hive-frontend:test",
                "/hive/agents,/hive/subscriptions",
            ],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

        payload = json.loads(result.stdout)
        self.assertEqual(payload["project_key"], "HIVE")
        self.assertEqual(payload["state"], "smoke_passed")
        self.assertEqual(payload["smoke_result"], "passed")
        self.assertNotIn("replicas_ready", payload["metadata"])
        self.assertEqual(
            payload["metadata"]["qa_surfaces"],
            ["/hive/agents", "/hive/subscriptions"],
        )
        self.assertEqual(
            payload["metadata"]["truth_contract"],
            "hive-health-accounting-v1",
        )

    def test_ring_executor_release_sha_is_accepted_without_github_context(self):
        sha = "b" * 40
        env = {
            **os.environ,
            "DEPLOYMENT_EVENT_DRY_RUN": "1",
            "RELEASE_SHA": sha,
        }
        env.pop("GITHUB_SHA", None)
        result = subprocess.run(
            [RECORDER, "hive", "hive-backend", "registry/hive:test"],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["commit_sha"], sha)
        self.assertTrue(payload["idempotency_key"].endswith(sha))

    def test_access_token_is_minted_by_identity_service(self):
        source = RECORDER.read_text()
        self.assertIn("shizuha-id.shizuha.svc.cluster.local:8001/api/auth/login/", source)
        self.assertIn('"$ID_LOGIN_URL"', source)
        self.assertNotIn('${PULSE_URL}/api/auth/login/', source)

    def test_id_credentials_take_precedence_over_token_only_fallback(self):
        source = RECORDER.read_text()
        token_path = "jsonpath='{.data.token}'"
        user_path = "jsonpath='{.data.username}'"
        self.assertIn(token_path, source)
        self.assertIn(user_path, source)
        self.assertLess(source.index(user_path), source.index(token_path))
        self.assertLess(source.index('"$ID_LOGIN_URL"'), source.index(token_path))

    def test_login_and_idempotent_ingest_retry_transient_transport(self):
        source = RECORDER.read_text()
        login = source.split('login_response="$(curl', 1)[1].split(
            ')" || {', 1
        )[0]
        self.assertIn("--retry 4", login)
        self.assertIn("--retry-all-errors", login)
        self.assertIn("--retry-max-time 75", login)
        self.assertIn("--connect-timeout 5", login)
        self.assertIn("--max-time 15", login)

        ingest = source.split('response="$(curl', 1)[1].split(
            ')"\nunset PULSE_TOKEN', 1
        )[0]
        self.assertIn("--retry 4", ingest)
        self.assertIn("--retry-all-errors", ingest)
        self.assertIn("--retry-max-time 150", ingest)
        self.assertIn("--connect-timeout 5", ingest)
        self.assertIn("--max-time 30", ingest)
        self.assertIn('-H "Idempotency-Key: ${IDEMPOTENCY_KEY}"', ingest)
        self.assertIn('-d "$payload"', ingest)


if __name__ == "__main__":
    unittest.main()
