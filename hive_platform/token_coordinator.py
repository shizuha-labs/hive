"""HIVE token-pool coordinator helpers.

The Coordinator stores token rows per owner/provider and never keeps raw tokens in
model fields. Each token is envelope-encrypted: a per-entry Fernet DEK encrypts
raw token bytes and the deployment KEK wraps that DEK. The KEK is supplied by a
k8s Secret via ``HIVE_TOKEN_COORDINATOR_KEK``. Missing KEK configuration
fails closed so tokens are never encrypted under a committed fallback key.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone

import requests
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction, models
from django.utils import timezone

logger = logging.getLogger(__name__)

OPENAI_OAUTH_TOKEN_URL = 'https://auth.openai.com/oauth/token'
OPENAI_CODEX_CLIENT_ID = 'app_EMoamEEZ73f0CkXaXp7hrann'
OPENAI_OAUTH_SCOPE = 'openid profile email'
XAI_OAUTH_TOKEN_URL = 'https://auth.x.ai/oauth2/token'
XAI_OAUTH_CLIENT_ID = 'b1a00492-073a-47ea-816f-4c329264a828'
XAI_OAUTH_ISSUER = 'https://auth.x.ai'
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 15 * 60
OPENAI_TERMINAL_REFRESH_STATUS_CODES = frozenset({401, 403})


@dataclass(frozen=True)
class TokenSelection:
    """A decrypted token selected for one owner/provider request."""

    entry_id: str
    pool_id: str
    owner_kind: str
    owner_id: str
    provider: str
    label: str
    token: str
    token_payload: dict[str, str]
    used_org_fallback: bool = False


class OAuthRefreshError(RuntimeError):
    """Raised when an OAuth refresh token cannot be exchanged."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str = '',
    ):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


class OpenAIOAuthRefreshError(OAuthRefreshError):
    """Raised when a Codex/OpenAI OAuth refresh token cannot be exchanged."""


class XaiOAuthRefreshError(OAuthRefreshError):
    """Raised when an xAI / SuperGrok OAuth refresh token cannot be exchanged."""


class AnthropicOAuthRefreshError(OAuthRefreshError):
    """Raised when Hive cannot refresh a canonical Claude OAuth grant."""


def set_agent_leasing_policy(entry, enabled: bool, *, actor: str, source: str) -> dict:
    """Change owner-owned leasing policy atomically and durably attribute it.

    Reserving an entry also expires every outstanding coordinator lease for that
    entry in the same transaction. This prevents a broker from renewing an
    already-issued lease after the owner removes the credential from the pool.
    """
    from hive_platform.models import TokenEntry, TokenEntryLeasingEvent, TokenLease

    enabled = bool(enabled)
    with transaction.atomic():
        locked = (
            TokenEntry.objects
            .select_for_update()
            .select_related('pool')
            .get(pk=entry.pk)
        )
        previous = locked.agent_leasing_enabled
        if previous == enabled:
            return {
                'changed': False,
                'entry': locked,
                'revoked_lease_count': 0,
            }

        locked.agent_leasing_enabled = enabled
        locked.save(update_fields=['agent_leasing_enabled', 'updated_at'])
        revoked_lease_count = 0
        if not enabled:
            now = timezone.now()
            revoked_lease_count = TokenLease.objects.filter(
                entry=locked,
                expires_at__gt=now,
            ).update(expires_at=now)

        TokenEntryLeasingEvent.objects.create(
            entry=locked,
            owner_kind=locked.pool.owner_kind,
            owner_id=locked.pool.owner_id,
            provider=locked.pool.provider,
            previous_enabled=previous,
            enabled=enabled,
            actor=str(actor or 'unknown')[:254],
            source=source,
            revoked_lease_count=revoked_lease_count,
        )
        return {
            'changed': True,
            'entry': locked,
            'revoked_lease_count': revoked_lease_count,
        }


def _fernet_key(raw: str) -> bytes:
    """Return a Fernet-compatible key from either Fernet base64 or raw secret."""
    value = (raw or '').strip()
    if not value:
        raise ImproperlyConfigured(
            'HIVE_TOKEN_COORDINATOR_KEK must be set before token pool encryption/decryption'
        )
    try:
        decoded = base64.urlsafe_b64decode(value.encode())
        if len(decoded) == 32:
            return value.encode()
    except Exception:
        pass
    return base64.urlsafe_b64encode(hashlib.sha256(value.encode()).digest())


def kek_fernet() -> Fernet:
    return Fernet(_fernet_key(getattr(settings, 'HIVE_TOKEN_COORDINATOR_KEK', '')))


def kek_id() -> str:
    return str(getattr(settings, 'HIVE_TOKEN_COORDINATOR_KEK_ID', 'k8s-secret-v1'))


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def fleet_credentials_pool(provider, owner_id: str | None = None):
    """The pool NEW fleet credentials are imported into.

    Personal-ownership model (operator 2026-07-11): credentials belong to the
    USER who added them — "the credentials are mine, the agents using them are
    mine" — so imports land in the platform owner's PERSONAL pool, not the
    synthetic org pool. Falls back to the org pool only when no fleet owner is
    configured. The coordinator's broker path draws from both scopes
    (``_broker_owner_scopes``), so either residence serves the fleet.
    """
    from django.conf import settings
    from hive_platform.models import TokenPool

    fleet_owner = str(owner_id or getattr(settings, 'HIVE_FLEET_OWNER_USER_ID', '') or '').strip()
    if fleet_owner:
        pool, _ = TokenPool.objects.get_or_create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id=fleet_owner,
            provider=provider,
            defaults={'display_name': f'Personal ({provider})'},
        )
        return pool
    pool, _ = TokenPool.objects.get_or_create(
        owner_kind=TokenPool.OwnerKind.ORG,
        owner_id=TokenPool.ORG_OWNER_ID,
        provider=provider,
        defaults={'display_name': f'Internal fleet org {provider} pool'},
    )
    return pool


def mask_token_hint(token: str) -> str:
    """A short, human-recognizable mask of a raw token — first chars + last chars,
    never the middle. Lets an operator tell WHICH credential a row is (e.g.
    ``sk-ant-o…XYZ789``) without exposing enough to use it. Safe to store and
    render; the secret-bearing middle is dropped, not encrypted."""
    if not token:
        return ''
    token = token.strip()
    if len(token) <= 10:
        return (token[:1] + '…' + token[-1:]) if len(token) > 3 else '…'
    return f'{token[:8]}…{token[-6:]}'


def encrypt_token(token: str) -> dict[str, str]:
    dek = Fernet.generate_key()
    ciphertext = Fernet(dek).encrypt(token.encode()).decode()
    wrapped_dek = kek_fernet().encrypt(dek).decode()
    return {
        'ciphertext': ciphertext,
        'wrapped_dek': wrapped_dek,
        'dek_key_id': kek_id(),
        'fingerprint': token_fingerprint(token),
    }


def decrypt_token(ciphertext: str, wrapped_dek: str) -> str:
    try:
        dek = kek_fernet().decrypt(wrapped_dek.encode())
        return Fernet(dek).decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise ValueError('token cannot be decrypted with the configured KEK') from exc


def _json_or_refresh_token(raw_token: str) -> dict[str, str]:
    """Accept legacy raw refresh-token rows and structured OAuth JSON rows."""
    value = (raw_token or '').strip()
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {'refresh_token': value}
    if not isinstance(decoded, dict):
        return {'refresh_token': value}
    return {
        'email': str(decoded.get('email') or ''),
        'access_token': str(decoded.get('access_token') or decoded.get('accessToken') or ''),
        'refresh_token': str(decoded.get('refresh_token') or decoded.get('refreshToken') or ''),
        'id_token': str(decoded.get('id_token') or decoded.get('idToken') or ''),
        'account_id': str(decoded.get('account_id') or decoded.get('accountId') or ''),
    }


def probe_token_capabilities(scopes, required_capabilities, *, now=None) -> dict:
    """Probe owner-scoped credential kinds without returning token material.

    ``codex.oauth`` requires a refreshable OAuth bundle.  A static OpenAI API
    token can satisfy ``provider.api:openai`` but deliberately cannot satisfy
    the Codex harness requirement.
    """
    from hive_platform.models import TokenEntry

    now = now or timezone.now()
    required = sorted({str(item) for item in required_capabilities or [] if item})
    available = []
    for capability in required:
        if capability == 'codex.oauth':
            provider = 'openai'
        elif capability.startswith('provider.api:'):
            provider = capability.split(':', 1)[1]
        else:
            continue
        entries = TokenEntry.objects.active_for_scopes(
            scopes, provider, now=now,
        ).select_related('pool')
        for entry in entries:
            if not quota_routing_eligible(entry, now=now):
                continue
            if capability.startswith('provider.api:'):
                available.append(capability)
                break
            try:
                payload = _json_or_refresh_token(entry.get_token())
            except Exception:
                continue
            if payload.get('access_token') and payload.get('refresh_token'):
                available.append(capability)
                break
    available = sorted(set(available))
    return {
        'required': required,
        'available': available,
        'missing': sorted(set(required).difference(available)),
    }


def _json_xai_oauth_token(raw_token: str) -> dict[str, str]:
    """Parse structured xAI OAuth JSON (or legacy bare access/refresh material)."""
    value = (raw_token or '').strip()
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        # Bare refresh/access JWT or API key — treat as static access token.
        if value.startswith('xai-') or value.count('.') >= 2:
            return {
                'type': 'static',
                'access_token': value,
                'refresh_token': '',
                'email': '',
                'expires_at': '',
                'oidc_client_id': XAI_OAUTH_CLIENT_ID,
                'oidc_issuer': XAI_OAUTH_ISSUER,
                'token_endpoint': XAI_OAUTH_TOKEN_URL,
            }
        return {
            'type': 'xai_oauth',
            'access_token': '',
            'refresh_token': value,
            'email': '',
            'expires_at': '',
            'oidc_client_id': XAI_OAUTH_CLIENT_ID,
            'oidc_issuer': XAI_OAUTH_ISSUER,
            'token_endpoint': XAI_OAUTH_TOKEN_URL,
        }
    if not isinstance(decoded, dict):
        return {}
    # HIVE-612 (CWE-918): the xAI OAuth token endpoint, client id, and issuer
    # are FIXED constants. User-supplied values from the token payload are
    # ignored so an authenticated caller cannot steer server-side refresh
    # requests to loopback/link-local/RFC1918/attacker-controlled destinations.
    return {
        'type': str(decoded.get('type') or ('xai_oauth' if decoded.get('refresh_token') else 'static')),
        'email': str(decoded.get('email') or ''),
        'access_token': str(
            decoded.get('access_token')
            or decoded.get('accessToken')
            or decoded.get('token')
            or decoded.get('key')
            or ''
        ),
        'refresh_token': str(decoded.get('refresh_token') or decoded.get('refreshToken') or ''),
        'id_token': str(decoded.get('id_token') or decoded.get('idToken') or ''),
        'account_id': str(decoded.get('account_id') or decoded.get('accountId') or ''),
        'expires_at': str(decoded.get('expires_at') or ''),
        'expires_in': decoded.get('expires_in'),
        'oidc_client_id': XAI_OAUTH_CLIENT_ID,
        'oidc_issuer': XAI_OAUTH_ISSUER,
        'token_endpoint': XAI_OAUTH_TOKEN_URL,
    }


def _jwt_payload(token: str) -> dict:
    try:
        parts = token.split('.')
        if len(parts) < 2:
            return {}
        raw = parts[1].replace('-', '+').replace('_', '/')
        raw += '=' * ((4 - (len(raw) % 4)) % 4)
        return json.loads(base64.b64decode(raw).decode())
    except Exception:
        return {}


def jwt_expiry_seconds(token: str) -> int | None:
    """Extract the ``exp`` claim from a JWT and return seconds-until-expiry (or None)."""
    payload = _jwt_payload(token)
    exp = payload.get('exp')
    if not isinstance(exp, (int, float)):
        return None
    return int(exp - time.time())


def openai_account_id(access_token: str) -> str:
    payload = _jwt_payload(access_token)
    auth_claim = payload.get('https://api.openai.com/auth') or {}
    if not isinstance(auth_claim, dict):
        auth_claim = {}
    return str(
        payload.get('account_id')
        or auth_claim.get('chatgpt_account_id')
        or auth_claim.get('account_id')
        or ''
    )


def openai_plan_type(access_token: str) -> str:
    payload = _jwt_payload(access_token)
    auth_claim = payload.get('https://api.openai.com/auth') or {}
    if not isinstance(auth_claim, dict):
        return ''
    return str(auth_claim.get('chatgpt_plan_type') or auth_claim.get('plan_type') or '')


def _oauth_response_error_code(response) -> str:
    """Extract only a bounded OAuth error identifier, never arbitrary body text."""
    try:
        data = response.json()
    except (TypeError, ValueError):
        return ''
    if not isinstance(data, dict):
        return ''
    value = data.get('error') or data.get('error_code')
    if isinstance(value, dict):
        value = value.get('code') or value.get('type') or value.get('error')
    code = str(value or '').strip().lower()
    if not code or len(code) > 64:
        return ''
    if not all(char.isalnum() or char in '._-' for char in code):
        return ''
    return code


def refresh_openai_oauth(refresh_token: str, *, timeout_seconds: int = 20) -> dict[str, str]:
    """Exchange a Codex/OpenAI refresh token for fresh access/id tokens."""
    if not refresh_token:
        raise OpenAIOAuthRefreshError('missing refresh_token')
    token_url = getattr(settings, 'HIVE_OPENAI_OAUTH_TOKEN_URL', OPENAI_OAUTH_TOKEN_URL)
    client_id = getattr(settings, 'HIVE_OPENAI_OAUTH_CLIENT_ID', OPENAI_CODEX_CLIENT_ID)
    try:
        response = requests.post(
            token_url,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            data={
                'grant_type': 'refresh_token',
                'refresh_token': refresh_token,
                'client_id': client_id,
                'scope': OPENAI_OAUTH_SCOPE,
            },
            timeout=timeout_seconds,
        )
    except requests.RequestException as exc:
        raise OpenAIOAuthRefreshError(f'openai oauth refresh request failed: {exc}') from exc
    if response.status_code >= 400:
        raise OpenAIOAuthRefreshError(
            f'openai oauth refresh failed: HTTP {response.status_code}',
            status_code=response.status_code,
            error_code=_oauth_response_error_code(response),
        )
    data = response.json()
    access = str(data.get('access_token') or '')
    if not access:
        raise OpenAIOAuthRefreshError('openai oauth refresh response missing access_token')
    refreshed = {
        'access_token': access,
        'refresh_token': str(data.get('refresh_token') or refresh_token),
        'id_token': str(data.get('id_token') or ''),
    }
    account_id = openai_account_id(access)
    if account_id:
        refreshed['account_id'] = account_id
    return refreshed


def _openai_needs_refresh(token_payload: dict[str, str], *, buffer_seconds: int = 600) -> bool:
    """Flap-free + non-hammering: refresh the Codex/OpenAI access token only when it is
    missing or within ``buffer_seconds`` of expiry — NOT on every serve. The OAuth
    refresh itself consumes no model quota, but refreshing on every model-token request
    (every codex agent, frequently) would needlessly hammer the OAuth endpoint. A short
    buffer (10 min) guarantees we always serve a token with comfortable validity left,
    so codex never flaps between fresh and expired."""
    import time
    access = token_payload.get('access_token') or ''
    if not access:
        return True  # no usable access token yet → refresh
    payload = _jwt_payload(access)
    exp = payload.get('exp')
    if not isinstance(exp, (int, float)):
        return True  # opaque/unknown expiry → refresh to be safe
    return (exp - time.time()) < buffer_seconds


def _google_needs_refresh(payload: dict, *, buffer_seconds: int = 300) -> bool:
    """True if Google access token is missing or near expiry."""
    access = payload.get('access_token')
    if not access:
        return True
    expiry_date = payload.get('expiry_date')
    if expiry_date:
        try:
            now_ms = timezone.now().timestamp() * 1000
            if now_ms + buffer_seconds * 1000 >= float(expiry_date):
                return True
        except (TypeError, ValueError):
            return True
    return False


