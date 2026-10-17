"""Codex/OpenAI device-code auth — central ingestion into the HIVE Token Coordinator.

This is the server-side port of the CLI's ``shizuha auth codex`` flow
(``cli/src/auth/codex-device-auth.ts``). It lets an operator/CoS re-authenticate
fleet Codex accounts from the Hive credentials page — in a browser, from anywhere —
instead of running an interactive device-code login on the phoenix host and
seeding ``~/.shizuha/credentials.json`` (HIVE-581).

The resulting OAuth material (access + rotating refresh token) is written as an
envelope-encrypted :class:`~hive_platform.models.TokenEntry` into the platform owner's
personal OpenAI/Codex :class:`~hive_platform.models.TokenPool`. Everything downstream
already exists: the
coordinator refreshes the access token from the refresh token on lease
(``token_coordinator._selection_from_entry``), spreads load, and cools revoked
tokens — so a fresh device-auth here is the durable fix for a revoked-refresh
outage, no host shell required.

Flow (identical client_id/endpoints to Codex CLI, Hermes, OpenClaw):
  1. POST .../deviceauth/usercode  {client_id}          -> device_auth_id + user_code
  2. Human visits the device page and enters the user_code
  3. POST .../deviceauth/token     {device_auth_id, user_code} -> authorization_code (+ code_verifier)
  4. POST /oauth/token  grant_type=authorization_code  -> access_token + refresh_token + id_token
  5. Upsert a TokenEntry (label ``org-openai-<email>``) in the personal OpenAI pool
"""
from __future__ import annotations

import json
import logging

import requests
from django.db import IntegrityError
from django.utils import timezone

from . import token_coordinator as tc

logger = logging.getLogger(__name__)

# Same OAuth client + endpoints as the CLI device-auth flow.
DEVICE_CODE_URL = 'https://auth.openai.com/api/accounts/deviceauth/usercode'
DEVICE_TOKEN_URL = 'https://auth.openai.com/api/accounts/deviceauth/token'
TOKEN_EXCHANGE_URL = tc.OPENAI_OAUTH_TOKEN_URL  # https://auth.openai.com/oauth/token
DEVICE_AUTH_CALLBACK_URI = 'https://auth.openai.com/deviceauth/callback'
DEVICE_AUTH_PAGE = 'https://auth.openai.com/codex/device'
ME_URL = 'https://api.openai.com/v1/me'

DEFAULT_POLL_INTERVAL_SECONDS = 3
DEFAULT_DEVICE_CODE_EXPIRES_SECONDS = 15 * 60


class CodexDeviceAuthError(RuntimeError):
    """Device-code auth failed (usercode request, exchange, or ingest)."""


class CodexDeviceAuthTransientError(CodexDeviceAuthError):
    """A transport/provider-edge failure that can be retried with the same device session."""


class CodexDeviceAuthTargetMismatch(CodexDeviceAuthError):
    """The authorized OpenAI account does not match the selected Hive entry."""


def _safe_upstream_error(resp) -> str:
    """Return a bounded provider error code without echoing arbitrary response bodies."""
    try:
        data = resp.json()
    except (TypeError, ValueError):
        return ''
    if not isinstance(data, dict):
        return ''
    value = data.get('error') or data.get('error_code') or data.get('code')
    if isinstance(value, dict):
        value = value.get('code') or value.get('type') or value.get('error')
    text = str(value or '').strip()
    if not text or len(text) > 120:
        return ''
    if not all(char.isalnum() or char in '._-:/ ' for char in text):
        return ''
    return text


def _response_json(resp, phase: str) -> dict:
    """Parse one trusted-shape provider response and fail without exposing HTML/WAF bodies."""
    try:
        data = resp.json()
    except (TypeError, ValueError) as exc:
        raise CodexDeviceAuthTransientError(
            f'{phase} returned an invalid response (HTTP {resp.status_code})'
        ) from exc
    if not isinstance(data, dict):
        raise CodexDeviceAuthTransientError(
            f'{phase} returned an invalid response (HTTP {resp.status_code})'
        )
    return data


