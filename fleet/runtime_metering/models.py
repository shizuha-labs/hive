"""Durable outbox + telemetry checkpoint models for runtime-hours meters."""
from __future__ import annotations

import uuid

from django.db import models


class RuntimeMeterCheckpoint(models.Model):
    """Per-agent high-water for runtime-hours emission.

    ``last_emitted_at`` is the exclusive upper bound already metered. The
    emitter advances it IN THE SAME TRANSACTION as the outbox insert, so a
    crash between the two can never double-count an interval (the checkpoint
    rolls back with the outbox row) and never skips one silently (a live
    agent with a stale checkpoint meters from the checkpoint forward).
    """

    agent = models.OneToOneField(
        'fleet.FleetAgent', on_delete=models.CASCADE, related_name='runtime_meter_checkpoint',
    )
    last_emitted_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'runtime meter checkpoint'
        verbose_name_plural = 'runtime meter checkpoints'

    def __str__(self):
        return f'checkpoint {self.agent_id} @ {self.last_emitted_at.isoformat()}'


class RuntimeUsageOutbox(models.Model):
    """Transactional outbox row for one io.shizuha.usage.measured.v1 event.

    Rows are never deleted: DELIVERED rows are the durable local record of
    what the core has accepted; FAILED rows stay retryable with the recorded
    error for alerting (a remote outage can never silently drop usage).
    """

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        DELIVERED = 'delivered', 'Delivered'
        FAILED = 'failed', 'Failed'

    source = models.CharField(max_length=512)
    event_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    event_type = models.CharField(max_length=255, default='io.shizuha.usage.measured.v1')
    subject = models.CharField(max_length=512)  # billing_account_id (server-resolved)
    resource_type = models.CharField(max_length=64, default='agent.runtime')
    resource_id = models.CharField(max_length=128)
    occurred_at = models.DateTimeField()
    interval_start = models.DateTimeField()
    interval_end = models.DateTimeField()
    data = models.JSONField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['created_at', 'id']
        indexes = [models.Index(fields=['status', 'created_at'], name='runtime_outbox_status_idx')]

    def __str__(self):
        return f'runtime outbox {self.event_id} ({self.status})'
