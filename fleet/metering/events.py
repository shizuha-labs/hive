"""Runtime-hours metering events from the fleet heartbeat (PLAT-7523, R2 Slice 2).

ADR-0032 contract: emit ``io.shizuha.usage.measured.v1`` CloudEvents for
per-agent/seat runtime consumption (SUM meter ``agent.runtime_seconds``),
source ``urn:shizuha:hive:runtime``, replay-safe by deterministic event id.

Each daemon heartbeat proves the seat was alive over
``[prev_last_active, now]`` — that half-open interval IS the runtime fact.
``enqueue_runtime_interval`` MUST be called inside the SAME
``transaction.atomic`` block as the ``last_active_at`` advance it describes
(producer transactional outbox, HLD §5): the fact is durable the moment the
heartbeat commits, and a Metering Core outage can never silently drop usage.

Billing subject is the server-derived free-v1 ``user:<owner_id>`` — the same
shape as R2 Slice 1 (the §12.3 BillingAccount migration replaces the producer
of this value, not the semantics). Owner-less agents (org/team fleet agents
with NULL ``owner_id``) are SKIPPED with a logged watermark gap — a payer is
never invented (HLD §2).
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.utils import timezone

from .models import FleetRuntimeOutbox, canonical_payload_sha256

logger = logging.getLogger(__name__)

# ADR-0032 §2 identity: the producer source URN for the Hive fleet runtime.
SOURCE_RUNTIME = 'urn:shizuha:hive:runtime'
# CloudEvents type for measured usage (R2 contract; one meter per event).
EVENT_TYPE_MEASURED = 'io.shizuha.usage.measured.v1'
DATASCHEMA = 'urn:shizuha:usage-event:1'

# R2 Slice 2 meter definition (immutable once effective — ADR-0032 §4).
METER_RUNTIME_SECONDS = 'agent.runtime_seconds'
METER_VERSION = 1
UNIT_SECONDS = 'second'

# A seat that stopped heartbeating gets its final partial interval credited up
# to this grace cap (one missed heartbeat cycle); runtime beyond the cap is a
# watermark gap — logged, never silently credited (no fabrication for dead
# seats). Open review item (a) from the implementation plan, proposal kept.
DEAD_SEAT_GRACE_SECONDS = 900


class RuntimeMeteringConflict(Exception):
    """Same (source, event_id) with a different canonical payload hash (409)."""

    def __init__(self, *, source: str, event_id: str,
                 existing_hash: str, attempted_hash: str):
        self.source = source
        self.event_id = event_id
        self.existing_hash = existing_hash
        self.attempted_hash = attempted_hash
        super().__init__(
            f'runtime metering conflict {source}/{event_id}: '
            f'{existing_hash[:12]} != {attempted_hash[:12]}'
        )


def runtime_event_id(agent_id: int, interval_start) -> str:
    """Deterministic, replay-safe event id: ``hive-runtime:{agent}:{epoch}``.

    The interval START (epoch seconds) keys the id, so a redelivered or
    re-derived fact for the same interval replays as the same identity, and a
    late event for an older interval never collides with a newer one.
    """
    start_epoch = int(interval_start.timestamp()) if hasattr(interval_start, 'timestamp') else int(interval_start)
    return f'hive-runtime:{int(agent_id)}:{start_epoch}'


def derive_interval(prev_active, now):
    """Half-open runtime interval ``[prev_active, now)``; None when empty.

    Returns None when the seat has no previous activity clock (first-ever
    heartbeat — nothing was proven alive before now) or when the advance is
    non-positive (clock skew clamp / duplicate frame).
    """
    if prev_active is None or now is None:
        return None
    if now <= prev_active:
        return None
    return (prev_active, now)


def billing_subject(*, owner_id: int | None) -> str | None:
    """Server-derived free-v1 subject; None for owner-less agents."""
    if owner_id is None:
        return None
    return f'user:{int(owner_id)}'


def _iso_utc(value) -> str:
    """RFC3339 UTC with an explicit offset (the core rejects naive times).

    Hive runs USE_TZ=False and stores naive-UTC clocks (fleet/consumers.py
    convention) — a naive value IS UTC, so it renders with the Z suffix.
    """
    iso = value.isoformat()
    if iso.endswith('+00:00'):
        return iso[:-6] + 'Z'
    if iso.endswith('Z'):
        return iso
    # naive → UTC by convention
    return iso + 'Z'


def build_envelope(*, event_id: str, subject: str, occurred_at,
                   interval_start, interval_end, quantity: Decimal,
                   agent_pk: int, agent_username: str = '',
                   authority: dict | None = None) -> dict:
    """Render one runtime fact as the exact wire contract the core validates.

    Mirrors R2 Slice 1's ``inference/metering/envelope.py``: the core's v1
    ingest reads ``data.meter`` / ``data.unit`` / ``data.quantity`` at the top
    level of ``data`` (one meter per event); quantity is a DECIMAL STRING,
    never a JSON number. The builder is DETERMINISTIC — the same outbox row
    always produces a byte-identical envelope, so at-least-once redelivery
    replays as the same payload hash (replay success), never a conflict.
    """
    data = {
        'meter': METER_RUNTIME_SECONDS,
        'meter_version': METER_VERSION,
        'quantity': str(quantity),
        'unit': UNIT_SECONDS,
        'interval': {'start': _iso_utc(interval_start), 'end': _iso_utc(interval_end)},
        'dimensions': {'product': 'hive', 'agent': int(agent_pk)},
        'quality': 'measured',
    }
    if agent_username:
        # Display snapshot only — never a billing key (HLD §2).
        data['metadata'] = {'agent_username': agent_username}
    envelope = {
        'specversion': '1.0',
        'id': event_id,
        'source': SOURCE_RUNTIME,
        'type': EVENT_TYPE_MEASURED,
        'subject': subject,
        'dataschema': DATASCHEMA,
        'datacontenttype': 'application/json',
        'time': _iso_utc(occurred_at),
        'data': data,
    }
    if authority:
        envelope['authority'] = dict(authority)
    return envelope


def enqueue_runtime_interval(*, agent, prev_active, now) -> FleetRuntimeOutbox | None:
    """Enqueue the runtime fact for ``[prev_active, now)`` in the caller's tx.

    MUST run inside ``transaction.atomic()`` (the heartbeat handler's block).
    Owner-less agents are skipped with a logged watermark gap (no invented
    payer). A same-identity/different-hash re-derive raises
    ``RuntimeMeteringConflict`` — the caller (heartbeat path) logs and
    swallows so metering can never break the heartbeat; the watermark gap
    covers the loss.
    """
    from django.db import transaction

    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError('enqueue_runtime_interval must run inside transaction.atomic().')

    interval = derive_interval(prev_active, now)
    if interval is None:
        return None
    interval_start, interval_end = interval

    subject = billing_subject(owner_id=getattr(agent, 'owner_id', None))
    if subject is None:
        logger.warning(
            'runtime metering: owner-less agent %s has no payer — watermark gap '
            'over [%s, %s) is logged, not credited',
            agent.pk, interval_start, interval_end,
        )
        return None

    event_id = runtime_event_id(agent.pk, interval_start)
    quantity = Decimal(str((interval_end - interval_start).total_seconds()))
    envelope = build_envelope(
        event_id=event_id,
        subject=subject,
        occurred_at=interval_end,
        interval_start=interval_start,
        interval_end=interval_end,
        quantity=quantity,
        agent_pk=agent.pk,
        agent_username=getattr(agent, 'agent_username', '') or '',
    )
    payload_hash = canonical_payload_sha256(envelope)
    try:
        row, created = FleetRuntimeOutbox.objects.get_or_create(
            source=SOURCE_RUNTIME,
            event_id=event_id,
            defaults={
                'event_type': EVENT_TYPE_MEASURED,
                'subject': subject,
                'occurred_at': interval_end,
                'interval_start': interval_start,
                'interval_end': interval_end,
                'quantity': quantity,
                'envelope': envelope,
                'payload_sha256': payload_hash,
            },
        )
    except Exception:
        # HIVE-408 discipline: a metering failure is a missed/at-risk billing
        # event — count it fail-loud before re-raising.
        from . import metrics
        metrics.record_emission_failure(SOURCE_RUNTIME)
        raise
    if not created:
        import hmac
        if not hmac.compare_digest(row.payload_sha256, payload_hash):
            raise RuntimeMeteringConflict(
                source=SOURCE_RUNTIME,
                event_id=event_id,
                existing_hash=row.payload_sha256,
                attempted_hash=payload_hash,
            )
    return row


# ── HIVE-2622 S4 deliverable 4: per-org credential-lease attribution ─────
# Composes with the stack-wide Metering Core boundary (ADR-0022/ADR-0032):
# Hive emits the org-attributed lease fact (the durable org→lease→agent
# binding the core joins Cortex's token facts against); it NEVER renders a
# product-local money ledger. The billing subject is the server-derived
# ``org:<organization_id>`` — the same free-v1 discipline as ``user:<id>``,
# sourced from the verified lease (which is sourced from the token owner
# scope), never from a client claim.

SOURCE_ORG_LEASE = 'urn:shizuha:hive:org-credential-lease'
METER_ORG_CREDENTIAL_LEASE = 'org.credential_lease'
UNIT_LEASE = 'lease'


def org_lease_subject(*, organization_id: int) -> str:
    """Server-derived free-v1 org billing subject (``org:<organization_id>``)."""
    return f'org:{int(organization_id)}'


def org_lease_event_id(grant_id: str) -> str:
    """Immutable identity — one attribution fact per lease grant (replay-safe)."""
    return f'org-lease:{grant_id}'


def build_org_lease_envelope(*, grant_id: str, lease_id: str, organization_id: int,
                             reference_id: int, cortex_provider_id: str,
                             provider: str, agent_identity: str,
                             interval_start, interval_end) -> dict:
    """Render the org-attributed lease fact (deterministic — byte-identical
    redelivery replays as the same payload hash, never a conflict)."""
    data = {
        'meter': METER_ORG_CREDENTIAL_LEASE,
        'meter_version': METER_VERSION,
        'quantity': '1',
        'unit': UNIT_LEASE,
        'interval': {'start': _iso_utc(interval_start), 'end': _iso_utc(interval_end)},
        'dimensions': {
            'product': 'hive',
            'organization': int(organization_id),
            'reference': int(reference_id),
            'cortex_provider': str(cortex_provider_id),
            'provider': str(provider),
            'agent': str(agent_identity or ''),
        },
        'quality': 'measured',
    }
    # Display snapshot only — never a billing key (HLD §2).
    data['metadata'] = {'lease_id': str(lease_id)}
    return {
        'specversion': '1.0',
        'id': org_lease_event_id(grant_id),
        'source': SOURCE_ORG_LEASE,
        'type': EVENT_TYPE_MEASURED,
        'subject': org_lease_subject(organization_id=organization_id),
        'dataschema': DATASCHEMA,
        'datacontenttype': 'application/json',
        'time': _iso_utc(interval_end),
        'data': data,
    }


def enqueue_org_lease_attribution(*, grant_id: str, lease_id: str, organization_id: int,
                                  reference_id: int, cortex_provider_id: str,
                                  provider: str, agent_identity: str,
                                  interval_start, interval_end) -> FleetRuntimeOutbox:
    """Enqueue the org-attributed lease fact in the caller's transaction.

    MUST run inside ``transaction.atomic()`` (the lease-issuance block): the
    attribution fact is durable the moment the lease commits — a Metering Core
    outage can never silently drop the org attribution. Same-identity /
    different-hash re-derive raises :class:`RuntimeMeteringConflict` (409
    discipline, never a rewrite).
    """
    from django.db import transaction

    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError('enqueue_org_lease_attribution must run inside transaction.atomic().')

    event_id = org_lease_event_id(grant_id)
    envelope = build_org_lease_envelope(
        grant_id=grant_id,
        lease_id=lease_id,
        organization_id=organization_id,
        reference_id=reference_id,
        cortex_provider_id=cortex_provider_id,
        provider=provider,
        agent_identity=agent_identity,
        interval_start=interval_start,
        interval_end=interval_end,
    )
    payload_hash = canonical_payload_sha256(envelope)
    row, created = FleetRuntimeOutbox.objects.get_or_create(
        source=SOURCE_ORG_LEASE,
        event_id=event_id,
        defaults={
            'event_type': EVENT_TYPE_MEASURED,
            'subject': envelope['subject'],
            'occurred_at': interval_end,
            'interval_start': interval_start,
            'interval_end': interval_end,
            'quantity': Decimal('1'),
            'envelope': envelope,
            'payload_sha256': payload_hash,
        },
    )
    if not created:
        import hmac
        if not hmac.compare_digest(row.payload_sha256, payload_hash):
            raise RuntimeMeteringConflict(
                source=SOURCE_ORG_LEASE,
                event_id=event_id,
                detail='org lease attribution re-derived with a different payload',
            )
    return row
