from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from claws.usage_metering import rebuild_daily_rollups


class Command(BaseCommand):
    help = 'Rebuild HIVE-319 daily usage rollups from raw UsageEvent rows.'

    def add_arguments(self, parser):
        parser.add_argument('--date', dest='day', help='Day to rebuild (YYYY-MM-DD). Defaults to yesterday.')

    def handle(self, *args, **options):
        raw_day = options.get('day')
        if raw_day:
            try:
                day = date.fromisoformat(raw_day)
            except ValueError as exc:
                raise CommandError('--date must be YYYY-MM-DD') from exc
        else:
            day = timezone.localdate() - timedelta(days=1)
        count = rebuild_daily_rollups(day)
        self.stdout.write(self.style.SUCCESS(f'Rebuilt {count} usage rollup row(s) for {day}'))
