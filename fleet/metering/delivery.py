"""Delivery dispatcher for the fleet runtime-metering outbox (PLAT-7523).

Drains PENDING/FAILED rows oldest-first through the neutral batch ingest.
DELIVERED on success; CONFLICT (terminal) when the core holds a DIFFERENT
payload under the same (source, event_id) — retrying the byte-identical
envelope would conflict forever; FAILED with exponential backoff on
transport errors. Envelopes rebuild byte-identically from the rows'
immutable columns, so redelivery replays by identity and never
double-counts.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from . import core_client
from .models import FleetRuntimeOutbox

logger = logging.getLogger(__name__)

BACKOFF_CAP_SECONDS = 300


def pending_rows(*, limit: int = 100):
    """Due rows oldest-first: PENDING/FAILED only (CONFLICT is terminal)."""
    return list(
        FleetRuntimeOutbox.objects
        .filter(
            status__in=(FleetRuntimeOutbox.State.PENDING, FleetRuntimeOutbox.State.FAILED),
            next_attempt_at__lte=timezone.now(),
        )
        .order_by('next_attempt_at', 'recorded_at')
        [:max(1, limit)]
    )


def dispatch_runtime_metering(*, limit: int = 100, dry_run: bool = False,
                              post=None) -> dict:
    """One dispatch pass. Returns a summary dict (also the command output)."""
    rows = pending_rows(limit=limit)
    summary = {'picked': len(rows), 'delivered': 0, 'conflict': 0,
               'failed': 0, 'deferred': 0, 'dry_run': dry_run}
    if dry_run:
        for row in rows:
            summary.setdefault('would_deliver', []).append(row.event_id)
        return summary
    if not rows:
        return summary

    # Batch in one call: the core's batch ingest is atomic per event and
    # returns per-event results, so one transport failure defers the whole
    # batch (rows stay durable) without double-counting delivered rows.
    envelopes = [row.envelope for row in rows]
    try:
        results = core_client.ingest_events(envelopes, post=post)
    except core_client.MeteringCoreUnavailable as exc:
        _mark_failed(rows, f'{type(exc).__name__}: {exc}'[:500])
        summary['failed'] = len(rows)
        return summary
    except core_client.MeteringCoreError as exc:
        # Whole-batch rejection (auth/shape): retryable only if transport-ish;
        # mark FAILED with backoff and surface the code.
        _mark_failed(rows, f'{exc.code}: {exc.detail}'[:500])
        summary['failed'] = len(rows)
        return summary

    if not isinstance(results, list) or len(results) != len(rows):
        # Ambiguous response — defer everything; at-least-once replay by
        # identity makes the next pass safe.
        _mark_failed(rows, 'ambiguous batch result: length mismatch')
        summary['failed'] = len(rows)
        return summary

    for row, result in zip(rows, results):
        status = str((result or {}).get('status') or '')
        with transaction.atomic():
            locked = FleetRuntimeOutbox.objects.select_for_update().get(pk=row.pk)
            if status in ('accepted', 'replayed'):
                locked.status = FleetRuntimeOutbox.State.DELIVERED
                locked.delivered_at = timezone.now()
                locked.last_error = ''
                summary['delivered'] += 1
            elif status == 'idempotency_conflict' or (result or {}).get('error') == 'idempotency_conflict':
                locked.status = FleetRuntimeOutbox.State.CONFLICT
                locked.last_error = str((result or {}).get('detail') or '')[:500]
                summary['conflict'] += 1
            else:
                locked.status = FleetRuntimeOutbox.State.FAILED
                locked.attempts += 1
                delay = min(2 ** min(locked.attempts, 8), BACKOFF_CAP_SECONDS)
                locked.next_attempt_at = timezone.now() + timedelta(seconds=delay)
                locked.last_error = str(result)[:500]
                summary['failed'] += 1
            locked.save(update_fields=[
                'status', 'attempts', 'next_attempt_at', 'last_error', 'delivered_at',
            ])
    return summary


def _mark_failed(rows, error: str) -> None:
    for row in rows:
        with transaction.atomic():
            locked = FleetRuntimeOutbox.objects.select_for_update().get(pk=row.pk)
            locked.status = FleetRuntimeOutbox.State.FAILED
            locked.attempts += 1
            delay = min(2 ** min(locked.attempts, 8), BACKOFF_CAP_SECONDS)
            locked.next_attempt_at = timezone.now() + timedelta(seconds=delay)
            locked.last_error = error
            locked.save(update_fields=[
                'status', 'attempts', 'next_attempt_at', 'last_error',
            ])
