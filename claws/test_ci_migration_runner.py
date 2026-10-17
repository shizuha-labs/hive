import os
import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / ".forgejo/scripts/run-migrations-with-retry.sh"


class MigrationRunnerTests(SimpleTestCase):
    def _run(self, mode):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            counter = directory / "count"
            fake_python = directory / "python"
            fake_python.write_text(
                """#!/bin/sh
if [ "$1" = "-c" ]; then
  [ "$FAKE_MODE" != import_fail ] || {
    echo "No module named django" >&2
    exit 1
  }
  exit 0
fi
count=0
[ ! -f "$FAKE_COUNTER" ] || count=$(cat "$FAKE_COUNTER")
count=$((count + 1))
printf '%s' "$count" >"$FAKE_COUNTER"
case "$FAKE_MODE" in
  transient_once)
    [ "$count" -gt 1 ] && exit 0
    echo "connection to server failed: Connection refused" >&2
    exit 1
    ;;
  transient_always)
    echo "the database system is starting up" >&2
    exit 1
    ;;
  deterministic)
    echo "django.db.migrations.exceptions.InconsistentMigrationHistory" >&2
    exit 1
    ;;
esac
"""
            )
            fake_python.chmod(0o755)
            result = subprocess.run(
                ["/bin/bash", str(RUNNER)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "FAKE_MODE": mode,
                    "FAKE_COUNTER": str(counter),
                    "HIVE_MIGRATION_PYTHON": str(fake_python),
                    "HIVE_MIGRATION_ATTEMPTS": "2",
                    "HIVE_MIGRATION_RETRY_SECONDS": "0",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            attempts = int(counter.read_text()) if counter.exists() else 0
            return result, attempts

    def test_missing_django_is_terminal_without_migration_attempt(self):
        result, attempts = self._run("import_fail")
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertEqual(attempts, 0)

    def test_deterministic_migration_failure_is_terminal(self):
        result, attempts = self._run("deterministic")
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertEqual(attempts, 1)
        self.assertIn("non-retryable Django migration failure", result.stderr)

    def test_transient_database_failure_recovers_within_window(self):
        result, attempts = self._run("transient_once")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(attempts, 2)
        self.assertIn("waiting for policy or primary convergence", result.stdout)

    def test_exhausted_transient_window_requests_job_retry(self):
        result, attempts = self._run("transient_always")
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertEqual(attempts, 2)
