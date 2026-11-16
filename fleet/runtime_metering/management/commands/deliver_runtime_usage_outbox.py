"""Deliver pending runtime usage outbox rows to the Metering Core.

At-least-once, oldest-first, same (source, event_id), byte-identical
envelope. Run periodically (e.g. every 2 minutes, concurrency-forbidden):
    python manage.py deliver_runtime_usage_outbox --limit 500
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from fleet.runtime_metering.delivery import deliver_pending


class Command(BaseCommand):
    help = 'Deliver pending runtime-hours usage events to the Metering Core neutral ingest'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=500)

    def handle(self, *args, **options):
        stats = deliver_pending(limit=options['limit'])
        self.stdout.write(
            'delivered={delivered} failed={failed} conflict={conflict} unavailable={unavailable}'.format(**stats)
        )
