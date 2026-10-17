"""xAI / Grok device-code auth into org TokenPool (SuperGrok OIDC)."""
import base64
import json
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from .grok_device_auth import (
    GrokDeviceAuthError,
    ingest_grok_account,
    poll_device_token,
    request_device_code,
    resolve_account_identity,
    resolve_account_label,
)
from hive_platform.models import TokenEntry, TokenEntryLeasingEvent, TokenLease, TokenPool
from .token_coordinator import (
    XaiOAuthRefreshError,
    _selection_from_entry,
    refresh_xai_oauth,
)


def _jwt_with(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    return f'h.{body}.s'


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=''):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)
        self.content = self.text.encode() if self.text else b''

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._payload


class GrokDeviceCodeFlowTests(TestCase):
    def test_request_device_code_returns_user_code_and_page(self):
        fake = FakeResponse(200, {
            'user_code': 'WXYZ-5678',
            'device_code': 'dev-xai-1',
            'verification_uri': 'https://accounts.x.ai/device',
            'interval': 5,
            'expires_in': 300,
        })
        with patch('claws.grok_device_auth.requests.post', return_value=fake):
            data = request_device_code()
        self.assertEqual(data['user_code'], 'WXYZ-5678')
        self.assertEqual(data['device_code'], 'dev-xai-1')
        self.assertEqual(data['device_auth_id'], 'dev-xai-1')
        self.assertEqual(data['interval'], 5)
        self.assertIn('x.ai', data['verification_url'])

    def test_request_device_code_missing_fields_raises(self):
        with patch('claws.grok_device_auth.requests.post', return_value=FakeResponse(200, {})):
            with self.assertRaises(GrokDeviceAuthError):
                request_device_code()

    def test_poll_pending_slow_down_authorized(self):
        pending = FakeResponse(400, {'error': 'authorization_pending'})
        with patch('claws.grok_device_auth.requests.post', return_value=pending):
            self.assertEqual(poll_device_token('d')['status'], 'pending')

        with patch('claws.grok_device_auth.requests.post', return_value=FakeResponse(429)):
            self.assertEqual(poll_device_token('d')['status'], 'slow_down')

        authorized = FakeResponse(200, {
            'access_token': 'at-1',
            'refresh_token': 'rt-1',
            'expires_in': 3600,
        })
        with patch('claws.grok_device_auth.requests.post', return_value=authorized):
            result = poll_device_token('d')
        self.assertEqual(result['status'], 'authorized')
        self.assertEqual(result['access_token'], 'at-1')
        self.assertEqual(result['refresh_token'], 'rt-1')
        self.assertTrue(result.get('expires_at'))

    def test_poll_denied_raises(self):
        denied = FakeResponse(400, {'error': 'access_denied'})
        with patch('claws.grok_device_auth.requests.post', return_value=denied):
            with self.assertRaises(GrokDeviceAuthError):
                poll_device_token('d')

    def test_resolve_account_label_from_jwt(self):
        token = _jwt_with({'email': 'ops@shizuha.com'})
        self.assertEqual(resolve_account_label(token), 'ops@shizuha.com')

    def test_identity_reads_subject_from_access_and_email_from_id_token(self):
        account_id = 'ef46a8ee-5796-4242-8e4b-97d6c4264da2'
        access = _jwt_with({'sub': account_id, 'email': account_id})
        identity = resolve_account_identity(
            access,
            _jwt_with({'sub': account_id, 'email': 'test-owner@shizuha.com'}),
        )
        self.assertEqual(identity['account_id'], account_id)
        self.assertEqual(identity['email'], 'test-owner@shizuha.com')
        self.assertEqual(identity['label'], 'test-owner@shizuha.com')