def refresh_xai_oauth(token_payload: dict, *, timeout_seconds: int = 20) -> dict[str, str]:
    """Exchange an xAI SuperGrok refresh token for a fresh access token."""
    refresh_token = str(token_payload.get('refresh_token') or '')
    if not refresh_token:
        raise XaiOAuthRefreshError('missing refresh_token')
    # HIVE-612 (CWE-918): the refresh destination and client id are derived from
    # settings/constants ONLY — never from the token payload, which is
    # caller-controlled. Redirects are disabled so a fixed HTTPS endpoint
    # cannot be coerced into following an attacker-chosen Location.
    token_url = str(getattr(settings, 'HIVE_XAI_OAUTH_TOKEN_URL', XAI_OAUTH_TOKEN_URL))
    client_id = str(getattr(settings, 'HIVE_XAI_OAUTH_CLIENT_ID', XAI_OAUTH_CLIENT_ID))
    try:
        response = requests.post(
            token_url,
            headers={
                'Content-Type': 'application/x-www-form-urlencoded',
                'Accept': 'application/json',
            },
            data={
                'grant_type': 'refresh_token',
                'refresh_token': refresh_token,
                'client_id': client_id,
            },
            timeout=timeout_seconds,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise XaiOAuthRefreshError(f'xai oauth refresh request failed: {exc}') from exc
    if response.status_code >= 400:
        error_code = ''
        try:
            body = response.json()
            if isinstance(body, dict):
                error_code = str(body.get('error') or '')
        except Exception:  # noqa: BLE001 — best-effort error classification
            error_code = ''
        raise XaiOAuthRefreshError(
            f'xai oauth refresh failed: HTTP {response.status_code}'
            + (f' ({error_code})' if error_code else ''),
            status_code=response.status_code,
            error_code=error_code,
        )
    data = response.json()
    access = str(data.get('access_token') or '')
    if not access:
        raise XaiOAuthRefreshError('xai oauth refresh response missing access_token')
    refreshed = {
        'access_token': access,
        'refresh_token': str(data.get('refresh_token') or refresh_token),
        'id_token': str(data.get('id_token') or token_payload.get('id_token') or ''),
        'type': 'xai_oauth',
        'oidc_client_id': client_id,
        'oidc_issuer': str(token_payload.get('oidc_issuer') or XAI_OAUTH_ISSUER),
        'token_endpoint': token_url,
        'email': str(token_payload.get('email') or ''),
        'account_id': str(token_payload.get('account_id') or ''),
    }
    expires_in = data.get('expires_in')
    if expires_in is not None:
        try:
            secs = int(expires_in)
            refreshed['expires_in'] = secs
            from datetime import datetime, timezone as dt_timezone
            refreshed['expires_at'] = (
                datetime.now(dt_timezone.utc) + timedelta(seconds=secs)
            ).isoformat()
        except (TypeError, ValueError):
            pass
    elif token_payload.get('expires_at'):
        refreshed['expires_at'] = str(token_payload['expires_at'])
    return refreshed


def _xai_needs_refresh(token_payload: dict, *, buffer_seconds: int = 300) -> bool:
    """Refresh SuperGrok access tokens near expiry (or when access is missing)."""
    import time
    if not token_payload.get('refresh_token'):
        return False
    access = token_payload.get('access_token') or ''
    if not access:
        return True
    # Prefer JWT exp when present.
    payload = _jwt_payload(access)
    exp = payload.get('exp')
    if isinstance(exp, (int, float)):
        return (exp - time.time()) < buffer_seconds
    expires_at = str(token_payload.get('expires_at') or '').strip()
    if expires_at:
        try:
            raw = expires_at
            if raw.endswith('Z'):
                raw = raw[:-1] + '+00:00'
            from datetime import datetime
            dt = datetime.fromisoformat(raw)
            return (dt.timestamp() - time.time()) < buffer_seconds
        except ValueError:
            return True
    # Unknown expiry with refresh available — refresh proactively on lease.
    return True


def _persist_oauth_payload(entry, payload: dict):
    now = timezone.now()
    entry.set_token(json.dumps(payload, sort_keys=True))
    entry.last_status = entry.__class__.LastStatus.OK
    entry.last_checked_at = now
    entry.save(update_fields=[
        'token_ciphertext', 'wrapped_dek', 'dek_key_id', 'token_fingerprint',
        'last_status', 'last_checked_at', 'updated_at',
    ])


def _persist_openai_payload(entry, payload: dict[str, str]):
    _persist_oauth_payload(entry, payload)


def _refresh_openai_entry_payload(
    entry,
    *,
    force: bool = False,
    timeout_seconds: int = 20,
) -> tuple[object, dict[str, str], bool]:
    """Serialize one rotating OpenAI refresh-token generation per TokenEntry.

    Every refresh path must re-read the encrypted row while holding a database
    row lock. OpenAI refresh tokens rotate on use; refreshing a stale in-memory
    copy from a broker request, quota poll, background worker, or Check-now can
    otherwise consume the same generation twice and permanently strand the row.
    """
    from hive_platform.models import TokenEntry

    with transaction.atomic():
        locked = (
            TokenEntry.objects
            .select_for_update()
            .select_related('pool')
            .get(pk=entry.pk)
        )
        payload = _json_or_refresh_token(locked.get_token())
        refresh_token = str(payload.get('refresh_token') or '')
        if not refresh_token:
            if force:
                raise OpenAIOAuthRefreshError('missing refresh_token')
            return locked, payload, False
        if not force and not _openai_needs_refresh(payload):
            return locked, payload, False

        refreshed = refresh_openai_oauth(
            refresh_token,
            timeout_seconds=timeout_seconds,
        )
        payload.update({key: value for key, value in refreshed.items() if value})
        _persist_openai_payload(locked, payload)
        return locked, payload, True


def _refresh_xai_entry_payload(
    entry,
    *,
    force: bool = False,
    timeout_seconds: int = 20,
    refresh_buffer_seconds: int = 5 * 60,
) -> tuple[object, dict, bool]:
    """Serialize one rotating xAI refresh-token generation per TokenEntry.

    xAI rotates public-client refresh tokens. Broker leases, quota polling, and
    the operator's Check-now action can all reach the same account from separate
    Hive workers, so every path must lock and re-read the encrypted row before
    exchanging a generation. For forced retries, a changed fingerprint proves
    another caller already installed a newer generation; return it instead of
    replaying the stale token that triggered the retry.
    """
    from hive_platform.models import TokenEntry

    caller_fingerprint = str(getattr(entry, 'token_fingerprint', '') or '')
    with transaction.atomic():
        locked = (
            TokenEntry.objects
            .select_for_update()
            .select_related('pool')
            .get(pk=entry.pk)
        )
        payload = _json_xai_oauth_token(locked.get_token())
        refresh_token = str(payload.get('refresh_token') or '')
        if not refresh_token:
            if force:
                raise XaiOAuthRefreshError('missing refresh_token')
            return locked, payload, False

        if (
            force
            and caller_fingerprint
            and locked.token_fingerprint != caller_fingerprint
        ):
            return locked, payload, False
        if not force and not _xai_needs_refresh(
            payload,
            buffer_seconds=refresh_buffer_seconds,
        ):
            return locked, payload, False

        try:
            refreshed = refresh_xai_oauth(
                payload,
                timeout_seconds=timeout_seconds,
            )
        except XaiOAuthRefreshError as exc:
            # Bind the provider failure to the exact encrypted generation sent
            # upstream.  The transaction releases its row lock when this is
            # raised; a device re-auth may install a newer generation before a
            # caller records the failure, and that new grant must stay healthy.
            exc.credential_fingerprint = locked.token_fingerprint
            raise
        payload.update({
            key: value
            for key, value in refreshed.items()
            if value is not None and value != ''
        })
        if refreshed.get('expires_in') is not None:
            payload['expires_in'] = refreshed['expires_in']
        _persist_oauth_payload(locked, payload)
        return locked, payload, True


def _openai_broker_payload(token_payload: dict[str, str]) -> dict[str, str]:
    """Return the access-only Codex external-auth lease safe for agent runtimes."""
    access = str(token_payload.get('access_token') or '')
    account_id = str(token_payload.get('account_id') or '') or openai_account_id(access)
    if not access:
        raise OpenAIOAuthRefreshError('openai credential missing access_token')
    if not account_id:
        raise OpenAIOAuthRefreshError('openai credential missing account_id')
    served = {
        'auth_mode': 'chatgptAuthTokens',
        'access_token': access,
        'account_id': account_id,
        'email': str(token_payload.get('email') or ''),
    }
    plan_type = str(token_payload.get('plan_type') or '') or openai_plan_type(access)
    if plan_type:
        served['plan_type'] = plan_type
    return served


def _xai_broker_payload(token_payload: dict[str, str]) -> dict[str, str]:
    """Return the access-only xAI lease safe to cross into Cortex.

    Hive is the sole custodian and refresh writer for TokenPool xAI grants.
    Cortex only needs the current access token to proxy one upstream request;
    sending the rotating refresh token (or ID token) would create a second
    credential copy and make another stale-generation replay possible.
    """
    access = str(token_payload.get('access_token') or '')
    if not access:
        raise XaiOAuthRefreshError('xai credential missing access_token')
    served = {
        'auth_mode': 'xaiAccessToken',
        'access_token': access,
        'email': str(token_payload.get('email') or ''),
    }
    account_id = str(token_payload.get('account_id') or '')
    if account_id:
        served['account_id'] = account_id
    return served


def _selection_from_entry(
    entry,
    *,
    used_org_fallback: bool = False,
    refresh_openai: bool = True,
    force_openai_refresh: bool = False,
    openai_refresh_timeout_seconds: int = 20,
) -> TokenSelection:
    from hive_platform.models import TokenPool

    raw_token = entry.get_token()
    token_payload: dict[str, str] = {}
    token = raw_token
    if entry.pool.provider == TokenPool.Provider.OPENAI:
        token_payload = _json_or_refresh_token(raw_token)
        if refresh_openai and token_payload.get('refresh_token') and (
            force_openai_refresh or _openai_needs_refresh(token_payload)
        ):
            _, token_payload, _ = _refresh_openai_entry_payload(
                entry,
                force=force_openai_refresh,
                timeout_seconds=openai_refresh_timeout_seconds,
            )
        # Codex's external ``chatgptAuthTokens`` mode is deliberately access-only:
        # Hive owns and persists the rotating refresh generation, while agents ask
        # the broker for a fresh access lease on 401. Never copy refresh_token or
        # id_token into a workload response.
        token = json.dumps(_openai_broker_payload(token_payload), sort_keys=True)
    elif entry.pool.provider == TokenPool.Provider.ANTHROPIC:
        # Full-login Claude credentials are stored as refreshable JSON bundles,
        # but Claude Code's CLAUDE_CODE_OAUTH_TOKEN contract requires the bare
        # sk-ant-oat access token. Serving the JSON blob verbatim makes Claude
        # reject a healthy account with "401 Invalid bearer token" even while
        # the same bundle's usage endpoint reports valid quota.
        token_payload = _anthropic_oauth_payload(raw_token) or {}
        if token_payload:
            access_token = _anthropic_usage_token(raw_token)
            if refresh_openai and (not access_token or _claude_needs_refresh(token_payload)):
                refreshed_raw = _refresh_and_persist_claude_bundle(entry, token_payload)
                if refreshed_raw:
                    raw_token = refreshed_raw
                    token_payload = _anthropic_oauth_payload(raw_token) or token_payload
                    access_token = _anthropic_usage_token(raw_token)
            if not access_token:
                raise AnthropicOAuthRefreshError('anthropic oauth bundle missing access token')
            token = access_token
    elif entry.pool.provider == TokenPool.Provider.XAI:
        token_payload = _json_xai_oauth_token(raw_token)
        if refresh_openai and token_payload.get('refresh_token') and _xai_needs_refresh(token_payload):
            entry, token_payload, _ = _refresh_xai_entry_payload(entry)
        # Cortex is a stateless access-token consumer, never a second OAuth
        # custodian. Keep the rotating refresh generation encrypted in Hive.
        token = json.dumps(_xai_broker_payload(token_payload), sort_keys=True)
    return TokenSelection(
        entry_id=str(entry.id),
        pool_id=str(entry.pool_id),
        owner_kind=entry.pool.owner_kind,
        owner_id=entry.pool.owner_id,
        provider=entry.pool.provider,
        label=entry.label,
        token=token,
        token_payload=token_payload,
        used_org_fallback=used_org_fallback,
    )


def _select_from_ready_entries(entries, *, used_org_fallback=False, refresh_openai=True):
    for entry in entries.select_related('pool'):
        if not quota_routing_eligible(entry):
            continue
        try:
            return _selection_from_entry(
                entry,
                used_org_fallback=used_org_fallback,
                refresh_openai=refresh_openai,
            )
        except OAuthRefreshError as exc:
            if isinstance(exc, OpenAIOAuthRefreshError):
                apply_openai_refresh_failure(entry, exc)
            elif isinstance(exc, XaiOAuthRefreshError):
                apply_xai_refresh_failure(entry, exc)
            elif isinstance(exc, AnthropicOAuthRefreshError):
                apply_anthropic_refresh_failure(entry, exc)
            elif exc.status_code == 401:
                apply_status_report(entry, '401')
            else:
                report_token_status(
                    entry.id,
                    error_code='oauth_refresh_failed',
                    cooldown_seconds=5 * 60,
                )
    return None


def select_token_for_owner(owner_id, provider, *, owner_kind='user', allow_org_fallback=True,
                           refresh_openai=True, probe=None):
    """Select a ready token for an owner/provider, with optional org fallback.

    This is the common coordinator entrypoint for both Claude/Anthropic and
    Codex/OpenAI harnesses. User pools are always checked first. If none are
    usable and ``allow_org_fallback`` is true, the internal org pool may be used
    only when that org pool explicitly enables fallback. ``probe`` is forwarded to
    the PLAT-881 pre-dry recovery sweep (injectable for tests).
    """
    from hive_platform.models import TokenEntry, TokenPool

    provider = str(provider)
    owner_kind = str(owner_kind)
    owner_id = str(owner_id)
    probe = probe or probe_token_health  # resolved here: defined later in the module
    selection = _select_from_ready_entries(
        TokenEntry.objects.active_for_owner(owner_kind, owner_id, provider),
        refresh_openai=refresh_openai,
    )
    if selection:
        return selection

    # PLAT-881: before declaring the user pool dry, try to revive an auth-killed
    # token whose recovery backoff has lapsed (the operator's "attempt it before
    # declaring the pool dry"), then retry the selection once. Best-effort.
    if recover_expired_dead_tokens(
        provider=provider, probe=probe,
        owner_scope=TokenEntry.objects.for_owner(owner_kind, owner_id, provider),
    )['recovered']:
        selection = _select_from_ready_entries(
            TokenEntry.objects.active_for_owner(owner_kind, owner_id, provider),
            refresh_openai=refresh_openai,
        )
        if selection:
            return selection

    # Companion pre-dry recovery: clear lingering (stale) rate-limit cooldowns that
    # would otherwise dry the pool (all tokens active but cooling at once).
    if recover_stale_cooldown_tokens(
        provider=provider,
        owner_scope=TokenEntry.objects.for_owner(owner_kind, owner_id, provider),
    )['cleared']:
        selection = _select_from_ready_entries(
            TokenEntry.objects.active_for_owner(owner_kind, owner_id, provider),
            refresh_openai=refresh_openai,
        )
        if selection:
            return selection

    if not allow_org_fallback or owner_kind != TokenPool.OwnerKind.USER:
        return None

    org_pool = TokenPool.objects.filter(
        owner_kind=TokenPool.OwnerKind.ORG,
        owner_id=TokenPool.ORG_OWNER_ID,
        provider=provider,
        org_fallback_allowed=True,
    ).first()
    if org_pool is None:
        return None

    def _ready_org_entries():
        return org_pool.entries.filter(
            active=True,
            agent_leasing_enabled=True,
        ).filter(
            models.Q(cooldown_until__isnull=True) | models.Q(cooldown_until__lte=timezone.now())
        ).order_by('priority', 'last_rate_limit_at', 'created_at')

    selection = _select_from_ready_entries(
        _ready_org_entries(), used_org_fallback=True, refresh_openai=refresh_openai,
    )
    if selection:
        return selection
    # PLAT-881: same pre-dry recovery for the org fallback pool.
    if recover_expired_dead_tokens(provider=provider, probe=probe, owner_scope=org_pool.entries.all())['recovered']:
        return _select_from_ready_entries(
            _ready_org_entries(), used_org_fallback=True, refresh_openai=refresh_openai,
        )
    return None


def clear_elapsed_rate_limit_cooldowns(provider=None, now=None, owner_scope=None) -> dict:
    """Clear cool stamps whose window has already elapsed.

    ``recover_stale_cooldown_tokens`` only wipes *future* cooldowns proven stale
    by age. A different leftover state is common on the Subscriptions board:
    ``cooldown_until`` is in the past but ``last_status`` is still
    ``rate_limited``, so the UI never flips back to active until Check now.
    Normalize those rows: drop the past cool, mark OK. A still-throttled
    provider re-cools on the next real 429. Returns ``{candidates, cleared}``.
    """
    from hive_platform.models import TokenEntry

    now = now or timezone.now()
    qs = owner_scope if owner_scope is not None else TokenEntry.objects.all()
    qs = qs.filter(
        active=True,
        last_status=TokenEntry.LastStatus.RATE_LIMITED,
    ).filter(
        models.Q(cooldown_until__isnull=True) | models.Q(cooldown_until__lte=now),
    )
    if provider:
        qs = qs.filter(pool__provider=provider)

    summary = {'candidates': 0, 'cleared': 0}
    for entry in qs:
        summary['candidates'] += 1
        exhausted_until = confirmed_xai_quota_exhausted_until(entry, now=now)
        if exhausted_until is not None:
            _preserve_quota_cooldown(entry, exhausted_until, now=now)
            continue
        entry.cooldown_until = None
        entry.last_status = TokenEntry.LastStatus.OK
        entry.last_checked_at = now
        entry.save(update_fields=['cooldown_until', 'last_status', 'last_checked_at', 'updated_at'])
        summary['cleared'] += 1
    return summary


def recover_stale_cooldown_tokens(provider=None, now=None, owner_scope=None, max_age_seconds=None):
    """Pre-dry recovery for a pool dried by lingering rate-limit cooldowns.

    Companion to ``recover_expired_dead_tokens`` (which only revives ``active=False``
    auth-killed tokens). A different failure mode dries the pool: ALL tokens are
    ``active=True`` but sitting in a *future* rate-limit ``cooldown_until`` at once
    (e.g. both org Codex accounts throttled in the same window). ``active_for_owner``
    then excludes every entry, the broker 404s, and every Codex agent fail-closes at
    spawn — a fleet crash-loop that only cleared when an operator hand-wiped the
    cooldowns (2026-06-26 half-fleet; 2026-06-27 ×2). The cooldowns were stale: the
    real throttle had long since lapsed but the (sometimes hour-long) cooldown stamp
    lingered.

    Before declaring the pool dry, clear the cooldown on rate-limited entries whose
    cooldown was stamped longer ago than a normal throttle window (``max_age_seconds``,
    default 15 min) — i.e. lingering, not a fresh 429. Bounded exactly like the
    PLAT-881 dead-token revival: a token that is genuinely still throttled is
    re-cooled by the next real rate-limit report, so the retry rate is capped by the
    throttle window, never a tight loop. A *fresh* cooldown (< max_age old) is left
    untouched so a live rate-limit is still respected. ``owner_scope`` limits the
    sweep to one owner's pool. Returns ``{candidates, cleared}``. Never raises.
    """
    from hive_platform.models import TokenEntry

    now = now or timezone.now()
    max_age = max_age_seconds if max_age_seconds is not None else DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS
    cutoff = now - timedelta(seconds=max_age)
    qs = owner_scope if owner_scope is not None else TokenEntry.objects.all()
    qs = qs.filter(active=True, cooldown_until__isnull=False, cooldown_until__gt=now)
    if provider:
        qs = qs.filter(pool__provider=provider)

    summary = {'candidates': 0, 'cleared': 0}
    for entry in qs:
        summary['candidates'] += 1
        exhausted_until = confirmed_xai_quota_exhausted_until(entry, now=now)
        if exhausted_until is not None:
            _preserve_quota_cooldown(entry, exhausted_until, now=now)
            continue
        # HIVE-1747: a provider-confirmed quota-exhaustion report (Cortex
        # classification.reason='quota_exhausted') holds the entry non-routable for
        # its requested window even when the usage API is unreadable
        # (confidence='unsupported'). The age heuristic must never re-lease a
        # provably out-of-credit token — only an explicit reason change or an ``ok``
        # report clears this hold.
        if entry.cooldown_reason == 'quota_exhausted':
            continue
        # Only clear a cooldown we can PROVE is stale. Skip when there is no throttle
        # timestamp (unknown age — never wipe a live-looking cooldown) or it was
        # stamped within the last max_age window (a FRESH throttle may still be real).
        # PLAT-924: the no-timestamp guard matters now that run_health_check runs this
        # proactively — a cooldown of unknown provenance must be left to expire on its
        # own, not assumed stale and wiped.
        if entry.last_rate_limit_at is None or entry.last_rate_limit_at > cutoff:
            continue
        # HIVE-1747 part 1: respect the REQUESTED cooldown window. A cooldown stamped
        # for longer than the normal throttle window (max_age) is a deliberate long
        # cool (e.g. a 6h quota cooldown), not a lingering stale throttle — the age
        # heuristic cannot prove it stale. Leave it to expire on its own UNLESS the
        # quota snapshot confirms the exhaustion has RESOLVED (reset boundary passed),
        # in which case the stale extended cooldown may still be cleared.
        stamped_duration = entry.cooldown_until - entry.last_rate_limit_at
        if (
            stamped_duration > timedelta(seconds=max_age)
            and not _snapshot_confirms_quota_resolved(entry, now=now)
        ):
            continue
        entry.cooldown_until = None
        entry.last_status = TokenEntry.LastStatus.OK
        entry.last_checked_at = now
        entry.cooldown_reason = ''
        entry.save(update_fields=['cooldown_until', 'last_status', 'last_checked_at', 'cooldown_reason', 'updated_at'])
        summary['cleared'] += 1
    return summary


def report_token_status(entry_id, *, status_code=None, error_code='', cooldown_seconds=None, success=None):
    """Record coordinator feedback so future selections fail over when needed."""
    from hive_platform.models import TokenEntry

    try:
        entry = TokenEntry.objects.get(id=entry_id, deleted_at__isnull=True)
    except (TokenEntry.DoesNotExist, ValueError):
        return False

    now = timezone.now()
    should_cooldown = status_code == 429 or str(error_code).lower() in {'rate_limited', 'rate_limit', 'quota_exceeded', 'oauth_refresh_failed'}
    if should_cooldown:
        apply_status_report(
            entry,
            'rate_limited',
            retry_after=cooldown_seconds or DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
            now=now,
        )
    elif success is True:
        # A late success report proves authentication/request delivery only.  It
        # must not erase a newer, provider-confirmed xAI zero-credit snapshot.
        apply_status_report(entry, 'ok', now=now)
    else:
        entry.save(update_fields=['updated_at'])
    return True


def apply_openai_refresh_failure(
    entry,
    exc: OpenAIOAuthRefreshError,
    *,
    now=None,
    mutate_lease_state: bool = True,
) -> str:
    """Fail closed on a terminal OpenAI refresh response.

    HTTP 401/403 or OAuth ``invalid_grant`` means the canonical rotating grant
    cannot be renewed. Retrying the same retained generation must never make the
    row broker-eligible again. Other 400 responses (for example a deployment's
    invalid client configuration), transport failures, 429, and provider 5xx
    remain transient cooldowns so valid grants are not destroyed by a service
    regression.

    Returns ``auth_failed`` or ``rate_limited``. When
    ``mutate_lease_state=False`` classification and metrics are retained, while
    an operator-disabled row remains untouched.
    """
    status_code = getattr(exc, 'status_code', None)
    terminal = (
        status_code in OPENAI_TERMINAL_REFRESH_STATUS_CODES
        or (
            status_code == 400
            and getattr(exc, 'error_code', '') == 'invalid_grant'
        )
    )
    now = now or timezone.now()

    if terminal:
        if mutate_lease_state:
            apply_status_report(entry, 'auth_failed', now=now)
        if status_code == 401:
            try:
                from hive_platform import codex_refresh_metrics
                codex_refresh_metrics.record_refresh_401(
                    entry,
                    status_code=status_code,
                )
            except Exception:
                pass
        return 'auth_failed'

    if mutate_lease_state:
        apply_status_report(
            entry,
            'rate_limited',
            retry_after=CODEX_REFRESH_COOLDOWN_ON_FAILURE,
            now=now,
        )
    return 'rate_limited'


def apply_xai_refresh_failure(
    entry,
    exc: XaiOAuthRefreshError,
    *,
    now=None,
    mutate_lease_state: bool = True,
) -> str:
    """Classify an xAI refresh failure without killing a newer re-auth grant.

    Refresh-token exchange is serialized while the provider call is in flight,
    but status recording necessarily happens after that transaction unwinds.
    ``credential_fingerprint`` identifies the generation that actually failed;
    a mismatch means device auth has already replaced it, so the stale result is
    discarded instead of marking the new credential dead or cooling it.
    """
    return _apply_rotating_refresh_failure(
        entry, exc, now=now, mutate_lease_state=mutate_lease_state,
    )


def apply_anthropic_refresh_failure(entry, exc, *, now=None, mutate_lease_state=True):
    return _apply_rotating_refresh_failure(
        entry, exc, now=now, mutate_lease_state=mutate_lease_state,
    )


def _apply_rotating_refresh_failure(entry, exc, *, now=None, mutate_lease_state=True):
    status_code = getattr(exc, 'status_code', None)
    error_code = getattr(exc, 'error_code', '')
    terminal = (
        status_code in (401, 403)
        or (status_code == 400 and error_code == 'invalid_grant')
    )
    outcome = 'auth_failed' if terminal else 'rate_limited'
    if not mutate_lease_state:
        return outcome

    applied = apply_status_report(
        entry,
        outcome,
        retry_after=(None if terminal else CODEX_REFRESH_COOLDOWN_ON_FAILURE),
        now=now,
        expected_fingerprint=str(
            getattr(exc, 'credential_fingerprint', '')
            or getattr(entry, 'token_fingerprint', '')
            or ''
        ),
    )
    return applied


@transaction.atomic
def tombstone_token_entry(entry, *, actor: str = '') -> dict:
    """Disable and cryptographically scrub a token while retaining lineage.

    Existing access leases may remain usable at the provider until their expiry;
    they stay attached to the tombstone for audit. No new lease can be issued
    because every serving path excludes deleted/inactive rows.
    """
    from hive_platform.models import TokenEntry

    locked = TokenEntry.objects.select_for_update().get(pk=entry.pk)
    if locked.deleted_at:
        return {
            'deleted_at': locked.deleted_at,
            'active_leases': locked.leases.filter(expires_at__gt=timezone.now()).count(),
            'lease_expires_at': (
                locked.leases.filter(expires_at__gt=timezone.now())
                .order_by('-expires_at')
                .values_list('expires_at', flat=True)
                .first()
            ),
        }

    now = timezone.now()
    live_leases = locked.leases.filter(expires_at__gt=now)
    active_lease_count = live_leases.count()
    lease_expires_at = (
        live_leases.order_by('-expires_at').values_list('expires_at', flat=True).first()
    )
    locked.active = False
    locked.cooldown_until = None
    locked.token_ciphertext = ''
    locked.wrapped_dek = ''
    locked.dek_key_id = 'deleted'
    locked.quota_snapshot = {}
    locked.deleted_at = now
    locked.deleted_by = str(actor or '')[:254]
    locked.save(update_fields=[
        'active',
        'cooldown_until',
        'token_ciphertext',
        'wrapped_dek',
        'dek_key_id',
        'quota_snapshot',
        'deleted_at',
        'deleted_by',
        'updated_at',
    ])
    return {
        'deleted_at': now,
        'active_leases': active_lease_count,
        'lease_expires_at': lease_expires_at,
    }


# ── HIVE-124: Coordinator service core (pick / report-status / health-check) ──
#
# Selection, cooldown and health logic that ports the single-host daemon
# (`getActiveClaudeToken`) into the owner-scoped Coordinator. All selection is
# scoped to the requesting owner's pool — a pick for owner A can never return
# owner B's token (the storage layer enforces this; HIVE-127 enforces it at the
# API boundary). Decoupled from HIVE-127's lease model: "load" is a self-contained
# ``active_sessions`` counter (daemon parity), not a lease count.

# Default transient cooldown when a 429 carries no Retry-After (HLD: 1h).
RATE_LIMIT_COOLDOWN_SECONDS = 3600
# When weekly is exhausted but a model-specific (extra:*) window still has
# remaining capacity, do not park the whole credential for a full hour —
# short cool lets Spark / similar extras keep serving (operator 2026-07-23).
MODEL_SPECIFIC_REMAINING_COOLDOWN_CAP_SECONDS = 120

# PLAT-881: how long an auth-killed (active=False) token stays dead before it is
# eligible for ONE auto-recovery retry. Bounds the re-try rate for a genuinely
# revoked token (re-killed by the next auth report) so recovery never crash-loops.
AUTH_FAILED_RECOVERY_BACKOFF_SECONDS = 15 * 60

# Provider health-probe endpoints (cheap, ~8-token ping).
_PROBE_SPECS = {
    'anthropic': {
        'url': 'https://api.anthropic.com/v1/messages',
        'headers': lambda token: {
            'x-api-key': token,
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json',
        },
        'body': {
            'model': 'claude-haiku-4-5-20251001',
            'max_tokens': 8,
            'messages': [{'role': 'user', 'content': 'ping'}],
        },
    },
    'openai': {
        'url': 'https://api.openai.com/v1/chat/completions',
        'headers': lambda token: {
            'authorization': f'Bearer {token}',
            'content-type': 'application/json',
        },
        'body': {
            'model': 'gpt-4o-mini',
            'max_tokens': 8,
            'messages': [{'role': 'user', 'content': 'ping'}],
        },
    },
}


def pick_token(owner_kind, owner_id, provider, now=None, model=None):
    """Pick the best live token for an owner's pool (daemon ``getActiveClaudeToken`` parity).

    Order: drop inactive / on-cooldown (``active_for_owner``) → lowest ``priority``
    tier → least-loaded by ``active_sessions`` → LRU by ``last_used_at`` (never-used
    first) → ``created_at``. The winner's ``active_sessions`` is incremented and
    ``last_used_at`` stamped, atomically, so concurrent picks spread load. Returns
    the ``TokenEntry`` or ``None`` when the owner has no usable token.

    ``model`` (optional): when provided, entries whose ``supported_models``
    allowlist is non-empty and does NOT contain the model are skipped (PLAT-5850)
    — an account whose plan rejects the agent's pinned model is never leased.
    Empty/unknown ``supported_models`` preserves the existing lease path.
    """
    from django.db import transaction
    from django.db.models import F
    from django.utils import timezone

    from hive_platform.models import TokenEntry

    now = now or timezone.now()
    with transaction.atomic():
        candidates = (
            TokenEntry.objects
            .active_for_owner(owner_kind, owner_id, provider, now=now)
            .select_for_update()
            .order_by('priority', 'active_sessions', F('last_used_at').asc(nulls_first=True), 'created_at')
        )
        candidate = next(
            (entry for entry in candidates
             if quota_routing_eligible(entry, now=now)
             and model_supports_entry(entry, model)),
            None,
        )
        if candidate is None:
            return None
        candidate.active_sessions = F('active_sessions') + 1
        candidate.last_used_at = now
        candidate.save(update_fields=['active_sessions', 'last_used_at', 'updated_at'])
        candidate.refresh_from_db(fields=['active_sessions'])
    return candidate


# gpt-6-astra on the ChatGPT Codex path. Observed: Pro serves it; Go returns
# "not supported when using Codex with a ChatGPT account"; Free is the same
# class of ChatGPT plan rejection. Other gpt-6 slugs are not gated here.
_ASTRA_SLUG = 'gpt-6-astra'
_ASTRA_CAPABLE_PLANS = frozenset({
    'pro', 'plus', 'team', 'business', 'enterprise', 'edu',
})
_ASTRA_INCAPABLE_PLANS = frozenset({'free', 'go'})


def _bare_model_slug(model):
    slug = str(model or '').strip().lower()
    for prefix in ('cortex/', 'codex/', 'openai/'):
        if slug.startswith(prefix):
            return slug[len(prefix):]
    return slug


def _quota_plan(entry):
    snap = getattr(entry, 'quota_snapshot', None) or {}
    if not isinstance(snap, dict):
        return ''
    plan = snap.get('plan')
    if plan is None:
        return ''
    return str(plan).strip().lower()


def model_supports_entry(entry, model):
    """True when ``entry`` may serve ``model`` (PLAT-5850).

    An empty/unknown ``supported_models`` allowlist means "no capability
    constraint" — the existing fail-open lease path is preserved so an unprobed
    account is never falsely dried. A non-empty allowlist is authoritative: the
    entry is skipped unless ``model`` is listed, and that allowlist wins over
    the plan table (an explicit gpt-6-astra grant on a Go row is honored).

    The one empty-allowlist exception is gpt-6-astra on a known incapable
    ChatGPT plan (``free``, ``go``). Capable plans and an empty/unknown plan
    stay fail-open. Quota windows are not remapped onto models here.
    """
    if not model:
        return True
    allowlist = getattr(entry, 'supported_models', None) or []
    if allowlist:
        return model in allowlist
    if _bare_model_slug(model) != _ASTRA_SLUG:
        return True
    plan = _quota_plan(entry)
    if plan in _ASTRA_CAPABLE_PLANS:
        return True
    if plan in _ASTRA_INCAPABLE_PLANS:
        return False
    return True


def release_session(entry):
    """Mark one in-flight session on ``entry`` finished (decrement, floor 0)."""
    from django.db import transaction
    from django.db.models import F
    from hive_platform.models import TokenEntry

    with transaction.atomic():
        locked = TokenEntry.objects.select_for_update().filter(pk=entry.pk).first()
        if locked is None:
            return
        if locked.active_sessions > 0:
            locked.active_sessions = F('active_sessions') - 1
            locked.save(update_fields=['active_sessions', 'updated_at'])


def _normalize_status(status):
    """Map a status code/string to one of: 'ok' | 'rate_limited' | 'auth_failed'."""
    s = str(status).lower().strip()
    if s in ('ok', '200', 'success'):
        return 'ok'
    if s in ('429', 'quota', 'rate_limited', 'rate-limited', 'ratelimited'):
        return 'rate_limited'
    if s in ('401', '403', 'auth', 'auth_failed', 'unauthorized', 'invalid'):
        return 'auth_failed'
    return 'unknown'


def _entry_has_remaining_extra_window(entry) -> bool:
    """True when quota_snapshot has any extra:* window with remaining capacity.

    Codex reports both weekly (aggregate) and model-specific extras
    (e.g. ``extra:GPT-5.3-Codex-Spark``). Weekly can be 0% while Spark still
    has headroom — that must not permanently cool the credential.
    """
    snap = getattr(entry, 'quota_snapshot', None) or {}
    windows = snap.get('windows') or []
    if not isinstance(windows, list):
        return False
    for window in windows:
        if not isinstance(window, dict):
            continue
        wid = str(window.get('id') or '')
        if not wid.startswith('extra:'):
            continue
        left = window.get('left_percent')
        if left is None and window.get('used_percent') is not None:
            try:
                left = 100.0 - float(window['used_percent'])
            except (TypeError, ValueError):
                left = None
        try:
            if left is not None and float(left) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def apply_status_report(
    entry,
    status,
    retry_after=None,
    now=None,
    *,
    expected_fingerprint: str = '',
    reason: str = '',
):
    """Apply a report-status outcome to a token entry (HIVE-124 cooldown semantics).

    Provider health is updated without changing operator-owned leasing policy.

    - ``ok``        → clear cooldown, mark healthy. A terminally failed OpenAI
                      rotating grant stays dead until exact-row device re-auth;
                      a still-valid leased access token cannot revive it.
    - ``429``/quota → transient cooldown (Retry-After seconds, else 1h); rate-limit
                      stamp; entry stays active.
    - ``401``/auth  → immediate cool AND mark dead (``active=False``) so no agent
                      picks it again until an operator re-activates / rotates it.

    ``reason`` (HIVE-1747) records WHY the cooldown was stamped (e.g.
    ``quota_exhausted`` vs ``rate_limited``). It is written only when explicitly
    provided and cleared on ``ok``; the pre-dry self-heal sweep uses it to hold a
    provider-confirmed quota-exhausted credential non-routable for its requested
    window even when the usage API is unreadable (confidence='unsupported').

    Returns the normalized status applied. Idempotent: re-applying the same report
    is safe. Used by both the report-status endpoint (HIVE-127's view delegates
    here) and the health-check loop.
    """
    from datetime import timedelta
    from django.utils import timezone
    from hive_platform.models import TokenEntry, TokenPool

    if expected_fingerprint and entry.pk:
        with transaction.atomic():
            locked = (
                TokenEntry.objects
                .select_for_update()
                .select_related('pool')
                .get(pk=entry.pk)
            )
            if locked.token_fingerprint != expected_fingerprint:
                return 'generation_changed'
            return apply_status_report(
                locked,
                status,
                retry_after=retry_after,
                now=now,
                reason=reason,
            )

    now = now or timezone.now()
    normalized = _normalize_status(status)
    fields = ['last_status', 'updated_at']

    # A health-check queryset or in-flight lease may have loaded this row before
    # the quota refresher/migration recorded a newer xAI zero-credit snapshot.
    # Re-read the small lease-state surface before any success/rate-limit write.
    # This closes the observed rollout race where a stale ``ok`` writer cleared
    # all four confirmed Grok reset-length cooldowns after migration 0034.
    if (
        normalized in {'ok', 'rate_limited'}
        and entry.pool.provider == TokenPool.Provider.XAI
        and entry.pk
    ):
        current = TokenEntry.objects.only(
            'active', 'cooldown_until', 'last_rate_limit_at', 'last_status',
            'quota_snapshot',
        ).get(pk=entry.pk)
        entry.active = current.active
        entry.cooldown_until = current.cooldown_until
        entry.last_rate_limit_at = current.last_rate_limit_at
        entry.last_status = current.last_status
        entry.quota_snapshot = current.quota_snapshot

    exhausted_until = confirmed_xai_quota_exhausted_until(entry, now=now)

    if normalized == 'ok':
        if (
            not entry.active
            and entry.last_status == entry.LastStatus.AUTH_FAILED
            and (
                entry.pool.provider == TokenPool.Provider.OPENAI
                or (
                    entry.pool.provider == TokenPool.Provider.ANTHROPIC
                    and _anthropic_oauth_payload(entry.get_token())
                )
            )
        ):
            # An OpenAI access JWT can remain usable after its rotating refresh
            # grant has been revoked. A late success report from such a lease
            # proves only the access generation, not renewability; reactivating
            # here would put the terminally broken grant back into the broker.
            # codex_device_auth.ingest_codex_account is the sole revival path.
            return 'auth_failed'
        if exhausted_until is not None:
            # Authentication success does not mean subscription credits have
            # recovered. Keep the account active but non-routable until the
            # provider's own reset boundary.
            entry.cooldown_until = _storage_datetime(exhausted_until)
            entry.last_status = entry.LastStatus.RATE_LIMITED
            fields.append('cooldown_until')
            if entry.last_rate_limit_at is None:
                entry.last_rate_limit_at = now
                fields.append('last_rate_limit_at')
            if not entry.active:
                entry.active = True
                fields.append('active')
            if entry.cooldown_reason != 'quota_exhausted':
                entry.cooldown_reason = 'quota_exhausted'
                fields.append('cooldown_reason')
            entry.save(update_fields=list(dict.fromkeys(fields)))
            return 'rate_limited'
        entry.cooldown_until = None
        entry.last_status = entry.LastStatus.OK
        fields.append('cooldown_until')
        if entry.cooldown_reason:
            entry.cooldown_reason = ''
            fields.append('cooldown_reason')
        # PLAT-881: a previously auth-killed token that now reports healthy is
        # auto-recovered — flip it back active so the pool self-heals instead of
        # sitting dead until an operator manually re-enables it.
        if not entry.active:
            entry.active = True
            fields.append('active')
    elif normalized == 'rate_limited':
        try:
            secs = int(retry_after) if retry_after else RATE_LIMIT_COOLDOWN_SECONDS
        except (TypeError, ValueError):
            secs = RATE_LIMIT_COOLDOWN_SECONDS
        secs = max(secs, 1)
        # Codex/OpenAI accounts often keep model-specific capacity (e.g. the
        # GPT-5.3-Codex-Spark extra window) after the weekly aggregate is dead.
        # A multi-hour cool from the weekly 429 would strand that capacity and
        # surface as "no healthy backend"/auth thrash on Spark (operator 2026-07-23).
        # Cap cool when any extra:* window still has remaining quota.
        if _entry_has_remaining_extra_window(entry):
            secs = min(secs, MODEL_SPECIFIC_REMAINING_COOLDOWN_CAP_SECONDS)
        cooldown_until = now + timedelta(seconds=secs)
        if exhausted_until is not None:
            cooldown_until = max(
                _storage_datetime(cooldown_until),
                _storage_datetime(exhausted_until),
            )
        entry.cooldown_until = cooldown_until
        entry.last_rate_limit_at = now
        entry.last_status = entry.LastStatus.RATE_LIMITED
        fields += ['cooldown_until', 'last_rate_limit_at']
        # HIVE-1747: persist WHY this cooldown was stamped. Written only when the
        # reporter explicitly provides a reason (Cortex sends classification.reason);
        # a legacy no-reason report leaves a prior quota_exhausted hold intact so
        # the sweep cannot re-lease a provably out-of-credit credential. Only an
        # explicit reason change or an ``ok`` report clears it.
        if reason and entry.cooldown_reason != reason:
            entry.cooldown_reason = reason
            fields.append('cooldown_reason')
    elif normalized == 'auth_failed':
        entry.active = False
        # PLAT-881: stamp a recovery backoff (not ``now``) so the entry stays dead
        # for at least AUTH_FAILED_RECOVERY_BACKOFF_SECONDS, then becomes eligible
        # for ONE auto-recovery retry (recover_expired_dead_tokens). A genuinely
        # revoked token is re-killed by the next real auth report — bounding the
        # retry rate to the backoff, never a tight crash loop (HIVE-122).
        entry.cooldown_until = now + timedelta(seconds=AUTH_FAILED_RECOVERY_BACKOFF_SECONDS)
        entry.last_rate_limit_at = now
        entry.last_status = entry.LastStatus.AUTH_FAILED
        fields += ['active', 'cooldown_until', 'last_rate_limit_at']
        if entry.cooldown_reason != 'auth_failed':
            entry.cooldown_reason = 'auth_failed'
            fields.append('cooldown_reason')
    else:
        return 'unknown'

    entry.save(update_fields=list(dict.fromkeys(fields)))
    return normalized


def probe_token_health(entry, timeout=10):
    """Cheap bounded probe. Returns 'ok' | 'rate_limited' | 'auth_failed' | 'error'.

    A network/timeout error returns 'error' (NOT a cool) so a transient blip never
    kills a healthy token; only a real 401/429 from the provider changes state.
    """
    import requests
    from hive_platform.models import TokenPool

    spec = _PROBE_SPECS.get(entry.pool.provider)
    if spec is None:
        return 'error'
    try:
        token = entry.get_token()
    except Exception:
        return 'error'
    # Claude Code OAuth tokens (sk-ant-oat01-) CANNOT be health-probed via a raw
    # API call: a *working* OAuth token returns 401 when sent as x-api-key, and
    # 429 even with the correct Bearer+oauth beta header, because Anthropic
    # throttles raw-API use of the token differently from the Claude Code CLI's
    # request shape (claude-code identity/betas/system prompt). A raw probe
    # therefore CANNOT distinguish a working token from a dead one and would
    # wrongly cool/kill live tokens (it deactivated cl2+cl3 on 2026-06-24). So we
    # do NOT change OAuth-token state from a synthetic probe — 'error' updates
    # only last_checked_at, never cooldown/active. The authoritative self-heal for
    # OAuth tokens is REAL agent traffic (the bridge's 429 reports), not a probe.
    if isinstance(token, str) and token.startswith('sk-ant-oat01'):
        return 'error'
    # Anthropic FULL-LOGIN bundles (JSON with refresh material) are the THIRD
    # instance of the "structured payload probed as raw Bearer" bug class (see
    # the OAT guard above and the OpenAI/xAI guard below): Bearer {json} is
    # ALWAYS 401, so this probe deactivated both org-claude entries on
    # 2026-07-10 and recover_expired_dead_tokens (same probe) could never
    # revive them. A bundle's real health signal is the background OAuth refresh;
    # this request-path probe must never perform that refresh.  HIVE-619: the
    # broker gives up after 10s, while the old synchronous refresh could run for
    # 20s.  When Daphne cancelled the abandoned request, asgiref synchronously
    # joined the still-blocked worker and froze the event loop. Fresh-enough
    # bundles are 'ok'; near-expiry bundles are observational so the HA refresh
    # loop can service them without request/caller timeout inversion.
    if entry.pool.provider == TokenPool.Provider.ANTHROPIC and isinstance(token, str) and token.strip().startswith('{'):
        payload = _anthropic_oauth_payload(token)
        if not payload:
            return 'error'  # structured but no refresh material — observational only
        if not _claude_needs_refresh(payload):
            return 'ok'
        return 'error'
    # OpenAI/Codex entries store a structured JSON payload (access_token +
    # refresh_token + email + …), not a raw Bearer token. Sending the JSON blob as
    # "Bearer {json}" to api.openai.com returns 401 for a perfectly healthy token
    # (same class of bug as the sk-ant-oat01 case above). The OAuth refresh happens
    # at serve time in _selection_from_entry; a synthetic raw-API probe would
    # incorrectly deactivate live entries (observed 2026-06-28: both org OpenAI
    # entries killed by health-check → fleet drifted entirely to Anthropic/Claude).
    # The authoritative health signal for Codex entries is real serve-time traffic.
    if entry.pool.provider in (TokenPool.Provider.OPENAI, TokenPool.Provider.XAI):
        # Structured OAuth JSON payloads cannot be probed as raw Bearer tokens.
        # Authoritative health signal is serve-time traffic + OAuth refresh errors.
        return 'error'
    try:
        resp = requests.post(
            spec['url'], headers=spec['headers'](token), json=spec['body'], timeout=timeout,
        )
    except Exception:
        return 'error'
    if resp.status_code in (401, 403):
        return 'auth_failed'
    if resp.status_code == 429:
        return 'rate_limited'
    if 200 <= resp.status_code < 300:
        return 'ok'
    return 'error'


def recover_expired_dead_tokens(provider=None, now=None, probe=probe_token_health, owner_scope=None):
    """PLAT-881: bring an auth-killed token back into service once its recovery
    backoff has elapsed, so the pool SELF-HEALS instead of sitting dead until an
    operator manually re-activates it. (2026-06-26 half-fleet outage: cl2-primary
    sat active=False ~13h after its cooldown lapsed → coordinator pool dry → every
    claude agent failed at spawn.)

    Candidates are ONLY entries auto-killed by ``apply_status_report('auth_failed')``
    — ``active=False`` AND ``last_status=AUTH_FAILED`` AND a non-null
    ``cooldown_until`` (the recovery-backoff stamp) that has now expired. An
    OPERATOR-disabled token (the dashboard PATCH sets ``active=False`` WITHOUT
    stamping ``cooldown_until``) is therefore never auto-re-enabled.

    For probe-able tokens we re-probe and revive only those that don't still 401.
    Claude Code OAuth tokens (``sk-ant-oat01-``) CANNOT be probed (``probe`` returns
    'error'; see ``probe_token_health``) — for those, and for transient probe
    errors, we revive optimistically: if the token is genuinely dead the next real
    auth report re-kills it (with a fresh backoff), so the retry rate is bounded by
    the backoff, never a tight crash loop (HIVE-122). OpenAI is deliberately
    excluded from that optimistic path: an OpenAI ``auth_failed`` row represents a
    failed canonical rotating grant and only exact-row device re-auth may revive it.
    ``owner_scope`` (a TokenEntry queryset) limits the sweep to one owner's pool for
    the pre-dry safety net. Returns ``{candidates, recovered, still_dead,
    manual_reauth_required}``. Never raises per-entry.
    """
    from django.utils import timezone

    from hive_platform.models import TokenEntry, TokenPool

    now = now or timezone.now()
    qs = owner_scope if owner_scope is not None else TokenEntry.objects.all()
    qs = qs.filter(
        active=False,
        last_status=TokenEntry.LastStatus.AUTH_FAILED,
        cooldown_until__isnull=False,
        cooldown_until__lte=now,
    ).select_related('pool')
    if provider:
        qs = qs.filter(pool__provider=provider)

    summary = {
        'candidates': 0,
        'recovered': 0,
        'still_dead': 0,
        'manual_reauth_required': 0,
    }
    for entry in qs:
        summary['candidates'] += 1
        if entry.pool.provider == TokenPool.Provider.OPENAI or (
            entry.pool.provider == TokenPool.Provider.ANTHROPIC
            and _anthropic_oauth_payload(entry.get_token())
        ):
            # OpenAI's synthetic probe is deliberately unsupported. Treating
            # that ``error`` as permission to revive re-leases a revoked
            # rotating grant every backoff window. Only a successful exact-row
            # device re-auth ingest may reactivate this entry.
            summary['still_dead'] += 1
            summary['manual_reauth_required'] += 1
            continue
        try:
            result = probe(entry)
        except Exception:
            result = 'error'
        if result == 'auth_failed':
            # Still genuinely dead — re-arm the backoff and stay out of the pool.
            apply_status_report(entry, 'auth_failed', now=now)
            entry.last_checked_at = now
            entry.save(update_fields=['last_checked_at', 'updated_at'])
            summary['still_dead'] += 1
            continue
        # Revive: probed healthy, throttled-but-alive, or unprobeable (OAuth)/blip.
        entry.active = True
        entry.last_checked_at = now
        if result == 'rate_limited':
            # Auth is fine, just throttled — alive but cooling, not dead.
            entry.cooldown_until = now + timedelta(seconds=RATE_LIMIT_COOLDOWN_SECONDS)
            entry.last_rate_limit_at = now
            entry.last_status = TokenEntry.LastStatus.RATE_LIMITED
            entry.save(update_fields=['active', 'cooldown_until', 'last_rate_limit_at',
                                      'last_status', 'last_checked_at', 'updated_at'])
        elif result == 'ok':
            entry.cooldown_until = None
            entry.last_status = TokenEntry.LastStatus.OK
            entry.save(update_fields=['active', 'cooldown_until', 'last_status',
                                      'last_checked_at', 'updated_at'])
        else:  # 'error' — unprobeable (OAuth) or transient; let real traffic re-judge.
            entry.cooldown_until = None
            entry.last_status = TokenEntry.LastStatus.UNKNOWN
            entry.save(update_fields=['active', 'cooldown_until', 'last_status',
                                      'last_checked_at', 'updated_at'])
        summary['recovered'] += 1
    return summary


def run_health_check(provider=None, now=None, probe=probe_token_health):
    """Probe every active token and auto-cool/kill dead ones before agents pick them.

    This is the loop that prevents the baked-expired-token incident: a token whose
    auth has died is marked dead here, so ``pick_token`` never hands it out. It also
    runs ``recover_expired_dead_tokens`` (PLAT-881) so auth-killed tokens are
    auto-revived once their recovery backoff lapses. It also clears stale
    rate-limit cooldowns proactively (PLAT-924) so the pool does not stay dry
    until the next serve-miss. Returns a summary
    ``{checked, ok, rate_limited, dead, errored, recovered, still_dead, cooldown_cleared}``.
    ``probe`` is injectable for tests so no real network call is made.
    """
    from django.db.models import Q
    from django.utils import timezone

    from hive_platform.models import TokenEntry

    now = now or timezone.now()
    # PLAT-881: first attempt to revive auth-killed tokens whose backoff has lapsed
    # (a dead entry is active=False, so the active-only probe loop below skips it).
    recovery = recover_expired_dead_tokens(provider=provider, now=now, probe=probe)
    # PLAT-924: also clear lingering/stale rate-limit cooldowns proactively, so the
    # periodic health loop self-heals the codex/openai pool-dry case (ALL tokens
    # active but cooled at once) the same way the reactive pre-dry net does — not
    # only on a serve-miss (2026-06-26 half-fleet, 2026-06-27 x2). Bounded identically:
    # a still-throttled token whose cooldown we clear is re-cooled by the probe loop
    # below or the next real rate-limit report. A FRESH cooldown is left untouched.
    stale = recover_stale_cooldown_tokens(provider=provider, now=now)
    qs = TokenEntry.objects.filter(active=True).select_related('pool')
    if provider:
        qs = qs.filter(pool__provider=provider)
    # ren P1: skip entries currently on cooldown — match pick_token eligibility.
    # Probing a cooled token and getting 'ok' would otherwise clear a live 429/quota
    # Retry-After cooldown and feed it back to the picker early. A cooled token
    # becomes eligible (and gets probed) naturally once its cooldown expires.
    qs = qs.filter(Q(cooldown_until__isnull=True) | Q(cooldown_until__lte=now))

    summary = {'checked': 0, 'ok': 0, 'rate_limited': 0, 'dead': 0, 'errored': 0,
               'recovered': recovery['recovered'], 'still_dead': recovery['still_dead'],
               'cooldown_cleared': stale['cleared']}
    for entry in qs:
        result = probe(entry)
        summary['checked'] += 1
        if result == 'error':
            summary['errored'] += 1
            entry.last_checked_at = now
            entry.save(update_fields=['last_checked_at', 'updated_at'])
            continue
        apply_status_report(entry, result, now=now)
        entry.last_checked_at = now
        entry.save(update_fields=['last_checked_at', 'updated_at'])
        if result == 'ok':
            summary['ok'] += 1
        elif result == 'rate_limited':
            summary['rate_limited'] += 1
        elif result == 'auth_failed':
            summary['dead'] += 1
    return summary


# ── HIVE-583: subscription OAuth refresh loop (HA background worker) ──
#
# Runs on every Hive pod but uses a DB advisory lock so only one pod
# actively refreshes at a time. Checks active refreshable OpenAI, xAI, and
# Anthropic entries ahead of access expiry. On refresh failure the entry is
# cooled for 5 minutes when transient; terminal OAuth responses park the row
# until re-auth.

CODEX_REFRESH_LEADER_LOCK_ID = 583583  # unique lock id for pg_advisory_lock
CODEX_REFRESH_LEADER_TIMEOUT = 5       # seconds to wait for the lock
CODEX_REFRESH_PRE_EXPIRY_SECONDS = 8 * 60  # 8 minutes before expiry
XAI_REFRESH_PRE_EXPIRY_SECONDS = 10 * 60  # exceed the 5-minute leader cadence
CODEX_REFRESH_COOLDOWN_ON_FAILURE = 5 * 60  # 5 minutes


def _openai_entries_needing_refresh(now=None):
    """Yield active OpenAI TokenEntry rows whose access_token is near expiry."""
    from django.utils import timezone

    from hive_platform.models import TokenEntry, TokenPool

    now = now or timezone.now()
    qs = (
        TokenEntry.objects
        .filter(active=True, pool__provider=TokenPool.Provider.OPENAI)
        .select_related('pool')
        .filter(models.Q(cooldown_until__isnull=True) | models.Q(cooldown_until__lte=now))
    )
    for entry in qs:
        try:
            raw = entry.get_token()
        except Exception:
            continue
        payload = _json_or_refresh_token(raw)
        refresh_token = payload.get('refresh_token')
        access_token = payload.get('access_token')
        if not refresh_token or not access_token:
            continue
        remaining = jwt_expiry_seconds(access_token)
        if remaining is None:
            continue
        if remaining > CODEX_REFRESH_PRE_EXPIRY_SECONDS:
            continue  # still fresh
        yield entry, payload


def _xai_entries_needing_refresh(now=None):
    """Yield active xAI OAuth rows whose access token needs rotation.

    xAI upkeep must not depend on quota-snapshot staleness.  The quota sweep is
    intentionally less frequent than the five-minute leader cadence and can
    otherwise observe an access token just outside its own five-minute refresh
    window, then revisit it only after expiry.  Rate-limit cooldowns gate model
    leasing, not OAuth custody, so cooled rows remain eligible for upkeep.
    """
    from hive_platform.models import TokenEntry, TokenPool

    qs = (
        TokenEntry.objects
        .filter(
            active=True,
            pool__provider=TokenPool.Provider.XAI,
            deleted_at__isnull=True,
        )
        .select_related('pool')
    )
    for entry in qs:
        try:
            payload = _json_xai_oauth_token(entry.get_token())
        except Exception:
            continue
        if not payload.get('refresh_token'):
            continue
        if not _xai_needs_refresh(
            payload,
            buffer_seconds=XAI_REFRESH_PRE_EXPIRY_SECONDS,
        ):
            continue
        yield entry, payload


def _anthropic_entries_needing_refresh(now=None):
    """Yield active Anthropic/Claude full-login bundle rows near access expiry.

    HIVE-606: only bundles that carry a refresh token are refreshable; bare
    ``claude setup-token`` OATs have no refresh material and stay
    observational-only (``_anthropic_oauth_payload`` returns None for them)."""
    from django.utils import timezone

    from hive_platform.models import TokenEntry, TokenPool

    now = now or timezone.now()
    # NOTE: cooldown does NOT exclude an entry from refresh — cooldown gates
    # LEASING, not token upkeep. A rate-limit-cooled bundle whose access token
    # expires during the cooldown used to come back unrefreshable (serve-time
    # 401 -> auth_failed -> deactivated); keeping material fresh while cooling
    # closes that window.
    qs = (
        TokenEntry.objects
        .filter(active=True, pool__provider=TokenPool.Provider.ANTHROPIC)
        .select_related('pool')
    )
    for entry in qs:
        try:
            raw = entry.get_token()
        except Exception:
            continue
        payload = _anthropic_oauth_payload(raw)
        if not payload:
            continue  # bare OAT — observational only
        if not _claude_needs_refresh(payload):
            continue  # still fresh
        yield entry, payload


def _quota_fetched_at(snapshot: dict):
    """Return parsed quota snapshot timestamp, or None when unavailable."""
    from django.utils.dateparse import parse_datetime

    fetched_at = str((snapshot or {}).get('fetched_at') or '').strip()
    if not fetched_at:
        return None
    try:
        parsed = parse_datetime(fetched_at)
    except Exception:
        return None
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, dt_timezone.utc)
    return parsed


