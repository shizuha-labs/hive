"""Provider credential probes for HIVE-314 BYO model providers.

Guarantees for callers:
- This module never persists provider keys, base URLs, or model results.
- Provider keys are only held in memory long enough to send one bounded probe.
- Error details are redacted before being returned; keys are never logged here.
- Probes are single-shot with short timeouts and no retry loop.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Literal
from urllib.parse import urljoin

import requests

ProviderType = Literal["openai", "anthropic", "openai_compatible"]
ErrorClass = Literal[
    "auth_failure",
    "network",
    "rate_limit",
    "quota_exhausted",
    "provider_error",
    "invalid_provider",
    "invalid_response",
]

DEFAULT_TIMEOUT_SECONDS = 5
OPENAI_BASE_URL = "https://api.openai.com"
ANTHROPIC_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_STATIC_MODELS = [
    "claude-sonnet-4-5",
    "claude-sonnet-4-0",
    "claude-3-7-sonnet-latest",
    "claude-3-5-haiku-latest",
]


@dataclass(frozen=True)
class ProviderProbeResult:
    ok: bool
    error_class: ErrorClass | None = None
    detail: str = ""


class ProviderProbeError(Exception):
    def __init__(self, result: ProviderProbeResult):
        super().__init__(result.detail)
        self.result = result


def validate_credential(provider_type: str, base_url: str | None, key: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Validate a BYO-provider key with one bounded liveness probe.

    Returns a dict shaped for future API/UI callers: ``{ok, error_class, detail}``.
    The key is never persisted or logged; returned detail is redacted.
    """
    try:
        provider = _normalize_provider(provider_type)
        if provider == "anthropic":
            _anthropic_models(base_url, key, timeout=timeout)
        else:
            _openai_models(provider, base_url, key, timeout=timeout)
        return {"ok": True, "error_class": None, "detail": ""}
    except ProviderProbeError as exc:
        return {"ok": False, "error_class": exc.result.error_class, "detail": _redact(exc.result.detail, key)}


def discover_models(provider_type: str, base_url: str | None, key: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> list[str]:
    """Discover model IDs for the provider without persisting keys or results.

    OpenAI and OpenAI-compatible providers use ``GET /v1/models``. Anthropic uses
    the Models API when available and falls back to a small curated list if that
    endpoint is unavailable on the account/region; auth/rate/quota failures still
    raise because they are actionable credential states.
    """
    provider = _normalize_provider(provider_type)
    try:
        if provider == "anthropic":
            payload = _anthropic_models(base_url, key, timeout=timeout)
        else:
            payload = _openai_models(provider, base_url, key, timeout=timeout)
    except ProviderProbeError as exc:
        if provider == "anthropic" and exc.result.error_class == "provider_error" and _looks_like_missing_models_endpoint(exc.result.detail):
            return list(ANTHROPIC_STATIC_MODELS)
        raise
    return _extract_model_ids(payload)


def _normalize_provider(provider_type: str) -> ProviderType:
    provider = (provider_type or "").strip().lower().replace("-", "_")
    if provider in {"openai", "anthropic", "openai_compatible"}:
        return provider  # type: ignore[return-value]
    raise ProviderProbeError(ProviderProbeResult(False, "invalid_provider", f"Unsupported provider_type={provider_type!r}"))


def _provider_base_url(provider: ProviderType, base_url: str | None) -> str:
    raw = (base_url or "").strip().rstrip("/")
    if raw:
        return raw
    if provider == "openai":
        return OPENAI_BASE_URL
    if provider == "anthropic":
        return ANTHROPIC_BASE_URL
    raise ProviderProbeError(ProviderProbeResult(False, "invalid_provider", "base_url is required for openai_compatible providers"))


def _models_url(provider: ProviderType, base_url: str | None) -> str:
    return urljoin(f"{_provider_base_url(provider, base_url)}/", "v1/models")


def _openai_models(provider: ProviderType, base_url: str | None, key: str, *, timeout: float) -> dict[str, Any]:
    return _request_json(
        "GET",
        _models_url(provider, base_url),
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        timeout=timeout,
        key=key,
    )


def _anthropic_models(base_url: str | None, key: str, *, timeout: float) -> dict[str, Any]:
    return _request_json(
        "GET",
        _models_url("anthropic", base_url),
        headers={"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION, "Accept": "application/json"},
        timeout=timeout,
        key=key,
    )


def _request_json(method: str, url: str, *, headers: dict[str, str], timeout: float, key: str) -> dict[str, Any]:
    try:
        response = requests.request(method, url, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise ProviderProbeError(ProviderProbeResult(False, "network", _redact(str(exc), key))) from exc

    if response.status_code >= 400:
        raise ProviderProbeError(_classify_http_error(response, key))

    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderProbeError(ProviderProbeResult(False, "invalid_response", "Provider returned non-JSON response")) from exc
    if not isinstance(payload, dict):
        raise ProviderProbeError(ProviderProbeResult(False, "invalid_response", "Provider returned a non-object response"))
    return payload


def _classify_http_error(response: requests.Response, key: str) -> ProviderProbeResult:
    status = response.status_code
    payload = _response_payload(response)
    text = _error_text(payload, getattr(response, "text", ""))
    lowered = text.lower()
    detail = _redact(f"HTTP {status}: {text}"[:500], key)

    if status in {401, 403}:
        return ProviderProbeResult(False, "auth_failure", detail)
    if status == 429:
        if any(token in lowered for token in ("quota", "insufficient_quota", "billing", "credit", "spend")):
            return ProviderProbeResult(False, "quota_exhausted", detail)
        return ProviderProbeResult(False, "rate_limit", detail)
    if status == 402 or any(token in lowered for token in ("quota", "insufficient_quota", "billing", "credit", "spend")):
        return ProviderProbeResult(False, "quota_exhausted", detail)
    return ProviderProbeResult(False, "provider_error", detail)


def _response_payload(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _error_text(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        err = payload.get("error", payload)
        if isinstance(err, dict):
            pieces = [str(err.get(name)) for name in ("type", "code", "message") if err.get(name)]
            return ": ".join(pieces) if pieces else fallback
        if isinstance(err, str):
            return err
    return fallback or "provider request failed"


def _extract_model_ids(payload: dict[str, Any]) -> list[str]:
    data: Any = payload.get("data", payload.get("models", []))
    if not isinstance(data, Iterable) or isinstance(data, (str, bytes, dict)):
        raise ProviderProbeError(ProviderProbeResult(False, "invalid_response", "Models response did not contain a list"))
    models: list[str] = []
    for item in data:
        model_id = item.get("id") if isinstance(item, dict) else item
        if isinstance(model_id, str) and model_id:
            models.append(model_id)
    if not models:
        raise ProviderProbeError(ProviderProbeResult(False, "invalid_response", "Models response did not contain model IDs"))
    return models


def _looks_like_missing_models_endpoint(detail: str) -> bool:
    lowered = detail.lower()
    return "http 404" in lowered or "http 405" in lowered or "http 501" in lowered or "not found" in lowered


def _redact(value: str, key: str) -> str:
    if not key:
        return value
    redacted = value.replace(key, "[redacted]")
    if len(key) >= 8:
        redacted = redacted.replace(key[:8], "[redacted]").replace(key[-4:], "[redacted]")
    return redacted
