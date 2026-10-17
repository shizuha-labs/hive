"""Completed architecture receipts survive Kubernetes Job TTL cleanup."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class BuildCompletionReceiptTests(unittest.TestCase):
    def run_wait(self, fail=False):
        source = (Path(__file__).parents[1] / 'workflows/deploy.yml').read_text()
        start = source.index('          python3 "$BUILD_GUARD" wait-pair')
        end = source.index('          # Assemble the OCI index', start)
        script = 'set -euo pipefail\n' + source[start:end]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            guard = root / 'guard.py'
            guard.write_text('import os,sys; print("both completion receipts captured"); sys.exit(int(os.environ["FAIL_PAIR"]))')
            kubectl = root / 'kubectl'
            kubectl.write_text('#!/bin/sh\necho "completed Job has already expired" >&2\nexit 1\n')
            kubectl.chmod(0o755)
            env = dict(os.environ, BUILD_GUARD=str(guard), AMD64_JOB='amd64', ARM64_JOB='arm64',
                       SHORT='fixture', FAIL_PAIR='1' if fail else '0', PATH=directory+os.pathsep+os.environ['PATH'])
            return subprocess.run(['bash'], input=script, env=env, capture_output=True, text=True)

    def test_verified_pair_does_not_reread_expired_job(self):
        result = self.run_wait()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Both arch builds done.', result.stdout)
        self.assertNotIn('expired', result.stderr)

    def test_pair_failure_stops_before_manifest(self):
        result = self.run_wait(fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('Both arch builds done.', result.stdout)


if __name__ == '__main__': unittest.main()
