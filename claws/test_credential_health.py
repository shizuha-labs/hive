"""Credential health board + Check-now (operator dashboard)."""
import base64
import json
import time
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from hive_platform.models import TokenEntry, TokenLease, TokenPool
from . import token_coordinator as tc
from .views import CredentialHealthOverviewView


def _entry(provider=TokenPool.Provider.OPENAI, **kwargs):
    pool, _ = TokenPool.objects.get_or_create(
        owner_kind=TokenPool.OwnerKind.ORG,
        owner_id=TokenPool.ORG_OWNER_ID,
        provider=provider,
    )
    e = TokenEntry(pool=pool, label=kwargs.pop('label', 't1'), tier='', priority=0)
    e.set_token(kwargs.pop('token', '{"refresh_token":"rt","access_token":"at","email":"a@b.com"}'))
    for k, v in kwargs.items():
        setattr(e, k, v)
    e.save()
    return e


def _jwt(exp_ts):
    header = base64.urlsafe_b64encode(json.dumps({'alg': 'none'}).encode()).decode().rstrip('=')
    payload = base64.urlsafe_b64encode(json.dumps({'exp': exp_ts}).encode()).decode().rstrip('=')
    return f'{header}.{payload}.signature'


class CredentialHealthOverviewTests(TestCase):
    def test_quota_snapshot_is_positive_projected_on_model_save_and_egress(self):
        e = _entry(
            label='projected',
            quota_snapshot={
                'source': 'codex_wham_usage',
                'fetched_at': datetime.now(dt_timezone.utc).isoformat(),
                'confidence': 'exact',
                'plan': 'pro',
                'detail': 'http_500:upstream-secret',
                'windows': [{
                    'id': '5h',
                    'label': 'Session (5h)',
                    'left_percent': 42,
                    'nested': {'token': 'secret'},
                }],
                'raw': {'accessToken': 'secret'},
                'accessToken': 'secret',
            },
        )
        e.refresh_from_db()

        self.assertNotIn('raw', e.quota_snapshot)
        self.assertNotIn('accessToken', e.quota_snapshot)
        self.assertNotIn('nested', e.quota_snapshot['windows'][0])
        self.assertEqual(e.quota_snapshot['detail'], 'http_500')
        overview = tc.build_credential_health_overview(
            owner_scope=TokenPool.objects.filter(owner_kind=TokenPool.OwnerKind.ORG),
        )
        row = next(a for a in overview['accounts'] if a['entry_id'] == str(e.id))
        self.assertNotIn('secret', str(row['quota']))

    def test_credential_health_response_is_not_cacheable(self):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user(
            username='quota-staff',
            email='quota-staff@example.com',
            is_staff=True,
        )
        request = APIRequestFactory().get('/v1/credential-health/')
        force_authenticate(request, user=user)

        response = CredentialHealthOverviewView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Cache-Control'], 'no-store')

    def test_generated_at_is_real_utc_instant_not_chicago_wall(self):
        """HIVE-1325: generated_at must match wall-clock UTC within a small skew.

        Hive USE_TZ=False + TIME_ZONE=America/Chicago makes timezone.now() a naive
        local wall clock. Labelling that value as UTC (or appending bare Z) leaves
        the operator page five hours behind the HTTP Date header. Emit a real
        aware-UTC instant instead.
        """
        before = datetime.now(dt_timezone.utc)
        overview = tc.build_credential_health_overview(
            owner_scope=TokenPool.objects.filter(owner_kind=TokenPool.OwnerKind.ORG),
        )
        after = datetime.now(dt_timezone.utc)

        raw = overview['generated_at']
        self.assertIsInstance(raw, str)
        # Explicit timezone required (Z or numeric offset) — never a bare naive ISO.
        self.assertRegex(raw, r'(Z|[+-][0-9]{2}:[0-9]{2})$')
        parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        self.assertIsNotNone(parsed.tzinfo)
        parsed_utc = parsed.astimezone(dt_timezone.utc)
        self.assertGreaterEqual(parsed_utc, before - timedelta(seconds=2))
        self.assertLessEqual(parsed_utc, after + timedelta(seconds=2))
        # Must not be the Chicago wall clock mislabelled as UTC (≈5h behind in CDT).
        chicago_skew = abs((parsed_utc - before).total_seconds())
        self.assertLess(chicago_skew, 30, msg=f'generated_at skew too large: {chicago_skew}s raw={raw}')


    def test_overview_does_not_count_personal_reserve_as_leaseable(self):
        _entry(
            provider=TokenPool.Provider.XAI,
            label='personal-reserve',
            token='{"access_token":"xai-reserved"}',
            agent_leasing_enabled=False,
            last_status=TokenEntry.LastStatus.OK,
            active=True,
        )

        overview = tc.build_credential_health_overview(
            owner_scope=TokenPool.objects.filter(owner_kind=TokenPool.OwnerKind.ORG),
        )

        self.assertEqual(overview['summary']['total'], 1)
        self.assertEqual(overview['summary']['leaseable'], 0)
        self.assertEqual(overview['summary']['revoked_or_disabled'], 1)
        self.assertTrue(overview['generated_at'].endswith('+00:00'))

    def test_overview_counts_cooling(self):
        now = timezone.now()
        _entry(
            label='cool',
            token='{"refresh_token":"rt-cool","access_token":"at-cool","email":"cool@b.com"}',
            cooldown_until=now + timedelta(minutes=5),
            last_status=TokenEntry.LastStatus.RATE_LIMITED,
            active=True,
        )
        _entry(
            label='ok',
            token='{"refresh_token":"rt-ok","access_token":"at-ok","email":"ok@b.com"}',
            last_status=TokenEntry.LastStatus.OK,
            active=True,
            cooldown_until=None,
        )
        overview = tc.build_credential_health_overview(
            owner_scope=TokenPool.objects.filter(owner_kind=TokenPool.OwnerKind.ORG),
        )
        self.assertGreaterEqual(overview['summary']['total'], 2)
        self.assertGreaterEqual(overview['summary']['cooling'], 1)
        self.assertIn('note', overview)
        emails = {a['label'] for a in overview['accounts']}
        self.assertIn('cool', emails)
        self.assertIn('ok', emails)

    def test_overview_exposes_xai_access_expiry_and_email(self):
        expires = int(time.time()) + 3600
        entry = _entry(
            provider=TokenPool.Provider.XAI,
            label='xai-expiry',
            account_email='xai@example.com',
            token=json.dumps({
                'type': 'xai_oauth',
                'email': 'xai@example.com',
                'access_token': _jwt(expires),
                'refresh_token': 'xai-refresh',
            }),
            last_status=TokenEntry.LastStatus.OK,
            active=True,
        )

        overview = tc.build_credential_health_overview(
            owner_scope=TokenPool.objects.filter(owner_kind=TokenPool.OwnerKind.ORG),
        )

        row = next(item for item in overview['accounts'] if item['entry_id'] == str(entry.id))
        self.assertEqual(row['account_email'], 'xai@example.com')
        self.assertEqual(
            int(datetime.fromisoformat(row['access_expires_at']).timestamp()),
            expires,
        )

    def test_overview_excludes_tombstoned_entries(self):
        visible = _entry(
            label='visible',
            token='{"refresh_token":"rt-visible","access_token":"at-visible","email":"visible@b.com"}',
        )
        deleted = _entry(
            label='deleted',
            token='{"refresh_token":"rt-deleted","access_token":"at-deleted","email":"deleted@b.com"}',
        )
        tc.tombstone_token_entry(deleted, actor='operator@example.com')

        overview = tc.build_credential_health_overview(
            owner_scope=TokenPool.objects.filter(owner_kind=TokenPool.OwnerKind.ORG),
        )

        self.assertEqual(overview['summary']['total'], 1)
        self.assertEqual([row['entry_id'] for row in overview['accounts']], [str(visible.id)])

    def test_overview_exposes_exact_openai_reauth_url(self):
        entry = _entry(
            label='reauth-me',
            account_email='reauth@example.com',
            token='{"refresh_token":"rt","access_token":"at","email":"reauth@example.com"}',
        )

        overview = tc.build_credential_health_overview(
            owner_scope=TokenPool.objects.filter(owner_kind=TokenPool.OwnerKind.ORG),
        )

        row = next(item for item in overview['accounts'] if item['entry_id'] == str(entry.id))
        self.assertIn(f'pool={entry.pool_id}', row['codex_reauth_url'])
        self.assertIn(f'entry={entry.id}', row['codex_reauth_url'])
        self.assertIn('email=reauth%40example.com', row['codex_reauth_url'])

    def test_status_report_ignores_tombstoned_entry(self):
        entry = _entry(label='deleted-status')
        tc.tombstone_token_entry(entry, actor='operator@example.com')

        updated = tc.report_token_status(entry.id, status_code=429)

        self.assertFalse(updated)
        entry.refresh_from_db()
        self.assertIsNone(entry.cooldown_until)

    def test_overview_exposes_quota_status(self):
        e = _entry(
            label='quota',
            token='{"refresh_token":"rt-ok","access_token":"at-ok","email":"quota@b.com"}',
            last_status=TokenEntry.LastStatus.OK,
            active=True,
            quota_snapshot={
                'source': 'codex_wham_usage',
                # Aware UTC, matching what refresh_subscription_quota_snapshots
                # writes since HIVE-618. A naive timezone.now() here is Chicago
                # wall-clock (USE_TZ=False) that _quota_fetched_at reads as UTC,
                # making the row look 5h old -> 'stale' and failing this test in
                # any non-UTC environment.
                'fetched_at': datetime.now(dt_timezone.utc).isoformat(),
                'confidence': 'exact',
                'plan': 'plus',
                'detail': 'ok',
                'windows': [
                    {
                        'id': '5h',
                        'label': 'Session (5h)',
                        'left_percent': 44,
                        'resets_at': '2026-07-09T12:00:00+00:00',
                    },
                ],
            },
        )
        overview = tc.build_credential_health_overview(
            owner_scope=TokenPool.objects.filter(owner_kind=TokenPool.OwnerKind.ORG),
        )
        row = next(a for a in overview['accounts'] if a['entry_id'] == str(e.id))
        self.assertEqual(row['quota_status']['status'], 'ok')
        self.assertEqual(row['quota_status']['min_left_percent'], 44)
        self.assertEqual(row['quota_status']['plan'], 'plus')

    def test_overview_exposes_real_broker_lease_activity(self):
        now = timezone.now()
        e = _entry(
            label='leased',
            token='{"refresh_token":"rt-leased","access_token":"at-leased","email":"leased@b.com"}',
            usage_count=0,
        )
        common = {
            'entry': e,
            'broker_identity': 'coordinator-broker',
            'owner_kind': e.pool.owner_kind,
            'owner_id': e.pool.owner_id,
            'provider': e.pool.provider,
        }
        TokenLease.objects.create(**common, expires_at=now - timedelta(seconds=1))
        live = TokenLease.objects.create(**common, expires_at=now + timedelta(minutes=15))

        overview = tc.build_credential_health_overview(
            owner_scope=TokenPool.objects.filter(owner_kind=TokenPool.OwnerKind.ORG),
        )

        row = next(a for a in overview['accounts'] if a['entry_id'] == str(e.id))
        self.assertEqual(row['usage_count'], 0)
        self.assertEqual(row['live_leases'], 1)
        self.assertEqual(row['last_leased_at'], live.issued_at.isoformat())

    def test_overview_replaces_callers_existing_entries_prefetch(self):
        e = _entry(
            label='prefetched',
            token='{"refresh_token":"rt-prefetched","access_token":"at-prefetched","email":"prefetched@b.com"}',
        )
        owner_scope = TokenPool.objects.prefetch_related('entries').filter(
            owner_kind=TokenPool.OwnerKind.ORG,
        )

        overview = tc.build_credential_health_overview(owner_scope=owner_scope)

        self.assertIn(str(e.id), {row['entry_id'] for row in overview['accounts']})


