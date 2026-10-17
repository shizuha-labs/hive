"""Compatibility tombstone for Hive's retired mutable promotion command."""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Disabled: runtime adoption requires a reviewed DesiredRuntimeRelease.'

    def add_arguments(self, parser):
        parser.add_argument('--image', required=True)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        raise CommandError(
            'mutable Hive runtime promotion is disabled; publish the candidate, '
            'then append it through a reviewed DesiredRuntimeRelease source change'
        )
