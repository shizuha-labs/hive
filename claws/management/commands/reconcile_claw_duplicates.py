"""Audit/delete stale duplicate Hive-managed claw namespaces (CON-161)."""
import json

from django.core.management.base import BaseCommand, CommandError

from claws.provisioner import cleanup_stale_duplicate_claw_namespaces, get_k8s_client


class Command(BaseCommand):
    help = 'Audit or clean stale duplicate Hive-managed claw namespaces for one owner.'

    def add_arguments(self, parser):
        parser.add_argument('--owner-id', type=int, required=True)
        parser.add_argument('--apply', action='store_true', help='Scale stale deployments to 0 and delete stale namespaces.')
        parser.add_argument('--json', action='store_true')

    def handle(self, *args, **options):
        core_v1, apps_v1 = get_k8s_client()
        summary = cleanup_stale_duplicate_claw_namespaces(
            core_v1,
            apps_v1,
            options['owner_id'],
            dry_run=not options['apply'],
        )
        if options['json']:
            self.stdout.write(json.dumps(summary, indent=2, sort_keys=True))
        else:
            self.stdout.write(
                'claw-duplicates owner={owner_id} checked={checked} kept={kept_n} stale={stale_n} deleted={deleted_n} errors={errors_n} dry_run={dry_run}'.format(
                    owner_id=summary['owner_id'],
                    checked=summary['checked'],
                    kept_n=len(summary['kept']),
                    stale_n=len(summary['stale']),
                    deleted_n=len(summary['deleted']),
                    errors_n=len(summary['errors']),
                    dry_run=summary['dry_run'],
                )
            )
            for row in summary['stale']:
                self.stdout.write(f"stale {row['namespace']} claw_id={row.get('claw_id')}")
        if summary['errors']:
            raise CommandError(f"cleanup errors: {summary['errors']}")
