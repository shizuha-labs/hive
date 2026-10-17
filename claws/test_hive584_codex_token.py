"""HIVE-584: /v1/codex/token endpoint — source Codex/OpenAI tokens from TokenEntry.

Tests the service-authenticated endpoint that replaces the legacy host
``credentials.json`` import path.
"""
import json
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from .codex_device_auth import ingest_codex_account
from hive_platform.models import TokenEntry, TokenPool


INTERNAL_TOKEN = 'hive584-test-secret'


def _route_url() -> str:
    """Return the URL for the codex-token endpoint (no-slash form)."""
    return '/hive/api/v1/codex/token'


def _jwt_with(payload: dict) -> str:
    """Build a fake JWT with the given payload as the body."""
    import base64
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    return f'h.{body}.s'


def _create_org_openai_entry(label='org-openai-codex@shizuha.com', payload=None, **kwargs):
    pool, _ = TokenPool.objects.get_or_create(
        owner_kind=TokenPool.OwnerKind.ORG,
        owner_id=TokenPool.ORG_OWNER_ID,
        provider=TokenPool.Provider.OPENAI,
        defaults={'display_name': 'Internal fleet org OpenAI/Codex pool'},
    )
    entry = TokenEntry(pool=pool, label=label, priority=kwargs.pop('priority', 100), **kwargs)
    entry.set_token(json.dumps(payload or {
        'access_token': _jwt_with({'exp': 4_102_444_800}),
        'refresh_token': 'rto_test_value',
        'id_token': 'id_test_value',
        'email': 'codex@shizuha.com',
        'account_id': 'acc_123',
    }, sort_keys=True))
    entry.save()
    return entry


class CodexTokenAuthTests(TestCase):
    """Authentication gating — must present X-Hive-Internal-Token."""

    @override_settings(HIVE_INTERNAL_AUTH_TOKEN=INTERNAL_TOKEN)
    def test_requires_internal_auth(self):
        resp = self.client.get(_route_url())
        self.assertEqual(resp.status_code, 403)
        self.assertIn('authentication required', resp.json()['error'].lower())

    @override_settings(HIVE_INTERNAL_AUTH_TOKEN=INTERNAL_TOKEN)
    def test_wrong_token_rejected(self):
        resp = self.client.get(
            _route_url(),
            HTTP_X_HIVE_INTERNAL_TOKEN='wrong-secret',
        )
        self.assertEqual(resp.status_code, 403)

    @override_settings(HIVE_INTERNAL_AUTH_TOKEN='')
    def test_empty_secret_rejected(self):
        resp = self.client.get(
            _route_url(),
            HTTP_X_HIVE_INTERNAL_TOKEN=INTERNAL_TOKEN,
        )
        self.assertEqual(resp.status_code, 403)


class CodexTokenNoEntryTests(TestCase):
    """No active org OpenAI TokenEntry rows → 404."""

    @override_settings(HIVE_INTERNAL_AUTH_TOKEN=INTERNAL_TOKEN)
    def test_no_entries_returns_404(self):
        resp = self.client.get(
            _route_url(),
            HTTP_X_HIVE_INTERNAL_TOKEN=INTERNAL_TOKEN,
        )
        self.assertEqual(resp.status_code, 404)
        self.assertIn('No active Codex TokenEntry', resp.json()['error'])


