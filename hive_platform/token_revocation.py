"""
CANONICAL — keep in sync, do not fork.

Shared token-revocation client for all RS256 verifiers within the
shizuha-tasks service. Copy verbatim (with this header) when pasting into
sibling services (claws, wiki-server, admin, …).

FAIL CLOSED: any lookup error (network, timeout, non-200, bad JSON) returns
True (is_revoked → deny access). This is intentional and is the opposite of
the advisory path in id/mcp-server/base.py, which is allowed to fail open
because it only influences logging; an authentication gate must deny on doubt.

The check applies to ALL token types, including non-OAuth internal/agent/
session JWTs (no `client_id` claim). SCLI-376: those tokens ARE tracked in
the id revocation store and MUST be rejected once revoked — skipping them
created a split-brain runtime where ID 401'd a revoked broker JWT while
Hive/Pulse kept accepting it.

Usage:
    from hive_platform.token_revocation import is_token_revoked

    if is_token_revoked(claims, raw_token):
        raise InvalidToken("Token has been revoked")

PLAT-1032 / PLAT-1034 (umbrella: RS256 revocation enforcement).
"""

import hashlib
import logging
import os
import time
from typing import Dict, Tuple

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-process revocation cache
# ---------------------------------------------------------------------------
# Keyed by SHA-256(raw_token hex digest).
# Value: (expires_at: float, is_revoked: bool) where expires_at is
# time.monotonic() + TTL.
#
# Thread safety: the GIL protects individual dict reads/writes on CPython,
# so no additional locking is needed for WSGI-style workers.  This cache is
# per-process; a fresh worker starts uncached and warms up on first request.
# ---------------------------------------------------------------------------
_REVOCATION_CACHE: Dict[str, Tuple[float, bool]] = {}
_CACHE_TTL_SECONDS: int = 30
# Bound the TTL cache so it can't grow unbounded for the process lifetime: entries
# expire logically but are reaped lazily on insert when the cache hits this cap.
_MAX_CACHE_ENTRIES: int = 10000


def _bound_cache(now: float) -> None:
    """Keep _REVOCATION_CACHE bounded. Called on insert when at the cap: first reap
    logically-expired entries; if still full (many concurrently-live tokens), evict
    the soonest-to-expire 10% as a hard ceiling. Lock-free (matches this module's
    GIL-atomic dict style); snapshots items() before mutating to avoid resize races."""
    if len(_REVOCATION_CACHE) < _MAX_CACHE_ENTRIES:
        return
    for k, (expires_at, _) in list(_REVOCATION_CACHE.items()):
        if expires_at <= now:
            _REVOCATION_CACHE.pop(k, None)
    if len(_REVOCATION_CACHE) >= _MAX_CACHE_ENTRIES:
        # Evict the soonest-to-expire entries down to a 90%-of-cap watermark, so we
        # land strictly below the cap (not just trim a fixed slice) and don't
        # re-trim on every subsequent insert.
        oldest = sorted(list(_REVOCATION_CACHE.items()), key=lambda kv: kv[1][0])
        target = _MAX_CACHE_ENTRIES * 9 // 10
        for k, _ in oldest[: max(1, len(_REVOCATION_CACHE) - target)]:
            _REVOCATION_CACHE.pop(k, None)


def _revocation_status_url() -> str:
    """Derive the id-service revocation-status endpoint from the issuer env."""
    explicit = os.environ.get("SHIZUHA_REVOCATION_STATUS_URL", "").strip()
    if explicit:
        return explicit
    issuer = (
        os.environ.get("SHIZUHA_EXPECTED_ISSUER") or "http://shizuha-id:8001"
    ).rstrip("/")
    return f"{issuer}/api/oauth/revocation-status"


