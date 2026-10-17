import json
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

from hive_platform.models import TokenEntry, TokenEntryLeasingEvent, TokenLease, TokenPool
from . import token_coordinator as tc
from .token_coordinator import (
    apply_status_report, encrypt_token, select_token_for_owner,
    reassert_confirmed_xai_credit_cooldowns, report_token_status,
)


@override_settings(HIVE_TOKEN_COORDINATOR_KEK='unit-test-kek', HIVE_TOKEN_COORDINATOR_KEK_ID='unit-v1')
class TokenCoordinatorStorageTests(TestCase):
    def test_token_entry_encrypts_raw_token_at_rest_and_decrypts(self):
        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id='42',
            provider=TokenPool.Provider.ANTHROPIC,
        )
        entry = TokenEntry(pool=pool, label='primary', priority=10)
        entry.set_token('oat-test-secret')
        entry.save()

        stored = TokenEntry.objects.get(pk=entry.pk)
        self.assertNotIn('oat-test-secret', stored.token_ciphertext)
        self.assertNotIn('oat-test-secret', stored.wrapped_dek)
        self.assertEqual(stored.dek_key_id, 'unit-v1')
        self.assertEqual(stored.get_token(), 'oat-test-secret')

    def test_active_for_owner_is_storage_scoped_to_owner(self):
        owner_a = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id='100',
            provider=TokenPool.Provider.ANTHROPIC,
        )
        owner_b = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id='200',
            provider=TokenPool.Provider.ANTHROPIC,
        )
        a_entry = TokenEntry(pool=owner_a, label='a', priority=1)
        a_entry.set_token('owner-a-token')
        a_entry.save()
        b_entry = TokenEntry(pool=owner_b, label='b', priority=100)
        b_entry.set_token('owner-b-token')
        b_entry.save()

        rows = list(TokenEntry.objects.active_for_owner(
            TokenPool.OwnerKind.USER,
            '100',
            TokenPool.Provider.ANTHROPIC,
        ))
        self.assertEqual([row.id for row in rows], [a_entry.id])
        self.assertEqual(rows[0].get_token(), 'owner-a-token')

    def test_active_for_owner_prefers_lowest_numeric_priority(self):
        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id='300',
            provider=TokenPool.Provider.ANTHROPIC,
        )
        fallback = TokenEntry(pool=pool, label='fallback', priority=100)
        fallback.set_token('fallback-token')
        fallback.save()
        primary = TokenEntry(pool=pool, label='primary', priority=1)
        primary.set_token('primary-token')
        primary.save()

        rows = list(TokenEntry.objects.active_for_owner(
            TokenPool.OwnerKind.USER,
            '300',
            TokenPool.Provider.ANTHROPIC,
        ))
        self.assertEqual([row.label for row in rows], ['primary', 'fallback'])

    def test_personal_reserve_is_excluded_even_when_health_is_active(self):
        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id='301',
            provider=TokenPool.Provider.OPENAI,
        )
        reserved = TokenEntry(
            pool=pool,
            label='personal-reserve',
            active=True,
            agent_leasing_enabled=False,
        )
        reserved.set_token('reserved-token')
        reserved.save()

        self.assertFalse(
            TokenEntry.objects.active_for_owner(
                TokenPool.OwnerKind.USER,
                '301',
                TokenPool.Provider.OPENAI,
            ).filter(id=reserved.id).exists()
        )

        apply_status_report(reserved, 'ok')
        reserved.refresh_from_db()
        self.assertTrue(reserved.active)
        self.assertFalse(reserved.agent_leasing_enabled)

    def test_stale_xai_ok_writer_cannot_clear_newer_zero_credit_cooldown(self):
        now = timezone.now()
        reset_at = now + timedelta(days=3)
        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id='xai-stale-writer',
            provider=TokenPool.Provider.XAI,
        )
        entry = TokenEntry(pool=pool, label='zero-credit')
        entry.set_token('{"access_token":"xai-zero"}')
        entry.quota_snapshot = {
            'source': 'grok_xai_usage',
            'confidence': 'ok',
            'windows': [{
                'id': 'credits',
                'left_percent': 0.0,
                'resets_at': reset_at.isoformat(),
            }],
        }
        entry.save()
        stale = TokenEntry.objects.select_related('pool').get(pk=entry.pk)

        TokenEntry.objects.filter(pk=entry.pk).update(
            cooldown_until=reset_at,
            last_status=TokenEntry.LastStatus.RATE_LIMITED,
        )

        self.assertEqual(apply_status_report(stale, 'ok', now=now), 'rate_limited')
        entry.refresh_from_db()
        self.assertGreaterEqual(entry.cooldown_until, reset_at)
        self.assertEqual(entry.last_status, TokenEntry.LastStatus.RATE_LIMITED)

    def test_late_success_feedback_cannot_clear_xai_zero_credit_cooldown(self):
        now = timezone.now()
        reset_at = now + timedelta(days=2)
        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id='xai-late-success',
            provider=TokenPool.Provider.XAI,
        )
        entry = TokenEntry(pool=pool, label='zero-credit')
        entry.set_token('{"access_token":"xai-zero"}')
        entry.quota_snapshot = {
            'source': 'grok_xai_usage',
            'confidence': 'ok',
            'windows': [{
                'id': 'credits',
                'left_percent': 0.0,
                'resets_at': reset_at.isoformat(),
            }],
        }
        entry.cooldown_until = reset_at
        entry.last_status = TokenEntry.LastStatus.RATE_LIMITED
        entry.save()

        self.assertTrue(report_token_status(entry.pk, success=True))
        entry.refresh_from_db()
        self.assertGreaterEqual(entry.cooldown_until, reset_at)
        self.assertEqual(entry.last_status, TokenEntry.LastStatus.RATE_LIMITED)

    def test_short_xai_rate_limit_report_cannot_shorten_credit_reset(self):
        now = timezone.now()
        reset_at = now + timedelta(days=4)
        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id='xai-short-cooldown',
            provider=TokenPool.Provider.XAI,
        )
        entry = TokenEntry(pool=pool, label='zero-credit')
        entry.set_token('{"access_token":"xai-zero"}')
        entry.quota_snapshot = {
            'source': 'grok_xai_usage',
            'confidence': 'ok',
            'windows': [{
                'id': 'credits',
                'left_percent': 0.0,
                'resets_at': reset_at.isoformat(),
            }],
        }
        entry.save()

        apply_status_report(entry, 'rate_limited', retry_after=300, now=now)
        entry.refresh_from_db()
        self.assertGreaterEqual(entry.cooldown_until, reset_at)
        self.assertEqual(entry.last_status, TokenEntry.LastStatus.RATE_LIMITED)

    def test_leader_reconciliation_repairs_post_migration_old_writer_race(self):
        now = timezone.now()
        reset_at = now + timedelta(days=4)
        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id='xai-rollout-reconciliation',
            provider=TokenPool.Provider.XAI,
        )
        entry = TokenEntry(pool=pool, label='zero-credit')
        entry.set_token('{"access_token":"xai-zero"}')
        entry.quota_snapshot = {
            'source': 'grok_xai_usage',
            'confidence': 'ok',
            'windows': [{
                'id': 'credits',
                'left_percent': 0.0,
                'resets_at': reset_at.isoformat(),
            }],
        }
        # Reproduce the observed rollout handoff: an old worker writes OK after
        # the repair migration but before that worker is replaced.
        entry.cooldown_until = None
        entry.last_rate_limit_at = None
        entry.last_status = TokenEntry.LastStatus.OK
        entry.save()

        summary = reassert_confirmed_xai_credit_cooldowns(now=now)

        self.assertEqual(summary, {'checked': 1, 'reasserted': 1})
        entry.refresh_from_db()
        self.assertGreaterEqual(entry.cooldown_until, reset_at)
        self.assertIsNotNone(entry.last_rate_limit_at)
        self.assertEqual(entry.last_status, TokenEntry.LastStatus.RATE_LIMITED)

        self.assertEqual(
            reassert_confirmed_xai_credit_cooldowns(now=now),
            {'checked': 1, 'reasserted': 0},
        )

