import json
import os
import re
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from test_hive_mcp_deployment_contract import declared_deployment, live_deployment


ROOT = Path(__file__).resolve().parents[1]


class HiveMcpCiJobCleanupPolicyTests(unittest.TestCase):
    def test_hive_mcp_jobs_expire_and_builds_clean_up(self):
        workflow = (ROOT / ".forgejo/workflows/deploy-hive-mcp.yml").read_text()
        ttls = [
            int(value)
            for value in re.findall(
                r"ttlSecondsAfterFinished:\s*(\d+)", workflow
            )
        ]
        self.assertEqual(workflow.count("kind: Job"), len(ttls))
        self.assertTrue(ttls)
        self.assertTrue(all(ttl <= 300 for ttl in ttls))
        self.assertEqual(
            workflow.count("- --cleanup"),
            workflow.count("image: gcr.io/kaniko-project/executor:v1.23.2"),
        )
        self.assertIn("wait_job amd64 &", workflow)
        self.assertIn("wait_job arm64 &", workflow)
        self.assertNotRegex(
            workflow, r"(?m)^\s*wait_job (?:amd64|arm64)$"
        )

    def test_rollout_failure_is_diagnostic_and_restores_successful_image(self):
        workflow = (ROOT / ".forgejo/workflows/deploy-hive-mcp.yml").read_text()
        release_script = (
            ROOT / ".forgejo/scripts/hive-mcp-release.sh"
        ).read_text()

        self.assertIn("shizuha.io/last-successful-image", workflow)
        self.assertIn("source .forgejo/scripts/hive-mcp-release.sh", workflow)
        self.assertIn("- '.forgejo/scripts/hive-mcp-release.sh'", workflow)
        self.assertIn("hive_mcp_load_release_state", workflow)
        self.assertIn("hive_mcp_promote_candidate", workflow)
        self.assertIn("hive_mcp_fail_candidate", workflow)
        self.assertIn("timeout --signal=TERM --kill-after=2s", release_script)
        self.assertIn('--request-timeout="${request_timeout}"', release_script)

        rollout = workflow.index("hive_mcp_promote_candidate")
        success_annotation = workflow.index(
            'shizuha.io/last-successful-sha="${{ github.sha }}"'
        )
        self.assertLess(rollout, success_annotation)

    def _run_release_harness(
        self, *, last_successful_image="", fail_target=False, slow_diagnostics=False,
        invalid_contract=False, patch_conflict=False,
    ):
        release_script = ROOT / ".forgejo/scripts/hive-mcp-release.sh"
        target = "localhost:30500/shizuha-hive-mcp:mcp-bbbbbbb"
        anchor = last_successful_image
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state = tmp_path / "state"
            calls = tmp_path / "calls"
            state.write_text("localhost:30500/shizuha-hive-mcp:mcp-ccccccc")
            declaration = declared_deployment()
            if invalid_contract:
                declaration['spec']['template']['spec']['containers'][0]['env'][0]['value'] = 'false'
            manifest = tmp_path / "declared.json"
            manifest.write_text(json.dumps(declaration))
            current = tmp_path / "current.json"
            current.write_text(json.dumps(live_deployment(state.read_text())))
            captured_patch = tmp_path / "patch.json"
            kubectl = tmp_path / "kubectl"
            kubectl.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    set -eu
                    echo "$*" >> "$CALLS_FILE"
                    case "$*" in
                      *last-successful-image*) printf '%s' "$LAST_SUCCESSFUL_IMAGE_MOCK"; exit 0 ;;
                      *last-successful-sha*) printf '%s' "$LAST_SUCCESSFUL_SHA_MOCK"; exit 0 ;;
                      *spec.template.spec.containers*) cat "$STATE_FILE"; exit 0 ;;
                    esac
                    if [[ " $* " == *" get deploy "* ]] && [[ " $* " == *" -o json "* ]]; then
                      cat "$CURRENT_JSON"
                      exit 0
                    fi
                    if [[ " $* " == *" patch "* ]] && [[ " $* " == *" --patch-file "* ]]; then
                      cp "${!#}" "$CAPTURED_PATCH"
                      if [ "$PATCH_CONFLICT" = 1 ]; then exit 1; fi
                      python3 -c 'import json,os,pathlib; patch=json.loads(pathlib.Path(os.environ["CAPTURED_PATCH"]).read_text()); pathlib.Path(os.environ["STATE_FILE"]).write_text(patch[2]["value"])'
                      exit 0
                    fi
                    if [[ " $* " == *" set image "* ]]; then
                      value="${!#}"
                      printf '%s' "${value#*=}" > "$STATE_FILE"
                      exit 0
                    fi
                    if [[ " $* " == *" rollout status "* ]]; then
                      current="$(cat "$STATE_FILE")"
                      if [ "$FAIL_TARGET" = 1 ] && [ "$current" = "$TARGET_IMAGE_MOCK" ]; then
                        exit 1
                      fi
                      exit 0
                    fi
                    if [ "$SLOW_DIAGNOSTICS" = 1 ] && [[ " $* " == *" describe deploy "* ]]; then
                      sleep 5
                    fi
                    # PLAT-7708 ledger emulation: no ledger ConfigMap in this
                    # harness world -> empty ledger (deploys allowed).
                    if [[ " $* " == *" cm/cd-rollback-ledger -o json"* ]]; then
                      echo '{"apiVersion":"v1","kind":"ConfigMap","data":{}}'
                      exit 0
                    fi
                    if [[ " $* " == *" cm/cd-rollback-ledger"* ]]; then
                      echo 'Error from server (NotFound): configmaps "cd-rollback-ledger" not found'
                      exit 1
                    fi
                    exit 0
                    """
                )
            )
            kubectl.chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{tmp}:{env['PATH']}",
                    "CALLS_FILE": str(calls),
                    "STATE_FILE": str(state),
                    "HIVE_MCP_MANIFEST_JSON": str(manifest),
                    "CURRENT_JSON": str(current),
                    "CAPTURED_PATCH": str(captured_patch),
                    "PATCH_CONFLICT": "1" if patch_conflict else "0",
                    "LAST_SUCCESSFUL_IMAGE_MOCK": anchor,
                    "LAST_SUCCESSFUL_SHA_MOCK": "",
                    "FAIL_TARGET": "1" if fail_target else "0",
                    "SLOW_DIAGNOSTICS": "1" if slow_diagnostics else "0",
                    "TARGET_IMAGE_MOCK": target,
                    "HIVE_MCP_DIAGNOSTIC_TIMEOUT_SECONDS": "1",
                    "HIVE_MCP_KUBECTL_REQUEST_TIMEOUT": "1s",
                }
            )
            command = textwrap.dedent(
                f"""\
                set -euo pipefail
                IMG=shizuha-hive-mcp
                TARGET_IMAGE={target}
                source {release_script}
                hive_mcp_load_release_state
                hive_mcp_promote_candidate
                """
            )
            started = time.monotonic()
            result = subprocess.run(
                ["bash", "-c", command],
                env=env,
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
            self.captured_patch = json.loads(captured_patch.read_text()) if captured_patch.exists() else None
            return (
                result,
                calls.read_text() if calls.exists() else "",
                state.read_text(),
                time.monotonic() - started,
            )

    def test_missing_anchor_fails_before_candidate_mutation(self):
        result, calls, state, _ = self._run_release_harness()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to mutate the Deployment", result.stdout)
        self.assertNotIn(" set image ", f" {calls} ")
        self.assertEqual(state, "localhost:30500/shizuha-hive-mcp:mcp-ccccccc")

    def test_diagnostic_timeout_still_rolls_back_and_exits_red(self):
        anchor = "localhost:30500/shizuha-hive-mcp:mcp-aaaaaaa"
        result, calls, state, elapsed = self._run_release_harness(
            last_successful_image=anchor,
            fail_target=True,
            slow_diagnostics=True,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("diagnostics exceeded 1s", result.stdout)
        self.assertIn(f"hive-mcp={anchor}", calls)
        self.assertIn("Rollback to", result.stdout)
        self.assertIn("converged", result.stdout)
        self.assertEqual(state, anchor)
        self.assertLess(elapsed, 4)

    def test_real_candidate_caller_converges_declared_auth_with_image(self):
        result, calls, state, _ = self._run_release_harness(
            last_successful_image="localhost:30500/shizuha-hive-mcp:mcp-aaaaaaa",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(state, "localhost:30500/shizuha-hive-mcp:mcp-bbbbbbb")
        self.assertNotIn(" set image ", f" {calls} ")
        self.assertEqual(self.captured_patch[0]['path'], '/metadata/uid')
        self.assertEqual(self.captured_patch[1]['path'], '/metadata/resourceVersion')
        environment = {entry['name']: entry for entry in self.captured_patch[3]['value']}
        self.assertEqual(environment['SHIZUHA_MCP_OAUTH_ENFORCE']['value'], 'true')
        self.assertIn('secretKeyRef', environment['SHIZUHA_HIVE_JWT_TOKEN']['valueFrom'])

    def test_invalid_declaration_refuses_candidate_before_patch(self):
        result, calls, state, _ = self._run_release_harness(
            last_successful_image="localhost:30500/shizuha-hive-mcp:mcp-aaaaaaa",
            invalid_contract=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(self.captured_patch)
        self.assertNotIn(" rollout status ", f" {calls} ")
        self.assertEqual(state, "localhost:30500/shizuha-hive-mcp:mcp-ccccccc")

    def test_live_identity_conflict_fails_without_overwriting_or_rollback(self):
        result, calls, state, _ = self._run_release_harness(
            last_successful_image="localhost:30500/shizuha-hive-mcp:mcp-aaaaaaa",
            patch_conflict=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNotNone(self.captured_patch)
        self.assertNotIn(" rollout status ", f" {calls} ")
        self.assertNotIn(" set image ", f" {calls} ")
        self.assertEqual(state, "localhost:30500/shizuha-hive-mcp:mcp-ccccccc")


if __name__ == "__main__":
    unittest.main()
