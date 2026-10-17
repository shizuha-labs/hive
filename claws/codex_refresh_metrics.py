"""Codex/OpenAI refresh-loop + revoked-token observability.

HIVE-583 adds fail-loud liveness/invariant metrics for the HA background
refresh loop. HIVE-585 adds per-account revoked refresh-token observability and
re-auth deep links. Keep both in one exporter so the refresh mechanism and the
revoked-token failure path coexist after HIVE-585 merged.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone as dt_timezone
from urllib.parse import quote, urlencode

from django.core.cache import cache
from django.utils import timezone

_TTL = 7 * 24 * 60 * 60
_LOOP_KEY = 'hive:codex-refresh'
_ACCOUNT_KEY = 'hive:hive585:codex_refresh'

# HIVE-583: loop liveness/invariant outcomes.
OUTCOMES = (
    'leader_held',
    'refreshed',
    'skipped_fresh',
    'failed',
)

# HIVE-585: bounded account statuses for dashboard + Prometheus labels.
STATUSES = ('active', 'expired', 'revoked', 'cooling', 'disabled', 'unknown')


def _incr(key):
    try:
        cache.add(key, 0, _TTL)
        cache.incr(key)
    except Exception:
        try:
            cache.set(key, (cache.get(key) or 0) + 1, _TTL)
        except Exception:
            pass


def _set(key, value):
    try:
        cache.set(key, value, _TTL)
    except Exception:
        pass


def _label(value) -> str:
    return str(value or '').replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')


def _entry_key(entry) -> str:
    return str(getattr(entry, 'id', '') or 'unknown')


def _jwt_payload(token: str) -> dict:
    from . import token_coordinator as tc

    return tc._jwt_payload(token or '')


# ── HIVE-583 loop outcome metrics ────────────────────────────────────────────

def record(outcome):
    """Record one HIVE-583 refresh-loop liveness outcome."""
    if outcome not in OUTCOMES:
        return
    _incr(f'{_LOOP_KEY}:{outcome}')


def loop_snapshot() -> dict:
    return {o: int(cache.get(f'{_LOOP_KEY}:{o}') or 0) for o in OUTCOMES}


# ── HIVE-585 per-account revoked-token observability ─────────────────────────

def codex_payload(entry) -> dict:
    """Best-effort decrypted structured OAuth payload for an OpenAI TokenEntry."""
    try:
        raw = entry.get_token()
    except Exception:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def access_expires_at(payload: dict):
    exp = _jwt_payload(str(payload.get('access_token') or '')).get('exp')
    if not isinstance(exp, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(exp, tz=dt_timezone.utc)
    except Exception:
        return None


def _to_aware_utc(value):
    """Normalize a datetime for comparison regardless of USE_TZ.

    Hive runs USE_TZ=False, so timezone.now() and DB datetimes are NAIVE local
    time while JWT expiries parse as AWARE UTC. Comparing them raises TypeError,
    which the health overview swallowed — every Codex entry with a valid access
    token showed health=unknown."""
    if value is None:
        return None
    if timezone.is_aware(value):
        return value.astimezone(dt_timezone.utc)
    return timezone.make_aware(value, timezone.get_current_timezone()).astimezone(dt_timezone.utc)


def entry_observability(entry, *, now=None) -> dict:
    """Redacted per-account state for the dashboard + Prometheus labels."""
    from hive_platform.models import TokenEntry

    now = _to_aware_utc(now or timezone.now())
    payload = codex_payload(entry)
    email = str(payload.get('email') or getattr(entry, 'account_email', '') or entry.label or 'unknown')
    account_id = str(payload.get('account_id') or '')
    expires_at = _to_aware_utc(access_expires_at(payload))
    cooldown_until = _to_aware_utc(getattr(entry, 'cooldown_until', None))
    last_status = getattr(entry, 'last_status', '') or TokenEntry.LastStatus.UNKNOWN

    if not getattr(entry, 'active', False):
        status = 'revoked' if last_status == TokenEntry.LastStatus.AUTH_FAILED else 'disabled'
    elif last_status == TokenEntry.LastStatus.AUTH_FAILED:
        status = 'revoked'
    elif cooldown_until and cooldown_until > now:
        status = 'cooling'
    elif expires_at and expires_at <= now:
        status = 'expired'
    elif last_status == TokenEntry.LastStatus.OK:
        status = 'active'
    else:
        status = 'unknown'

    reauth_path = '/hive/credentials/codex/re-auth?' + urlencode({
        'scope': entry.pool.owner_kind,
        'pool': str(entry.pool_id),
        'entry': str(entry.id),
        'email': email,
    })
    return {
        'email': email,
        'account_id': account_id,
        'status': status,
        'access_expires_at': expires_at.isoformat() if expires_at else None,
        'last_refresh_at': (
            entry.last_checked_at.isoformat() if getattr(entry, 'last_checked_at', None)
            else entry.updated_at.isoformat() if getattr(entry, 'updated_at', None)
            else None
        ),
        'reauth_path': reauth_path,
    }


def record_refresh_401(entry, *, status_code=401):
    """Increment the revoked-token counter for one OpenAI TokenEntry."""
    key = _entry_key(entry)
    _incr(f'{_ACCOUNT_KEY}:401:{key}')
    _set(f'{_ACCOUNT_KEY}:last_401:{key}', int(time.time()))
    _set(f'{_ACCOUNT_KEY}:last_status_code:{key}', int(status_code or 0))


def _openai_entries():
    from hive_platform.models import TokenEntry, TokenPool

    return (
        TokenEntry.objects
        .filter(
            pool__provider=TokenPool.Provider.OPENAI,
            deleted_at__isnull=True,
        )
        .select_related('pool')
        .order_by('pool__owner_kind', 'label', 'created_at')
    )


def account_snapshot(entries=None) -> list[dict]:
    rows = []
    for entry in (entries if entries is not None else _openai_entries()):
        info = entry_observability(entry)
        key = _entry_key(entry)
        rows.append({
            'entry_id': key,
            'label': entry.label,
            'email': info['email'],
            'email_urlencoded': quote(info['email'], safe=''),
            'status': info['status'],
            'access_expires_at': info['access_expires_at'],
            'last_refresh_at': info['last_refresh_at'],
            'reauth_path': info['reauth_path'],
            'refresh_401_total': int(cache.get(f'{_ACCOUNT_KEY}:401:{key}') or 0),
            'last_401_at': int(cache.get(f'{_ACCOUNT_KEY}:last_401:{key}') or 0),
            'last_status_code': int(cache.get(f'{_ACCOUNT_KEY}:last_status_code:{key}') or 0),
        })
    return rows


def snapshot(entries=None):
    """Backward-compatible snapshot helper.

    HIVE-583 tests call ``snapshot()`` for loop outcome counters; HIVE-585 calls
    ``snapshot([entry])`` for account observability rows.
    """
    if entries is None:
        return loop_snapshot()
    return account_snapshot(entries)


def to_prometheus() -> str:
    loop = loop_snapshot()
    rows = account_snapshot()
    lines = [
        '# HELP hive_codex_refresh_total Codex/OpenAI token refresh loop terminal outcomes (HIVE-583).',
        '# TYPE hive_codex_refresh_total counter',
    ]
    for outcome in OUTCOMES:
        lines.append(f'hive_codex_refresh_total{{outcome="{outcome}"}} {loop[outcome]}')

    lines.extend([
        '# HELP hive_codex_refresh_metrics_present Codex refresh observability exporter liveness (HIVE-585).',
        '# TYPE hive_codex_refresh_metrics_present gauge',
        'hive_codex_refresh_metrics_present 1',
        '# HELP hive_codex_refresh_accounts OpenAI/Codex TokenEntry rows observed by Hive.',
        '# TYPE hive_codex_refresh_accounts gauge',
        f'hive_codex_refresh_accounts {len(rows)}',
        '# HELP hive_codex_refresh_401_total OpenAI/Codex OAuth refresh attempts rejected with HTTP 401 by account (revoked token; fail-loud).',
        '# TYPE hive_codex_refresh_401_total counter',
    ])
    for row in rows:
        labels = (
            f'account_email="{_label(row["email"])}",'
            f'account_email_urlencoded="{_label(row["email_urlencoded"])}",'
            f'label="{_label(row["label"])}"'
        )
        lines.append(f'hive_codex_refresh_401_total{{{labels}}} {row["refresh_401_total"]}')
    lines.extend([
        '# HELP hive_codex_account_status Current redacted Codex account status, one-hot by status.',
        '# TYPE hive_codex_account_status gauge',
    ])
    for row in rows:
        for status in STATUSES:
            labels = (
                f'account_email="{_label(row["email"])}",'
                f'account_email_urlencoded="{_label(row["email_urlencoded"])}",'
                f'label="{_label(row["label"])}",status="{status}"'
            )
            lines.append(f'hive_codex_account_status{{{labels}}} {1 if row["status"] == status else 0}')
        if row['last_401_at']:
            labels = (
                f'account_email="{_label(row["email"])}",'
                f'account_email_urlencoded="{_label(row["email_urlencoded"])}",'
                f'label="{_label(row["label"])}"'
            )
            lines.append(f'hive_codex_refresh_last_401_timestamp_seconds{{{labels}}} {row["last_401_at"]}')
    return '\n'.join(lines) + '\n'