class CredentialHealthAccountIdentityTests(TestCase):
    def test_codex_health_active_with_aware_jwt_expiry(self):
        """Regression: aware JWT exp vs naive now (USE_TZ=False) raised TypeError
        inside entry_observability, so every Codex entry with a valid access
        token fell back to health=unknown."""
        import base64
        import json as jsonlib
        import time

        payload = base64.urlsafe_b64encode(
            jsonlib.dumps({'exp': int(time.time()) + 7 * 24 * 3600}).encode()
        ).decode().rstrip('=')
        jwt = f'eyJhbGciOiJub25lIn0.{payload}.sig'
        e = _entry(
            label='jwt-ok',
            token=jsonlib.dumps({'refresh_token': 'rt', 'access_token': jwt, 'email': 'jwt@b.com'}),
            last_status=TokenEntry.LastStatus.OK,
            active=True,
            cooldown_until=None,
        )
        overview = tc.build_credential_health_overview(
            owner_scope=TokenPool.objects.filter(owner_kind=TokenPool.OwnerKind.ORG),
        )
        row = next(a for a in overview['accounts'] if a['entry_id'] == str(e.id))
        self.assertEqual(row['health'], 'active')
        self.assertIsNotNone(row['access_expires_at'])

    def test_manual_account_email_surfaces_for_anthropic(self):
        e = _entry(
            provider=TokenPool.Provider.ANTHROPIC,
            label='cl-manual',
            token='sk-ant-oat01-manual-test',
            last_status=TokenEntry.LastStatus.OK,
            active=True,
            account_email='claude-owner@shizuha.com',
        )
        overview = tc.build_credential_health_overview(
            owner_scope=TokenPool.objects.filter(owner_kind=TokenPool.OwnerKind.ORG),
        )
        row = next(a for a in overview['accounts'] if a['entry_id'] == str(e.id))
        self.assertEqual(row['account_email'], 'claude-owner@shizuha.com')

    def test_claude_scope_403_reports_limited_token(self):
        # c4fcb58: `claude setup-token` OATs are inference-only — the usage API
        # needs user:profile, which they structurally cannot carry. A scope-403
        # is a CAPABILITY gap (neutral 'limited_token', confidence unsupported),
        # NOT needs_reauth: re-authing mints the same inference-only token and
        # the old red badge sent operators on pointless re-auth loops.
        from . import quota_fetchers

        class Resp:
            status_code = 403
            text = '{"error":{"type":"permission_error","message":"OAuth token does not meet scope requirement user:profile"}}'

            def json(self):
                import json as jsonlib
                return jsonlib.loads(self.text)

        with patch('hive_platform.quota_fetchers.requests.get', return_value=Resp()):
            snap = quota_fetchers.fetch_claude_oauth_usage('sk-ant-oat01-test')
        self.assertTrue(snap['detail'].startswith('limited_token'))
        self.assertEqual(snap['confidence'], 'unsupported')


