"""Regression tests for HIVE-1810: rollback ERR trap must name the actual
failing pipeline stage, not merely the last element (`tail -n1`).

The r1/r2 failure handlers record `$BASH_COMMAND`, which for a failed pipeline
is the LAST element. When an upstream stage (e.g. `kubectl exec`) fails and
`tail -n1` succeeds, the rollback evidence would report `command=tail -n1` and
destroy the causal command. The fix passes `PIPESTATUS` through the ERR trap and
derives `stage=N rc=N pipeline_status=...` naming the real culprit.
"""

import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".forgejo" / "scripts"


class Hive1810PipelineEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fleet = (SCRIPTS / "ring-release-fleet.sh").read_text()
        cls.canary = (SCRIPTS / "ring-release-canary.sh").read_text()

    def test_err_trap_passes_pipestatus_to_fleet_handler(self):
        self.assertIn(
            'trap \'r2_failure "$?" "$BASH_COMMAND" "${PIPESTATUS[*]}"\' ERR',
            self.fleet,
        )

    def test_err_trap_passes_pipestatus_to_canary_handler(self):
        self.assertIn(
            'trap \'r1_failure "$?" "$BASH_COMMAND" "${PIPESTATUS[*]}"\' ERR',
            self.canary,
        )

    def test_fleet_handler_derives_stage_info_from_pipestatus(self):
        # The handler must compute stage=N rc=N from PIPESTATUS and include it
        # in every evidence carrier (capture-ring-evidence.sh call, phase-local
        # rollback, and the full rollback record).
        self.assertIn('stage_info="stage=${i} rc=${rc} pipeline_status=${pipeline_status}"', self.fleet)
        self.assertIn('"command=${failed_command} ${stage_info}"', self.fleet)
        self.assertEqual(
            self.fleet.count("${stage_info}"),
            3,  # capture-ring-evidence.sh + phase-local + full rollback
        )

    def test_fixture_pipeline_stderr_is_captured_for_evidence(self):
        # The HIVE-725 fixture pipeline redirects kubectl exec stderr so the
        # failing stage's bounded stderr is retained in the evidence carrier.
        self.assertIn("2>/tmp/h725-fixture.err", self.fleet)
        self.assertIn("fixture_stderr=", self.fleet)

    def test_upstream_failure_with_successful_tail_n1_reports_stage_1(self):
        # Black-box: reproduce an upstream non-zero with a successful tail -n1
        # through the same ERR-trap pattern and assert the evidence names the
        # failing stage, not just `tail -n1`.
        harness = r"""
set -Eeuo pipefail
r2_failure() {
  local original_rc="$1" failed_command="$2" pipeline_status="${3:-}" stage_info="" i=0 rc
  for rc in $pipeline_status; do
    i=$((i + 1))
    if [ "$rc" != "0" ]; then
      stage_info="stage=${i} rc=${rc} pipeline_status=${pipeline_status}"
      break
    fi
  done
  [ -n "$stage_info" ] || stage_info="pipeline_status=${pipeline_status}"
  printf 'EVIDENCE command=%s %s\n' "$failed_command" "$stage_info"
  exit "$original_rc"
}
trap 'r2_failure "$?" "$BASH_COMMAND" "${PIPESTATUS[*]}"' ERR
# Upstream stage fails (rc 1); sed and tail succeed (rc 0).
false | sed -n 's/^X=//p' | tail -n1
"""
        result = subprocess.run(
            ["bash", "-c", harness],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("EVIDENCE command=tail -n1", result.stdout)
        # Truthful classification: the failing stage is stage 1 (upstream),
        # not the last pipeline element.
        self.assertIn("stage=1 rc=1 pipeline_status=1 0 0", result.stdout)
        self.assertNotRegex(result.stdout, r"stage=3")

    def test_simple_command_failure_reports_stage_1(self):
        # A non-pipeline failure still gets truthful classification: the single
        # stage is stage 1 with its real exit status.
        harness = r"""
set -Eeuo pipefail
r2_failure() {
  local original_rc="$1" failed_command="$2" pipeline_status="${3:-}" stage_info="" i=0 rc
  for rc in $pipeline_status; do
    i=$((i + 1))
    if [ "$rc" != "0" ]; then
      stage_info="stage=${i} rc=${rc} pipeline_status=${pipeline_status}"
      break
    fi
  done
  [ -n "$stage_info" ] || stage_info="pipeline_status=${pipeline_status}"
  printf 'EVIDENCE command=%s %s\n' "$failed_command" "$stage_info"
  exit "$original_rc"
}
trap 'r2_failure "$?" "$BASH_COMMAND" "${PIPESTATUS[*]}"' ERR
false
"""
        result = subprocess.run(
            ["bash", "-c", harness],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("EVIDENCE command=false", result.stdout)
        self.assertIn("stage=1 rc=1 pipeline_status=1", result.stdout)


if __name__ == "__main__":
    unittest.main()
