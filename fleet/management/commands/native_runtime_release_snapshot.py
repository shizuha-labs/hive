"""Read-only joined native release proof for the source publication lane."""
import json
from django.core.management.base import BaseCommand, CommandError
from fleet.native_runtime_release import runtime_snapshot
from fleet.runtime_release import RuntimeReleaseProjectionUnavailable


class Command(BaseCommand):
    help = 'Read native runtime authority and actual Hive-owned Deployment/Pod census; never actuates'

    def add_arguments(self, parser):
        parser.add_argument('--bootstrap', action='store_true', help='Verify explicit native27 CM before consumer activation')

    def handle(self, *args, **options):
        try:
            snapshot = runtime_snapshot(bootstrap=options['bootstrap'], include_writers=True)
        except RuntimeReleaseProjectionUnavailable as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(snapshot, sort_keys=True, separators=(',', ':')))
