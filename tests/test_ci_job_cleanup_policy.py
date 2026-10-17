import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


class CiJobCleanupPolicyTests(unittest.TestCase):
    @staticmethod
    def workflow(name: str) -> str:
        return (ROOT / ".forgejo" / "workflows" / name).read_text()

    def assert_job_ttl(self, workflow: str, name_pattern: str) -> None:
        self.assertRegex(
            workflow,
            rf"name: {name_pattern}[\s\S]{{0,500}}?ttlSecondsAfterFinished: 300",
        )

    def assert_all_kaniko_jobs_cleanup(self, workflow: str) -> None:
        executors = workflow.count("image: gcr.io/kaniko-project/executor:v1.23.2")
        self.assertGreater(executors, 0)
        self.assertEqual(workflow.count("- --cleanup"), executors)

    def assert_multiarch_waits_are_concurrent(self, workflow: str) -> None:
        self.assertIn(
            "wait_job amd64 &\n"
            "          AMD64_WAIT_PID=$!\n"
            "          wait_job arm64 &\n"
            "          ARM64_WAIT_PID=$!\n"
            "          WAIT_STATUS=0\n"
            '          wait "$AMD64_WAIT_PID" || WAIT_STATUS=$?\n'
            '          wait "$ARM64_WAIT_PID" || WAIT_STATUS=$?\n'
            '          [ "$WAIT_STATUS" -eq 0 ] || exit "$WAIT_STATUS"',
            workflow,
        )
        self.assertNotIn("wait_job amd64\n          wait_job arm64", workflow)

    def test_backend_build_manifest_verify_and_smoke_jobs_expire_quickly(self):
        workflow = self.workflow("deploy.yml")
        for name in (
            r"ci-build-hive-\$\{SHORT\}-\$\{ARCH\}",
            r"ci-verify-hive-\$\{SHORT\}",
        ):
            with self.subTest(name=name):
                self.assert_job_ttl(workflow, name)
        preflight = workflow[workflow.index('JOB="hive-pulse-auth-preflight-') :]
        self.assertIn("ttlSecondsAfterFinished: 300", preflight)
        self.assert_all_kaniko_jobs_cleanup(workflow)
        self.assert_multiarch_waits_are_concurrent(workflow)

    def test_frontend_build_smoke_manifest_and_verify_jobs_expire_quickly(self):
        workflow = self.workflow("deploy-frontend.yml")
        for name in (
            r"\$\{JOB\}",
            r"ci-build-hive-frontend-\$\{SHORT\}-\$\{ARCH\}",
            r"ci-manifest-hive-frontend-\$\{SHORT\}",
        ):
            with self.subTest(name=name):
                self.assert_job_ttl(workflow, name)
        self.assertIn(
            "labels: { app: ci-verify, service: ${REPO} }\n"
            "          spec:\n"
            "            backoffLimit: 0\n"
            "            ttlSecondsAfterFinished: 300",
            workflow,
        )
        self.assert_all_kaniko_jobs_cleanup(workflow)
        self.assert_multiarch_waits_are_concurrent(workflow)

    def test_frontend_workflow_does_not_run_for_backend_only_pushes(self):
        workflow = self.workflow("deploy-frontend.yml")
        trigger = workflow[: workflow.index("concurrency:")]
        self.assertIn(
            "    paths:\n"
            "      - '.forgejo/workflows/deploy-frontend.yml'\n"
            "      - 'frontend/**'",
            trigger,
        )

    def test_frontend_test_job_bounds_runtime_with_active_deadline(self):
        """HIVE-1195: the frontend test Job (ci-test-shizuha-hive-frontend, labelled
        shizuha.io/ci-gate: PLAT-1219) must carry an activeDeadlineSeconds so an
        image-pull failure (ImagePullBackOff) self-terminates as DeadlineExceeded ->
        Failed -> reaped, instead of lingering Pending forever and firing
        PodImagePullFailing indefinitely. backoffLimit: 0 is retained so the gate
        never silently retries a failed test."""
        workflow = self.workflow("deploy-frontend.yml")
        # Isolate the frontend test Job spec (unique PLAT-1219 gate label) up to the
        # pod template, and assert it bounds runtime while keeping backoffLimit: 0.
        start = workflow.index("shizuha.io/ci-gate: PLAT-1219")
        block = workflow[start : workflow.index("template:", start)]
        self.assertIn("backoffLimit: 0", block)
        self.assertIn("activeDeadlineSeconds: 3600", block)

    def test_all_frontend_jobs_bound_runtime_with_active_deadline(self):
        """HIVE-1195 (review expansion): every frontend CI Job in deploy-frontend.yml
        (ci-test, ci-build, ci-manifest, ci-verify) must carry an activeDeadlineSeconds
        bounded to its corresponding gate `kubectl wait --timeout`, so an
        image-pull/scheduling failure self-terminates (DeadlineExceeded -> Failed ->
        reaped) instead of lingering Pending forever. backoffLimit: 0 is retained on
        every Job so no gate silently retries."""
        workflow = self.workflow("deploy-frontend.yml")
        # Job anchor -> expected activeDeadlineSeconds (matches its gate wait
        # timeout). ci-test and ci-verify are rendered with `name: ${JOB}`, so
        # anchor on their unique labels instead of the name line.
        expected = [
            ("labels: { app: ci-test, service: shizuha-hive-frontend, shizuha.io/ci-gate: PLAT-1219 }", 3600),
            ("name: ci-build-hive-frontend-${SHORT}-${ARCH}", 1800),
            ("name: ci-manifest-hive-frontend-${SHORT}", 300),
            ("labels: { app: ci-verify, service: ${REPO} }", 120),
        ]
        for anchor, deadline in expected:
            with self.subTest(anchor=anchor):
                start = workflow.index(anchor)
                block = workflow[start : workflow.index("template:", start)]
                self.assertIn("backoffLimit: 0", block)
                self.assertIn(f"activeDeadlineSeconds: {deadline}", block)

    def test_ephemeral_backend_test_jobs_keep_the_database_until_pod_cleanup(self):
        command = "/app/.venv/bin/python manage.py test --noinput --keepdb"
        for name in ("deploy.yml", "premerge.yml"):
            with self.subTest(workflow=name):
                workflow = self.workflow(name)
                self.assertIn(command, workflow)
                self.assertNotIn(
                    "/app/.venv/bin/python manage.py test --noinput\n",
                    workflow,
                )

    def test_backend_builder_fetches_kubectl_anonymously(self):
        # ORIG-144: adding Authorization to the public kubectl package fetch
        # turns it into a 401, so the builder must fetch anonymously (no
        # PACKAGE_TOKEN / GIT_PASSWORD header on the kubectl wget).
        workflow = self.workflow("deploy.yml")
        self.assertIn(
            "wget -q \\\n                          -O kubectl \\\n",
            workflow,
        )
        self.assertNotIn(
            'wget -q --header="Authorization: token \\${PACKAGE_TOKEN}"',
            workflow,
        )
        self.assertNotIn(
            'wget -q --header="Authorization: token \\${GIT_PASSWORD}"',
            workflow,
        )
        self.assertIn("anonymous-fetch: sanctioned", workflow)

    def test_ring_rbac_is_administrator_bootstrapped(self):
        workflow = self.workflow("deploy.yml")
        self.assertEqual(
            workflow.count("Verify administrator-bootstrapped ring executor identities"),
            2,
        )
        self.assertNotIn("kubectl apply -f k8s/hive-ring-executor-rbac.yaml", workflow)
        self.assertNotIn('kubectl create -f "k8s/ring/hive-ring-owner-', workflow)

    def test_bootstrap_leases_use_kubernetes_microtime(self):
        for ring in ("canary", "fleet"):
            manifest = yaml.safe_load(
                (ROOT / f"k8s/ring/hive-ring-owner-{ring}.yaml").read_text()
            )
            self.assertRegex(
                manifest["spec"]["acquireTime"],
                r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$",
            )
            self.assertRegex(
                manifest["spec"]["renewTime"],
                r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$",
            )


if __name__ == "__main__":
    unittest.main()
