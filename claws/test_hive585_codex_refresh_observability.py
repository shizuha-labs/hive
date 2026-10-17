"""HIVE-585: revoked Codex refresh tokens alert + dashboard recovery link."""
import json
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from . import codex_refresh_metrics
from hive_platform.models import TokenEntry, TokenPool
from .token_coordinator import select_token_for_owner


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


@override_settings(
    HIVE_TOKEN_COORDINATOR_KEK='unit-test-kek',
    HIVE_TOKEN_COORDINATOR_KEK_ID='unit-v1',
    CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
)
class CodexRefreshObservabilityTests(TestCase):
    METRICS_URL = '/api/v1/internal/codex-refresh/metrics/'

    def setUp(self):
        from .authentication import FederatedUser

        cache.clear()
        self.client = APIClient()
        self.staff = FederatedUser(user_id=1, email='staff@example.com', username='staff', is_staff=True)
        self.pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.ORG,
            owner_id=TokenPool.ORG_OWNER_ID,
            provider=TokenPool.Provider.OPENAI,
            display_name='Internal fleet org OpenAI/Codex pool',
        )
        self.entry = TokenEntry(pool=self.pool, label='org-openai-digital@shizuha.com', priority=100)
        self.entry.set_token(json.dumps({
            'email': 'digital@shizuha.com',
            'refresh_token': 'rt-revoked',
            'access_token': '',
            'id_token': 'idt',
        }, sort_keys=True))
        self.entry.save()

    def test_refresh_401_marks_entry_revoked_and_emits_prometheus_counter(self):
        with patch('hive_platform.token_coordinator.requests.post', return_value=FakeResponse(401)):
            selected = select_token_for_owner(
                TokenPool.ORG_OWNER_ID,
                TokenPool.Provider.OPENAI,
                owner_kind=TokenPool.OwnerKind.ORG,
                allow_org_fallback=False,
            )
        self.assertIsNone(selected)

        self.entry.refresh_from_db()
        self.assertFalse(self.entry.active)
        self.assertEqual(self.entry.last_status, TokenEntry.LastStatus.AUTH_FAILED)

        snap = codex_refresh_metrics.snapshot([self.entry])[0]
        self.assertEqual(snap['refresh_401_total'], 1)
        self.assertEqual(snap['status'], 'revoked')
        self.assertIn('/hive/credentials/codex/re-auth?scope=org', snap['reauth_path'])
        self.assertIn(f'pool={self.pool.id}', snap['reauth_path'])
        self.assertIn(f'entry={self.entry.id}', snap['reauth_path'])
        self.assertIn('email=digital%40shizuha.com', snap['reauth_path'])

        text = codex_refresh_metrics.to_prometheus()
        self.assertIn('hive_codex_refresh_metrics_present 1', text)
        self.assertIn(
            'hive_codex_refresh_401_total{account_email="digital@shizuha.com",account_email_urlencoded="digital%40shizuha.com",label="org-openai-digital@shizuha.com"} 1',
            text,
        )
        self.assertIn(
            'hive_codex_account_status{account_email="digital@shizuha.com",account_email_urlencoded="digital%40shizuha.com",label="org-openai-digital@shizuha.com",status="revoked"} 1',
            text,
        )

    def test_invalid_grant_http_400_is_terminal_and_requires_reauth(self):
        with patch(
            'hive_platform.token_coordinator.requests.post',
            return_value=FakeResponse(400, {'error': 'invalid_grant'}),
        ):
            selected = select_token_for_owner(
                TokenPool.ORG_OWNER_ID,
                TokenPool.Provider.OPENAI,
                owner_kind=TokenPool.OwnerKind.ORG,
                allow_org_fallback=False,
            )

        self.assertIsNone(selected)
        self.entry.refresh_from_db()
        self.assertFalse(self.entry.active)
        self.assertEqual(
            self.entry.last_status,
            TokenEntry.LastStatus.AUTH_FAILED,
        )

    def test_invalid_client_http_400_cools_without_destroying_valid_grant(self):
        with patch(
            'hive_platform.token_coordinator.requests.post',
            return_value=FakeResponse(400, {'error': 'invalid_client'}),
        ):
            selected = select_token_for_owner(
                TokenPool.ORG_OWNER_ID,
                TokenPool.Provider.OPENAI,
                owner_kind=TokenPool.OwnerKind.ORG,
                allow_org_fallback=False,
            )

        self.assertIsNone(selected)
        self.entry.refresh_from_db()
        self.assertTrue(self.entry.active)
        self.assertEqual(
            self.entry.last_status,
            TokenEntry.LastStatus.RATE_LIMITED,
        )
        self.assertIsNotNone(self.entry.cooldown_until)

    def test_metrics_endpoint_and_token_pool_dashboard_fields_include_reauth_link(self):
        codex_refresh_metrics.record_refresh_401(self.entry)
        resp = self.client.get(self.METRICS_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('hive_codex_refresh_401_total', resp.content.decode())
        self.assertTrue(resp['Content-Type'].startswith('text/plain'))

        self.client.force_authenticate(user=self.staff)
        resp = self.client.get('/api/v1/token-pools/?scope=org')
        self.assertEqual(resp.status_code, 200)
        row = resp.data[0]['entries'][0]
        self.assertEqual(row['codex_account_email'], 'digital@shizuha.com')
        self.assertIn(row['codex_account_status'], {'unknown', 'active', 'expired'})
        self.assertIn('/hive/credentials/codex/re-auth?scope=org', row['codex_reauth_url'])
        self.assertIn(f'pool={self.pool.id}', row['codex_reauth_url'])
        self.assertIn(f'entry={self.entry.id}', row['codex_reauth_url'])
