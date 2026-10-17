"""Move org-pool token entries to the platform owner's PERSONAL pools.

Personal-ownership model (operator 2026-07-11): "the credentials are mine, the
agents using them are mine" — credentials belong to the USER who added them; the
synthetic org pool is only for genuinely org-shared credentials. Historically
everything was imported into the org pool, so this one-time command re-homes
those entries to the fleet owner's user pools.

SEQUENCING (critical): run this ONLY after the scope-union coordinator code
(`_broker_owner_scopes` / `active_for_scopes`) is LIVE on every hive replica.
Old code resolves fleet brokers to the org pool alone — moving the entries first
would dry the pool and 503 every agent's model-token mint. The union makes the
move order-safe afterwards.

Usage:
    manage.py migrate_org_pool_to_user            # dry-run (default)
    manage.py migrate_org_pool_to_user --apply    # actually move
"""
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from hive_platform.models import TokenPool, TokenEntry


class Command(BaseCommand):
    help = "Move org-pool token entries to the platform owner's personal pools (dry-run by default)."

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Actually move entries (default: dry-run).')
        parser.add_argument(
            '--owner-user-id', default='',
            help='Target user id (default: settings.HIVE_FLEET_OWNER_USER_ID).',
        )

    def handle(self, *args, **opts):
        target_user = (opts['owner_user_id'] or str(getattr(settings, 'HIVE_FLEET_OWNER_USER_ID', '') or '')).strip()
        if not target_user:
            self.stderr.write(self.style.ERROR('No target user id (set HIVE_FLEET_OWNER_USER_ID or --owner-user-id).'))
            return
        apply = opts['apply']
        moved = skipped = 0
        with transaction.atomic():
            org_pools = TokenPool.objects.filter(
                owner_kind=TokenPool.OwnerKind.ORG, owner_id=TokenPool.ORG_OWNER_ID,
            )
            for org_pool in org_pools:
                user_pool, _created = TokenPool.objects.get_or_create(
                    owner_kind=TokenPool.OwnerKind.USER,
                    owner_id=target_user,
                    provider=org_pool.provider,
                    defaults={'display_name': f'Personal ({org_pool.provider})'},
                )
                for entry in org_pool.entries.all():
                    # Unique constraints are (pool, label) and (pool, fingerprint):
                    # a same-token/same-label copy already in the user pool means
                    # this entry is a duplicate — leave it in place and report.
                    clash = user_pool.entries.filter(token_fingerprint=entry.token_fingerprint).exists() \
                        or user_pool.entries.filter(label=entry.label).exists()
                    if clash:
                        skipped += 1
                        self.stdout.write(f'SKIP (duplicate in user pool): {org_pool.provider}/{entry.label}')
                        continue
                    self.stdout.write(f'MOVE: {org_pool.provider}/{entry.label} -> user/{target_user}')
                    if apply:
                        entry.pool = user_pool
                        entry.save(update_fields=['pool', 'updated_at'])
                    moved += 1
            if not apply:
                transaction.set_rollback(True)
        mode = 'APPLIED' if apply else 'DRY-RUN'
        self.stdout.write(self.style.SUCCESS(f'{mode}: moved={moved} skipped={skipped} target=user/{target_user}'))
