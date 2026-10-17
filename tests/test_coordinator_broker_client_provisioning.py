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
    "coordinator_broker_client_bootstrap", SCRIPTS / "ensure-coordinator-broker-client.py",
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
        "client_id": bootstrap.CLIENT_ID, "client_name": "Hive Coordinator Broker",
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
                raise bootstrap.ProvisioningError("read Hive Coordinator Secret failed (exit 1)")
            return json.dumps(self.secret) if self.secret else ""
        if args == ["create", "-f", "-"]:
            if self.race_secret:
                self.secret = secret_resource(self.race_secret)
                self.client = client_row(
                    client_secret_hash=hashlib.sha256(self.race_secret.encode()).hexdigest(),
                )
                raise bootstrap.ProvisioningError("create Hive Coordinator Secret failed (exit 1)")
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
                   {**valid, "scope": "hive-coordinator:*"}, {**valid, "token_type": "refresh"},
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
                self.assertIn("hive-coordinator-broker", stdin)

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
            ({"scope": "hive-coordinator:*"}, "contract mismatch"),
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


if __name__ == "__main__":
    unittest.main()
