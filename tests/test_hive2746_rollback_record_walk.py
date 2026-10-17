"""HIVE-2746: the rollback record emitter's close-walk must never fire a
side-effect transition (Escalate to Leads / blocked / cancelled).

The old fallback took ANY transition whose target slug was not "todo"; from
`todo` (no direct terminal edge in autonomous-bug) that fired "Escalate to
Leads", which Pulse rejected (missing_evidence_schema) and auto-created an
urgent schema-repair blocker child PER ROLLBACK. The walk is now constrained
to the linear implementation path and fails closed to the manual path."""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".forgejo" / "scripts" / "record-rollout-failure.sh"


def _selector_source():
    src = SCRIPT.read_text(encoding="utf-8")
    start = src.index("HIVE-2746:")
    end = src.find("|| return 1", start)
    body = src[start:end]
    marker = 'print(json.dumps({"id": best.get("id") or "", "slug": slug}))'
    return body[body.index("import json,sys"):body.index(marker) + len(marker)]


def _select(transitions, current="todo"):
    source = _selector_source()
    payload = dict(transitions)
    proc = subprocess_run(["python3", "-c", source, current], json.dumps(payload))
    return proc


import subprocess


def subprocess_run(argv, stdin_text):
    return subprocess.run(argv, input=stdin_text, capture_output=True, text=True)


class SelectorContractTests(unittest.TestCase):
    def test_terminal_transition_wins(self):
        rows = {"transitions": [
            {"id": "t1", "to_status": "in_progress"},
            {"id": "t2", "to_status": "done"},
        ]}
        out = _select(rows)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["slug"], "done")

    def test_todo_falls_back_to_start_work_only(self):
        rows = {"transitions": [
            {"id": "a", "to_status": "blocked"},          # Escalate to Leads
            {"id": "b", "to_status": "in_progress"},      # Start Work
            {"id": "c", "to_status": "cancelled"},
        ]}
        out = _select(rows, current="todo")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["slug"], "in_progress")
        self.assertEqual(json.loads(out.stdout)["id"], "b")

    def test_no_linear_eligible_fails_closed_never_blocked(self):
        rows = {"transitions": [
            {"id": "a", "to_status": "blocked"},
            {"id": "c", "to_status": "cancelled"},
        ]}
        out = _select(rows, current="todo")
        self.assertNotEqual(out.returncode, 0)  # fail-closed exit 3, no pick

    def test_forbidden_slugs_never_selected_even_if_listed_terminal_like(self):
        rows = {"transitions": [
            {"id": "a", "to_status": "blocked"},
        ]}
        out = _select(rows, current="in_progress")
        self.assertNotEqual(out.returncode, 0)


class ShellCallerContractTests(unittest.TestCase):
    def test_real_shell_passes_current_status_into_selector(self):
        # Execute the production shell function, not just its extracted Python.
        # A shell stub represents the already-authenticated Pulse boundary and
        # records every transition; repeat a full walk to test the reset path.
        source = SCRIPT.read_text()
        function = source[source.index("close_rollback_record() {"):source.index("\npulse_call() {")]
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "transitions"
            harness = r'''set -euo pipefail
PULSE_URL=http://fixture.invalid
pulse_call() {
  if [[ "$1" == POST ]]; then
    printf '%s\n' "$3" >> "$CALL_LOG"
    printf '{}'
  elif [[ "$2" == */available-transitions/ ]]; then
    case "$(wc -l < "$CALL_LOG")" in
      0|3) printf '{"transitions":[{"id":"escalate","to_status":"blocked"},{"id":"start","to_status":"in_progress"}]}' ;;
      1|4) printf '{"transitions":[{"id":"review","to_status":"in_review"}]}' ;;
      2|5) printf '{"transitions":[{"id":"close","to_status":"done"}]}' ;;
    esac
  else printf '{"status":"todo"}'
  fi
}
'''
            import os
            log.write_text("")
            proc = subprocess.run(["bash", "-c", harness + function +
                '\nclose_rollback_record 17 "reviewed fixture"\nclose_rollback_record 18 "reviewed fixture"\n'],
                text=True, capture_output=True, env={**os.environ, "CALL_LOG": str(log)})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            rows = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual([row["transition_id"] for row in rows], ["start", "review", "close"] * 2)
            self.assertIn("reviewed fixture", rows[-1]["comment"])


if __name__ == "__main__":
    unittest.main()
