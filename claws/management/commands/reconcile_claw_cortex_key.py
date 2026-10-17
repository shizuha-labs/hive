"""HIVE-2233 — explicit repair command for the Claw↔Cortex key reconcile.

Usage:
    python manage.py reconcile_claw_cortex_key --claw-id <uuid> [--dry-run]

Idempotent: safe to re-run; resumes the HIVE-1397 saga from its crash point.
"""
from django.core.management.base import BaseCommand, CommandError

from claws.models import Claw
from claws.reconcile_cortex_key import reconcile_claw_cortex_key


class Command(BaseCommand):
    help = (
        'Reconcile a Claw\'s bound Cortex API key onto the owner-attributed '
        'desired tier (HIVE-2233 / HIVE-1400 cutover saga). Idempotent.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--claw-id', required=True, help='Claw UUID to reconcile.')
        parser.add_argument('--dry-run', action='store_true', help='Report intent without mutating.')
        parser.add_argument(
            '--org-id',
            type=int,
            default=None,
            help=(
                'Tenant org id for the claw (HIVE-2233 fix). Cortex requires '
                'org_id on STANDARD key mints; the reconcile path has no owner '
                'JWT. Persists claw.org_id when unset, then reconciles.'
            ),
        )

    def handle(self, *args, **options):
        claw_id = options['claw_id']
        try:
            claw = Claw.objects.get(id=claw_id)
        except (Claw.DoesNotExist, ValueError) as exc:
            raise CommandError(f'claw {claw_id} not found: {exc}') from exc

        org_id = options.get('org_id')
        if org_id is not None and not claw.org_id:
            claw.org_id = org_id
            claw.save(update_fields=['org_id', 'updated_at'])
            self.stdout.write(f'claw {claw.id}: persisted org_id={org_id}')

        binding, outcome = reconcile_claw_cortex_key(
            claw.id, dry_run=options['dry_run'],
        )
        self.stdout.write(
            f'claw {claw.id} ({claw.agent_type}) -> state={binding.state} '
            f'outcome={outcome} tier={binding.desired_tier or "?"}'
        )
        if binding.last_error:
            self.stdout.write(self.style.WARNING(f'last_error: {binding.last_error}'))
        if outcome == 'error':
            raise CommandError(f'reconcile failed: {binding.last_error}')