def _quota_now(now=None):
    """Return an aware timestamp for quota freshness comparisons."""
    # Hive runs with USE_TZ=False, where timezone.now() is a naive wall-clock
    # value in TIME_ZONE. Labelling that local time as UTC makes snapshots look
    # hours newer than they are and prevents the background refresh from firing.
    now = now if now is not None else datetime.now(dt_timezone.utc)
    if timezone.is_naive(now):
        return timezone.make_aware(now, dt_timezone.utc)
    return now


def _parse_quota_reset(value):
    """Parse a provider reset timestamp into aware UTC for comparisons."""
    from django.utils.dateparse import parse_datetime

    raw = str(value or '').strip()
    if not raw:
        return None
    try:
        parsed = parse_datetime(raw)
    except Exception:
        return None
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, dt_timezone.utc)
    return parsed.astimezone(dt_timezone.utc)


def confirmed_xai_quota_exhausted_until(entry_or_snapshot, *, now=None):
    """Return the future reset for a confirmed zero-credit xAI snapshot.

    Grok's billing snapshot is stronger evidence than a generic retry timer: when
    it says the single Credits window has 0% left, re-leasing the account before
    ``resets_at`` can only produce another 403. Snapshot age is deliberately not
    used to erase that fact; the reset boundary (or a later positive snapshot) is
    the recovery authority.
    """
    from hive_platform.models import TokenPool

    snapshot = entry_or_snapshot
    provider = None
    if not isinstance(entry_or_snapshot, dict):
        snapshot = getattr(entry_or_snapshot, 'quota_snapshot', None) or {}
        provider = getattr(getattr(entry_or_snapshot, 'pool', None), 'provider', None)
    if provider is not None and provider != TokenPool.Provider.XAI:
        return None
    snapshot = snapshot or {}
    if str(snapshot.get('source') or '') != 'grok_xai_usage':
        return None
    if str(snapshot.get('confidence') or '') not in {'ok', 'exact'}:
        return None

    measured = []
    for window in snapshot.get('windows') or []:
        if not isinstance(window, dict):
            continue
        left = window.get('left_percent')
        if left is None and window.get('used_percent') is not None:
            try:
                left = 100.0 - float(window['used_percent'])
            except (TypeError, ValueError):
                left = None
        try:
            left = float(left) if left is not None else None
        except (TypeError, ValueError):
            left = None
        if left is None:
            continue
        measured.append((left, _parse_quota_reset(window.get('resets_at'))))
    if not measured or any(left > 0 for left, _reset in measured):
        return None

    now_utc = _quota_now(now)
    future_resets = [reset for _left, reset in measured if reset and reset > now_utc]
    return max(future_resets) if future_resets else None


