"""Subscription quota fetchers (CodexBar-compatible sources).

These call the same private/session endpoints the official CLIs and tools
like CodexBar use — NOT public API billing dashboards.

  Claude OAuth:  GET api.anthropic.com/api/oauth/usage
  Codex OAuth:   GET chatgpt.com/backend-api/wham/usage
  Grok/xAI:      best-effort SuperGrok/session endpoints (may partial)

Responses are normalized to:
  {
    "source": str,
    "fetched_at": iso,
    "confidence": "exact"|"partial"|"error",
    "plan": str|None,
    "windows": [
      {"id": "5h"|"weekly"|..., "label": str,
       "used_percent": float|None,  # 0-100 used
       "left_percent": float|None,  # 0-100 remaining
       "resets_at": iso|None}
    ],
    "detail": str,  # bounded internal diagnostic code
  }
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone as dt_timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)

CLAUDE_OAUTH_USAGE_URL = 'https://api.anthropic.com/api/oauth/usage'
CLAUDE_OAUTH_BETA = 'oauth-2025-04-20'
CODEX_WHAM_USAGE_URL = 'https://chatgpt.com/backend-api/wham/usage'
CODEX_QUOTA_SCHEMA_VERSION = 2
CODEX_WHAM_RESET_CREDITS_URL = 'https://chatgpt.com/backend-api/wham/rate-limit-reset-credits'
# Best-effort Grok; may 404 depending on plan/token type.
GROK_USAGE_CANDIDATES = (
    'https://api.x.ai/v1/usage',
    'https://api.x.ai/v1/account/usage',
)
# grok.com consumer billing (SuperGrok credits) — the gRPC-web endpoint the
# grok.com web app + grok CLI use. Verified 2026-07-11: accepts OUR device-flow
# OAuth access tokens as `Authorization: Bearer` (grpc-status 0), unlike
# /rest/rate-limits which rejects OAuth2 token users. Parsing approach mirrors
# steipete/CodexBar GrokWebBillingFetcher (generic protobuf scan — no .proto
# schema published): used% = shallowest fixed32 float field #1 in [0,100];
# reset = earliest future unix-seconds varint, preferring path 1.5.1.
GROK_BILLING_URL = 'https://grok.com/grok_api_v2.GrokBuildBilling/GetGrokCreditsConfig'
# Banked "Usage Limit Reset" tokens (Settings → Usage). Same OAuth bearer as
# GetGrokCreditsConfig. Proto: prod_mc_billing.ConsumerUiSvc/GetRemainingResets
# (verified live 2026-08-17). Response: repeated ConsumerResetToken field 10
# {token_id=10, validity_start=20, validity_end=30}.
GROK_REMAINING_RESETS_URL = (
    'https://grok.com/prod_mc_billing.ConsumerUiSvc/GetRemainingResets'
)
GROK_RESET_TYPE = 'grok_usage_limit'
GROK_SETTINGS_URL = 'https://cli-chat-proxy.grok.com/v1/settings'

REQUEST_TIMEOUT = 15


def _now_iso() -> str:
    return datetime.now(tz=dt_timezone.utc).isoformat()


def _window(
    *,
    wid: str,
    label: str,
    used_percent: float | None = None,
    left_percent: float | None = None,
    resets_at: str | None = None,
) -> dict[str, Any]:
    if used_percent is not None and left_percent is None:
        left_percent = max(0.0, min(100.0, 100.0 - float(used_percent)))
    if left_percent is not None and used_percent is None:
        used_percent = max(0.0, min(100.0, 100.0 - float(left_percent)))
    return {
        'id': wid,
        'label': label,
        'used_percent': round(float(used_percent), 2) if used_percent is not None else None,
        'left_percent': round(float(left_percent), 2) if left_percent is not None else None,
        'resets_at': resets_at,
    }


def _util_to_used_percent(utilization) -> float | None:
    """Claude returns utilization as 0..1; some payloads use 0..100."""
    if utilization is None:
        return None
    try:
        v = float(utilization)
    except (TypeError, ValueError):
        return None
    if v <= 1.0:
        return round(v * 100.0, 2)
    return round(min(v, 100.0), 2)


def _parse_resets_at(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # unix seconds or ms
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=dt_timezone.utc).isoformat()
        except Exception:
            return None
    s = str(value).strip()
    if not s:
        return None
    return s


def fetch_claude_oauth_usage(access_token: str) -> dict[str, Any]:
    """CodexBar / claudebar path: Claude Code OAuth usage API."""
    out: dict[str, Any] = {
        'source': 'claude_oauth_usage',
        'fetched_at': _now_iso(),
        'confidence': 'error',
        'plan': None,
        'windows': [],
        'detail': '',
    }
    if not access_token:
        out['detail'] = 'missing_access_token'
        return out
    try:
        resp = requests.get(
            CLAUDE_OAUTH_USAGE_URL,
            headers={
                'Authorization': f'Bearer {access_token}',
                'anthropic-beta': CLAUDE_OAUTH_BETA,
                'User-Agent': 'shizuha-hive-quota/1.0',
                'Accept': 'application/json',
            },
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as exc:
        out['detail'] = f'request_failed:{type(exc).__name__}'
        return out

    if resp.status_code == 401:
        out['detail'] = 'auth_failed'
        return out
    if resp.status_code == 429:
        out['detail'] = 'rate_limited_usage_endpoint'
        out['confidence'] = 'partial'
        return out
    if resp.status_code == 403 and 'scope requirement' in resp.text:
        # `claude setup-token` mints inference-only OATs; the usage/profile APIs
        # need user:profile, which only a full Claude Code login can mint. Such a
        # token works for inference but structurally cannot report quota — surface
        # it as a neutral "limited token" capability gap, not a red error. The
        # full-OAuth org-claude-* accounts still report real quota bars.
        out['detail'] = 'limited_token: inference-only OAT (no quota API)'
        out['confidence'] = 'unsupported'
        return out
    if resp.status_code != 200:
        out['detail'] = f'http_{resp.status_code}'
        return out

    try:
        data = resp.json()
    except Exception:
        out['detail'] = 'invalid_json'
        return out

    windows = []
    # Anthropic's /api/oauth/usage returns per-model-family blocks. Fable is the
    # default Claude Code model, so surface its window when the API exposes it
    # (seven_day_fable / five_hour_fable). Unknown keys are simply skipped, so
    # listing candidate names is forward-compatible with the account's plan.
    mapping = (
        ('five_hour', '5h', 'Session (5h)'),
        ('five_hour_fable', '5h_fable', 'Session Fable (5h)'),
        ('seven_day', 'weekly', 'Weekly'),
        ('seven_day_fable', 'weekly_fable', 'Weekly Fable'),
        ('seven_day_sonnet', 'weekly_sonnet', 'Weekly Sonnet'),
        ('seven_day_opus', 'weekly_opus', 'Weekly Opus'),
    )
    for key, wid, label in mapping:
        block = data.get(key) if isinstance(data, dict) else None
        if not isinstance(block, dict):
            continue
        used = _util_to_used_percent(block.get('utilization'))
        if used is None and block.get('used_percent') is not None:
            used = _util_to_used_percent(float(block['used_percent']) / 100.0 if float(block['used_percent']) > 1 else block['used_percent'])
        resets = _parse_resets_at(block.get('resets_at') or block.get('resetsAt'))
        if used is None and resets is None:
            continue
        windows.append(_window(wid=wid, label=label, used_percent=used, resets_at=resets))

    plan = None
    for k in ('subscriptionType', 'subscription_type', 'rate_limit_tier', 'plan'):
        if isinstance(data, dict) and data.get(k):
            plan = str(data[k])
            break

    out['windows'] = windows
    out['plan'] = plan
    out['confidence'] = 'exact' if windows else 'partial'
    out['detail'] = 'ok' if windows else 'empty_windows'
    return out


def fetch_claude_profile_email(access_token: str) -> str:
    """Account identity for a Claude OAuth token (requires user:profile scope).

    Returns the account email, or '' when the token lacks the scope (setup-token
    OATs) or on any error — callers treat this as best-effort enrichment."""
    if not access_token:
        return ''
    try:
        resp = requests.get(
            'https://api.anthropic.com/api/oauth/profile',
            headers={
                'Authorization': f'Bearer {access_token}',
                'anthropic-beta': CLAUDE_OAUTH_BETA,
                'User-Agent': 'shizuha-hive-quota/1.0',
                'Accept': 'application/json',
            },
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return ''
        data = resp.json()
    except Exception:
        return ''
    if not isinstance(data, dict):
        return ''
    account = data.get('account') if isinstance(data.get('account'), dict) else data
    email = account.get('email') or account.get('email_address') or ''
    return str(email)[:254]


def _codex_window_from_block(block: dict, *, wid: str, label: str) -> dict[str, Any] | None:
    if not isinstance(block, dict):
        return None
    used = None
    for key in (
        'used_percent', 'usedPercent', 'utilization',
        'limit_reached_percent', 'percent_used',
    ):
        if block.get(key) is not None:
            try:
                v = float(block[key])
            except (TypeError, ValueError):
                continue
            # usedPercent is typically 0-100 already in wham payloads
            if key in ('utilization',) and v <= 1.0:
                used = v * 100.0
            else:
                used = v if v > 1.0 or key != 'utilization' else v * 100.0
            break
    # remaining_percent / left
    left = None
    for key in ('remaining_percent', 'remainingPercent', 'left_percent', 'percent_left'):
        if block.get(key) is not None:
            try:
                left = float(block[key])
            except (TypeError, ValueError):
                left = None
            break
    resets = _parse_resets_at(
        block.get('resets_at')
        or block.get('resetsAt')
        or block.get('reset_at')
        or block.get('resetAt')
        or block.get('reset_time')
    )
    if used is None and left is None and resets is None:
        return None
    return _window(wid=wid, label=label, used_percent=used, left_percent=left, resets_at=resets)


def _codex_window_identity(block: dict, *, fallback_id: str, fallback_label: str) -> tuple[str, str]:
    """Name Codex quota windows by their provider-supplied duration.

    OpenAI historically returned primary=5h and secondary=weekly, but current
    token-based plans can omit the 5h lane and return the seven-day window as
    ``primary_window``.  Positional labels therefore turn real weekly usage
    into a misleading "Session (5h)" bar.  Duration is semantic when present;
    retain the old positional contract only for legacy payloads without it.
    """
    seconds = None
    if isinstance(block, dict):
        for key in ('limit_window_seconds', 'limitWindowSeconds', 'window_seconds', 'windowSeconds'):
            if block.get(key) is None:
                continue
            try:
                seconds = int(block[key])
            except (TypeError, ValueError):
                seconds = None
            break
    if seconds == 5 * 60 * 60:
        return '5h', 'Session (5h)'
    if seconds == 7 * 24 * 60 * 60:
        return 'weekly', 'Weekly'
    return fallback_id, fallback_label


def fetch_codex_wham_usage(access_token: str, *, account_id: str = '') -> dict[str, Any]:
    """CodexBar OAuth path: ChatGPT backend wham/usage."""
    out: dict[str, Any] = {
        'source': 'codex_wham_usage',
        'schema_version': CODEX_QUOTA_SCHEMA_VERSION,
        'fetched_at': _now_iso(),
        'confidence': 'error',
        'plan': None,
        'windows': [],
        'detail': '',
        'reset_credits': None,
    }
    if not access_token:
        out['detail'] = 'missing_access_token'
        return out

    headers = {
        'Authorization': f'Bearer {access_token}',
        'Accept': 'application/json',
        'User-Agent': 'shizuha-hive-quota/1.0',
        'ChatGPT-Account-ID': account_id or '',
    }
    # Some deployments require chatgpt-account-id header from JWT
    if not account_id:
        headers.pop('ChatGPT-Account-ID', None)

    try:
        resp = requests.get(
            CODEX_WHAM_USAGE_URL,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as exc:
        out['detail'] = f'request_failed:{type(exc).__name__}'
        return out

    if resp.status_code in (401, 403):
        out['detail'] = f'auth_failed_http_{resp.status_code}'
        return out
    if resp.status_code == 429:
        out['detail'] = 'rate_limited'
        out['confidence'] = 'partial'
        return out
    if resp.status_code != 200:
        out['detail'] = f'http_{resp.status_code}'
        return out

    try:
        data = resp.json()
    except Exception:
        out['detail'] = 'invalid_json'
        return out

    windows = []
    rate = data.get('rate_limit') or data.get('rateLimit') or {}
    if not isinstance(rate, dict):
        rate = {}
    primary = rate.get('primary_window') or rate.get('primaryWindow') or rate.get('primary')
    secondary = rate.get('secondary_window') or rate.get('secondaryWindow') or rate.get('secondary')
    primary_id, primary_label = _codex_window_identity(
        primary or {}, fallback_id='5h', fallback_label='Session (5h)')
    w = _codex_window_from_block(primary or {}, wid=primary_id, label=primary_label)
    if w:
        windows.append(w)
    secondary_id, secondary_label = _codex_window_identity(
        secondary or {}, fallback_id='weekly', fallback_label='Weekly')
    w = _codex_window_from_block(secondary or {}, wid=secondary_id, label=secondary_label)
    if w:
        windows.append(w)

    # Per-model buckets (e.g. GPT-5.3-Codex-Spark) live at the TOP level as
    # additional_rate_limits: [{limit_name, metered_feature,
    # rate_limit: {primary_window, secondary_window}}] — the windows are NESTED
    # under entry['rate_limit'] and the display name is `limit_name`. The old
    # code looked for flat name/used_percent keys on the entry itself, so these
    # buckets were silently dropped (operator 2026-07-14: show spark usage).
    extras = (
        data.get('additional_rate_limits')
        or data.get('additionalRateLimits')
        or rate.get('additional_rate_limits')
        or rate.get('additionalRateLimits')
        or []
    )
    for extra in extras:
        if not isinstance(extra, dict):
            continue
        name = str(
            extra.get('limit_name') or extra.get('limitName')
            or extra.get('metered_feature') or extra.get('name')
            or extra.get('id') or 'extra'
        )
        erate = extra.get('rate_limit') or extra.get('rateLimit') or {}
        if not isinstance(erate, dict):
            erate = {}
        added = False
        w = _codex_window_from_block(
            erate.get('primary_window') or erate.get('primaryWindow') or erate.get('primary') or {},
            wid=f'extra:{name}', label=name)
        if w:
            windows.append(w)
            added = True
        w = _codex_window_from_block(
            erate.get('secondary_window') or erate.get('secondaryWindow') or erate.get('secondary') or {},
            wid=f'extra:{name}:secondary', label=f'{name} (secondary)')
        if w:
            windows.append(w)
            added = True
        if not added:
            # Legacy flat shape (older wham payloads): keep working.
            w = _codex_window_from_block(extra, wid=f'extra:{name}', label=name)
            if w:
                windows.append(w)

    plan = None
    for k in ('plan_type', 'planType', 'plan', 'subscription'):
        if data.get(k):
            plan = str(data[k])
            break
        if isinstance(data.get('account'), dict) and data['account'].get(k):
            plan = str(data['account'][k])
            break

    # "Usage resets available" (codex.bar parity): the usage payload carries
    # rate_limit_reset_credits.available_count — a credit redeems an immediate
    # rate-limit reset (operator 2026-07-14: surface on /hive/subscriptions).
    reset_credits = None
    rrc = data.get('rate_limit_reset_credits')
    if isinstance(rrc, dict) and rrc.get('available_count') is not None:
        try:
            reset_credits = {'available': int(rrc['available_count'])}
        except (TypeError, ValueError):
            reset_credits = None

    out['windows'] = windows
    out['plan'] = plan
    out['reset_credits'] = reset_credits
    out['confidence'] = 'exact' if windows else 'partial'
    out['detail'] = 'ok' if windows else 'empty_windows'
    return out


def fetch_codex_reset_credits(access_token: str, *, account_id: str = '') -> dict[str, Any] | None:
    """Detail for the "usage resets available" chip (codex.bar parity).

    GET wham/rate-limit-reset-credits -> {available_count, total_earned_count,
    credits: [{reset_type, status, granted_at, expires_at, ...}]}. Returns a
    BOUNDED, redacted dict (no profile fields) or None on any failure — this is
    enrichment on top of the usage payload's available_count, never load-bearing.
    """
    if not access_token:
        return None
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Accept': 'application/json',
        'User-Agent': 'shizuha-hive-quota/1.0',
    }
    if account_id:
        headers['ChatGPT-Account-ID'] = account_id
    try:
        resp = requests.get(
            CODEX_WHAM_RESET_CREDITS_URL, headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    out: dict[str, Any] = {}
    for src, dst in (('available_count', 'available'), ('total_earned_count', 'total_earned')):
        if data.get(src) is not None:
            try:
                out[dst] = int(data[src])
            except (TypeError, ValueError):
                pass
    creds = []
    for c in (data.get('credits') or [])[:5]:
        if not isinstance(c, dict):
            continue
        creds.append({
            # id is the RateLimitResetCredit_... handle needed to redeem; not a
            # secret (no token material) — required by the operator-triggered
            # redeem button (2026-07-14).
            'id': str(c.get('id') or ''),
            'reset_type': str(c.get('reset_type') or ''),
            'status': str(c.get('status') or ''),
            'granted_at': c.get('granted_at'),
            'expires_at': c.get('expires_at'),
        })
    if creds:
        out['credits'] = creds
    return out or None


CODEX_WHAM_CONSUME_CREDIT_URL = (
    'https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume'
)


def redeem_codex_reset_credit(
    access_token: str, *, account_id: str = '', credit_id: str = '',
    redeem_request_id: str = '',
) -> dict[str, Any]:
    """Redeem ONE banked Codex rate-limit reset credit (codex.md / codex-reset
    parity): POST wham/rate-limit-reset-credits/consume with an optional credit
    id (the provider selects a credit when detail enrichment is unavailable).
    Operator-triggered only (staff-gated view). Returns
    {ok, code, windows_reset, status, detail}."""
    out: dict[str, Any] = {'ok': False, 'detail': ''}
    if not access_token:
        out['detail'] = 'missing_access_token'
        return out
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'User-Agent': 'shizuha-hive-quota/1.0',
    }
    if account_id:
        headers['ChatGPT-Account-ID'] = account_id
    body = {'redeem_request_id': redeem_request_id or str(uuid.uuid4())}
    # Codex deliberately supports consuming without a credit id when the
    # summary reports credits but the best-effort detail request did not land.
    # In that case the backend selects the applicable available credit.
    if credit_id:
        body['credit_id'] = credit_id
    try:
        resp = requests.post(
            CODEX_WHAM_CONSUME_CREDIT_URL, headers=headers, json=body,
            timeout=REQUEST_TIMEOUT)
    except Exception as exc:
        out['detail'] = f'request_failed:{type(exc).__name__}'
        return out
    if resp.status_code in (401, 403):
        out['detail'] = f'auth_failed_http_{resp.status_code}'
        return out
    if resp.status_code >= 400:
        out['detail'] = f'http_{resp.status_code}'
        return out
    try:
        data = resp.json()
    except Exception:
        data = {}
    code = str(data.get('code') or '')
    credit = data.get('credit') if isinstance(data.get('credit'), dict) else {}
    out.update({
        # A successful HTTP response can still mean no reset occurred.
        'ok': code == 'reset',
        'code': code,
        'windows_reset': data.get('windows_reset'),
        'status': str(credit.get('status') or ''),
        'redeemed_at': credit.get('redeemed_at'),
        'detail': 'ok' if code == 'reset' else (code or 'unknown_response_code'),
    })
    return out


def _grpcweb_data_frames(raw: bytes) -> list[bytes]:
    """Split a gRPC-web response into DATA frame payloads (flag 0x00).

    Frame = 1-byte flags + 4-byte big-endian length + payload; trailer frames
    (flag 0x80) carry grpc-status headers and are skipped here.
    """
    out: list[bytes] = []
    i = 0
    while i + 5 <= len(raw):
        flags = raw[i]
        ln = int.from_bytes(raw[i + 1:i + 5], 'big')
        chunk = raw[i + 5:i + 5 + ln]
        i += 5 + ln
        if flags == 0 and chunk:
            out.append(bytes(chunk))
    return out


def _scan_protobuf(data: bytes, path: tuple = (), out: dict | None = None, order: list | None = None):
    """Schema-less protobuf field scan (CodexBar approach — xAI publishes no
    .proto for GrokBuildBilling). Collects fixed32-as-float and varint fields
    with their field-number paths; recurses into length-delimited fields (a
    non-message bytes/string field simply yields no nested numeric fields).
    Returns None only on structural corruption at THIS level.
    """
    import struct as _struct
    if out is None:
        out = {'f32': [], 'var': []}
    if order is None:
        order = [0]
    i, n = 0, len(data)
    while i < n:
        try:
            tag = 0
            shift = 0
            while True:
                b = data[i]; i += 1
                tag |= (b & 0x7f) << shift
                shift += 7
                if not (b & 0x80):
                    break
            fnum, wt = tag >> 3, tag & 7
            if fnum == 0 or wt in (3, 4, 6, 7):
                return None
            if wt == 0:  # varint
                v = 0
                shift = 0
                while True:
                    b = data[i]; i += 1
                    v |= (b & 0x7f) << shift
                    shift += 7
                    if not (b & 0x80):
                        break
                order[0] += 1
                out['var'].append((path + (fnum,), order[0], v))
            elif wt == 1:  # fixed64 — not needed for billing fields
                i += 8
            elif wt == 5:  # fixed32 → float
                v = _struct.unpack('<f', data[i:i + 4])[0]
                i += 4
                order[0] += 1
                out['f32'].append((path + (fnum,), order[0], v))
            elif wt == 2:  # length-delimited: try as submessage
                ln = 0
                shift = 0
                while True:
                    b = data[i]; i += 1
                    ln |= (b & 0x7f) << shift
                    shift += 7
                    if not (b & 0x80):
                        break
                sub = data[i:i + ln]
                i += ln
                if ln > 0:
                    _scan_protobuf(bytes(sub), path + (fnum,), out, order)
        except Exception:
            return None
    return out


def fetch_grok_billing(access_token: str) -> dict[str, Any] | None:
    """Back-compat wrapper: credits dict, or None for *any* failure."""
    data, _transient = fetch_grok_billing_ex(access_token)
    return data


def fetch_grok_billing_ex(access_token: str) -> tuple[dict[str, Any] | None, bool]:
    """SuperGrok credits via grok.com GetGrokCreditsConfig (gRPC-web).

    Returns ``(credits_or_None, transient_failure)``.

    The transient flag exists because collapsing every failure into ``None`` was
    actively harmful: the caller treats "no billing data" as a PERMANENT
    capability gap (``confidence=unsupported``), which puts the account on a
    24-hour re-check backoff. A timeout, a 429, or a 5xx therefore locked an
    account out of quota refresh for a day and rendered it as "no usage API" on
    the Subscriptions dashboard — while `Check now` (force=True) fetched real
    numbers immediately, because it bypasses the staleness gate.

    Observed 2026-08-04: 8 xAI accounts probed inside a 7-second burst
    (07:41:46-07:41:53) were all stamped unsupported, while the one account
    polled later on the normal cadence returned `ok_via_grok_billing`.
    """
    import math
    import time as _time
    try:
        resp = requests.post(
            GROK_BILLING_URL,
            headers={
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/grpc-web+proto',
                'X-Grpc-Web': '1',
                'Accept': '*/*',
                'User-Agent': 'shizuha-hive-quota/1.0',
            },
            data=bytes(5),  # empty request message in a gRPC-web frame
            timeout=REQUEST_TIMEOUT,
        )
    except Exception:
        # Network error / timeout — says nothing about the account's plan.
        return None, True
    if resp.status_code != 200:
        # 429 and 5xx are the provider throttling or failing us, not evidence
        # that this account lacks credits. Only a clean 4xx rejection is.
        transient = resp.status_code == 429 or resp.status_code >= 500
        return None, transient
    grpc_status = resp.headers.get('grpc-status')
    if grpc_status not in (None, '0'):
        return None, False
    scan = {'f32': [], 'var': []}
    for frame in _grpcweb_data_frames(resp.content):
        got = _scan_protobuf(frame)
        if got:
            scan['f32'] += got['f32']
            scan['var'] += got['var']
    # trailers-in-body carry grpc-status when the header is absent
    if b'grpc-status:0' not in resp.content and grpc_status is None and not scan['f32'] and not scan['var']:
        return None, False
    percents = [
        f for f in scan['f32']
        if f[0][-1] == 1 and math.isfinite(f[2]) and 0 <= f[2] <= 100
    ]
    percents.sort(key=lambda f: (len(f[0]), f[1]))
    now = _time.time()
    future_ts = [
        (p, v) for (p, _o, v) in scan['var']
        if 1_700_000_000 <= v <= 2_100_000_000 and v > now
    ]
    preferred = [v for (p, v) in future_ts if p == (1, 5, 1)]
    reset = min(preferred) if preferred else (min((v for _, v in future_ts), default=None))
    used = float(percents[0][2]) if percents else None
    if used is None:
        # "No usage yet" shape: no percent fields but a future reset present.
        if reset is not None:
            used = 0.0
        else:
            return None, False
    return {
        'used_percent': round(used, 2),
        'resets_at': _parse_resets_at(reset),
    }, False


def _proto_read_varint(data: bytes, i: int) -> tuple[int, int]:
    x = 0
    shift = 0
    n = len(data)
    while i < n:
        b = data[i]
        i += 1
        x |= (b & 0x7f) << shift
        if not (b & 0x80):
            return x, i
        shift += 7
        if shift > 63:
            break
    raise ValueError('truncated varint')


def _proto_fields(data: bytes):
    """Yield (field_number, wire_type, value) from a protobuf message."""
    i = 0
    n = len(data)
    while i < n:
        try:
            key, i = _proto_read_varint(data, i)
        except ValueError:
            return
        fnum, wt = key >> 3, key & 7
        if fnum == 0 or wt in (3, 4, 6, 7):
            return
        if wt == 0:
            try:
                v, i = _proto_read_varint(data, i)
            except ValueError:
                return
            yield fnum, 0, v
        elif wt == 2:
            try:
                ln, i = _proto_read_varint(data, i)
            except ValueError:
                return
            yield fnum, 2, data[i:i + ln]
            i += ln
        elif wt == 1:
            i += 8
        elif wt == 5:
            i += 4
        else:
            return


def _proto_timestamp_unix(data: bytes) -> int | None:
    for fnum, wt, val in _proto_fields(data):
        if fnum == 1 and wt == 0:
            return int(val)
    return None


def _parse_grok_reset_token(data: bytes) -> dict[str, Any] | None:
    token_id = ''
    granted_at = None
    expires_at = None
    for fnum, wt, val in _proto_fields(data):
        if fnum == 10 and wt == 2:
            try:
                token_id = val.decode('utf-8')
            except UnicodeDecodeError:
                token_id = ''
        elif fnum == 20 and wt == 2:
            granted_at = _parse_resets_at(_proto_timestamp_unix(val))
        elif fnum == 30 and wt == 2:
            expires_at = _parse_resets_at(_proto_timestamp_unix(val))
    if not token_id:
        return None
    return {
        'id': token_id,
        'reset_type': GROK_RESET_TYPE,
        'granted_at': granted_at,
        'expires_at': expires_at,
    }


def _parse_grok_remaining_resets(raw: bytes) -> list[dict[str, Any]]:
    tokens: list[dict[str, Any]] = []
    for frame in _grpcweb_data_frames(raw):
        for fnum, wt, val in _proto_fields(frame):
            if fnum == 10 and wt == 2:
                token = _parse_grok_reset_token(val)
                if token:
                    tokens.append(token)
    return tokens


def _grok_reset_still_pending(token: dict[str, Any], *, now: float | None = None) -> bool:
    expires = token.get('expires_at')
    if not expires:
        return True
    try:
        ts = datetime.fromisoformat(str(expires).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return True
    return ts > (time.time() if now is None else now)


def fetch_grok_reset_credits(access_token: str) -> dict[str, Any] | None:
    """Banked grok.com usage-limit resets for the Subscriptions chip.

    Returns ``{available, credits:[...]}`` or None on any fetch failure.
    An empty token list is a real zero (the account has no resets pending),
    not a failure — same contract as Codex ``available_count: 0``.
    """
    if not access_token:
        return None
    try:
        resp = requests.post(
            GROK_REMAINING_RESETS_URL,
            headers={
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/grpc-web+proto',
                'X-Grpc-Web': '1',
                'Accept': '*/*',
                'User-Agent': 'shizuha-hive-quota/1.0',
            },
            data=bytes(5),
            timeout=REQUEST_TIMEOUT,
        )
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    grpc_status = resp.headers.get('grpc-status')
    if grpc_status not in (None, '0'):
        return None
    frames = _grpcweb_data_frames(resp.content)
    if (
        grpc_status is None
        and b'grpc-status:0' not in resp.content
        and not frames
        and resp.content
    ):
        # HTML 200 / unknown body — not a remaining-resets response.
        return None
    tokens = _parse_grok_remaining_resets(resp.content)
    now = time.time()
    credits = []
    available = 0
    for token in tokens:
        pending = _grok_reset_still_pending(token, now=now)
        if pending:
            available += 1
        if len(credits) < 5:
            credits.append({
                'id': token['id'],
                'reset_type': GROK_RESET_TYPE,
                'status': 'available' if pending else 'expired',
                'granted_at': token.get('granted_at'),
                'expires_at': token.get('expires_at'),
            })
    out: dict[str, Any] = {'available': available}
    if credits:
        out['credits'] = credits
    return out


def _attach_grok_reset_credits(out: dict[str, Any], access_token: str) -> None:
    reset = fetch_grok_reset_credits(access_token)
    if reset is not None:
        out['reset_credits'] = reset


def fetch_grok_plan(access_token: str) -> str | None:
    """Provider-reported plan, independent of credits and coding entitlement.

    The official Grok Build client uses RemoteSettings.subscription_tier_display
    for its plan label. Missing/failed settings must never imply a Free plan.
    """
    if not access_token:
        return None
    try:
        resp = requests.get(
            GROK_SETTINGS_URL,
            headers={
                'Authorization': f'Bearer {access_token}',
                'X-XAI-Token-Auth': 'xai-grok-cli',
                'Accept': 'application/json',
                'User-Agent': 'shizuha-hive-quota/1.0',
            },
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not isinstance(data, dict):
            return None
        plan = data.get('subscription_tier_display')
        from hive_platform.quota_projection_v2 import normalize_grok_plan
        return normalize_grok_plan(plan)
    except (requests.RequestException, ValueError):
        pass
    return None


def fetch_grok_usage(access_token: str) -> dict[str, Any]:
    """Best-effort Grok/xAI usage. May return partial if endpoints differ by plan."""
    out: dict[str, Any] = {
        'source': 'grok_xai_usage',
        'fetched_at': _now_iso(),
        'confidence': 'error',
        'plan': None,
        'windows': [],
        'detail': '',
    }
    if not access_token:
        out['detail'] = 'missing_access_token'
        return out
    out['plan'] = fetch_grok_plan(access_token)
    # Primary: SuperGrok credits via the grok.com billing gRPC-web endpoint
    # (works with our device-flow OAuth tokens; verified live 2026-07-11).
    billing, billing_transient = fetch_grok_billing_ex(access_token)
    if billing is not None:
        out['windows'] = [_window(
            wid='credits',
            label='Credits',
            used_percent=billing['used_percent'],
            resets_at=billing['resets_at'],
        )]
        out['confidence'] = 'exact'
        out['detail'] = 'ok_via_grok_billing'
        _attach_grok_reset_credits(out, access_token)
        return out
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Accept': 'application/json',
        'User-Agent': 'shizuha-hive-quota/1.0',
    }
    last_status = None
    for url in GROK_USAGE_CANDIDATES:
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        except Exception as exc:
            last_status = f'{url}:{type(exc).__name__}'
            continue
        last_status = f'{url}:http_{resp.status_code}'
        if resp.status_code in (401, 403):
            out['detail'] = 'auth_failed'
            return out
        if resp.status_code == 404:
            continue
        if resp.status_code != 200:
            continue
        try:
            data = resp.json()
        except Exception:
            continue
        # Best-effort normalize common shapes
        windows = []
        if isinstance(data, dict):
            for key, label in (
                ('five_hour', 'Session (5h)'),
                ('session', 'Session'),
                ('weekly', 'Weekly'),
                ('monthly', 'Monthly'),
            ):
                block = data.get(key)
                if isinstance(block, dict):
                    w = _codex_window_from_block(block, wid=key, label=label)
                    if w:
                        windows.append(w)
            if not windows and data.get('used_percent') is not None:
                windows.append(_window(
                    wid='usage', label='Usage',
                    used_percent=_util_to_used_percent(data.get('used_percent')),
                    resets_at=_parse_resets_at(data.get('resets_at')),
                ))
        if windows:
            out['windows'] = windows
            out['confidence'] = 'partial'
            out['detail'] = f'ok_via_{url}'
            _attach_grok_reset_credits(out, access_token)
            return out
    if billing_transient:
        # The billing probe failed for a reason that says nothing about this
        # account's plan (timeout / 429 / 5xx). Marking it `unsupported` would
        # put it on the 24h UNSUPPORTED_QUOTA_RECHECK_SECONDS backoff and render
        # it as "no usage API" for a day — the exact symptom reported on
        # 2026-08-04, where `Check now` (force) returned real numbers instantly.
        # `error` keeps it on the normal ~30 min cadence so it self-heals.
        out['detail'] = f'billing_probe_transient ({last_status or "no_response"})'
        out['confidence'] = 'error'
        return out
    # xAI exposes no OAuth usage/quota API (both candidates 404). That is an
    # expected capability gap, NOT a fetch error — mark it cleanly so the UI shows
    # a neutral "usage not available" instead of a raw `...:http_404` error line.
    out['detail'] = 'usage_api_unavailable'
    out['confidence'] = 'unsupported'
    return out


def fetch_google_usage(access_token: str) -> dict[str, Any]:
    """Fetch Google Antigravity / Gemini CLI usage & subscription quota snapshot.

    Calls Google Cloud Code / Antigravity endpoints:
      1. https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist
         Returns plan metadata, tier description, and GCP companion project.
      2. Local/Sidecar agy language server (POST https://127.0.0.1:<port>/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary)
         Returns merged 5-hour and weekly quota windows for Gemini & 3P models.
    """
    import socket
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    out: dict[str, Any] = {
        'source': 'antigravity_cloudcode_usage',
        'fetched_at': _now_iso(),
        'confidence': 'error',
        'plan': None,
        'windows': [],
        'detail': '',
        'raw': None,
    }
    if not access_token:
        out['detail'] = 'missing_access_token'
        return out

    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json',
        'User-Agent': 'antigravity/1.1.7 (Linux; x86_64)',
    }

    # 1. Fetch loadCodeAssist tier metadata
    try:
        resp = requests.post(
            'https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist',
            json={},
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code in (401, 403):
            out['detail'] = f'auth_failed_http_{resp.status_code}'
            return out
        if resp.status_code == 200:
            ca = resp.json()
            tier = ca.get('currentTier') or {}
            plan_name = tier.get('name') or 'Antigravity'
            tier_id = tier.get('id') or 'free-tier'
            out['plan'] = f'{plan_name} ({tier_id})'
            out['confidence'] = 'partial'
            out['detail'] = 'ok_via_loadCodeAssist'
            out['raw'] = {'tier': tier_id, 'project': ca.get('cloudaicompanionProject')}
    except Exception as exc:
        logger.warning('[quota-fetcher] google loadCodeAssist failed: %s', exc)
        out['detail'] = f'loadCodeAssist_error:{type(exc).__name__}'

    # 2. Check mounted host quota file /tmp/shizuha-host/shizuha-agy-quota.json or /tmp/shizuha-agy-quota.json
    quota_files = [
        '/tmp/shizuha-host/shizuha-agy-quota.json',
        '/tmp/shizuha-agy-quota.json',
    ]
    qdata = None
    for qf in quota_files:
        if os.path.exists(qf):
            try:
                with open(qf, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    if content:
                        qdata = json.loads(content)
                        if qdata and 'response' in qdata:
                            break
            except Exception as exc:
                logger.warning('[quota-fetcher] error reading %s: %s', qf, exc)

    windows = []
    if qdata and 'response' in qdata:
        for grp in qdata.get('response', {}).get('groups', []):
            gname = grp.get('displayName', '')
            for b in grp.get('buckets', []):
                rem = b.get('remainingFraction')
                pct = round(rem * 100.0, 2) if rem is not None else None
                wid = str(b.get('bucketId') or '')
                windows.append({
                    'id': wid,
                    'label': f"{gname} - {b.get('displayName', wid)}",
                    'left_percent': pct,
                    'used_percent': round(100.0 - pct, 2) if pct is not None else None,
                    'resets_at': b.get('resetTime'),
                })
        if windows:
            out['windows'] = windows
            out['confidence'] = 'exact'
            out['detail'] = 'ok_via_agy_quota_file'

    if not windows:
        proxy_urls = [
            'http://agy-quota-proxy:38597/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary',
            'http://127.0.0.1:38597/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary',
        ]
        for url in proxy_urls:
            try:
                r = requests.post(
                    url,
                    json={},
                    headers={'Connect-Protocol-Version': '1'},
                    timeout=0.8,
                )
                if r.status_code == 200:
                    qdata = r.json()
                    for grp in qdata.get('response', {}).get('groups', []):
                        gname = grp.get('displayName', '')
                        for b in grp.get('buckets', []):
                            rem = b.get('remainingFraction')
                            pct = round(rem * 100.0, 2) if rem is not None else None
                            wid = str(b.get('bucketId') or '')
                            windows.append({
                                'id': wid,
                                'label': f"{gname} - {b.get('displayName', wid)}",
                                'left_percent': pct,
                                'used_percent': round(100.0 - pct, 2) if pct is not None else None,
                                'resets_at': b.get('resetTime'),
                            })
                    if windows:
                        out['windows'] = windows
                        out['confidence'] = 'exact'
                        out['detail'] = f'ok_via_proxy_{url}'
                        break
            except Exception:
                pass

    if not windows:
        # Fall back to scanning local loopback ports
        open_ports = []
        for p in range(30000, 45000):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.0005)
            if s.connect_ex(('127.0.0.1', p)) == 0:
                open_ports.append(p)
            s.close()

        for port in open_ports:
            try:
                r = requests.post(
                    f'https://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary',
                    json={},
                    headers={'Connect-Protocol-Version': '1'},
                    verify=False,
                    timeout=0.2,
                )
                if r.status_code == 200:
                    qdata = r.json()
                    for grp in qdata.get('response', {}).get('groups', []):
                        gname = grp.get('displayName', '')
                        for b in grp.get('buckets', []):
                            rem = b.get('remainingFraction')
                            pct = round(rem * 100.0, 2) if rem is not None else None
                            wid = str(b.get('bucketId') or '')
                            windows.append({
                                'id': wid,
                                'label': f"{gname} - {b.get('displayName', wid)}",
                                'left_percent': pct,
                                'used_percent': round(100.0 - pct, 2) if pct is not None else None,
                                'resets_at': b.get('resetTime'),
                            })
                    if windows:
                        out['windows'] = windows
                        out['confidence'] = 'exact'
                        out['detail'] = f'ok_via_local_port_{port}'
                        break
            except Exception:
                pass

    # Never invent 100% windows when the language-server quota scrape failed.
    # loadCodeAssist only yields plan/tier metadata — remainingFraction lives on
    # RetrieveUserQuotaSummary (host file / agy-quota-proxy / local LS port).
    # Fabricating full bars made /hive/subscriptions disagree with the live
    # Antigravity UI (operator 2026-07-28: CLI ~44% Gemini weekly left vs Hive
    # "min 100% left" / all bars full). Keep plan metadata at confidence=partial
    # with empty windows so UI shows unknown, not green-full.
    if not out['windows']:
        if out['confidence'] == 'partial':
            out['detail'] = (out.get('detail') or 'ok_via_loadCodeAssist') + ';quota_windows_unavailable'
        elif out['confidence'] != 'error':
            out['confidence'] = 'error'
            out['detail'] = out.get('detail') or 'quota_windows_unavailable'

    return out


def persist_quota_snapshot(
    entry,
    snapshot: dict,
    *,
    expected_fingerprint: str = '',
) -> bool:
    """Persist only the positive, bounded quota schema on TokenEntry.

    For rotating OAuth credentials, callers can bind a provider response to
    the encrypted generation that produced it.  A device re-authorization may
    replace that generation while an old usage request is in flight; in that
    case the stale response must not overwrite the new grant's cleared quota
    state.
    """
    if expected_fingerprint and getattr(entry, 'pk', None):
        from django.db import transaction
        from hive_platform.models import TokenEntry

        with transaction.atomic():
            locked = TokenEntry.objects.select_for_update().get(pk=entry.pk)
            if locked.token_fingerprint != expected_fingerprint:
                return False
            return persist_quota_snapshot(locked, snapshot)

    from django.utils import timezone
    from hive_platform.quota_projection_v2 import project_quota_snapshot
    from .token_coordinator import (
        _preserve_quota_cooldown,
        confirmed_xai_quota_exhausted_until,
    )

    entry.quota_snapshot = project_quota_snapshot(snapshot)
    entry.last_checked_at = timezone.now()
    entry.save(update_fields=['quota_snapshot', 'last_checked_at', 'updated_at'])
    exhausted_until = confirmed_xai_quota_exhausted_until(entry)
    if exhausted_until is not None:
        _preserve_quota_cooldown(entry, exhausted_until)
    return True
