"""Deliver due Metering Core producer-outbox rows."""
import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from claws import usage_metering_metrics
from claws.metering_events import deliver_usage_outbox
from hive_platform.models import UsageEventOutbox


class Command(BaseCommand):
    help = 'Deliver due CloudEvents usage outbox rows with replay-safe retries.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=100)
        parser.add_argument(
            '--time-budget-seconds', type=float, default=None,
            help='Stop processing once this many seconds have elapsed (leaves '
                 'remaining rows for the next run). Defaults to 100s — under the '
                 'CronJob activeDeadlineSeconds (120s) so a burst of pending rows '
                 'or a slow Metering Core endpoint cannot push the run past its '
                 'deadline (PLAT-6295 DeadlineExceeded).',
        )

    def handle(self, *args, **options):
        limit = max(1, min(int(options['limit']), 1000))
        # Default budget leaves a safety margin under the CronJob's
        # activeDeadlineSeconds (120s) so the pod finishes and records its
        # heartbeat instead of being killed as DeadlineExceeded.
        budget = options.get('time_budget_seconds')
        if budget is None:
            budget = 100.0
        budget = max(1.0, float(budget))
        deadline = time.monotonic() + budget
        ids = list(
            UsageEventOutbox.objects
            .filter(
                status=UsageEventOutbox.Status.PENDING,
                next_attempt_at__lte=timezone.now(),
            )
            .order_by('next_attempt_at', 'created_at')
            .values_list('id', flat=True)[:limit]
        )
        delivered = 0
        pending = 0
        conflicts = 0
        for outbox_id in ids:
            # PLAT-6295: honor the time budget — stop mid-batch when the budget
            # is exhausted so the run never exceeds the CronJob deadline. Rows
            # not yet attempted stay PENDING with their next_attempt_at intact
            # and are picked up by the next run (every minute, Forbid).
            if time.monotonic() >= deadline:
                self.stdout.write(
                    f'time_budget_exhausted processed={delivered + pending + conflicts} '
                    f'delivered={delivered} pending={pending} conflicts={conflicts} '
                    f'remaining={len(ids) - (delivered + pending + conflicts)}'
                )
                usage_metering_metrics.record_outbox_dispatch_heartbeat()
                return
            row = deliver_usage_outbox(outbox_id)
            if row.status == UsageEventOutbox.Status.DELIVERED:
                delivered += 1
            elif row.status == UsageEventOutbox.Status.CONFLICT:
                conflicts += 1
            else:
                pending += 1
        self.stdout.write(
            f'processed={len(ids)} delivered={delivered} pending={pending} conflicts={conflicts}'
        )
        usage_metering_metrics.record_outbox_dispatch_heartbeat()