class TokenCoordinatorKEKConfigTests(TestCase):
    @override_settings(HIVE_TOKEN_COORDINATOR_KEK='')
    def test_missing_kek_fails_closed(self):
        with self.assertRaises(ImproperlyConfigured):
            encrypt_token('must-not-encrypt-under-default-key')


@override_settings(HIVE_TOKEN_COORDINATOR_KEK='unit-test-kek', HIVE_TOKEN_COORDINATOR_KEK_ID='unit-v1')
class OpenAITokenCoordinatorTests(TestCase):

    def test_openai_owner_pool_refreshes_and_returns_codex_payload(self):
        from unittest.mock import patch

        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id='700',
            provider=TokenPool.Provider.OPENAI,
        )
        entry = TokenEntry(pool=pool, label='codex-primary', priority=1)
        entry.set_token('{"refresh_token":"refresh-a","email":"owner@example.com"}')
        entry.save()

        class FakeResponse:
            status_code = 200
            def json(self):
                return {
                    'access_token': (
                        'h.'
                        'eyJleHAiOjQxMDI0NDQ4MDAsImh0dHBzOi8vYXBpLm9wZW5haS5jb20vYXV0aCI6'
                        'eyJjaGF0Z3B0X2FjY291bnRfaWQiOiJhY2N0LTEifX0'
                        '.s'
                    ),
                    'refresh_token': 'refresh-b',
                    'id_token': 'fresh-id',
                }

        with patch('hive_platform.token_coordinator.requests.post', return_value=FakeResponse()) as post:
            selection = select_token_for_owner('700', TokenPool.Provider.OPENAI)

        self.assertEqual(selection.entry_id, str(entry.id))
        served = json.loads(selection.token)
        self.assertNotIn('refresh_token', served)
        self.assertNotIn('id_token', served)
        self.assertEqual(served['account_id'], 'acct-1')
        self.assertEqual(served['auth_mode'], 'chatgptAuthTokens')
        self.assertEqual(selection.token_payload['refresh_token'], 'refresh-b')
        post.assert_called_once()
        self.assertEqual(TokenEntry.objects.get(id=entry.id).get_token().count('refresh-b'), 1)

    def test_openai_refresh_reloads_locked_generation_before_rotating(self):
        """A stale caller cannot consume the same rotating generation twice."""
        from unittest.mock import patch
        from . import token_coordinator as tc

        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id='701',
            provider=TokenPool.Provider.OPENAI,
        )
        entry = TokenEntry(pool=pool, label='serialized-refresh', priority=1)
        entry.set_token(json.dumps({
            'access_token': 'h.eyJleHAiOjF9.s',
            'refresh_token': 'generation-a',
            'account_id': 'acct-serialized',
        }))
        entry.save()
        refreshed_access = (
            'h.'
            'eyJleHAiOjQxMDI0NDQ4MDAsImh0dHBzOi8vYXBpLm9wZW5haS5jb20vYXV0aCI6'
            'eyJjaGF0Z3B0X2FjY291bnRfaWQiOiJhY2N0LXNlcmlhbGl6ZWQifX0'
            '.s'
        )

        with patch('hive_platform.token_coordinator.refresh_openai_oauth', return_value={
            'access_token': refreshed_access,
            'refresh_token': 'generation-b',
        }) as refresh:
            _, first, first_rotated = tc._refresh_openai_entry_payload(entry)
            # Reuse the intentionally stale in-memory object. The helper must
            # lock and re-read the row rather than replay generation-a.
            _, second, second_rotated = tc._refresh_openai_entry_payload(entry)

        self.assertTrue(first_rotated)
        self.assertFalse(second_rotated)
        self.assertEqual(first['refresh_token'], 'generation-b')
        self.assertEqual(second['refresh_token'], 'generation-b')
        refresh.assert_called_once_with('generation-a', timeout_seconds=20)

    def test_openai_selection_is_owner_scoped_before_org_fallback(self):
        org_pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.ORG,
            owner_id=TokenPool.ORG_OWNER_ID,
            provider=TokenPool.Provider.OPENAI,
            org_fallback_allowed=True,
        )
        org_entry = TokenEntry(pool=org_pool, label='org', priority=1)
        org_entry.set_token('{"access_token":"org-access","account_id":"acct-org"}')
        org_entry.save()

        user_pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id='800',
            provider=TokenPool.Provider.OPENAI,
        )
        user_entry = TokenEntry(pool=user_pool, label='user', priority=1)
        user_entry.set_token('{"access_token":"user-access","account_id":"acct-user"}')
        user_entry.save()

        self.assertEqual(
            select_token_for_owner('800', TokenPool.Provider.OPENAI).entry_id,
            str(user_entry.id),
        )
        fallback = select_token_for_owner('801', TokenPool.Provider.OPENAI)
        self.assertEqual(fallback.entry_id, str(org_entry.id))
        self.assertTrue(fallback.used_org_fallback)

    def test_report_status_cools_rate_limited_token_and_failover_uses_next_entry(self):
        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id='900',
            provider=TokenPool.Provider.OPENAI,
        )
        first = TokenEntry(pool=pool, label='first', priority=1)
        first.set_token('{"access_token":"first-access","account_id":"acct-first"}')
        first.save()
        second = TokenEntry(pool=pool, label='second', priority=2)
        second.set_token('{"access_token":"second-access","account_id":"acct-second"}')
        second.save()

        self.assertTrue(report_token_status(first.id, status_code=429, cooldown_seconds=60))
        self.assertEqual(
            select_token_for_owner('900', TokenPool.Provider.OPENAI).entry_id,
            str(second.id),
        )

    def test_openai_refresh_failure_cools_entry_and_fails_over(self):
        from unittest.mock import patch

        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id='950',
            provider=TokenPool.Provider.OPENAI,
        )
        stale = TokenEntry(pool=pool, label='stale', priority=1)
        stale.set_token('{"refresh_token":"stale-refresh"}')
        stale.save()
        ready = TokenEntry(pool=pool, label='ready', priority=2)
        ready.set_token('{"access_token":"ready-access","account_id":"acct-ready"}')
        ready.save()

        class FakeResponse:
            status_code = 401
            def json(self):
                return {}

        with patch('hive_platform.token_coordinator.requests.post', return_value=FakeResponse()):
            selection = select_token_for_owner('950', TokenPool.Provider.OPENAI)

        stale.refresh_from_db()
        self.assertEqual(selection.entry_id, str(ready.id))
        self.assertIsNotNone(stale.cooldown_until)


