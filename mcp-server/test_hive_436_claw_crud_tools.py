"""HIVE-436 (HIVE-MCP-2): Claw CRUD MCP tool tests.

Verifies the CRUD tools register on the FastMCP instance and call the documented
Hive API routes with the expected request body/query params. Endpoint authz and
owner scoping stay server-side; these tests mock get_base so no network is used.
"""
import asyncio
import sys
import unittest
from unittest.mock import MagicMock, patch


class ClawCrudToolsTest(unittest.TestCase):
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

    def test_all_crud_tools_registered(self):
        names = self._tool_names()
        for expected in (
            "hive_legacy_list_claws",
            "hive_legacy_create_claw",
            "hive_legacy_get_claw",
            "hive_legacy_update_claw",
            "hive_legacy_delete_claw",
        ):
            self.assertIn(expected, names)

    def test_list_claws_hits_documented_route(self):
        mock_base = MagicMock()
        mock_base.api_request.return_value = {"count": 0, "results": []}
        with patch.object(self.hive_server, "get_base", return_value=mock_base):
            self.hive_server.hive_legacy_list_claws()
        mock_base.api_request.assert_called_once_with("GET", "v1/claws/")

    def test_create_claw_refuses_to_provision(self):
        # ADR-0080 (HIVE-2008): Claw is retired — the create tool never mints a
        # new Claw row and never calls POST v1/claws/.
        mock_base = MagicMock()
        mock_base.api_request.return_value = {"id": "cid-1", "name": "Alpha"}
        with patch.object(self.hive_server, "get_base", return_value=mock_base):
            out = self.hive_server.hive_legacy_create_claw("Alpha", "free")
        self.assertIn("retired", out)
        self.assertIn("ADR-0080", out)
        mock_base.api_request.assert_not_called()

    def test_get_claw_hits_documented_route(self):
        mock_base = MagicMock()
        mock_base.api_request.return_value = {"id": "cid-1"}
        with patch.object(self.hive_server, "get_base", return_value=mock_base):
            self.hive_server.hive_legacy_get_claw("cid-1")
        mock_base.api_request.assert_called_once_with("GET", "v1/claws/cid-1/")

    def test_update_claw_patches_name(self):
        mock_base = MagicMock()
        mock_base.api_request.return_value = {"id": "cid-1", "name": "Renamed"}
        with patch.object(self.hive_server, "get_base", return_value=mock_base):
            self.hive_server.hive_legacy_update_claw("cid-1", "Renamed")
        mock_base.api_request.assert_called_once_with(
            "PATCH",
            "v1/claws/cid-1/",
            data={"name": "Renamed"},
        )

    def test_delete_claw_passes_confirm_param(self):
        mock_base = MagicMock()
        mock_base.api_request.return_value = {"message": "Claw scheduled for deletion"}
        with patch.object(self.hive_server, "get_base", return_value=mock_base):
            self.hive_server.hive_legacy_delete_claw("cid-1", "Alpha Claw")
        mock_base.api_request.assert_called_once_with(
            "DELETE",
            "v1/claws/cid-1/",
            params={"confirm": "Alpha Claw"},
        )

    def test_tool_surfaces_api_error_verbatim(self):
        mock_base = MagicMock()
        mock_base.api_request.return_value = {"error": "Confirmation required"}
        with patch.object(self.hive_server, "get_base", return_value=mock_base):
            out = self.hive_server.hive_legacy_delete_claw("cid-1", "wrong")
        self.assertIn("Confirmation required", out)
        self.assertIn("cid-1", out)


if __name__ == "__main__":
    unittest.main()