def _snapshot_confirms_quota_resolved(entry, *, now=None) -> bool:
    """True when a confirmed zero-credit xAI snapshot's reset boundary has passed.

    A snapshot that confirmed exhaustion but whose reset is now in the past means
    the provider's quota window has reset — the cooldown's underlying reason is
    gone and the age heuristic may clear it. This distinguishes a stale extended
    cooldown (clear) from a still-active deliberate long cool (respect), so the
    HIVE-1747 requested-window guard does not strand a pool whose exhaustion has
    actually resolved.
    """
    from hive_platform.models import TokenPool

    snapshot = getattr(entry, 'quota_snapshot', None) or {}
    provider = getattr(getattr(entry, 'pool', None), 'provider', None)
    if provider is not None and provider != TokenPool.Provider.XAI:
        return False
    if str(snapshot.get('source') or '') != 'grok_xai_usage':
        return False
    if str(snapshot.get('confidence') or '') not in {'ok', 'exact'}:
        return False
    now_utc = _quota_now(now)
    for window in snapshot.get('windows') or []:
        if not isinstance(window, dict):
            continue
        reset = _parse_quota_reset(window.get('resets_at'))
        if reset is not None and reset <= now_utc:
            return True
    return False


def _all_quota_windows_exhausted(entry, *, now=None) -> bool:
    """True only when EVERY window with data reports 0% left and a future reset.

    Deliberately conservative: a pool with ANY remaining headroom stays
    eligible, and a missing/unparseable snapshot fails OPEN (eligible). We are
    excluding proven-dead credentials, not guessing at capacity.

    All time comparisons run in AWARE UTC (via ``_quota_now`` /
    ``_parse_quota_reset``). The live quota snapshots carry aware ``+00:00``
    reset stamps; Hive runs with ``USE_TZ=False``, so ``timezone.now()`` is a
    NAIVE wall-clock value and comparing it directly to ``parse_datetime()``
    raised ``TypeError: can't compare offset-naive and offset-aware``. That
    exception was swallowed by the broad ``except`` and FAILED OPEN, marking a
    proven-exhausted free account (0% left, reset weeks away) as eligible and
    keeping the whole codex fleet pinned to it (2026-08-02 onward: every
    codex agent silently failed every turn, freezing Hive's live-activity
    feeds). Compare in one consistent aware frame so exhaustion is detected.
    """
    now = _quota_now(now)
    snap = getattr(entry, 'quota_snapshot', None) or {}
    windows = snap.get('windows') if isinstance(snap, dict) else None
    if not isinstance(windows, list) or not windows:
        return False
    saw_window = False
    for w in windows:
        if not isinstance(w, dict):
            continue
        left = w.get('left_percent')
        if left is None:
            continue
        try:
            if float(left) > 0:
                return False  # headroom somewhere -> keep eligible
        except (TypeError, ValueError):
            return False
        # A zero without a parseable future reset is only a coarse snapshot,
        # not proof that this credential cannot serve.  Keep it eligible as the
        # all-siblings-exhausted last resort: model-specific allowance may still
        # exist, and real-traffic status reports remain authoritative.  This
        # also keeps ModelTokenView from emitting ``pool_dry`` while a safe
        # last-resort issuance is still possible (HIVE-796 reconciliation).
        reset = _parse_quota_reset(w.get('resets_at'))
        if reset is None or reset <= now:
            return False
        saw_window = True
    return saw_window


