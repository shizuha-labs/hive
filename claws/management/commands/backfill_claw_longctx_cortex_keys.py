"""Backfill existing OpenClaw/Hermes Cortex keys onto the claw-longctx tier."""

from django.core.management.base import BaseCommand
from django.utils import timezone
from kubernetes.client.rest import ApiException

from claws.models import Claw
from claws.provisioner import DEPLOYMENT_NAME, get_k8s_client, mint_owner_cortex_key


class Command(BaseCommand):
    help = (
        'Backfill existing Hive OpenClaw/Hermes pods by minting owner-scoped '
        'claw-longctx Cortex keys through the trusted internal endpoint and '
        'patching each claw-config Secret. Pods are restarted so env-mounted '
        'CORTEX_API_KEY values pick up the replacement key.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='List target claws without minting or patching.')
        parser.add_argument('--owner-id', type=int, help='Limit to one owner id.')
        parser.add_argument('--claw-id', help='Limit to one Claw UUID.')
        parser.add_argument(
            '--no-restart',
            action='store_true',
            help='Patch the Secret but do not restart the Deployment. Not recommended for env-mounted keys.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        restart = not options['no_restart']
        qs = (
            Claw.objects.exclude(status=Claw.Status.DELETED)
            .filter(agent_type__in=[Claw.AgentType.CLAW, Claw.AgentType.HERMES])
            .order_by('owner_id', 'created_at')
        )
        if options.get('owner_id') is not None:
            qs = qs.filter(owner_id=options['owner_id'])
        if options.get('claw_id'):
            qs = qs.filter(id=options['claw_id'])

        core_v1 = apps_v1 = None
        if not dry_run:
            core_v1, apps_v1 = get_k8s_client()

        scanned = minted = patched = restarted = skipped = 0
        for claw in qs:
            scanned += 1
            ns = (claw.namespace or '').strip()
            if not ns:
                skipped += 1
                self.stdout.write(f'{claw.id} {claw.agent_type}: skip (no namespace)')
                continue

            self.stdout.write(f'{claw.id} {claw.agent_type}: target namespace={ns}')
            if dry_run:
                continue

            key = mint_owner_cortex_key(claw, '')
            if not key:
                skipped += 1
                self.stdout.write(f'{claw.id} {claw.agent_type}: skip (Cortex key mint failed)')
                continue
            minted += 1

            try:
                core_v1.patch_namespaced_secret(
                    name='claw-config',
                    namespace=ns,
                    body={'stringData': {'cortex-api-key': key}},
                )
            except ApiException as exc:
                skipped += 1
                self.stdout.write(f'{claw.id} {claw.agent_type}: secret patch failed ({exc.status})')
                continue
            patched += 1

            if restart:
                annotation_key = 'hive.shizuha.com/claw-longctx-backfill-at'
                body = {
                    'spec': {
                        'template': {
                            'metadata': {
                                'annotations': {
                                    annotation_key: timezone.now().isoformat(),
                                }
                            }
                        }
                    }
                }
                try:
                    apps_v1.patch_namespaced_deployment(name=DEPLOYMENT_NAME, namespace=ns, body=body)
                    restarted += 1
                except ApiException as exc:
                    skipped += 1
                    self.stdout.write(f'{claw.id} {claw.agent_type}: deployment restart patch failed ({exc.status})')
                    continue

            self.stdout.write(f'{claw.id} {claw.agent_type}: backfilled claw-longctx key')

        suffix = ' (dry-run)' if dry_run else ''
        self.stdout.write(
            self.style.SUCCESS(
                f'scanned={scanned} minted={minted} patched={patched} restarted={restarted} skipped={skipped}{suffix}'
            )
        )
