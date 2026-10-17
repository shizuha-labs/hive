"""ASGI listener boundary for the privileged Connect delivery path.

Kubernetes NetworkPolicy is L3/L4-only, so it cannot distinguish the Connect
delivery endpoint from the rest of Hive while both share port 8030.  Daphne
therefore listens on two ports and this wrapper makes the local socket port a
non-forgeable part of routing:

* 8030 is the ordinary Hive HTTP/WebSocket listener.
* 8031 is the delivery-only listener selected by the dedicated ClusterIP.

The public-listener denial is feature-gated for the two-phase cutover.  Phase 1
ships the listener and Service while the legacy route remains available; after
Connect is proven on 8031, phase 2 enables the denial.  Port 8031 is always
delivery-only regardless of the flag.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import Any


CONNECT_DELIVERY_PATH = "/api/v1/internal/connect/messages"
CONNECT_DELIVERY_PORT = 8031
ENFORCEMENT_ENV = "HIVE_CONNECT_ENFORCE_DEDICATED_LISTENER"

ASGIApp = Callable[
    [
        dict[str, Any],
        Callable[[], Awaitable[dict[str, Any]]],
        Callable[[dict[str, Any]], Awaitable[None]],
    ],
    Awaitable[None],
]


def _is_true(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _is_delivery_path(path: str) -> bool:
    return path.rstrip("/") == CONNECT_DELIVERY_PATH


def _listener_port(scope: dict[str, Any]) -> int | None:
    server = scope.get("server")
    if not isinstance(server, (list, tuple)) or len(server) < 2:
        return None
    try:
        return int(server[1])
    except (TypeError, ValueError):
        return None


async def _http_json(
    send: Callable[[dict[str, Any]], Awaitable[None]],
    *,
    status: int,
    body: bytes,
) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _http_not_found(send: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
    await _http_json(send, status=404, body=b'{"detail":"Not found."}')


def _is_kube_health_path(path: str) -> bool:
    return path.rstrip("/") == "/api/health"


async def _http_kube_health(send: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
    # Answer liveness/readiness WITHOUT Django. A sync GET /fleet/agents/
    # occupies Daphne's thread-sensitive pool and otherwise times out this
    # probe, which is how one Agents poll took Hive intermittently unreachable.
    await _http_json(send, status=200, body=b'{"status":"ok","service":"shizuha-hive"}')


class ConnectDeliveryListenerBoundary:
    """Route the Connect delivery endpoint only through the dedicated port."""

    def __init__(self, application: ASGIApp, *, enforce_public_denial: bool | None = None):
        self.application = application
        self.enforce_public_denial = enforce_public_denial

    def _enforced(self) -> bool:
        if self.enforce_public_denial is not None:
            return self.enforce_public_denial
        return _is_true(os.environ.get(ENFORCEMENT_ENV))

    async def __call__(self, scope, receive, send):
        scope_type = scope.get("type")
        port = _listener_port(scope)
        dedicated = port == CONNECT_DELIVERY_PORT

        if scope_type == "http":
            path = str(scope.get("path") or "")
            delivery = _is_delivery_path(path)
            if (dedicated and not delivery) or (
                not dedicated and delivery and self._enforced()
            ):
                await _http_not_found(send)
                return
            # Public listener only: kube probes must not wait on Django.
            if not dedicated and _is_kube_health_path(path):
                await _http_kube_health(send)
                return

        # The dedicated listener is HTTP-only.  Reject WebSocket upgrades before
        # Channels can route them, even if a future URL pattern is added.
        if scope_type == "websocket" and dedicated:
            await send({"type": "websocket.close", "code": 4404})
            return

        await self.application(scope, receive, send)
