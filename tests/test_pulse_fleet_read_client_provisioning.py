"""Exercise CI's create/read/register sequence, including reruns and races.

The remote registration program executes against a small model adapter, so
tests cover the actual program sent to ID without requiring an ID deployment.
"""
from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest import mock

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".forgejo/scripts"
SPEC = importlib.util.spec_from_file_location(
    "pulse_fleet_client_bootstrap", SCRIPTS / "ensure-pulse-fleet-read-client.py",
)
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)
TEST_SECRET = "test-only-hive-pulse-client-secret-0123456789abcdef"


def secret_resource(secret=TEST_SECRET):
    return {"data": {key: base64.b64encode(value.encode()).decode() for key, value in {
        "client-id": bootstrap.CLIENT_ID, "client-secret": secret, "token-url": bootstrap.TOKEN_URL,
    }.items()}}


def client_row(**overrides):
    values = {
        "client_id": bootstrap.CLIENT_ID, "client_name": "Hive Pulse Fleet Reader",
        "client_secret_hash": hashlib.sha256(TEST_SECRET.encode()).hexdigest(),
        "client_type": "confidential", "is_system": True,
        "scope": bootstrap.SCOPE, "grant_types": ["client_credentials"],
        "redirect_uris": [], "response_types": [], "access_revoked_before": None,
    }
    values.update(overrides)
    return types.SimpleNamespace(**values)


class ClusterAdapter:
    def __init__(self, secret=None, client=None):
        self.secret = secret
        self.client = client
        self.calls = []
        self.creates = 0
        self.race_secret = None
        self.read_error = False

    def select_for_update(self):
        return self

    def filter(self, **kwargs):
        if kwargs != {"client_id": bootstrap.CLIENT_ID}:
            raise AssertionError(kwargs)
        return self

    def first(self):
        return self.client

    def create(self, **kwargs):
        if self.client is not None:
            raise AssertionError("existing verifier must never be overwritten")
        self.client = client_row(**kwargs)
        self.creates += 1

    def run(self, args, *, stdin=None, operation="kubectl"):
        self.calls.append((args, stdin))
        if "get" in args:
            if self.read_error:
                raise bootstrap.ProvisioningError("read Hive Pulse Secret failed (exit 1)")
            return json.dumps(self.secret) if self.secret else ""
        if args == ["create", "-f", "-"]:
            if self.race_secret:
                self.secret = secret_resource(self.race_secret)
                self.client = client_row(
                    client_secret_hash=hashlib.sha256(self.race_secret.encode()).hexdigest(),
                )
                raise bootstrap.ProvisioningError("create Hive Pulse Secret failed (exit 1)")
            if self.secret:
                raise AssertionError("existing secret must never be overwritten")
            self.secret = json.loads(stdin)
            return "created"
        if "exec" in args:
            models = types.ModuleType("identity.models")
            models.OAuthClient = types.SimpleNamespace(
                objects=self, CLIENT_TYPE_CONFIDENTIAL="confidential",
            )
            db = types.ModuleType("django.db")
            db.transaction = types.SimpleNamespace(atomic=contextlib.nullcontext)
            output = io.StringIO()
            with mock.patch.dict(sys.modules, {"identity.models": models, "django.db": db}):
                with contextlib.redirect_stdout(output):
                    exec(compile(stdin, "id-client-registration", "exec"), {})
            return output.getvalue()
        raise AssertionError(args)


