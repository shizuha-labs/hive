"""PLAT-5275 nonce-bound Hive MCP scope projection producer.

Hive is the authority. The broker only relays these canonical Ed25519 envelopes;
the writable child/workspace never supplies services, audience, key id, lease,
or generation. A projection can be minted only from a current live Hive read and
its five-second lease expires even if every downstream process silently stalls.
"""
from __future__ import annotations

import base64
import hashlib
import re
from datetime import timedelta

from django.db import transaction
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.utils import timezone

from .capability_signing import stable_json
from .models import (
    AgentCapabilityOverride,
    AgentMcpProjectionState,
    CapabilityDefinition,
    FleetAgent,
    TeamRequiredCapability,
)

PROTOCOL_PREFIX = b'shizuha-hive-projection/v1'
SCHEMA_VERSION = 1
AUDIENCE = 'shizuha-mcp-multiplexer'
MAX_LIFETIME_MS = 5_000
NONCE_RE = re.compile(r'^[A-Za-z0-9_-]{43}$')
SERVICE_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,63}$')
AUTHORITY_CURRENT = 'current'
AUTHORITY_UNKNOWN = 'authority_unknown'


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode('ascii').rstrip('=')


def _unb64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def projection_fingerprint(services: list[str], catalog_version) -> str:
    canonical = stable_json({
        'catalog_version': catalog_version,
        'mcp_services': services,
    }).encode('utf-8')
    return hashlib.sha256(canonical).hexdigest()


def _projection_state(agent_id: str, fingerprint: str) -> AgentMcpProjectionState:
    """Return Hive's durable monotonic high-water for this exact projection.

    The row is locked while comparing fingerprints, so concurrent requests can
    never publish two generations for one change or move the high-water back.
    """
    with transaction.atomic():
        agent = FleetAgent.objects.select_for_update().get(id=agent_id)
        state, created = AgentMcpProjectionState.objects.select_for_update().get_or_create(
            agent=agent,
            defaults={
                'generation': 1,
                'fingerprint': fingerprint,
                'authority_state': AUTHORITY_UNKNOWN,
                'current_high_water': 1,
            },
        )
        if not created and state.fingerprint != fingerprint:
            state.generation += 1
            state.fingerprint = fingerprint
            # A new authority projection cannot reuse the old lease.  The next
            # authenticated daemon-link currentness heartbeat renews this exact
            # generation before Hive will sign it.
            state.current_high_water = state.generation
            state.authority_state = AUTHORITY_UNKNOWN
            state.lease_deadline = None
            state.save(update_fields=[
                'generation', 'fingerprint', 'current_high_water',
                'authority_state', 'lease_deadline', 'updated_at',
            ])
        elif not created and state.authority_state == AUTHORITY_UNKNOWN and state.lease_deadline is not None:
            # The endpoint has recomputed the exact current fingerprint after a
            # capability-mutation tombstone. Consume the tombstone, but do not
            # self-renew: one subsequent authenticated daemon heartbeat must
            # witness and renew this current generation before signing resumes.
            state.lease_deadline = None
            state.save(update_fields=['lease_deadline', 'updated_at'])
        return state


def renew_projection_authority_leases(agent_ids, *, now=None) -> int:
    """Renew currentness only from an authenticated in-order daemon frame.

    The caller supplies the FleetAgent primary keys owned by that authenticated
    daemon connection. Existing rows only are renewed: a projection request is
    responsible for materialising the first fingerprint/generation tuple, and
    no heartbeat is allowed to guess that security-sensitive value.
    """
    now = now or timezone.now()
    deadline = now + timedelta(milliseconds=MAX_LIFETIME_MS)
    rows = AgentMcpProjectionState.objects.filter(agent_id__in=agent_ids)
    renewed = 0
    with transaction.atomic():
        for state in rows.select_for_update():
            if state.generation != state.current_high_water:
                state.authority_state = AUTHORITY_UNKNOWN
                state.lease_deadline = None
            elif state.authority_state == AUTHORITY_UNKNOWN and state.lease_deadline is not None:
                # An explicit capability-mutation fence stays fenced until the
                # projection endpoint recomputes the new fingerprint/generation.
                # Routine expiry uses a past deadline and is renewable.
                state.lease_deadline = None
            elif state.authority_state == AUTHORITY_UNKNOWN and state.lease_deadline is None:
                state.authority_state = AUTHORITY_CURRENT
                state.lease_deadline = deadline
                renewed += 1
            else:
                state.authority_state = AUTHORITY_CURRENT
                state.lease_deadline = deadline
                renewed += 1
            state.save(update_fields=['authority_state', 'lease_deadline', 'updated_at'])
    return renewed


def fence_projection_authority_leases(agent_ids) -> int:
    """Fence an authenticated daemon's MCP authorities on disconnect/down."""
    return AgentMcpProjectionState.objects.filter(agent_id__in=agent_ids).update(
        authority_state=AUTHORITY_UNKNOWN,
        lease_deadline=None,
        updated_at=timezone.now(),
    )


def fence_all_projection_authority_leases() -> int:
    """Fence before a catalog/topology mutation can publish stale authority."""
    return AgentMcpProjectionState.objects.update(
        authority_state=AUTHORITY_UNKNOWN,
        # Retain a non-null tombstone to distinguish a capability-mutation fence
        # from ordinary lease expiry/disconnect. Heartbeats cannot clear it;
        # the endpoint must first observe and advance the new fingerprint.
        lease_deadline=timezone.now(),
        updated_at=timezone.now(),
    )


