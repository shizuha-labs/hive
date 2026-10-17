"""Claude / Anthropic OAuth code-paste auth — ingestion into the HIVE Token Coordinator.

Operator-facing browser flow (mirrors ``codex_device_auth`` / ``grok_device_auth``):
the Hive UI shows the OFFICIAL Claude Code sign-in link (same public client_id and
authorize page the Claude Code CLI uses — the interactive login itself stays fully
on Anthropic's side), the operator signs in and pastes back the ``code#state``
string Anthropic's callback page displays, and Hive exchanges it (PKCE) and
envelope-encrypts the bundle into the org Anthropic :class:`~hive_platform.models.TokenPool`.

Why this exists: ``claude setup-token`` OATs are user:inference-only, so the
subscriptions board can never read usage/profile with them (403 scope error).
A full login mints ``user:profile`` + a refresh token, enabling CodexBar-style
quota bars, account identity, and hive-side refresh.

Flow:
  1. start_login()     -> PKCE verifier cached by state; returns authorize_url
  2. Human opens the link, signs in, copies the code the callback page shows
  3. exchange_code()   -> POST /v1/oauth/token grant_type=authorization_code
  4. ingest_claude_account() -> upsert TokenEntry (label ``org-claude-<email>``)
     in the org Anthropic pool + auto-fill account_email + quota snapshot
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone as dt_timezone
from urllib.parse import urlencode

import requests
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

# Anthropic's official public Claude Code OAuth client. Sign-in happens on
# claude.ai; we only generate PKCE material and exchange the pasted code.
CLAUDE_OAUTH_CLIENT_ID = '9d1c250a-e61b-44d9-88ed-5944d1962f5e'
AUTHORIZE_URL = 'https://claude.ai/oauth/authorize'
TOKEN_URL = 'https://console.anthropic.com/v1/oauth/token'
REDIRECT_URI = 'https://console.anthropic.com/oauth/code/callback'
SCOPES = 'org:create_api_key user:profile user:inference'

_STATE_CACHE_PREFIX = 'hive:claude-oauth:state:'
_STATE_TTL_SECONDS = 15 * 60


class ClaudeOAuthError(RuntimeError):
    """Claude OAuth failed (start, exchange, refresh, or ingest)."""


def start_login() -> dict:
    """Step 1 — mint PKCE material and build the official sign-in URL."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip('=')
    state = secrets.token_urlsafe(32)
    cache.set(f'{_STATE_CACHE_PREFIX}{state}', verifier, _STATE_TTL_SECONDS)
    authorize_url = f'{AUTHORIZE_URL}?' + urlencode({
        'code': 'true',
        'client_id': CLAUDE_OAUTH_CLIENT_ID,
        'response_type': 'code',
        'redirect_uri': REDIRECT_URI,
        'scope': SCOPES,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
        'state': state,
    })
    return {
        'authorize_url': authorize_url,
        'state': state,
        'expires_in': _STATE_TTL_SECONDS,
        'instructions': 'Sign in, then paste the code the page shows (it looks like code#state).',
    }


def exchange_code(pasted: str, state: str = '', *, timeout_seconds: int = 20) -> dict:
    """Step 3 — exchange the pasted ``code`` / ``code#state`` for tokens."""
    raw = (pasted or '').strip()
    if not raw:
        raise ClaudeOAuthError('code is required')
    code, _, pasted_state = raw.partition('#')
    state = (pasted_state or state or '').strip()
    code = code.strip()
    if not code or not state:
        raise ClaudeOAuthError('paste the full code shown by Anthropic (code#state)')
    verifier = cache.get(f'{_STATE_CACHE_PREFIX}{state}')
    if not verifier:
        raise ClaudeOAuthError('sign-in session expired — press Connect again for a fresh link')
    try:
        resp = requests.post(
            TOKEN_URL,
            json={
                'grant_type': 'authorization_code',
                'code': code,
                'state': state,
                'client_id': CLAUDE_OAUTH_CLIENT_ID,
                'redirect_uri': REDIRECT_URI,
                'code_verifier': verifier,
            },
            headers={'Content-Type': 'application/json'},
            timeout=timeout_seconds,
        )
    except Exception as exc:
        raise ClaudeOAuthError(f'token exchange request failed: {type(exc).__name__}') from exc
    if resp.status_code != 200:
        raise ClaudeOAuthError(f'token exchange returned {resp.status_code}: {resp.text[:200]}')
    try:
        tokens = resp.json()
    except Exception as exc:
        raise ClaudeOAuthError('token exchange returned invalid JSON') from exc
    if not tokens.get('access_token'):
        raise ClaudeOAuthError('token exchange response missing access_token')
    cache.delete(f'{_STATE_CACHE_PREFIX}{state}')
    return tokens