def quota_routing_eligible(entry, *, now=None) -> bool:
    """Whether quota evidence permits leasing this otherwise-active entry.

    Was xAI-ONLY. For every other provider it returned True unconditionally, so
    a credential Hive had already proven unusable stayed selectable — the model
    literally labels one state `'auth_failed', 'Auth failed (dead)'` — and
    `active_for_scopes` filters only active/leasing_enabled/cooldown, never
    `last_status`.

    Consequence (operator 2026-08-04): 24 of 59 agents sat provider_unavailable
    on OpenAI `rate_limit` errors while a pool with 57% weekly / 17% Spark
    remaining was available. Agents were leased dead tokens, burned their turn,
    and reported quota exhaustion that was an artefact of pool SELECTION rather
    than of actual exhaustion.

    Now provider-agnostic, using evidence Hive already records.
    """
    from hive_platform.models import TokenEntry

    if getattr(entry, 'last_status', None) == TokenEntry.LastStatus.AUTH_FAILED:
        return False
    if _all_quota_windows_exhausted(entry, now=now):
        return False
    return (
        confirmed_xai_quota_exhausted_until(entry, now=now) is None
        and not confirmed_xai_auth_unavailable(entry)
    )


def reassert_confirmed_xai_credit_cooldowns(*, now=None, dry_run: bool = False) -> dict:
    """Repair lease state from authoritative zero-credit xAI snapshots.

    Data migrations run before a rolling deployment replaces the old workers.
    A worker that loaded a row before that migration can therefore write an
    obsolete ``ok`` result after the migration completes.  The status-report
    chokepoint prevents that once the new code is live; this leader-pass
    reconciliation closes the finite old-worker handoff without probing xAI or
    waiting for a credential that is intentionally non-routable to be leased.
    """
    from hive_platform.models import TokenEntry, TokenPool

    now = now or timezone.now()
    entries = TokenEntry.objects.select_related('pool').filter(
        pool__provider=TokenPool.Provider.XAI,
        active=True,
        agent_leasing_enabled=True,
        deleted_at__isnull=True,
    )
    summary = {'checked': 0, 'reasserted': 0}
    for entry in entries.iterator():
        reset = confirmed_xai_quota_exhausted_until(entry, now=now)
        if reset is None:
            continue
        summary['checked'] += 1
        reset_for_storage = _storage_datetime(reset)
        current_cooldown = entry.cooldown_until
        if (
            current_cooldown is not None
            and timezone.is_aware(current_cooldown) != timezone.is_aware(reset_for_storage)
        ):
            current_cooldown = _storage_datetime(_quota_now(current_cooldown))
        needs_repair = (
            current_cooldown is None
            or current_cooldown < reset_for_storage
            or entry.last_status != entry.LastStatus.RATE_LIMITED
            or entry.last_rate_limit_at is None
        )
        if not needs_repair:
            continue
        summary['reasserted'] += 1
        if dry_run:
            continue
        fields = {
            'cooldown_until': reset_for_storage,
            'last_status': entry.LastStatus.RATE_LIMITED,
            'updated_at': now,
        }
        if entry.last_rate_limit_at is None:
            fields['last_rate_limit_at'] = now
        TokenEntry.objects.filter(pk=entry.pk).update(**fields)
    return summary


def confirmed_xai_auth_unavailable(entry_or_snapshot) -> bool:
    """Whether the latest xAI refresh proved the account unusable for routing."""
    from hive_platform.models import TokenPool

    snapshot = entry_or_snapshot
    provider = None
    if not isinstance(entry_or_snapshot, dict):
        snapshot = getattr(entry_or_snapshot, 'quota_snapshot', None) or {}
        provider = getattr(getattr(entry_or_snapshot, 'pool', None), 'provider', None)
    if provider is not None and provider != TokenPool.Provider.XAI:
        return False
    snapshot = snapshot or {}
    detail = str(snapshot.get('detail') or '')
    return (
        str(snapshot.get('source') or '') == 'grok_xai_usage'
        and str(snapshot.get('confidence') or '') == 'error'
        and (
            detail == 'oauth_refresh_failed'
            or detail.startswith('oauth_refresh_failed:')
        )
    )


def _storage_datetime(value):
    """Match reset timestamps to Hive's DateTimeField awareness convention."""
    from django.conf import settings

    if value is None or getattr(settings, 'USE_TZ', False):
        return value
    if timezone.is_aware(value):
        return timezone.make_naive(value, dt_timezone.utc)
    return value


def _preserve_quota_cooldown(entry, exhausted_until, *, now=None) -> bool:
    """Extend a quota cooldown to its confirmed reset without shortening it."""
    reset_for_storage = _storage_datetime(exhausted_until)
    current = entry.cooldown_until
    if current is not None and timezone.is_aware(current) != timezone.is_aware(reset_for_storage):
        current = _storage_datetime(_quota_now(current))
    fields = []
    if current is None or current < reset_for_storage:
        entry.cooldown_until = reset_for_storage
        fields.append('cooldown_until')
    if entry.last_status != entry.LastStatus.RATE_LIMITED:
        entry.last_status = entry.LastStatus.RATE_LIMITED
        fields.append('last_status')
    if entry.last_rate_limit_at is None:
        entry.last_rate_limit_at = now or timezone.now()
        fields.append('last_rate_limit_at')
    if fields:
        entry.save(update_fields=[*fields, 'updated_at'])
        return True
    return False


# Inference-only OATs / providers with no usage API never grow windows. Re-hit
# them at most once a day so the auto-refresh loop does not waste provider
# quota every 10 minutes on a permanent capability gap.
UNSUPPORTED_QUOTA_RECHECK_SECONDS = 24 * 60 * 60


def _quota_snapshot_needs_refresh(entry, *, now=None, stale_after_seconds: int = 30 * 60) -> bool:
    """True when a subscription quota snapshot is absent or stale.

    ``confidence=unsupported`` is a permanent capability gap (inference-only
    Claude OAT, no xAI usage API, …). Once we have recorded that fact, only
    re-check once a day — not on the normal 10–30 min dashboard cadence.
    """
    now = _quota_now(now)
    snapshot = getattr(entry, 'quota_snapshot', None) or {}
    if str(snapshot.get('source') or '') == 'codex_wham_usage':
        # Parser semantics are part of snapshot freshness.  A release that
        # learns a new provider window shape must not keep serving a recently
        # fetched but misclassified cached snapshot for another 30 minutes.
        from hive_platform.quota_fetchers import CODEX_QUOTA_SCHEMA_VERSION
        try:
            snapshot_version = int(snapshot.get('schema_version') or 0)
        except (TypeError, ValueError):
            snapshot_version = 0
        if snapshot_version < CODEX_QUOTA_SCHEMA_VERSION:
            return True
    fetched_at = _quota_fetched_at(snapshot)
    if fetched_at is None:
        return True
    effective_stale = max(int(stale_after_seconds), 60)
    if str(snapshot.get('confidence') or '') == 'unsupported':
        effective_stale = max(effective_stale, UNSUPPORTED_QUOTA_RECHECK_SECONDS)
    return (now - fetched_at).total_seconds() >= effective_stale


def quota_snapshot_status(snapshot: dict, *, now=None, stale_after_seconds: int = 30 * 60) -> dict:
    """Summarize a normalized quota snapshot for dashboard rows."""
    from hive_platform.quota_projection_v2 import project_quota_snapshot

    now = _quota_now(now)
    snapshot = project_quota_snapshot(snapshot)
    windows = snapshot.get('windows') or []
    fetched_at = _quota_fetched_at(snapshot)
    min_left = None
    next_reset = None
    for window in windows:
        if not isinstance(window, dict):
            continue
        left = window.get('left_percent')
        if left is None and window.get('used_percent') is not None:
            try:
                left = 100.0 - float(window['used_percent'])
            except (TypeError, ValueError):
                left = None
        if left is not None:
            try:
                left = max(0.0, min(100.0, float(left)))
            except (TypeError, ValueError):
                left = None
        if left is not None:
            min_left = left if min_left is None else min(min_left, left)
        reset = window.get('resets_at')
        if reset and (next_reset is None or str(reset) < str(next_reset)):
            next_reset = str(reset)

    stale = True
    age_seconds = None
    if fetched_at is not None:
        age_seconds = int(max(0, (now - fetched_at).total_seconds()))
        stale = age_seconds >= max(int(stale_after_seconds), 60)

    confidence = str(snapshot.get('confidence') or '')
    if not snapshot:
        status = 'missing'
    elif confidence == 'error':
        status = 'error'
    elif confidence == 'unsupported':
        # Provider has no usage/quota API for this credential (xAI/Grok has no
        # per-account usage endpoint; inference-only Claude OATs lack the scope).
        # This is an expected capability gap, NOT staleness or an error — mark it
        # so the UI shows a neutral dash and the "quota attention" alarm ignores it.
        status = 'unsupported'
    elif stale:
        status = 'stale'
    elif not windows:
        status = 'partial'
    else:
        status = 'ok'

    return {
        'status': status,
        'confidence': confidence or None,
        'source': snapshot.get('source') or None,
        'plan': snapshot.get('plan') or None,
        'windows': len(windows),
        'min_left_percent': round(min_left, 2) if min_left is not None else None,
        'next_reset_at': next_reset,
        'fetched_at': fetched_at.isoformat() if fetched_at else None,
        'age_seconds': age_seconds,
        'detail': snapshot.get('detail') or '',
    }