class CheckEntryHealthTests(TestCase):
    @patch('hive_platform.quota_fetchers.fetch_codex_wham_usage')
    @patch('hive_platform.token_coordinator.refresh_openai_oauth')
    def test_openai_refresh_ok_clears_cool(self, mock_refresh, mock_usage):
        now = timezone.now()
        e = _entry(
            cooldown_until=now + timedelta(minutes=10),
            last_status=TokenEntry.LastStatus.RATE_LIMITED,
            active=True,
        )
        mock_refresh.return_value = {
            'access_token': 'new-at',
            'refresh_token': 'new-rt',
            'id_token': 'idt',
        }
        mock_usage.return_value = {
            'confidence': 'partial',
            'detail': 'empty_windows',
            'windows': [],
        }
        out = tc.check_entry_health(e, force_refresh=True)
        self.assertEqual(out['outcome'], 'ok')
        e.refresh_from_db()
        self.assertIsNone(e.cooldown_until)
        self.assertEqual(e.last_status, TokenEntry.LastStatus.OK)

    @patch('hive_platform.quota_fetchers.fetch_codex_wham_usage')
    @patch('hive_platform.token_coordinator.refresh_openai_oauth')
    def test_openai_fresh_health_check_does_not_rotate_refresh_token(
        self,
        mock_refresh,
        mock_usage,
    ):
        import base64
        import json as jsonlib
        import time

        body = base64.urlsafe_b64encode(jsonlib.dumps({
            'exp': int(time.time()) + 3600,
            'https://api.openai.com/auth': {
                'chatgpt_account_id': 'acct-health',
            },
        }).encode()).decode().rstrip('=')
        access = f'h.{body}.s'
        e = _entry(
            token=jsonlib.dumps({
                'access_token': access,
                'refresh_token': 'generation-must-not-rotate',
                'account_id': 'acct-health',
                'email': 'health@example.com',
            }),
            last_status=TokenEntry.LastStatus.OK,
            active=True,
        )
        mock_usage.return_value = {
            'confidence': 'exact',
            'detail': 'ok',
            'windows': [],
        }

        first = tc.check_entry_health(e)
        e.refresh_from_db()
        second = tc.check_entry_health(e)

        self.assertEqual(first['outcome'], 'ok')
        self.assertEqual(second['outcome'], 'ok')
        self.assertEqual(first['detail'], 'token_ok')
        self.assertEqual(second['detail'], 'token_ok')
        mock_refresh.assert_not_called()
        self.assertEqual(mock_usage.call_count, 2)
        self.assertEqual(
            jsonlib.loads(e.get_token())['refresh_token'],
            'generation-must-not-rotate',
        )

    @patch('hive_platform.quota_fetchers.fetch_grok_usage')
    @patch('hive_platform.token_coordinator.refresh_xai_oauth')
    def test_xai_fresh_health_check_replaces_stale_reauth_quota_without_rotating(
        self,
        mock_refresh,
        mock_usage,
    ):
        """Device reauth -> Check now must publish the new token's quota."""
        import json as jsonlib

        e = _entry(
            provider=TokenPool.Provider.XAI,
            label='reauthenticated-grok',
            token=jsonlib.dumps({
                'type': 'xai_oauth',
                'access_token': 'fresh-access',
                'refresh_token': 'fresh-generation-must-not-rotate',
                'expires_at': '2099-01-01T00:00:00+00:00',
            }),
            quota_snapshot={
                'source': TokenPool.Provider.XAI,
                'confidence': 'error',
                'detail': 'oauth_refresh_failed:XaiOAuthRefreshError',
                'windows': [],
            },
            last_status=TokenEntry.LastStatus.OK,
            active=True,
        )
        mock_usage.return_value = {
            'source': 'grok_xai_usage',
            'confidence': 'ok',
            'detail': 'ok_via_grok_billing',
            'windows': [{
                'id': 'credits',
                'label': 'Credits',
                'used_percent': 10.0,
                'left_percent': 90.0,
                'resets_at': '2099-01-06T00:00:00+00:00',
            }],
        }

        out = tc.check_entry_health(e)
        e.refresh_from_db()

        self.assertEqual(out['outcome'], 'ok')
        self.assertEqual(out['quota']['windows'][0]['left_percent'], 90.0)
        # The V1 projection preserves the successful quota data while
        # canonicalising fetcher-specific detail strings to the bounded enum.
        self.assertEqual(e.quota_snapshot['detail'], 'ok')
        mock_usage.assert_called_once_with('fresh-access')
        mock_refresh.assert_not_called()
        self.assertEqual(
            jsonlib.loads(e.get_token())['refresh_token'],
            'fresh-generation-must-not-rotate',
        )

    @patch('hive_platform.token_coordinator.refresh_xai_oauth')
    def test_xai_invalid_grant_400_is_terminal_auth_failed_not_rate_limited(
        self,
        mock_refresh,
    ):
        """An xAI refresh returning HTTP 400 invalid_grant means the stored
        refresh grant is terminally dead (revoked/rotated). It must be marked
        auth_failed (re-auth required), NOT retried forever as a 5-minute
        'cooling' rate-limit — the behaviour that made a dead hothritik1 grok
        account show a perpetual 'oauth refresh failed' error in the UI."""
        import json as jsonlib

        e = _entry(
            provider=TokenPool.Provider.XAI,
            label='xai-invalid-grant',
            token=jsonlib.dumps({
                'type': 'xai_oauth',
                'access_token': 'at',
                'refresh_token': 'rt-dead',
                'expires_at': '2000-01-01T00:00:00+00:00',  # expired -> needs refresh
            }),
            last_status=TokenEntry.LastStatus.OK,
            active=True,
            cooldown_until=None,
        )
        mock_refresh.side_effect = tc.XaiOAuthRefreshError(
            'xai oauth refresh failed: HTTP 400 (invalid_grant)',
            status_code=400,
            error_code='invalid_grant',
        )

        out = tc.check_entry_health(e)
        e.refresh_from_db()

        self.assertEqual(out['outcome'], 'auth_failed')
        self.assertEqual(out['detail'], 'xai_refresh_invalid_grant')
        self.assertEqual(e.last_status, TokenEntry.LastStatus.AUTH_FAILED)
        self.assertFalse(e.active)

    def test_late_xai_refresh_failure_cannot_kill_reauthenticated_generation(self):
        e = _entry(
            provider=TokenPool.Provider.XAI,
            label='xai-reauth-race',
            token=json.dumps({
                'type': 'xai_oauth',
                'access_token': 'old-access',
                'refresh_token': 'old-generation',
                'expires_at': '2000-01-01T00:00:00+00:00',
            }),
            last_status=TokenEntry.LastStatus.OK,
            active=True,
        )
        stale = TokenEntry.objects.select_related('pool').get(pk=e.pk)

        current = TokenEntry.objects.get(pk=e.pk)
        current.set_token(json.dumps({
            'type': 'xai_oauth',
            'access_token': 'new-access',
            'refresh_token': 'new-generation',
            'expires_at': '2099-01-01T00:00:00+00:00',
        }))
        current.active = True
        current.last_status = TokenEntry.LastStatus.OK
        current.cooldown_until = None
        current.save()

        exc = tc.XaiOAuthRefreshError(
            'xai oauth refresh failed: HTTP 400 (invalid_grant)',
            status_code=400,
            error_code='invalid_grant',
        )
        exc.credential_fingerprint = stale.token_fingerprint

        outcome = tc.apply_xai_refresh_failure(stale, exc)

        self.assertEqual(outcome, 'generation_changed')
        current.refresh_from_db()
        self.assertTrue(current.active)
        self.assertEqual(current.last_status, TokenEntry.LastStatus.OK)
        self.assertIsNone(current.cooldown_until)
        self.assertEqual(
            json.loads(current.get_token())['refresh_token'],
            'new-generation',
        )

    def test_late_xai_quota_result_cannot_poison_reauthenticated_generation(self):
        from .quota_fetchers import persist_quota_snapshot

        e = _entry(
            provider=TokenPool.Provider.XAI,
            label='xai-quota-race',
            token=json.dumps({
                'type': 'xai_oauth',
                'access_token': 'old-access',
                'refresh_token': 'old-generation',
            }),
            quota_snapshot={'confidence': 'error', 'detail': 'auth_failed'},
            last_status=TokenEntry.LastStatus.OK,
            active=True,
        )
        stale = TokenEntry.objects.get(pk=e.pk)
        old_fingerprint = stale.token_fingerprint

        current = TokenEntry.objects.get(pk=e.pk)
        current.set_token(json.dumps({
            'type': 'xai_oauth',
            'access_token': 'new-access',
            'refresh_token': 'new-generation',
        }))
        current.quota_snapshot = {}
        current.save()

        persisted = persist_quota_snapshot(
            stale,
            {
                'source': 'grok_xai_usage',
                'confidence': 'error',
                'detail': 'auth_failed',
                'windows': [],
            },
            expected_fingerprint=old_fingerprint,
        )

        self.assertFalse(persisted)
        current.refresh_from_db()
        self.assertEqual(current.quota_snapshot, {})
        self.assertEqual(
            json.loads(current.get_token())['refresh_token'],
            'new-generation',
        )

    @patch('hive_platform.token_coordinator.refresh_xai_oauth')
    def test_xai_transient_400_keeps_rate_limited_cooldown(self, mock_refresh):
        """A non-invalid_grant 400 (e.g. bad client config) must NOT destroy a
        valid grant — it stays a transient cooldown, mirroring the OpenAI path."""
        import json as jsonlib

        e = _entry(
            provider=TokenPool.Provider.XAI,
            label='xai-transient-400',
            token=jsonlib.dumps({
                'type': 'xai_oauth',
                'access_token': 'at',
                'refresh_token': 'rt',
                'expires_at': '2000-01-01T00:00:00+00:00',
            }),
            last_status=TokenEntry.LastStatus.OK,
            active=True,
            cooldown_until=None,
        )
        mock_refresh.side_effect = tc.XaiOAuthRefreshError(
            'xai oauth refresh failed: HTTP 400 (invalid_client)',
            status_code=400,
            error_code='invalid_client',
        )

        out = tc.check_entry_health(e)
        e.refresh_from_db()

        self.assertEqual(out['outcome'], 'rate_limited')
        self.assertEqual(e.last_status, TokenEntry.LastStatus.RATE_LIMITED)
        self.assertTrue(e.active)

    @patch('hive_platform.token_coordinator.refresh_xai_oauth')
    def test_background_refresh_xai_invalid_grant_is_auth_failed(self, mock_refresh):
        """The background quota-refresh sweep must also mark a dead xAI
        invalid_grant grant auth_failed rather than looping as rate_limited."""
        import json as jsonlib

        e = _entry(
            provider=TokenPool.Provider.XAI,
            label='xai-bg-invalid-grant',
            token=jsonlib.dumps({
                'type': 'xai_oauth',
                'access_token': 'at',
                'refresh_token': 'rt-dead',
                'expires_at': '2000-01-01T00:00:00+00:00',
            }),
            last_status=TokenEntry.LastStatus.OK,
            active=True,
            cooldown_until=None,
            quota_snapshot={'confidence': 'error', 'windows': []},
        )
        mock_refresh.side_effect = tc.XaiOAuthRefreshError(
            'xai oauth refresh failed: HTTP 400 (invalid_grant)',
            status_code=400,
            error_code='invalid_grant',
        )

        tc.refresh_entry_quota_snapshot(e, force=True, refresh_oauth=True)
        e.refresh_from_db()

        self.assertEqual(e.last_status, TokenEntry.LastStatus.AUTH_FAILED)
        self.assertFalse(e.active)
        self.assertEqual(e.quota_snapshot['source'], 'grok_xai_usage')
        self.assertEqual(e.quota_snapshot['detail'], 'oauth_refresh_failed')
        self.assertTrue(tc.confirmed_xai_auth_unavailable(e))

    @patch('hive_platform.token_coordinator.probe_token_health')
    @patch('hive_platform.quota_fetchers.fetch_claude_oauth_usage')
    def test_claude_oat_uses_usage_endpoint_not_chat_probe(self, mock_usage, mock_probe):
        """OAT tokens must not hit Anthropic chat probe; usage endpoint is allowed."""
        mock_usage.return_value = {
            'confidence': 'partial',
            'detail': 'usage_partial',
            'windows': [],
        }
        e = _entry(
            provider=TokenPool.Provider.ANTHROPIC,
            label='oat',
            token='sk-ant-oat01-deadbeef',
            active=True,
        )
        out = tc.check_entry_health(e)
        # Usage path is supported (CodexBar); chat probe must not run.
        self.assertTrue(out['probe_supported'])
        mock_probe.assert_not_called()
        mock_usage.assert_called_once()
        self.assertIn(out['outcome'], ('unknown', 'cooling', 'disabled', 'ok', 'auth_failed'))