@override_settings(HIVE_TOKEN_COORDINATOR_KEK='unit-test-kek', HIVE_TOKEN_COORDINATOR_KEK_ID='unit-v1')
class CodexTokenWithEntryTests(TestCase):
    """TokenEntry source of truth → endpoint returns an access-only lease."""

    @override_settings(HIVE_INTERNAL_AUTH_TOKEN=INTERNAL_TOKEN)
    def test_returns_payload_from_org_openai_token_entry(self):
        payload = {
            'access_token': _jwt_with({'exp': 4_102_444_800}),
            'refresh_token': 'rto_test_value',
            'id_token': 'id_test_value',
            'email': 'codex@shizuha.com',
            'account_id': 'acc_123',
        }
        _create_org_openai_entry(payload=payload)

        resp = self.client.get(
            _route_url(),
            HTTP_X_HIVE_INTERNAL_TOKEN=INTERNAL_TOKEN,
        )

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['access_token'], payload['access_token'])
        self.assertEqual(data['account_id'], payload['account_id'])
        self.assertEqual(data['email'], payload['email'])
        self.assertEqual(data['auth_mode'], 'chatgptAuthTokens')
        self.assertNotIn('refresh_token', data)
        self.assertNotIn('id_token', data)

    @override_settings(HIVE_INTERNAL_AUTH_TOKEN=INTERNAL_TOKEN)
    def test_inactive_entry_skipped(self):
        _create_org_openai_entry(active=False)

        resp = self.client.get(
            _route_url(),
            HTTP_X_HIVE_INTERNAL_TOKEN=INTERNAL_TOKEN,
        )

        self.assertEqual(resp.status_code, 404)

    @override_settings(HIVE_INTERNAL_AUTH_TOKEN=INTERNAL_TOKEN)
    def test_cooling_entry_skipped(self):
        _create_org_openai_entry(cooldown_until=timezone.now() + timedelta(minutes=5))

        resp = self.client.get(
            _route_url(),
            HTTP_X_HIVE_INTERNAL_TOKEN=INTERNAL_TOKEN,
        )

        self.assertEqual(resp.status_code, 404)

    @override_settings(HIVE_INTERNAL_AUTH_TOKEN=INTERNAL_TOKEN)
    @patch('hive_platform.token_coordinator.refresh_openai_oauth')
    def test_refresh_near_expiry_token_and_persist(self, mock_refresh):
        import time
        near_expiry = int(time.time()) + 60
        entry = _create_org_openai_entry(payload={
            'access_token': _jwt_with({'exp': near_expiry}),
            'refresh_token': 'rt_refresh_me',
            'id_token': 'id_old',
            'email': 'codex@shizuha.com',
            'account_id': 'acc_123',
        })
        refreshed = {
            'access_token': _jwt_with({'exp': int(time.time()) + 3600}),
            'refresh_token': 'rt_new_value',
            'id_token': 'id_new',
        }
        mock_refresh.return_value = refreshed

        resp = self.client.get(
            _route_url(),
            HTTP_X_HIVE_INTERNAL_TOKEN=INTERNAL_TOKEN,
        )

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['access_token'], refreshed['access_token'])
        self.assertEqual(data['account_id'], 'acc_123')
        self.assertNotIn('refresh_token', data)
        self.assertNotIn('id_token', data)
        mock_refresh.assert_called_once_with('rt_refresh_me', timeout_seconds=20)
        entry.refresh_from_db()
        persisted = json.loads(entry.get_token())
        self.assertEqual(persisted['access_token'], refreshed['access_token'])
        self.assertEqual(persisted['refresh_token'], refreshed['refresh_token'])

    @override_settings(HIVE_INTERNAL_AUTH_TOKEN=INTERNAL_TOKEN)
    @patch('claws.codex_device_auth.resolve_account_email', return_value='device-auth@shizuha.com')
    @patch('hive_platform.token_coordinator.openai_account_id', return_value='acc_device_auth')
    def test_ingested_device_auth_account_is_served(self, _mock_account, _mock_email):
        access = _jwt_with({'exp': 4_102_444_800})
        ingest_codex_account({
            'access_token': access,
            'refresh_token': 'rt_from_device_auth',
            'id_token': 'id_from_device_auth',
        }, actor='test')

        resp = self.client.get(
            _route_url(),
            HTTP_X_HIVE_INTERNAL_TOKEN=INTERNAL_TOKEN,
        )

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['access_token'], access)
        self.assertEqual(data['email'], 'device-auth@shizuha.com')
        self.assertEqual(data['account_id'], 'acc_device_auth')
        self.assertNotIn('refresh_token', data)
        self.assertNotIn('id_token', data)
