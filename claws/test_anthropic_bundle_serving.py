import json

from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from .authentication import FederatedUser
from .coordinator_views import ModelTokenView
from hive_platform.models import TokenEntry, TokenPool
from .token_coordinator import _selection_from_entry


@override_settings(
    HIVE_TOKEN_COORDINATOR_KEK='anthropic-serving-test-kek',
    HIVE_TOKEN_COORDINATOR_KEK_ID='anthropic-serving-v1',
    HIVE_COORDINATOR_AUDIENCE='hive-coordinator',
    HIVE_COORDINATOR_BROKER_SCOPE='coordinator:model-token',
)
class AnthropicBundleServingTests(TestCase):
    def _entry(self, bundle):
        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id='42',
            provider=TokenPool.Provider.ANTHROPIC,
        )
        entry = TokenEntry(pool=pool, label='claude-full-login', priority=1)
        entry.set_token(json.dumps(bundle))
        entry.save()
        return entry

    def test_repeated_selection_serves_bare_access_token_not_bundle(self):
        entry = self._entry({
            'type': 'claude_oauth',
            'access_token': 'sk-ant-oat01-serving-access',
            'refresh_token': 'sk-ant-ort01-serving-refresh',
            'email': 'owner@example.com',
        })

        first = _selection_from_entry(entry)
        second = _selection_from_entry(entry)

        self.assertEqual(first.token, 'sk-ant-oat01-serving-access')
        self.assertEqual(second.token, 'sk-ant-oat01-serving-access')
        self.assertFalse(first.token.startswith('{'))
        self.assertEqual(first.token_payload['refresh_token'], 'sk-ant-ort01-serving-refresh')

    def test_broker_api_repeatedly_returns_bare_access_token(self):
        self._entry({
            'type': 'claude_oauth',
            'access_token': 'sk-ant-oat01-api-access',
            'refresh_token': 'sk-ant-ort01-api-refresh',
        })
        claims = {
            'user_id': 0,
            'aud': ['hive-coordinator'],
            'scope': 'coordinator:model-token',
            'owner_kind': 'user',
            'owner_id': '42',
            'workload_id': 'anthropic-serving-test-broker',
        }
        factory = APIRequestFactory()

        responses = []
        for _ in range(2):
            request = factory.post(
                '/hive/api/v1/coordinator/model-token',
                {'provider': TokenPool.Provider.ANTHROPIC},
                format='json',
            )
            force_authenticate(
                request,
                user=FederatedUser(user_id=0, email='broker@example.com', username='broker'),
                token=claims,
            )
            responses.append(ModelTokenView.as_view()(request))

        self.assertEqual([response.status_code for response in responses], [200, 200])
        self.assertEqual(
            [response.data['token'] for response in responses],
            ['sk-ant-oat01-api-access', 'sk-ant-oat01-api-access'],
        )
        self.assertEqual(TokenEntry.objects.get().leases.count(), 2)

    def test_near_expiry_bundle_refreshes_then_serves_new_access_token(self):
        from unittest.mock import patch

        entry = self._entry({
            'type': 'claude_oauth',
            'access_token': 'sk-ant-oat01-stale-access',
            'refresh_token': 'sk-ant-ort01-stale-refresh',
            'expires_at': '2000-01-01T00:00:00+00:00',
        })
        refreshed = json.dumps({
            'type': 'claude_oauth',
            'access_token': 'sk-ant-oat01-fresh-access',
            'refresh_token': 'sk-ant-ort01-fresh-refresh',
            'expires_at': '2099-01-01T00:00:00+00:00',
        })

        with patch(
            'hive_platform.token_coordinator._refresh_and_persist_claude_bundle',
            return_value=refreshed,
        ) as refresh:
            selection = _selection_from_entry(entry)

        refresh.assert_called_once()
        self.assertEqual(selection.token, 'sk-ant-oat01-fresh-access')
        self.assertEqual(selection.token_payload['refresh_token'], 'sk-ant-ort01-fresh-refresh')
