"""HIVE-326: audit/backfill claw namespace isolation controls.

Run inside the hive-api pod:

    # Audit only; exits non-zero if drift remains
    python manage.py reconcile_claw_isolation --check

    # Reapply required NetworkPolicies, ResourceQuota, LimitRange, and default-SA hardening
    python manage.py reconcile_claw_isolation

    # Preview a subset
    python manage.py reconcile_claw_isolation --dry-run --owner-id 116
"""

from django.core.management.base import BaseCommand, CommandError

from claws.provisioner import reconcile_claw_namespace_isolation


class Command(BaseCommand):
    help = (
        'Audit/backfill required tenant-isolation controls on every non-deleted '
        'Claw namespace (HIVE-326).'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Only report drift; do not modify Kubernetes resources.',
        )
        parser.add_argument(
            '--check', action='store_true',
            help='Audit only and fail with non-zero exit if any drift remains.',
        )
        parser.add_argument('--owner-id', type=int, help='Limit to a single Claw owner id.')
        parser.add_argument('--claw-id', help='Limit to one Claw UUID.')

    def handle(self, *args, **options):
        dry_run = options['dry_run'] or options['check']
        summary = reconcile_claw_namespace_isolation(
            dry_run=dry_run,
            owner_id=options.get('owner_id'),
            claw_id=options.get('claw_id'),
        )
        for issue in summary['issues']:
            self.stdout.write(
                '{namespace}: before={before} after={after}'.format(
                    namespace=issue['namespace'],
                    before=','.join(issue['missing_before']) or 'none',
                    after=','.join(issue['missing_after']) or 'none',
                )
            )
        self.stdout.write(
            'claw-isolation: checked={checked} drifted={drifted} reconciled={reconciled} '
            'still_missing={still_missing} dry_run={dry_run}'.format(**summary)
        )
        if options['check'] and summary['still_missing']:
            raise CommandError(
                f"claw isolation drift detected in {summary['still_missing']} namespace(s)"
            )
        if not options['check'] and summary['still_missing']:
            raise CommandError(
                f"claw isolation drift remains in {summary['still_missing']} namespace(s)"
            )
