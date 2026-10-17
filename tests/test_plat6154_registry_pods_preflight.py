"""Regression tests for PLAT-6154: registry Pod reads must stay forbidden.

The fleet NetworkPolicy RBAC preflight asserts several negative RBAC
invariants (fleet pods/exec, secrets/get, PVC patch/delete) but never asserted
that origin/origin-ci-deployer cannot ``get pods -n registry``. A re-grant of
the PLAT-4692/PLAT-5595 overbroad permission would therefore leave the gate
green and silently re-open the original regression.

These tests pin the new negative assertion and prove a controlled grant of
registry pods/get fails the gate for the intended reason (exit 21 + overbroad
diagnostic), while the absent permission stays green.
"""

import re
import subprocess
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".forgejo" / "workflows" / "deploy-fleet-networkpolicy.yml"
STEP_NAME = "Verify arbitrary-gx outer and nested cache paths"
ASSERTION = "require_no 'registry Pod reads must stay forbidden' get pods -n registry"


def _require_no_definition(run: str) -> str:
    """Extract the workflow's real ``require_no`` function verbatim."""
    match = re.search(r"require_no\(\) \{.*?\n\}\n", run, re.DOTALL)
    assert match is not None, "require_no() definition missing from workflow"
    return match.group(0)


class RegistryPodsPreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.safe_load(WORKFLOW.read_text())
        cls.step = next(
            step for step in cls.workflow["jobs"]["reconcile"]["steps"]
            if step.get("name") == STEP_NAME
        )
        cls.run_script = cls.step["run"]
        cls.require_no = _require_no_definition(cls.run_script)

    def test_registry_pods_get_negative_assertion_is_present(self):
        self.assertIn(ASSERTION, self.run_script)

    def test_assertion_runs_before_registry_endpoint_reads(self):
        # The negative check must run before the canary reads registry
        # endpoints/svc (which are allowed), so an overbroad grant is caught
        # before any registry metadata is touched.
        self.assertLess(self.run_script.index(ASSERTION), self.run_script.index("APT_ENDPOINT="))

    def test_controlled_grant_of_registry_pods_get_fails_the_gate(self):
        # A kubectl auth can-i that answers `yes` for `get pods -n registry`
        # must make the gate fail with the intended overbroad diagnostic
        # (exit 21), not pass silently.
        script = (
            self.require_no
            + "kubectl() { echo yes; }\n"
            + ASSERTION + "\n"
        )
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 21)
        combined = proc.stdout + proc.stderr
        self.assertIn("registry Pod reads must stay forbidden", combined)
        self.assertIn("overbroad", combined)

    def test_absent_permission_stays_green(self):
        # With the permission absent (`no`), the gate must stay green.
        script = (
            self.require_no
            + "kubectl() { echo no; }\n"
            + ASSERTION + "\n"
            + "echo GREEN\n"
        )
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("GREEN", proc.stdout)


if __name__ == "__main__":
    unittest.main()
