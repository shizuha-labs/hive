"""xAI / Grok SuperGrok device-code auth — central ingestion into the HIVE Token Coordinator.

Browser-side port of the Goose / Grok-CLI OAuth device-code flow (RFC 8628) so an
operator can add or re-authenticate SuperGrok / Grok Build accounts from Hive
Token Pools — same UX as Codex device auth (HIVE-581), no host shell required.

The resulting OAuth material (access + rotating refresh token) is written as an
envelope-encrypted :class:`~hive_platform.models.TokenEntry` into the platform owner's
personal xAI :class:`~hive_platform.models.TokenPool`. The coordinator refreshes access tokens on
lease and Cortex leases them via ``hive_tokenpool_openai`` with
``hive_provider=xai``.

Flow (identical client_id/endpoints to Goose ``xai_oauth`` / Grok CLI):
  1. POST https://auth.x.ai/oauth2/device/code  {client_id, scope}
       -> device_code + user_code + verification_uri
  2. Human visits verification_uri and enters user_code
  3. POST https://auth.x.ai/oauth2/token  grant_type=device_code
       -> access_token + refresh_token (+ expires_in)
  4. Upsert TokenEntry (label ``org-xai-<email-or-sub>``) in the personal xAI pool
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone as dt_timezone

import requests
from django.db import transaction
from django.utils import timezone

from . import token_coordinator as tc

logger = logging.getLogger(__name__)

# Public Grok-CLI OAuth client (xAI rejects non-allowlisted desktop clients).
XAI_OAUTH_CLIENT_ID = 'b1a00492-073a-47ea-816f-4c329264a828'
DEVICE_CODE_URL = 'https://auth.x.ai/oauth2/device/code'
TOKEN_URL = 'https://auth.x.ai/oauth2/token'
DEFAULT_ISSUER = 'https://auth.x.ai'
DEVICE_CODE_GRANT_TYPE = 'urn:ietf:params:oauth:grant-type:device_code'
OAUTH_SCOPES = ' '.join([
    'openid',
    'profile',
    'email',
    'offline_access',
    'grok-cli:access',
    'api:access',
])
DEFAULT_POLL_INTERVAL_SECONDS = 5
DEFAULT_DEVICE_CODE_EXPIRES_SECONDS = 5 * 60


class GrokDeviceAuthError(RuntimeError):
    """Device-code auth failed (usercode request, poll, or ingest)."""


def request_device_code(*, timeout_seconds: int = 20) -> dict:
    """Step 1 — request a device code (RFC 8628).

    Returns ``{user_code, device_code, verification_url, interval, expires_in}``.
    ``device_auth_id`` is aliased to ``device_code`` for UI parity with Codex.
    """
    try:
        resp = requests.post(
            DEVICE_CODE_URL,
            headers={
                'Content-Type': 'application/x-www-form-urlencoded',
                'Accept': 'application/json',
            },
            data={
                'client_id': XAI_OAUTH_CLIENT_ID,
                'scope': OAUTH_SCOPES,
            },
            timeout=timeout_seconds,
        )
    except requests.RequestException as exc:
        raise GrokDeviceAuthError(f'device code request failed: {exc}') from exc
    if resp.status_code == 429:
        raise GrokDeviceAuthError('rate limited by xAI — wait a few minutes and retry')
    if resp.status_code >= 400:
        detail = (resp.text or '')[:200]
        raise GrokDeviceAuthError(
            f'device code request failed: HTTP {resp.status_code} {detail}'.strip()
        )
    data = resp.json() if resp.content else {}
    user_code = str(data.get('user_code') or '')
    device_code = str(data.get('device_code') or '')
    verification_url = str(
        data.get('verification_uri_complete')
        or data.get('verification_uri')
        or data.get('verification_url')
        or ''
    )
    if not user_code or not device_code:
        raise GrokDeviceAuthError(
            'invalid device code response (missing user_code/device_code)'
        )
    if not verification_url:
        verification_url = 'https://accounts.x.ai/device'
    interval = int(data.get('interval') or DEFAULT_POLL_INTERVAL_SECONDS)
    expires_in = int(data.get('expires_in') or DEFAULT_DEVICE_CODE_EXPIRES_SECONDS)
    return {
        'user_code': user_code,
        'device_code': device_code,
        # Codex UI field alias — frontend can pass either name to poll.
        'device_auth_id': device_code,
        'verification_url': verification_url,
        'interval': max(1, interval),
        'expires_in': max(60, expires_in),
    }


def poll_device_token(device_code: str, *, timeout_seconds: int = 20) -> dict:
    """Step 3 — poll once for authorization (RFC 8628 §3.5).

    Returns one of:
      ``{'status': 'pending'}``
      ``{'status': 'slow_down'}``
      ``{'status': 'authorized', ...tokens}``
    """
    code = str(device_code or '').strip()
    if not code:
        raise GrokDeviceAuthError('device_code is required')
    try:
        resp = requests.post(
            TOKEN_URL,
            headers={
                'Content-Type': 'application/x-www-form-urlencoded',
                'Accept': 'application/json',
            },
            data={
                'grant_type': DEVICE_CODE_GRANT_TYPE,
                'client_id': XAI_OAUTH_CLIENT_ID,
                'device_code': code,
            },
            timeout=timeout_seconds,
        )
    except requests.RequestException as exc:
        raise GrokDeviceAuthError(f'device token poll failed: {exc}') from exc

    if resp.status_code == 429:
        return {'status': 'slow_down'}

    data = {}
    try:
        if resp.content:
            data = resp.json()
    except ValueError:
        data = {}

    if resp.status_code >= 400:
        error = str(data.get('error') or '')
        if error in ('authorization_pending',):
            return {'status': 'pending'}
        if error == 'slow_down':
            return {'status': 'slow_down'}
        if error in ('access_denied', 'authorization_denied'):
            raise GrokDeviceAuthError('xAI device authorization was denied')
        if error == 'expired_token':
            raise GrokDeviceAuthError('xAI device code expired — start again')
        # Not yet authorized / transient non-ok — keep waiting (Codex parity).
        if not error or error in ('invalid_grant',):
            return {'status': 'pending'}
        detail = str(data.get('error_description') or error or resp.status_code)
        raise GrokDeviceAuthError(f'device auth error: {detail}')

    access = str(data.get('access_token') or '')
    if not access:
        return {'status': 'pending'}

    expires_in = data.get('expires_in')
    expires_at = ''
    if expires_in is not None:
        try:
            secs = int(expires_in)
            expires_at = (
                datetime.now(dt_timezone.utc) + timedelta(seconds=secs)
            ).isoformat()
        except (TypeError, ValueError):
            expires_in = None

    return {
        'status': 'authorized',
        'access_token': access,
        'refresh_token': str(data.get('refresh_token') or ''),
        'id_token': str(data.get('id_token') or ''),
        'expires_in': expires_in,
        'expires_at': expires_at,
        'token_type': str(data.get('token_type') or 'Bearer'),
    }


def resolve_account_identity(
    access_token: str,
    id_token: str = '',
    *,
    fallback_email: str = '',
) -> dict[str, str]:
    """Return xAI's immutable subject and best human email independently.

    xAI access tokens can expose only ``sub`` while the ID token carries the
    email.  Returning from the first token used to mistake that UUID for the
    account label and, more importantly, left re-auth keyed only by an editable
    TokenEntry label.  Read both tokens before deciding either field.
    """
    claims = [
        tc._jwt_payload(token)
        for token in (access_token, id_token)
        if token
    ]
    subjects = {
        str(payload.get('sub') or '').strip()
        for payload in claims
        if str(payload.get('sub') or '').strip()
    }
    if len(subjects) > 1:
        raise GrokDeviceAuthError('xAI access and ID token subjects do not match')
    account_id = next(iter(subjects), '')

    email = ''
    # Prefer the ID token for profile claims, but accept a normal email from
    # either token.  UUID-shaped values in an access-token `email` claim are
    # provider identities, not human email addresses.
    ordered_claims = list(reversed(claims))
    for payload in ordered_claims:
        for key in ('email', 'preferred_username'):
            candidate = str(payload.get(key) or '').strip()
            if '@' in candidate and '|' not in candidate:
                email = candidate
                break
        if email:
            break
    if not email:
        candidate = str(fallback_email or '').strip()
        if '@' in candidate and '|' not in candidate:
            email = candidate
    return {
        'account_id': account_id,
        'email': email,
        'label': email or account_id or 'unknown',
    }


def resolve_account_label(access_token: str, id_token: str = '') -> str:
    """Best-effort human email or immutable subject for display labels."""
    return resolve_account_identity(access_token, id_token)['label']


def _payload_account_identity(payload: dict) -> dict[str, str]:
    identity = resolve_account_identity(
        str(payload.get('access_token') or ''),
        str(payload.get('id_token') or ''),
        fallback_email=str(payload.get('email') or ''),
    )
    explicit_account_id = str(payload.get('account_id') or '').strip()
    if explicit_account_id and identity['account_id'] and (
        explicit_account_id != identity['account_id']
    ):
        raise GrokDeviceAuthError('stored xAI account_id does not match token subject')
    if explicit_account_id:
        identity['account_id'] = explicit_account_id
        if identity['label'] == 'unknown':
            identity['label'] = explicit_account_id
    return identity


def _entry_account_id(entry) -> str:
    if entry.deleted_at:
        return ''
    try:
        stored = json.loads(entry.get_token())
    except (TypeError, ValueError, json.JSONDecodeError):
        return ''
    if not isinstance(stored, dict):
        return ''
    return _payload_account_identity(stored)['account_id']


def ingest_grok_account(
    tokens: dict,
    *,
    actor: str = '',
    available_to_agents: bool = True,
) -> dict:
    """Upsert OAuth material by immutable xAI subject in the personal pool.

    The TokenPool row is locked so two device callbacks cannot create parallel
    entries for one account.  Existing operator-edited labels are deliberately
    preserved; identity comes from the signed-token subject, never the label.
    ``available_to_agents=False`` excludes the credential from new leases.
    """
    from hive_platform.models import TokenEntry, TokenLease, TokenPool

    access = str(tokens.get('access_token') or '')
    id_token = str(tokens.get('id_token') or '')
    identity = resolve_account_identity(
        access,
        id_token,
        fallback_email=str(tokens.get('email') or ''),
    )
    account_id = identity['account_id']
    email = identity['email']
    display_identity = identity['label']
    expires_at = str(tokens.get('expires_at') or '')
    expires_in = tokens.get('expires_in')
    if not expires_at and expires_in is not None:
        try:
            expires_at = (
                datetime.now(dt_timezone.utc) + timedelta(seconds=int(expires_in))
            ).isoformat()
        except (TypeError, ValueError):
            expires_at = ''

    payload = {
        'type': 'xai_oauth',
        'email': email or display_identity,
        'account_id': account_id,
        'access_token': access,
        'refresh_token': str(tokens.get('refresh_token') or ''),
        'id_token': id_token,
        'expires_at': expires_at,
        'oidc_issuer': DEFAULT_ISSUER,
        'oidc_client_id': XAI_OAUTH_CLIENT_ID,
        'token_endpoint': TOKEN_URL,
    }
    if expires_in is not None:
        try:
            payload['expires_in'] = int(expires_in)
        except (TypeError, ValueError):
            pass

    # Personal-ownership model (operator 2026-07-11): new credentials land in
    # the platform owner's PERSONAL pool (labels stay stable for re-auth dedup).
    from .token_coordinator import fleet_credentials_pool
    pool = fleet_credentials_pool(TokenPool.Provider.XAI)
    label = f'org-xai-{display_identity}'
    merged_entry_ids: list[str] = []
    with transaction.atomic():
        # Locking the parent serializes even the empty-pool/first-ingest case.
        pool = TokenPool.objects.select_for_update().get(pk=pool.pk)
        entries = list(
            TokenEntry.objects.select_for_update()
            .filter(pool=pool)
            .order_by('created_at', 'id')
        )
        identity_matches = [
            candidate
            for candidate in entries
            if account_id and _entry_account_id(candidate) == account_id
        ]
        label_matches = [candidate for candidate in entries if candidate.label == label]
        matches = identity_matches or label_matches
        entry = matches[0] if matches else None
        created = entry is None
        if entry is None:
            entry = TokenEntry(pool=pool, label=label, priority=100)

        # A prior label-keyed callback may already have created duplicates.
        # Retire them before installing the new payload because the newest
        # duplicate may have the same encrypted-payload fingerprint.
        from .token_coordinator import tombstone_token_entry
        for duplicate in identity_matches:
            if duplicate.pk == entry.pk or duplicate.deleted_at:
                continue
            TokenLease.objects.filter(
                entry=duplicate,
                expires_at__gt=timezone.now(),
            ).update(expires_at=timezone.now())
            tombstone_token_entry(
                duplicate,
                actor=actor or 'grok_device_auth_identity_merge',
            )
            merged_entry_ids.append(str(duplicate.id))

        entry.set_token(json.dumps(payload, sort_keys=True))
        entry.account_email = email or account_id
        entry.active = True
        entry.last_status = TokenEntry.LastStatus.OK
        entry.last_checked_at = timezone.now()
        entry.cooldown_until = None
        # Completing device auth is an unambiguous request to revive a retired
        # account. The old quota snapshot described the previous grant.
        entry.deleted_at = None
        entry.deleted_by = ''
        entry.quota_snapshot = {}
        entry.save()
        from .token_coordinator import set_agent_leasing_policy
        policy = set_agent_leasing_policy(
            entry,
            bool(available_to_agents),
            actor=actor,
            source='grok_device_auth',
        )
        entry = policy['entry']

    logger.info(
        '[grok-device-auth] ingested personal xai account email=%s entry=%s '
        'created=%s available_to_agents=%s actor=%s changed=%s '
        'revoked_lease_count=%s',
        email or account_id, entry.id, created, entry.agent_leasing_enabled, actor or '?',
        policy['changed'], policy['revoked_lease_count'],
    )
    return {
        'email': email or account_id,
        'account_id': account_id,
        'entry_id': str(entry.id),
        'created': created,
        'merged_entry_ids': merged_entry_ids,
        'available_to_agents': entry.agent_leasing_enabled,
    }
