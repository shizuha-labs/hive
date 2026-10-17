import json
from unittest.mock import patch

from django.test import TestCase, override_settings

from hive_platform.models import TokenEntry, TokenPool
from hive_platform.token_coordinator import apply_status_report, refresh_entry_quota_snapshot


@override_settings(
    HIVE_TOKEN_COORDINATOR_KEK='claude-review-test-kek',
    HIVE_TOKEN_COORDINATOR_KEK_ID='claude-review-v1',
)
class ClaudeQuotaGenerationReviewTests(TestCase):
    def setUp(self):
        self.pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER, owner_id='42',
            provider=TokenPool.Provider.ANTHROPIC,
        )
        self.payload = {
            'type': 'claude_oauth', 'access_token': 'sk-ant-oat01-old',
            'refresh_token': 'sk-ant-ort01-old',
            'expires_at': '2099-01-01T00:00:00+00:00',
        }
        self.entry = TokenEntry(pool=self.pool, label='review', account_email='fixture@example.com')
        self.entry.set_token(json.dumps(self.payload))
        self.entry.save()
        guard = patch('requests.sessions.Session.request', side_effect=AssertionError('unexpected provider egress'))
        guard.start()
        self.addCleanup(guard.stop)

    def test_old_usage_rejection_cannot_disable_new_reauthentication(self):
        def usage_after_reauthentication(access):
            self.assertEqual(access, 'sk-ant-oat01-old')
            current = TokenEntry.objects.get(pk=self.entry.pk)
            current.set_token(json.dumps({**self.payload, 'refresh_token': 'new-generation'}))
            current.quota_snapshot = {
                'source': 'claude_oauth_usage', 'confidence': 'exact',
                'windows': [{'id': 'five_hour', 'used_percent': 37}],
            }
            current.save()
            self.reauthenticated_quota = current.quota_snapshot
            return {'source': 'claude_oauth_usage', 'confidence': 'error', 'windows': [], 'detail': 'auth_failed'}

        with patch('hive_platform.quota_fetchers.fetch_claude_oauth_usage', side_effect=usage_after_reauthentication):
            result = refresh_entry_quota_snapshot(self.entry, force=True, refresh_oauth=False)
        self.entry.refresh_from_db()
        self.assertTrue(self.entry.active)
        self.assertEqual(self.entry.quota_snapshot, self.reauthenticated_quota)
        self.assertFalse(result['updated'])

    def test_old_usage_success_cannot_erase_terminal_refresh_failure(self):
        def usage_after_failure(access):
            current = TokenEntry.objects.get(pk=self.entry.pk)
            apply_status_report(current, 'auth_failed')
            return {
                'source': 'claude_oauth_usage', 'confidence': 'exact',
                'windows': [{'id': 'five_hour', 'used_percent': 10}],
            }

        with patch('hive_platform.quota_fetchers.fetch_claude_oauth_usage', side_effect=usage_after_failure):
            refresh_entry_quota_snapshot(self.entry, force=True, refresh_oauth=False)
        self.entry.refresh_from_db()
        self.assertFalse(self.entry.active)
        self.assertEqual(self.entry.last_status, TokenEntry.LastStatus.AUTH_FAILED)

    def test_current_generation_usage_still_persists(self):
        snapshot = {
            'source': 'claude_oauth_usage', 'confidence': 'exact',
            'windows': [{'id': 'five_hour', 'used_percent': 10}],
        }
        with patch('hive_platform.quota_fetchers.fetch_claude_oauth_usage', return_value=snapshot):
            result = refresh_entry_quota_snapshot(self.entry, force=True, refresh_oauth=False)
        self.entry.refresh_from_db()
        self.assertTrue(result['updated'])
        self.assertTrue(self.entry.active)
        self.assertEqual(self.entry.last_status, TokenEntry.LastStatus.OK)
        self.assertEqual(self.entry.quota_snapshot['confidence'], 'exact')
        self.assertEqual(self.entry.quota_snapshot['windows'][0]['used_percent'], 10)
