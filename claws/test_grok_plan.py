"""Grok plan comes from authenticated settings, never credit utilization."""
from unittest.mock import Mock, patch
import requests
from django.test import SimpleTestCase, TestCase
from hive_platform import quota_fetchers as qf


class GrokPlanTests(SimpleTestCase):
    @patch.object(qf.requests, 'get')
    def test_provider_labels_are_preserved(self, get):
        for plan in ('Free', 'X Premium+', 'SuperGrok', 'SuperGrok Heavy'):
            get.return_value = Mock(status_code=200, json=lambda: {'subscription_tier_display': plan})
            self.assertEqual(qf.fetch_grok_plan('access'), plan)
        self.assertEqual(get.call_args.args, (qf.GROK_SETTINGS_URL,))
        self.assertEqual(get.call_args.kwargs['headers']['X-XAI-Token-Auth'], 'xai-grok-cli')
        self.assertEqual(get.call_args.kwargs['headers']['Authorization'], 'Bearer access')

    @patch.object(qf.requests, 'get')
    def test_missing_malformed_or_failed_settings_are_unknown(self, get):
        for data in ({}, [], {'subscription_tier_display': None}, {'subscription_tier_display': ''},
                     {'subscription_tier_display': '  '}, {'subscription_tier_display': 1},
                     {'subscription_tier_display': 'x' * 121}, {'allow_access': False},
                     {'subscription_tier_display': 'Bearer secret'}, {'subscription_tier_display': 'Future Plan'}):
            get.return_value = Mock(status_code=200, json=lambda: data)
            self.assertIsNone(qf.fetch_grok_plan('access'))
        for status in (401, 403, 429, 500):
            get.return_value = Mock(status_code=status)
            self.assertIsNone(qf.fetch_grok_plan('access'))
        get.side_effect = requests.Timeout()
        self.assertIsNone(qf.fetch_grok_plan('access'))
        get.side_effect = None
        get.return_value = Mock(status_code=200, json=Mock(side_effect=ValueError()))
        self.assertIsNone(qf.fetch_grok_plan('access'))

    @patch.object(qf.requests, 'get')
    def test_missing_token_never_requests_settings(self, get):
        self.assertIsNone(qf.fetch_grok_plan(''))
        self.assertEqual(qf.fetch_grok_usage('')['plan'], None)
        get.assert_not_called()

    @patch.object(qf, '_attach_grok_reset_credits')
    @patch.object(qf, 'fetch_grok_billing_ex')
    @patch.object(qf, 'fetch_grok_plan')
    def test_plan_is_independent_of_credit_balance(self, plan, billing, resets):
        for label in ('Free', 'SuperGrok Heavy', None):
            for used in (0, 100):
                plan.return_value = label
                billing.return_value = ({'used_percent': used, 'resets_at': None}, False)
                out = qf.fetch_grok_usage('access')
                self.assertEqual(out['plan'], label)
                self.assertEqual(out['confidence'], 'exact')
                self.assertEqual(out['windows'][0]['used_percent'], used)

    @patch.object(qf.requests, 'get', return_value=Mock(status_code=404))
    @patch.object(qf, 'fetch_grok_billing_ex', return_value=(None, False))
    @patch.object(qf, 'fetch_grok_plan', return_value='Free')
    def test_plan_survives_unavailable_usage_api(self, plan, billing, get):
        out = qf.fetch_grok_usage('access')
        self.assertEqual(out['plan'], 'Free')
        self.assertEqual(out['confidence'], 'unsupported')
        self.assertEqual(out['windows'], [])


class GrokProjectionTests(SimpleTestCase):
    def test_v2_preserves_known_grok_names_and_redacts_unrecognized_values(self):
        from hive_platform.quota_projection_v1 import project_quota_snapshot as v1
        from hive_platform.quota_projection_v2 import project_quota_snapshot as v2
        for name in ('Free', 'X Premium+', 'SuperGrok', 'SuperGrok Heavy'):
            raw = {'source': 'grok_xai_usage', 'plan': name, 'access_token': 'secret', 'windows': []}
            projected = v2(raw)
            self.assertEqual(projected['plan'], name)
            self.assertEqual(projected['quota_schema_version'], 2)
            self.assertEqual(v2(projected), projected)
            self.assertNotIn('secret', str(projected))
        self.assertIsNone(v1({'source': 'grok_xai_usage', 'plan': 'SuperGrok Heavy'})['plan'])
        for name in ('Bearer secret', 'sk-secret', 'Future Plan', '', None, [], {}):
            self.assertIsNone(v2({'source': 'grok_xai_usage', 'plan': name})['plan'])
        self.assertEqual(v2({'source': 'codex_wham_usage', 'plan': 'PRO'})['plan'], 'pro')


class GrokPlanPersistenceTests(TestCase):
    @patch.object(qf, '_attach_grok_reset_credits')
    @patch.object(qf, 'fetch_grok_billing_ex', return_value=({'used_percent': 0, 'resets_at': None}, False))
    @patch.object(qf.requests, 'get')
    def test_real_refresh_persist_and_api_keep_plan_without_raw_provider_fields(self, get, billing, resets):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIRequestFactory, force_authenticate
        from hive_platform.models import TokenPool, TokenEntry
        from hive_platform.token_coordinator import refresh_entry_quota_snapshot
        from claws.views import CredentialHealthOverviewView
        pool = TokenPool.objects.create(owner_kind='org', owner_id=TokenPool.ORG_OWNER_ID, provider='xai')
        entry = TokenEntry(pool=pool, label='grok-plan')
        entry.set_token('{"access_token":"opaque-test-token"}')
        entry.save()
        user = get_user_model().objects.create_user(username='plan-staff', is_staff=True)
        for name in ('Free', 'SuperGrok', 'SuperGrok Heavy', 'X Premium+', None):
            get.return_value = Mock(status_code=200, json=lambda: {
                'subscription_tier_display': name, 'token': 'provider-secret', 'allow_access': False,
            })
            refresh_entry_quota_snapshot(entry, force=True, mutate_lease_state=False)
            entry.refresh_from_db()
            self.assertEqual(entry.quota_snapshot['plan'], name)
            request = APIRequestFactory().get('/v1/credential-health/')
            force_authenticate(request, user=user)
            response = CredentialHealthOverviewView.as_view()(request)
            self.assertEqual(response.status_code, 200)
            row = next(a for a in response.data['accounts'] if a['entry_id'] == str(entry.id))
            self.assertEqual(row['quota']['plan'], name)
            self.assertEqual(row['quota_status']['plan'], name)
            self.assertNotIn('provider-secret', str(row))
            self.assertNotIn('allow_access', row['quota'])

        # Check-now returns the fetched snapshot directly, before projection.
        # The fetcher must redact unrecognized tier strings on that path too.
        from claws.views import TokenEntryCheckView
        get.return_value = Mock(status_code=200, json=lambda: {'subscription_tier_display': 'Bearer provider-secret'})
        request = APIRequestFactory().post('/v1/token-pools/check/', {}, format='json')
        force_authenticate(request, user=user)
        response = TokenEntryCheckView.as_view()(request, pool_id=pool.id, entry_id=entry.id)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data['check']['quota']['plan'])
        self.assertNotIn('provider-secret', str(response.data))
