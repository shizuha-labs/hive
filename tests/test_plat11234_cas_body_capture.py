"""PLAT-11234 (HIVE-2819 residual): CAS-conflict body capture + non-empty diagnostics group.

Run 3451 recorded (a) the truncated generic 422 body with zero .details/.causes
and (b) an empty diagnostics group. #455 preserved kubectl's stderr, but kubectl
summarizes generic 422s — the full Status body only appears in verbose (-v=6)
stderr. These tests pin both halves of the residual fix.
"""

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / ".forgejo" / "scripts"


def _load(file_name: str, name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file_name)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ring_fence = _load("ring_fence.py", "ring_fence")
ring_executor = _load("ring_executor.py", "ring_executor")


class _StubKubectlBinary:
    """Writes a stub kubectl script: invocations without -v=6 fail with the
    generic 422 summary; an invocation carrying -v=6 emits the full Status
    body (that richer capture is what the fix must surface)."""

    def __init__(self, plain_stderr: str, verbose_stderr: str):
        self._tmp = tempfile.TemporaryDirectory()
        script = Path(self._tmp.name) / "kubectl-stub.sh"
        script.write_text(
            "#!/bin/sh\n"
            'case "$*" in *-v=6*)\n'
            f"  printf {json.dumps(verbose_stderr)} >&2\n"
            "  exit 1 ;;\n"
            "*)\n"
            f"  printf {json.dumps(plain_stderr)} >&2\n"
            "  exit 1 ;;\n"
            "esac\n"
        )
        script.chmod(0o755)
        self.path = str(script)

    def cleanup(self):
        self._tmp.cleanup()


class TestCasConflictBodyCapture(unittest.TestCase):
    GENERIC = "The request is invalid: the server rejected our request due to an error in our request"
    VERBOSE = (
        "Response Status: 422 Invalid in 12 milliseconds\n"
        '{"kind":"Status","apiVersion":"v1","metadata":{},"status":"Failure",'
        '"message":"Job.batch \\"hive-ring-execute-canary-55158\\" is invalid: '
        'spec.template.spec.containers: Required value","reason":"Invalid",'
        '"details":{"causes":[{"reason":"FieldValueRequired",'
        '"message":"spec.template.spec.containers: Required value"}]},"code":422}'
    )

    def test_generic_422_conflict_upgrades_to_full_verbose_body(self):
        stub = _StubKubectlBinary(self.GENERIC, self.VERBOSE)
        self.addCleanup(stub.cleanup)
        api = ring_fence.Kubectl(stub.path)
        with self.assertRaises(ring_fence.Conflict) as ctx:
            api.run(["create", "-f", "-"], stdin="{}")
        detail = str(ctx.exception)
        self.assertIn("Kubernetes CAS conflict:", detail)
        # the full body surfaced despite plain stderr being the generic summary:
        # .details/.causes present (run 3451 had zero of them)
        self.assertIn("FieldValueRequired", detail)
        self.assertIn('"code":422', detail)

    def test_verbose_replane_keeps_original_when_verbose_is_thinner(self):
        stub = _StubKubectlBinary(self.GENERIC, "error: generic text")
        self.addCleanup(stub.cleanup)
        api = ring_fence.Kubectl(stub.path)
        with self.assertRaises(ring_fence.Conflict) as ctx:
            api.run(["create", "-f", "-"], stdin="{}")
        detail = str(ctx.exception)
        self.assertIn(self.GENERIC, detail)
        self.assertNotIn("error: generic text", detail)

    def test_non_cas_failure_does_not_replane(self):
        stub = _StubKubectlBinary("error: forbidden", "never-called-marker")
        self.addCleanup(stub.cleanup)
        api = ring_fence.Kubectl(stub.path)
        with self.assertRaises(ring_fence.FenceError) as ctx:
            api.run(["create", "-f", "-"], stdin="{}")
        self.assertIn("forbidden", str(ctx.exception))
        self.assertNotIn("never-called-marker", str(ctx.exception))


class TestFailClosedDiagnosticsGroup(unittest.TestCase):
    BODY = (
        "Kubernetes CAS conflict: The request is invalid: the server rejected "
        "our request due to an error in our request details.causes.full-body"
    )

    def test_main_fail_closed_wraps_error_in_non_empty_group(self):
        calls = {}

        def exploding_submit(args):
            calls["raised"] = True
            raise ring_fence.Conflict(self.BODY)

        original = ring_executor.submit
        ring_executor.submit = exploding_submit
        try:
            argv = [
                "ring-executor", "submit", "--ring", "canary",
                "--release-sha", "a" * 40, "--restored-sha", "b" * 40,
                "--run-id", "run", "--run-number", "1", "--job", "j",
                "--source-tree", "/tmp",
            ]
            with contextlib.redirect_stderr(io.StringIO()) as err, \
                 unittest.mock.patch.object(sys, "argv", argv):
                rc = ring_executor.main()
        finally:
            ring_executor.submit = original
        self.assertTrue(calls.get("raised"))
        self.assertEqual(rc, 1)
        out = err.getvalue()
        self.assertIn("::group::ring executor failure diagnostics", out)
        self.assertIn(f"::error::ring executor fail-closed: {self.BODY}", out)
        # group ordering: opened before the error, closed after
        self.assertLess(
            out.index("::group::ring executor failure diagnostics"),
            out.index("::error::ring executor fail-closed:"),
        )
        self.assertGreater(
            out.index("::endgroup::"),
            out.index("::error::ring executor fail-closed:"),
        )


if __name__ == "__main__":
    unittest.main()
