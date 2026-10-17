"""HIVE-MCP-1: skeleton wiring smoke tests for the Hive MCP server.

Guards the foundation the concrete Hive tools are built on: the FastMCP instance
is named correctly, OAuth/base import cleanly, CLI args parse, the port default
is 18107, and the seam tools register. No network — import-time wiring only.
"""
import asyncio
import sys
import unittest


class HiveServerSkeletonTest(unittest.TestCase):
    def setUp(self):
        # Import lazily so a failed import surfaces as a test failure, not a
        # collection error, and keep argv clean for parse_args.
        self._argv = sys.argv
        sys.argv = ["hive_server.py"]
        import hive_server
        self.hive_server = hive_server

    def tearDown(self):
        sys.argv = self._argv

    def test_fastmcp_named_shizuha_hive(self):
        self.assertEqual(self.hive_server.mcp.name, "shizuha-hive")

    def test_default_http_port_is_18107(self):
        self.assertEqual(self.hive_server.DEFAULT_HTTP_PORT, 18107)

    def test_seam_tools_registered(self):
        tools = asyncio.new_event_loop().run_until_complete(
            self.hive_server.mcp.list_tools()
        )
        names = {t.name for t in tools}
        self.assertIn("hive_health", names)
        self.assertIn("hive_get_my_identity", names)

    def test_cli_args_parse_jwt_and_api_url(self):
        sys.argv = ["hive_server.py", "--jwt-token", "tok", "--api-url", "http://h/hive"]
        args = self.hive_server.parse_args()
        self.assertEqual(args.jwt_token, "tok")
        self.assertEqual(args.api_url, "http://h/hive")

    def test_base_constructs_for_hive_service(self):
        base = self.hive_server.ShizuhaMCPBase(
            service_name="hive", default_port=18107, api_prefix="/api",
            api_url="http://hive.local/hive",
        )
        self.assertEqual(base.service_name, "hive")
        self.assertEqual(base.api_url, "http://hive.local/hive")


if __name__ == "__main__":
    unittest.main()