@override_settings(HIVE_TOKEN_COORDINATOR_KEK='unit-test-kek', HIVE_TOKEN_COORDINATOR_KEK_ID='unit-v1')
class XaiTokenCoordinatorRefreshSerializationTests(TestCase):
    def _entry(self):
        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id='xai-refresh-owner',
            provider=TokenPool.Provider.XAI,
        )
        entry = TokenEntry(pool=pool, label='serialized-xai', priority=1)
        entry.set_token(json.dumps({
            'type': 'xai_oauth',
            'access_token': 'access-generation-a',
            'refresh_token': 'refresh-generation-a',
            'expires_at': '2020-01-01T00:00:00+00:00',
            'oidc_client_id': 'xai-client',
        }))
        entry.save()
        return entry

    @patch('hive_platform.quota_fetchers.fetch_grok_usage')
    @patch('hive_platform.token_coordinator.refresh_xai_oauth')
    def test_two_check_now_callers_reuse_one_rotated_generation(
        self,
        refresh,
        fetch_usage,
    ):
        """Two stale top-level health callers must make one provider exchange.

        This is the production recurrence sequence: separate web workers both
        hold generation A, the first Check-now installs B, and the second must
        re-read B rather than replay A and invalidate the account family.
        """
        presented_generations = []

        def rotate(payload, **kwargs):
            presented_generations.append(payload['refresh_token'])
            return {
                'type': 'xai_oauth',
                'access_token': 'access-generation-b',
                'refresh_token': 'refresh-generation-b',
                'expires_at': '2099-01-01T00:00:00+00:00',
                'expires_in': 3600,
            }

        refresh.side_effect = rotate
        fetch_usage.return_value = {
            'source': 'grok_xai_usage',
            'confidence': 'ok',
            'detail': 'ok_via_grok_billing',
            'windows': [],
        }
        entry = self._entry()
        first_worker = TokenEntry.objects.select_related('pool').get(pk=entry.pk)
        second_worker = TokenEntry.objects.select_related('pool').get(pk=entry.pk)

        first = tc.check_entry_health(first_worker, force_refresh=True)
        second = tc.check_entry_health(second_worker, force_refresh=True)

        self.assertEqual(first['outcome'], 'ok')
        self.assertEqual(second['outcome'], 'ok')
        self.assertEqual(second['detail'], 'xai_oauth_generation_reused')
        refresh.assert_called_once()
        self.assertEqual(presented_generations, ['refresh-generation-a'])
        entry.refresh_from_db()
        stored = json.loads(entry.get_token())
        self.assertEqual(stored['refresh_token'], 'refresh-generation-b')

    @patch('hive_platform.token_coordinator.refresh_xai_oauth')
    def test_two_broker_selections_rotate_only_once(self, refresh):
        refresh.return_value = {
            'type': 'xai_oauth',
            'access_token': 'access-generation-b',
            'refresh_token': 'refresh-generation-b',
            'expires_at': '2099-01-01T00:00:00+00:00',
            'expires_in': 3600,
        }
        entry = self._entry()

        first = select_token_for_owner('xai-refresh-owner', TokenPool.Provider.XAI)
        second = select_token_for_owner('xai-refresh-owner', TokenPool.Provider.XAI)

        self.assertEqual(first.entry_id, str(entry.pk))
        self.assertEqual(second.entry_id, str(entry.pk))
        self.assertEqual(first.token_payload['refresh_token'], 'refresh-generation-b')
        self.assertEqual(second.token_payload['refresh_token'], 'refresh-generation-b')
        for selection in (first, second):
            leased = json.loads(selection.token)
            self.assertEqual(leased['access_token'], 'access-generation-b')
            self.assertEqual(leased['auth_mode'], 'xaiAccessToken')
            self.assertNotIn('refresh_token', leased)
            self.assertNotIn('id_token', leased)
        refresh.assert_called_once()