def refresh_claude_oauth(payload: dict, *, timeout_seconds: int = 20) -> dict:
    """Refresh an ingested bundle. Returns the token-endpoint response dict."""
    refresh_token = str(
        payload.get('refresh_token') or payload.get('refreshToken') or ''
    )
    if not refresh_token:
        raise ClaudeOAuthError('no refresh_token in bundle')
    try:
        resp = requests.post(
            TOKEN_URL,
            json={
                'grant_type': 'refresh_token',
                'refresh_token': refresh_token,
                'client_id': CLAUDE_OAUTH_CLIENT_ID,
            },
            headers={'Content-Type': 'application/json'},
            timeout=timeout_seconds,
        )
    except Exception as exc:
        raise ClaudeOAuthError(f'refresh request failed: {type(exc).__name__}') from exc
    if resp.status_code in (400, 401, 403):
        raise ClaudeOAuthError(f'refresh rejected ({resp.status_code}): {resp.text[:160]}')
    if resp.status_code != 200:
        raise ClaudeOAuthError(f'refresh returned {resp.status_code}')
    try:
        tokens = resp.json()
    except Exception as exc:
        raise ClaudeOAuthError('refresh returned invalid JSON') from exc
    if not tokens.get('access_token'):
        raise ClaudeOAuthError('refresh response missing access_token')
    return tokens


def build_bundle(tokens: dict, *, email: str = '') -> dict:
    """Normalize a token-endpoint response into the stored bundle shape."""
    expires_at = ''
    expires_in = tokens.get('expires_in')
    if expires_in is not None:
        try:
            expires_at = (
                datetime.now(dt_timezone.utc) + timedelta(seconds=int(expires_in))
            ).isoformat()
        except (TypeError, ValueError):
            expires_at = ''
    scope = tokens.get('scope') or SCOPES
    return {
        'type': 'claude_oauth',
        'email': email,
        'access_token': str(tokens.get('access_token') or ''),
        'refresh_token': str(tokens.get('refresh_token') or ''),
        'expires_at': expires_at,
        'scopes': scope.split() if isinstance(scope, str) else list(scope or []),
        'client_id': CLAUDE_OAUTH_CLIENT_ID,
        'token_endpoint': TOKEN_URL,
    }


def ingest_claude_account(tokens: dict, *, actor: str = '') -> dict:
    """Upsert the OAuth bundle as a TokenEntry in the org Anthropic pool.

    Idempotent per account (label ``org-claude-<email>``): re-authing rotates the
    secret in place. Auto-fills ``account_email`` from /api/oauth/profile and
    refreshes the quota snapshot so the board shows bars immediately.
    """
    from hive_platform.models import TokenEntry, TokenPool
    from .quota_fetchers import fetch_claude_profile_email

    access = str(tokens.get('access_token') or '')
    account = tokens.get('account') if isinstance(tokens.get('account'), dict) else {}
    email = (
        fetch_claude_profile_email(access)
        or str(account.get('email_address') or account.get('email') or '')
    )
    bundle = build_bundle(tokens, email=email)

    # Personal-ownership model (operator 2026-07-11): new credentials land in
    # the platform owner's PERSONAL pool (labels stay stable for re-auth dedup).
    from .token_coordinator import fleet_credentials_pool
    pool = fleet_credentials_pool(TokenPool.Provider.ANTHROPIC)
    label = f'org-claude-{email or secrets.token_hex(4)}'
    entry = TokenEntry.objects.filter(pool=pool, label=label).first()
    created = entry is None
    if entry is None:
        entry = TokenEntry(pool=pool, label=label, priority=100)
    entry.set_token(json.dumps(bundle, sort_keys=True))
    entry.account_email = email
    entry.active = True
    entry.last_status = TokenEntry.LastStatus.OK
    entry.last_checked_at = timezone.now()
    entry.cooldown_until = None
    entry.save()

    quota_windows = 0
    try:
        from .token_coordinator import refresh_entry_quota_snapshot

        refresh_entry_quota_snapshot(entry, force=True, refresh_oauth=False)
        entry.refresh_from_db()
        quota_windows = len((entry.quota_snapshot or {}).get('windows') or [])
    except Exception:  # noqa: BLE001 — quota is best-effort at ingest time
        logger.exception('[claude-oauth] quota snapshot after ingest failed entry=%s', entry.id)

    logger.info(
        '[claude-oauth] ingested org claude account email=%s entry=%s created=%s windows=%s actor=%s',
        email, entry.id, created, quota_windows, actor or '?',
    )
    return {
        'email': email,
        'entry_id': str(entry.id),
        'created': created,
        'quota_windows': quota_windows,
    }
