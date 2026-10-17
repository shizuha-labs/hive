"""HIVE-606: the HA background refresh loop (run_codex_refresh_loop) also
proactively refreshes Anthropic/Claude full-login bundles near expiry, so
lease-time consumers don't depend on the Subscriptions dashboard being open.
Bare setup-token OATs (no refresh material) stay observational-only.
"""
import json
import time
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings

from hive_platform.claude_oauth import ClaudeOAuthError
from hive_platform.models import TokenEntry, TokenPool
from hive_platform.token_coordinator import (
    _anthropic_entries_needing_refresh,
    run_codex_refresh_loop,
)


def _iso(secs_from_now):
    return (datetime.now(dt_timezone.utc) + timedelta(seconds=secs_from_now)).isoformat()


@override_settings(HIVE_TOKEN_COORDINATOR_KEK='unit-test-kek', HIVE_TOKEN_COORDINATOR_KEK_ID='unit-v1')
class AnthropicBackgroundRefreshTests(TestCase):
    def setUp(self):
        cache.clear()
        release_patcher = patch('hive_platform.token_coordinator._release_leader_lock', return_value=True)
        self.mock_release = release_patcher.start()
        self.addCleanup(release_patcher.stop)
        self.pool, _ = TokenPool.objects.get_or_create(
            owner_kind=TokenPool.OwnerKind.ORG,
            owner_id=TokenPool.ORG_OWNER_ID,
            provider=TokenPool.Provider.ANTHROPIC,
        )

    _counter = 0

    def _bundle_entry(self, *, exp_secs_from_now=60, has_refresh=True, active=True):
        type(self)._counter += 1
        bundle = {
            'type': 'claude_oauth',
            'access_token': f'at-{self._counter}',
            'refresh_token': (f'rt-{self._counter}' if has_refresh else ''),
            'expires_at': _iso(exp_secs_from_now),
            'scopes': ['user:profile', 'user:inference'],
        }
        e = TokenEntry(pool=self.pool, label=f'claude-{self._counter}', priority=0, active=active)
        e.set_token(json.dumps(bundle))
        e.save()
        return e

    def _oat_entry(self):
        type(self)._counter += 1
        e = TokenEntry(pool=self.pool, label=f'oat-{self._counter}', priority=0, active=True)
        e.set_token('sk-ant-oat01-observational-only')
        e.save()
        return e

    # ── selection helper ──
    def test_selection_yields_near_expiry_bundle(self):
        self._bundle_entry(exp_secs_from_now=60)
        self.assertEqual(len(list(_anthropic_entries_needing_refresh())), 1)

    def test_selection_skips_fresh_bundle(self):
        self._bundle_entry(exp_secs_from_now=3600)
        self.assertEqual(list(_anthropic_entries_needing_refresh()), [])

    def test_selection_skips_bare_oat(self):
        self._oat_entry()
        self.assertEqual(list(_anthropic_entries_needing_refresh()), [])

    def test_selection_skips_bundle_without_refresh_token(self):
        self._bundle_entry(exp_secs_from_now=60, has_refresh=False)
        self.assertEqual(list(_anthropic_entries_needing_refresh()), [])

    # ── loop behavior ──
    @patch('hive_platform.token_coordinator._try_acquire_leader_lock', return_value=True)
    def test_dry_run_counts_bundles(self, _lock):
        self._bundle_entry(exp_secs_from_now=60)
        self._bundle_entry(exp_secs_from_now=120)
        summary = run_codex_refresh_loop(dry_run=True)
        self.assertTrue(summary['leader_held'])
        self.assertEqual(summary['refreshed'], 2)
        self.assertEqual(summary['failed'], 0)

    @patch('hive_platform.token_coordinator._try_acquire_leader_lock', return_value=True)
    @patch('hive_platform.claude_oauth.refresh_claude_oauth')
    def test_successful_refresh_rotates_bundle(self, mock_refresh, _lock):
        mock_refresh.return_value = {
            'access_token': 'new-access', 'refresh_token': 'new-refresh',
            'expires_in': 3600, 'scope': 'user:profile user:inference',
        }
        entry = self._bundle_entry(exp_secs_from_now=60)
        summary = run_codex_refresh_loop()
        self.assertEqual(summary['refreshed'], 1)
        self.assertEqual(summary['failed'], 0)
        mock_refresh.assert_called_once()
        self.assertEqual(mock_refresh.call_args.kwargs['timeout_seconds'], 5)
        entry.refresh_from_db()
        stored = json.loads(entry.get_token())
        self.assertEqual(stored['access_token'], 'new-access')
        self.assertEqual(stored['refresh_token'], 'new-refresh')
        self.assertEqual(entry.last_status, TokenEntry.LastStatus.OK)
        self.assertIsNone(entry.cooldown_until)

    @patch('hive_platform.token_coordinator._try_acquire_leader_lock', return_value=True)
    @patch('hive_platform.claude_oauth.refresh_claude_oauth', side_effect=ClaudeOAuthError(
        'refresh rejected (401)', status_code=401,
    ))
    def test_rejection_parks_entry_and_records_failed(self, _mock_refresh, _lock):
        entry = self._bundle_entry(exp_secs_from_now=60)
        summary = run_codex_refresh_loop()
        self.assertEqual(summary['refreshed'], 0)
        self.assertEqual(summary['failed'], 1)
        self.assertTrue(summary['errors'])
        entry.refresh_from_db()
        self.assertEqual(entry.last_status, TokenEntry.LastStatus.AUTH_FAILED)
        self.assertFalse(entry.active)
        self.assertIsNotNone(entry.cooldown_until)

    @patch('hive_platform.token_coordinator._try_acquire_leader_lock', return_value=True)
    @patch('hive_platform.claude_oauth.refresh_claude_oauth')
    def test_bare_oat_not_refreshed(self, mock_refresh, _lock):
        self._oat_entry()
        summary = run_codex_refresh_loop()
        self.assertEqual(summary['refreshed'], 0)
        mock_refresh.assert_not_called()