def request_device_code(*, timeout_seconds: int = 20) -> dict:
    """Step 1 — request a device code. Returns ``{user_code, device_auth_id, interval}``."""
    try:
        resp = requests.post(
            DEVICE_CODE_URL,
            headers={'Content-Type': 'application/json'},
            json={'client_id': tc.OPENAI_CODEX_CLIENT_ID},
            timeout=timeout_seconds,
        )
    except requests.RequestException as exc:
        raise CodexDeviceAuthTransientError(f'device code request failed: {exc}') from exc
    if resp.status_code == 429:
        raise CodexDeviceAuthError('rate limited by OpenAI — wait a few minutes and retry')
    if resp.status_code == 404:
        raise CodexDeviceAuthError(
            'OpenAI device-code login is not enabled for this account or workspace'
        )
    if resp.status_code >= 400:
        detail = _safe_upstream_error(resp)
        suffix = f' ({detail})' if detail else ''
        error_class = (
            CodexDeviceAuthTransientError if resp.status_code >= 500
            else CodexDeviceAuthError
        )
        raise error_class(
            f'device code request failed: HTTP {resp.status_code}{suffix}'
        )
    data = _response_json(resp, 'device code request')
    user_code = str(data.get('user_code') or '')
    device_auth_id = str(data.get('device_auth_id') or '')
    if not user_code or not device_auth_id:
        raise CodexDeviceAuthError('invalid device code response (missing user_code/device_auth_id)')
    return {
        'user_code': user_code,
        'device_auth_id': device_auth_id,
        'verification_url': DEVICE_AUTH_PAGE,
        'interval': int(data.get('interval') or DEFAULT_POLL_INTERVAL_SECONDS),
        'expires_in': int(data.get('expires_in') or DEFAULT_DEVICE_CODE_EXPIRES_SECONDS),
    }


def poll_device_token(device_auth_id: str, user_code: str, *, timeout_seconds: int = 20) -> dict:
    """Step 3 — poll once for authorization.

    Returns one of:
      ``{'status': 'pending'}``            — user has not yet authorized
      ``{'status': 'slow_down'}``          — back off the poll interval
      ``{'status': 'authorized', ...}``    — with ``authorization_code`` (+ ``code_verifier``)
    """
    if not device_auth_id or not user_code:
        raise CodexDeviceAuthError('device_auth_id and user_code are required')
    try:
        resp = requests.post(
            DEVICE_TOKEN_URL,
            headers={'Content-Type': 'application/json'},
            json={'device_auth_id': device_auth_id, 'user_code': user_code},
            timeout=timeout_seconds,
        )
    except requests.RequestException as exc:
        raise CodexDeviceAuthTransientError(f'device token poll failed: {exc}') from exc
    if resp.status_code == 429:
        return {'status': 'slow_down'}
    # Current Codex treats only 403/404 as "not authorized yet".
    if resp.status_code in (403, 404):
        return {'status': 'pending'}
    if resp.status_code >= 400:
        detail = _safe_upstream_error(resp)
        suffix = f' ({detail})' if detail else ''
        error_class = (
            CodexDeviceAuthTransientError if resp.status_code >= 500
            else CodexDeviceAuthError
        )
        raise error_class(
            f'device auth failed: HTTP {resp.status_code}{suffix}'
        )
    data = _response_json(resp, 'device token poll')
    error = data.get('error')
    if error == 'authorization_pending':
        return {'status': 'pending'}
    if error == 'slow_down':
        return {'status': 'slow_down'}
    if error:
        raise CodexDeviceAuthError(f'device auth error: {error}')
    auth_code = data.get('authorization_code')
    if not auth_code:
        return {'status': 'pending'}
    return {
        'status': 'authorized',
        'authorization_code': str(auth_code),
        'code_verifier': str(data.get('code_verifier') or ''),
    }


def exchange_authorization_code(authorization_code: str, code_verifier: str = '',
                                *, timeout_seconds: int = 20) -> dict:
    """Step 4 — exchange the authorization code for OAuth tokens."""
    body = {
        'grant_type': 'authorization_code',
        'code': authorization_code,
        'redirect_uri': DEVICE_AUTH_CALLBACK_URI,
        'client_id': tc.OPENAI_CODEX_CLIENT_ID,
    }
    if code_verifier:
        body['code_verifier'] = code_verifier
    try:
        resp = requests.post(
            TOKEN_EXCHANGE_URL,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            data=body,
            timeout=timeout_seconds,
        )
    except requests.RequestException as exc:
        raise CodexDeviceAuthTransientError(f'token exchange request failed: {exc}') from exc
    if resp.status_code >= 400:
        detail = _safe_upstream_error(resp)
        suffix = f' ({detail})' if detail else ''
        error_class = (
            CodexDeviceAuthTransientError if resp.status_code >= 500
            else CodexDeviceAuthError
        )
        raise error_class(
            f'token exchange failed: HTTP {resp.status_code}{suffix}'
        )
    data = _response_json(resp, 'token exchange')
    access = str(data.get('access_token') or '')
    refresh = str(data.get('refresh_token') or '')
    identity = str(data.get('id_token') or '')
    missing = [
        name for name, value in (
            ('access_token', access),
            ('refresh_token', refresh),
            ('id_token', identity),
        )
        if not value
    ]
    if missing:
        raise CodexDeviceAuthError(
            f'token exchange returned an incomplete grant (missing {", ".join(missing)})'
        )
    return {
        'access_token': access,
        'refresh_token': refresh,
        'id_token': identity,
    }


