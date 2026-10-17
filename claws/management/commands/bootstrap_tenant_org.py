"""Create/resume a HIVE-320 tenant-org provisioning run.

The first implementation slice is deliberately safe: by default it records the
idempotent saga and stops before external side effects. Use ``--dry-run`` to
preview all planned resources, or ``--allow-side-effects`` only after concrete
service adapters for the remaining steps are implemented.
"""

import json

from django.core.management.base import BaseCommand

from claws.tenant_provisioning import (
    advance_preview,
    create_or_get_run,
    summarize_steps,
)


class Command(BaseCommand):
    help = 'Create/resume a HIVE-320 tenant org provisioning saga.'

    def add_arguments(self, parser):
        parser.add_argument('--requester-user-id', type=int, required=True)
        parser.add_argument('--requester-email', default='')
        parser.add_argument('--org-name', required=True)
        parser.add_argument('--org-slug', required=True)
        parser.add_argument('--template-slug', required=True)
        parser.add_argument('--plan-slug', default='')
        parser.add_argument('--idempotency-key', default=None)
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument(
            '--allow-side-effects',
            action='store_true',
            help='Reserved for future adapter-backed runs; currently non-dry-runs still stop before side effects.',
        )
        parser.add_argument(
            '--json',
            action='store_true',
            help='Emit a machine-readable summary instead of human lines.',
        )

    def handle(self, *args, **options):
        run, created = create_or_get_run(
            requester_user_id=options['requester_user_id'],
            requester_email=options['requester_email'],
            org_name=options['org_name'],
            org_slug=options['org_slug'],
            template_slug=options['template_slug'],
            plan_slug=options['plan_slug'],
            dry_run=options['dry_run'],
            idempotency_key=options['idempotency_key'],
        )
        run = advance_preview(run, allow_side_effects=options['allow_side_effects'])

        if options['json']:
            self.stdout.write(json.dumps({
                'id': str(run.id),
                'created': created,
                'status': run.status,
                'current_step': run.current_step,
                'idempotency_key': run.idempotency_key,
                'resource_refs': run.resource_refs,
                'steps': run.step_state,
            }, sort_keys=True))
            return

        self.stdout.write(f'run={run.id} created={created} status={run.status} current_step={run.current_step}')
        self.stdout.write(f'idempotency_key={run.idempotency_key}')
        if run.status_message:
            self.stdout.write(run.status_message)
        for line in summarize_steps(run):
            self.stdout.write(f'- {line}')