@override_settings(HIVE_TOKEN_COORDINATOR_KEK='unit-test-kek', HIVE_TOKEN_COORDINATOR_KEK_ID='unit-v1')
class TokenPoolConfigAPITests(TestCase):
    def setUp(self):
        from rest_framework.test import APIClient
        from .authentication import FederatedUser

        self.client = APIClient()
        self.user_a = FederatedUser(user_id=100, email='a@example.com', username='owner-a')
        self.user_b = FederatedUser(user_id=200, email='b@example.com', username='owner-b')
        self.staff = FederatedUser(user_id=1, email='staff@example.com', username='staff', is_staff=True)

    def test_owner_scoped_pool_and_entry_crud_never_accepts_owner_id_from_body(self):
        self.client.force_authenticate(user=self.user_a)
        response = self.client.post(
            '/api/v1/token-pools/',
            {
                'provider': TokenPool.Provider.ANTHROPIC,
                'display_name': 'Owner A',
                'owner_id': '200',  # ignored: owner is always the authenticated JWT user
            },
            format='json',
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['owner_id'], '100')
        pool_id = response.data['id']

        response = self.client.post(
            f'/api/v1/token-pools/{pool_id}/entries/',
            {'label': 'primary', 'token': 'owner-a-secret', 'tier': 'pro', 'priority': 1},
            format='json',
        )
        self.assertEqual(response.status_code, 201)
        self.assertNotIn('owner-a-secret', str(response.data))
        entry_id = response.data['id']

        self.client.force_authenticate(user=self.user_b)
        response = self.client.get(f'/api/v1/token-pools/{pool_id}/')
        self.assertEqual(response.status_code, 404)

        self.client.force_authenticate(user=self.user_a)
        response = self.client.patch(
            f'/api/v1/token-pools/{pool_id}/entries/{entry_id}/',
            {'label': 'primary-renamed', 'tier': 'free', 'priority': 5},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['label'], 'primary-renamed')
        self.assertEqual(response.data['tier'], 'free')

        response = self.client.post(f'/api/v1/token-pools/{pool_id}/entries/{entry_id}/disable/', {}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.data['active'], False)

    def test_org_pool_scope_is_staff_only(self):
        self.client.force_authenticate(user=self.user_a)
        response = self.client.get('/api/v1/token-pools/?scope=org')
        self.assertEqual(response.status_code, 403)
        response = self.client.post(
            '/api/v1/token-pools/',
            {'provider': TokenPool.Provider.OPENAI, 'scope': 'org'},
            format='json',
        )
        self.assertEqual(response.status_code, 403)

        self.client.force_authenticate(user=self.staff)
        response = self.client.post(
            '/api/v1/token-pools/',
            {'provider': TokenPool.Provider.OPENAI, 'scope': 'org', 'display_name': 'Internal OpenAI'},
            format='json',
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['owner_kind'], TokenPool.OwnerKind.ORG)
        self.assertEqual(response.data['owner_id'], TokenPool.ORG_OWNER_ID)

    def test_reserve_policy_is_persistent_and_removed_from_available_count(self):
        self.client.force_authenticate(user=self.user_a)
        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id=str(self.user_a.id),
            provider=TokenPool.Provider.OPENAI,
        )
        entry = TokenEntry(pool=pool, label='personal', active=True)
        entry.set_token('personal-token')
        entry.save()
        lease = TokenLease.objects.create(
            entry=entry,
            broker_identity='agent-before-reserve',
            owner_kind=pool.owner_kind,
            owner_id=pool.owner_id,
            provider=pool.provider,
            expires_at=timezone.now() + timedelta(minutes=15),
        )

        response = self.client.patch(
            f'/api/v1/token-pools/{pool.id}/entries/{entry.id}/',
            {'agent_leasing_enabled': False},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['active'])
        self.assertFalse(response.data['agent_leasing_enabled'])

        apply_status_report(entry, 'ok')
        entry.refresh_from_db()
        lease.refresh_from_db()
        self.assertTrue(entry.active)
        self.assertFalse(entry.agent_leasing_enabled)
        self.assertLessEqual(lease.expires_at, timezone.now())
        event = TokenEntryLeasingEvent.objects.get(entry=entry)
        self.assertTrue(event.previous_enabled)
        self.assertFalse(event.enabled)
        self.assertEqual(event.actor, 'owner-a')
        self.assertEqual(event.source, TokenEntryLeasingEvent.Source.OWNER_API)
        self.assertEqual(event.revoked_lease_count, 1)
        self.assertIsNone(
            select_token_for_owner(
                self.user_a.id,
                TokenPool.Provider.OPENAI,
                refresh_openai=False,
            )
        )

        listed = self.client.get('/api/v1/token-pools/?scope=user')
        self.assertEqual(listed.status_code, 200)
        row = next(item for item in listed.data if str(item['id']) == str(pool.id))
        self.assertEqual(row['active_entry_count'], 0)

        # Repeating the already-applied policy is idempotent and does not create
        # misleading audit noise.
        response = self.client.patch(
            f'/api/v1/token-pools/{pool.id}/entries/{entry.id}/',
            {'agent_leasing_enabled': False},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(TokenEntryLeasingEvent.objects.filter(entry=entry).count(), 1)

    def test_delete_tombstones_and_scrubs_entry_without_erasing_lineage(self):
        self.client.force_authenticate(user=self.user_a)
        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id=str(self.user_a.id),
            provider=TokenPool.Provider.OPENAI,
        )
        entry = TokenEntry(pool=pool, label='delete-me', priority=1)
        entry.set_token('sensitive-token')
        entry.save()

        response = self.client.delete(
            f'/api/v1/token-pools/{pool.id}/entries/{entry.id}/',
        )
        self.assertEqual(response.status_code, 204)

        entry.refresh_from_db()
        self.assertFalse(entry.active)
        self.assertIsNotNone(entry.deleted_at)
        self.assertEqual(entry.deleted_by, 'a@example.com')
        self.assertEqual(entry.token_ciphertext, '')
        self.assertEqual(entry.wrapped_dek, '')
        self.assertEqual(entry.dek_key_id, 'deleted')
        with self.assertRaisesMessage(ValueError, 'deleted token entry'):
            entry.get_token()

        listed = self.client.get('/api/v1/token-pools/?scope=user')
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.data[0]['entry_count'], 0)
        self.assertEqual(listed.data[0]['entries'], [])

        recreated = self.client.post(
            f'/api/v1/token-pools/{pool.id}/entries/',
            {'label': 'delete-me', 'token': 'sensitive-token'},
            format='json',
        )
        self.assertEqual(recreated.status_code, 201)
        self.assertNotEqual(recreated.data['id'], str(entry.id))

    def test_delete_is_owner_scoped_and_retains_active_lease_evidence(self):
        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id=str(self.user_a.id),
            provider=TokenPool.Provider.OPENAI,
        )
        entry = TokenEntry(pool=pool, label='leased', priority=1)
        entry.set_token('leased-token')
        entry.save()
        lease = TokenLease.objects.create(
            entry=entry,
            broker_identity='agent-a',
            owner_kind=pool.owner_kind,
            owner_id=pool.owner_id,
            provider=pool.provider,
            expires_at=timezone.now() + timedelta(minutes=10),
        )

        self.client.force_authenticate(user=self.user_b)
        denied = self.client.delete(
            f'/api/v1/token-pools/{pool.id}/entries/{entry.id}/',
        )
        self.assertEqual(denied.status_code, 404)

        self.client.force_authenticate(user=self.user_a)
        response = self.client.delete(
            f'/api/v1/token-pools/{pool.id}/entries/{entry.id}/',
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data['active_leases'], 1)
        self.assertTrue(TokenLease.objects.filter(id=lease.id, entry=entry).exists())
        entry.refresh_from_db()
        self.assertIsNotNone(entry.deleted_at)