def _anthropic_usage_token(raw: str) -> str:
    """Extract a Claude OAuth access token from raw or JSON token-pool material.

    Accepts a bare sk-ant-oat01 token, a flat JSON bundle, or the Claude Code
    CLI credentials file shape (~/.claude/.credentials.json) which nests the
    bundle under 'claudeAiOauth' — so operators can paste that file verbatim."""
    value = (raw or '').strip()
    if not value:
        return ''
    if value.startswith('sk-ant-oat01'):
        return value
    if not value.startswith('{'):
        return ''
    try:
        payload = json.loads(value)
    except Exception:
        return ''
    if not isinstance(payload, dict):
        return ''
    nested = payload.get('claudeAiOauth')
    if isinstance(nested, dict):
        payload = nested
    token = (
        payload.get('accessToken')
        or payload.get('access_token')
        or payload.get('token')
        or ''
    )
    token = str(token)
    return token if token.startswith('sk-ant-oat01') else ''


def _anthropic_oauth_payload(raw: str) -> dict | None:
    """Parse Claude OAuth bundle material (flat or claudeAiOauth-nested).

    Returns the payload dict only when it carries a refresh token — bare OAT
    entries stay observational-only (no refresh path exists for them)."""
    value = (raw or '').strip()
    if not value.startswith('{'):
        return None
    try:
        payload = json.loads(value)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    nested = payload.get('claudeAiOauth')
    if isinstance(nested, dict):
        payload = dict(nested)
    if not (payload.get('refresh_token') or payload.get('refreshToken')):
        return None
    return payload


def _claude_needs_refresh(payload: dict, *, buffer_seconds: int = 300) -> bool:
    """True when the bundle's access token is expired or near expiry.

    Handles both shapes: 'expires_at' ISO string (hive bundles) and
    'expiresAt' epoch milliseconds (Claude Code credentials file)."""
    from datetime import datetime, timezone as dt_timezone

    expires_at = payload.get('expires_at') or payload.get('expiresAt')
    if expires_at in (None, ''):
        return False
    try:
        if isinstance(expires_at, (int, float)):
            ts = float(expires_at)
            if ts > 1e12:
                ts /= 1000.0
            exp = datetime.fromtimestamp(ts, tz=dt_timezone.utc)
        else:
            exp = datetime.fromisoformat(str(expires_at))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=dt_timezone.utc)
    except (TypeError, ValueError):
        return False
    return exp <= datetime.now(dt_timezone.utc) + timedelta(seconds=buffer_seconds)


def _refresh_and_persist_claude_bundle(
    entry, payload: dict, *, raise_on_reject: bool = False, force: bool = False,
) -> str:
    """Refresh the locked canonical generation; never return stale access on failure."""
    from hive_platform.claude_oauth import ClaudeOAuthError, build_bundle, refresh_claude_oauth
    from hive_platform.models import TokenEntry

    caller_fingerprint = str(entry.token_fingerprint or '')
    with transaction.atomic():
        locked = TokenEntry.objects.select_for_update().select_related('pool').get(pk=entry.pk)
        if not locked.active and locked.last_status == locked.LastStatus.AUTH_FAILED:
            failure = AnthropicOAuthRefreshError(
                'canonical Claude grant requires reauthentication',
                status_code=400, error_code='invalid_grant',
            )
            failure.credential_fingerprint = locked.token_fingerprint
            raise failure
        raw = locked.get_token()
        payload = _anthropic_oauth_payload(raw)
        if not payload:
            raise AnthropicOAuthRefreshError('canonical Claude bundle has no refresh token')
        generation_changed = locked.token_fingerprint != caller_fingerprint
        if _claude_needs_refresh(payload) or not _anthropic_usage_token(raw) or (force and not generation_changed):
            try:
                tokens = refresh_claude_oauth(payload, timeout_seconds=5)
            except ClaudeOAuthError as exc:
                failure = AnthropicOAuthRefreshError(
                    str(exc), status_code=exc.status_code, error_code=exc.error_code,
                )
                failure.credential_fingerprint = locked.token_fingerprint
                raise failure from exc
            if not tokens.get('refresh_token'):
                tokens['refresh_token'] = payload.get('refresh_token') or payload.get('refreshToken') or ''
            email = str(payload.get('email') or locked.account_email or '')
            bundle = build_bundle(tokens, email=email)
            _persist_oauth_payload(locked, bundle)
            raw = json.dumps(bundle, sort_keys=True)
        for field in ('token_ciphertext', 'wrapped_dek', 'dek_key_id', 'token_fingerprint'):
            setattr(entry, field, getattr(locked, field))
        return raw


def refresh_entry_quota_snapshot(
    entry,
    *,
    now=None,
    force: bool = False,
    stale_after_seconds: int = 30 * 60,
    refresh_oauth: bool = True,
    mutate_lease_state: bool = True,
) -> dict:
    """Refresh one TokenEntry's redacted subscription quota snapshot.

    This is the CodexBar-equivalent data path. It intentionally persists only a
    normalized, redacted snapshot: source, confidence, plan, quota windows,
    reset timestamps, and bounded diagnostics. Raw tokens never leave the
    encrypted TokenEntry material.

    ``mutate_lease_state`` controls whether a successful/failed usage fetch also
    flips ``active`` / cooldown via ``apply_status_report``. Background dashboard
    refresh of *operator-disabled* accounts sets this False so a quiet quota
    poll never re-enables a credential an operator turned off. Check-now and
    active-pool refresh leave it True (default).
    """
    from hive_platform.models import TokenPool
    from hive_platform.quota_fetchers import (
        fetch_claude_oauth_usage,
        fetch_codex_wham_usage,
        fetch_grok_usage,
        persist_quota_snapshot,
    )

    now = now or timezone.now()
    quota_now = _quota_now()
    generation_fingerprint = str(entry.token_fingerprint or '')
    summary = {
        'entry_id': str(entry.id),
        'provider': entry.pool.provider,
        'checked': False,
        'updated': False,
        'detail': '',
    }
    if not force and not _quota_snapshot_needs_refresh(
        entry, now=quota_now, stale_after_seconds=stale_after_seconds,
    ):
        summary['detail'] = 'fresh_skipped'
        return summary

    def _report(status, **kwargs):
        if mutate_lease_state:
            apply_status_report(
                entry,
                status,
                now=now,
                expected_fingerprint=(
                    generation_fingerprint
                    if entry.pool.provider in (TokenPool.Provider.XAI, TokenPool.Provider.ANTHROPIC)
                    else ''
                ),
                **kwargs,
            )

    try:
        raw = entry.get_token()
    except Exception as exc:
        snap = {
            'source': 'token_pool',
            'fetched_at': quota_now.isoformat(),
            'confidence': 'error',
            'plan': None,
            'windows': [],
            'detail': f'token_decrypt_failed:{type(exc).__name__}',
        }
        persist_quota_snapshot(entry, snap)
        snap = entry.quota_snapshot
        summary.update({'checked': True, 'updated': True, 'detail': snap['detail']})
        return summary

    provider = entry.pool.provider
    provider_source = {
        TokenPool.Provider.OPENAI: 'codex_wham_usage',
        TokenPool.Provider.XAI: 'grok_xai_usage',
        TokenPool.Provider.ANTHROPIC: 'claude_oauth_usage',
    }.get(provider, 'token_pool')
    snap = None
    try:
        if provider == TokenPool.Provider.OPENAI:
            payload = _json_or_refresh_token(raw)
            if refresh_oauth and payload.get('refresh_token') and _openai_needs_refresh(payload):
                _, payload, _ = _refresh_openai_entry_payload(entry)
            account_id = str(payload.get('account_id') or '')
            if not account_id:
                account_id = openai_account_id(payload.get('access_token') or '') or ''
            snap = fetch_codex_wham_usage(
                payload.get('access_token') or '',
                account_id=account_id,
            )
            if snap.get('detail') == 'auth_failed_http_401' and payload.get('refresh_token'):
                # One forced refresh retry handles stale access JWTs without
                # marking the subscription dead from a quota endpoint alone.
                _, payload, _ = _refresh_openai_entry_payload(entry, force=True)
                snap = fetch_codex_wham_usage(
                    payload.get('access_token') or '',
                    account_id=payload.get('account_id') or account_id,
                )
            if snap.get('detail') == 'rate_limited':
                _report('rate_limited', retry_after=CODEX_REFRESH_COOLDOWN_ON_FAILURE)
            elif snap.get('confidence') in {'exact', 'partial'} and snap.get('detail') != 'auth_failed_http_401':
                _report('ok')
            # Enrich the "usage resets available" chip with the dedicated
            # reset-credits endpoint (total earned + expiries). Best-effort:
            # the usage payload's available_count already populated the field.
            if snap.get('detail') == 'ok':
                from hive_platform.quota_fetchers import fetch_codex_reset_credits
                detail = fetch_codex_reset_credits(
                    payload.get('access_token') or '',
                    account_id=str(payload.get('account_id') or '') or account_id,
                )
                if detail:
                    merged = dict(snap.get('reset_credits') or {})
                    merged.update(detail)
                    snap['reset_credits'] = merged
        elif provider == TokenPool.Provider.XAI:
            payload = _json_xai_oauth_token(raw)
            if refresh_oauth and payload.get('refresh_token') and _xai_needs_refresh(payload):
                entry, payload, _ = _refresh_xai_entry_payload(entry)
                generation_fingerprint = str(entry.token_fingerprint or '')
            snap = fetch_grok_usage(payload.get('access_token') or '')
            if snap.get('detail') == 'auth_failed':
                _report('auth_failed')
            elif snap.get('confidence') in {'exact', 'partial'}:
                _report('ok')
        elif provider == TokenPool.Provider.ANTHROPIC:
            payload = _anthropic_oauth_payload(raw)
            if payload and refresh_oauth and _claude_needs_refresh(payload):
                raw = _refresh_and_persist_claude_bundle(entry, payload) or raw
                generation_fingerprint = str(entry.token_fingerprint or '')
            access = _anthropic_usage_token(raw)
            if access:
                snap = fetch_claude_oauth_usage(access)
                if snap.get('detail') == 'auth_failed' and payload and refresh_oauth:
                    # Expired access token on a full-OAuth bundle: refresh once
                    # and retry before declaring the credential dead.
                    refreshed_raw = _refresh_and_persist_claude_bundle(entry, payload, force=True)
                    if refreshed_raw:
                        generation_fingerprint = str(entry.token_fingerprint or '')
                        access = _anthropic_usage_token(refreshed_raw)
                        snap = fetch_claude_oauth_usage(access)
                if snap.get('confidence') == 'exact' and snap.get('windows'):
                    _report('ok')
                    if not getattr(entry, 'account_email', ''):
                        from hive_platform.quota_fetchers import fetch_claude_profile_email

                        email = fetch_claude_profile_email(access)
                        if email:
                            entry.account_email = email
                            entry.save(update_fields=['account_email', 'updated_at'])
                elif snap.get('detail') == 'auth_failed':
                    _report('auth_failed')
            else:
                snap = {
                    'source': 'claude_oauth_usage',
                    'fetched_at': quota_now.isoformat(),
                    'confidence': 'partial',
                    'plan': None,
                    'windows': [],
                    'detail': 'not_oauth_subscription_token',
                }
        elif provider in (TokenPool.Provider.GOOGLE, 'google'):
            from .antigravity_oauth import refresh_google_oauth_token
            from hive_platform.quota_fetchers import fetch_google_usage

            payload = _json_or_refresh_token(raw)
            access = payload.get('access_token') or ''
            refresh_tok = payload.get('refresh_token') or ''

            expiry_date = payload.get('expiry_date')
            now_ms = timezone.now().timestamp() * 1000
            needs_refresh = False
            if expiry_date and (now_ms + 5 * 60 * 1000 >= expiry_date):
                needs_refresh = True
            elif not access and refresh_tok:
                needs_refresh = True

            if refresh_oauth and refresh_tok and needs_refresh:
                try:
                    refreshed = refresh_google_oauth_token(refresh_tok)
                    new_access = refreshed.get('access_token')
                    if new_access:
                        access = new_access
                        payload['access_token'] = new_access
                        if refreshed.get('id_token'):
                            payload['id_token'] = refreshed['id_token']
                        if refreshed.get('expires_in'):
                            payload['expiry_date'] = int(
                                (timezone.now() + timedelta(seconds=int(refreshed['expires_in']))).timestamp() * 1000
                            )
                        entry.set_token(json.dumps(payload, sort_keys=True))
                        entry.save()
                except Exception as exc:
                    logger.warning('[token-coordinator] google oauth refresh failed entry=%s: %s', entry.id, exc)

            snap = fetch_google_usage(access)
            if snap.get('detail') in ('auth_failed_http_401', 'auth_failed_http_403') and refresh_oauth and refresh_tok:
                try:
                    refreshed = refresh_google_oauth_token(refresh_tok)
                    new_access = refreshed.get('access_token')
                    if new_access:
                        access = new_access
                        payload['access_token'] = new_access
                        if refreshed.get('id_token'):
                            payload['id_token'] = refreshed['id_token']
                        if refreshed.get('expires_in'):
                            payload['expiry_date'] = int(
                                (timezone.now() + timedelta(seconds=int(refreshed['expires_in']))).timestamp() * 1000
                            )
                        entry.set_token(json.dumps(payload, sort_keys=True))
                        entry.save()
                        snap = fetch_google_usage(access)
                except Exception as exc:
                    logger.warning('[token-coordinator] google oauth forced retry refresh failed entry=%s: %s', entry.id, exc)

            if snap.get('confidence') in {'exact', 'partial'}:
                _report('ok')
            elif snap.get('detail') in ('auth_failed_http_401', 'auth_failed_http_403'):
                _report('auth_failed')
        else:
            snap = {
                'source': provider_source,
                'fetched_at': quota_now.isoformat(),
                'confidence': 'partial',
                'plan': None,
                'windows': [],
                'detail': 'unsupported_provider',
            }
    except OAuthRefreshError as exc:
        snap = {
            'source': provider_source,
            'fetched_at': quota_now.isoformat(),
            'confidence': 'error',
            'plan': None,
            'windows': [],
            'detail': f'oauth_refresh_failed:{type(exc).__name__}',
        }
        if isinstance(exc, OpenAIOAuthRefreshError):
            apply_openai_refresh_failure(
                entry,
                exc,
                now=now,
                mutate_lease_state=mutate_lease_state,
            )
        elif isinstance(exc, (XaiOAuthRefreshError, AnthropicOAuthRefreshError)):
            generation_fingerprint = str(
                getattr(exc, 'credential_fingerprint', '')
                or generation_fingerprint
            )
            failure = apply_xai_refresh_failure(
                entry,
                exc,
                now=now,
                mutate_lease_state=mutate_lease_state,
            )
            if failure == 'generation_changed':
                summary.update({
                    'checked': True,
                    'updated': False,
                    'detail': 'credential_generation_changed',
                })
                return summary
        else:
            _report('rate_limited', retry_after=CODEX_REFRESH_COOLDOWN_ON_FAILURE)
    except Exception as exc:
        logger.warning('subscription quota refresh failed entry=%s: %s', entry.id, exc)
        snap = {
            'source': provider_source,
            'fetched_at': quota_now.isoformat(),
            'confidence': 'error',
            'plan': None,
            'windows': [],
            'detail': f'quota_refresh_failed:{type(exc).__name__}',
        }

    persisted = persist_quota_snapshot(
        entry,
        snap or {},
        expected_fingerprint=(
            generation_fingerprint
            if provider in (TokenPool.Provider.XAI, TokenPool.Provider.ANTHROPIC)
            else ''
        ),
    )
    if not persisted:
        summary.update({
            'checked': True,
            'updated': False,
            'detail': 'credential_generation_changed',
        })
        return summary
    if provider in (TokenPool.Provider.XAI, TokenPool.Provider.ANTHROPIC):
        entry.refresh_from_db()
    snap = entry.quota_snapshot
    summary.update({
        'checked': True,
        'updated': True,
        'detail': (snap or {}).get('detail') or '',
        'confidence': (snap or {}).get('confidence') or '',
        'windows': len((snap or {}).get('windows') or []),
    })
    return summary


def refresh_subscription_quota_snapshots(
    *,
    provider=None,
    force: bool = False,
    dry_run: bool = False,
    stale_after_seconds: int = 30 * 60,
    max_entries: int | None = None,
) -> dict:
    """Refresh stale quota snapshots for subscription/token entries.

    Covers **active and inactive non-deleted** entries. The Subscriptions
    dashboard shows disabled / cooling rows too; previously only ``active=True``
    was polled, so a cooled or operator-disabled account stayed "stale" until a
    human clicked Check now. Tombstones are audit-only and have no decryptable
    token material, so they must never enter quota refresh. Operator-disabled
    rows (``active=False`` with no recovery cooldown stamp) get snapshot-only
    updates — never auto-re-enabled. Entries that still carry a recovery/cool
    window keep lease-state mutation so a recovered usage signal can clear the
    cool the same way Check now does.
    """
    from hive_platform.models import TokenEntry

    now = timezone.now()
    quota_now = _quota_now()
    # Include inactive rows — the dashboard lists them and operators expect the
    # background loop to keep their quota chip current without Check-now.
    qs = TokenEntry.objects.filter(deleted_at__isnull=True).select_related('pool').order_by(
        'pool__provider', 'last_checked_at', 'created_at',
    )
    if provider:
        qs = qs.filter(pool__provider=str(provider))

    summary = {
        'quota_checked': 0,
        'quota_updated': 0,
        'quota_skipped_fresh': 0,
        'quota_failed': 0,
        'quota_errors': [],
    }
    processed = 0
    for entry in qs:
        if max_entries is not None and processed >= max_entries:
            break
        if not force and not _quota_snapshot_needs_refresh(
            entry, now=quota_now, stale_after_seconds=stale_after_seconds,
        ):
            summary['quota_skipped_fresh'] += 1
            continue
        processed += 1
        summary['quota_checked'] += 1
        if dry_run:
            summary['quota_updated'] += 1
            continue
        # Operator-disabled: active=False AND no recovery cool stamp. Snapshot
        # only — never flip active back on from a quiet background poll.
        # Anything with a cool window (rate-limit park, auth-fail recovery) or
        # currently active may still mutate lease state from usage signals.
        mutate_lease_state = bool(entry.active or entry.cooldown_until is not None)
        result = refresh_entry_quota_snapshot(
            entry,
            now=now,
            force=True,
            stale_after_seconds=stale_after_seconds,
            mutate_lease_state=mutate_lease_state,
        )
        if result.get('updated'):
            summary['quota_updated'] += 1
        detail = str(result.get('detail') or '')
        confidence = str(result.get('confidence') or '')
        if confidence == 'error' or detail.startswith(('token_decrypt_failed', 'oauth_refresh_failed', 'quota_refresh_failed')):
            summary['quota_failed'] += 1
            summary['quota_errors'].append(f'{entry.id}:{detail}'[:240])
    return summary