@override_settings(HIVE_TOKEN_COORDINATOR_KEK='unit-test-kek', HIVE_TOKEN_COORDINATOR_KEK_ID='unit-v1')
class GrokIngestTests(TestCase):
    def _tokens(
        self,
        email='digital@shizuha.com',
        refresh='rt-new',
        account_id='',
    ):
        access_claims = {'email': account_id or email, 'exp': 9999999999}
        if account_id:
            access_claims['sub'] = account_id
        tokens = {
            'access_token': _jwt_with(access_claims),
            'refresh_token': refresh,
            'expires_in': 3600,
            'expires_at': '2099-01-01T00:00:00+00:00',
        }
        if account_id:
            tokens['id_token'] = _jwt_with({'sub': account_id, 'email': email})
        return tokens

    def test_ingest_creates_org_xai_pool_entry(self):
        result = ingest_grok_account(self._tokens(), actor='staff@shizuha.com')
        self.assertTrue(result['created'])
        self.assertEqual(result['email'], 'digital@shizuha.com')

        from django.conf import settings
        # Personal-ownership model (2cd4176): fleet imports land in the
        # platform owner's PERSONAL pool, not the synthetic org pool.
        pool = TokenPool.objects.get(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id=settings.HIVE_FLEET_OWNER_USER_ID,
            provider=TokenPool.Provider.XAI,
        )
        entry = TokenEntry.objects.get(pool=pool, label='org-xai-digital@shizuha.com')
        payload = json.loads(entry.get_token())
        self.assertEqual(payload['refresh_token'], 'rt-new')
        self.assertEqual(payload['type'], 'xai_oauth')
        self.assertEqual(payload['oidc_client_id'], 'b1a00492-073a-47ea-816f-4c329264a828')
        self.assertNotIn('rt-new', entry.token_ciphertext)

    def test_reauth_rotates_in_place(self):
        first = ingest_grok_account(self._tokens(refresh='rt-old'))
        entry = TokenEntry.objects.get(id=first['entry_id'])
        entry.active = False
        entry.last_status = TokenEntry.LastStatus.AUTH_FAILED
        entry.save(update_fields=['active', 'last_status', 'updated_at'])

        second = ingest_grok_account(self._tokens(refresh='rt-fresh'))
        self.assertFalse(second['created'])
        self.assertEqual(first['entry_id'], second['entry_id'])
        entry.refresh_from_db()
        self.assertTrue(entry.active)
        self.assertEqual(json.loads(entry.get_token())['refresh_token'], 'rt-fresh')

    def test_reauth_uses_immutable_subject_after_operator_renames_label(self):
        account_id = 'ef46a8ee-5796-4242-8e4b-97d6c4264da2'
        first = ingest_grok_account(self._tokens(
            email='test-owner@shizuha.com',
            refresh='rt-old',
            account_id=account_id,
        ))
        entry = TokenEntry.objects.get(id=first['entry_id'])
        entry.label = 'test-owner@shizuha.com - old'
        entry.save(update_fields=['label', 'updated_at'])

        second = ingest_grok_account(self._tokens(
            email='test-owner@shizuha.com',
            refresh='rt-fresh',
            account_id=account_id,
        ))

        self.assertFalse(second['created'])
        self.assertEqual(second['entry_id'], first['entry_id'])
        self.assertEqual(TokenEntry.objects.filter(deleted_at__isnull=True).count(), 1)
        entry.refresh_from_db()
        self.assertEqual(entry.label, 'test-owner@shizuha.com - old')
        self.assertEqual(entry.account_email, 'test-owner@shizuha.com')
        payload = json.loads(entry.get_token())
        self.assertEqual(payload['account_id'], account_id)
        self.assertEqual(payload['refresh_token'], 'rt-fresh')

    def test_reauth_merges_prior_label_keyed_duplicate_and_revokes_its_lease(self):
        account_id = 'ef46a8ee-5796-4242-8e4b-97d6c4264da2'
        first = ingest_grok_account(self._tokens(
            email='test-owner@shizuha.com',
            refresh='rt-old',
            account_id=account_id,
        ))
        canonical = TokenEntry.objects.get(id=first['entry_id'])
        canonical.label = 'test-owner@shizuha.com - old'
        canonical.save(update_fields=['label', 'updated_at'])

        duplicate = TokenEntry(
            pool=canonical.pool,
            label='test-owner@shizuha.com',
            account_email=account_id,
            priority=100,
        )
        duplicate.set_token(json.dumps({
            'type': 'xai_oauth',
            'email': account_id,
            'account_id': account_id,
            'access_token': self._tokens(account_id=account_id)['access_token'],
            'id_token': self._tokens(account_id=account_id)['id_token'],
            'refresh_token': 'rt-duplicate',
        }, sort_keys=True))
        duplicate.save()
        lease = TokenLease.objects.create(
            entry=duplicate,
            broker_identity='agent-test',
            owner_kind=duplicate.pool.owner_kind,
            owner_id=duplicate.pool.owner_id,
            provider=TokenPool.Provider.XAI,
            expires_at=timezone.now() + timedelta(minutes=5),
        )

        result = ingest_grok_account(self._tokens(
            email='test-owner@shizuha.com',
            refresh='rt-newest',
            account_id=account_id,
        ), actor='owner@shizuha.com')

        self.assertEqual(result['entry_id'], first['entry_id'])
        self.assertEqual(result['merged_entry_ids'], [str(duplicate.id)])
        canonical.refresh_from_db()
        duplicate.refresh_from_db()
        lease.refresh_from_db()
        self.assertEqual(json.loads(canonical.get_token())['refresh_token'], 'rt-newest')
        self.assertIsNotNone(duplicate.deleted_at)
        self.assertEqual(duplicate.token_ciphertext, '')
        self.assertLessEqual(lease.expires_at, timezone.now())
        self.assertEqual(
            TokenEntry.objects.filter(
                pool=canonical.pool,
                deleted_at__isnull=True,
            ).count(),
            1,
        )

    def test_ingest_can_reserve_account_from_agent_leases(self):
        result = ingest_grok_account(
            self._tokens(email='personal@shizuha.com'),
            available_to_agents=False,
        )

        entry = TokenEntry.objects.get(id=result['entry_id'])
        self.assertFalse(result['available_to_agents'])
        self.assertTrue(entry.active)
        self.assertFalse(entry.agent_leasing_enabled)
        self.assertEqual(entry.account_email, 'personal@shizuha.com')
        event = TokenEntryLeasingEvent.objects.get(entry=entry)
        self.assertEqual(event.source, TokenEntryLeasingEvent.Source.GROK_DEVICE_AUTH)
        self.assertTrue(event.previous_enabled)
        self.assertFalse(event.enabled)
        self.assertFalse(
            TokenEntry.objects.active_for_owner(
                entry.pool.owner_kind,
                entry.pool.owner_id,
                TokenPool.Provider.XAI,
            ).filter(id=entry.id).exists()
        )