class ClientProvisioningTests(unittest.TestCase):
    def test_ci_reruns_exchange_existing_secret_without_create_or_exec(self):
        cluster = ClusterAdapter(secret=secret_resource(), client=client_row())
        grants = []

        def exchange(values):
            grants.append(dict(values))
            return {"access_token": f"fresh-{len(grants)}", "token_type": "Bearer",
                    "scope": bootstrap.SCOPE, "expires_in": 900}

        for _ in range(2):
            bootstrap.ensure_client(run=cluster.run, exchange=exchange)
        self.assertEqual(len(grants), 2)
        self.assertEqual(grants[0]["client-secret"], TEST_SECRET)
        self.assertTrue(all("get" in args for args, _ in cluster.calls))
        self.assertEqual(cluster.creates, 0)

    def test_ci_missing_secret_requires_explicit_administrator_bootstrap(self):
        cluster = ClusterAdapter()
        with self.assertRaisesRegex(bootstrap.ProvisioningError, "administrator.*--bootstrap"):
            bootstrap.ensure_client(run=cluster.run)
        self.assertEqual(len(cluster.calls), 1)
        self.assertIsNone(cluster.secret)

    def test_ci_rejects_bad_grants_and_never_repairs_them(self):
        valid = {"access_token": "test-token", "token_type": "Bearer",
                 "scope": bootstrap.SCOPE, "expires_in": 900}
        invalid = (None, [], {**valid, "access_token": ""}, {**valid, "expires_in": 0},
                   {**valid, "expires_in": 901}, {**valid, "expires_in": True},
                   {**valid, "scope": "pulse:*"}, {**valid, "token_type": "refresh"},
                   {**valid, "refresh_token": "forbidden"})
        for result in invalid:
            with self.subTest(result=result):
                cluster = ClusterAdapter(secret=secret_resource())
                with self.assertRaises(bootstrap.ProvisioningError):
                    bootstrap.ensure_client(run=cluster.run, exchange=lambda _: result)
                self.assertEqual(len(cluster.calls), 1)
                self.assertEqual(cluster.creates, 0)
        cluster = ClusterAdapter(secret=secret_resource())
        with mock.patch.object(bootstrap, "exchange_token") as exchange:
            exchange.side_effect = bootstrap.ProvisioningError("ID OAuth client verification rejected (HTTP 400)")
            with self.assertRaisesRegex(bootstrap.ProvisioningError, "HTTP 400"):
                bootstrap.ensure_client(run=cluster.run, exchange=exchange)
        self.assertEqual(len(cluster.calls), 1)

    def test_two_administrator_bootstraps_preserve_secret_and_verifier(self):
        cluster = ClusterAdapter()
        with mock.patch.object(bootstrap.secrets, "token_urlsafe", return_value=TEST_SECRET):
            bootstrap.ensure_client(bootstrap=True, run=cluster.run)
        original_secret = copy.deepcopy(cluster.secret)
        original_client = copy.deepcopy(vars(cluster.client))
        bootstrap.ensure_client(bootstrap=True, run=cluster.run)
        self.assertEqual(cluster.secret, original_secret)
        self.assertEqual(vars(cluster.client), original_client)
        self.assertEqual(cluster.creates, 1)
        self.assertEqual(sum(args == ["create", "-f", "-"] for args, _ in cluster.calls), 1)
        for args, stdin in cluster.calls:
            self.assertNotIn(TEST_SECRET, repr(args))
            if "exec" in args:
                self.assertNotIn(TEST_SECRET, stdin)
                self.assertIn("hive-pulse-fleet-read", stdin)

    def test_existing_secret_recreates_absent_id_registration_only(self):
        cluster = ClusterAdapter(secret=secret_resource())
        bootstrap.ensure_client(bootstrap=True, run=cluster.run)
        self.assertEqual(cluster.creates, 1)
        self.assertFalse(any(args == ["create", "-f", "-"] for args, _ in cluster.calls))

    def test_existing_client_without_secret_refuses_rotation(self):
        cluster = ClusterAdapter(client=client_row())
        with self.assertRaisesRegex(bootstrap.ProvisioningError, "refusing implicit rotation"):
            bootstrap.ensure_client(bootstrap=True, run=cluster.run)
        self.assertIsNone(cluster.secret)
        self.assertEqual(cluster.creates, 0)

    def test_existing_verifier_revocation_and_grants_are_immutable(self):
        cases = (
            ({"client_secret_hash": "different-verifier"}, "verifier mismatch"),
            ({"access_revoked_before": "emergency-revocation"}, "revoked"),
            ({"scope": "pulse:*"}, "contract mismatch"),
            ({"grant_types": ["password"]}, "contract mismatch"),
            ({"is_system": False}, "contract mismatch"),
            ({"client_type": "public"}, "contract mismatch"),
        )
        for changes, reason in cases:
            with self.subTest(changes=changes):
                cluster = ClusterAdapter(secret=secret_resource(), client=client_row(**changes))
                original = copy.deepcopy(vars(cluster.client))
                with self.assertRaisesRegex(RuntimeError, reason):
                    bootstrap.ensure_client(bootstrap=True, run=cluster.run)
                self.assertEqual(vars(cluster.client), original)
                self.assertEqual(cluster.creates, 0)

    def test_concurrent_create_adopts_and_verifies_winner(self):
        cluster = ClusterAdapter()
        cluster.race_secret = TEST_SECRET
        bootstrap.ensure_client(bootstrap=True, run=cluster.run)
        self.assertEqual(cluster.secret, secret_resource())
        self.assertEqual(cluster.creates, 0)

    def test_forbidden_secret_read_is_not_absence(self):
        cluster = ClusterAdapter()
        cluster.read_error = True
        with self.assertRaises(bootstrap.ProvisioningError):
            bootstrap.ensure_client(bootstrap=True, run=cluster.run)
        self.assertEqual(len(cluster.calls), 1)
        self.assertIsNone(cluster.secret)

    def test_malformed_secret_never_changes_id(self):
        for invalid in ({"data": {}}, secret_resource("short"), {"data": {"client-id": "!!!"}}):
            with self.subTest(secret=invalid):
                cluster = ClusterAdapter(secret=invalid)
                with self.assertRaises(bootstrap.ProvisioningError):
                    bootstrap.ensure_client(bootstrap=True, run=cluster.run)
                self.assertEqual(cluster.creates, 0)
                self.assertEqual(len(cluster.calls), 1)

    def test_failed_kubectl_never_includes_secret_output(self):
        failure = subprocess.CompletedProcess(["kubectl"], 1, TEST_SECRET, TEST_SECRET)
        with mock.patch.object(bootstrap.subprocess, "run", return_value=failure):
            with self.assertRaises(bootstrap.ProvisioningError) as raised:
                bootstrap.run_kubectl(["create", "-f", "-"], stdin=TEST_SECRET)
        self.assertNotIn(TEST_SECRET, str(raised.exception))


