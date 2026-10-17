"""Claude/Anthropic OAuth code-paste flow → org TokenPool ingest + refresh."""
import json
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from . import claude_oauth
from hive_platform.models import TokenEntry, TokenPool


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=''):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


@override_settings(
    HIVE_TOKEN_COORDINATOR_KEK='unit-test-kek',
    HIVE_TOKEN_COORDINATOR_KEK_ID='unit-v1',
    CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
)
class ClaudeOAuthFlowTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_start_login_mints_pkce_and_official_authorize_url(self):
        data = claude_oauth.start_login()
        self.assertIn('https://claude.ai/oauth/authorize?', data['authorize_url'])
        self.assertIn(claude_oauth.CLAUDE_OAUTH_CLIENT_ID, data['authorize_url'])
        self.assertIn('user%3Aprofile', data['authorize_url'])
        self.assertIn('code_challenge=', data['authorize_url'])
        # state → verifier cached for the exchange step
        verifier = cache.get(f'hive:claude-oauth:state:{data["state"]}')
        self.assertTrue(verifier)

    def test_exchange_accepts_code_hash_state_paste(self):
        started = claude_oauth.start_login()
        with patch('hive_platform.claude_oauth.requests.post', return_value=FakeResponse(200, {
            'access_token': 'sk-ant-oat01-new', 'refresh_token': 'sk-ant-ort01-new',
            'expires_in': 28800, 'scope': claude_oauth.SCOPES,
        })) as mock_post:
            tokens = claude_oauth.exchange_code(f'authcode#{started["state"]}')
        self.assertEqual(tokens['access_token'], 'sk-ant-oat01-new')
        body = mock_post.call_args.kwargs['json']
        self.assertEqual(body['grant_type'], 'authorization_code')
        self.assertEqual(body['redirect_uri'], claude_oauth.REDIRECT_URI)
        self.assertTrue(body['code_verifier'])
        # state is single-use
        with self.assertRaises(claude_oauth.ClaudeOAuthError):
            claude_oauth.exchange_code(f'authcode#{started["state"]}')

    def test_exchange_expired_state_fails_closed(self):
        with self.assertRaises(claude_oauth.ClaudeOAuthError):
            claude_oauth.exchange_code('authcode#never-started')

    def test_ingest_upserts_org_entry_with_email_and_bundle(self):
        with patch('hive_platform.quota_fetchers.requests.get', return_value=FakeResponse(200, {
            'account': {'email_address': 'claude-owner@shizuha.com'},
        })):
            ingested = claude_oauth.ingest_claude_account({
                'access_token': 'sk-ant-oat01-full', 'refresh_token': 'sk-ant-ort01-full',
                'expires_in': 28800,
            })
        self.assertEqual(ingested['email'], 'claude-owner@shizuha.com')
        entry = TokenEntry.objects.get(id=ingested['entry_id'])
        self.assertEqual(entry.pool.provider, TokenPool.Provider.ANTHROPIC)
        self.assertEqual(entry.account_email, 'claude-owner@shizuha.com')
        bundle = json.loads(entry.get_token())
        self.assertEqual(bundle['access_token'], 'sk-ant-oat01-full')
        self.assertEqual(bundle['refresh_token'], 'sk-ant-ort01-full')
        self.assertIn('user:profile', bundle['scopes'])
        # re-ingest rotates in place, no duplicate entry
        with patch('hive_platform.quota_fetchers.requests.get', return_value=FakeResponse(200, {
            'account': {'email_address': 'claude-owner@shizuha.com'},
        })):
            again = claude_oauth.ingest_claude_account({
                'access_token': 'sk-ant-oat01-full2', 'refresh_token': 'sk-ant-ort01-full2',
                'expires_in': 28800,
            })
        self.assertFalse(again['created'])
        self.assertEqual(again['entry_id'], ingested['entry_id'])

    def test_refresh_rotates_bundle(self):
        with patch('hive_platform.claude_oauth.requests.post', return_value=FakeResponse(200, {
            'access_token': 'sk-ant-oat01-rotated', 'refresh_token': 'sk-ant-ort01-rotated',
            'expires_in': 28800,
        })):
            tokens = claude_oauth.refresh_claude_oauth({'refresh_token': 'sk-ant-ort01-old'})
        self.assertEqual(tokens['access_token'], 'sk-ant-oat01-rotated')

    def test_refresh_rejected_raises(self):
        with patch('hive_platform.claude_oauth.requests.post', return_value=FakeResponse(400, {}, text='invalid_grant')):
            with self.assertRaises(claude_oauth.ClaudeOAuthError):
                claude_oauth.refresh_claude_oauth({'refresh_token': 'sk-ant-ort01-dead'})

    def test_views_are_staff_gated(self):
        from .authentication import FederatedUser

        client = APIClient()
        user = FederatedUser(user_id=7, email='pleb@example.com', username='pleb', is_staff=False)
        client.force_authenticate(user=user)
        self.assertEqual(client.post('/api/v1/claude-oauth/start/').status_code, 403)
        self.assertEqual(client.post('/api/v1/claude-oauth/exchange/', {'code': 'x#y'}).status_code, 403)


