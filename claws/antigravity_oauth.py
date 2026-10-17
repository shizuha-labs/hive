"""Google Antigravity / Gemini CLI token ingestion into the HIVE Token Coordinator.

Two ingestion paths:

1. **Direct import** (recommended):
   The operator runs ``agy`` (Antigravity CLI), which handles the full Google
   OAuth loopback flow and writes ``~/.gemini/oauth_creds.json``.  The operator
   then pastes that JSON into the Hive UI → ``import_token_json()`` validates
   and encrypts the token bundle into the owner's Google TokenPool.

2. **Code-paste OAuth** (fallback):
   Hive builds the official Gemini CLI authorize URL (same client-id, scopes,
   PKCE) and the operator signs in, copies the code shown on the callback page,
   and pastes it back.  Hive exchanges it server-side.

Both paths converge on ``ingest_antigravity_account()`` for encrypted storage.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
from datetime import timedelta
from urllib.parse import urlencode

import requests
from django.core.cache import cache
from django.utils import timezone

from . import token_coordinator as tc

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Google Antigravity / Gemini CLI public OAuth credentials (installed-app)
# These are the *same* values that `agy` / Gemini CLI ships with.
# ---------------------------------------------------------------------------
GOOGLE_ANTIGRAVITY_CLIENT_ID = '681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com'
GOOGLE_ANTIGRAVITY_CLIENT_SECRET = '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'
AUTHORIZE_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
USERINFO_URL = 'https://www.googleapis.com/oauth2/v3/userinfo'
# The Gemini CLI uses a loopback redirect; for our web-initiated code-paste
# fallback we use the same redirect so the token exchange succeeds.
REDIRECT_URI = 'http://localhost:1'
SCOPES = (
    'https://www.googleapis.com/auth/cloud-platform '
    'https://www.googleapis.com/auth/userinfo.email '
    'https://www.googleapis.com/auth/userinfo.profile '
    'openid'
)

_STATE_CACHE_PREFIX = 'hive:antigravity-oauth:state:'
_STATE_TTL_SECONDS = 15 * 60


class AntigravityOAuthError(RuntimeError):
    """Google Antigravity OAuth failed (start, exchange, or ingest)."""


# ──────────────────────────────────────────────────────────────────────────────
# Path 1 — Direct token import (recommended)
# ──────────────────────────────────────────────────────────────────────────────

def import_token_json(
    raw_json: str,
    *,
    owner_id: str | None = None,
) -> dict:
    """Import a token bundle from ``~/.gemini/oauth_creds.json`` verbatim.

    The *raw_json* is the full content of that file.  We validate that it
    contains at minimum an ``access_token`` (or ``refresh_token``), derive the
    account email from the ``id_token`` or a UserInfo call, then encrypt and
    store it.
    """
    try:
        tokens = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise AntigravityOAuthError(f'invalid JSON: {exc}') from exc

    if not isinstance(tokens, dict):
        raise AntigravityOAuthError('expected a JSON object with token fields')

    access_token = str(tokens.get('access_token') or '').strip()
    refresh_token = str(tokens.get('refresh_token') or '').strip()

    if not access_token and not refresh_token:
        raise AntigravityOAuthError(
            'token JSON must contain at least access_token or refresh_token'
        )

    # Try to extract email from the id_token JWT payload (no verification
    # needed — we just want the claim for labelling).
    email = _email_from_id_token(tokens.get('id_token', ''))
    if not email and access_token:
        email = _fetch_google_user_email(access_token)

    return ingest_antigravity_account(tokens, account_email=email, owner_id=owner_id)


def refresh_google_oauth_token(refresh_token: str) -> dict:
    """Refresh a Google access token using an OAuth refresh_token."""
    if not refresh_token:
        raise AntigravityOAuthError('missing refresh_token')
    resp = requests.post(
        TOKEN_URL,
        data={
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token,
            'client_id': GOOGLE_ANTIGRAVITY_CLIENT_ID,
            'client_secret': GOOGLE_ANTIGRAVITY_CLIENT_SECRET,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise AntigravityOAuthError(
            f'refresh failed: http_{resp.status_code} {resp.text}'
        )
    return resp.json()


def _email_from_id_token(raw_id_token: str) -> str:
    """Best-effort email extraction from a Google id_token JWT (unverified)."""
    try:
        parts = (raw_id_token or '').split('.')
        if len(parts) < 2:
            return ''
        # JWT payload is base64url-encoded
        padded = parts[1] + '=' * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        return str(payload.get('email') or '').strip().lower()
    except Exception:
        return ''



# ──────────────────────────────────────────────────────────────────────────────
# Path 2 — Code-paste OAuth (fallback)
# ──────────────────────────────────────────────────────────────────────────────

def start_login() -> dict:
    """Mint PKCE material and build the official Gemini CLI sign-in URL."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip('=')
    state = secrets.token_urlsafe(32)
    cache.set(f'{_STATE_CACHE_PREFIX}{state}', verifier, _STATE_TTL_SECONDS)

    authorize_url = f'{AUTHORIZE_URL}?' + urlencode({
        'client_id': GOOGLE_ANTIGRAVITY_CLIENT_ID,
        'response_type': 'code',
        'redirect_uri': REDIRECT_URI,
        'scope': SCOPES,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
        'state': state,
        'access_type': 'offline',
        'prompt': 'consent',
    })
    return {
        'authorize_url': authorize_url,
        'state': state,
        'expires_in': _STATE_TTL_SECONDS,
        'instructions': (
            'Sign in with Google, then copy and paste the authorization code '
            'shown on the callback page.'
        ),
    }


