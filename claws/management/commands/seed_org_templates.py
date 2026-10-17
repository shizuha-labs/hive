import json

from django.core.management.base import BaseCommand

from claws.org_templates import seed_builtin_org_templates


class Command(BaseCommand):
    help = 'Seed/update the HIVE-368 built-in org template starter library.'

    def add_arguments(self, parser):
        parser.add_argument('--actor', default='hive:hive-368-starter-library')
        parser.add_argument('--json', action='store_true')

    def handle(self, *args, **options):
        templates = seed_builtin_org_templates(actor=options['actor'])
        payload = [
            {
                'slug': template.slug,
                'version': template.version,
                'status': template.status,
                'definition_hash': template.definition_hash,
                'team_count': len(template.teams or []),
                'starter_task_count': len(template.starter_tasks or []),
            }
            for template in templates
        ]
        if options['json']:
            self.stdout.write(json.dumps(payload, sort_keys=True))
            return
        for item in payload:
            self.stdout.write(
                '{slug}@v{version} {status} teams={team_count} starter_tasks={starter_task_count}'.format(**item)
            )
