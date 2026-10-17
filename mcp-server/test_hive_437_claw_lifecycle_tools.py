"""HIVE-437 (HIVE-MCP-3): Claw lifecycle MCP tool tests.

Verifies the 8 lifecycle tools (a) register on the FastMCP instance and (b) each
call the correct Hive API method + path. Endpoint authz/scoping is server-side;
these tests assert only the client-side wiring (no network — get_base is mocked).
"""
import asyncio
import sys
import unittest
from unittest.mock import MagicMock, patch


class ClawLifecycleToolsTest(unittest.TestCase):
    def setUp(self):
        self._argv = sys.argv
        sys.argv = ["hive_server.py"]
        import hive_server
        self.hive_server = hive_server

    def tearDown(self):
        sys.argv = self._argv

    def test_all_lifecycle_tools_registered(self):
        tools = asyncio.new_event_loop().run_until_complete(
            self.hive_server.mcp.list_tools()
        )
        names = {t.name for t in tools}
        for expected in (
            "hive_legacy_start_claw", "hive_legacy_stop_claw", "hive_legacy_undo_delete_claw",
            "hive_legacy_get_claw_events", "hive_legacy_get_claw_status", "hive_legacy_export_claw",
            "hive_legacy_hard_delete_claw", "hive_legacy_cancel_delete_claw",
        ):
            self.assertIn(expected, names)

    def _assert_route(self, tool_name, expected_method, expected_path):
        mock_base = MagicMock()
        mock_base.api_request.return_value = {"ok": True}
        fn = getattr(self.hive_server, tool_name)
        with patch.object(self.hive_server, "get_base", return_value=mock_base):
            fn("cid-1")
        mock_base.api_request.assert_called_once_with(expected_method, expected_path)

    def test_each_tool_hits_its_documented_route(self):
        cases = [
            ("hive_legacy_start_claw",       "POST", "v1/claws/cid-1/start/"),
            ("hive_legacy_stop_claw",        "POST", "v1/claws/cid-1/stop/"),
            ("hive_legacy_undo_delete_claw", "POST", "v1/claws/cid-1/undo-delete/"),
            ("hive_legacy_get_claw_events",  "GET",  "v1/claws/cid-1/events/"),
            ("hive_legacy_get_claw_status",  "GET",  "v1/claws/cid-1/status/"),
            ("hive_legacy_export_claw",      "POST", "v1/owner/claws/cid-1/export/"),
            ("hive_legacy_hard_delete_claw", "POST", "v1/owner/claws/cid-1/hard-delete/"),
            ("hive_legacy_cancel_delete_claw","POST","v1/owner/claws/cid-1/cancel-delete/"),
        ]
        for tool_name, method, path in cases:
            with self.subTest(tool=tool_name):
                self._assert_route(tool_name, method, path)

    def test_tool_surfaces_api_error_verbatim(self):
        mock_base = MagicMock()
        mock_base.api_request.return_value = {"error": "claw not found"}
        with patch.object(self.hive_server, "get_base", return_value=mock_base):
            out = self.hive_server.hive_legacy_start_claw("missing")
        self.assertIn("claw not found", out)
        self.assertIn("missing", out)


if __name__ == "__main__":
    unittest.main()
