"""HIVE-441 (HIVE-MCP-4): Fleet management MCP tool tests.

Verifies the 6 fleet tools (a) register and (b) each call the correct /v1/fleet/
API method + path (and, for the write tool, the correct body). Authz/org scoping
is server-side; these tests assert client-side wiring only (get_base mocked).
"""
import asyncio
import sys
import unittest
from unittest.mock import MagicMock, patch


class FleetMgmtToolsTest(unittest.TestCase):
    def setUp(self):
        self._argv = sys.argv
        sys.argv = ["hive_server.py"]
        import hive_server
        self.hive_server = hive_server

    def tearDown(self):
        sys.argv = self._argv

    def test_all_fleet_tools_registered(self):
        tools = asyncio.new_event_loop().run_until_complete(
            self.hive_server.mcp.list_tools()
        )
        names = {t.name for t in tools}
        for expected in (
            "hive_list_fleet_agents", "hive_get_agent_roster", "hive_list_teams",
            "hive_get_team_roster", "hive_get_capability_grants", "hive_set_agent_team",
        ):
            self.assertIn(expected, names)

    def _call(self, tool_name, *args, **kwargs):
        mock_base = MagicMock()
        mock_base.api_request.return_value = {"ok": True}
        fn = getattr(self.hive_server, tool_name)
        with patch.object(self.hive_server, "get_base", return_value=mock_base):
            fn(*args, **kwargs)
        return mock_base.api_request.call_args

    def test_read_tools_hit_documented_routes(self):
        self.assertEqual(self._call("hive_list_fleet_agents"), (("GET", "v1/fleet/agents/"),))
        self.assertEqual(self._call("hive_get_agent_roster", "a1"), (("GET", "v1/fleet/agents/a1/"),))
        self.assertEqual(self._call("hive_list_teams"), (("GET", "v1/fleet/teams/"),))
        self.assertEqual(self._call("hive_get_team_roster", "engineering"),
                         (("GET", "v1/fleet/teams/engineering/members/"),))
        self.assertEqual(self._call("hive_get_capability_grants", "a1"),
                         (("GET", "v1/fleet/agents/a1/effective-capabilities/"),))

    def test_set_agent_team_posts_placement_body(self):
        call = self._call("hive_set_agent_team", "a1", "t9", action="add", role="lead", organization_id=7)
        args, kwargs = call
        self.assertEqual(args, ("POST", "v1/fleet/agents/a1/team-placement/"))
        self.assertEqual(kwargs["data"], {"team_id": "t9", "action": "add", "membership_role": "lead"})
        self.assertEqual(kwargs["organization_id"], 7)

    def test_tool_surfaces_api_error_verbatim(self):
        mock_base = MagicMock()
        mock_base.api_request.return_value = {"error": "forbidden: staff only"}
        with patch.object(self.hive_server, "get_base", return_value=mock_base):
            out = self.hive_server.hive_set_agent_team("a1", "t9")
        self.assertIn("forbidden: staff only", out)


if __name__ == "__main__":
    unittest.main()
