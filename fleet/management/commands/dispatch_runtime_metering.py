"""Dispatch due fleet runtime-metering outbox rows to the Metering Core."""
from __future__ import annotations

from django.core.management.base import BaseCommand

from fleet.metering.delivery import dispatch_runtime_metering


class Command(BaseCommand):
    help = 'Deliver due fleet runtime-metering outbox rows (at-least-once, replay-safe).'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=100)
        parser.add_argument('--dry-run', action='store_true',
                            help='Report due rows without delivering.')

    def handle(self, *args, **options):
        summary = dispatch_runtime_metering(
            limit=max(1, int(options['limit'])),
            dry_run=bool(options['dry_run']),
        )
        if summary.get('dry_run'):
            for event_id in summary.get('would_deliver', []):
                self.stdout.write(f'would_deliver {event_id}')
        self.stdout.write(
            f"dispatch complete picked={summary['picked']} "
            f"delivered={summary['delivered']} conflict={summary['conflict']} "
            f"failed={summary['failed']}"
        )
