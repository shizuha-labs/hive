import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".forgejo" / "scripts" / "pulse-auth-preflight.py"
SPEC = importlib.util.spec_from_file_location("pulse_auth_preflight", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class StatefulTransport:
    def __init__(self):
        self.calls = []
        self.probe_ref = None
        self.project_reads = 0
        self.delete_attempts = 0

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url))
        if url.endswith("/api/auth/login/"):
            return MODULE.HttpResponse(
                200,
                b'{"user":{"username":"ci"},"tokens":{"access":"token","refresh":"r"}}',
                {},
            )
        if "/api/projects/" in url:
            self.project_reads += 1
            if self.project_reads == 1:
                raise MODULE.TransportFailure("one Pulse endpoint stalled")
            return MODULE.HttpResponse(200, b'[{"id":7,"key":"HIVE"}]', {})
        if method == "OPTIONS":
            return MODULE.HttpResponse(200, b"", {"Allow": "GET, POST, OPTIONS"})
        if method == "GET" and url.endswith("/api/items/HIVE-1372/"):
            return MODULE.HttpResponse(404, b"", {})
        if method == "GET" and "/api/items/?" in url:
            rows = [] if self.probe_ref is None else [{"item_key": self.probe_ref}]
            return MODULE.HttpResponse(
                200,
                json.dumps({"results": rows}).encode(),
                {},
            )
        if method == "POST" and url.endswith("/api/items/"):
            # Simulate Pulse committing before the client loses the response.
            self.probe_ref = "HIVE-999"
            raise MODULE.TransportFailure("response lost after commit")
        if method == "DELETE":
            self.delete_attempts += 1
            if self.delete_attempts == 1:
                return MODULE.HttpResponse(500, b"notification race", {})
            self.probe_ref = None
            return MODULE.HttpResponse(204, b"", {})
        raise AssertionError((method, url))


class DegradedThenCompleteMintTransport:
    def __init__(self):
        self.login_calls = 0
        self.project_calls = 0

    def __call__(self, method, url, headers, body, timeout):
        if url.endswith("/api/auth/login/"):
            self.login_calls += 1
            return MODULE.HttpResponse(
                200,
                json.dumps(
                    {"tokens": {"access": f"token-{self.login_calls}"}}
                ).encode(),
                {},
            )
        self.project_calls += 1
        if self.project_calls == 1:
            return MODULE.HttpResponse(200, b'{"count":0,"results":[]}', {})
        return MODULE.HttpResponse(
            200,
            b'{"count":1,"results":[{"id":7,"key":"HIVE"}]}',
            {},
        )


class LegacyLookupUnavailableTransport(StatefulTransport):
    def __call__(self, method, url, headers, body, timeout):
        if method == "GET" and url.endswith("/api/items/HIVE-1372/"):
            self.calls.append((method, url))
            raise MODULE.TransportFailure("legacy detail timed out")
        return super().__call__(method, url, headers, body, timeout)


class OrphanedProbeTransport(StatefulTransport):
    """Simulates a probe orphaned by a PRIOR release SHA surviving in Pulse.

    HIVE-1523: the old per-sha source_id lookup never found a probe whose
    source_id belonged to a different (earlier) release SHA, so it survived
    forever. This transport seeds one such orphan (source=hive_deploy_preflight,
    source_id for a different SHA) and asserts the source-scoped cleanup removes
    it on a later run.
    """

    def __init__(self):
        super().__init__()
        # Orphan from a prior release SHA, present before this run creates one.
        self.probe_ref = "HIVE-1429"
        self.orphan_source_id = "hive-rollback-create-permission-" + ("a" * 40)

    def __call__(self, method, url, headers, body, timeout):
        if method == "GET" and "/api/items/?" in url:
            rows = (
                []
                if self.probe_ref is None
                else [
                    {
                        "item_key": self.probe_ref,
                        "source": MODULE.PROBE_SOURCE,
                        "source_id": self.orphan_source_id,
                    }
                ]
            )
            return MODULE.HttpResponse(
                200,
                json.dumps({"results": rows}).encode(),
                {},
            )
        return super().__call__(method, url, headers, body, timeout)