class RedeemEntryResetCreditTests(TestCase):
    @patch('hive_platform.token_coordinator.refresh_entry_quota_snapshot')
    @patch('hive_platform.quota_fetchers.redeem_codex_reset_credit')
    def test_summary_credit_can_redeem_without_detail_id(self, mock_redeem, mock_refresh):
        e = _entry(
            label='summary-only-reset',
            token='{"access_token":"opaque-at","account_id":"acc"}',
            quota_snapshot={'reset_credits': {'available': 1}},
        )
        mock_redeem.return_value = {
            'ok': True, 'detail': 'ok', 'windows_reset': 2,
        }

        out = tc.redeem_entry_reset_credit(e)

        self.assertTrue(out['ok'])
        self.assertEqual(out['windows_reset'], 2)
        mock_redeem.assert_called_once_with('opaque-at', account_id='acc', credit_id='')
        mock_refresh.assert_called_once_with(e, force=True)

    @patch('hive_platform.quota_fetchers.redeem_codex_reset_credit')
    def test_zero_summary_credit_never_calls_consume(self, mock_redeem):
        e = _entry(
            label='no-reset',
            token='{"access_token":"opaque-at","account_id":"acc"}',
            quota_snapshot={'reset_credits': {'available': 0}},
        )

        out = tc.redeem_entry_reset_credit(e)

        self.assertFalse(out['ok'])
        self.assertEqual(out['detail'], 'no_available_reset_credit')
        mock_redeem.assert_not_called()


