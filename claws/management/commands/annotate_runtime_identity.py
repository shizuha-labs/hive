"""Annotate Claw rows with the runtime image actually deployed in Kubernetes."""
from django.core.management.base import BaseCommand
from django.conf import settings
from kubernetes.client.rest import ApiException

from claws.models import Claw
from claws.provisioner import DEPLOYMENT_NAME, get_k8s_client


class Command(BaseCommand):
    help = 'Backfill Claw.runtime_image/runtime_compat_mode from live k8s deployments.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Print changes without writing them.')
        parser.add_argument('--owner-id', type=int, help='Limit to a single owner id.')
        parser.add_argument('--agent-type', choices=[choice[0] for choice in Claw.AgentType.choices], help='Limit by agent_type.')

    def handle(self, *args, **options):
        _, apps_v1 = get_k8s_client()
        qs = Claw.objects.exclude(status=Claw.Status.DELETED).order_by('owner_id', 'created_at')
        if options.get('owner_id') is not None:
            qs = qs.filter(owner_id=options['owner_id'])
        if options.get('agent_type'):
            qs = qs.filter(agent_type=options['agent_type'])

        openclaw_image = (getattr(settings, 'HIVE_CLAW_IMAGE', '') or '').strip()
        dry_run = options['dry_run']
        scanned = updated = skipped = 0
        for claw in qs:
            scanned += 1
            try:
                deployment = apps_v1.read_namespaced_deployment(name=DEPLOYMENT_NAME, namespace=claw.namespace)
            except ApiException as exc:
                skipped += 1
                self.stdout.write(f'{claw.id} {claw.agent_type}: skip {claw.namespace} ({exc.status})')
                continue
            containers = getattr(deployment.spec.template.spec, 'containers', []) or []
            container = next((c for c in containers if c.name == DEPLOYMENT_NAME), containers[0] if containers else None)
            image = (getattr(container, 'image', '') or '').strip() if container else ''
            if not image:
                skipped += 1
                self.stdout.write(f'{claw.id} {claw.agent_type}: skip {claw.namespace} (no image)')
                continue

            image_l = image.lower()
            compat = claw.agent_type == Claw.AgentType.HERMES and (
                image == openclaw_image or 'openclaw' in image_l
            )
            if claw.runtime_image == image and claw.runtime_compat_mode == compat:
                self.stdout.write(f'{claw.id} {claw.agent_type}: already {image} compat={compat}')
                continue

            self.stdout.write(
                f'{claw.id} {claw.agent_type}: runtime_image {claw.runtime_image or "—"} -> {image}; '
                f'compat {claw.runtime_compat_mode} -> {compat}'
            )
            updated += 1
            if not dry_run:
                claw.runtime_image = image
                claw.runtime_compat_mode = compat
                claw.save(update_fields=['runtime_image', 'runtime_compat_mode', 'updated_at'])

        suffix = ' (dry-run)' if dry_run else ''
        self.stdout.write(self.style.SUCCESS(f'scanned={scanned} updated={updated} skipped={skipped}{suffix}'))
