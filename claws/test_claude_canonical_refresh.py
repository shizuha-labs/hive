import json
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from hive_platform.claude_oauth import ClaudeOAuthError
from hive_platform.models import TokenEntry, TokenPool
from hive_platform.token_coordinator import (
    AnthropicOAuthRefreshError,
    _refresh_and_persist_claude_bundle,
    _selection_from_entry,
    apply_anthropic_refresh_failure,
    apply_status_report,
    check_entry_health,
    recover_expired_dead_tokens,
    select_token_for_owner,
)


@override_settings(
    HIVE_TOKEN_COORDINATOR_KEK='claude-canonical-test-kek',
    HIVE_TOKEN_COORDINATOR_KEK_ID='claude-canonical-v1',
)
class ClaudeCanonicalRefreshTests(TestCase):
    def setUp(self):
        self.pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER, owner_id='42',
            provider=TokenPool.Provider.ANTHROPIC,
        )
        self.payload = {
            'type': 'claude_oauth', 'access_token': 'sk-ant-oat01-expired',
            'refresh_token': 'sk-ant-ort01-original',
            'expires_at': '2000-01-01T00:00:00+00:00',
        }
        self.entry = TokenEntry(pool=self.pool, label='canonical')
        self.entry.set_token(json.dumps(self.payload))
        self.entry.save()

    def rejected(self):
        return ClaudeOAuthError('refresh rejected', status_code=400, error_code='invalid_grant')

    def fresh(self):
        return {
            'access_token': 'sk-ant-oat01-refreshed', 'refresh_token': 'sk-ant-ort01-next',
            'expires_in': 3600,
        }

    def broker_request(self):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws.authentication import FederatedUser
        from hive_platform.coordinator_views import ModelTokenView

        request = APIRequestFactory().post(
            '/hive/api/v1/coordinator/model-token', {'provider': 'anthropic'}, format='json',
        )
        force_authenticate(request, user=FederatedUser(
            user_id=0, email='broker@example.com', username='broker',
        ), token={
            'user_id': 0, 'aud': ['hive-coordinator'], 'scope': 'coordinator:model-token',
            'owner_kind': 'user', 'owner_id': '42', 'workload_id': 'claude-canonical-broker',
        })
        return ModelTokenView.as_view()(request)

    def test_real_broker_does_not_lease_rejected_grant(self):
        with patch('hive_platform.claude_oauth.refresh_claude_oauth', side_effect=self.rejected()):
            response = self.broker_request()
        self.assertEqual(response.status_code, 404)
        self.assertFalse(self.entry.leases.exists())
        self.entry.refresh_from_db()
        self.assertFalse(self.entry.active)

    def test_real_broker_checks_generation_and_health_at_lease_commit(self):
        for mutation in ('disabled', 'replaced'):
            with self.subTest(mutation=mutation):
                self.entry.active = True
                self.entry.cooldown_until = None
                self.entry.last_status = TokenEntry.LastStatus.OK
                self.entry.set_token(json.dumps({**self.payload, 'expires_at': '2099-01-01T00:00:00+00:00'}))
                self.entry.save()

                def select_then_mutate(entry, **kwargs):
                    selection = _selection_from_entry(entry, **kwargs)
                    current = TokenEntry.objects.get(pk=entry.pk)
                    if mutation == 'disabled':
                        current.active = False
                        current.last_status = TokenEntry.LastStatus.AUTH_FAILED
                    else:
                        current.set_token(json.dumps({**self.payload, 'refresh_token': 'replacement'}))
                    current.save()
                    return selection

                with patch('hive_platform.token_coordinator._selection_from_entry', side_effect=select_then_mutate):
                    response = self.broker_request()
                self.assertEqual(response.status_code, 404)
                self.assertFalse(self.entry.leases.exists())

    def test_expired_access_never_falls_back_after_rejected_refresh(self):
        with patch('hive_platform.claude_oauth.refresh_claude_oauth', side_effect=self.rejected()):
            with self.assertRaises(AnthropicOAuthRefreshError) as caught:
                _selection_from_entry(self.entry)
        self.assertEqual(caught.exception.error_code, 'invalid_grant')
        self.assertEqual(caught.exception.credential_fingerprint, self.entry.token_fingerprint)

    def test_transient_refresh_also_never_serves_expired_access(self):
        with patch('hive_platform.claude_oauth.refresh_claude_oauth', side_effect=ClaudeOAuthError('timeout')):
            with self.assertRaises(AnthropicOAuthRefreshError) as caught:
                _selection_from_entry(self.entry)
        self.assertEqual(apply_anthropic_refresh_failure(self.entry, caught.exception), 'rate_limited')
        self.entry.refresh_from_db()
        self.assertTrue(self.entry.active)

    def test_selection_parks_rejected_grant_instead_of_releasing_stale_token(self):
        with patch('hive_platform.claude_oauth.refresh_claude_oauth', side_effect=self.rejected()):
            selected = select_token_for_owner('42', 'anthropic', allow_org_fallback=False)
        self.assertIsNone(selected)
        self.entry.refresh_from_db()
        self.assertFalse(self.entry.active)
        self.assertEqual(self.entry.last_status, TokenEntry.LastStatus.AUTH_FAILED)

    def test_two_stale_callers_exchange_only_one_refresh_generation(self):
        other = TokenEntry.objects.select_related('pool').get(pk=self.entry.pk)
        with patch('hive_platform.claude_oauth.refresh_claude_oauth', return_value=self.fresh()) as refresh:
            first = _selection_from_entry(self.entry)
            second = _selection_from_entry(other)
        refresh.assert_called_once()
        self.assertEqual(first.token, 'sk-ant-oat01-refreshed')
        self.assertEqual(first.token, second.token)

    def test_forced_stale_caller_does_not_rotate_newer_generation_again(self):
        other = TokenEntry.objects.select_related('pool').get(pk=self.entry.pk)
        with patch('hive_platform.claude_oauth.refresh_claude_oauth', return_value=self.fresh()) as refresh:
            first = _refresh_and_persist_claude_bundle(self.entry, self.payload)
            second = _refresh_and_persist_claude_bundle(other, self.payload, force=True)
        refresh.assert_called_once()
        self.assertEqual(first, second)

    def test_late_rejection_cannot_disable_reauthenticated_generation(self):
        with patch('hive_platform.claude_oauth.refresh_claude_oauth', side_effect=self.rejected()):
            with self.assertRaises(AnthropicOAuthRefreshError) as caught:
                _selection_from_entry(self.entry)
        replacement = TokenEntry.objects.get(pk=self.entry.pk)
        replacement.set_token(json.dumps({**self.payload, 'refresh_token': 'reauthenticated'}))
        replacement.save()
        self.assertEqual(apply_anthropic_refresh_failure(self.entry, caught.exception), 'generation_changed')
        replacement.refresh_from_db()
        self.assertTrue(replacement.active)

    def test_rejected_refresh_grant_is_not_optimistically_revived(self):
        apply_status_report(self.entry, 'auth_failed', now=timezone.now() - timedelta(days=2))
        result = recover_expired_dead_tokens(provider='anthropic', probe=lambda entry: 'error')
        self.assertEqual(result['recovered'], 0)
        self.assertEqual(result['manual_reauth_required'], 1)
        self.entry.refresh_from_db()
        self.assertFalse(self.entry.active)
        self.assertEqual(apply_status_report(self.entry, 'ok'), 'auth_failed')

    def test_check_now_does_not_claim_health_when_refresh_fails_transiently(self):
        with patch('hive_platform.claude_oauth.refresh_claude_oauth', side_effect=ClaudeOAuthError('timeout')):
            result = check_entry_health(self.entry, force_refresh=True)
        self.assertEqual(result['outcome'], 'rate_limited')
        self.entry.refresh_from_db()
        self.assertTrue(self.entry.active)

    def test_check_now_classifies_provider_rejection_as_reauth_required(self):
        with patch('hive_platform.claude_oauth.refresh_claude_oauth', side_effect=self.rejected()):
            result = check_entry_health(self.entry, force_refresh=True)
        self.assertEqual(result['outcome'], 'auth_failed')
        self.entry.refresh_from_db()
        self.assertFalse(self.entry.active)

    def test_check_now_cannot_refresh_or_revive_terminal_grant(self):
        apply_status_report(self.entry, 'auth_failed')
        with patch('hive_platform.claude_oauth.refresh_claude_oauth') as refresh:
            result = check_entry_health(self.entry, force_refresh=True)
        refresh.assert_not_called()
        self.assertEqual(result['outcome'], 'auth_failed')
        self.entry.refresh_from_db()
        self.assertFalse(self.entry.active)

    def test_fresh_terminal_bundle_does_not_display_false_ok(self):
        self.entry.set_token(json.dumps({**self.payload, 'expires_at': '2099-01-01T00:00:00+00:00'}))
        self.entry.save()
        apply_status_report(self.entry, 'auth_failed')
        with patch('hive_platform.claude_oauth.refresh_claude_oauth') as refresh:
            result = check_entry_health(self.entry, force_refresh=False)
        refresh.assert_not_called()
        self.assertEqual(result['outcome'], 'auth_failed')