@receiver(post_save, sender=CapabilityDefinition)
@receiver(post_delete, sender=CapabilityDefinition)
@receiver(post_save, sender=TeamRequiredCapability)
@receiver(post_delete, sender=TeamRequiredCapability)
@receiver(post_save, sender=AgentCapabilityOverride)
@receiver(post_delete, sender=AgentCapabilityOverride)
def _fence_projection_authority_on_capability_mutation(**_kwargs):
    # The next authenticated daemon heartbeat can renew the new high-water, but
    # no envelope is signed in the mutation→reconcile gap. This is deliberately
    # broad; authorization revocation safety outranks a few seconds of early
    # expiry for unaffected agents.
    fence_all_projection_authority_leases()


def load_projection_signing_identity():
    # Lazy import avoids the existing capability-evidence -> effective-grant ->
    # fleet.views cycle. Both producers intentionally share the same daemon-held
    # Ed25519 identity, but neither may make module import order authoritative.
    from .capability_evidence import load_signing_identity
    return load_signing_identity()


def projection_service_identity(payload: dict):
    """Translate authoritative runtime refs into v1 logical upstream IDs.

    Runtime refs retain their tenant suffix for configuration and credentials.
    The multiplexer addresses logical catalog services; tenant routing and
    bearer authorization remain separate. Bind qualified refs into its opaque
    catalog revision so a tenant-only revocation still fences currentness.
    """
    from .runtime_fleet import get_platform_mcp_service_slugs

    refs = payload.get('enabled_mcp_servers')
    if not isinstance(refs, list) or not refs:
        raise ValueError('invalid MCP service projection')
    catalog = get_platform_mcp_service_slugs()
    services = set()
    for ref in refs:
        if not isinstance(ref, str):
            raise ValueError('invalid MCP runtime ref')
        parts = ref.split(':')
        if len(parts) not in (1, 2) or any(not SERVICE_RE.fullmatch(part) for part in parts):
            raise ValueError('invalid MCP runtime ref')
        if parts[0] not in catalog:
            raise ValueError('unknown MCP catalog service')
        services.add(parts[0])
    revision = payload.get('catalog_version', 0)
    if (not isinstance(revision, str) or not 0 < len(revision) <= 128) and (
        isinstance(revision, bool) or not isinstance(revision, int) or not 0 <= revision <= 9007199254740991
    ):
        raise ValueError('invalid MCP catalog revision')
    raw_refs = sorted(set(refs))
    if len(raw_refs) > 64:
        raise ValueError('invalid MCP service projection')
    if any(':' in ref for ref in raw_refs):
        revision = 'refs-v1:' + hashlib.sha256(stable_json({
            'catalog_version': revision, 'runtime_refs': raw_refs,
        }).encode('utf-8')).hexdigest()
    return sorted(services), revision


def canonical_projection_envelope(payload: dict, *, agent_id: str, agent_audience: str, nonce: str,
                                  now_ms: int | None = None) -> str:
    if not NONCE_RE.fullmatch(nonce or ''):
        raise ValueError('invalid projection nonce')
    nonce_raw = _unb64url(nonce)
    if len(nonce_raw) != 32:
        raise ValueError('invalid projection nonce')
    services, catalog_revision = projection_service_identity(payload)
    audience = str(agent_audience or '').strip()
    if not audience:
        raise ValueError('projection audience unavailable')
    fingerprint = projection_fingerprint(services, catalog_revision)
    state = _projection_state(agent_id, fingerprint)
    wall_now = timezone.now()
    if (
        state.authority_state != AUTHORITY_CURRENT
        or state.current_high_water != state.generation
        or state.lease_deadline is None
        or state.lease_deadline <= wall_now
    ):
        raise RuntimeError('MCP projection authority lease unavailable')
    lease_deadline = int(state.lease_deadline.timestamp() * 1_000)
    # The wall clock used for the signed issuance must describe the same instant
    # as the DB lease check. Tests may provide a deterministic now_ms, but it
    # cannot move issuance outside the currently authenticated lease.
    issued_at = int(now_ms if now_ms is not None else wall_now.timestamp() * 1_000)
    if now_ms is not None and abs(issued_at - int(wall_now.timestamp() * 1_000)) > MAX_LIFETIME_MS:
        raise ValueError('projection now_ms outside current authority lease')
    expires_at = min(issued_at + MAX_LIFETIME_MS, lease_deadline)
    if expires_at <= issued_at:
        raise RuntimeError('MCP projection authority lease expired')
    signing_identity = load_projection_signing_identity()
    claims = {
        'agent_audience': audience,
        'catalog_version': catalog_revision,
        'expires_at': expires_at,
        'fingerprint': fingerprint,
        'generation': state.generation,
        'issued_at': issued_at,
        'kid': signing_identity.kid,
        'lease_deadline': lease_deadline,
        'mcp_services': services,
        'schema_version': SCHEMA_VERSION,
        'source': 'hive',
    }
    canonical_claims = stable_json(claims).encode('utf-8')
    preimage = PROTOCOL_PREFIX + b'\x00' + nonce_raw + b'\x00' + canonical_claims
    signature = _b64url(signing_identity.private_key.sign(preimage))
    return stable_json({'nonce': nonce, 'projection': claims, 'signature': signature})
