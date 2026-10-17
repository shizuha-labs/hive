from django.core.management.base import BaseCommand

from analytics.backlog import materialize_discovery_backlog


class Command(BaseCommand):
    help = 'HIVE-396: materialize concrete Theme rows into Pulse discovery backlog items.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=100)
        parser.add_argument('--project-id', type=int, default=None)
        parser.add_argument('--parent-item-id', default=None)

    def handle(self, *args, **options):
        kwargs = {'limit': options['limit']}
        if options.get('project_id') is not None:
            kwargs['project_id'] = options['project_id']
        if options.get('parent_item_id'):
            kwargs['parent_item_id'] = options['parent_item_id']
        result = materialize_discovery_backlog(**kwargs)
        self.stdout.write(self.style.SUCCESS(f'materialized discovery backlog: {result}'))
