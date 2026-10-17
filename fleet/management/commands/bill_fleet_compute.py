"""Debit owner Hane for reserved fleet CPU/RAM over completed clock hours."""

from django.core.management.base import BaseCommand

from fleet.compute_billing import bill_recent_hours


class Command(BaseCommand):
    help = 'Bill reserved Hive agent CPU/RAM in Hane for completed clock hours.'

    def add_arguments(self, parser):
        parser.add_argument('--hours', type=int, default=1, help='How many completed hours to bill (max 168).')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        hours = max(1, min(int(options['hours'] or 1), 168))
        dry = bool(options['dry_run'])
        rows = bill_recent_hours(hours=hours, dry_run=dry)
        for row in rows:
            self.stdout.write(
                f"{row['hour']} agents={row['agents']} charged_micros={row['charged_micros']} "
                f"shortfall={row['shortfall_micros']} errors={row['errors']}"
            )
        total = sum(int(row['charged_micros']) for row in rows)
        self.stdout.write(self.style.SUCCESS(
            f"{'dry-run ' if dry else ''}billed {total} micros across {len(rows)} hour(s)"
        ))
