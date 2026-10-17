from pathlib import Path
import json
import os
import re
import stat
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".forgejo" / "workflows" / "deploy.yml"
RETIRE = ".forgejo/scripts/retire-canary-probe.sh"


class CanaryTeardownOrderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = WORKFLOW.read_text()
        cls.canary = (ROOT / ".forgejo/scripts/ring-release-canary.sh").read_text()
        cls.fleet = (ROOT / ".forgejo/scripts/ring-release-fleet.sh").read_text()
        cls.cleanup = (ROOT / ".forgejo/scripts/ring-cleanup-canary.sh").read_text()

    def _block(self, start: str, end: str) -> str:
        begin = self.workflow.index(start)
        return self.workflow[begin : self.workflow.index(end, begin)]

    def _assert_probe_fenced_before_targets(self, block: str):
        retire = block.index(RETIRE)
        deploy = block.index("delete deploy hive-canary")
        service = block.index("delete svc hive-canary")
        self.assertLess(retire, deploy)
        self.assertLess(retire, service)

    def test_r1_failure_fences_probe_before_targets(self):
        block = self.canary[
            self.canary.index("r1_failure() {") :
            self.canary.index(".forgejo/scripts/render-ring-manifest.sh")
        ]
        self._assert_probe_fenced_before_targets(block)
        self.assertIn(f"if {RETIRE}; then", block)

        recovery = (ROOT / ".forgejo/scripts/ring-recovery.py").read_text()
        retire = recovery.index("retire-canary-probe.sh")
        service = recovery.index('delete_preconditioned(fence, "shizuha-hive", "service", "hive-canary")')
        deployment = recovery.index('delete_preconditioned(fence, "shizuha-hive", "deployment", "hive-canary")')
        self.assertLess(retire, service)
        self.assertLess(retire, deployment)

    def test_r2_failure_fences_probe_before_targets(self):
        self.assertNotIn("delete deploy hive-canary", self.fleet)
        self.assertNotIn("delete svc hive-canary", self.fleet)

    def test_r2_success_fences_probe_before_targets(self):
        self._assert_probe_fenced_before_targets(self.cleanup)

    def test_preclean_also_fences_any_stale_probe(self):
        block = self.canary[
            self.canary.index(".forgejo/scripts/render-ring-manifest.sh k8s/rollout/hive-canary.yaml.tpl") :
            self.canary.index("# The cluster-resident watchdog survives")
        ]
        self.assertLess(block.index(RETIRE), block.index("delete deploy hive-canary"))

    def test_r0_fences_staging_probe_before_workload_replacement(self):
        block = self._block(
            "          # Retire the old build-attributed target",
            "          kubectl -n staging rollout status deploy/hive",
        )
        retire = f"{RETIRE} staging hive.staging.svc.cluster.local"
        self.assertIn(retire, block)
        self.assertLess(block.index(retire), block.index("delete deploy hive --wait=true"))
        self.assertLess(block.index(retire), block.index("kubectl apply -f /tmp/hive-staging-app.yaml"))
        self.assertNotIn("kubectl -n monitoring delete probe", block)

    def test_fence_waits_for_prometheus_active_target_absence(self):
        script = (ROOT / RETIRE).read_text()
        self.assertIn("delete probe", script)
        self.assertIn("--wait=true", script)
        self.assertIn("shizuha_io_probe_retire_reconcile", script)
        self.assertIn('get endpointslice', script)
        self.assertIn('kubernetes.io/service-name=${PROMETHEUS_SERVICE}', script)
        self.assertIn('exec "$pod"', script)
        self.assertIn('timeout --signal=KILL', script)
        self.assertIn('kubectl --request-timeout=', script)
        self.assertIn("/api/v1/scrape_pools", script)
        self.assertIn("SCRAPE_POOL_PREFIX", script)
        self.assertIn('RELEASE_RING="${1:-canary}"', script)
        self.assertIn("hive-canary.shizuha-hive", script)
        self.assertIn("hive.staging.svc.cluster.local", script)
        self.assertIn('CANARY_PROBE_RETIRE_TIMEOUT_SECONDS:-300', script)
        self.assertIn('PROMETHEUS_READ_TIMEOUT_SECONDS:-15', script)
        self.assertIn('now="$EPOCHREALTIME"', script)
        self.assertNotIn("deadline=$((SECONDS + TIMEOUT_SECONDS))", script)
        self.assertIn("exit 1", script)

    def test_fleet_release_overwrites_verified_broker_pin(self):
        settings = (ROOT / 'hive_project/settings.py').read_text()
        source = re.search(r"FLEET_BROKER_IMAGE = os.environ.get\(\s*'FLEET_BROKER_IMAGE',\s*'([^']+)'", settings)
        self.assertIsNotNone(source)
        image = source.group(1)
        self.assertRegex(image, r'^localhost:30500/mcp-auth-proxy:src-[0-9a-f]{12}$')
        self.assertIn(f'ensure_env(container, "FLEET_BROKER_IMAGE", "{image}")', self.fleet)
        for filename in ('shizuha-hive.yaml', 'hive-backend-live.yaml'):
            manifest = (ROOT / 'k8s' / filename).read_text()
            pins = re.findall(r'name: FLEET_BROKER_IMAGE\s+value: (\S+)', manifest)
            self.assertEqual(pins, [image], filename)

    def run_api_producer_roll(self, desired):
        """Execute the real API rollout block against a stale live env fixture."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / 'state.json'
            state.write_text(json.dumps({'FLEET_BROKER_IMAGE': 'old-broker', 'UNRELATED': 'keep'}))
            patch = root / 'patch.json'
            patch.write_text(json.dumps({'spec': {'template': {'spec': {'containers': [
                {'name': 'hive', 'env': desired},
            ]}}}}))
            calls = root / 'calls'
            kubectl = root / '.forgejo/scripts/kubectl'
            kubectl.parent.mkdir(parents=True)
            kubectl.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['CALLS'], 'a') as log: log.write(' '.join(args) + '\\n')
if 'env' in args:
    path = pathlib.Path(os.environ['STATE'])
    state = json.loads(path.read_text())
    for arg in args:
        if '=' in arg and not arg.startswith('--'):
            key, value = arg.split('=', 1)
            state[key] = value
    path.write_text(json.dumps(state))
''')
            kubectl.chmod(0o755)
            body = self.fleet.split('# hive-api serves the same Django code', 1)[1]
            body = '# hive-api serves the same Django code' + body.split('# Separate codex-refresh Deploy:', 1)[0]
            body = body.replace('/tmp/hive-codex-refresh-deploy-patch.json', str(patch))
            result = subprocess.run(['bash', '-ec', body], cwd=root, capture_output=True, text=True,
                env={**os.environ, 'TAG': 'fixture-source', 'STATE': str(state), 'CALLS': str(calls)})
            return result, json.loads(state.read_text()), calls.read_text() if calls.exists() else ''

    def test_api_roll_replaces_old_broker_env_from_rendered_source_without_fifth_pin(self):
        for pin in ('qualified-before', 'qualified-after-publisher'):
            result, state, calls = self.run_api_producer_roll([{'name': 'FLEET_BROKER_IMAGE', 'value': pin},
                {'name': 'FLEET_COORDINATOR_OAUTH_SECRET_NAME', 'value': 'hive-coordinator-broker-oauth'}])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(state['FLEET_BROKER_IMAGE'], pin)
            self.assertEqual(state['FLEET_COORDINATOR_OAUTH_SECRET_NAME'], 'hive-coordinator-broker-oauth')
            self.assertEqual(state['UNRELATED'], 'keep')
            self.assertIn('set image deploy/hive-api', calls)
            self.assertIn('rollout status', calls)

    def test_api_roll_refuses_missing_or_ambiguous_oauth_authority_before_any_write(self):
        for values in ([], [{'name': 'FLEET_COORDINATOR_OAUTH_SECRET_NAME', 'value': ''}],
                       [{'name': 'FLEET_COORDINATOR_OAUTH_SECRET_NAME', 'value': 'one'},
                        {'name': 'FLEET_COORDINATOR_OAUTH_SECRET_NAME', 'value': 'two'}]):
            result, state, calls = self.run_api_producer_roll(
                [{'name': 'FLEET_BROKER_IMAGE', 'value': 'qualified'}] + values)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(calls, '')

    def test_api_roll_refuses_missing_or_ambiguous_producer_pin_before_api_mutation(self):
        for env in ([], [{'name': 'FLEET_BROKER_IMAGE', 'value': ''}],
                    [{'name': 'FLEET_BROKER_IMAGE', 'value': 'one'}, {'name': 'FLEET_BROKER_IMAGE', 'value': 'two'}]):
            result, state, calls = self.run_api_producer_roll(env)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(state['FLEET_BROKER_IMAGE'], 'old-broker')
            self.assertEqual(calls, '')


PREMERGE_WORKFLOW = ROOT / ".forgejo" / "workflows" / "premerge.yml"


class CiGateParityTests(unittest.TestCase):
    """HIVE-1512: premerge (the PR gate) must run everything deploy (the push
    gate) runs.

    While it was a strict subset, a PR could pass premerge and still red `main`
    on push — and because the deploy stages are gated on `test`, that shuts the
    whole ring while the PR that caused it shows green. Whoever adds a check to
    one workflow must add it to the other; this pins that.
    """

    @staticmethod
    def _static_and_suite_steps(workflow_path: Path) -> list:
        """The `/app/.venv/bin/python …` invocations in the backend test job."""
        text = workflow_path.read_text()
        begin = text.index("# Static + full suite before auto-deploy")
        end = text.index("volumeMounts:", begin)
        return [
            line.strip()
            for line in text[begin:end].splitlines()
            if line.strip().startswith("/app/.venv/bin/python")
        ]

    def test_premerge_runs_every_check_deploy_runs(self):
        deploy_steps = self._static_and_suite_steps(WORKFLOW)
        premerge_steps = self._static_and_suite_steps(PREMERGE_WORKFLOW)
        self.assertTrue(deploy_steps, "no steps parsed from deploy.yml")
        missing = [s for s in deploy_steps if s not in premerge_steps]
        self.assertEqual(
            missing, [],
            "premerge.yml is missing checks that deploy.yml runs, so a PR can go "
            "green and still red main on push (HIVE-1512):\n  "
            + "\n  ".join(missing),
        )

    def test_both_gates_run_the_standalone_test_scripts(self):
        """Positive control: the parity check above is vacuous if neither
        workflow runs these, so assert their presence directly."""
        for name, path in (("deploy.yml", WORKFLOW), ("premerge.yml", PREMERGE_WORKFLOW)):
            steps = " ".join(self._static_and_suite_steps(path))
            for script in (
                "tests/test_hive707_canary_teardown.py",
                "tests/test_hive_api_service_selector.py",
                "tests/test_plat4086_rollout_rings.py RingFenceContractTests",
            ):
                self.assertIn(script, steps, f"{name} does not run {script}")

    def test_premerge_gate_changes_retrigger_the_push_gate(self):
        """A parity repair must create a fresh push run for the failed SHA."""
        deploy_text = WORKFLOW.read_text()
        push_paths = deploy_text[
            deploy_text.index("  push:"):deploy_text.index("concurrency:")
        ]
        self.assertIn("'.forgejo/workflows/premerge.yml'", push_paths)


# HIVE-1512: the fence deadline is Bash `SECONDS`, an integer shell-uptime
# counter, so a budget of N yields an effective window of (N-1, N] seconds — a
# budget of 2 can be as little as 1s of wall clock. Every poll iteration spawns
# several fake-`kubectl` processes (bash + python3); on a contended CI runner
# those cost whole seconds, so the loop can expire before completing its FIRST
# discovery read. That is the run-1448 failure: three success-path fixtures
# died with "Prometheus replica discovery has not completed" while the same
# fence completes in ~0.07s locally.
#
# The budget is not what these fixtures test, so size it per assertion shape:
#
#   success-path  — exits as soon as discovery succeeds, so a generous budget
#                   costs nothing and removes the spawn-latency coupling.
#   fail-closed   — must run the deadline OUT; expiry is the assertion, so the
#                   budget stays tight or every one of them pays it in full.
#   multi-poll    — fails closed AND asserts >2 reconcile patches, i.e. needs
#                   at least three poll iterations before expiry, so the bare
#                   fail-closed budget is not enough for it.
FENCE_TIMEOUT_SUCCESS_SECONDS = 30
FENCE_TIMEOUT_FAIL_CLOSED_SECONDS = 2
FENCE_TIMEOUT_MULTI_POLL_SECONDS = 8


class CanaryTeardownHaFenceTests(unittest.TestCase):
    def _run_fence(
        self, endpoints: str, active_endpoint: str = "", response_mode: str = "valid",
        release_ring: str = "canary", target_needle: str = "hive-canary.shizuha-hive",
        timeout_seconds: int = FENCE_TIMEOUT_FAIL_CLOSED_SECONDS,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            kubectl = tmp_path / "kubectl"
            kubectl.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$FAKE_KUBECTL_LOG\"\n"
                "if [[ \" $* \" == *\" delete probe \"* ]]; then exit 0; fi\n"
                "if [[ \" $* \" == *\" apply -f - \"* ]]; then cat >/dev/null; exit 0; fi\n"
                "if [[ \" $* \" == *\" get probe \"* ]]; then printf '%s\\n' 'probe.monitoring.coreos.com/probe-shizuha-hive-fleet'; exit 0; fi\n"
                "if [[ \" $* \" == *\" patch probe.monitoring.coreos.com/\"* ]]; then exit 0; fi\n"
                "if [[ \" $* \" == *\" get endpointslice \"* ]]; then\n"
                "  printf '%s\\n' \"$FAKE_ENDPOINTS\" | python3 -c '\n"
                "import json, sys\n"
                "names = [line.strip() for line in sys.stdin if line.strip()]\n"
                "print(json.dumps({\"items\": [{\"endpoints\": [{\"conditions\": {\"ready\": True}, \"targetRef\": {\"kind\": \"Pod\", \"name\": name}} for name in names]}]}))\n"
                "'\n"
                "  exit 0\n"
                "fi\n"
                "if [[ \" $* \" != *\" exec \"* ]]; then exit 1; fi\n"
                "case \"$FAKE_RESPONSE_MODE\" in\n"
                "  hung) sleep 10; exit 0 ;;\n"
                "  malformed) printf '%s\\n' '<html>not prometheus'; exit 0 ;;\n"
                "  error) printf '%s\\n' '{\"status\":\"error\",\"error\":\"query failed\"}'; exit 0 ;;\n"
                "  missing-pools) printf '%s\\n' '{\"status\":\"success\",\"data\":{}}'; exit 0 ;;\n"
                "  other-ring) printf '%s\\n' '{\"status\":\"success\",\"data\":{\"scrapePools\":[\"probe/monitoring/probe-shizuha-hive-fleet-deadbeef\"]}}'; exit 0 ;;\n"
                "esac\n"
                "if [[ -n \"$FAKE_ACTIVE_ENDPOINT\" && \" $* \" == *\" exec $FAKE_ACTIVE_ENDPOINT \"* ]]; then\n"
                "  printf '%s\\n' \"{\\\"status\\\":\\\"success\\\",\\\"data\\\":{\\\"scrapePools\\\":[\\\"$FAKE_SCRAPE_POOL\\\"]}}\"\n"
                "else\n"
                "  printf '%s\\n' '{\"status\":\"success\",\"data\":{\"scrapePools\":[]}}'\n"
                "fi\n"
            )
            kubectl.chmod(kubectl.stat().st_mode | stat.S_IXUSR)

            env = os.environ.copy()
            env.update({
                "PATH": f"{tmp}{os.pathsep}{env['PATH']}",
                "FAKE_ENDPOINTS": endpoints,
                "FAKE_ACTIVE_ENDPOINT": active_endpoint,
                "FAKE_RESPONSE_MODE": response_mode,
                "FAKE_SCRAPE_POOL": f"probe/monitoring/probe-shizuha-hive-{release_ring}-deadbeef",
                "FAKE_KUBECTL_LOG": str(tmp_path / "kubectl.log"),
                "PROMETHEUS_EXPECTED_REPLICAS": "2",
                "PROMETHEUS_READ_TIMEOUT_SECONDS": "1",
                "PROMETHEUS_RECONCILE_INTERVAL_SECONDS": "0",
                # See FENCE_TIMEOUT_* above — sized per assertion shape, because
                # the integer SECONDS deadline costs a full second of the budget
                # and each poll iteration spawns several subprocesses.
                "CANARY_PROBE_RETIRE_TIMEOUT_SECONDS": str(timeout_seconds),
                "CANARY_PROBE_RETIRE_POLL_SECONDS": "0",
            })
            started = time.monotonic()
            result = subprocess.run(
                [str(ROOT / RETIRE), release_ring, target_needle], env=env, text=True,
                capture_output=True, timeout=timeout_seconds + 10,
            )
            result.elapsed = time.monotonic() - started
            result.kubectl_log = (tmp_path / "kubectl.log").read_text()
            return result

    def test_split_view_fails_closed_when_one_replica_still_has_target(self):
        result = self._run_fence(
            "10.0.0.1\n10.0.0.2", active_endpoint="10.0.0.2",
            timeout_seconds=FENCE_TIMEOUT_MULTI_POLL_SECONDS,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertGreater(result.kubectl_log.count("--type=merge"), 2)

    def test_staging_split_view_fails_closed_and_selects_only_staging_probe(self):
        result = self._run_fence(
            "10.0.0.1\n10.0.0.2", active_endpoint="10.0.0.2",
            release_ring="staging", target_needle="hive.staging.svc.cluster.local",
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("service=hive,release_ring=staging", result.kubectl_log)

    def test_probe_retirement_forces_event_driven_operator_reconcile(self):
        result = self._run_fence(
            "10.0.0.1\n10.0.0.2", timeout_seconds=FENCE_TIMEOUT_SUCCESS_SECONDS,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "apply -f -",
            result.kubectl_log,
        )
        self.assertIn("patch probe.monitoring.coreos.com/probe-shizuha-hive-reconcile-canary-", result.kubectl_log)
        self.assertIn("shizuha_io_probe_retire_reconcile", result.kubectl_log)
        self.assertIn("--type=merge", result.kubectl_log)
        self.assertGreaterEqual(result.kubectl_log.count("--type=merge"), 1)
        self.assertIn("delete probe.monitoring.coreos.com/probe-shizuha-hive-reconcile-canary-", result.kubectl_log)
        self.assertNotIn("get prometheus", result.kubectl_log)

    def test_missing_replica_fails_closed(self):
        result = self._run_fence("10.0.0.1")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_all_replicas_absent_succeeds(self):
        result = self._run_fence(
            "10.0.0.1\n10.0.0.2", timeout_seconds=FENCE_TIMEOUT_SUCCESS_SECONDS,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("all 2 Prometheus replicas", result.stdout)

    def test_other_ring_scrape_pool_does_not_block_canary_retirement(self):
        result = self._run_fence(
            "10.0.0.1\n10.0.0.2", response_mode="other-ring",
            timeout_seconds=FENCE_TIMEOUT_SUCCESS_SECONDS,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_malformed_http_200_fails_closed(self):
        result = self._run_fence("10.0.0.1\n10.0.0.2", response_mode="malformed")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_prometheus_error_payload_fails_closed(self):
        result = self._run_fence("10.0.0.1\n10.0.0.2", response_mode="error")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_missing_scrape_pools_fails_closed(self):
        result = self._run_fence("10.0.0.1\n10.0.0.2", response_mode="missing-pools")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_hung_pod_exec_is_bounded_by_fence_deadline(self):
        # Keeps the TIGHT budget deliberately: the deadline is this fixture's
        # subject, not incidental to it. The fake exec sleeps 10s per call, so
        # an unfenced loop is unbounded — a ceiling of budget+6 still
        # discriminates while absorbing runner spawn latency.
        result = self._run_fence("10.0.0.1\n10.0.0.2", response_mode="hung")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertLess(
            result.elapsed,
            FENCE_TIMEOUT_FAIL_CLOSED_SECONDS + 6,
            result.stdout + result.stderr,
        )


if __name__ == "__main__":
    unittest.main()