def _cache_key(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _lookup_revocation(raw_token: str) -> dict | None:
    """Network call to the id revocation-status endpoint — FAIL CLOSED.

    Returns the parsed JSON dict on success, or None on any error, timeout,
    non-200 response, or malformed body. A None return means the lookup failed
    and the caller MUST fail closed (treat the token as revoked).
    """
    url = _revocation_status_url()
    try:
        resp = requests.post(url, json={"token": raw_token}, timeout=2, allow_redirects=False)
    except Exception as exc:
        logger.warning(
            "Token revocation-status check failed (network/timeout): %s — "
            "FAILING CLOSED (treating token as revoked).",
            exc,
        )
        return None  # FAIL CLOSED

    if resp.status_code != 200:
        logger.warning(
            "Token revocation-status returned HTTP %s — "
            "FAILING CLOSED (treating token as revoked).",
            resp.status_code,
        )
        return None  # FAIL CLOSED

    try:
        data = resp.json()
        if not isinstance(data, dict) or not isinstance(data.get("active"), bool):
            logger.warning("Token revocation-status response has no boolean active verdict; denying access.")
            return None
        return data
    except Exception as exc:
        logger.warning(
            "Token revocation-status response is not JSON: %s — "
            "FAILING CLOSED (treating token as revoked).",
            exc,
        )
        return None  # FAIL CLOSED


def is_token_revoked(claims: dict, raw_token: str) -> bool:
    """Return True if the access token has been revoked.

    SCLI-376 (hiro architecture determination, ID-1 §8): every consumer
    rejects a revoked access token on its next request — REGARDLESS of token
    type. Agent/session/broker JWTs carry no `client_id` claim but ARE tracked
    in and revoked from the id revocation store (e.g. broker logout revokes the
    agent JWT; ID then 401s it while Hive/Pulse previously accepted it — the
    split-brain runtime this check closes). So the check is applied to every
    validated token; it is no longer skipped for missing client_id.

    PLAT-5996 (ID-1 §3.1 primary): the revocation-status response also carries
    the subject's CURRENT ``token_version``. A token whose ``token_version``
    claim is strictly less than the current version is a replayed
    pre-revocation token and is rejected EVEN IF the OAuth-store row is gone
    (revive-by-replay closure). The version check is gated on the claim being
    present in BOTH the token and the response, so it is a no-op during the
    coordinated rollout (before ID mints the claim) and on rollback (consumers
    ignore the claim).

    Results are cached for _CACHE_TTL_SECONDS (30 s) to avoid hammering the
    id service on every request.

    FAIL CLOSED: any lookup error returns True — access is denied.

    Args:
        claims:    Decoded, already-verified JWT payload dict.
        raw_token: Encoded JWT string. Used as the cache key (SHA-256)
                   and as the payload body for the revocation-status call.

    Returns:
        True  — token is revoked, or the lookup failed (deny access).
        False — token is active (allow).
    """
    key = _cache_key(raw_token)
    now = time.monotonic()
    cached = _REVOCATION_CACHE.get(key)
    if cached is not None:
        expires_at, is_revoked = cached
        if now < expires_at:
            return is_revoked

    data = _lookup_revocation(raw_token)
    if data is None:
        # Deny this request without caching an indeterminate transport failure.
        # The next request must be able to observe ID recovery.
        _REVOCATION_CACHE.pop(key, None)
        return True  # FAIL CLOSED, uncached
    else:
        active = data.get("active", False)
        is_revoked = not bool(active)
        if not is_revoked:
            # PLAT-5996: stale-version check. Only enforced when BOTH the token
            # carries the claim (ID mints it) AND the response reports a current
            # version — absent either side, fall back to the active check alone
            # (coordinated rollout / rollback path).
            token_version = claims.get("token_version")
            current_version = data.get("token_version")
            if (
                token_version is not None
                and current_version is not None
                and token_version < current_version
            ):
                is_revoked = True
    _bound_cache(now)  # P2: keep the TTL cache from growing unbounded
    _REVOCATION_CACHE[key] = (now + _CACHE_TTL_SECONDS, is_revoked)
    return is_revoked
