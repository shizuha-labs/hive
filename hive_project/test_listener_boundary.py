import unittest

from hive_project.listener_boundary import (
    CONNECT_DELIVERY_PATH,
    ConnectDeliveryListenerBoundary,
)


class ConnectDeliveryListenerBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def _call(self, *, port, path, scope_type="http", enforced=True):
        downstream = []
        sent = []

        async def app(scope, receive, send):
            downstream.append(scope)
            if scope["type"] == "http":
                await send({"type": "http.response.start", "status": 204, "headers": []})
                await send({"type": "http.response.body", "body": b""})

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        boundary = ConnectDeliveryListenerBoundary(
            app, enforce_public_denial=enforced
        )
        await boundary(
            {"type": scope_type, "path": path, "server": ["127.0.0.1", port]},
            receive,
            send,
        )
        return downstream, sent

    async def test_dedicated_listener_allows_delivery_with_or_without_slash(self):
        for path in (CONNECT_DELIVERY_PATH, f"{CONNECT_DELIVERY_PATH}/"):
            downstream, sent = await self._call(port=8031, path=path)
            self.assertEqual(len(downstream), 1)
            self.assertEqual(sent[0]["status"], 204)

    async def test_dedicated_listener_denies_every_other_http_path(self):
        downstream, sent = await self._call(port=8031, path="/api/health/")
        self.assertEqual(downstream, [])
        self.assertEqual(sent[0]["status"], 404)

    async def test_dedicated_listener_denies_websocket(self):
        downstream, sent = await self._call(
            port=8031, path="/v1/fleet/daemon-link", scope_type="websocket"
        )
        self.assertEqual(downstream, [])
        self.assertEqual(sent, [{"type": "websocket.close", "code": 4404}])

    async def test_public_listener_denies_delivery_after_cutover(self):
        downstream, sent = await self._call(port=8030, path=CONNECT_DELIVERY_PATH)
        self.assertEqual(downstream, [])
        self.assertEqual(sent[0]["status"], 404)

    async def test_public_listener_answers_kube_health_without_django(self):
        downstream, sent = await self._call(port=8030, path="/api/health/")
        self.assertEqual(downstream, [])
        self.assertEqual(sent[0]["status"], 200)
        body = next(m.get("body") for m in sent if m.get("type") == "http.response.body")
        self.assertIn(b'"status":"ok"', body)

    async def test_public_listener_keeps_ordinary_http_and_websocket(self):
        http_downstream, sent = await self._call(port=8030, path="/hive/api/v1/me/")
        ws_downstream, ws_sent = await self._call(
            port=8030, path="/v1/fleet/daemon-link", scope_type="websocket"
        )
        self.assertEqual(len(http_downstream), 1)
        self.assertEqual(sent[0]["status"], 204)
        self.assertEqual(len(ws_downstream), 1)
        self.assertEqual(ws_sent, [])

    async def test_phase_one_keeps_legacy_delivery_until_connect_cutover(self):
        downstream, sent = await self._call(
            port=8030, path=CONNECT_DELIVERY_PATH, enforced=False
        )
        self.assertEqual(len(downstream), 1)
        self.assertEqual(sent[0]["status"], 204)
