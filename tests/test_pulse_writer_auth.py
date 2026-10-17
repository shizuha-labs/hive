"""Run both release writers through fresh-login and token-only caller sequences."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".forgejo/scripts"
COMMANDS = (
    ["record-deployment-event.sh", "hive", "hive-backend", "registry/hive:test"],
    ["record-rollout-failure.sh", "canary", "a" * 40, "b" * 40, "fixture evidence"],
)


class PulseWriterAuthTests(unittest.TestCase):
    def run_sequence(self, command, *, keys="renewable", login_fails=False, runs=2):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "kubectl").write_text(textwrap.dedent('''\
                #!/usr/bin/env python3
                import base64, json, os, sys
                args = " ".join(sys.argv)
                if "get deployment/hive -o json" in args:
                    print(json.dumps({
                        "metadata": {"name": "hive", "namespace": "shizuha-hive", "uid": "fixture-deployment", "resourceVersion": "1", "generation": 1},
                        "spec": {"replicas": 1, "template": {"spec": {"containers": [{"name": "hive", "image": "registry/hive:test"}]}}},
                        "status": {"replicas": 1, "readyReplicas": 1, "updatedReplicas": 1, "availableReplicas": 1, "observedGeneration": 1},
                    }))
                    raise SystemExit(0)
                values = {"token": "token-only"}
                if os.environ["TEST_KEYS"] == "renewable":
                    values.update(username="test-ci", password="test-password")
                for key, value in values.items():
                    if ".data." + key in args:
                        print(base64.b64encode(value.encode()).decode(), end="")
                        break
            '''))
            (path / "curl").write_text(textwrap.dedent('''\
                #!/usr/bin/env python3
                import json, os, pathlib, sys
                args = sys.argv[1:]
                state = pathlib.Path(os.environ["TEST_STATE"])
                generation = int(state.read_text()) if state.exists() else 0
                url = next(value for value in args if value.startswith("http"))
                method = args[args.index("-X") + 1] if "-X" in args else "GET"
                headers = [args[index + 1] for index, value in enumerate(args) if value == "-H"]
                auth = next((value for value in headers if value.startswith("Authorization:")), "")
                with open(os.environ["TEST_CALLS"], "a") as output:
                    output.write(json.dumps({"url": url, "authorization": auth}) + "\\n")
                code = 200
                if url.endswith("/api/auth/login/"):
                    if os.environ["TEST_LOGIN_FAILS"] == "1":
                        raise SystemExit(22)
                    generation += 1
                    state.write_text(str(generation))
                    result = {"tokens": {"access": "fresh-" + str(generation)}}
                elif url.endswith("/api/v2/alerts"):
                    result = {}
                else:
                    expected = "fresh-" + str(generation) if os.environ["TEST_KEYS"] == "renewable" else "token-only"
                    if auth != "Authorization: Bearer " + expected:
                        raise SystemExit(22)
                    if "/api/projects/" in url:
                        result = {"results": [{"key": "HIVE", "id": 7}]}
                    elif "/api/deployment-events/" in url:
                        result = {"id": "test-event", "state": "smoke_passed"}
                    elif method == "DELETE":
                        result, code = {}, 204
                    elif method == "POST":
                        result, code = {"id": 123, "item_key": "HIVE-123", "project_key": "HIVE",
                                        "workflow_name": "autonomous-bug", "assignment_group": "engineering"}, 201
                    else:
                        result = {"results": []}
                print(json.dumps(result), end="")
                if "-w" in args:
                    print("\\n" + str(code), end="")
            '''))
            for name in ("kubectl", "curl"):
                (path / name).chmod(0o755)
            env = {**os.environ, "PATH": f"{path}:{os.environ['PATH']}",
                   "TEST_STATE": str(path / "state"), "TEST_CALLS": str(path / "calls"),
                   "TEST_KEYS": keys, "TEST_LOGIN_FAILS": "1" if login_fails else "0",
                   "GITHUB_SHA": "a" * 40, "DEPLOYMENT_ENVIRONMENT": "live",
                   "DEPLOYMENT_EVENT_DRY_RUN": "0",
                   "PULSE_TOKEN": "expired-snapshot" if keys == "renewable" else ""}
            for name in ("PULSE_USERNAME", "PULSE_PASSWORD"):
                env.pop(name, None)
            results = [subprocess.run([SCRIPTS / command[0], *command[1:]], env=env,
                                      capture_output=True, text=True, check=False)
                       for _ in range(runs)]
            calls = [json.loads(line) for line in (path / "calls").read_text().splitlines()]
            for result in results:
                for secret in ("test-password", "expired-snapshot", "fresh-1", "fresh-2"):
                    self.assertNotIn(secret, result.stdout + result.stderr)
            return results, calls

    def test_each_writer_run_renews_access_despite_populated_snapshot(self):
        for command in COMMANDS:
            with self.subTest(script=command[0]):
                results, calls = self.run_sequence(command)
                for result in results:
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(sum(call["url"].endswith("/api/auth/login/") for call in calls), 2)
                auth = {call["authorization"] for call in calls if call["authorization"]}
                self.assertEqual(auth, {"Authorization: Bearer fresh-1", "Authorization: Bearer fresh-2"})

    def test_token_only_installations_still_work_without_login(self):
        for command in COMMANDS:
            with self.subTest(script=command[0]):
                results, calls = self.run_sequence(command, keys="token", runs=1)
                self.assertEqual(results[0].returncode, 0, results[0].stdout + results[0].stderr)
                self.assertFalse(any(call["url"].endswith("/api/auth/login/") for call in calls))

    def test_configured_login_failure_never_uses_stale_token(self):
        for command in COMMANDS:
            with self.subTest(script=command[0]):
                results, calls = self.run_sequence(command, login_fails=True, runs=1)
                self.assertNotEqual(results[0].returncode, 0)
                self.assertTrue(all(call["url"].endswith("/api/auth/login/") for call in calls))


if __name__ == "__main__":
    unittest.main()
