"""Metering Core client — the R1A neutral API, consumed over HTTP only.

The producer NEVER touches cross-service tables (invariant 10), even though
the core lives in this repo: delivery goes through the internal usage-events
API with the internal service token. Transport failures map to
``MeteringCoreUnavailable`` so delivery can retry without dropping usage.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10
DEFAULT_CORE_URL = 'http://localhost:8000'


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
        getattr(settings, 'METERING_CORE_EVENTS_URL', '')
        or getattr(settings, 'HIVE_INTERNAL_URL', '')
        or DEFAULT_CORE_URL
    ).rstrip('/')


def _token() -> str:
    return str(
        getattr(settings, 'METERING_CORE_SERVICE_TOKEN', '')
        or getattr(settings, 'HIVE_INTERNAL_AUTH_TOKEN', '')
        or ''
    ).strip()


def _headers() -> dict:
    headers = {'Content-Type': 'application/json'}
    token = _token()
    if token:
        headers['Authorization'] = f'Bearer {token}'
    return headers


def post_usage_events_batch(
    envelopes: list[Mapping[str, Any]],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    post=requests.post,
) -> list[dict]:
    """POST /internal/v1/usage/events:batch — atomic CloudEvents JSON batch."""
    url = f'{core_base_url()}/internal/v1/usage/events:batch'
    try:
        response = post(url, json=envelopes, headers=_headers(), timeout=timeout)
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
    raise MeteringCoreError(
        code=str(body.get('error') or f'http_{response.status_code}'),
        detail=str(body.get('detail') or body.get('raw') or '')[:400],
        http_status=response.status_code,
    )
