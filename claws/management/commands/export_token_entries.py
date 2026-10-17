"""Export TokenPool entries as JSON for migration into Cortex (read-only).

Operator 2026-08-06: Cortex must own its provider token pool — "cortex should
not use / interact with hive service at all .. it should maintain its own token
pool .. and keep track of what's healthy and what isn't". This command is the
export half of that migration: it decrypts the requested provider's entries
inside the pod (where the KEK lives) and prints a bundle Cortex's
``import_grok_oauth --oauth-json-stdin`` ingests directly.

READ-ONLY: no row is modified, tombstoned, or re-encrypted. Retiring the Hive
entries after a verified import is a separate, deliberate step.

Output shape (stdout, single JSON object):

    {"accounts": [{"email": ..., "access_token": ..., "refresh_token": ...,
                   "id_token": ..., "expires_at": ..., "oidc_client_id": ...,
                   "oidc_issuer": ..., "cooldown_until": ...,
                   "cooldown_reason": ..., "fingerprint": ...}, ...]}

``fingerprint`` is sha256 of the stored plaintext payload — the importer side
can be diffed against it before the Hive entries are retired.

Token material goes to STDOUT ONLY — pipe it, never write it to a shared file:

    kubectl exec deploy/hive -- python manage.py export_token_entries \
        --provider xai | kubectl exec -i deploy/<cortex> -- \
        python manage.py import_grok_oauth --oauth-json-stdin
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from hive_platform.models import TokenEntry
from hive_platform.token_coordinator import (
    _json_xai_oauth_token,
    _storage_datetime,
    confirmed_xai_quota_exhausted_until,
    token_fingerprint,
)


class Command(BaseCommand):
    help = 'Export decrypted TokenPool entries as a JSON bundle for Cortex import (read-only).'

    def add_arguments(self, parser):
        parser.add_argument('--provider', required=True, help='Pool provider slug, e.g. xai')
        parser.add_argument(
            '--include-cooling',
            action='store_true',
            default=True,
            help='Include entries currently cooling (default: yes — migration wants the whole pool).',
        )

    def handle(self, *args, **options):
        provider = (options['provider'] or '').strip().lower()
        if not provider:
            raise CommandError('--provider is required')

        entries = (
            TokenEntry.objects.filter(pool__provider=provider, deleted_at__isnull=True)
            .select_related('pool')
            .order_by('created_at')
        )
        accounts = []
        skipped = []
        for entry in entries:
            try:
                raw = entry.get_token()
            except Exception as exc:  # noqa: BLE001 - report per-row, keep going
                skipped.append({'id': str(entry.id), 'label': entry.label, 'error': str(exc)})
                continue
            payload = _json_xai_oauth_token(raw) if provider == 'xai' else {'access_token': raw}
            cooldown_until = entry.cooldown_until
            cooldown_reason = 'rate_limited'
            # A confirmed 0%-credit snapshot outranks the plain cooldown: export
            # it as quota_exhausted so the importer can bench the account with
            # the semantics Cortex's router treats as a hard exclusion.
            confirmed = confirmed_xai_quota_exhausted_until(entry) if provider == 'xai' else None
            if confirmed is not None:
                # confirmed_* returns aware UTC; Hive stores naive-UTC columns
                # (USE_TZ off). _storage_datetime is the canonical converter —
                # exported isoformats are therefore naive-UTC, which the Cortex
                # importer make_aware()s as UTC.
                confirmed = _storage_datetime(confirmed)
                cooldown_reason = 'quota_exhausted'
                if cooldown_until is None or confirmed > cooldown_until:
                    cooldown_until = confirmed
            accounts.append({
                'email': payload.get('email') or entry.label,
                'account_id': payload.get('email') or entry.label,
                'access_token': payload.get('access_token') or '',
                'refresh_token': payload.get('refresh_token') or '',
                'id_token': payload.get('id_token') or '',
                'expires_at': payload.get('expires_at') or '',
                'oidc_client_id': payload.get('oidc_client_id') or '',
                'oidc_issuer': payload.get('oidc_issuer') or '',
                'cooldown_until': cooldown_until.isoformat() if cooldown_until else '',
                'cooldown_reason': cooldown_reason if cooldown_until else '',
                'fingerprint': token_fingerprint(raw),
                'hive_entry_id': str(entry.id),
                'hive_last_status': entry.last_status,
            })

        # Bundle on stdout; diagnostics on stderr so piping stays clean.
        self.stdout.write(json.dumps({'accounts': accounts}))
        self.stderr.write(
            f'exported {len(accounts)} {provider} entrie(s) at {timezone.now().isoformat()}; '
            f'skipped {len(skipped)}: {skipped}',
        )
