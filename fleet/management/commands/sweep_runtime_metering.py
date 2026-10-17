"""Catch-up sweeper: emit the final partial runtime interval for quiet seats.

PLAT-7523 (R2 Slice 2): the heartbeat hook only meters intervals that end at
a NEW heartbeat. A seat that stops heartbeating would otherwise lose its
final partial interval. This command emits ``[last_active_at,
last_active_at + grace)`` for seats whose last heartbeat is older than the
heartbeat freshness threshold but within the dead-seat grace cap — runtime
beyond the cap is a watermark gap: logged, never silently credited (no
fabrication for dead seats).

Idempotent by the deterministic event id (same interval → same identity →
get_or_create replay); ``--dry-run`` reports without writing.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from fleet.models import FleetAgent
from fleet.metering.events import (
    DEAD_SEAT_GRACE_SECONDS,
    SOURCE_RUNTIME,
    enqueue_runtime_interval,
)
from fleet.metering.models import FleetRuntimeOutbox

logger = logging.getLogger(__name__)

# A seat is "quiet" once its last heartbeat is older than this (two missed
# 5-minute heartbeat cycles); the sweeper then closes its final interval.
QUIET_THRESHOLD_SECONDS = 600


class Command(BaseCommand):
    help = (
        'Emit the final partial runtime interval for seats that stopped '
        'heartbeating, capped at the dead-seat grace; log watermark gaps.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--limit', type=int, default=500)

    def handle(self, *args, **options):
        dry_run = bool(options['dry_run'])
        limit = max(1, int(options['limit']))
        now = timezone.now()
        quiet_before = now - timedelta(seconds=QUIET_THRESHOLD_SECONDS)
        grace_cap = timedelta(seconds=DEAD_SEAT_GRACE_SECONDS)

        candidates = (
            FleetAgent.objects
            .filter(last_active_at__lt=quiet_before)
            .exclude(last_active_at__isnull=True)
            .order_by('last_active_at')[:limit]
        )

        emitted = 0
        skipped_ownerless = 0
        watermark_gaps = 0
        for agent in candidates:
            final_end = agent.last_active_at + grace_cap
            if final_end > now:
                # Still inside the grace window — the next sweep after the cap
                # elapses closes the interval; nothing to do yet.
                continue
            if agent.owner_id is None:
                skipped_ownerless += 1
                logger.warning(
                    'runtime metering sweep: owner-less agent %s quiet since %s — '
                    'watermark gap, no payer invented',
                    agent.pk, agent.last_active_at,
                )
                continue
            event_id = (
                f'hive-runtime:{agent.pk}:'
                f'{int(agent.last_active_at.timestamp())}'
            )
            already = FleetRuntimeOutbox.objects.filter(
                source=SOURCE_RUNTIME, event_id=event_id,
            ).exclude(status=FleetRuntimeOutbox.State.CONFLICT).exists()
            if already:
                continue
            if dry_run:
                self.stdout.write(f'would_emit {event_id}')
                emitted += 1
                continue
            try:
                with transaction.atomic():
                    enqueue_runtime_interval(
                        agent=agent,
                        prev_active=agent.last_active_at,
                        now=final_end,
                    )
                emitted += 1
            except Exception as exc:  # noqa: BLE001 — one bad row never stops the sweep
                watermark_gaps += 1
                logger.error(
                    'runtime metering sweep: emission failed for agent %s: %s',
                    agent.pk, exc,
                )

        # Watermark-gap report: quiet seats whose grace window has NOT yet
        # elapsed are pending closure, not gaps; seats beyond the cap that
        # failed emission above are the gaps.
        self.stdout.write(
            f'sweep complete emitted={emitted} skipped_ownerless={skipped_ownerless} '
            f'watermark_gaps={watermark_gaps} grace_seconds={DEAD_SEAT_GRACE_SECONDS}'
        )
