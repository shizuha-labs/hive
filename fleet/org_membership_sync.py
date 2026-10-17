"""Keep Pulse roster placement compatible with Admin's org-tenancy boundary.

Pulse TeamRoster remains the exclusive source of truth for *team* membership.
Admin remains the source of truth for *organization* membership.  A fleet
identity placed into an org-scoped Pulse roster therefore also needs a plain
Admin organization membership; otherwise Connect correctly rejects same-org
agent DMs as ``cross_org_not_allowed``.

The placement API calls :func:`ensure_roster_org_membership` synchronously.
The on-demand reconciler calls :func:`reconcile_roster_org_memberships` as a
bounded repair backstop for roster edges created before this invariant existed.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import time
from typing import Any, Iterable
from urllib.parse import urlsplit
import uuid

import requests
from django.conf import settings


@dataclass(frozen=True)
class OrgMembershipResult:
    ok: bool
    status: str
    email: str
    organization_slug: str
    user_id: int | None = None
    organization_id: int | None = None
    detail: str = ''


def _service_headers() -> dict[str, str]:
    headers = {
        'Content-Type': 'application/json',
        'X-Internal-Service': 'shizuha-hive',
    }
    token = str(getattr(settings, 'ADMIN_INTERNAL_SERVICE_TOKEN', '') or '').strip()
    if token:
        headers['X-Internal-Service-Token'] = token
    return headers


def _id_base() -> str:
    return str(
        getattr(settings, 'SHIZUHA_ID_INTERNAL_URL', '')
        or 'http://shizuha-id.shizuha.svc.cluster.local:8001'
    ).rstrip('/')


def _admin_base() -> str:
    return str(
        getattr(settings, 'HIVE_ADMIN_API_URL', '')
        or 'http://shizuha-admin.shizuha.svc.cluster.local:8003/api'
    ).rstrip('/')


def _signed_membership_request(url: str, payload: dict) -> tuple[bytes, dict[str, str]]:
    """Bind the exact wire body and operation to Admin's Hive-only verifier."""
    key = str(getattr(settings, 'HIVE_SERVICE_SIGNING_KEY', '') or '').strip()
    if not key:
        raise ValueError('Hive membership signing key is not configured')
    body = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    body_sha = hashlib.sha256(body).hexdigest()
    signing_input = '\n'.join(('POST', urlsplit(url).path, timestamp, nonce, body_sha))
    return body, {
        'Content-Type': 'application/json',
        'X-Internal-Service': 'shizuha-hive',
        'X-Internal-Service-Token': key,
        'X-Hive-Timestamp': timestamp,
        'X-Hive-Nonce': nonce,
        'X-Hive-Body-Sha256': body_sha,
        'X-Hive-Signature': hmac.new(key.encode(), signing_input.encode(), hashlib.sha256).hexdigest(),
    }


def _json(response) -> dict[str, Any]:
    try:
        payload = response.json() or {}
    except ValueError:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _platform_agent_username(email: str) -> str:
    """Return the username encoded by a trusted platform-agent email alias."""
    localpart, separator, domain = str(email or '').strip().lower().rpartition('@')
    if not separator or domain not in {'shizuha.com', 'agents.shizuha.io'}:
        return ''
    return localpart