def exchange_code(
    pasted: str,
    state: str = '',
    *,
    timeout_seconds: int = 20,
    owner_id: str | None = None,
) -> dict:
    """Exchange a pasted authorization code for Google OAuth tokens."""
    raw = (pasted or '').strip()
    if not raw:
        raise AntigravityOAuthError('code is required')

    extracted_code = ''
    if 'code=' in raw or 'http://' in raw or 'https://' in raw:
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(raw)
        qs = parse_qs(parsed.query or parsed.fragment)
        if 'code' in qs and qs['code']:
            extracted_code = qs['code'][0].strip()
        if 'state' in qs and qs['state']:
            state = qs['state'][0].strip() or state

    if not extracted_code:
        code_part, _, pasted_state = raw.partition('#')
        state = (pasted_state or state or '').strip()
        extracted_code = code_part.strip()

    if not extracted_code:
        raise AntigravityOAuthError('authorization code is required')

    code = extracted_code

    verifier = cache.get(f'{_STATE_CACHE_PREFIX}{state}') if state else None

    body = {
        'grant_type': 'authorization_code',
        'code': code,
        'client_id': GOOGLE_ANTIGRAVITY_CLIENT_ID,
        'client_secret': GOOGLE_ANTIGRAVITY_CLIENT_SECRET,
        'redirect_uri': REDIRECT_URI,
    }
    if verifier:
        body['code_verifier'] = verifier

    try:
        resp = requests.post(
            TOKEN_URL,
            data=body,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=timeout_seconds,
        )
    except requests.RequestException as exc:
        raise AntigravityOAuthError(f'network error exchanging code: {exc}') from exc

    if resp.status_code >= 400:
        detail = (resp.text or '')[:300]
        raise AntigravityOAuthError(
            f'Google token exchange failed: HTTP {resp.status_code} {detail}'.strip()
        )

    tokens = resp.json()
    access_token = str(tokens.get('access_token') or '').strip()
    if not access_token:
        raise AntigravityOAuthError('Google token endpoint returned no access_token')

    email = _fetch_google_user_email(access_token, timeout_seconds=timeout_seconds)
    return ingest_antigravity_account(tokens, account_email=email, owner_id=owner_id)


# ──────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ──────────────────────────────────────────────────────────────────────────────

def _fetch_google_user_email(access_token: str, *, timeout_seconds: int = 10) -> str:
    """Fetch user email from Google UserInfo endpoint."""
    try:
        resp = requests.get(
            USERINFO_URL,
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=timeout_seconds,
        )
        if resp.status_code == 200:
            info = resp.json()
            return str(info.get('email') or '').strip().lower()
    except Exception as exc:
        logger.warning('Google userinfo fetch failed: %s', exc)
    return ''


def ingest_antigravity_account(
    tokens: dict,
    *,
    account_email: str = '',
    owner_id: str | None = None,
) -> dict:
    """Encrypt and store the token bundle into the platform Google TokenPool."""
    from hive_platform.models import TokenEntry, TokenPool

    access_token = str(tokens.get('access_token') or '').strip()
    refresh_token = str(tokens.get('refresh_token') or '').strip()
    id_token = str(tokens.get('id_token') or '').strip()
    scope = str(tokens.get('scope') or SCOPES).strip()
    expires_in = int(tokens.get('expires_in') or 3600)

    if not access_token and not refresh_token:
        raise AntigravityOAuthError('at least access_token or refresh_token is required')

    email_label = (account_email or 'account').lower()
    label = f'org-google-{email_label}'

    pool = tc.fleet_credentials_pool(TokenPool.Provider.GOOGLE, owner_id=owner_id)

    expiry_date = tokens.get('expiry_date')
    if not expiry_date:
        expiry_date = int(
            (timezone.now() + timedelta(seconds=expires_in)).timestamp() * 1000
        )

    raw_secret_bundle = json.dumps({
        'access_token': access_token,
        'refresh_token': refresh_token,
        'id_token': id_token,
        'scope': scope,
        'token_type': 'Bearer',
        'expiry_date': expiry_date,
    }, sort_keys=True)

    entry = TokenEntry.objects.filter(pool=pool, label=label).first()
    created = entry is None
    if entry is None:
        entry = TokenEntry(pool=pool, label=label, priority=100)
    entry.set_token(raw_secret_bundle)
    entry.account_email = account_email
    entry.active = True
    entry.last_status = TokenEntry.LastStatus.OK
    entry.last_checked_at = timezone.now()
    entry.cooldown_until = None
    entry.quota_snapshot = {
        'provider': 'google',
        'updated_at': timezone.now().isoformat(),
        'status': 'active',
    }
    entry.save()

    return {
        'token_entry_id': str(entry.id),
        'pool_id': str(pool.id),
        'created': created,
        'email': account_email,
        'label': label,
        'has_refresh_token': bool(refresh_token),
    }