@override_settings(HIVE_TOKEN_COORDINATOR_KEK='unit-test-kek', HIVE_TOKEN_COORDINATOR_KEK_ID='unit-v1')
class GrokCoordinatorRefreshTests(TestCase):
    def test_selection_refreshes_near_expiry_xai_token(self):
        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.ORG,
            owner_id=TokenPool.ORG_OWNER_ID,
            provider=TokenPool.Provider.XAI,
        )
        entry = TokenEntry(pool=pool, label='org-xai-ops', priority=10)
        entry.set_token(json.dumps({
            'type': 'xai_oauth',
            'access_token': 'old-access',
            'refresh_token': 'rt-1',
            'expires_at': '2000-01-01T00:00:00+00:00',
            'oidc_client_id': 'cid',
            'token_endpoint': 'https://auth.x.ai/oauth2/token',
            'email': 'ops@shizuha.com',
            'account_id': 'acct-xai-1',
        }, sort_keys=True))
        entry.save()

        with patch('hive_platform.token_coordinator.requests.post') as post:
            post.return_value = FakeResponse(200, {
                'access_token': 'new-access',
                'refresh_token': 'rt-2',
                'expires_in': 3600,
            })
            selection = _selection_from_entry(entry, refresh_openai=True)

        payload = json.loads(selection.token)
        self.assertEqual(payload['access_token'], 'new-access')
        self.assertEqual(payload['auth_mode'], 'xaiAccessToken')
        self.assertEqual(payload['account_id'], 'acct-xai-1')
        self.assertNotIn('refresh_token', payload)
        self.assertNotIn('id_token', payload)
        entry.refresh_from_db()
        stored = json.loads(entry.get_token())
        self.assertEqual(stored['refresh_token'], 'rt-2')
        self.assertEqual(stored['account_id'], 'acct-xai-1')

    def test_refresh_xai_oauth_missing_refresh_raises(self):
        with self.assertRaises(XaiOAuthRefreshError):
            refresh_xai_oauth({'access_token': 'a'})