def ensure_roster_org_membership(
    *,
    email: str,
    organization_slug: str,
    display_name: str = '',
    timeout: float = 8,
) -> OrgMembershipResult:
    """Idempotently ensure one roster identity belongs to its organization.

    This deliberately creates no Admin TeamMembership.  Team placement remains
    Pulse-owned; this is only the tenant edge shared services such as Connect
    use for authorization.
    """
    normalized_email = str(email or '').strip().lower()
    normalized_org = str(organization_slug or '').strip().lower()
    if not normalized_email or not normalized_org:
        return OrgMembershipResult(
            False, 'invalid_input', normalized_email, normalized_org,
            detail='email and organization_slug are required',
        )

    headers = _service_headers()
    try:
        identity = requests.get(
            f'{_id_base()}/api/internal/users/by-email/',
            params={'email': normalized_email},
            headers=headers,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return OrgMembershipResult(
            False, 'identity_unavailable', normalized_email, normalized_org,
            detail=f'{exc.__class__.__name__}: {exc}',
        )
    identity_payload = _json(identity)
    if identity.status_code == 404:
        # Pulse's historic roster convention is ``username@shizuha.com`` while
        # some valid platform agents have canonical ID mail at
        # ``username@agents.shizuha.io``.  Resolve that trusted alias through
        # ID's agent-only username endpoint; never use this fallback for an
        # arbitrary customer domain or a human identity.
        alias_username = _platform_agent_username(normalized_email)
        if not alias_username:
            return OrgMembershipResult(
                False, 'identity_not_found', normalized_email, normalized_org,
                detail='Shizuha ID identity does not exist',
            )
        try:
            alias_identity = requests.get(
                f'{_id_base()}/api/internal/agents/',
                params={'username': alias_username},
                headers=headers,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            return OrgMembershipResult(
                False, 'identity_unavailable', normalized_email, normalized_org,
                detail=f'{exc.__class__.__name__}: {exc}',
            )
        alias_payload = _json(alias_identity)
        if (
            alias_identity.status_code == 200
            and alias_payload.get('exists') is True
            and alias_payload.get('account_type') == 'agent'
            and alias_payload.get('is_active') is not False
        ):
            identity_payload = {
                **alias_payload,
                'id': alias_payload.get('user_id'),
                'username': alias_payload.get('username') or alias_username,
            }
        elif alias_identity.status_code >= 400 and alias_identity.status_code != 404:
            return OrgMembershipResult(
                False, 'identity_rejected', normalized_email, normalized_org,
                detail=f'HTTP {alias_identity.status_code}',
            )
        else:
            return OrgMembershipResult(
                False, 'identity_not_found', normalized_email, normalized_org,
                detail='Shizuha ID agent identity does not exist',
            )
    if identity.status_code >= 400:
        # A 404 handled by the trusted agent-alias branch above is now a valid
        # identity payload.  Other HTTP failures remain fail-closed.
        if identity.status_code != 404:
            return OrgMembershipResult(
                False, 'identity_rejected', normalized_email, normalized_org,
                detail=f'HTTP {identity.status_code}',
            )
    try:
        user_id = int(identity_payload.get('id'))
    except (TypeError, ValueError):
        return OrgMembershipResult(
            False, 'identity_invalid', normalized_email, normalized_org,
            detail='Shizuha ID response omitted an integer id',
        )

    try:
        organization = requests.get(
            f'{_admin_base()}/internal/organizations/{normalized_org}/',
            headers=headers,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return OrgMembershipResult(
            False, 'admin_unavailable', normalized_email, normalized_org,
            user_id=user_id, detail=f'{exc.__class__.__name__}: {exc}',
        )
    if organization.status_code == 404:
        return OrgMembershipResult(
            False, 'organization_not_found', normalized_email, normalized_org,
            user_id=user_id, detail='Admin organization does not exist',
        )
    if organization.status_code >= 400:
        return OrgMembershipResult(
            False, 'admin_rejected', normalized_email, normalized_org,
            user_id=user_id, detail=f'HTTP {organization.status_code}',
        )
    organization_payload = _json(organization)
    try:
        organization_id = int(organization_payload.get('id'))
    except (TypeError, ValueError):
        return OrgMembershipResult(
            False, 'organization_invalid', normalized_email, normalized_org,
            user_id=user_id, detail='Admin response omitted an integer id',
        )

    membership_url = f'{_admin_base()}/internal/organizations/{organization_id}/members/'
    try:
        body, write_headers = _signed_membership_request(membership_url, {
            'user_id': user_id,
            'user_email': normalized_email,
            'display_name': str(display_name or identity_payload.get('username') or '').strip(),
            'role': 'member',
            # Stable provenance for this roster-owned tenant edge; each attempt
            # has a fresh signed nonce, while the operation remains idempotent.
            'operation_id': f'roster-org-membership:{organization_id}:{user_id}',
        })
    except ValueError:
        return OrgMembershipResult(
            False, 'membership_signing_unavailable', normalized_email, normalized_org,
            user_id=user_id, organization_id=organization_id,
            detail='Hive membership signing key is not configured',
        )
    try:
        membership = requests.post(
            membership_url,
            data=body,
            headers=write_headers,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return OrgMembershipResult(
            False, 'membership_unavailable', normalized_email, normalized_org,
            user_id=user_id, organization_id=organization_id,
            detail=f'{exc.__class__.__name__}: {exc}',
        )
    if membership.status_code >= 400:
        return OrgMembershipResult(
            False, 'membership_rejected', normalized_email, normalized_org,
            user_id=user_id, organization_id=organization_id,
            detail=f'HTTP {membership.status_code}',
        )
    membership_payload = _json(membership)
    if membership_payload.get('is_active') is False:
        return OrgMembershipResult(
            False, 'membership_inactive', normalized_email, normalized_org,
            user_id=user_id, organization_id=organization_id,
            detail='Admin retained an inactive membership; no reactivation attempted',
        )
    return OrgMembershipResult(
        True,
        str(membership_payload.get('status') or 'exists'),
        normalized_email,
        normalized_org,
        user_id=user_id,
        organization_id=organization_id,
    )


def _member_email(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get('email') or value.get('user_email') or ''
    return str(value or '').strip().lower()


def roster_org_edges(rosters: Iterable[dict[str, Any]]) -> list[tuple[str, str]]:
    edges = set()
    for roster in rosters or []:
        if not isinstance(roster, dict) or roster.get('is_active') is False:
            continue
        org = str(roster.get('organization_slug') or 'shizuha').strip().lower()
        for tier in roster.get('tiers') or []:
            for member in tier or []:
                email = _member_email(member)
                if email and org:
                    edges.add((org, email))
    return sorted(edges)


def _existing_org_member_emails(
    organization_slug: str,
    *,
    timeout: float = 8,
) -> tuple[set[str] | None, dict[str, str] | None]:
    """Fetch one org's membership set once for the whole repair sweep."""
    headers = _service_headers()
    try:
        organization = requests.get(
            f'{_admin_base()}/internal/organizations/{organization_slug}/',
            headers=headers,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return None, {'status': 'admin_unavailable', 'detail': f'{exc.__class__.__name__}: {exc}'}
    if organization.status_code >= 400:
        status = 'organization_not_found' if organization.status_code == 404 else 'admin_rejected'
        return None, {'status': status, 'detail': f'HTTP {organization.status_code}'}
    try:
        organization_id = int(_json(organization).get('id'))
    except (TypeError, ValueError):
        return None, {'status': 'organization_invalid', 'detail': 'Admin response omitted an integer id'}
    try:
        members = requests.get(
            f'{_admin_base()}/internal/organizations/{organization_id}/members/',
            headers=headers,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return None, {'status': 'members_unavailable', 'detail': f'{exc.__class__.__name__}: {exc}'}
    if members.status_code >= 400:
        return None, {'status': 'members_rejected', 'detail': f'HTTP {members.status_code}'}
    payload = _json(members)
    return {
        str(row.get('user_email') or '').strip().lower()
        for row in payload.get('members') or []
        if isinstance(row, dict) and str(row.get('user_email') or '').strip()
    }, None


def reconcile_roster_org_memberships(rosters: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Repair every resolvable org edge and return a bounded typed summary.

    Missing identities are reported separately from service failures: a stale
    roster email cannot be granted membership, but it must not prevent valid
    identities from self-healing in the same sweep.
    """
    edges = roster_org_edges(rosters)
    by_org: dict[str, list[str]] = {}
    for org, email in edges:
        by_org.setdefault(org, []).append(email)

    results = []
    existing_count = 0
    failures = []
    for org, emails in sorted(by_org.items()):
        existing, error = _existing_org_member_emails(org)
        if error:
            failures.append({
                'email': '*',
                'organization_slug': org,
                **error,
            })
            continue
        for email in emails:
            if email in existing:
                existing_count += 1
                continue
            results.append(
                ensure_roster_org_membership(email=email, organization_slug=org)
            )
    return {
        'checked': len(edges),
        'created': sum(r.ok and r.status == 'created' for r in results),
        'existing': existing_count + sum(r.ok and r.status != 'created' for r in results),
        'unresolved': [r.email for r in results if r.status == 'identity_not_found'],
        'failures': failures + [
            {
                'email': r.email,
                'organization_slug': r.organization_slug,
                'status': r.status,
                'detail': r.detail,
            }
            for r in results
            if not r.ok and r.status != 'identity_not_found'
        ],
    }
