"""HIVE-581: staff-only browser device-code auth for fleet Codex accounts.

Replaces the host-shell ``shizuha auth codex`` path: the flow mints a fresh
refresh token from the Hive credentials page and ingests it as an
envelope-encrypted TokenEntry in the platform owner's OpenAI/Codex pool, which the
coordinator then leases/refreshes fleet-wide.
"""
import base64
import json
from unittest.mock import patch

from django.test import TestCase, override_settings

from .codex_device_auth import (
    CodexDeviceAuthError, CodexDeviceAuthTargetMismatch, exchange_authorization_code,
    ingest_codex_account, poll_device_token, request_device_code, resolve_account_email,
)
from hive_platform.models import TokenEntry, TokenPool


def _jwt_with(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    return f'h.{body}.s'


class FakeResponse:
    def __init__(self, status_code=200, payload=None, *, json_error=None):
        self.status_code = status_code
        self._payload = payload or {}
        self._json_error = json_error

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        if self._json_error:
            raise self._json_error
        return self._payload


class DeviceCodeFlowTests(TestCase):
    def test_request_device_code_returns_user_code_and_page(self):
        fake = FakeResponse(200, {'user_code': 'ABCD-1234', 'device_auth_id': 'dev-1', 'interval': 5})
        with patch('claws.codex_device_auth.requests.post', return_value=fake):
            data = request_device_code()
        self.assertEqual(data['user_code'], 'ABCD-1234')
        self.assertEqual(data['device_auth_id'], 'dev-1')
        self.assertEqual(data['interval'], 5)
        self.assertEqual(data['expires_in'], 15 * 60)
        self.assertIn('auth.openai.com', data['verification_url'])

    def test_request_device_code_missing_fields_raises(self):
        with patch('claws.codex_device_auth.requests.post', return_value=FakeResponse(200, {})):
            with self.assertRaises(CodexDeviceAuthError):
                request_device_code()

    def test_request_device_code_non_json_response_raises_safe_error(self):
        fake = FakeResponse(200, json_error=ValueError('html challenge body'))
        with patch('claws.codex_device_auth.requests.post', return_value=fake):
            with self.assertRaisesRegex(
                CodexDeviceAuthError, r'invalid response \(HTTP 200\)',
            ):
                request_device_code()

    def test_poll_pending_and_slow_down_and_authorized(self):
        pending = FakeResponse(200, {'error': 'authorization_pending'})
        with patch('claws.codex_device_auth.requests.post', return_value=pending):
            self.assertEqual(poll_device_token('d', 'u')['status'], 'pending')

        with patch('claws.codex_device_auth.requests.post', return_value=FakeResponse(429)):
            self.assertEqual(poll_device_token('d', 'u')['status'], 'slow_down')

        # Current Codex treats 403 and 404 as "not yet authorized".
        with patch('claws.codex_device_auth.requests.post', return_value=FakeResponse(404)):
            self.assertEqual(poll_device_token('d', 'u')['status'], 'pending')

        authorized = FakeResponse(200, {'authorization_code': 'authz-1', 'code_verifier': 'ver-1'})
        with patch('claws.codex_device_auth.requests.post', return_value=authorized):
            result = poll_device_token('d', 'u')
        self.assertEqual(result['status'], 'authorized')
        self.assertEqual(result['authorization_code'], 'authz-1')
        self.assertEqual(result['code_verifier'], 'ver-1')

    def test_poll_non_pending_http_error_is_terminal_and_keeps_safe_code(self):
        denied = FakeResponse(401, {'error': 'authorization_declined'})
        with patch('claws.codex_device_auth.requests.post', return_value=denied):
            with self.assertRaisesRegex(
                CodexDeviceAuthError,
                r'HTTP 401 \(authorization_declined\)',
            ):
                poll_device_token('d', 'u')

    def test_poll_non_json_success_response_raises_safe_error(self):
        fake = FakeResponse(200, json_error=ValueError('cloudflare html'))
        with patch('claws.codex_device_auth.requests.post', return_value=fake):
            with self.assertRaisesRegex(
                CodexDeviceAuthError, r'invalid response \(HTTP 200\)',
            ):
                poll_device_token('d', 'u')

    def test_poll_terminal_error_raises(self):
        denied = FakeResponse(200, {'error': 'access_denied'})
        with patch('claws.codex_device_auth.requests.post', return_value=denied):
            with self.assertRaises(CodexDeviceAuthError):
                poll_device_token('d', 'u')

    def test_exchange_requires_complete_renewable_grant(self):
        incomplete = FakeResponse(200, {'access_token': 'access-only'})
        with patch('claws.codex_device_auth.requests.post', return_value=incomplete):
            with self.assertRaisesRegex(
                CodexDeviceAuthError, r'missing refresh_token, id_token',
            ):
                exchange_authorization_code('authz', 'verifier')

    def test_resolve_account_email_from_jwt_claims(self):
        token = _jwt_with({'https://api.openai.com/profile': {'email': 'ops@shizuha.com'}})
        self.assertEqual(resolve_account_email(token), 'ops@shizuha.com')


@override_settings(HIVE_TOKEN_COORDINATOR_KEK='unit-test-kek', HIVE_TOKEN_COORDINATOR_KEK_ID='unit-v1')
class IngestTests(TestCase):
    def _tokens(self, email='digital@shizuha.com', refresh='rt-new'):
        access = _jwt_with({
            'email': email,
            'https://api.openai.com/auth': {'chatgpt_account_id': 'acct-9'},
        })
        return {'access_token': access, 'refresh_token': refresh, 'id_token': 'idt'}

    def test_ingest_creates_personal_pool_entry_with_oauth_json(self):
        result = ingest_codex_account(self._tokens(), actor='staff@shizuha.com')
        self.assertTrue(result['created'])
        self.assertEqual(result['email'], 'digital@shizuha.com')
        self.assertEqual(result['account_id'], 'acct-9')

        from django.conf import settings
        # Personal-ownership model (2cd4176): fleet imports land in the
        # platform owner's PERSONAL pool, not the synthetic org pool.
        pool = TokenPool.objects.get(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id=settings.HIVE_FLEET_OWNER_USER_ID,
            provider=TokenPool.Provider.OPENAI,
        )
        entry = TokenEntry.objects.get(pool=pool, label='org-openai-digital@shizuha.com')
        payload = json.loads(entry.get_token())
        self.assertEqual(payload['refresh_token'], 'rt-new')
        self.assertEqual(payload['account_id'], 'acct-9')
        self.assertEqual(entry.account_email, 'digital@shizuha.com')
        # raw material never stored in plaintext columns
        self.assertNotIn('rt-new', entry.token_ciphertext)

    def test_reauth_rotates_in_place_and_revives_dead_entry(self):
        first = ingest_codex_account(self._tokens(refresh='rt-old'))
        entry = TokenEntry.objects.get(id=first['entry_id'])
        # simulate the revoked-token outage state the flow must recover from
        entry.active = False
        entry.last_status = TokenEntry.LastStatus.AUTH_FAILED
        entry.save(update_fields=['active', 'last_status', 'updated_at'])

        second = ingest_codex_account(self._tokens(refresh='rt-fresh'))
        self.assertFalse(second['created'])
        self.assertEqual(first['entry_id'], second['entry_id'])

        entry.refresh_from_db()
        self.assertTrue(entry.active)
        self.assertEqual(entry.last_status, TokenEntry.LastStatus.OK)
        self.assertIsNone(entry.cooldown_until)
        self.assertEqual(json.loads(entry.get_token())['refresh_token'], 'rt-fresh')

    def test_reauth_preserves_personal_reserve_policy(self):
        first = ingest_codex_account(self._tokens(refresh='rt-old'))
        entry = TokenEntry.objects.get(id=first['entry_id'])
        entry.agent_leasing_enabled = False
        entry.save(update_fields=['agent_leasing_enabled', 'updated_at'])

        ingest_codex_account(
            self._tokens(refresh='rt-fresh'),
            target_entry=entry,
            expected_email='digital@shizuha.com',
            expected_account_id='acct-9',
        )

        entry.refresh_from_db()
        self.assertTrue(entry.active)
        self.assertEqual(entry.last_status, TokenEntry.LastStatus.OK)
        self.assertFalse(entry.agent_leasing_enabled)

    def test_distinct_accounts_get_distinct_entries(self):
        ingest_codex_account(self._tokens(email='digital@shizuha.com'))
        ingest_codex_account(self._tokens(email='trading@shizuha.com'))
        from django.conf import settings
        pool = TokenPool.objects.get(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id=settings.HIVE_FLEET_OWNER_USER_ID,
            provider=TokenPool.Provider.OPENAI,
        )
        labels = set(pool.entries.values_list('label', flat=True))
        self.assertEqual(labels, {
            'org-openai-digital@shizuha.com',
            'org-openai-trading@shizuha.com',
        })

    def test_targeted_reauth_rejects_a_different_authenticated_account(self):
        first = ingest_codex_account(self._tokens(email='trading@shizuha.com'))
        entry = TokenEntry.objects.get(id=first['entry_id'])

        with self.assertRaises(CodexDeviceAuthTargetMismatch):
            ingest_codex_account(
                self._tokens(email='global@shizuha.com', refresh='rt-wrong'),
                target_entry=entry,
                expected_email='trading@shizuha.com',
                expected_account_id='acct-9',
            )

        entry.refresh_from_db()
        self.assertEqual(json.loads(entry.get_token())['refresh_token'], 'rt-new')
        self.assertFalse(TokenEntry.objects.filter(account_email='global@shizuha.com').exists())


@override_settings(HIVE_TOKEN_COORDINATOR_KEK='unit-test-kek', HIVE_TOKEN_COORDINATOR_KEK_ID='unit-v1')
class DeviceAuthAPITests(TestCase):
    def setUp(self):
        from rest_framework.test import APIClient
        from .authentication import FederatedUser
        from django.conf import settings

        self.client = APIClient()
        self.user = FederatedUser(user_id=100, email='user@example.com', username='user')
        self.staff = FederatedUser(
            user_id=int(settings.HIVE_FLEET_OWNER_USER_ID),
            email='staff@example.com',
            username='staff',
            is_staff=True,
        )

    def test_start_and_poll_are_staff_only(self):
        self.client.force_authenticate(user=self.user)
        self.assertEqual(self.client.post('/api/v1/codex-device-auth/start/', {}, format='json').status_code, 403)
        self.assertEqual(
            self.client.post('/api/v1/codex-device-auth/poll/',
                             {'device_auth_id': 'd', 'user_code': 'u'}, format='json').status_code, 403)

    def test_start_returns_user_code(self):
        self.client.force_authenticate(user=self.staff)
        fake = FakeResponse(200, {'user_code': 'WXYZ-7777', 'device_auth_id': 'dev-7'})
        with patch('claws.codex_device_auth.requests.post', return_value=fake):
            response = self.client.post('/api/v1/codex-device-auth/start/', {}, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['user_code'], 'WXYZ-7777')
        self.assertEqual(response.data['device_auth_id'], 'dev-7')

    def test_cookie_auth_me_bootstrap_enables_csrf_protected_start(self):
        """Browser order survives the public-edge to internal-ingress host hop."""
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import AccessToken

        token = AccessToken()
        token['user_id'] = self.staff.id
        token['email'] = self.staff.email
        token['username'] = self.staff.username
        token['is_staff'] = True

        client = APIClient(enforce_csrf_checks=True)
        client.cookies['shizuha-access-token'] = str(token)

        rejected = client.post(
            '/api/v1/codex-device-auth/start/', {}, format='json',
            HTTP_ORIGIN='https://s1.tail.shizuha.com',
            HTTP_X_FORWARDED_PROTO='https',
            HTTP_HOST='hive.gx10.local',
        )
        self.assertEqual(rejected.status_code, 403)

        me = client.get(
            '/api/v1/me/',
            HTTP_X_FORWARDED_PROTO='https',
            HTTP_HOST='hive.gx10.local',
        )
        self.assertEqual(me.status_code, 200)
        csrf_token = me.cookies['csrftoken'].value

        fake = FakeResponse(200, {'user_code': 'WXYZ-7777', 'device_auth_id': 'dev-cookie'})
        with patch('claws.codex_device_auth.requests.post', return_value=fake):
            accepted = client.post(
                '/api/v1/codex-device-auth/start/', {}, format='json',
                HTTP_X_CSRFTOKEN=csrf_token,
                HTTP_ORIGIN='https://s1.tail.shizuha.com',
                HTTP_X_FORWARDED_PROTO='https',
                HTTP_HOST='hive.gx10.local',
            )
        self.assertEqual(accepted.status_code, 201)
        self.assertEqual(accepted.data['device_auth_id'], 'dev-cookie')

    def test_poll_requires_ids(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.post('/api/v1/codex-device-auth/poll/', {}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_poll_pending_passthrough(self):
        self.client.force_authenticate(user=self.staff)
        pending = FakeResponse(200, {'error': 'authorization_pending'})
        with patch('claws.codex_device_auth.requests.post', return_value=pending):
            response = self.client.post(
                '/api/v1/codex-device-auth/poll/',
                {'device_auth_id': 'd', 'user_code': 'u'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'pending')

    def test_poll_transient_provider_failure_is_retryable(self):
        self.client.force_authenticate(user=self.staff)
        upstream = FakeResponse(503, {'error': 'temporarily_unavailable'})
        with patch('claws.codex_device_auth.requests.post', return_value=upstream):
            response = self.client.post(
                '/api/v1/codex-device-auth/poll/',
                {'device_auth_id': 'd', 'user_code': 'u'},
                format='json',
            )
        self.assertEqual(response.status_code, 503)
        self.assertTrue(response.data['retryable'])
        self.assertIn('temporarily_unavailable', response.data['error'])

    def test_poll_declined_is_terminal(self):
        self.client.force_authenticate(user=self.staff)
        upstream = FakeResponse(401, {'error': 'authorization_declined'})
        with patch('claws.codex_device_auth.requests.post', return_value=upstream):
            response = self.client.post(
                '/api/v1/codex-device-auth/poll/',
                {'device_auth_id': 'd', 'user_code': 'u'},
                format='json',
            )
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.data['retryable'])
        self.assertIn('authorization_declined', response.data['error'])

    def test_poll_authorized_ingests_account(self):
        self.client.force_authenticate(user=self.staff)
        access = _jwt_with({
            'email': 'digital@shizuha.com',
            'https://api.openai.com/auth': {'chatgpt_account_id': 'acct-1'},
        })
        responses = iter([
            FakeResponse(200, {'authorization_code': 'authz', 'code_verifier': 'ver'}),  # poll
            FakeResponse(200, {'access_token': access, 'refresh_token': 'rt-ui', 'id_token': 'idt'}),  # exchange
        ])
        with patch('claws.codex_device_auth.requests.post', side_effect=lambda *a, **k: next(responses)):
            response = self.client.post(
                '/api/v1/codex-device-auth/poll/',
                {'device_auth_id': 'd', 'user_code': 'u'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'authorized')
        self.assertEqual(response.data['email'], 'digital@shizuha.com')
        self.assertTrue(response.data['created'])

        entry = TokenEntry.objects.get(id=response.data['entry_id'])
        self.assertEqual(json.loads(entry.get_token())['refresh_token'], 'rt-ui')
        self.assertEqual(entry.pool.provider, TokenPool.Provider.OPENAI)
        self.assertEqual(entry.pool.owner_kind, TokenPool.OwnerKind.USER)

    def test_poll_targeted_reauth_rotates_exact_dead_entry(self):
        first = ingest_codex_account({
            'access_token': _jwt_with({
                'email': 'trading@shizuha.com',
                'https://api.openai.com/auth': {'chatgpt_account_id': 'acct-trading'},
            }),
            'refresh_token': 'rt-old',
            'id_token': 'idt-old',
        })
        entry = TokenEntry.objects.get(id=first['entry_id'])
        entry.active = False
        entry.last_status = TokenEntry.LastStatus.AUTH_FAILED
        entry.save(update_fields=['active', 'last_status', 'updated_at'])

        access = _jwt_with({
            'email': 'trading@shizuha.com',
            'https://api.openai.com/auth': {'chatgpt_account_id': 'acct-trading'},
        })
        responses = iter([
            FakeResponse(200, {'authorization_code': 'authz', 'code_verifier': 'ver'}),
            FakeResponse(200, {
                'access_token': access,
                'refresh_token': 'rt-fresh',
                'id_token': 'idt-fresh',
            }),
        ])
        self.client.force_authenticate(user=self.staff)
        with patch('claws.codex_device_auth.requests.post', side_effect=lambda *a, **k: next(responses)):
            response = self.client.post(
                '/api/v1/codex-device-auth/poll/',
                {
                    'device_auth_id': 'd',
                    'user_code': 'u',
                    'target_pool_id': str(entry.pool_id),
                    'target_entry_id': str(entry.id),
                },
                format='json',
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['entry_id'], str(entry.id))
        self.assertFalse(response.data['created'])
        entry.refresh_from_db()
        self.assertTrue(entry.active)
        self.assertEqual(entry.last_status, TokenEntry.LastStatus.OK)
        self.assertEqual(json.loads(entry.get_token())['refresh_token'], 'rt-fresh')

    def test_poll_targeted_reauth_wrong_account_returns_409_without_mutation(self):
        first = ingest_codex_account({
            'access_token': _jwt_with({
                'email': 'trading@shizuha.com',
                'https://api.openai.com/auth': {'chatgpt_account_id': 'acct-trading'},
            }),
            'refresh_token': 'rt-old',
            'id_token': 'idt-old',
        })
        entry = TokenEntry.objects.get(id=first['entry_id'])
        access = _jwt_with({
            'email': 'global@shizuha.com',
            'https://api.openai.com/auth': {'chatgpt_account_id': 'acct-global'},
        })
        responses = iter([
            FakeResponse(200, {'authorization_code': 'authz', 'code_verifier': 'ver'}),
            FakeResponse(200, {
                'access_token': access,
                'refresh_token': 'rt-wrong',
                'id_token': 'idt-wrong',
            }),
        ])
        self.client.force_authenticate(user=self.staff)
        with patch('claws.codex_device_auth.requests.post', side_effect=lambda *a, **k: next(responses)):
            response = self.client.post(
                '/api/v1/codex-device-auth/poll/',
                {
                    'device_auth_id': 'd',
                    'user_code': 'u',
                    'target_pool_id': str(entry.pool_id),
                    'target_entry_id': str(entry.id),
                },
                format='json',
            )
        self.assertEqual(response.status_code, 409)
        entry.refresh_from_db()
        self.assertEqual(json.loads(entry.get_token())['refresh_token'], 'rt-old')
        self.assertFalse(TokenEntry.objects.filter(account_email='global@shizuha.com').exists())