class PulseAuthPreflightTests(unittest.TestCase):
    def test_reads_canonical_and_legacy_id_login_envelopes(self):
        self.assertEqual(
            MODULE.access_token_from_login(
                {"user": {"username": "ci"}, "tokens": {"access": "canonical"}}
            ),
            "canonical",
        )
        self.assertEqual(
            MODULE.access_token_from_login({"access": "legacy"}),
            "legacy",
        )
        self.assertIsNone(MODULE.access_token_from_login({"tokens": {}}))

    def test_legacy_probe_lookup_is_bounded_and_requires_disposable_identity(self):
        def transport(method, url, headers, body, timeout):
            self.assertEqual(method, "GET")
            self.assertEqual(url, "http://pulse/api/items/HIVE-1372/")
            return MODULE.HttpResponse(
                200,
                json.dumps(
                    {
                        "item_key": "HIVE-1372",
                        "title": MODULE.PROBE_TITLE,
                        "source": None,
                        "source_id": None,
                    }
                ).encode(),
                {},
            )

        self.assertEqual(
            MODULE.find_legacy_probe_refs(
                "http://pulse",
                "token",
                transport=transport,
                sleeper=lambda _: None,
            ),
            ["HIVE-1372"],
        )

    def test_legacy_probe_lookup_preserves_identity_mismatch(self):
        self.assertEqual(
            MODULE.find_legacy_probe_refs(
                "http://pulse",
                "token",
                transport=lambda *args: MODULE.HttpResponse(
                    200,
                    json.dumps(
                        {
                            "item_key": "HIVE-1372",
                            "title": "Real engineering task",
                            "source": "operator",
                            "source_id": "keep",
                        }
                    ).encode(),
                    {},
                ),
                sleeper=lambda _: None,
            ),
            [],
        )

    def test_remints_after_semantically_empty_project_visibility(self):
        transport = DegradedThenCompleteMintTransport()
        token, project_id = MODULE.mint_hive_access(
            "http://id/api/auth/login/",
            "http://pulse",
            "ci",
            "secret",
            transport=transport,
            sleeper=lambda _: None,
        )
        self.assertEqual(token, "token-2")
        self.assertEqual(project_id, 7)
        self.assertEqual(transport.login_calls, 2)
        self.assertEqual(transport.project_calls, 2)

    def test_rejects_semantically_empty_project_visibility(self):
        with self.assertRaisesRegex(
            MODULE.PreflightFailure,
            "rows=0 hive_matches=0",
        ):
            MODULE.find_hive_project_id(
                "http://pulse",
                "token",
                transport=lambda *args: MODULE.HttpResponse(
                    200,
                    b'{"count":0,"results":[]}',
                    {},
                ),
                sleeper=lambda _: None,
            )

    def test_bearer_token_skips_id_login(self):
        """origin/pulse-api-token is key=token; username/password never existed."""
        transport = StatefulTransport()
        env = {
            "PULSE_TOKEN": "pulse-service-token",
            "RELEASE_SHA": "a" * 40,
            "PULSE_BASE_URL": "http://pulse",
        }
        MODULE.run_preflight(env, transport=transport, sleeper=lambda _: None)
        self.assertFalse(
            any(url.endswith("/api/auth/login/") for _method, url in transport.calls)
        )
        self.assertGreaterEqual(transport.project_reads, 1)
        self.assertIsNone(transport.probe_ref)

    def test_repeated_runs_mint_fresh_tokens_despite_expired_snapshot(self):
        transport = StatefulTransport()
        generation = 0

        def renewable(method, url, headers, body, timeout):
            nonlocal generation
            if url.endswith("/api/auth/login/"):
                generation += 1
                return MODULE.HttpResponse(200, json.dumps({
                    "tokens": {"access": f"fresh-{generation}"},
                }).encode(), {})
            self.assertEqual(headers["Authorization"], f"Bearer fresh-{generation}")
            return transport(method, url, headers, body, timeout)

        env = {"PULSE_TOKEN": "expired-snapshot", "PULSE_USERNAME": "ci",
               "PULSE_PASSWORD": "test-only-secret", "RELEASE_SHA": "a" * 40,
               "PULSE_BASE_URL": "http://pulse", "ID_LOGIN_URL": "http://id/api/auth/login/"}
        for _ in range(2):
            MODULE.run_preflight(env, transport=renewable, sleeper=lambda _: None)
            self.assertIsNone(transport.probe_ref)
        self.assertEqual(generation, 2)
        self.assertEqual(sum(method == "POST" and url.endswith("/api/items/")
                             for method, url in transport.calls), 2)

    def test_configured_login_failure_does_not_fall_back_to_snapshot(self):
        calls = []

        def denied(method, url, headers, body, timeout):
            calls.append(url)
            self.assertTrue(url.endswith("/api/auth/login/"))
            return MODULE.HttpResponse(401, b"denied", {})

        with self.assertRaises(MODULE.PreflightFailure):
            MODULE.run_preflight({"PULSE_TOKEN": "expired-snapshot", "PULSE_USERNAME": "ci",
                                  "PULSE_PASSWORD": "test-secret", "RELEASE_SHA": "a" * 40},
                                 transport=denied, sleeper=lambda _: None)
        self.assertTrue(calls)

    def test_retries_safe_reads_recovers_uncertain_create_and_cleans_probe(self):
        transport = StatefulTransport()
        env = {
            "PULSE_USERNAME": "ci",
            "PULSE_PASSWORD": "secret",
            "RELEASE_SHA": "a" * 40,
            "PULSE_BASE_URL": "http://pulse",
            "ID_LOGIN_URL": "http://id/api/auth/login/",
        }
        MODULE.run_preflight(env, transport=transport, sleeper=lambda _: None)
        self.assertEqual(transport.project_reads, 2)
        self.assertEqual(
            sum(method == "POST" and url.endswith("/api/items/")
                for method, url in transport.calls),
            1,
        )
        self.assertEqual(transport.delete_attempts, 2)
        self.assertIsNone(transport.probe_ref)

    def test_reconciles_orphan_probe_from_prior_release_sha(self):
        # HIVE-1523: a probe orphaned by an interrupted run for a DIFFERENT
        # release SHA (different source_id) must be found and deleted by a later
        # run's source-scoped cleanup. The old per-sha lookup left it forever.
        transport = OrphanedProbeTransport()
        env = {
            "PULSE_USERNAME": "ci",
            "PULSE_PASSWORD": "secret",
            "RELEASE_SHA": "b" * 40,  # different SHA from the orphan's source_id
            "PULSE_BASE_URL": "http://pulse",
            "ID_LOGIN_URL": "http://id/api/auth/login/",
        }
        MODULE.run_preflight(env, transport=transport, sleeper=lambda _: None)
        # The orphan (HIVE-1429, prior SHA) and the fresh probe are both gone.
        self.assertIsNone(transport.probe_ref)
        self.assertGreaterEqual(transport.delete_attempts, 2)
        # The orphan's ref was deleted by the source-scoped cleanup.
        self.assertTrue(
            any(
                method == "DELETE" and url.endswith("/api/items/HIVE-1429/")
                for method, url in transport.calls
            )
        )

    def test_legacy_cleanup_timeout_does_not_block_versioned_permission_probe(self):
        transport = LegacyLookupUnavailableTransport()
        env = {
            "PULSE_USERNAME": "ci",
            "PULSE_PASSWORD": "secret",
            "RELEASE_SHA": "a" * 40,
            "PULSE_BASE_URL": "http://pulse",
            "ID_LOGIN_URL": "http://id/api/auth/login/",
        }

        MODULE.run_preflight(env, transport=transport, sleeper=lambda _: None)

        self.assertEqual(
            sum(
                method == "POST" and url.endswith("/api/items/")
                for method, url in transport.calls
            ),
            1,
        )
        self.assertEqual(
            sum(url.endswith("/api/items/HIVE-1372/") for _, url in transport.calls),
            1,
        )
        self.assertIsNone(transport.probe_ref)

    def test_rejects_missing_credentials_before_network_access(self):
        with self.assertRaisesRegex(
            MODULE.PreflightFailure,
            "credentials are unavailable",
        ):
            MODULE.run_preflight(
                {"RELEASE_SHA": "a" * 40},
                transport=lambda *args: self.fail("network should not be called"),
                sleeper=lambda _: None,
            )

    def test_rejects_non_sha_release_identity(self):
        with self.assertRaisesRegex(MODULE.PreflightFailure, "full lowercase SHA"):
            MODULE.run_preflight(
                {
                    "PULSE_USERNAME": "ci",
                    "PULSE_PASSWORD": "secret",
                    "RELEASE_SHA": "short",
                },
                transport=lambda *args: self.fail("network should not be called"),
                sleeper=lambda _: None,
            )


if __name__ == "__main__":
    unittest.main()
