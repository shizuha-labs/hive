"""PLAT-7708 — CD rollback-ledger reconciliation contract tests.

Gap (PLAT-7681 hive-mcp 59e9439 class): a rollback that only mutates live state
leaves the CD desired-state annotations pointing at the bad build, so the next
reconcile/re-run re-asserts a sha whose tag was pruned.

Contract under test:
  1. A rollback updates CD desired state (annotations) AND appends the failed
     sha to the ledger in the same operation (cd_rollback).
  2. A deploy attempt against a ledger sha fails loudly and does not loop
     (cd_ledger_check gates both the workflow pre-build and the promote path).
  3. Regression: roll back sha X -> force reconcile -> CD deploys the
     post-rollback sha, never X.
"""

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER_SCRIPT = ROOT / ".forgejo/scripts/cd-rollback-ledger.sh"
RELEASE_SCRIPT = ROOT / ".forgejo/scripts/hive-mcp-release.sh"
WORKFLOW = ROOT / ".forgejo/workflows/deploy-hive-mcp.yml"

FAKE_KUBECTL = r"""#!/usr/bin/env bash
# Minimal kubectl stub persisting deploy image, annotations, and ledger data.
set -u
STATE_DIR="${FAKE_KUBE_STATE:?}"
IMG_FILE="$STATE_DIR/image"
ANN_FILE="$STATE_DIR/annotations"
LEDGER_FILE="$STATE_DIR/ledger"

get_ann() { grep -F "$1=" "$ANN_FILE" 2>/dev/null | head -1 | cut -d= -f2-; }
set_ann() { python3 - "$ANN_FILE" "$1" "$2" <<'PY'
import sys, pathlib
path, key, val = sys.argv[1], sys.argv[2], sys.argv[3]
lines = [l for l in pathlib.Path(path).read_text().splitlines() if not l.startswith(key + "=")]
lines.append(f"{key}={val}")
pathlib.Path(path).write_text("\n".join(lines) + "\n")
PY
}

cmd="$1"; shift
case "$cmd" in
  get)
    # get -n NS deploy/NAME -o jsonpath='{...}' | get -n NS cm/cd-rollback-ledger [-o json]
    args="$*"
    case "$args" in
      *"deploy/shizuha-hive-mcp -o jsonpath='{.spec.template.spec.containers[0].image}'"*)
        cat "$IMG_FILE" ;;
      *"last-successful-image}"*)
        get_ann "shizuha.io/last-successful-image" ;;
      *"last-successful-sha}"*)
        get_ann "shizuha.io/last-successful-sha" ;;
      *"cm/cd-rollback-ledger -o json"*)
        python3 - "$LEDGER_FILE" <<'PY'
import json, sys, pathlib
raw = pathlib.Path(sys.argv[1]).read_text() if pathlib.Path(sys.argv[1]).exists() else ""
data = {}
for line in raw.splitlines():
    if "=" in line:
        k, v = line.split("=", 1)
        data[k] = v
print(json.dumps({"apiVersion": "v1", "kind": "ConfigMap", "data": data}))
PY
        ;;
      *"cm/cd-rollback-ledger"*)
        # existence probe: succeed only if the ledger file exists
        [ -f "$LEDGER_FILE" ] || { echo "Error from server (NotFound)"; exit 1; } ;;
      *) echo "stub: unsupported get: $args" >&2; exit 1 ;;
    esac ;;
  set)
    # set image -n NS deploy/NAME container=IMAGE
    for a in "$@"; do
      case "$a" in *=hive-mcp=*|*hive-mcp=*) printf '%s\n' "${a#*hive-mcp=}" > "$IMG_FILE" ;; esac
    done ;;
  rollout)
    exit 0 ;;
  annotate)
    key=""; val=""
    for a in "$@"; do
      case "$a" in shizuha.io/*=*) key="${a%%=*}"; val="${a#*=}" ;; esac
    done
    [ -n "$key" ] && set_ann "$key" "$val"
    exit 0 ;;
  create)
    # create -n NS configmap cd-rollback-ledger --from-literal=k=v
    for a in "$@"; do
      case "$a" in --from-literal=*) kv="${a#--from-literal=}"; printf '%s\n' "$kv" >> "$LEDGER_FILE" ;; esac
    done
    exit 0 ;;
  patch)
    # patch -n NS cm/cd-rollback-ledger --type merge -p <json>
    # PLAT-8421: the real script passes `-p` and the JSON as SEPARATE args;
    # the old stub matched the bare `-p` arg and stripped it to an empty
    # string, so json.loads("") raised and nothing was ever recorded — the
    # stub masked the broken patch payload. Parse both glued and separate
    # forms, and fail loudly on an unparseable patch.
    patch_json=""
    for a in "$@"; do
      if [ "$a" = "-p" ]; then patch_json="__NEXT__"; continue; fi
      if [ "$patch_json" = "__NEXT__" ]; then patch_json="$a"; continue; fi
      case "$a" in -p*) patch_json="${a#-p}" ;; esac
    done
    if [ -z "$patch_json" ] || [ "$patch_json" = "__NEXT__" ]; then
      echo "stub: patch called without a parseable -p payload" >&2; exit 64
    fi
    python3 - "$LEDGER_FILE" "$patch_json" <<'PY'
import json, sys, pathlib
path, patch = sys.argv[1], sys.argv[2]
data = json.loads(patch).get("data", {})
for k, v in data.items():
    if not isinstance(v, str):
        # Mirror the API server: ConfigMap.data is map[string]string.
        sys.stderr.write(f"Invalid value for data[{k}]: must be a string\n")
        sys.exit(1)
lines = [l for l in pathlib.Path(path).read_text().splitlines() if l.strip()] if pathlib.Path(path).exists() else []
kv = dict(l.split("=", 1) for l in lines)
kv.update(data)
pathlib.Path(path).write_text("\n".join(f"{k}={v}" for k, v in kv.items()) + "\n")
PY
    exit $? ;;
  *) echo "stub: unsupported command: $cmd" >&2; exit 64 ;;
esac
"""


class HiveMcpRollbackLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        stub = self.bin / "kubectl"
        stub.write_text(FAKE_KUBECTL)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        (self.state / "image").write_text("localhost:30500/shizuha-hive-mcp:mcp-dab2088\n")
        (self.state / "annotations").write_text(
            "shizuha.io/last-successful-image=localhost:30500/shizuha-hive-mcp:mcp-dab2088\n"
            "shizuha.io/last-successful-sha=dab2088\n"
        )
        self.env = dict(os.environ)
        self.env["PATH"] = f"{self.bin}:{self.env['PATH']}"
        self.env["FAKE_KUBE_STATE"] = str(self.state)

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, snippet):
        proc = subprocess.run(
            ["bash", "-c", snippet],
            env=self.env, capture_output=True, text=True, timeout=60,
        )
        return proc

    # ---- static contracts -------------------------------------------------

    def test_workflow_gates_build_on_ledger_before_mutation(self):
        text = WORKFLOW.read_text()
        self.assertIn("source .forgejo/scripts/cd-rollback-ledger.sh", text)
        self.assertIn(
            'cd_ledger_check shizuha shizuha-hive-mcp "${{ github.sha }}" || exit 1', text
        )
        # The gate must run in the kubectl-setup step, i.e. BEFORE the build step.
        self.assertLess(
            text.index("cd_ledger_check shizuha shizuha-hive-mcp"),
            text.index("Multi-arch build + deploy + verify"),
        )

    def test_release_script_wires_ledger_into_promote_and_rollback(self):
        text = RELEASE_SCRIPT.read_text()
        self.assertIn("cd_ledger_check shizuha shizuha-hive-mcp", text)
        self.assertIn("cd_rollback shizuha shizuha-hive-mcp", text)
        # Promote must fail closed on a ledger sha.
        self.assertIn("is on the rollback ledger; refusing to deploy", text)

    # ---- functional contracts (stubbed kubectl) ---------------------------

    def test_rollback_updates_desired_state_and_ledger_in_one_operation(self):
        proc = self.run_script(
            'source .forgejo/scripts/cd-rollback-ledger.sh && '
            'cd_rollback shizuha shizuha-hive-mcp hive-mcp '
            '"localhost:30500/shizuha-hive-mcp:mcp-dab2088" 59e9439 "candidate failed"'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        ann = (self.state / "annotations").read_text()
        # Desired state names the RESTORED build — the exact HIVE-2266 bug class.
        self.assertIn(
            "shizuha.io/last-successful-image=localhost:30500/shizuha-hive-mcp:mcp-dab2088", ann
        )
        self.assertIn("shizuha.io/last-successful-sha=dab2088", ann)
        ledger = (self.state / "ledger").read_text()
        self.assertIn("shizuha-hive-mcp__59e9439=", ledger)
        entry = json.loads(
            [l for l in ledger.splitlines() if l.startswith("shizuha-hive-mcp__59e9439=")][0]
            .split("=", 1)[1]
        )
        self.assertEqual(entry["sha"], "59e9439")
        self.assertIn("candidate failed", entry["reason"])

    def test_ledger_sha_is_refused_and_post_rollback_sha_is_allowed(self):
        # Seed the ledger via a rollback of sha X = 59e9439.
        proc = self.run_script(
            'source .forgejo/scripts/cd-rollback-ledger.sh && '
            'cd_rollback shizuha shizuha-hive-mcp hive-mcp '
            '"localhost:30500/shizuha-hive-mcp:mcp-dab2088" 59e9439 "candidate failed"'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # Force reconcile against the rolled-back sha: refused, loudly.
        refused = self.run_script(
            'source .forgejo/scripts/cd-rollback-ledger.sh && '
            'cd_ledger_check shizuha shizuha-hive-mcp 59e94397b14c64591bbf898916ddf61ba54c4095'
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("was ROLLED BACK and is forbidden", refused.stdout)
        # The post-rollback sha reconciles clean.
        allowed = self.run_script(
            'source .forgejo/scripts/cd-rollback-ledger.sh && '
            'cd_ledger_check shizuha shizuha-hive-mcp dab2088'
        )
        self.assertEqual(allowed.returncode, 0, allowed.stdout)

    def test_second_rollback_appends_via_patch_path_with_string_payload(self):
        """PLAT-8421 regression: the SECOND append takes the patch path, and the
        ConfigMap.data value must be the payload JSON encoded as a STRING (the
        API server rejects a raw object — the old code aborted every second+
        rollback before `kubectl set image`, leaving the failed sha off the
        ledger and re-deployable)."""
        # Episode 1: ledger CM absent -> create path (string value, OK).
        first = self.run_script(
            'source .forgejo/scripts/cd-rollback-ledger.sh && '
            'cd_rollback shizuha shizuha-hive-mcp hive-mcp '
            '"localhost:30500/shizuha-hive-mcp:mcp-dab2088" 59e9439 "candidate failed"'
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        # Episode 2: ledger CM exists -> patch path (the broken one). The
        # restore target is the last-good build (dab2088); the failed sha is
        # the merged-fix build c693dd3.
        second = self.run_script(
            'source .forgejo/scripts/cd-rollback-ledger.sh && '
            'cd_rollback shizuha shizuha-hive-mcp hive-mcp '
            '"localhost:30500/shizuha-hive-mcp:mcp-dab2088" c693dd3 "merged fix regressed"'
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        ledger = (self.state / "ledger").read_text()
        entries = dict(l.split("=", 1) for l in ledger.splitlines() if "=" in l)
        self.assertIn("shizuha-hive-mcp__59e9439", entries)
        self.assertIn("shizuha-hive-mcp__c693dd3", entries)
        # Both stored values are the JSON TEXT (string), parseable back.
        for key in ("shizuha-hive-mcp__59e9439", "shizuha-hive-mcp__c693dd3"):
            entry = json.loads(entries[key])
            self.assertEqual(entry["sha"], key.split("__")[1])
        self.assertIn("merged fix regressed", json.loads(entries["shizuha-hive-mcp__c693dd3"])["reason"])
        # The second rollback completed its restore: live state moved to the
        # restored image (the old bug aborted BEFORE `kubectl set image`).
        self.assertIn("mcp-dab2088", (self.state / "image").read_text())
        # Both shas are now refused; the post-rollback sha reconciles clean.
        for bad in ("59e94397b14c64591bbf898916ddf61ba54c4095", "c693dd3000000000000000000000000000000000"):
            refused = self.run_script(
                'source .forgejo/scripts/cd-rollback-ledger.sh && '
                f'cd_ledger_check shizuha shizuha-hive-mcp {bad}'
            )
            self.assertNotEqual(refused.returncode, 0, bad)
            self.assertIn("was ROLLED BACK and is forbidden", refused.stdout)
        allowed = self.run_script(
            'source .forgejo/scripts/cd-rollback-ledger.sh && '
            'cd_ledger_check shizuha shizuha-hive-mcp dab2088'
        )
        self.assertEqual(allowed.returncode, 0, allowed.stdout)

    def test_promote_path_refuses_ledger_sha_without_mutating_live_state(self):
        seed = self.run_script(
            'source .forgejo/scripts/cd-rollback-ledger.sh && '
            'cd_rollback shizuha shizuha-hive-mcp hive-mcp '
            '"localhost:30500/shizuha-hive-mcp:mcp-dab2088" 59e9439 "candidate failed"'
        )
        self.assertEqual(seed.returncode, 0, seed.stderr)
        before = (self.state / "image").read_text()
        promote = self.run_script(
            'IMG=shizuha-hive-mcp; TARGET_IMAGE="localhost:30500/shizuha-hive-mcp:mcp-59e9439"; '
            'LAST_SUCCESSFUL_IMAGE="localhost:30500/shizuha-hive-mcp:mcp-dab2088"; '
            'source .forgejo/scripts/hive-mcp-release.sh && hive_mcp_promote_candidate'
        )
        self.assertNotEqual(promote.returncode, 0)
        self.assertIn("is on the rollback ledger", promote.stderr + promote.stdout)
        self.assertEqual((self.state / "image").read_text(), before, "live state must be untouched")


if __name__ == "__main__":
    unittest.main()
