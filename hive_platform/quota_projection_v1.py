"""Immutable V1 positive-schema projection for provider quota snapshots.

Provider responses are untrusted.  Only the bounded fields defined here may be
persisted or returned by the credential-health API.  Migrations import this
versioned module; incompatible schema changes must use a new V2 module.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

_SOURCES = {
    'claude_oauth_usage',
    'codex_wham_usage',
    'grok_xai_usage',
    'token_pool',
}
_CONFIDENCE = {'exact', 'partial', 'error', 'unsupported'}
QUOTA_SCHEMA_VERSION = 1
_WINDOW_TEXT = re.compile(r'[^A-Za-z0-9 ._:+()/=-]+')
_PLAN = re.compile(r'^[a-z0-9._-]{1,32}$')
_CREDIT_ID = re.compile(r'^[A-Za-z0-9._:-]{1,160}$')
_RESET_STATUSES = {'available', 'redeemed', 'expired', 'unavailable'}


def _bounded_text(value: Any, *, limit: int) -> str:
    if not isinstance(value, str):
        return ''
    return _WINDOW_TEXT.sub('', value).strip()[:limit]


def _timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return value


def _percent(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0 or number > 100:
        return None
    return round(number, 2)


def _non_negative_int(value: Any, *, maximum: int = 1_000_000) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < 0 or number > maximum:
        return None
    return number


def _diagnostic_code(value: Any) -> str:
    """Map provider/error detail to a bounded internal enum-like code."""
    if not isinstance(value, str):
        return ''
    detail = value.strip().lower()
    if not detail:
        return ''
    if detail.startswith('request_failed:'):
        return 'request_failed'
    if detail.startswith('token_decrypt_failed:'):
        return 'token_decrypt_failed'
    if detail.startswith('oauth_refresh_failed:'):
        return 'oauth_refresh_failed'
    if detail.startswith('quota_refresh_failed:'):
        return 'quota_refresh_failed'
    if detail.startswith('limited_token:'):
        return 'limited_token'
    if detail.startswith('ok_via_'):
        return 'ok'
    match = re.match(r'^(auth_failed_)?http_(\d{3})', detail)
    if match:
        prefix = 'auth_failed_http_' if match.group(1) else 'http_'
        return f'{prefix}{match.group(2)}'
    allowed = {
        'missing_access_token',
        'auth_failed',
        'rate_limited_usage_endpoint',
        'rate_limited',
        'invalid_json',
        'ok',
        'empty_windows',
        'usage_api_unavailable',
        'limited_token',
        'request_failed',
        'token_decrypt_failed',
        'oauth_refresh_failed',
        'quota_refresh_failed',
        'not_oauth_subscription_token',
        'unsupported_provider',
    }
    return detail if detail in allowed else 'provider_error'


def _window(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    window_id = _bounded_text(value.get('id'), limit=80)
    label = _bounded_text(value.get('label'), limit=80)
    if not window_id and not label:
        return None
    out: dict[str, Any] = {
        'id': window_id,
        'label': label,
        'used_percent': _percent(value.get('used_percent')),
        'left_percent': _percent(value.get('left_percent')),
        'resets_at': _timestamp(value.get('resets_at')),
    }
    return out


def _reset_credits(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    for key in ('available', 'total_earned'):
        number = _non_negative_int(value.get(key))
        if number is not None:
            out[key] = number
    credits = []
    for item in (value.get('credits') or [])[:5]:
        if not isinstance(item, dict):
            continue
        credit_id = str(item.get('id') or '')
        if not _CREDIT_ID.fullmatch(credit_id):
            credit_id = ''
        reset_type = _bounded_text(item.get('reset_type'), limit=48)
        status = str(item.get('status') or '').lower()
        credits.append({
            'id': credit_id,
            'reset_type': reset_type,
            'status': status if status in _RESET_STATUSES else '',
            'granted_at': _timestamp(item.get('granted_at')),
            'expires_at': _timestamp(item.get('expires_at')),
        })
    if credits:
        out['credits'] = credits
    return out or None


def project_quota_snapshot(value: Any) -> dict[str, Any]:
    """Return only the documented, bounded quota snapshot schema."""
    if not isinstance(value, dict) or not value:
        return {}
    out: dict[str, Any] = {'quota_schema_version': QUOTA_SCHEMA_VERSION}

    source = str(value.get('source') or '')
    if source in _SOURCES:
        out['source'] = source
    schema_version = _non_negative_int(value.get('schema_version'), maximum=100)
    if schema_version is not None:
        out['schema_version'] = schema_version
    fetched_at = _timestamp(value.get('fetched_at'))
    if fetched_at:
        out['fetched_at'] = fetched_at
    confidence = str(value.get('confidence') or '')
    if confidence == 'ok':  # historical Grok snapshots before PLAT-4125
        confidence = 'exact'
    out['confidence'] = confidence if confidence in _CONFIDENCE else 'error'
    plan = str(value.get('plan') or '').lower()
    out['plan'] = plan if _PLAN.fullmatch(plan) else None
    out['windows'] = [
        projected
        for projected in (_window(item) for item in (value.get('windows') or [])[:24])
        if projected is not None
    ]
    out['detail'] = _diagnostic_code(value.get('detail'))
    reset_credits = _reset_credits(value.get('reset_credits'))
    if reset_credits is not None:
        out['reset_credits'] = reset_credits
    return out
