"""Operator-only, time-boxed, per-seat model-lease pin (CTX-1055 / CTX-5382).

The lease planner (``fleet.model_lease``) is deliberately fair: free slots go
least-recently-held among worthy waiters, and the only reservation surface is
team-level (``LeaseCriticalTeamPolicy``).  That leaves an operator no supported
way to seat ONE specific agent for a short, time-critical job (for example the
only qualified architect for a privileged review) without hibernating other
seats by hand and hoping the planner hands the freed slot to the right agent.

This module adds the smallest such tool:

* **Default off.**  With no pin set, the planner behaves exactly as before.
* **Operator-only.**  Pins are set and cleared only by the
  ``lease_pin`` management command, so they need cluster exec on the Hive pod.
  No HTTP or MCP surface exists.
* **Time-boxed.**  At most one pin at a time.  Every pin carries a TTL capped
  at ``OPERATOR_PIN_MAX_TTL_S``, is stored with that cache TTL, and expires on
  its own.  An expired pin is ignored even if the cache still holds it.
* **Audited.**  Every set, clear and expiry is logged at WARNING with an
  ``AUDIT lease-operator-pin`` prefix and appended to a capped audit book.
  The planner also logs each tick on which a pin influenced a decision.

Planner semantics, applied in ``_reconcile_one_pool``: the pinned agent is
treated as a one-seat critical-team floor target that is processed first.  It
takes the next free slot ahead of ordinary fairness.  When the board is full
it may preempt only a QUIET, non-reserved holder.  Work never evicts work, the
same fence the HIVE-2841 floor uses.  Once seated, it is keep-fenced until the
pin expires.
"""
from __future__ import annotations

import logging
import time

from django.core.cache import cache

logger = logging.getLogger(__name__)

OPERATOR_PIN_CACHE_KEY = 'fleet:lease-operator-pin:v1'
OPERATOR_PIN_AUDIT_CACHE_KEY = 'fleet:lease-operator-pin-audit:v1'
OPERATOR_PIN_MAX_TTL_S = 120 * 60
OPERATOR_PIN_MIN_TTL_S = 60
OPERATOR_PIN_AUDIT_MAX = 200
OPERATOR_PIN_AUDIT_TTL_S = 30 * 24 * 3600
OPERATOR_PIN_SLUG = 'operator-pin'


def _bare(username: str) -> str:
    return str(username or '').strip().removeprefix('agent-')


def _audit(event: str, **fields) -> None:
    entry = {'event': event, 'at': time.time(), **fields}
    logger.warning('AUDIT lease-operator-pin %s %s', event, fields)
    try:
        book = cache.get(OPERATOR_PIN_AUDIT_CACHE_KEY)
        book = list(book) if isinstance(book, list) else []
        book.append(entry)
        cache.set(
            OPERATOR_PIN_AUDIT_CACHE_KEY, book[-OPERATOR_PIN_AUDIT_MAX:],
            OPERATOR_PIN_AUDIT_TTL_S,
        )
    except Exception:  # noqa: BLE001 — audit book is best-effort; the log line is the record
        logger.warning('lease-operator-pin audit book write failed', exc_info=True)


def set_operator_pin(username: str, *, ttl_seconds: int, reason: str, actor: str,
                     now: float | None = None) -> dict:
    """Pin ``username`` for ``ttl_seconds`` (capped).  Replaces any prior pin."""
    uname = _bare(username)
    if not uname:
        raise ValueError('username is required')
    reason = str(reason or '').strip()
    actor = str(actor or '').strip()
    if not reason:
        raise ValueError('reason is required (audited)')
    if not actor:
        raise ValueError('actor is required (audited)')
    try:
        ttl = int(ttl_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError('ttl_seconds must be an integer') from exc
    if ttl < OPERATOR_PIN_MIN_TTL_S or ttl > OPERATOR_PIN_MAX_TTL_S:
        raise ValueError(
            f'ttl_seconds must be between {OPERATOR_PIN_MIN_TTL_S} and '
            f'{OPERATOR_PIN_MAX_TTL_S}'
        )
    now = time.time() if now is None else float(now)
    prior = active_operator_pin(now=now)
    pin = {
        'username': uname,
        'reason': reason[:280],
        'actor': actor[:120],
        'set_at': now,
        'expires_at': now + ttl,
    }
    cache.set(OPERATOR_PIN_CACHE_KEY, pin, ttl)
    if prior and prior.get('username') != uname:
        _audit('replace', previous=prior.get('username'), username=uname,
               actor=pin['actor'])
    _audit('set', username=uname, ttl_s=ttl, reason=pin['reason'], actor=pin['actor'],
           expires_at=pin['expires_at'])
    return dict(pin)


def clear_operator_pin(*, actor: str, reason: str = '') -> dict | None:
    """Remove the pin (if any).  Returns the removed pin."""
    pin = active_operator_pin()
    cache.delete(OPERATOR_PIN_CACHE_KEY)
    _audit('clear', username=(pin or {}).get('username'),
           actor=str(actor or '')[:120], reason=str(reason or '')[:280])
    return pin


def active_operator_pin(*, now: float | None = None) -> dict | None:
    """The live pin, or None.  Fails closed (no pin) on any cache/shape error."""
    try:
        raw = cache.get(OPERATOR_PIN_CACHE_KEY)
    except Exception:  # noqa: BLE001 — cache must never break leasing
        return None
    if not isinstance(raw, dict):
        return None
    uname = _bare(raw.get('username') or '')
    try:
        expires_at = float(raw.get('expires_at'))
    except (TypeError, ValueError):
        return None
    now = time.time() if now is None else float(now)
    if not uname or expires_at <= now:
        if uname:
            try:
                cache.delete(OPERATOR_PIN_CACHE_KEY)
            except Exception:  # noqa: BLE001
                pass
            _audit('expire', username=uname)
        return None
    return {**raw, 'username': uname, 'expires_at': expires_at}


def operator_pinned_usernames(*, now: float | None = None) -> set[str]:
    pin = active_operator_pin(now=now)
    return {pin['username']} if pin else set()


def audit_log() -> list[dict]:
    try:
        book = cache.get(OPERATOR_PIN_AUDIT_CACHE_KEY)
    except Exception:  # noqa: BLE001
        return []
    return list(book) if isinstance(book, list) else []
