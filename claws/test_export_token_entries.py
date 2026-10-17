"""export_token_entries — read-only Cortex-migration export (2026-08-06).

Operator: Cortex must own its provider token pool. This command is the export
half; these tests pin the contract Cortex's ``import_grok_oauth`` relies on:
full OAuth material, quota benches exported as ``quota_exhausted``, tombstoned
rows excluded, and strictly no writes.
"""
import json
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from hive_platform.models import TokenEntry, TokenPool


def _entry(label, payload, **kwargs):
    pool, _ = TokenPool.objects.get_or_create(
        owner_kind=TokenPool.OwnerKind.ORG,
        owner_id=TokenPool.ORG_OWNER_ID,
        provider=TokenPool.Provider.XAI,
    )
    e = TokenEntry(pool=pool, label=label, tier='', priority=0)
    e.set_token(json.dumps(payload))
    for k, v in kwargs.items():
        setattr(e, k, v)
    e.save()
    return e


def _xai_payload(email='acct@example.com'):
    return {
        'type': 'xai_oauth',
        'email': email,
        'access_token': 'eyJ.stub.access',
        'refresh_token': 'stub-refresh',
        'id_token': 'eyJ.stub.id',
        'expires_at': '2030-01-01T00:00:00+00:00',
        'oidc_client_id': 'b1a00492-073a-47ea-816f-4c329264a828',
        'oidc_issuer': 'https://auth.x.ai',
        'token_endpoint': 'https://auth.x.ai/oauth2/token',
    }


def _run(*args):
    out, err = StringIO(), StringIO()
    call_command('export_token_entries', *args, stdout=out, stderr=err)
    return json.loads(out.getvalue()), err.getvalue()


class ExportTokenEntriesTests(TestCase):
    def test_exports_full_oauth_material_for_import(self):
        _entry('acct-1', _xai_payload('one@example.com'))
        bundle, _ = _run('--provider', 'xai')
        self.assertEqual(len(bundle['accounts']), 1)
        acct = bundle['accounts'][0]
        self.assertEqual(acct['email'], 'one@example.com')
        self.assertEqual(acct['access_token'], 'eyJ.stub.access')
        self.assertEqual(acct['refresh_token'], 'stub-refresh')
        self.assertEqual(acct['oidc_client_id'], 'b1a00492-073a-47ea-816f-4c329264a828')
        self.assertTrue(acct['fingerprint'])

    def test_plain_cooldown_exports_as_rate_limited(self):
        until = timezone.now() + timedelta(hours=3)
        _entry('acct-cooling', _xai_payload('cooling@example.com'), cooldown_until=until)
        bundle, _ = _run('--provider', 'xai')
        acct = bundle['accounts'][0]
        self.assertEqual(acct['cooldown_reason'], 'rate_limited')
        self.assertEqual(acct['cooldown_until'], until.isoformat())

    def test_confirmed_zero_credit_snapshot_exports_as_quota_exhausted(self):
        cooldown = timezone.now() + timedelta(hours=3)
        reset = timezone.now() + timedelta(days=5)
        _entry(
            'acct-benched', _xai_payload('benched@example.com'),
            cooldown_until=cooldown,
            quota_snapshot={
                'source': 'grok_xai_usage',
                'confidence': 'ok',
                'fetched_at': timezone.now().isoformat(),
                'windows': [{
                    'id': 'credits', 'label': 'Credits',
                    'resets_at': reset.isoformat(), 'left_percent': 0.0,
                }],
            },
        )
        bundle, _ = _run('--provider', 'xai')
        acct = bundle['accounts'][0]
        self.assertEqual(acct['cooldown_reason'], 'quota_exhausted')
        # The confirmed reset outranks the shorter plain cooldown.
        self.assertEqual(acct['cooldown_until'], reset.isoformat())

    def test_tombstoned_entries_are_excluded(self):
        _entry('acct-live', _xai_payload('live@example.com'))
        _entry('acct-dead', _xai_payload('dead@example.com'), deleted_at=timezone.now())
        bundle, _ = _run('--provider', 'xai')
        emails = [a['email'] for a in bundle['accounts']]
        self.assertEqual(emails, ['live@example.com'])

    def test_export_writes_nothing(self):
        entry = _entry('acct-ro', _xai_payload('ro@example.com'))
        before = TokenEntry.objects.get(pk=entry.pk).updated_at
        _run('--provider', 'xai')
        self.assertEqual(TokenEntry.objects.get(pk=entry.pk).updated_at, before)
