"""Runtime-hours occurrence events — the Hive leg's canonical meters.

Meter: ``agent.runtime_hours`` (unit ``hour``), a DELTA-style interval meter
summing agent runtime consumption per billing interval. The quantity is a
decimal string (the core rejects JSON numbers) with at most 6 decimal places.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Optional

from django.db import transaction
from django.utils import timezone

from .models import RuntimeMeterCheckpoint, RuntimeUsageOutbox

SOURCE_HIVE_FLEET = 'urn:shizuha:hive:fleet'
EVENT_TYPE_MEASURED = 'io.shizuha.usage.measured.v1'
METER_RUNTIME_HOURS = 'agent.runtime_hours'
UNIT_HOUR = 'hour'

# Emission cadence bound: a single event meters at most this many hours, so a
# long-stalled emitter catches up in bounded-size increments.
MAX_INTERVAL_HOURS = Decimal('1.0')
MIN_INTERVAL_SECONDS = 1


def _quantity_for_interval(seconds: float) -> str:
    hours = Decimal(str(seconds)) / Decimal('3600')
    # quantize to 6 dp (the core's decimal(24,6) bound), round down so a
    # partial second never rounds UP into billed quantity.
    return str(hours.quantize(Decimal('0.000001')))


def enqueue_runtime_hours(
    *,
    agent,
    subject: str,
    now=None,
) -> Optional[RuntimeUsageOutbox]:
    """Meter the agent's runtime since the checkpoint, transactionally.

    Advances the per-agent checkpoint and inserts the outbox row in ONE
    transaction (the telemetry checkpoint invariant). Returns the outbox row,
    or None when no meterable interval exists yet: the checkpoint seeds at
    the first observation with wall-clock time, so a first observation at the
    current time has no proven interval and emits nothing (runtime before
    the first checkpoint is unproven telemetry, never invented). An
    observation timestamped behind the checkpoint (late/backdated telemetry,
    inside the core's 72h late window) meters the bounded interval ENDING at
    its own timestamp; the deterministic event id makes a repeated late
    observation of the same interval replay as the SAME fact at the core,
    never a second one, and the checkpoint high-water never moves backward.
    """
    now = now or timezone.now()
    with transaction.atomic():
        checkpoint, created = RuntimeMeterCheckpoint.objects.select_for_update().get_or_create(
            agent=agent,
            defaults={'last_emitted_at': timezone.now()},
        )
        span_start = min(checkpoint.last_emitted_at, now)
        span_end = max(checkpoint.last_emitted_at, now)
        interval_start = span_start
        interval_end = min(
            span_end,
            span_start + timedelta(seconds=float(MAX_INTERVAL_HOURS * 3600)),
        )
        metered_seconds = (interval_end - interval_start).total_seconds()
        if metered_seconds < MIN_INTERVAL_SECONDS:
            return None
        # Deterministic event_id: the same (agent, interval) always maps to the
        # same id, so a transaction retry after a crash replays as the SAME
        # event (replay success at the core), never a second fact.
        event_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f'{SOURCE_HIVE_FLEET}:{agent.pk}:{interval_start.isoformat()}',
        )
        data = {
            'billing_account_id': subject,
            'meter': METER_RUNTIME_HOURS,
            'meter_version': 1,
            'unit': UNIT_HOUR,
            'quantity': _quantity_for_interval(metered_seconds),
            'interval': {
                'start': interval_start.isoformat(),
                'end': interval_end.isoformat(),
            },
            'dimensions': {
                'product': 'agent-runtime',
                'agent_username': agent.agent_username,
                'resource_uid': agent.pk and f'agent:{agent.pk}' or '',
            },
            'measurements': [{
                'meter': METER_RUNTIME_HOURS,
                'meter_version': 1,
                'quantity': _quantity_for_interval(metered_seconds),
                'unit': UNIT_HOUR,
                'interval': {
                    'start': interval_start.isoformat(),
                    'end': interval_end.isoformat(),
                },
                'dimensions': {
                    'product': 'agent-runtime',
                    'agent_username': agent.agent_username,
                },
            }],
        }
        row = RuntimeUsageOutbox.objects.create(
            source=SOURCE_HIVE_FLEET,
            event_id=event_id,
            event_type=EVENT_TYPE_MEASURED,
            subject=subject,
            resource_type='agent.runtime',
            resource_id=str(agent.pk),
            occurred_at=interval_end,
            interval_start=interval_start,
            interval_end=interval_end,
            data=data,
        )
        # High-water: advance only forward — a late/backdated observation must
        # never drag the checkpoint below an already-metered position.
        checkpoint.last_emitted_at = max(checkpoint.last_emitted_at, interval_end)
        checkpoint.save(update_fields=['last_emitted_at', 'updated_at'])
        return row
