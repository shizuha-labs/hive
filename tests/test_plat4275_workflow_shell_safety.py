"""Regression tests for PLAT-4275 workflow command-injection hardening."""

import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".forgejo" / "workflows" / "deploy.yml"


class WorkflowShellSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.safe_load(WORKFLOW.read_text())

    def _step(self, job_name, step_name):
        steps = self.workflow["jobs"][job_name]["steps"]
        return next(step for step in steps if step.get("name") == step_name)

    def test_github_event_context_is_never_interpolated_into_run_scripts(self):
        unsafe = []
        for job_name, job in self.workflow["jobs"].items():
            for step in job.get("steps", []):
                run = step.get("run", "")
                if "${{ github.event" in run:
                    unsafe.append(f"{job_name}: {step.get('name', '<unnamed>')}")
        self.assertEqual(
            unsafe,
            [],
            "github.event values must cross the workflow/shell boundary via step env",
        )

    def test_canary_dispatch_input_crosses_via_environment(self):
        step = self._step("deploy-canary", "R1 — deploy, soak, rollback fail-loud")
        self.assertEqual(step["env"]["CANARY_HOLD"], "${{ github.event.inputs.hold || 'false' }}")
        self.assertIn('"$CANARY_HOLD"', step["run"])

    def test_fleet_dispatch_inputs_cross_via_environment(self):
        step = self._step("deploy-fleet", "R2 — Fleet deploy + verify")
        self.assertEqual(step["env"]["EVENT_NAME"], "${{ github.event_name }}")
        self.assertEqual(step["env"]["REQUESTED_RING"], "${{ github.event.inputs.ring }}")
        self.assertEqual(step["env"]["REQUESTED_SHA"], "${{ github.event.inputs.sha }}")
        for variable in ("EVENT_NAME", "REQUESTED_RING", "REQUESTED_SHA"):
            self.assertIn(f'"${variable}"', step["run"])

    def test_release_sha_uses_the_validated_shell_variable(self):
        step = self._step("deploy-fleet", "R2 — Fleet deploy + verify")
        self.assertEqual(step["env"]["RELEASE_SHA"], "${{ github.sha }}")
        self.assertIn('SHA="$RELEASE_SHA"', step["run"])
        self.assertIn('monotonic-release-guard.sh shizuha-hive hive "$SHA"', step["run"])
        self.assertNotIn("${{ github.sha }}", step["run"])


if __name__ == "__main__":
    unittest.main()


class HIVE1363KekHeredocBoundaryTests(unittest.TestCase):
    """HIVE-1363: pod-only command substitution must not run on the runner.

    The premerge/deploy Job manifests are rendered via an UNQUOTED
    `cat <<YAML | kubectl apply -f -` heredoc, so the outer runner shell
    expands `$(...)` while rendering YAML. The KEK line is meant to execute
    INSIDE the Job pod (with the container's known interpreter), not on the
    runner. Before the fix, `export HIVE_TOKEN_COORDINATOR_KEK="$(python ...)"`
    was expanded by the runner shell — green only because that runner happened
    to expose bare `python`; a runner image/path change silently substitutes an
    empty KEK and the pod fails later with ImproperlyConfigured.

    The fix escapes the dollar sign so the outer shell leaves it literal
    and the pod's bash evaluates it at runtime, using the explicit
    `/app/.venv/bin/python` interpreter that exists in the container.
    """

    WORKFLOWS = (
        ROOT / ".forgejo" / "workflows" / "premerge.yml",
        ROOT / ".forgejo" / "workflows" / "deploy.yml",
    )

    def test_kek_line_has_no_unescaped_command_substitution(self):
        """The KEK `$(...)` must survive the outer heredoc expansion."""
        for wf in self.WORKFLOWS:
            src = wf.read_text()
            with self.subTest(workflow=wf.name):
                self.assertIn(
                    'HIVE_TOKEN_COORDINATOR_KEK="\\$(/app/.venv/bin/python -c',
                    src,
                    f"{wf.name}: KEK must use escaped \\$(...) so it runs in the pod",
                )
                # No bare-python unescaped form may remain.
                self.assertNotIn(
                    'HIVE_TOKEN_COORDINATOR_KEK="$(python -c',
                    src,
                    f"{wf.name}: unescaped $(python ...) would run on the runner",
                )

    def test_kek_uses_the_container_interpreter(self):
        """The pod command must use /app/.venv/bin/python, not bare `python`."""
        for wf in self.WORKFLOWS:
            src = wf.read_text()
            with self.subTest(workflow=wf.name):
                line = next(
                    l for l in src.splitlines()
                    if "HIVE_TOKEN_COORDINATOR_KEK=" in l
                )
                self.assertIn("/app/.venv/bin/python", line)
                self.assertNotIn('"$(python ', line)

    def test_workflow_yaml_still_parses(self):
        """The escaped dollar-sign must not break YAML parsing of the manifest."""
        for wf in self.WORKFLOWS:
            with self.subTest(workflow=wf.name):
                data = yaml.safe_load(wf.read_text())
                self.assertIsInstance(data, dict)
                self.assertIn("jobs", data)
