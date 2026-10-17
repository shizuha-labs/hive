"""Public keyring projection into dynamic fleet-* namespaces is best-effort.

Live 2026-08-28: after the capability-evidence JSON-patch repair, every Hive
canary died in ~4s on `project-mcp-projection-keyring.sh shizuha-hive
shizuha-fleet` because origin-ci-deployer Forbidden'd `kubectl apply` of the
public Secret into dynamic `fleet-*` namespaces (`set -euo pipefail`). The
static `shizuha-fleet` projection stays the hard gate; the provisioner
repairs fleet-* on create/wake.
"""
from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".forgejo/scripts/project-mcp-projection-keyring.sh"


class ProjectMcpProjectionKeyringTests(unittest.TestCase):
    def test_fleet_star_forbidden_is_non_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp) / "bin"
            fake_bin.mkdir()
            kubectl = fake_bin / "kubectl"
            kubectl.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import os, sys
                    args = sys.argv[1:]
                    joined = " ".join(args)
                    open(os.environ["FAKE_KUBECTL_LOG"], "a").write(joined + "\\n")
                    stdin = sys.stdin.read() if not sys.stdin.isatty() else ""
                    if "get secret hive-capability-evidence-signer" in joined and "-o jsonpath=" in joined:
                        sys.stdout.write("eyJrZXlzIjpbXX0=")
                        sys.exit(0)
                    if args[:2] == ["get", "namespace"]:
                        sys.exit(0)
                    if args[:2] == ["get", "namespaces"]:
                        sys.stdout.write('{"items":[{"metadata":{"name":"fleet-shizuha-115"}},{"metadata":{"name":"shizuha-fleet"}}]}\\n')
                        sys.exit(0)
                    if args[:2] == ["apply", "-f"]:
                        if "namespace: fleet-shizuha-115" in stdin:
                            sys.stderr.write("Error from server (Forbidden): secrets is forbidden\\n")
                            sys.exit(1)
                        sys.exit(0)
                    if "-n" in args and "create" in args and "secret" in args:
                        ns = args[args.index("-n") + 1]
                        sys.stdout.write(
                            "apiVersion: v1\\nkind: Secret\\nmetadata:\\n  name: hive-capability-evidence-signer-public\\n  namespace: %s\\n" % ns
                        )
                        sys.exit(0)
                    if "-n" in args and args[args.index("-n") + 1] == "shizuha-fleet" and "get" in args and "secret" in args:
                        sys.stdout.write('{"data":{"keyring.json":"e30="}}\\n')
                        sys.exit(0)
                    sys.stderr.write("unexpected kubectl invocation: %s\\n" % joined)
                    sys.exit(1)
                    """
                )
            )
            kubectl.chmod(kubectl.stat().st_mode | stat.S_IEXEC)
            log = Path(tmp) / "kubectl.log"
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
            env["FAKE_KUBECTL_LOG"] = str(log)
            completed = subprocess.run(
                ["bash", str(SCRIPT), "shizuha-hive", "shizuha-fleet"],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(
                completed.returncode,
                0,
                msg=f"stdout={completed.stdout!r} stderr={completed.stderr!r}",
            )
            self.assertIn("skipped public keyring projection to fleet-shizuha-115", completed.stdout + completed.stderr)

    def test_static_shizuha_fleet_failure_is_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp) / "bin"
            fake_bin.mkdir()
            kubectl = fake_bin / "kubectl"
            kubectl.write_text(
                textwrap.dedent(
                    """\
                    #!/bin/sh
                    set -eu
                    case " $* " in
                      *" get secret hive-capability-evidence-signer "*)
                        printf 'eyJrZXlzIjpbXX0='
                        exit 0
                        ;;
                      *" get namespace "*)
                        exit 0
                        ;;
                      *" apply -f -"*|*" -n shizuha-fleet "*)
                        echo "Error from server (Forbidden): error when retrieving current configuration" >&2
                        exit 1
                        ;;
                    esac
                    echo "unexpected kubectl invocation: $*" >&2
                    exit 1
                    """
                )
            )
            kubectl.chmod(kubectl.stat().st_mode | stat.S_IEXEC)
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
            completed = subprocess.run(
                ["bash", str(SCRIPT), "shizuha-hive", "shizuha-fleet"],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("Forbidden", completed.stderr)


if __name__ == "__main__":
    unittest.main()
