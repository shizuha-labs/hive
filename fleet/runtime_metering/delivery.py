"""At-least-once delivery of the runtime usage outbox to the Metering Core.

Retries PENDING rows oldest-first with the SAME (source, event_id) and a
byte-identical envelope: the core's replay invariant classifies redelivery as
replay success — never a new fact, never a conflict. A 409 payload-hash
conflict is terminal (the core holds a DIFFERENT fact under the same id —
alert, never overwrite). Rows are never deleted.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from . import core_client, envelope
from .models import RuntimeUsageOutbox

logger = logging.getLogger(__name__)

MAX_BATCH = 500
CONFLICT_RETRY_BACKOFF = timedelta(minutes=10)


def pending_rows(limit: int = 100):
    return list(
        RuntimeUsageOutbox.objects.filter(status=RuntimeUsageOutbox.Status.PENDING)
        .order_by('created_at', 'id')[: max(min(limit, MAX_BATCH), 1)]
    )


def mark_delivered(row):
    row.status = RuntimeUsageOutbox.Status.DELIVERED
    row.delivered_at = timezone.now()
    row.last_error = ''
    row.save(update_fields=['status', 'delivered_at', 'last_error'])


def mark_failed(row, error: str):
    row.status = RuntimeUsageOutbox.Status.FAILED
    row.attempts = row.attempts + 1
    row.last_error = str(error)[:1000]
    row.save(update_fields=['status', 'attempts', 'last_error'])


def requeue_failed(row):
    """Return a FAILED row to PENDING for retry (ops action / backoff expiry)."""
    row.status = RuntimeUsageOutbox.Status.PENDING
    row.save(update_fields=['status'])


def deliver_pending(*, limit: int = 100, post=None) -> dict:
    stats = {'delivered': 0, 'failed': 0, 'conflict': 0, 'unavailable': 0}
    rows = pending_rows(limit=limit)
    if not rows:
        return stats

    built = []
    batch = []
    for row in rows:
        try:
            env = envelope.build_envelope(row)
            built.append((row, env))
            batch.append(env)
        except envelope.EnvelopeError as exc:
            # A malformed row can never become deliverable by retrying; record
            # it as failed so monitoring sees it (never dropped silently).
            mark_failed(row, f'envelope error: {exc}')
            stats['failed'] += 1

    if not batch:
        return stats

    if post is None:
        post = core_client.requests.post
    try:
        results = core_client.post_usage_events_batch(batch, post=post)
    except core_client.MeteringCoreUnavailable as exc:
        for row, _env in built:
            mark_failed(row, f'core unavailable: {exc}')
            stats['unavailable'] += 1
        logger.warning('runtime metering delivery: core unavailable (%s); %d rows deferred', exc, len(built))
        return stats
    except core_client.MeteringCoreError as exc:
        if exc.http_status == 409:
            # Payload-hash conflict: the core holds a DIFFERENT fact under this
            # (source, event_id). Terminal — never overwrite, alert for triage.
            for row, _env in built:
                mark_failed(row, f'payload-hash conflict (terminal): {exc.detail}')
                stats['conflict'] += 1
            logger.error('runtime metering delivery: %d conflict(s); core said: %s', len(built), exc.detail)
            return stats
        for row, _env in built:
            mark_failed(row, f'core error: {exc.code}: {exc.detail}')
            stats['failed'] += 1
        return stats

    # The batch endpoint answers per-event results; classify each row.
    if isinstance(results, list) and len(results) == len(built):
        for (row, _env), result in zip(built, results):
            outcome = str((result or {}).get('status') or (result or {}).get('outcome') or 'accepted')
            if outcome in ('accepted', 'replayed', 'ok', 'success'):
                mark_delivered(row)
                stats['delivered'] += 1
            elif outcome in ('conflict', 'payload_hash_conflict'):
                mark_failed(row, f"payload-hash conflict (terminal): {result}")
                stats['conflict'] += 1
            else:
                mark_failed(row, f'core outcome {outcome}: {result}')
                stats['failed'] += 1
    else:
        # Batch accepted atomically without per-event detail.
        for row, _env in built:
            mark_delivered(row)
            stats['delivered'] += 1
    return stats