def _try_acquire_leader_lock() -> bool:
    """Try to acquire the PostgreSQL session advisory lock for leader election.

    ``pg_try_advisory_xact_lock`` would be released at the end of the single
    autocommit statement, before the Python refresh pass runs. Use a
    session-level advisory lock and release it in ``run_codex_refresh_loop``'s
    ``finally`` block so only one Hive pod can execute the critical section.
    """
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [CODEX_REFRESH_LEADER_LOCK_ID])
        (got_lock,) = cursor.fetchone()
        return bool(got_lock)


def _release_leader_lock() -> bool:
    """Release the PostgreSQL session advisory lock on the current connection."""
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_unlock(%s)", [CODEX_REFRESH_LEADER_LOCK_ID])
        (released,) = cursor.fetchone()
        return bool(released)


def run_codex_refresh_loop(
    *,
    dry_run: bool = False,
    refresh_quotas: bool = False,
    quota_stale_after_seconds: int = 30 * 60,
) -> dict:
    """HA background refresh loop for subscription OAuth credentials.

    Uses a PostgreSQL session advisory lock (``pg_advisory_lock`` family) for
    leader election — only one Hive pod holds the lock throughout the refresh
    pass. Non-leader pods return immediately with ``leader_held=False``.

    Records liveness metrics via ``codex_refresh_metrics`` (PLAT-1254).

    When ``refresh_quotas`` is true, the same HA leader pass also refreshes
    stale subscription quota snapshots for all TokenPool providers. That makes
    the Credential Health / Subscriptions dashboard continuously useful instead
    of depending on manual Check-now clicks.

    Returns a summary dict with counts of refreshed/skipped/failed entries.
    """
    from django.utils import timezone

    from hive_platform import codex_refresh_metrics as _metrics

    summary: dict = {
        'leader_held': False,
        'checked': 0,
        'refreshed': 0,
        'skipped_fresh': 0,
        'failed': 0,
        'errors': [],
        'quota_checked': 0,
        'quota_updated': 0,
        'quota_skipped_fresh': 0,
        'quota_failed': 0,
        'quota_errors': [],
        'xai_cooldowns_checked': 0,
        'xai_cooldowns_reasserted': 0,
        'xai_refresh_checked': 0,
        'xai_refreshed': 0,
        'xai_refresh_failed': 0,
        'xai_refresh_errors': [],
    }

    # ── Leader election via session-level pg_advisory_lock ──
    if not _try_acquire_leader_lock():
        return summary  # another pod is the leader

    summary['leader_held'] = True
    try:
        now = timezone.now()

        # Reconcile before any recovery/refresh writers run.  This is local DB
        # state repair only: it never calls xAI and therefore cannot waste WAN
        # while the authoritative snapshot says the account has zero credit.
        xai_cooldowns = reassert_confirmed_xai_credit_cooldowns(
            now=now,
            dry_run=dry_run,
        )
        summary['xai_cooldowns_checked'] = xai_cooldowns['checked']
        summary['xai_cooldowns_reasserted'] = xai_cooldowns['reasserted']

        for entry, payload in _openai_entries_needing_refresh(now):
            summary['checked'] += 1
            refresh_token = payload.get('refresh_token', '')
            if not refresh_token:
                summary['skipped_fresh'] += 1
                continue

            if dry_run:
                summary['refreshed'] += 1
                continue

            try:
                locked, payload, did_refresh = _refresh_openai_entry_payload(entry)
                if not did_refresh:
                    summary['skipped_fresh'] += 1
                    continue
                # Clear any cooldown — a successful refresh proves the token is live
                locked.cooldown_until = None
                locked.last_status = locked.LastStatus.OK
                locked.save(update_fields=['cooldown_until', 'last_status', 'updated_at'])
                summary['refreshed'] += 1
            except OpenAIOAuthRefreshError as exc:
                summary['failed'] += 1
                summary['errors'].append(str(exc))
                apply_openai_refresh_failure(entry, exc, now=now)

        # Keep xAI OAuth material fresh independently of quota polling.  The
        # per-entry row lock inside _refresh_xai_entry_payload serializes this
        # leader with broker requests and operator Check-now calls, while the
        # leader lock prevents duplicate background passes.
        for entry, payload in _xai_entries_needing_refresh(now):
            summary['checked'] += 1
            summary['xai_refresh_checked'] += 1
            if dry_run:
                summary['refreshed'] += 1
                summary['xai_refreshed'] += 1
                continue
            try:
                locked, payload, did_refresh = _refresh_xai_entry_payload(
                    entry,
                    refresh_buffer_seconds=XAI_REFRESH_PRE_EXPIRY_SECONDS,
                )
                if not did_refresh:
                    summary['skipped_fresh'] += 1
                    continue

                # OAuth success must not erase independent provider-confirmed
                # zero-credit evidence.  This generation-guarded status pass
                # preserves the quota cooldown when present, while refusing to
                # touch a newer device-auth generation.
                apply_status_report(
                    locked,
                    'ok',
                    now=now,
                    expected_fingerprint=str(locked.token_fingerprint or ''),
                )
                summary['refreshed'] += 1
                summary['xai_refreshed'] += 1
            except XaiOAuthRefreshError as exc:
                summary['failed'] += 1
                summary['xai_refresh_failed'] += 1
                error = str(exc)[:240]
                summary['errors'].append(error)
                summary['xai_refresh_errors'].append(error)
                apply_xai_refresh_failure(entry, exc, now=now)

        # PLAT-4122-adjacent (2026-07-10): the same leader pass also revives
        # auth-killed entries whose recovery backoff lapsed. Recovery used to
        # run only inside run_health_check / serve-time pool-dry paths, so a
        # falsely-killed bundle could stay dead indefinitely when the pool was
        # otherwise healthy. probe_token_health is bundle-safe and network-free;
        # near-expiry bundles are revived optimistically and refreshed by the
        # dedicated background pass below, never by a broker request.
        try:
            recovery = recover_expired_dead_tokens(now=now)
            summary['recovered'] = recovery.get('recovered', 0)
        except Exception:
            logger.exception('dead-token recovery pass failed (non-fatal)')

        # Clear lingering rate-limit cooldowns (PLAT-924) on the same HA cadence.
        # coordinator_healthcheck is not always scheduled in-cluster; without this
        # the Subscriptions page shows "rate_limited" forever after cool lapses
        # until a human clicks Check now.
        try:
            stale_cool = recover_stale_cooldown_tokens(now=now)
            summary['cooldown_cleared'] = stale_cool.get('cleared', 0)
        except Exception:
            logger.exception('stale-cooldown recovery pass failed (non-fatal)')

        # Elapsed cool stamps: last_status can still say rate_limited long after
        # cooldown_until passed. Normalize so the board + pick_token share truth.
        try:
            elapsed = clear_elapsed_rate_limit_cooldowns(now=now)
            summary['elapsed_cooldown_cleared'] = elapsed.get('cleared', 0)
        except Exception:
            logger.exception('elapsed-cooldown clear pass failed (non-fatal)')

        # HIVE-606: the same HA leader pass proactively refreshes Anthropic/Claude
        # full-login bundles near expiry, so lease-time consumers don't depend on
        # the Subscriptions dashboard being open (quota-path/Check-now refresh only
        # fires on access). Refresh failures increment summary['failed'] → the
        # PLAT-1254 fail-loud _metrics.record('failed') below (shared observability).
        for entry, payload in _anthropic_entries_needing_refresh(now):
            summary['checked'] += 1
            if dry_run:
                summary['refreshed'] += 1
                continue
            try:
                _refresh_and_persist_claude_bundle(entry, payload)
            except AnthropicOAuthRefreshError as exc:
                summary['failed'] += 1
                summary['errors'].append(f'claude bundle refresh failed entry={entry.id}')
                apply_anthropic_refresh_failure(entry, exc, now=now)
            else:
                apply_status_report(
                    entry, 'ok', now=now, expected_fingerprint=entry.token_fingerprint,
                )
                summary['refreshed'] += 1

        if refresh_quotas:
            quota_summary = refresh_subscription_quota_snapshots(
                dry_run=dry_run,
                stale_after_seconds=quota_stale_after_seconds,
            )
            summary.update(quota_summary)

        # ── Record liveness metric (PLAT-1254) ──
        # A leader pass can partially succeed: one account refreshes while
        # another fails. Failure must still increment the fail-loud counter so
        # HiveCodexRefreshLoopFailures routes the partial outage to on-call.
        _metrics.record('leader_held')
        if summary['failed'] > 0:
            _metrics.record('failed')
        if summary['refreshed'] > 0:
            _metrics.record('refreshed')
        elif summary['failed'] == 0:
            _metrics.record('skipped_fresh')
    finally:
        _release_leader_lock()

    return summary


def redeem_entry_reset_credit(entry, *, credit_id: str = '') -> dict:
    """Operator-triggered: redeem ONE banked Codex rate-limit reset credit on
    this OpenAI TokenEntry (2026-07-14). Resolves the account's OAuth access
    token (refreshing if stale), redeems via the wham consume endpoint, then
    force-refreshes the quota snapshot so the UI reflects the reset. OpenAI
    only; raises no secrets. Returns {ok, detail, windows_reset, credit_id,
    snapshot_windows}."""
    from hive_platform.models import TokenPool
    if entry.pool.provider != TokenPool.Provider.OPENAI:
        return {'ok': False, 'detail': 'not_an_openai_entry'}
    try:
        raw = entry.get_token()
    except Exception as exc:
        return {'ok': False, 'detail': f'token_decrypt_failed:{type(exc).__name__}'}
    payload = _json_or_refresh_token(raw)
    if payload.get('refresh_token') and _openai_needs_refresh(payload):
        try:
            _, payload, _ = _refresh_openai_entry_payload(entry)
        except Exception:
            pass  # redeem may still work on the current access token
    account_id = str(payload.get('account_id') or '') or (
        openai_account_id(payload.get('access_token') or '') or '')

    # Prefer the first detailed credit when present. The detail endpoint is
    # best-effort, though, while wham/usage can still authoritatively report an
    # available count. Codex's own client omits credit_id in that case and lets
    # the consume endpoint select the applicable available credit.
    snap = entry.quota_snapshot or {}
    reset_credits = snap.get('reset_credits') or {}
    if not credit_id:
        for c in (reset_credits.get('credits') or []):
            if str(c.get('status') or '').lower() == 'available' and c.get('id'):
                credit_id = str(c['id'])
                break
    if not credit_id:
        try:
            available_count = int(reset_credits.get('available') or 0)
        except (TypeError, ValueError):
            available_count = 0
        if available_count <= 0:
            return {'ok': False, 'detail': 'no_available_reset_credit'}

    from hive_platform.quota_fetchers import redeem_codex_reset_credit
    res = redeem_codex_reset_credit(
        payload.get('access_token') or '', account_id=account_id, credit_id=credit_id)
    out = {'ok': bool(res.get('ok')), 'detail': res.get('detail'),
           'windows_reset': res.get('windows_reset'), 'credit_id': credit_id}
    if res.get('ok'):
        try:
            refresh_entry_quota_snapshot(entry, force=True)
            entry.refresh_from_db()
            out['snapshot_windows'] = [
                w.get('label') for w in (entry.quota_snapshot or {}).get('windows', [])]
        except Exception:
            pass
    return out


