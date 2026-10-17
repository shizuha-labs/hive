"""Re-connecting an account must revive a retired row (operator 2026-08-06).

    i just added a xai grok token here in hive .. but i don't see it

Device-auth dedup is by (pool, label), which matches TOMBSTONED rows too. A
re-auth therefore wrote fresh OAuth material onto a `deleted_at` row: every
serving path excludes it and `get_token()` refuses it, so a freshly authorized
account was invisible in the UI and unusable by the fleet. Completing device
auth is an unambiguous intent for the account to be live.
"""
import json

from django.test import TestCase
from django.utils import timezone

from hive_platform.models import TokenEntry, TokenPool
from .token_coordinator import fleet_credentials_pool


def _tokens(email='acct@example.com'):
    return {
        'access_token': 'eyJ.stub.access',
        'refresh_token': 'stub-refresh',
        'id_token': 'eyJ.stub.id',
        'expires_in': 3600,
        'email': email,
    }


class GrokIngestResurrectsTombstonedEntryTests(TestCase):
    def _ingest(self, email, **kw):
        from .grok_device_auth import ingest_grok_account
        return ingest_grok_account(_tokens(email), **kw)

    def test_reauth_revives_a_tombstoned_entry(self):
        email = 'revive@example.com'
        self._ingest(email)
        pool = fleet_credentials_pool(TokenPool.Provider.XAI)
        # Label is derived from the OAuth subject, not the email argument.
        entry = TokenEntry.objects.get(pool=pool)
        entry.deleted_at = timezone.now()
        entry.active = False
        entry.save(update_fields=['deleted_at', 'active'])

        self._ingest(email)

        entry.refresh_from_db()
        self.assertIsNone(entry.deleted_at, 're-auth must clear the tombstone')
        self.assertTrue(entry.active)
        # And the credential must actually be usable, not just visible.
        payload = json.loads(entry.get_token())
        self.assertEqual(payload['access_token'], 'eyJ.stub.access')

    def test_revived_entry_is_visible_to_the_pool_listing(self):
        email = 'visible@example.com'
        self._ingest(email)
        pool = fleet_credentials_pool(TokenPool.Provider.XAI)
        # Label is derived from the OAuth subject, not the email argument.
        entry = TokenEntry.objects.get(pool=pool)
        entry.deleted_at = timezone.now()
        entry.save(update_fields=['deleted_at'])
        self.assertEqual(pool.entries.filter(deleted_at__isnull=True).count(), 0)

        self._ingest(email)
        self.assertEqual(pool.entries.filter(deleted_at__isnull=True).count(), 1)

    def test_reauth_drops_the_stale_quota_bench(self):
        """The old snapshot described the OLD credential's credits."""
        email = 'quota@example.com'
        self._ingest(email)
        pool = fleet_credentials_pool(TokenPool.Provider.XAI)
        # Label is derived from the OAuth subject, not the email argument.
        entry = TokenEntry.objects.get(pool=pool)
        entry.deleted_at = timezone.now()
        entry.quota_snapshot = {'source': 'grok_xai_usage', 'confidence': 'ok',
                                'windows': [{'id': 'credits', 'left_percent': 0.0}]}
        entry.save(update_fields=['deleted_at', 'quota_snapshot'])

        self._ingest(email)
        entry.refresh_from_db()
        self.assertEqual(entry.quota_snapshot, {})
        self.assertIsNone(entry.cooldown_until)