def resolve_account_email(access_token: str, *, timeout_seconds: int = 10) -> str:
    """Best-effort email from the access-token JWT, falling back to /v1/me."""
    payload = tc._jwt_payload(access_token)
    profile = payload.get('https://api.openai.com/profile')
    profile = profile if isinstance(profile, dict) else {}
    auth_claim = payload.get('https://api.openai.com/auth')
    auth_claim = auth_claim if isinstance(auth_claim, dict) else {}
    email = str(payload.get('email') or profile.get('email') or auth_claim.get('email') or '')
    if not email or '|' in email:
        try:
            resp = requests.get(
                ME_URL, headers={'Authorization': f'Bearer {access_token}'}, timeout=timeout_seconds)
            if resp.ok:
                emails = resp.json().get('emails') or []
                primary = next((e.get('email') for e in emails if e.get('verified')), None)
                primary = primary or (emails[0].get('email') if emails else None)
                if primary:
                    email = str(primary)
        except requests.RequestException:
            pass  # best effort
    if not email:
        email = str(payload.get('sub') or 'unknown')
    return email


def ingest_codex_account(
    tokens: dict,
    *,
    actor: str = '',
    target_entry=None,
    expected_email: str = '',
    expected_account_id: str = '',
) -> dict:
    """Step 5 — upsert the OAuth material as a TokenEntry in the org OpenAI pool.

    An untargeted add is idempotent per account email (label
    ``org-openai-<email>``). A row-level re-auth passes ``target_entry`` and
    rotates that exact entry only after the authenticated account identity matches
    the entry's current email/account id. This prevents a mistaken browser login
    from silently creating or replacing another account. Returns
    ``{'email', 'account_id', 'entry_id', 'created'}``.
    """
    from hive_platform.models import TokenEntry, TokenPool

    access = str(tokens.get('access_token') or '')
    refresh = str(tokens.get('refresh_token') or '')
    identity = str(tokens.get('id_token') or '')
    if not access or not refresh or not identity:
        raise CodexDeviceAuthError(
            'OpenAI device authorization did not return a complete renewable grant'
        )
    email = resolve_account_email(access) if access else str(tokens.get('email') or 'unknown')
    account_id = tc.openai_account_id(access) if access else str(tokens.get('account_id') or '')
    payload = {
        'email': email,
        'access_token': access,
        'refresh_token': refresh,
        'id_token': identity,
        'account_id': account_id,
    }

    if target_entry is not None:
        if target_entry.pool.provider != TokenPool.Provider.OPENAI:
            raise CodexDeviceAuthError('selected token entry is not an OpenAI credential')
        normalized_expected_email = str(expected_email or '').strip().casefold()
        normalized_email = email.strip().casefold()
        if normalized_expected_email and normalized_expected_email != normalized_email:
            raise CodexDeviceAuthTargetMismatch(
                f'Authorized {email}, but this re-auth was started for {expected_email}. '
                'Sign out of OpenAI in the authorization tab and retry with the selected account.'
            )
        if expected_account_id and account_id and str(expected_account_id) != account_id:
            raise CodexDeviceAuthTargetMismatch(
                'The authorized OpenAI account does not match the selected Hive token entry.'
            )
        pool = target_entry.pool
        entry = target_entry
        created = False
    else:
        # Personal-ownership model (operator 2026-07-11): new credentials land in
        # the platform owner's PERSONAL pool (labels stay stable for re-auth dedup).
        from .token_coordinator import fleet_credentials_pool
        pool = fleet_credentials_pool(TokenPool.Provider.OPENAI)
        label = f'org-openai-{email}'
        entry = (
            TokenEntry.objects.filter(pool=pool, account_email__iexact=email).first()
            or TokenEntry.objects.filter(pool=pool, label=label).first()
        )
        created = entry is None
        if entry is None:
            entry = TokenEntry(pool=pool, label=label, priority=100)
    entry.set_token(json.dumps(payload, sort_keys=True))
    # A freshly re-authed account is healthy again — clear any prior cool/dead state.
    entry.account_email = email
    entry.active = True
    entry.last_status = TokenEntry.LastStatus.OK
    entry.last_checked_at = timezone.now()
    entry.cooldown_until = None
    try:
        entry.save()
    except IntegrityError as exc:
        raise CodexDeviceAuthError(
            'This OpenAI credential is already stored in another entry in the selected pool.'
        ) from exc

    logger.info(
        '[codex-device-auth] ingested fleet codex account email=%s account_id=%s '
        'entry=%s created=%s actor=%s', email, account_id, entry.id, created, actor or '?')
    return {'email': email, 'account_id': account_id, 'entry_id': str(entry.id), 'created': created}
