import asyncio
import importlib
import json
import os
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from base import (
    ShizuhaMCPBase,
    ShizuhaTokenVerifier,
    _oauth_issuer,
    _register_oauth_discovery_routes,
    _resource_server_url,
)


class _FakeSigningKey:
    key = "rs-key"


class _FakeJwksClient:
    urls = []

    def __init__(self, url):
        self.urls.append(url)

    def get_signing_key_from_jwt(self, token):
        return _FakeSigningKey()


class OAuthUrlDefaultsTests(unittest.TestCase):
    @mock.patch.dict(os.environ, {}, clear=True)
    def test_defaults_use_canonical_https_without_redirects(self):
        self.assertEqual(_oauth_issuer(), "https://shizuha.com/id")
        self.assertEqual(
            _resource_server_url("hive"),
            "https://shizuha.com/mcp/hive",
        )

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_discovery_advertises_canonical_authorization_and_resource(self):
        routes = {}

        class FakeMcp:
            def custom_route(self, path, methods):
                def register(handler):
                    routes[path] = handler
                    return handler

                return register

        from starlette.requests import Request

        _register_oauth_discovery_routes(FakeMcp(), "hive")
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "scheme": "https",
                "path": "/.well-known/oauth-protected-resource",
                "raw_path": b"/.well-known/oauth-protected-resource",
                "query_string": b"",
                "headers": [
                    (b"host", b"shizuha.com"),
                    (b"x-forwarded-prefix", b"/mcp/hive"),
                ],
                "server": ("shizuha.com", 443),
            }
        )
        response = asyncio.run(
            routes["/.well-known/oauth-protected-resource"](request)
        )
        metadata = json.loads(response.body)
        self.assertEqual(metadata["resource"], "https://shizuha.com/mcp/hive")
        self.assertEqual(
            metadata["authorization_servers"],
            ["https://shizuha.com/id"],
        )

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_verifier_uses_canonical_https_jwks_url(self):
        _FakeJwksClient.urls.clear()
        verifier = ShizuhaTokenVerifier("hive", _oauth_issuer())
        with mock.patch("jwt.PyJWKClient", _FakeJwksClient), mock.patch(
            "base.jwt.decode",
            return_value={"user_id": 7, "exp": int(time.time()) + 3600},
        ):
            result = asyncio.run(
                verifier.verify_token("header.payload.signature")
            )

        self.assertIsNotNone(result)
        self.assertEqual(
            _FakeJwksClient.urls,
            ["https://shizuha.com/id/.well-known/jwks.json"],
        )


class HiveBearerTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from cryptography.hazmat.primitives.asymmetric import rsa

        self.environment = mock.patch.dict(os.environ, {
            "SHIZUHA_MCP_OAUTH_ENFORCE": "true",
            "SHIZUHA_OAUTH_ISSUER": "https://shizuha.com/id",
            "SHIZUHA_MCP_RESOURCE_URL": "https://shizuha.com/mcp/hive",
            "SHIZUHA_HIVE_URL": "http://hive.test",
            "SHIZUHA_HIVE_JWT_TOKEN": "staff-fallback-must-not-be-used",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        with mock.patch("sys.argv", ["hive_server.py"]):
            import hive_server
            self.server = importlib.reload(hive_server)
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.server.mcp._token_verifier._jwks_client = SimpleNamespace(
            get_signing_key_from_jwt=lambda token: SimpleNamespace(key=self.private_key.public_key()),
        )
        self.app = self.server.mcp.streamable_http_app()

    def signed_token(self, expires_in=300, user_id=190):
        import jwt
        return jwt.encode({
            "sub": str(user_id), "user_id": user_id, "username": "hina",
            "iss": "https://shizuha.com/id", "exp": int(time.time()) + expires_in,
        }, self.private_key, algorithm="RS256")

    async def call_identity(self, token=None):
        return (await self.call_identities([token]))[0]

    async def call_identities(self, tokens):
        import httpx

        async with self.app.router.lifespan_context(self.app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app), base_url="http://localhost",
                headers={"Accept": "application/json, text/event-stream"},
            ) as client:
                return await asyncio.gather(*[
                    client.post("/mcp", headers={"Authorization": "Bearer " + token} if token else {}, json={
                        "jsonrpc": "2.0", "id": index + 1, "method": "tools/call",
                        "params": {"name": "hive_get_my_identity", "arguments": {}},
                    })
                    for index, token in enumerate(tokens)
                ])

    async def test_missing_bearer_cannot_reach_static_staff_fallback(self):
        with mock.patch("base.requests.request") as upstream:
            response = await self.call_identity()
        self.assertEqual(response.status_code, 401)
        self.assertIn("resource_metadata=", response.headers["www-authenticate"])
        upstream.assert_not_called()

    async def test_invalid_and_expired_bearers_are_rejected_before_tools(self):
        with mock.patch("base.requests.request") as upstream:
            responses = await self.call_identities(["not-a-jwt", self.signed_token(-60)])
        self.assertEqual([response.status_code for response in responses], [401, 401])
        upstream.assert_not_called()

    async def test_valid_caller_bearer_reaches_harmless_tool_without_staff_substitution(self):
        token = self.signed_token()
        upstream_response = mock.Mock(status_code=200, content=b"identity")
        upstream_response.json.return_value = {"user_id": 190, "username": "hina"}
        with mock.patch("base.requests.request", return_value=upstream_response) as upstream:
            response = await self.call_identity(token)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("hina", response.text)
        self.assertEqual(upstream.call_args.kwargs["headers"]["Authorization"], "Bearer " + token)
        self.assertEqual(upstream.call_args.kwargs["url"], "http://hive.test/api/v1/me/")

    async def test_concurrent_callers_keep_separate_authorization_headers(self):
        tokens = [self.signed_token(user_id=190), self.signed_token(user_id=191)]
        upstream_response = mock.Mock(status_code=200, content=b"identity")
        upstream_response.json.return_value = {"ok": True}
        with mock.patch("base.requests.request", return_value=upstream_response) as upstream:
            responses = await self.call_identities(tokens)
        self.assertEqual([response.status_code for response in responses], [200, 200])
        self.assertCountEqual(
            [call.kwargs["headers"]["Authorization"] for call in upstream.call_args_list],
            ["Bearer " + token for token in tokens],
        )


class HiveTokenFallbackTests(unittest.TestCase):
    def base(self, context):
        with mock.patch("sys.argv", ["test"]):
            client = ShizuhaMCPBase(
                service_name="hive", default_port=18107,
                api_url="http://hive.test", jwt_token="static-agent",
                request_context_getter=lambda: context,
            )
        client.user_jwt_token = "static-user"
        return client

    @mock.patch.dict(os.environ, {"SHIZUHA_MCP_OAUTH_ENFORCE": "true"})
    def test_enforced_http_missing_context_cannot_substitute_static_credentials(self):
        for context in (None, SimpleNamespace(request_context=None)):
            client = self.base(context)
            with self.assertRaises(ValueError):
                client.get_headers()
            with self.assertRaises(ValueError):
                client.get_headers(as_user=True)

    @mock.patch.dict(os.environ, {"SHIZUHA_MCP_OAUTH_ENFORCE": "false"})
    def test_stdio_static_fallback_remains_available(self):
        client = self.base(None)
        self.assertEqual(client.get_headers()["Authorization"], "Bearer static-agent")
        self.assertEqual(client.get_headers(as_user=True)["Authorization"], "Bearer static-user")

    @mock.patch.dict(os.environ, {"SHIZUHA_MCP_OAUTH_ENFORCE": "true"})
    def test_raw_and_wrapped_context_preserve_caller_and_delegated_user(self):
        context = SimpleNamespace(request=SimpleNamespace(headers={
            "Authorization": "Bearer caller", "X-Shizuha-User-Authorization": "Bearer delegated",
        }))
        for value in (context, SimpleNamespace(request_context=context)):
            client = self.base(value)
            self.assertEqual(client.get_headers()["Authorization"], "Bearer caller")
            self.assertEqual(client.get_headers(as_user=True)["Authorization"], "Bearer delegated")


if __name__ == "__main__":
    unittest.main()
