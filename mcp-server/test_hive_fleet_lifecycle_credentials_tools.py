"""Hive MCP fleet lifecycle + credential tools.

These tools are the Hive-owned replacements for the old local shizuha-cron
agent lifecycle / credential surfaces. The tests verify registration and route
wiring only; authz, audit, and validation remain backend responsibilities.
"""
import asyncio
import sys
import unittest
from unittest.mock import MagicMock, patch


class FleetLifecycleCredentialToolsTest(unittest.TestCase):
    def setUp(self):
        self._argv = sys.argv
        sys.argv = ["hive_server.py"]
        import hive_server
        self.hive_server = hive_server

    def tearDown(self):
        sys.argv = self._argv

    def _tool_names(self):
        loop = asyncio.new_event_loop()
        try:
            tools = loop.run_until_complete(self.hive_server.mcp.list_tools())
        finally:
            loop.close()
        return {t.name for t in tools}

    def _call(self, tool_name, *args, **kwargs):
        mock_base = MagicMock()
        mock_base.api_request.return_value = {"ok": True}
        fn = getattr(self.hive_server, tool_name)
        with patch.object(self.hive_server, "get_base", return_value=mock_base):
            fn(*args, **kwargs)
        return mock_base.api_request.call_args

    def test_tools_registered(self):
        names = self._tool_names()
        for expected in (
            "hive_create_fleet_agent",
            "hive_update_fleet_agent",
            "hive_delete_fleet_agent",
            "hive_enable_fleet_agent",
            "hive_disable_fleet_agent",
            "hive_restart_fleet_agent",
            "hive_reset_agent_runtime_session",
            "hive_list_agent_templates",
            "hive_get_agent_template",
            "hive_list_credentials",
            "hive_create_credential",
            "hive_upsert_self_credential",
            "hive_request_credential",
            "hive_list_credential_requests",
            "hive_deny_credential_request",
            "hive_expire_credential_requests",
            "hive_get_credential",
            "hive_update_credential",
            "hive_revoke_credential",
            "hive_get_credential_audit",
        ):
            self.assertIn(expected, names)

    def test_fleet_lifecycle_routes(self):
        self.assertEqual(
            self._call("hive_create_fleet_agent", "nori", display_name="Nori", role="Social"),
            (("POST", "v1/fleet/agents/"), {"data": {
                "agent_username": "nori", "display_name": "Nori", "role": "Social",
            }}),
        )
        self.assertEqual(
            self._call("hive_update_fleet_agent", "a1", model="gpt-5.5"),
            (("PATCH", "v1/fleet/agents/a1/"), {"data": {"model": "gpt-5.5"}}),
        )
        self.assertEqual(
            self._call(
                "hive_update_fleet_agent", "a1",
                model="gpt-5.6-sol", execution_method="codex_app_server",
                reasoning_effort="xhigh",
            ),
            (("PATCH", "v1/fleet/agents/a1/"), {"data": {
                "model": "gpt-5.6-sol",
                "execution_method": "codex_app_server",
                "reasoning_effort": "xhigh",
            }}),
        )
        self.assertEqual(
            self._call(
                "hive_update_fleet_agent", "a1",
                resource_overrides={"limits": {"memory": "8Gi"}},
            ),
            (("PATCH", "v1/fleet/agents/a1/"), {"data": {
                "resource_overrides": {"limits": {"memory": "8Gi"}},
            }}),
        )
        self.assertEqual(self._call("hive_delete_fleet_agent", "a1"), (("DELETE", "v1/fleet/agents/a1/"),))
        self.assertEqual(self._call("hive_enable_fleet_agent", "a1"), (("POST", "v1/fleet/agents/a1/enable/"),))
        self.assertEqual(self._call("hive_disable_fleet_agent", "a1"), (("POST", "v1/fleet/agents/a1/disable/"),))
        self.assertEqual(self._call("hive_restart_fleet_agent", "a1"), (("POST", "v1/fleet/agents/a1/restart/"),))
        self.assertEqual(
            self._call("hive_reset_agent_runtime_session", "a1", reason="wedged session"),
            (("POST", "v1/fleet/agents/a1/reset-session/"), {"data": {"reason": "wedged session"}}),
        )
        self.assertEqual(
            self._call("hive_list_agent_templates", query="security", max_results=5),
            (("GET", "v1/fleet/agent-templates/"), {"params": {"query": "security", "max_results": 5}}),
        )
        self.assertEqual(
            self._call("hive_get_agent_template", "security-auditor"),
            (("GET", "v1/fleet/agent-templates/security-auditor/"),),
        )

    def test_credential_routes(self):
        self.assertEqual(
            self._call("hive_list_credentials", scope="github", agent_id="a1"),
            (("GET", "v1/fleet/credentials/"), {"params": {"scope": "github", "agent": "a1"}}),
        )
        self.assertEqual(
            self._call("hive_create_credential", "github", "vault://secret", agent_id="a1"),
            (("POST", "v1/fleet/credentials/"), {"data": {
                "scope": "github", "secret_ref": "vault://secret", "agent": "a1",
            }}),
        )
        self.assertEqual(
            self._call(
                "hive_upsert_self_credential",
                "x-twitter",
                "broker://agents/nori/x-twitter",
                agent_id="nori",
                fingerprint="sha256:abcdef123456",
            ),
            (("POST", "v1/fleet/credentials/self-upsert/"), {"data": {
                "service": "x-twitter",
                "secret_ref": "broker://agents/nori/x-twitter",
                "agent_id": "nori",
                "fingerprint": "sha256:abcdef123456",
            }}),
        )
        self.assertEqual(
            self._call("hive_request_credential", "github", "needed for PR authoring", agent_id="nori"),
            (("POST", "v1/fleet/credential-requests/"), {"data": {
                "scope": "github",
                "reason": "needed for PR authoring",
                "agent_id": "nori",
            }}),
        )
        self.assertEqual(
            self._call("hive_list_credential_requests", status="pending", scope="github", agent_id="nori"),
            (("GET", "v1/fleet/credential-requests/"), {"params": {
                "status": "pending",
                "scope": "github",
                "agent": "nori",
            }}),
        )
        self.assertEqual(
            self._call("hive_deny_credential_request", "r1", "not needed"),
            (("POST", "v1/fleet/credential-requests/r1/deny/"), {"data": {"reason": "not needed"}}),
        )
        self.assertEqual(
            self._call("hive_expire_credential_requests", scope="github"),
            (("POST", "v1/fleet/credential-requests/expire/"), {"data": {"scope": "github"}}),
        )
        self.assertEqual(self._call("hive_get_credential", "c1"), (("GET", "v1/fleet/credentials/c1/"),))
        self.assertEqual(
            self._call("hive_update_credential", "c1", fingerprint="sha256:abc123"),
            (("PATCH", "v1/fleet/credentials/c1/"), {"data": {"fingerprint": "sha256:abc123"}}),
        )
        self.assertEqual(self._call("hive_revoke_credential", "c1"), (("DELETE", "v1/fleet/credentials/c1/"),))
        self.assertEqual(
            self._call("hive_get_credential_audit", "c1"),
            (("GET", "v1/fleet/credentials/c1/audit/"),),
        )


if __name__ == "__main__":
    unittest.main()