@override_settings(HIVE_TOKEN_COORDINATOR_KEK='unit-test-kek', HIVE_TOKEN_COORDINATOR_KEK_ID='unit-v1')
class GrokDeviceAuthAPITests(TestCase):
    def setUp(self):
        from rest_framework.test import APIClient
        from .authentication import FederatedUser

        self.client = APIClient()
        self.staff = FederatedUser(
            user_id=1, username='staff', email='staff@shizuha.com', is_staff=True,
        )
        self.user = FederatedUser(
            user_id=2, username='user', email='user@shizuha.com', is_staff=False,
        )
        self.fleet_owner = FederatedUser(
            user_id=3, username='owner', email='owner@shizuha.com', is_staff=False,
        )

    def test_start_rejects_unrelated_non_staff_user(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post('/api/v1/grok-device-auth/start/')
        self.assertEqual(resp.status_code, 403)

    def test_poll_rejects_unrelated_non_staff_user(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post(
            '/api/v1/grok-device-auth/poll/',
            {'device_code': 'not-authorized'},
            format='json',
        )
        self.assertEqual(resp.status_code, 403)

    def test_start_allows_configured_fleet_owner(self):
        self.client.force_authenticate(user=self.fleet_owner)
        start_fake = FakeResponse(200, {
            'user_code': 'OWNER-99',
            'device_code': 'owner-device',
            'verification_uri': 'https://accounts.x.ai/device',
            'interval': 3,
            'expires_in': 300,
        })
        with patch('claws.grok_device_auth.requests.post', return_value=start_fake):
            resp = self.client.post('/api/v1/grok-device-auth/start/')
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data['user_code'], 'OWNER-99')

    def test_start_and_poll_happy_path(self):
        self.client.force_authenticate(user=self.staff)
        start_fake = FakeResponse(200, {
            'user_code': 'ABCD-9999',
            'device_code': 'dev-99',
            'verification_uri': 'https://accounts.x.ai/device',
            'interval': 3,
            'expires_in': 300,
        })
        with patch('claws.grok_device_auth.requests.post', return_value=start_fake):
            start = self.client.post('/api/v1/grok-device-auth/start/')
        self.assertEqual(start.status_code, 201)
        self.assertEqual(start.data['user_code'], 'ABCD-9999')

        access = _jwt_with({'email': 'fleet@shizuha.com'})
        poll_fake = FakeResponse(200, {
            'access_token': access,
            'refresh_token': 'rt-live',
            'expires_in': 7200,
        })
        with patch('claws.grok_device_auth.requests.post', return_value=poll_fake):
            poll = self.client.post('/api/v1/grok-device-auth/poll/', {
                'device_code': 'dev-99',
            }, format='json')
        self.assertEqual(poll.status_code, 200)
        self.assertEqual(poll.data['status'], 'authorized')
        self.assertEqual(poll.data['email'], 'fleet@shizuha.com')
        self.assertTrue(TokenEntry.objects.filter(label='org-xai-fleet@shizuha.com').exists())

    def test_reauth_poll_updates_renamed_existing_entry_by_subject(self):
        """Mirror the real callback sequence: authorize, rename, authorize again."""
        self.client.force_authenticate(user=self.staff)
        account_id = 'ef46a8ee-5796-4242-8e4b-97d6c4264da2'

        def response(refresh_token):
            return FakeResponse(200, {
                'access_token': _jwt_with({
                    'sub': account_id,
                    'email': account_id,
                    'exp': 9999999999,
                }),
                'id_token': _jwt_with({
                    'sub': account_id,
                    'email': 'test-owner@shizuha.com',
                }),
                'refresh_token': refresh_token,
                'expires_in': 7200,
            })

        with patch(
            'claws.grok_device_auth.requests.post',
            return_value=response('rt-old'),
        ):
            first = self.client.post(
                '/api/v1/grok-device-auth/poll/',
                {'device_code': 'dev-old'},
                format='json',
            )
        self.assertEqual(first.status_code, 200)
        entry = TokenEntry.objects.get(id=first.data['entry_id'])
        entry.label = 'test-owner@shizuha.com - old'
        entry.save(update_fields=['label', 'updated_at'])

        with patch(
            'claws.grok_device_auth.requests.post',
            return_value=response('rt-fresh'),
        ):
            second = self.client.post(
                '/api/v1/grok-device-auth/poll/',
                {'device_code': 'dev-fresh'},
                format='json',
            )

        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.data['created'])
        self.assertEqual(second.data['entry_id'], first.data['entry_id'])
        self.assertEqual(second.data['account_id'], account_id)
        self.assertEqual(
            TokenEntry.objects.filter(deleted_at__isnull=True).count(),
            1,
        )
        entry.refresh_from_db()
        self.assertEqual(json.loads(entry.get_token())['refresh_token'], 'rt-fresh')

    def test_poll_can_add_account_reserved_from_agents(self):
        self.client.force_authenticate(user=self.staff)
        poll_fake = FakeResponse(200, {
            'access_token': _jwt_with({'email': 'personal@shizuha.com'}),
            'refresh_token': 'rt-personal',
            'expires_in': 7200,
        })

        with patch('claws.grok_device_auth.requests.post', return_value=poll_fake):
            poll = self.client.post('/api/v1/grok-device-auth/poll/', {
                'device_code': 'dev-personal',
                'available_to_agents': False,
            }, format='json')

        self.assertEqual(poll.status_code, 200)
        self.assertFalse(poll.data['available_to_agents'])
        entry = TokenEntry.objects.get(id=poll.data['entry_id'])
        self.assertTrue(entry.active)
        self.assertFalse(entry.agent_leasing_enabled)

    def test_create_xai_token_pool(self):
        self.client.force_authenticate(user=self.staff)
        resp = self.client.post(
            '/api/v1/token-pools/',
            {'provider': TokenPool.Provider.XAI, 'scope': 'org', 'display_name': 'Org Grok'},
            format='json',
        )
        self.assertIn(resp.status_code, (200, 201))
        self.assertEqual(resp.data['provider'], 'xai')