@override_settings(
    HIVE_TOKEN_COORDINATOR_KEK='claude-canonical-test-kek',
    HIVE_TOKEN_COORDINATOR_KEK_ID='claude-canonical-v1',
)
class ClaudeConcurrentRefreshTests(TransactionTestCase):
    def setUp(self):
        ClaudeCanonicalRefreshTests.setUp(self)

    def test_overlapping_refresh_callers_use_one_provider_generation(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from django.db import close_old_connections, connections

        first_entered = Event()
        second_entered = Event()
        release = Event()
        other = TokenEntry.objects.select_related('pool').get(pk=self.entry.pk)

        def provider_refresh(payload, **kwargs):
            first_entered.set()
            if not release.wait(5):
                raise AssertionError('refresh release was not signaled')
            return ClaudeCanonicalRefreshTests.fresh(self)

        def select(entry, entered=None):
            close_old_connections()
            try:
                if entered:
                    entered.set()
                return _selection_from_entry(entry).token
            finally:
                connections.close_all()

        with patch('hive_platform.claude_oauth.refresh_claude_oauth', side_effect=provider_refresh) as refresh:
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(select, self.entry)
                self.assertTrue(first_entered.wait(5))
                second = executor.submit(select, other, second_entered)
                self.assertTrue(second_entered.wait(5))
                release.set()
                self.assertEqual(first.result(timeout=10), 'sk-ant-oat01-refreshed')
                self.assertEqual(second.result(timeout=10), 'sk-ant-oat01-refreshed')
        refresh.assert_called_once()