class SubscriptionQuotaRefreshTests(TestCase):
    def test_background_refresh_skips_tombstoned_entries(self):
        entry = _entry(label='deleted-quota')
        tc.tombstone_token_entry(entry, actor='operator@example.com')

        summary = tc.refresh_subscription_quota_snapshots(force=True, dry_run=True)

        self.assertEqual(summary['quota_checked'], 0)
        self.assertEqual(summary['quota_updated'], 0)

    def test_background_refresh_uses_utc_when_django_time_is_naive(self):
        fetched_at = datetime.now(dt_timezone.utc) - timedelta(minutes=11)
        _entry(
            label='stale-quota',
            active=True,
            quota_snapshot={
                'fetched_at': fetched_at.isoformat(),
                'confidence': 'exact',
                'detail': 'ok',
                'windows': [],
            },
        )

        summary = tc.refresh_subscription_quota_snapshots(
            dry_run=True,
            stale_after_seconds=600,
        )

        self.assertEqual(summary['quota_checked'], 1)
        self.assertEqual(summary['quota_updated'], 1)
        self.assertEqual(summary['quota_skipped_fresh'], 0)

    def test_background_refresh_invalidates_old_codex_parser_schema(self):
        _entry(
            label='old-codex-quota-schema',
            active=True,
            quota_snapshot={
                'source': 'codex_wham_usage',
                'fetched_at': datetime.now(dt_timezone.utc).isoformat(),
                'confidence': 'exact',
                'detail': 'ok',
                'windows': [{'id': '5h', 'label': 'Session (5h)', 'left_percent': 39}],
            },
        )

        summary = tc.refresh_subscription_quota_snapshots(dry_run=True)

        self.assertEqual(summary['quota_checked'], 1)
        self.assertEqual(summary['quota_updated'], 1)
        self.assertEqual(summary['quota_skipped_fresh'], 0)

    def test_overview_marks_old_utc_snapshot_stale(self):
        fetched_at = datetime.now(dt_timezone.utc) - timedelta(minutes=31)
        e = _entry(
            label='stale-overview',
            active=True,
            quota_snapshot={
                'fetched_at': fetched_at.isoformat(),
                'confidence': 'exact',
                'detail': 'ok',
                'windows': [],
            },
        )

        overview = tc.build_credential_health_overview(
            owner_scope=TokenPool.objects.filter(owner_kind=TokenPool.OwnerKind.ORG),
        )

        row = next(a for a in overview['accounts'] if a['entry_id'] == str(e.id))
        self.assertEqual(row['quota_status']['status'], 'stale')

    @patch('hive_platform.token_coordinator.refresh_openai_oauth')
    @patch('hive_platform.quota_fetchers.fetch_codex_wham_usage')
    def test_refresh_subscription_quota_snapshots_updates_openai_entry(self, mock_usage, mock_refresh):
        e = _entry(
            label='codex-quota',
            token='{"refresh_token":"rt","access_token":"opaque-at","email":"codex@b.com"}',
            active=True,
            last_status=TokenEntry.LastStatus.UNKNOWN,
        )
        mock_usage.return_value = {
            'source': 'codex_wham_usage',
            'fetched_at': timezone.now().isoformat(),
            'confidence': 'exact',
            'plan': 'team',
            'detail': 'ok',
            'windows': [{'id': '5h', 'label': 'Session (5h)', 'left_percent': 72}],
        }
        mock_refresh.return_value = {
            'access_token': 'fresh-at',
            'refresh_token': 'rt',
            'id_token': 'idt',
        }

        summary = tc.refresh_subscription_quota_snapshots(force=True)

        self.assertEqual(summary['quota_checked'], 1)
        self.assertEqual(summary['quota_updated'], 1)
        e.refresh_from_db()
        self.assertEqual(e.quota_snapshot['plan'], 'team')
        self.assertEqual(e.quota_snapshot['windows'][0]['left_percent'], 72)
        self.assertEqual(e.last_status, TokenEntry.LastStatus.OK)

    @patch('hive_platform.quota_fetchers.fetch_claude_oauth_usage')
    def test_refresh_subscription_quota_snapshots_updates_claude_oat(self, mock_usage):
        e = _entry(
            provider=TokenPool.Provider.ANTHROPIC,
            label='claude-quota',
            token='sk-ant-oat01-test',
            active=True,
            last_status=TokenEntry.LastStatus.UNKNOWN,
        )
        mock_usage.return_value = {
            'source': 'claude_oauth_usage',
            'fetched_at': timezone.now().isoformat(),
            'confidence': 'exact',
            'plan': 'max',
            'detail': 'ok',
            'windows': [{'id': 'weekly', 'label': 'Weekly', 'left_percent': 18}],
        }

        summary = tc.refresh_subscription_quota_snapshots(
            provider=TokenPool.Provider.ANTHROPIC,
            force=True,
        )

        self.assertEqual(summary['quota_checked'], 1)
        e.refresh_from_db()
        self.assertEqual(e.quota_snapshot['plan'], 'max')
        self.assertEqual(e.last_status, TokenEntry.LastStatus.OK)

    @patch('hive_platform.quota_fetchers.fetch_claude_oauth_usage')
    def test_quota_failure_summary_uses_only_opaque_id_and_safe_code(self, mock_usage):
        e = _entry(
            provider=TokenPool.Provider.ANTHROPIC,
            label='account-secret@example.com',
            token='sk-ant-oat01-test',
        )
        mock_usage.return_value = {
            'source': 'claude_oauth_usage',
            'fetched_at': datetime.now(dt_timezone.utc).isoformat(),
            'confidence': 'error',
            'detail': 'http_500:upstream-secret',
            'windows': [],
        }

        summary = tc.refresh_subscription_quota_snapshots(
            provider=TokenPool.Provider.ANTHROPIC,
            force=True,
        )

        self.assertEqual(summary['quota_errors'], [f'{e.id}:http_500'])
        self.assertNotIn(e.label, str(summary))
        self.assertNotIn('upstream-secret', str(summary))

    @patch('hive_platform.quota_fetchers.fetch_claude_oauth_usage')
    def test_background_refresh_covers_inactive_stale_entries(self, mock_usage):
        """Disabled rows still appear on Subscriptions — auto-refresh must not skip them.

        Regression: active=True filter left cooled/disabled accounts stale until
        a human clicked Check now (operator report 2026-07-16).
        """
        e = _entry(
            provider=TokenPool.Provider.ANTHROPIC,
            label='inactive-stale',
            token='sk-ant-oat01-inactive',
            active=False,
            last_status=TokenEntry.LastStatus.RATE_LIMITED,
            cooldown_until=timezone.now() + timedelta(hours=48),
            quota_snapshot={
                'fetched_at': (datetime.now(dt_timezone.utc) - timedelta(hours=6)).isoformat(),
                'confidence': 'partial',
                'detail': 'rate_limited_usage_endpoint',
                'windows': [],
            },
        )
        mock_usage.return_value = {
            'source': 'claude_oauth_usage',
            'fetched_at': datetime.now(dt_timezone.utc).isoformat(),
            'confidence': 'exact',
            'plan': 'max',
            'detail': 'ok',
            'windows': [{'id': 'weekly', 'label': 'Weekly', 'left_percent': 55}],
        }

        summary = tc.refresh_subscription_quota_snapshots(
            provider=TokenPool.Provider.ANTHROPIC,
            stale_after_seconds=600,
        )

        self.assertEqual(summary['quota_checked'], 1)
        self.assertEqual(summary['quota_updated'], 1)
        e.refresh_from_db()
        self.assertEqual(e.quota_snapshot['detail'], 'ok')
        self.assertEqual(e.quota_snapshot['windows'][0]['left_percent'], 55)
        # Recovery cool present → lease state may mutate on exact usage ok.
        self.assertTrue(e.active)
        self.assertEqual(e.last_status, TokenEntry.LastStatus.OK)

    @patch('hive_platform.quota_fetchers.fetch_claude_oauth_usage')
    def test_operator_disabled_snapshot_only_never_reenabled(self, mock_usage):
        """Operator-disabled (active=False, no cool stamp) must not auto-re-enable."""
        e = _entry(
            provider=TokenPool.Provider.ANTHROPIC,
            label='op-disabled',
            token='sk-ant-oat01-op-off',
            active=False,
            last_status=TokenEntry.LastStatus.OK,
            cooldown_until=None,
            quota_snapshot={
                'fetched_at': (datetime.now(dt_timezone.utc) - timedelta(hours=2)).isoformat(),
                'confidence': 'partial',
                'detail': 'stale_old',
                'windows': [],
            },
        )
        mock_usage.return_value = {
            'source': 'claude_oauth_usage',
            'fetched_at': datetime.now(dt_timezone.utc).isoformat(),
            'confidence': 'exact',
            'plan': 'max',
            'detail': 'ok',
            'windows': [{'id': 'weekly', 'label': 'Weekly', 'left_percent': 90}],
        }

        summary = tc.refresh_subscription_quota_snapshots(
            provider=TokenPool.Provider.ANTHROPIC,
            stale_after_seconds=600,
        )

        self.assertEqual(summary['quota_updated'], 1)
        e.refresh_from_db()
        self.assertEqual(e.quota_snapshot['detail'], 'ok')
        self.assertFalse(e.active)  # still operator-disabled

    def test_unsupported_confidence_not_refreshed_every_tick(self):
        e = _entry(
            provider=TokenPool.Provider.ANTHROPIC,
            label='inference-only',
            token='sk-ant-oat01-limited',
            active=True,
            quota_snapshot={
                'fetched_at': (datetime.now(dt_timezone.utc) - timedelta(hours=2)).isoformat(),
                'confidence': 'unsupported',
                'detail': 'limited_token: inference-only OAT (no quota API)',
                'windows': [],
            },
        )
        summary = tc.refresh_subscription_quota_snapshots(
            provider=TokenPool.Provider.ANTHROPIC,
            dry_run=True,
            stale_after_seconds=600,
        )
        self.assertEqual(summary['quota_checked'], 0)
        self.assertEqual(summary['quota_skipped_fresh'], 1)
        e.refresh_from_db()
        self.assertEqual(e.quota_snapshot['confidence'], 'unsupported')

    def test_clear_elapsed_rate_limit_cooldowns_normalizes_status(self):
        e = _entry(
            provider=TokenPool.Provider.ANTHROPIC,
            label='elapsed-cool',
            token='sk-ant-oat01-elapsed',
            active=True,
            last_status=TokenEntry.LastStatus.RATE_LIMITED,
            cooldown_until=timezone.now() - timedelta(days=2),
        )
        out = tc.clear_elapsed_rate_limit_cooldowns()
        self.assertEqual(out['cleared'], 1)
        e.refresh_from_db()
        self.assertIsNone(e.cooldown_until)
        self.assertEqual(e.last_status, TokenEntry.LastStatus.OK)