def check_entry_health(entry, *, clear_expired_cooldown: bool = True, force_refresh: bool = False) -> dict:
    """Operator-initiated health check for a single TokenEntry (dashboard "Check now").

    Unlike synthetic raw Bearer probes (unsafe for OAuth JSON / Claude OAT), this:
      - OpenAI/Codex: validates the current access token and refreshes only when
        stale, explicitly requested, or rejected with HTTP 401
      - xAI/Grok: same via refresh_xai_oauth
      - Anthropic API keys (non-OAT): bounded probe_token_health
      - Claude Code OAT: observational only (no raw probe — would false-kill)

    Always updates last_checked_at. Applies apply_status_report on definitive
    ok / rate_limited / auth_failed outcomes so cooldown clears when usage is back.

    Returns a redacted status dict safe for the UI.
    """
    from django.utils import timezone

    from hive_platform.models import TokenEntry, TokenPool

    now = timezone.now()
    provider = entry.pool.provider
    result = {
        'entry_id': str(entry.id),
        'label': entry.label,
        'provider': provider,
        'checked_at': now.isoformat(),
        'outcome': 'unknown',
        'detail': '',
        'cooldown_until': entry.cooldown_until.isoformat() if entry.cooldown_until else None,
        'cooldown_remaining_seconds': None,
        'last_status': entry.last_status,
        'active': entry.active,
        'usage_count': entry.usage_count or 0,
        'access_expires_at': None,
        'account_email': '',
        'probe_supported': True,
        'quota': getattr(entry, 'quota_snapshot', None) or {},
    }
    if entry.cooldown_until and entry.cooldown_until > now:
        result['cooldown_remaining_seconds'] = int((entry.cooldown_until - now).total_seconds())
    elif entry.cooldown_until and clear_expired_cooldown and entry.cooldown_until <= now:
        # Cooldown window elapsed but last_status still rate_limited — clear stamp
        # so the row is leaseable again; a live check below may re-cool if still bad.
        entry.cooldown_until = None
        entry.save(update_fields=['cooldown_until', 'updated_at'])
        result['detail'] = 'expired_cooldown_cleared'

    try:
        raw = entry.get_token()
    except Exception as exc:
        result['outcome'] = 'error'
        result['detail'] = f'token_decrypt_failed: {type(exc).__name__}'
        entry.last_checked_at = now
        entry.save(update_fields=['last_checked_at', 'updated_at'])
        return result

    if provider == TokenPool.Provider.OPENAI:
        payload = _json_or_refresh_token(raw)
        result['account_email'] = str(payload.get('email') or '')
        access = payload.get('access_token') or ''
        if access:
            try:
                from .codex_refresh_metrics import access_expires_at
                exp = access_expires_at(payload)
                result['access_expires_at'] = exp.isoformat() if exp else None
            except Exception:
                pass
        refresh = payload.get('refresh_token') or ''
        if not refresh:
            result['outcome'] = 'error'
            result['detail'] = 'missing_refresh_token — use Re-auth'
            result['probe_supported'] = False
        else:
            try:
                # A known auth-failed row must prove its refresh generation is
                # usable before it can be reactivated. Healthy entries first use
                # the current access token so repeated "Check now" clicks do not
                # rotate a single-use refresh token.
                must_prove_refresh = (
                    force_refresh
                    or (
                        entry.last_status == TokenEntry.LastStatus.AUTH_FAILED
                        and not entry.active
                    )
                )
                refreshed = False
                if must_prove_refresh or _openai_needs_refresh(payload):
                    _, payload, refreshed = _refresh_openai_entry_payload(
                        entry,
                        force=must_prove_refresh,
                    )
                try:
                    from .codex_refresh_metrics import access_expires_at
                    exp = access_expires_at(payload)
                    result['access_expires_at'] = exp.isoformat() if exp else None
                except Exception:
                    pass
                # CodexBar path: ChatGPT wham/usage for 5h + weekly % remaining.
                try:
                    from hive_platform.quota_fetchers import fetch_codex_wham_usage, persist_quota_snapshot
                    acct = str(payload.get('account_id') or '')
                    if not acct:
                        try:
                            acct = openai_account_id(payload.get('access_token') or '') or ''
                        except Exception:
                            acct = ''
                    snap = fetch_codex_wham_usage(
                        payload.get('access_token') or '',
                        account_id=acct,
                    )
                    if snap.get('detail') == 'auth_failed_http_401' and not refreshed:
                        # The access token, not a timer or UI click, is the
                        # authority to rotate. Serialize one forced refresh and
                        # retry once before declaring the credential revoked.
                        _, payload, refreshed = _refresh_openai_entry_payload(
                            entry,
                            force=True,
                        )
                        acct = str(payload.get('account_id') or '') or (
                            openai_account_id(payload.get('access_token') or '') or ''
                        )
                        snap = fetch_codex_wham_usage(
                            payload.get('access_token') or '',
                            account_id=acct,
                        )
                    persist_quota_snapshot(entry, snap)
                    result['quota'] = snap
                    if snap.get('detail') == 'rate_limited':
                        apply_status_report(
                            entry, 'rate_limited',
                            retry_after=CODEX_REFRESH_COOLDOWN_ON_FAILURE,
                            now=now,
                        )
                        result['outcome'] = 'rate_limited'
                        result['detail'] = 'wham_usage_rate_limited'
                    elif snap.get('detail') == 'auth_failed_http_401':
                        apply_status_report(entry, 'auth_failed', now=now)
                        result['outcome'] = 'auth_failed'
                        result['detail'] = 'access_401_after_refresh — Re-auth required'
                    else:
                        apply_status_report(entry, 'ok', now=now)
                        result['outcome'] = 'ok'
                        result['detail'] = 'oauth_refresh_ok' if refreshed else 'token_ok'
                except OpenAIOAuthRefreshError:
                    raise
                except Exception as qexc:
                    logger.warning('codex quota fetch failed: %s', qexc)
                    result['outcome'] = 'unknown'
                    result['detail'] = f'quota_check_failed: {type(qexc).__name__}'
            except OpenAIOAuthRefreshError as exc:
                failure = apply_openai_refresh_failure(entry, exc, now=now)
                if failure == 'auth_failed':
                    result['outcome'] = 'auth_failed'
                    result['detail'] = (
                        f'refresh_http_{getattr(exc, "status_code", "auth")}'
                        '_revoked — Re-auth required'
                    )
                else:
                    result['outcome'] = 'rate_limited'
                    result['detail'] = f'refresh_failed: {exc}'[:240]
    elif provider == TokenPool.Provider.XAI:
        payload = _json_xai_oauth_token(raw)
        xai_generation = str(entry.token_fingerprint or '')
        result['account_email'] = str(payload.get('email') or payload.get('account_email') or '')
        refresh = payload.get('refresh_token') or ''
        if not refresh and not payload.get('access_token'):
            result['outcome'] = 'error'
            result['detail'] = 'missing_xai_credentials'
            result['probe_supported'] = False
        elif refresh and (force_refresh or _xai_needs_refresh(payload)):
            try:
                entry, payload, refreshed = _refresh_xai_entry_payload(
                    entry,
                    force=force_refresh,
                )
                xai_generation = str(entry.token_fingerprint or '')
                apply_status_report(
                    entry,
                    'ok',
                    now=now,
                    expected_fingerprint=xai_generation,
                )
                result['outcome'] = 'ok'
                result['detail'] = (
                    'xai_oauth_refresh_ok' if refreshed else 'xai_oauth_generation_reused'
                )
                try:
                    from hive_platform.quota_fetchers import fetch_grok_usage, persist_quota_snapshot
                    snap = fetch_grok_usage(payload.get('access_token') or '')
                    persist_quota_snapshot(
                        entry,
                        snap,
                        expected_fingerprint=xai_generation,
                    )
                    result['quota'] = snap
                except Exception as qexc:
                    logger.warning('grok quota fetch failed: %s', qexc)
            except OAuthRefreshError as exc:
                # HTTP 401/403 or OAuth ``invalid_grant`` means the stored
                # refresh grant is terminally dead (revoked / rotated away) and
                # the account MUST be re-authenticated. Mark it auth_failed so it
                # stops being retried every 5 minutes and stops showing a
                # misleading perpetual "cooling" state. Other 400s (bad client
                # config), transport failures, 429, and provider 5xx stay
                # transient cooldowns so a valid grant is never destroyed by a
                # service regression — mirroring apply_openai_refresh_failure.
                failure = apply_xai_refresh_failure(entry, exc, now=now)
                status_code = getattr(exc, 'status_code', None)
                error_code = getattr(exc, 'error_code', '')
                if failure == 'generation_changed':
                    result['outcome'] = 'ok'
                    result['detail'] = 'xai_oauth_generation_reused'
                elif failure == 'auth_failed':
                    result['outcome'] = 'auth_failed'
                    result['detail'] = (
                        'xai_refresh_invalid_grant'
                        if (status_code == 400 and error_code == 'invalid_grant')
                        else f'xai_refresh_{status_code}'
                    )
                else:
                    result['outcome'] = 'rate_limited'
                    result['detail'] = f'xai_refresh_failed: {exc}'[:240]
        else:
            # A freshly re-authenticated token can still have a stale quota
            # snapshot from the revoked generation.  The dashboard immediately
            # follows device auth with Check now, so observe the free billing
            # endpoint even when no OAuth refresh is needed.  Otherwise the stale
            # auth error keeps this healthy account out of broker routing until
            # the background quota refresh ages the snapshot out.
            try:
                from hive_platform.quota_fetchers import fetch_grok_usage, persist_quota_snapshot

                snap = fetch_grok_usage(payload.get('access_token') or '')
                persist_quota_snapshot(
                    entry,
                    snap,
                    expected_fingerprint=xai_generation,
                )
                result['quota'] = snap
            except Exception as qexc:
                logger.warning('grok quota fetch failed: %s', qexc)
            # Static / still-fresh access token — clear elapsed cool only; no paid chat probe.
            entry.refresh_from_db()
            if entry.cooldown_until and entry.cooldown_until <= now:
                apply_status_report(
                    entry,
                    'ok',
                    now=now,
                    expected_fingerprint=xai_generation,
                )
                result['outcome'] = 'ok'
                result['detail'] = 'xai_cooldown_cleared'
            elif entry.cooldown_until and entry.cooldown_until > now:
                result['outcome'] = 'cooling'
                result['detail'] = 'xai_still_cooling'
            else:
                result['outcome'] = 'ok'
                result['detail'] = 'xai_static_or_fresh'
    elif provider == TokenPool.Provider.ANTHROPIC:
        if isinstance(raw, str) and raw.startswith('sk-ant-oat01'):
            # No chat probe — but Claude Code OAuth usage endpoint IS safe (CodexBar path).
            result['probe_supported'] = True
            try:
                from hive_platform.quota_fetchers import fetch_claude_oauth_usage, persist_quota_snapshot
                # OAT may be raw token or JSON; usage endpoint wants Bearer access token.
                access = raw
                if raw.strip().startswith('{'):
                    try:
                        access = json.loads(raw).get('accessToken') or json.loads(raw).get('access_token') or raw
                    except Exception:
                        access = raw
                snap = fetch_claude_oauth_usage(access)
                persist_quota_snapshot(entry, snap)
                result['quota'] = snap
                if snap.get('confidence') == 'exact' and snap.get('windows'):
                    apply_status_report(entry, 'ok', now=now)
                    result['outcome'] = 'ok'
                    result['detail'] = 'claude_oauth_usage_ok'
                elif snap.get('detail') == 'auth_failed':
                    apply_status_report(entry, 'auth_failed', now=now)
                    result['outcome'] = 'auth_failed'
                    result['detail'] = 'claude_oauth_usage_auth_failed'
                elif snap.get('detail') == 'rate_limited_usage_endpoint':
                    # Usage endpoint itself rate-limited — do NOT kill the token.
                    result['outcome'] = 'cooling' if (entry.cooldown_until and entry.cooldown_until > now) else 'unknown'
                    result['detail'] = 'claude_usage_endpoint_429'
                else:
                    result['outcome'] = 'unknown'
                    result['detail'] = snap.get('detail') or 'claude_usage_partial'
            except Exception as qexc:
                logger.warning('claude quota fetch failed: %s', qexc)
                result['outcome'] = 'unknown'
                result['detail'] = f'claude_quota_error:{type(qexc).__name__}'
            if entry.cooldown_until and entry.cooldown_until > now and result['outcome'] == 'ok':
                pass  # ok already cleared cool via apply_status_report
        elif raw.strip().startswith('{') and _anthropic_oauth_payload(raw):
            # Full-login OAuth bundle: refresh-first (the authoritative health
            # signal), then fetch usage with the fresh access token. This
            # branch previously fell through to the raw Bearer probe, which
            # 401s on ANY bundle — "Check now" was killing freshly-authed
            # accounts (org-claude x2, 2026-07-10).
            payload = _anthropic_oauth_payload(raw)
            result['account_email'] = str(payload.get('email') or '')
            try:
                if force_refresh or _claude_needs_refresh(payload):
                    new_raw = _refresh_and_persist_claude_bundle(entry, payload, force=force_refresh)
                    if new_raw:
                        payload = _anthropic_oauth_payload(new_raw) or payload
                outcome = apply_status_report(
                    entry, 'ok', now=now, expected_fingerprint=entry.token_fingerprint,
                )
                result['outcome'] = outcome
                result['detail'] = (
                    'claude_bundle_refresh_ok' if outcome == 'ok' else 'claude_reauth_required'
                )
            except AnthropicOAuthRefreshError as exc:
                result['outcome'] = apply_anthropic_refresh_failure(entry, exc, now=now)
                result['detail'] = (
                    'claude_refresh_rejected — re-auth this account'
                    if result['outcome'] == 'auth_failed' else 'claude_refresh_temporarily_unavailable'
                )
            except Exception as exc:
                apply_status_report(
                    entry, 'rate_limited',
                    retry_after=CODEX_REFRESH_COOLDOWN_ON_FAILURE, now=now,
                )
                result['outcome'] = 'rate_limited'
                result['detail'] = f'claude_refresh_failed: {type(exc).__name__}'
            if result['outcome'] == 'ok':
                try:
                    from hive_platform.quota_fetchers import fetch_claude_oauth_usage, persist_quota_snapshot
                    access = str(payload.get('access_token') or payload.get('accessToken') or '')
                    snap = fetch_claude_oauth_usage(access)
                    persist_quota_snapshot(
                        entry, snap, expected_fingerprint=entry.token_fingerprint,
                    )
                    result['quota'] = snap
                except Exception as qexc:
                    logger.warning('claude bundle quota fetch failed: %s', qexc)
        else:
            probe = probe_token_health(entry)
            if probe in ('ok', 'rate_limited', 'auth_failed'):
                apply_status_report(entry, probe, now=now)
            result['outcome'] = probe
            result['detail'] = f'anthropic_api_probe:{probe}'
    elif provider in (TokenPool.Provider.GOOGLE, 'google'):
        payload = _json_or_refresh_token(raw)
        result['account_email'] = str(payload.get('email') or payload.get('account_email') or getattr(entry, 'account_email', ''))
        access = payload.get('access_token') or ''
        refresh_tok = payload.get('refresh_token') or ''
        if refresh_tok and (force_refresh or not access or _google_needs_refresh(payload)):
            try:
                from .antigravity_oauth import refresh_google_oauth_token
                refreshed = refresh_google_oauth_token(refresh_tok)
                new_access = refreshed.get('access_token')
                if new_access:
                    access = new_access
                    payload['access_token'] = new_access
                    if refreshed.get('id_token'):
                        payload['id_token'] = refreshed['id_token']
                    if refreshed.get('expires_in'):
                        payload['expiry_date'] = int(
                            (timezone.now() + timedelta(seconds=int(refreshed['expires_in']))).timestamp() * 1000
                        )
                    entry.set_token(json.dumps(payload, sort_keys=True))
                    entry.save()
            except Exception as exc:
                logger.warning('[check_entry_health] google refresh failed: %s', exc)

        try:
            from hive_platform.quota_fetchers import fetch_google_usage, persist_quota_snapshot
            snap = fetch_google_usage(access)
            persist_quota_snapshot(entry, snap)
            result['quota'] = snap
            if snap.get('confidence') in ('exact', 'partial'):
                apply_status_report(entry, 'ok', now=now)
                result['outcome'] = 'ok'
                result['detail'] = 'google_quota_ok'
            elif snap.get('detail') in ('auth_failed_http_401', 'auth_failed_http_403'):
                apply_status_report(entry, 'auth_failed', now=now)
                result['outcome'] = 'auth_failed'
                result['detail'] = 'google_auth_failed'
            else:
                apply_status_report(entry, 'ok', now=now)
                result['outcome'] = 'ok'
                result['detail'] = snap.get('detail') or 'google_usage_ok'
        except Exception as qexc:
            logger.warning('google quota fetch failed: %s', qexc)
            result['outcome'] = 'unknown'
            result['detail'] = f'google_quota_error:{type(qexc).__name__}'
    else:
        result['probe_supported'] = False
        result['outcome'] = 'unknown'
        result['detail'] = f'unsupported_provider:{provider}'

    entry.refresh_from_db()
    entry.last_checked_at = now
    entry.save(update_fields=['last_checked_at', 'updated_at'])
    result['last_status'] = entry.last_status
    result['active'] = entry.active
    result['cooldown_until'] = entry.cooldown_until.isoformat() if entry.cooldown_until else None
    if entry.cooldown_until and entry.cooldown_until > now:
        result['cooldown_remaining_seconds'] = int((entry.cooldown_until - now).total_seconds())
        if result['outcome'] == 'ok':
            pass
        elif result['outcome'] not in ('rate_limited', 'auth_failed', 'cooling'):
            result['outcome'] = 'cooling'
    else:
        result['cooldown_remaining_seconds'] = None
    return result


def build_credential_health_overview(*, owner_scope=None) -> dict:
    """Aggregate Claude / Codex / Grok pool health for the multi-provider dashboard.

    ``owner_scope`` is a TokenPool queryset (already permission-filtered).
    Provider usage *remaining* is not exposed by ChatGPT/Claude OAuth the same
    way — we surface operational health: leaseable, cooling (with ETA), revoked,
    access JWT expiry, live broker leases, last_status.
    """
    from django.utils import timezone

    from .codex_refresh_metrics import entry_observability
    from hive_platform.models import TokenEntry, TokenPool
    from hive_platform.quota_projection_v2 import project_quota_snapshot

    now = timezone.now()
    pools = owner_scope if owner_scope is not None else TokenPool.objects.all()
    entries = TokenEntry.objects.filter(deleted_at__isnull=True).annotate(
        live_leases=models.Count(
            'leases',
            filter=models.Q(leases__expires_at__gt=now),
        ),
        last_leased_at=models.Max('leases__issued_at'),
    )
    # Callers such as ``_allowed_token_pools`` already prefetch ``entries`` for
    # the pool-list response. Replace that generic prefetch here: Django raises
    # when the same relation is prefetched again with a different queryset.
    pools = pools.prefetch_related(None).prefetch_related(
        models.Prefetch('entries', queryset=entries),
    ).order_by('owner_kind', 'provider')

    by_provider: dict[str, list] = {}
    accounts = []
    counts = {
        'total': 0,
        'leaseable': 0,
        'cooling': 0,
        'revoked_or_disabled': 0,
        'unknown': 0,
    }

    for pool in pools:
        provider = pool.provider
        by_provider.setdefault(provider, [])
        for entry in pool.entries.all():
            counts['total'] += 1
            cool_rem = None
            if entry.cooldown_until and entry.cooldown_until > now:
                cool_rem = int((entry.cooldown_until - now).total_seconds())

            health = 'unknown'
            email = ''
            access_exp = None
            reauth_path = ''
            if provider == TokenPool.Provider.OPENAI:
                try:
                    info = entry_observability(entry, now=now)
                    health = info.get('status') or 'unknown'
                    email = info.get('email') or ''
                    access_exp = info.get('access_expires_at')
                    reauth_path = info.get('reauth_path') or ''
                except Exception:
                    health = 'unknown'
            else:
                if provider == TokenPool.Provider.XAI:
                    try:
                        payload = _json_xai_oauth_token(entry.get_token())
                        email = str(payload.get('email') or '')
                        from .codex_refresh_metrics import access_expires_at
                        expires_at = access_expires_at(payload)
                        access_exp = expires_at.isoformat() if expires_at else None
                    except Exception:
                        email = getattr(entry, 'account_email', '')
                if not entry.active:
                    health = 'disabled' if entry.last_status != TokenEntry.LastStatus.AUTH_FAILED else 'revoked'
                elif cool_rem is not None:
                    health = 'cooling'
                elif entry.last_status == TokenEntry.LastStatus.OK:
                    health = 'active'
                elif entry.last_status == TokenEntry.LastStatus.RATE_LIMITED:
                    # Cool window elapsed but last_status not yet rewritten — the
                    # entry is leaseable again (pick_token already ignores past
                    # cools). Surface as active so the board doesn't look stuck
                    # at "unknown" until a human Check-now.
                    health = 'active'
                elif entry.last_status == TokenEntry.LastStatus.AUTH_FAILED:
                    health = 'revoked'
                else:
                    health = 'unknown'

            # Owner policy is part of actual leaseability: every selector filters
            # agent_leasing_enabled=True before considering health/cooldown. Keep
            # personal reserves out of the green dashboard count even when their
            # provider credential is operationally healthy.
            if not entry.agent_leasing_enabled:
                counts['revoked_or_disabled'] += 1
            elif health in ('active',):
                counts['leaseable'] += 1
            elif health == 'cooling':
                counts['cooling'] += 1
            elif health in ('revoked', 'disabled', 'expired'):
                counts['revoked_or_disabled'] += 1
            else:
                counts['unknown'] += 1

            snap = project_quota_snapshot(getattr(entry, 'quota_snapshot', None))
            quota_status = quota_snapshot_status(snap)
            row = {
                'entry_id': str(entry.id),
                'pool_id': str(pool.id),
                'provider': provider,
                'provider_label': {
                    TokenPool.Provider.ANTHROPIC: 'Claude / Anthropic',
                    TokenPool.Provider.OPENAI: 'Codex / OpenAI',
                    TokenPool.Provider.XAI: 'Grok / xAI',
                    TokenPool.Provider.GOOGLE: 'Google Antigravity',
                }.get(provider, provider),
                'owner_kind': pool.owner_kind,
                'label': entry.label,
                'account_email': email or getattr(entry, 'account_email', ''),
                # Identify WHICH underlying credential a row maps to without
                # exposing the secret. token_hint is a first+last-chars mask
                # (e.g. sk-ant-o…XYZ789); token_fingerprint is a sha256 hex
                # used to correlate copies of the same token across pools.
                'token_hint': getattr(entry, 'token_hint', '') or '',
                'token_fingerprint': entry.token_fingerprint,
                'health': health,
                'active': entry.active,
                'last_status': entry.last_status,
                'cooldown_until': entry.cooldown_until.isoformat() if entry.cooldown_until else None,
                'cooldown_remaining_seconds': cool_rem,
                'access_expires_at': access_exp,
                'codex_reauth_url': reauth_path,
                'usage_count': entry.usage_count or 0,
                'live_leases': int(getattr(entry, 'live_leases', 0) or 0),
                'last_leased_at': (
                    entry.last_leased_at.isoformat()
                    if getattr(entry, 'last_leased_at', None)
                    else None
                ),
                'last_used_at': entry.last_used_at.isoformat() if entry.last_used_at else None,
                'last_checked_at': entry.last_checked_at.isoformat() if entry.last_checked_at else None,
                'tier': entry.tier or 'default',
                'priority': entry.priority,
                'quota': snap,
                'quota_status': quota_status,
            }
            by_provider[provider].append(row)
            accounts.append(row)

    return {
        # USE_TZ=False keeps model comparisons on the configured database
        # timezone, but API timestamps must identify their timezone explicitly.
        # The former naive America/Chicago value looked five hours stale beside
        # the UTC quota snapshots on the same operator page.
        'generated_at': _quota_now().isoformat(),
        'summary': counts,
        'note': (
            'Quota bars use the same sources as CodexBar: Claude GET /api/oauth/usage '
            '(5h + weekly per model family incl. Fable/Opus/Sonnet, when the token scope '
            'allows it — inference-only OATs show a dash), Codex GET '
            'chatgpt.com/backend-api/wham/usage (5h + weekly used% and next-reset time). '
            'xAI/Grok uses the SuperGrok billing-credit snapshot when the account '
            'exposes it; unavailable or unsupported accounts show a dash. '
            'Rows show a masked token hint (first+last chars) to identify the credential, '
            'plus Hive lease cool ETA and access-token expiry. Quotas auto-refresh on the '
            'health loop — no manual Check now needed.'
        ),
        'by_provider': by_provider,
        'accounts': accounts,
    }