@override_settings(HIVE_TOKEN_COORDINATOR_KEK='unit-test-kek', HIVE_TOKEN_COORDINATOR_KEK_ID='unit-v1')
class StaleCooldownRecoveryTests(TestCase):
    """recover_stale_cooldown_tokens: a pool dried by lingering rate-limit cooldowns
    self-heals (the 2026-06-26/27 codex fleet crash-loops), while a FRESH throttle
    is still respected. Bounded like the PLAT-881 dead-token revival."""

    def _entry(self, *, cooldown_secs, cooled_ago_secs, label):
        from django.utils import timezone
        from datetime import timedelta
        now = timezone.now()
        pool, _ = TokenPool.objects.get_or_create(
            owner_kind=TokenPool.OwnerKind.ORG,
            owner_id=TokenPool.ORG_OWNER_ID,
            provider=TokenPool.Provider.OPENAI,
        )
        e = TokenEntry(pool=pool, label=label, priority=0)
        e.set_token('{"access_token":"'+label+'"}')
        e.active = True
        e.cooldown_until = now + timedelta(seconds=cooldown_secs)
        e.last_rate_limit_at = now - timedelta(seconds=cooled_ago_secs)
        e.last_status = TokenEntry.LastStatus.RATE_LIMITED
        e.save()
        return e

    def test_respects_long_requested_cooldown_and_fresh_throttle(self):
        from .token_coordinator import recover_stale_cooldown_tokens
        # HIVE-1747: a cooldown requested for longer than the normal throttle window
        # (60 min stamped here) is a deliberate long cool — the 15m age heuristic
        # cannot prove it stale, so it is respected (not cleared), exactly like a
        # fresh throttle. Only a cooldown whose underlying reason has resolved (e.g.
        # a confirmed zero-credit snapshot whose reset passed) is cleared early.
        long_cool = self._entry(cooldown_secs=30 * 60, cooled_ago_secs=30 * 60, label='long-cool')
        fresh = self._entry(cooldown_secs=30 * 60, cooled_ago_secs=60, label='fresh')

        summary = recover_stale_cooldown_tokens(provider=TokenPool.Provider.OPENAI)
        self.assertEqual(summary['cleared'], 0)

        long_cool.refresh_from_db()
        fresh.refresh_from_db()
        self.assertIsNotNone(long_cool.cooldown_until)  # long cool respected
        self.assertEqual(long_cool.last_status, TokenEntry.LastStatus.RATE_LIMITED)
        self.assertIsNotNone(fresh.cooldown_until)      # fresh throttle respected

    def test_dead_tokens_untouched(self):
        from django.utils import timezone
        from datetime import timedelta
        from .token_coordinator import recover_stale_cooldown_tokens
        # active=False (auth-killed) must NOT be revived by this sweep (that's the
        # dead-token recovery's job) — only active rate-limited entries.
        dead = self._entry(cooldown_secs=30 * 60, cooled_ago_secs=30 * 60, label='dead')
        dead.active = False
        dead.save(update_fields=['active'])
        summary = recover_stale_cooldown_tokens(provider=TokenPool.Provider.OPENAI)
        self.assertEqual(summary['cleared'], 0)
        dead.refresh_from_db()
        self.assertFalse(dead.active)

    def test_confirmed_grok_zero_credit_cooldown_is_preserved_until_reset(self):
        from .token_coordinator import recover_stale_cooldown_tokens

        now = timezone.now()
        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id='42',
            provider=TokenPool.Provider.XAI,
        )
        exhausted = TokenEntry(pool=pool, label='grok-exhausted', priority=0)
        exhausted.set_token('{"access_token":"grok-exhausted"}')
        reset_at = now + timedelta(days=3)
        exhausted.quota_snapshot = {
            'source': 'grok_xai_usage',
            'confidence': 'ok',
            'fetched_at': (now - timedelta(hours=2)).isoformat(),
            'windows': [{
                'id': 'credits',
                'left_percent': 0.0,
                'resets_at': reset_at.isoformat(),
            }],
        }
        exhausted.active = True
        exhausted.cooldown_until = now + timedelta(hours=4)
        exhausted.last_rate_limit_at = now - timedelta(hours=2)
        exhausted.last_status = TokenEntry.LastStatus.RATE_LIMITED
        exhausted.save()

        summary = recover_stale_cooldown_tokens(
            provider=TokenPool.Provider.XAI,
            now=now,
        )

        exhausted.refresh_from_db()
        self.assertEqual(summary['cleared'], 0)
        self.assertGreaterEqual(exhausted.cooldown_until, reset_at)
        self.assertEqual(exhausted.last_status, TokenEntry.LastStatus.RATE_LIMITED)

    def test_confirmed_grok_zero_credit_snapshot_stops_blocking_after_reset(self):
        from .token_coordinator import recover_stale_cooldown_tokens

        now = timezone.now()
        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id='43',
            provider=TokenPool.Provider.XAI,
        )
        exhausted = TokenEntry(pool=pool, label='grok-reset', priority=0)
        exhausted.set_token('{"access_token":"grok-reset"}')
        exhausted.quota_snapshot = {
            'source': 'grok_xai_usage',
            'confidence': 'ok',
            'fetched_at': (now - timedelta(days=1)).isoformat(),
            'windows': [{
                'id': 'credits',
                'left_percent': 0.0,
                'resets_at': (now - timedelta(seconds=1)).isoformat(),
            }],
        }
        exhausted.active = True
        exhausted.cooldown_until = now + timedelta(hours=1)
        exhausted.last_rate_limit_at = now - timedelta(hours=2)
        exhausted.last_status = TokenEntry.LastStatus.RATE_LIMITED
        exhausted.save()

        summary = recover_stale_cooldown_tokens(
            provider=TokenPool.Provider.XAI,
            now=now,
        )

        exhausted.refresh_from_db()
        self.assertEqual(summary['cleared'], 1)
        self.assertIsNone(exhausted.cooldown_until)
        self.assertEqual(exhausted.last_status, TokenEntry.LastStatus.OK)

    # HIVE-1747 — the pre-dry sweep must respect the REQUESTED cooldown window.
    def test_long_requested_cooldown_is_not_cleared_by_age_heuristic(self):
        from .token_coordinator import recover_stale_cooldown_tokens
        # Cortex asked for a 6h cool (21600s) and it was stamped 20 min ago. The
        # age heuristic (15m cutoff) alone would call it stale — but the requested
        # window is 6h, so the sweep cannot prove it stale and must leave it.
        long_cool = self._entry(
            cooldown_secs=6 * 60 * 60, cooled_ago_secs=20 * 60, label='long-cool',
        )
        summary = recover_stale_cooldown_tokens(provider=TokenPool.Provider.OPENAI)
        self.assertEqual(summary['cleared'], 0)
        long_cool.refresh_from_db()
        self.assertIsNotNone(long_cool.cooldown_until)
        self.assertEqual(long_cool.last_status, TokenEntry.LastStatus.RATE_LIMITED)

    # HIVE-1747 — a provider-confirmed quota_exhausted reason holds the entry even
    # when the cooldown is short and the usage API is unreadable.
    def test_quota_exhausted_reason_holds_entry_against_the_sweep(self):
        from .token_coordinator import recover_stale_cooldown_tokens
        now = timezone.now()
        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id='44',
            provider=TokenPool.Provider.XAI,
        )
        entry = TokenEntry(pool=pool, label='grok-unsupported', priority=0)
        entry.set_token('{"access_token":"grok-unsupported"}')
        entry.active = True
        # Usage API unreadable -> confidence='unsupported' (no positive confirmation).
        entry.quota_snapshot = {
            'source': 'grok_xai_usage',
            'confidence': 'unsupported',
            'fetched_at': (now - timedelta(hours=1)).isoformat(),
            'windows': [],
        }
        # Short cooldown (5 min) stamped 20 min ago — the age heuristic alone would
        # clear it, but the quota_exhausted reason must hold it non-routable.
        entry.cooldown_until = now + timedelta(minutes=5)
        entry.last_rate_limit_at = now - timedelta(minutes=20)
        entry.last_status = TokenEntry.LastStatus.RATE_LIMITED
        entry.cooldown_reason = 'quota_exhausted'
        entry.save()

        summary = recover_stale_cooldown_tokens(
            provider=TokenPool.Provider.XAI,
            now=now,
        )
        self.assertEqual(summary['cleared'], 0)
        entry.refresh_from_db()
        self.assertIsNotNone(entry.cooldown_until)
        self.assertEqual(entry.last_status, TokenEntry.LastStatus.RATE_LIMITED)
        self.assertEqual(entry.cooldown_reason, 'quota_exhausted')

    def test_quota_exhausted_reason_cleared_on_ok_report(self):
        from .token_coordinator import apply_status_report
        now = timezone.now()
        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id='45',
            provider=TokenPool.Provider.XAI,
        )
        entry = TokenEntry(pool=pool, label='grok-recovered', priority=0)
        entry.set_token('{"access_token":"grok-recovered"}')
        entry.active = True
        entry.cooldown_until = now + timedelta(hours=6)
        entry.last_rate_limit_at = now
        entry.last_status = TokenEntry.LastStatus.RATE_LIMITED
        entry.cooldown_reason = 'quota_exhausted'
        entry.save()

        apply_status_report(entry, 'ok', now=now)
        entry.refresh_from_db()
        self.assertIsNone(entry.cooldown_until)
        self.assertEqual(entry.last_status, TokenEntry.LastStatus.OK)
        self.assertEqual(entry.cooldown_reason, '')


