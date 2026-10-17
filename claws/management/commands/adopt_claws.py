"""HIVE-173: Adopt manually-deployed OpenClaw/Hermes pods into HIVE.

Some claws were deployed manually to a user namespace before the HIVE
provisioner existed (e.g. the four agents in ns user-116). This command
creates the HIVE DB records that point to those *existing* k8s resources
so harshita can see and manage them via the standard dashboard at
/hive/openclaw/<claw_id>/.

Key assumptions:
  - The k8s resources already exist and are running.
  - The owner's Shizuha-ID integer user_id is provided (--owner-id).
  - Each adopted claw gets a synthetic tier_id unique within the owner to
    satisfy the DB uniqueness constraint (uniq_active_claw_owner_tier).
  - The existing namespace is preserved (not renamed to the HIVE convention).

Usage (run inside the hive-api pod):

    # Dry-run first to preview what would be created:
    python manage.py adopt_claws \\
      --owner-id 116 --owner-username harshita --owner-email harshita@shizuha.com \\
      --namespace user-116 \\
      --claw "name=claw-qwen tier=adopt-qwen display=Qwen Assistant"   \\
      --claw "name=claw-gemma tier=adopt-gemma display=Gemma Assistant"  \\
      --claw "name=hermes-qwen tier=adopt-hermes-qwen display=Hermes Qwen"      \\
      --claw "name=hermes-gemma tier=adopt-hermes-gemma display=Hermes Gemma"   \\
      --dry-run

    # Apply:
    python manage.py adopt_claws \\
      --owner-id 116 --owner-username harshita --owner-email harshita@shizuha.com \\
      --namespace user-116 \\
      --claw "name=claw-qwen tier=adopt-qwen display=Qwen Assistant"   \\
      --claw "name=claw-gemma tier=adopt-gemma display=Gemma Assistant"  \\
      --claw "name=hermes-qwen tier=adopt-hermes-qwen display=Hermes Qwen"      \\
      --claw "name=hermes-gemma tier=adopt-hermes-gemma display=Hermes Gemma"

The command is idempotent: if a claw with the given (owner_id, tier_id) already
exists, it is reported and skipped (not duplicated).
"""
import shlex

from django.core.management.base import BaseCommand, CommandError

from claws.models import Claw, ClawEvent


class Command(BaseCommand):
    help = 'Adopt manually-deployed OpenClaw pods into HIVE (HIVE-173)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--owner-id', type=int, required=True,
            help='Shizuha-ID integer user_id for the owner (e.g. 116)',
        )
        parser.add_argument(
            '--owner-username', required=True,
            help='Shizuha-ID username of the owner (e.g. harshita)',
        )
        parser.add_argument(
            '--owner-email', required=True,
            help='Owner email address (e.g. harshita@shizuha.com)',
        )
        parser.add_argument(
            '--namespace', required=True,
            help='k8s namespace containing the existing deployments (e.g. user-116)',
        )
        parser.add_argument(
            '--claw', dest='claws', action='append', default=[],
            metavar='"name=NAME tier=TIER_ID [display=DISPLAY NAME]"',
            help=(
                'Claw spec as key=value tokens. Required: name (k8s Deployment name), '
                'tier (unique tier_id). Optional: display (human-readable name). '
                'Repeat --claw for each deployment to adopt.'
            ),
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Preview what would be created without writing to the DB',
        )

    def _parse_claw_spec(self, raw):
        spec = {}
        for token in shlex.split(raw):
            if '=' not in token:
                raise CommandError(
                    f'Invalid claw spec token (expected key=value): {token!r}'
                )
            k, _, v = token.partition('=')
            spec[k.strip()] = v.strip()
        for required in ('name', 'tier'):
            if required not in spec:
                raise CommandError(
                    f'Claw spec missing required key {required!r}: {raw!r}'
                )
        spec.setdefault('display', spec['name'])
        return spec

    def handle(self, *args, **options):
        owner_id = options['owner_id']
        owner_username = options['owner_username']
        owner_email = options['owner_email']
        namespace = options['namespace']
        dry_run = options['dry_run']

        if not options['claws']:
            raise CommandError('No --claw specs provided; nothing to adopt.')

        specs = [self._parse_claw_spec(r) for r in options['claws']]

        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN — no DB changes will be made\n'))

        created = []
        skipped = []

        for spec in specs:
            deployment_name = spec['name']
            tier_id = spec['tier']
            display_name = spec['display']

            existing = Claw.objects.filter(
                owner_id=owner_id,
                tier_id=tier_id,
                status__in=[
                    Claw.Status.PROVISIONING,
                    Claw.Status.RUNNING,
                    Claw.Status.STOPPED,
                    Claw.Status.SUSPENDED_OWNER_REMOVED,
                    Claw.Status.SUSPENDED_NO_CREDITS,
                    Claw.Status.DELETING,
                ],
            ).first()

            if existing:
                skipped.append((spec, existing))
                self.stdout.write(
                    f'  SKIP   {deployment_name!r} — already exists '
                    f'(id={existing.id}, tier={tier_id}, status={existing.status})'
                )
                continue

            self.stdout.write(
                f'  {"PREVIEW" if dry_run else "CREATE"} '
                f'{display_name!r} → ns={namespace} deploy={deployment_name} '
                f'tier={tier_id}'
            )

            if not dry_run:
                claw = Claw(
                    owner_id=owner_id,
                    owner_username=owner_username,
                    owner_email=owner_email,
                    name=display_name,
                    tier_id=tier_id,
                    namespace=namespace,
                    pod_name=deployment_name,
                    status=Claw.Status.RUNNING,
                    pvc_size='10Gi',
                )
                claw.save()
                ClawEvent.objects.create(
                    claw=claw,
                    event_type='adopted',
                    message=(
                        f'Adopted from manual deployment {deployment_name!r} '
                        f'in namespace {namespace!r} (HIVE-173).'
                    ),
                )
                created.append(claw)

        self.stdout.write('')
        if dry_run:
            self.stdout.write(self.style.SUCCESS(
                f'Dry run complete — {len(specs)} spec(s): '
                f'{len(specs) - len(skipped)} would be created, {len(skipped)} skipped.'
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f'Done — {len(created)} adopted, {len(skipped)} skipped.'
            ))
            for claw in created:
                self.stdout.write(
                    f'  {claw.name}  id={claw.id}  '
                    f'/hive/assistants/{claw.id}/data'
                )