class FleetReaderDeployTests(unittest.TestCase):
    def assert_reader_env(self, container):
        env = {item["name"]: item for item in container["env"]}
        for name, key in (
            ("HIVE_PULSE_OAUTH_CLIENT_ID", "client-id"),
            ("HIVE_PULSE_OAUTH_CLIENT_SECRET", "client-secret"),
            ("HIVE_PULSE_OAUTH_TOKEN_URL", "token-url"),
        ):
            self.assertEqual(env[name]["valueFrom"]["secretKeyRef"], {
                "name": "hive-pulse-fleet-read", "key": key, "optional": False,
            })
            self.assertNotIn("value", env[name])

    def test_canary_and_rendered_live_fleet_patch_require_same_credential(self):
        documents = yaml.safe_load_all((ROOT / "k8s/rollout/hive-canary.yaml.tpl").read_text())
        canary = next(doc for doc in documents if doc and doc["kind"] == "Deployment")
        self.assertEqual(canary["metadata"]["namespace"], "shizuha-hive")
        self.assert_reader_env(next(c for c in canary["spec"]["template"]["spec"]["containers"]
                                    if c["name"] == "hive"))
        release = (SCRIPTS / "ring-release-fleet.sh").read_text()
        program = release.split("python3 - <<'PY' > /tmp/hive-codex-refresh-deploy-patch.json\n", 1)[1].split("\nPY\n", 1)[0]
        stale_env = [{"name": name, "value": "stale"} for name in (
            "HIVE_PULSE_OAUTH_CLIENT_ID", "HIVE_PULSE_OAUTH_CLIENT_SECRET", "HIVE_PULSE_OAUTH_TOKEN_URL",
        )]
        deployment = {"spec": {"template": {"spec": {
            "containers": [{"name": "hive", "env": stale_env}],
        }}}}
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"TAG": "hive-abcdef0", "BUILD_SHA": "a" * 40, "H725_MODE": "steady"}):
            with mock.patch("builtins.open", mock.mock_open(read_data=json.dumps(deployment))):
                with contextlib.redirect_stdout(output):
                    exec(compile(program, "fleet-release-patch", "exec"), {})
        patch = json.loads(output.getvalue())
        self.assert_reader_env(patch["spec"]["template"]["spec"]["containers"][0])

    def test_both_live_rings_verify_client_before_executor_submission(self):
        workflow = yaml.safe_load((ROOT / ".forgejo/workflows/deploy.yml").read_text())
        for ring in ("canary", "fleet"):
            job = workflow["jobs"][f"deploy-{ring}"]
            scripts = "\n".join(step.get("run", "") for step in job["steps"])
            bootstrap_pos = scripts.index("ensure-pulse-fleet-read-client.sh shizuha-hive shizuha")
            self.assertLess(bootstrap_pos, scripts.index("ring-executor.py submit"))


if __name__ == "__main__":
    unittest.main()