class XaiOauthSsrfRegressionTests(TestCase):
    """HIVE-612 (CWE-918): server-side refresh destination must be fixed.

    Attacker-controlled token payloads must never steer the server-side
    requests.post destination and redirects must be disabled.
    """

    def test_json_xai_oauth_token_ignores_attacker_token_endpoint(self):
        payload = tc._json_xai_oauth_token(json.dumps({
            'type': 'xai_oauth',
            'refresh_token': 'attacker-refresh',
            'token_endpoint': 'http://169.254.169.254/latest/meta-data/',
            'oidc_client_id': 'attacker-client',
            'oidc_issuer': 'http://127.0.0.1:8000/issuer',
        }))
        self.assertEqual(payload['token_endpoint'], tc.XAI_OAUTH_TOKEN_URL)
        self.assertEqual(payload['oidc_client_id'], tc.XAI_OAUTH_CLIENT_ID)
        self.assertEqual(payload['oidc_issuer'], tc.XAI_OAUTH_ISSUER)

    def test_json_xai_oauth_token_ignores_token_endpoint_aliases(self):
        for alias in ('tokenEndpoint', 'token_url', 'endpoint', 'oidc_issuer'):
            payload = tc._json_xai_oauth_token(json.dumps({
                'type': 'xai_oauth',
                'refresh_token': 'attacker-refresh',
                alias: 'http://127.0.0.1:8080/internal',
            }))
            self.assertEqual(payload['token_endpoint'], tc.XAI_OAUTH_TOKEN_URL)

    def test_refresh_xai_oauth_uses_fixed_endpoint_and_disables_redirects(self):
        with patch('hive_platform.token_coordinator.requests.post', return_value=type(
            'R', (), {
                'status_code': 200,
                'json': lambda self: {'access_token': 'fresh', 'expires_in': 3600},
            },
        )()) as post:
            refreshed = tc.refresh_xai_oauth({
                'refresh_token': 'legacy-refresh',
                'token_endpoint': 'http://169.254.169.254/',
                'oidc_client_id': 'attacker-client',
            })
            self.assertEqual(refreshed['token_endpoint'], tc.XAI_OAUTH_TOKEN_URL)
            call = post.call_args
            self.assertEqual(call[0][0], tc.XAI_OAUTH_TOKEN_URL)
            self.assertIs(call.kwargs.get('allow_redirects'), False)
            # client_id must come from the fixed constant, never the payload.
            self.assertEqual(call.kwargs['data']['client_id'], tc.XAI_OAUTH_CLIENT_ID)

    def test_refresh_xai_oauth_legacy_payload_with_attacker_endpoint_uses_fixed(self):
        """A legacy/malicious stored bundle must fail closed to the constant."""
        with patch('hive_platform.token_coordinator.requests.post', return_value=type(
            'R', (), {
                'status_code': 200,
                'json': lambda self: {'access_token': 'fresh'},
            },
        )()) as post:
            refreshed = tc.refresh_xai_oauth({
                'refresh_token': 'legacy',
                'token_endpoint': 'http://10.0.0.1:9090/',
            })
            self.assertEqual(refreshed['token_endpoint'], tc.XAI_OAUTH_TOKEN_URL)
            self.assertEqual(post.call_args[0][0], tc.XAI_OAUTH_TOKEN_URL)

    def test_refresh_xai_oauth_missing_refresh_token_still_fails_closed(self):
        with self.assertRaises(tc.XaiOAuthRefreshError):
            tc.refresh_xai_oauth({
                'token_endpoint': 'http://127.0.0.1:1337/',
            })
