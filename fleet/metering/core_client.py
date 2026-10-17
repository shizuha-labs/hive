"""Metering Core client — the neutral API, consumed over HTTP only.

PLAT-7523 (R2 Slice 2): the fleet runtime meter is a producer against the
product-neutral Metering Core. Per HLD §1/§9 the producer consumes the
neutral API and NEVER touches cross-service tables. Every call is
authenticated with the internal service token, bounded in time, and maps
transport failures to ``MeteringCoreUnavailable`` so delivery can defer (the
local outbox fact is already durable; retry keeps the same source/id and
byte-identical envelope).

Mirrors R2 Slice 1's ``inference/metering/core_client.py`` (cortex#161) —
coordinate, never fork a parallel ingest path.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10
# Hive hosts the Metering Core; the fleet producer reaches it over the same
# in-cluster HTTP surface the cross-service producers use (neutral API only).
DEFAULT_CORE_URL = 'http://shizuha-hive.shizuha.svc.cluster.local:8000'


class MeteringCoreError(Exception):
    """The core answered with a non-retryable API error."""

    def __init__(self, *, code: str, detail: str = '', http_status: int = 400):
        self.code = code
        self.detail = detail
        self.http_status = http_status
        super().__init__(f'metering core {code}: {detail}'[:500])


class MeteringCoreUnavailable(Exception):
    """The core could not be reached — transport-level, retryable."""


def core_base_url() -> str:
    return str(
        getattr(settings, 'METERING_CORE_URL', '')
        or getattr(settings, 'HIVE_INTERNAL_URL', '')
        or DEFAULT_CORE_URL
    ).rstrip('/')


def _token() -> str:
    return str(
        getattr(settings, 'METERING_CORE_SERVICE_TOKEN', '')
        or getattr(settings, 'HIVE_INTERNAL_AUTH_TOKEN', '')
        or ''
    ).strip()


def _headers(extra: Mapping[str, str] | None = None) -> dict:
    headers = {'Content-Type': 'application/json'}
    token = _token()
    if token:
        headers['Authorization'] = f'Bearer {token}'
    if extra:
        headers.update(extra)
    return headers


def _post(path: str, *, payload: Any, headers: Mapping[str, str],
          timeout: float, post=requests.post) -> Any:
    url = f'{core_base_url()}{path}'
    try:
        response = post(url, json=payload, headers=dict(headers), timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — transport-level, retryable
        raise MeteringCoreUnavailable(f'{type(exc).__name__}: {exc}') from exc
    if response.status_code in (200, 201):
        try:
            return response.json()
        except ValueError as exc:
            raise MeteringCoreUnavailable(f'non-JSON 2xx from core: {exc}') from exc
    try:
        body = response.json()
    except ValueError:
        body = {'raw': str(response.text)[:500]}
    code = str(body.get('error') or f'http_{response.status_code}')
    raise MeteringCoreError(
        code=code,
        detail=str(body.get('detail') or body.get('raw') or '')[:400],
        http_status=response.status_code,
    )


def resolve_account(
    *,
    account_type: str,
    owner_user_id: int | None = None,
    owner_org_id: str = '',
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    post=requests.post,
) -> dict:
    """POST /internal/v1/accounts:resolve — the core derives the payer."""
    payload: dict[str, Any] = {'account_type': account_type}
    if owner_user_id is not None:
        payload['owner_user_id'] = int(owner_user_id)
    if owner_org_id:
        payload['owner_org_id'] = str(owner_org_id)
    return _post(
        '/internal/v1/accounts:resolve',
        payload=payload, headers=_headers(), timeout=timeout, post=post,
    )


def ingest_events(
    envelopes: list[dict],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    post=requests.post,
) -> list[dict]:
    """POST /internal/v1/usage/events:batch — at-least-once, replay-safe.

    Returns the per-event result list from the core. Transport failures raise
    ``MeteringCoreUnavailable``; per-event rejections (409 conflict, 400
    validation) come back inside the result list and never abort the batch.
    """
    if not envelopes:
        return []
    result = _post(
        '/internal/v1/usage/events:batch',
        payload=envelopes,
        headers=_headers({'Content-Type': 'application/cloudevents-batch+json'}),
        timeout=timeout,
        post=post,
    )
    if isinstance(result, list):
        return result
    return result.get('results', [result])
