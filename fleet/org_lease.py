"""HIVE-2622 S4 — org-scoped credential-lease grant machinery (BYO grant slice).

Extends the existing encrypted TokenPool / credential-grant machinery to the
org-scoped BYO chain so bridge/broker runtime resolution can consume an org's
OWN Cortex-managed credential, mirroring the coordinator TokenPool flow:

    agent → org → Cortex key (source of truth = Cortex)

Design contract (HIVE-314 epic scope item 2 + item 5; the encrypted-credential
machinery invariants):

- **Never plaintext org key material.** The org's real credential is
  server-side in Cortex; Hive holds opaque refs only (``HiveOrgProviderReference``
  stores identifiers/display state, never keys). What this module issues is a
  Hive-issued, TTL-bound **consumer token** whose claims pin the same-org
  reference — the runtime calls Cortex's router with it and Cortex's router
  enforces the BYO tenant-lock server-side (HLD Router Core v18 §5).
- **Fail-closed conjunction.** A grant issues only when the org's reference is
  same-org (resolve-time assertion, kumo refinement #4), servable (``active``),
  and provider-matched. Every other outcome is a typed
  :class:`OrgLeaseUnavailable` carrying the S3-lite reason enum — never a
  fallback onto Shizuha-owned keys (the HLD-forbidden fallback), never a
  fleet-wide paint (org-scoped failure surfaces only).
- **Per-agent fallback chains preserved.** This module never touches the
  personal/synthetic-org TokenPool scopes; the coordinator broker flow keeps
  its existing candidate order byte-for-byte (the runtime wiring slice decides
  sequencing at the call site, and a BYO org with an active reference never
  falls through to platform pools — HLD Router Core v18 §5).
- **Audit events on leasing transitions** (TokenPool runbook invariant): every
  issued grant writes a durable :class:`~fleet.models.HiveOrgCredentialLease`
  row (append-only accounting event: same-org reference, verified agent
  identity, runtime class, consumer-token FINGERPRINT — never the token) and
  emits loud counters keyed by reason, never by secret material.

Dormancy: the S3-lite resolver consumes this surface only when
``provider_resolution.LEASE_WIRING_COMPLETE`` flips True (the follow-up flip is
gated on end-to-end BYO comms verification). Until then the machinery is
callable but nothing routes on it.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.utils import timezone

from .models import HiveOrgProviderReference
from .provider_resolution import (
    REASON_IDENTITY_UNRESOLVED,
    REASON_INVARIANT_VIOLATION,
    REASON_LEASE_WIRING_PENDING,
    REASON_NO_PROVIDER_CONFIGURED,
    _incr,
    _label,
    _same_org_assert,
)

# Wire-protocol mapping: TokenPool.Provider (the coordinator vocabulary) → the
# reference ProviderType rows that can serve it. openai_compatible rows serve
# the OpenAI wire protocol (that is what "compatible" means), so provider
# 'openai' matches both openai and openai_compatible references.
_PROVIDER_TO_REFERENCE_TYPES = {
    'openai': {'openai', 'openai_compatible'},
    'anthropic': {'anthropic'},
}

_CONSUMER_TOKEN_AUDIENCE = 'cortex-router'
_CONSUMER_TOKEN_SCOPE = 'cortex:org-lease'


class OrgLeaseUnavailable(Exception):
    """Typed org-scoped lease failure — never a fallback, never fleet-wide.

    ``reason`` reuses the S3-lite reason enum so operators see one vocabulary
    across resolution and leasing; ``detail`` is redacted provenance (never a
    raw provider body, never secret material).
    """

    def __init__(self, reason: str, detail: str = '') -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


@dataclass(frozen=True)
class OrgLeaseGrant:
    """The lease handle a BYO runtime consumes. Never org key material."""

    grant_id: str
    lease_id: str
    organization_id: int
    reference_id: int
    cortex_provider_id: str
    provider: str
    model_id: str
    # The /v1 surface the runtime calls with the consumer token (Cortex router).
    router_base_url: str
    consumer_token: str
    expires_at: Any


def _consumer_token_fingerprint(token: str) -> str:
    """Redacted provenance for the durable lease row — sha256, never the token."""
    return 'sha256:' + hashlib.sha256(token.encode('utf-8')).hexdigest()[:64]


def _lease_ttl_seconds() -> int:
    return int(getattr(settings, 'HIVE_ORG_LEASE_TTL_SECONDS', 3600) or 3600)


def _router_base_url() -> str:
    return str(getattr(settings, 'CORTEX_PUBLIC_BASE_URL', '') or '').rstrip('/')


def _mint_consumer_token(
    *,
    grant_id: str,
    organization_id: int,
    reference_id: int,
    cortex_provider_id: str,
    provider: str,
    runtime_class: str,
    agent_identity: str,
    expires_at,
) -> str:
    """Hive-issued, TTL-bound consumer token for the Cortex router.

    Claims pin the same-org lease (organization_id + reference_id +
    cortex_provider_id + grant_id); the audience/scope mark it as an org-lease
    consumer so Cortex can refuse it for anything but the pinned tenant.
    Signed with Hive's existing JWT signing key (SHIZUHA_JWT_SIGNING_KEY via
    SIMPLE_JWT) — the same trust boundary the Coordinator already validates.
    """
    from rest_framework_simplejwt.tokens import AccessToken

    lifetime = max(60, int((expires_at - timezone.now()).total_seconds()))
    token = AccessToken()
    token.set_exp(lifetime=lifetime)
    token[_CONSUMER_TOKEN_SCOPE] = True
    token['aud'] = _CONSUMER_TOKEN_AUDIENCE
    token['grant_id'] = grant_id
    token['organization_id'] = int(organization_id)
    token['reference_id'] = int(reference_id)
    token['cortex_provider_id'] = str(cortex_provider_id)
    token['provider'] = str(provider)
    token['runtime_class'] = _label(runtime_class) or 'default'
    token['agent_identity'] = str(agent_identity or '')
    return str(token)


def issue_org_lease(
    *,
    organization_id: int,
    provider: str,
    runtime_class: str = '',
    agent_identity: str = '',
    reference: Any = None,
    now=None,
) -> OrgLeaseGrant:
    """Issue an org-scoped credential lease for one verified org/runtime.

    ``reference`` may be passed in (the caller already resolved the org's
    ``HiveOrgProviderReference``); otherwise the active same-org reference is
    loaded here. Raises :class:`OrgLeaseUnavailable` with a typed reason on
    every non-servable outcome — the caller surfaces that as an org-scoped
    Unavailable and keeps its per-agent fallback chain, never a platform-pool
    fallback for a BYO org (HLD Router Core v18 §5).
    """
    from django.db import transaction

    now = now or timezone.now()
    organization_id = int(organization_id)
    provider = str(provider or '').strip()

    if provider not in _PROVIDER_TO_REFERENCE_TYPES:
        raise OrgLeaseUnavailable(
            REASON_NO_PROVIDER_CONFIGURED,
            'provider %r is not a BYO-servable wire protocol' % (provider,),
        )

    if reference is None:
        reference = (
            HiveOrgProviderReference.objects
            .filter(org_id=organization_id, status=HiveOrgProviderReference.Status.ACTIVE)
            .order_by('id')
            .first()
        )
    if reference is None:
        _incr('org_lease_total', {'outcome': REASON_NO_PROVIDER_CONFIGURED})
        raise OrgLeaseUnavailable(
            REASON_NO_PROVIDER_CONFIGURED,
            'no active org provider reference for org %s' % (organization_id,),
        )

    ref_org = getattr(reference, 'org_id', None)
    if ref_org is None:
        _incr('org_lease_total', {'outcome': REASON_INVARIANT_VIOLATION})
        raise OrgLeaseUnavailable(
            REASON_INVARIANT_VIOLATION, 'provider reference row carries no org_id',
        )
    try:
        _same_org_assert(ref_org, organization_id)
    except Exception as exc:
        _incr('org_lease_total', {'outcome': REASON_INVARIANT_VIOLATION})
        raise OrgLeaseUnavailable(REASON_INVARIANT_VIOLATION, str(exc)) from exc

    ref_type = str(getattr(reference, 'provider_type', '') or '').strip().lower()
    if ref_type not in _PROVIDER_TO_REFERENCE_TYPES[provider]:
        _incr('org_lease_total', {'outcome': REASON_NO_PROVIDER_CONFIGURED})
        raise OrgLeaseUnavailable(
            REASON_NO_PROVIDER_CONFIGURED,
            'org reference type %r cannot serve wire protocol %r' % (ref_type, provider),
        )

    status = str(getattr(reference, 'status', '') or '').strip().lower()
    if status != 'active':
        _incr('org_lease_total', {'outcome': 'provider_lifecycle_pending'})
        raise OrgLeaseUnavailable(
            'provider_lifecycle_pending',
            'org reference status %r is not servable' % (status,),
        )

    model_id = ''
    by_runtime = getattr(reference, 'default_models_by_runtime', None) or {}
    if isinstance(by_runtime, dict):
        model_id = str(by_runtime.get(runtime_class) or by_runtime.get(_label(runtime_class)) or '')
    if not model_id:
        flat = getattr(reference, 'default_models', None) or []
        if isinstance(flat, (list, tuple)) and flat:
            model_id = str(flat[0])
    if not model_id:
        _incr('org_lease_total', {'outcome': REASON_NO_PROVIDER_CONFIGURED})
        raise OrgLeaseUnavailable(
            REASON_NO_PROVIDER_CONFIGURED,
            'org reference carries no default model for runtime %r' % (runtime_class,),
        )

    router_base_url = _router_base_url()
    if not router_base_url:
        _incr('org_lease_total', {'outcome': 'endpoint_unreachable'})
        raise OrgLeaseUnavailable(
            'endpoint_unreachable', 'CORTEX_PUBLIC_BASE_URL is not configured',
        )

    grant_id = str(uuid.uuid4())
    expires_at = now + timedelta(seconds=_lease_ttl_seconds())
    consumer_token = _mint_consumer_token(
        grant_id=grant_id,
        organization_id=organization_id,
        reference_id=reference.id,
        cortex_provider_id=reference.cortex_provider_id,
        provider=provider,
        runtime_class=runtime_class,
        agent_identity=agent_identity,
        expires_at=expires_at,
    )

    # One accounting event: the durable append-only lease row + the loud
    # counter. The row is the audit event of the leasing transition (actor =
    # verified agent identity, trigger = runtime class, reference pinned).
    # HIVE-2622 deliverable 4: the per-org usage attribution fact rides the
    # SAME transaction (producer transactional outbox, ADR-0032) — the org
    # attribution is durable the moment the lease commits.
    from .models import HiveOrgCredentialLease
    from .metering.events import enqueue_org_lease_attribution

    with transaction.atomic():
        lease = HiveOrgCredentialLease.objects.create(
            grant_id=grant_id,
            reference=reference,
            organization_id=organization_id,
            provider=provider,
            runtime_class=_label(runtime_class) or 'default',
            agent_identity=str(agent_identity or ''),
            consumer_token_fingerprint=_consumer_token_fingerprint(consumer_token),
            expires_at=expires_at,
        )
        enqueue_org_lease_attribution(
            grant_id=grant_id,
            lease_id=str(lease.id),
            organization_id=organization_id,
            reference_id=reference.id,
            cortex_provider_id=str(reference.cortex_provider_id),
            provider=provider,
            agent_identity=str(agent_identity or ''),
            interval_start=now,
            interval_end=expires_at,
        )
    _incr('org_lease_total', {'outcome': 'issued'})

    return OrgLeaseGrant(
        grant_id=grant_id,
        lease_id=str(lease.id),
        organization_id=organization_id,
        reference_id=reference.id,
        cortex_provider_id=str(reference.cortex_provider_id),
        provider=provider,
        model_id=model_id,
        router_base_url=router_base_url,
        consumer_token=consumer_token,
        expires_at=expires_at,
    )


__all__ = [
    'OrgLeaseGrant',
    'OrgLeaseUnavailable',
    'issue_org_lease',
    'REASON_LEASE_WIRING_PENDING',
    'REASON_IDENTITY_UNRESOLVED',
]


def org_provider_grant_handle(*, organization_slug: Any) -> dict | None:
    """Additive effective-capabilities entry: the org's BYO provider handle.

    HIVE-2622 deliverable 2 piece 2 — the signed effective-capabilities payload
    (PLAT-4000 HMAC discipline) carries the handle so the BYO runtime learns
    WHERE to lease (``POST /v1/org-provider-lease`` with its verified broker
    credential). The handle is redacted provenance only — identifiers, display
    state, and the router surface; NEVER org key material (the real credential
    is server-side in Cortex).

    Dormancy: the key is ABSENT entirely until
    ``provider_resolution.LEASE_WIRING_COMPLETE`` flips (deliverable 3) —
    absent = byte-for-byte today's payload. After the flip:
    - org has an active same-org reference → ``{'ok': True, ...handle}``;
    - the slug→org_id bridge itself fails/ambiguous (kumo refinement #5) →
      a LOUD typed hold ``{'ok': False, 'reason': 'identity_unresolved'}`` —
      never silently resolved as non-BYO;
    - genuinely non-BYO org (shape-1 miss) → ``None`` (key absent).
    """
    from .provider_resolution import (
        LEASE_WIRING_COMPLETE,
        IdentityUnresolved,
        ORG_SLUG_LOOKUP,
        resolve_org_id_from_slug,
    )

    if not LEASE_WIRING_COMPLETE:
        return None

    slug = str(organization_slug or '').strip()
    if not slug:
        return None
    try:
        organization_id = resolve_org_id_from_slug(slug, ORG_SLUG_LOOKUP)
    except IdentityUnresolved as exc:
        _incr('org_provider_grant_total', {'outcome': 'identity_unresolved'})
        return {'ok': False, 'reason': 'identity_unresolved', 'detail': exc.detail}
    if organization_id is None:
        return None  # shape-1 miss: genuinely non-BYO — key absent

    reference = (
        HiveOrgProviderReference.objects
        .filter(org_id=organization_id, status=HiveOrgProviderReference.Status.ACTIVE)
        .order_by('id')
        .first()
    )
    if reference is None:
        _incr('org_provider_grant_total', {'outcome': REASON_NO_PROVIDER_CONFIGURED})
        return None

    model_id = ''
    by_runtime = getattr(reference, 'default_models_by_runtime', None) or {}
    if isinstance(by_runtime, dict) and by_runtime:
        model_id = str(next(iter(by_runtime.values())))
    if not model_id:
        flat = getattr(reference, 'default_models', None) or []
        if isinstance(flat, (list, tuple)) and flat:
            model_id = str(flat[0])
    if not model_id:
        _incr('org_provider_grant_total', {'outcome': REASON_NO_PROVIDER_CONFIGURED})
        return None

    router_base_url = _router_base_url()
    if not router_base_url:
        _incr('org_provider_grant_total', {'outcome': 'endpoint_unreachable'})
        return None

    _incr('org_provider_grant_total', {'outcome': 'emitted'})
    return {
        'ok': True,
        'organization_id': int(organization_id),
        'reference_id': reference.id,
        'cortex_provider_id': str(reference.cortex_provider_id),
        'provider_type': str(getattr(reference, 'provider_type', '') or ''),
        'model_id': model_id,
        'router_base_url': router_base_url,
        'lease_endpoint': '/hive/api/v1/org-provider-lease/',
    }
