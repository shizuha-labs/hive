"""Fleet runtime-metering outbox models (PLAT-7523, R2 Slice 2)."""
from __future__ import annotations

import hashlib
import json
import uuid

from django.db import models
from django.utils import timezone


def canonical_payload_sha256(payload) -> str:
    """Canonical JSON SHA-256 — the replay/conflict hash (ADR-0032 §3)."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        .encode('utf-8')
    ).hexdigest()


class FleetRuntimeOutbox(models.Model):
    """Transactional usage-event outbox for the fleet heartbeat runtime meter.

    PLAT-7523 (R2 Slice 2, ADR-0032): one row = one measured runtime fact for
    the half-open interval ``[interval_start, interval_end)``, enqueued in the
    SAME transaction as the ``last_active_at`` advance it describes (producer
    transactional outbox, at-least-once delivery). A Metering Core outage can
    never silently drop usage: the fact is durable the moment the heartbeat
    commits.

    Identity is immutable ``(source, event_id)`` with a canonical payload
    hash; same identity + same hash is a successful replay, same identity +
    different hash is a 409 idempotency conflict (never a rewrite). Delivery
    is at-least-once: PENDING/FAILED rows retry oldest-first with a
    byte-identical envelope; DELIVERED is terminal; CONFLICT is terminal and
    excluded from retry (surfaces for alerting).
    """

    class State(models.TextChoices):
        PENDING = 'pending', 'Pending'
        DELIVERED = 'delivered', 'Delivered'
        FAILED = 'failed', 'Failed'
        CONFLICT = 'conflict', 'Conflict'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.CharField(max_length=255, db_index=True)
    event_id = models.CharField(max_length=255)
    event_type = models.CharField(max_length=255)
    occurred_at = models.DateTimeField(db_index=True)
    recorded_at = models.DateTimeField(auto_now_add=True)

    subject = models.CharField(max_length=255)
    interval_start = models.DateTimeField()
    interval_end = models.DateTimeField()
    quantity = models.DecimalField(max_digits=24, decimal_places=6)

    envelope = models.JSONField()
    payload_sha256 = models.CharField(max_length=64)

    status = models.CharField(
        max_length=16, choices=State.choices, default=State.PENDING, db_index=True,
    )
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    last_error = models.TextField(blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = 'fleet'
        db_table = 'fleet_fleetruntimeoutbox'
        ordering = ['next_attempt_at', 'recorded_at']
        constraints = [
            models.UniqueConstraint(
                fields=['source', 'event_id'],
                name='uniq_fleet_runtime_outbox_source_id',
            ),
        ]
        indexes = [
            models.Index(
                fields=['status', 'next_attempt_at'],
                name='fleet_runtime_outbox_due_idx',
            ),
        ]

    def __str__(self):
        return f'FleetRuntimeOutbox({self.event_id}, {self.status})'