@override_settings(
    HIVE_TOKEN_COORDINATOR_KEK='unit-test-kek',
    HIVE_TOKEN_COORDINATOR_KEK_ID='unit-v1',
)
class ClaudeBundleRefreshInQuotaPathTests(TestCase):
    def _bundle_entry(self, *, expires_at='2020-01-01T00:00:00+00:00'):
        pool, _ = TokenPool.objects.get_or_create(
            owner_kind=TokenPool.OwnerKind.ORG,
            owner_id=TokenPool.ORG_OWNER_ID,
            provider=TokenPool.Provider.ANTHROPIC,
        )
        e = TokenEntry(pool=pool, label='org-claude-x@y.z', priority=100)
        e.set_token(json.dumps({
            'type': 'claude_oauth', 'email': 'x@y.z',
            'access_token': 'sk-ant-oat01-stale', 'refresh_token': 'sk-ant-ort01-live',
            'expires_at': expires_at,
        }))
        e.active = True
        e.save()
        return e

    def test_expired_bundle_refreshes_before_usage_fetch(self):
        from . import token_coordinator as tc

        e = self._bundle_entry()
        with patch('hive_platform.claude_oauth.requests.post', return_value=FakeResponse(200, {
            'access_token': 'sk-ant-oat01-fresh', 'refresh_token': 'sk-ant-ort01-next',
            'expires_in': 28800,
        })), patch('hive_platform.quota_fetchers.fetch_claude_oauth_usage', return_value={
            'source': 'claude_oauth_usage', 'fetched_at': '2026-07-10T00:00:00+00:00',
            'confidence': 'exact', 'plan': 'max', 'detail': 'ok',
            'windows': [{'id': '5h', 'label': 'Session (5h)', 'left_percent': 80, 'resets_at': None}],
        }) as mock_usage, patch(
            'hive_platform.quota_fetchers.fetch_claude_profile_email', return_value='x@y.z',
        ):
            tc.refresh_entry_quota_snapshot(e, force=True)
        # usage fetched with the REFRESHED access token
        mock_usage.assert_called_with('sk-ant-oat01-fresh')
        e.refresh_from_db()
        bundle = json.loads(e.get_token())
        self.assertEqual(bundle['access_token'], 'sk-ant-oat01-fresh')
        self.assertEqual(bundle['refresh_token'], 'sk-ant-ort01-next')
        self.assertEqual(e.last_status, TokenEntry.LastStatus.OK)

    def test_claudeai_oauth_nested_shape_detected_for_refresh(self):
        from .token_coordinator import _anthropic_oauth_payload, _claude_needs_refresh

        payload = _anthropic_oauth_payload(json.dumps({
            'claudeAiOauth': {
                'accessToken': 'sk-ant-oat01-cli', 'refreshToken': 'sk-ant-ort01-cli',
                'expiresAt': 1600000000000,  # epoch ms, long expired
            },
        }))
        self.assertIsNotNone(payload)
        self.assertTrue(_claude_needs_refresh(payload))

    def test_bare_oat_entry_is_not_refreshable(self):
        from .token_coordinator import _anthropic_oauth_payload

        self.assertIsNone(_anthropic_oauth_payload('sk-ant-oat01-bare'))
