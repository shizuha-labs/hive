"""PLAT-9402 Slice A — the Metering Core read plane (HLD §9).

Covers the four routes added in ``claws/metering_read_views.py``:

* internal service-audience auth fails closed (no/wrong token → 403);
* usage read returns rollup slices + the as-of ``usage_watermark``;
* entitlements read returns balances with exact grant/reset generations and
  decimal-string quantities (the portal's no-float-math contract);
* spend-balance is the redacted aggregate projection (no per-grant rows);
* the ID-subject summary resolves the CALLER'S OWN account server-side —
  two-account isolation is structural (the account id never comes from
  client input, HLD §2/§2.1).
"""
from datetime import datetime
from unittest import mock
from decimal import Decimal

from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from .authentication import FederatedUser
from .metering_models import (
    BillingAccount,
    EntitlementBalance,
    EntitlementGrant,
    UsageRollup,
)
from .metering_read_views import (
    InternalAccountEntitlementsView,
    InternalAccountSpendBalanceView,
    InternalAccountUsageView,
    SubjectUsageSummaryView,
)

TOKEN = 'internal-test-token'
AUTH = {'HTTP_X_HIVE_INTERNAL_TOKEN': TOKEN}


def _dt(day, hour=0):
    # Hive runs USE_TZ=False (hive_project/settings.py) — naive datetimes.
    return datetime(2026, 9, day, hour, 0, 0)


def _account(owner_id, display_name=''):
    return BillingAccount.objects.create(
        account_type=BillingAccount.AccountType.INDIVIDUAL,
        owner_user_id=owner_id,
        display_name=display_name,
    )


def _rollup(account, meter_key, quantity, day=10, gen=1):
    return UsageRollup.objects.create(
        billing_account=account,
        period_start=_dt(day),
        period_end=_dt(day, 23),
        meter_key=meter_key,
        unit='token',
        gross_quantity=Decimal(quantity),
        generation=gen,
        event_count=3,
    )


def _grant_and_balance(account, grant_key='cap.inference.tokens', granted='1000',
                       consumed='400', reserved='100', gen=1):
    grant = EntitlementGrant.objects.create(
        grant_key=grant_key,
        billing_account=account,
        amount=Decimal(granted),
        unit='token',
        canonical_hash='a' * 64,
    )
    balance = EntitlementBalance.objects.create(
        grant=grant,
        billing_account=account,
        reset_generation=gen,
        granted_amount=Decimal(granted),
        reserved_amount=Decimal(reserved),
        consumed_amount=Decimal(consumed),
        available_amount=Decimal(granted) - Decimal(reserved) - Decimal(consumed),
        period_start_utc=_dt(1),
        period_end_utc=_dt(30),
    )
    return grant, balance


class InternalReadAuthTests(TestCase):
    """The service-audience shape fails closed."""

    def setUp(self):
        self.factory = APIRequestFactory()
        self.account = _account(42)

    @override_settings(HIVE_INTERNAL_AUTH_TOKEN=TOKEN)
    def test_missing_token_is_403(self):
        request = self.factory.get(f'/internal/v1/accounts/{self.account.id}/usage')
        response = InternalAccountUsageView.as_view()(request, account_id=self.account.id)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data['error'], 'internal_service_authentication_required')

    @override_settings(HIVE_INTERNAL_AUTH_TOKEN=TOKEN)
    def test_wrong_token_is_403(self):
        request = self.factory.get(
            f'/internal/v1/accounts/{self.account.id}/usage',
            HTTP_X_HIVE_INTERNAL_TOKEN='nope',
        )
        response = InternalAccountUsageView.as_view()(request, account_id=self.account.id)
        self.assertEqual(response.status_code, 403)

    @override_settings(HIVE_INTERNAL_AUTH_TOKEN=TOKEN)
    def test_unknown_account_is_404(self):
        import uuid
        missing = uuid.uuid4()
        request = self.factory.get(
            f'/internal/v1/accounts/{missing}/usage', **AUTH,
        )
        response = InternalAccountUsageView.as_view()(request, account_id=missing)
        self.assertEqual(response.status_code, 404)


class InternalUsageReadTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.account = _account(42, 'acme')
        self.other = _account(43, 'other')

    @override_settings(HIVE_INTERNAL_AUTH_TOKEN=TOKEN)
    def test_usage_returns_slices_and_watermark(self):
        _rollup(self.account, 'inference.input_tokens', '21', day=10)
        _rollup(self.account, 'inference.output_tokens', '8', day=11)
        _rollup(self.other, 'inference.input_tokens', '999', day=11)

        request = self.factory.get(
            f'/internal/v1/accounts/{self.account.id}/usage', **AUTH,
        )
        response = InternalAccountUsageView.as_view()(request, account_id=self.account.id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data['rollups']), 2)
        self.assertEqual(
            {r['meter_key'] for r in response.data['rollups']},
            {'inference.input_tokens', 'inference.output_tokens'},
        )
        # Quantities are decimal strings (the portal's no-float contract).
        self.assertIsInstance(response.data['rollups'][0]['gross_quantity'], str)
        # As-of watermark = newest rebuild across the account's slices.
        self.assertIsNotNone(response.data['usage_watermark'])

    @override_settings(HIVE_INTERNAL_AUTH_TOKEN=TOKEN)
    def test_period_filters_bound_the_window(self):
        _rollup(self.account, 'inference.input_tokens', '21', day=10)
        _rollup(self.account, 'agent.runtime_seconds', '60', day=20)

        request = self.factory.get(
            f'/internal/v1/accounts/{self.account.id}/usage',
            {'period_start': _dt(15).isoformat()}, **AUTH,
        )
        response = InternalAccountUsageView.as_view()(request, account_id=self.account.id)
        self.assertEqual(
            [r['meter_key'] for r in response.data['rollups']],
            ['agent.runtime_seconds'],
        )

    @override_settings(HIVE_INTERNAL_AUTH_TOKEN=TOKEN)
    def test_empty_account_has_null_watermark(self):
        request = self.factory.get(
            f'/internal/v1/accounts/{self.account.id}/usage', **AUTH,
        )
        response = InternalAccountUsageView.as_view()(request, account_id=self.account.id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['rollups'], [])
        self.assertIsNone(response.data['usage_watermark'])


class InternalEntitlementsAndSpendTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.account = _account(42)

    @override_settings(HIVE_INTERNAL_AUTH_TOKEN=TOKEN)
    def test_entitlements_return_exact_generations(self):
        _, balance = _grant_and_balance(self.account)

        request = self.factory.get(
            f'/internal/v1/accounts/{self.account.id}/entitlements', **AUTH,
        )
        response = InternalAccountEntitlementsView.as_view()(request, account_id=self.account.id)

        self.assertEqual(response.status_code, 200)
        entry = response.data['entitlements'][0]
        self.assertEqual(entry['grant_key'], 'cap.inference.tokens')
        self.assertEqual(entry['reset_generation'], 1)
        self.assertEqual(entry['granted_amount'], '1000.000000')
        self.assertEqual(entry['consumed_amount'], '400.000000')
        self.assertEqual(entry['available_amount'], '500.000000')

    @override_settings(HIVE_INTERNAL_AUTH_TOKEN=TOKEN)
    def test_spend_balance_is_redacted_aggregate(self):
        _grant_and_balance(self.account, granted='1000', consumed='400', reserved='100')
        _grant_and_balance(
            self.account, grant_key='cap.storage.bytes',
            granted='500', consumed='50', reserved='0',
        )

        request = self.factory.get(
            f'/internal/v1/accounts/{self.account.id}/spend-balance', **AUTH,
        )
        response = InternalAccountSpendBalanceView.as_view()(request, account_id=self.account.id)

        self.assertEqual(response.status_code, 200)
        spend = response.data['spend']
        # 500 + 450 available across the two grants (granted − reserved − consumed).
        self.assertEqual(spend['available'], '950.000000')
        self.assertEqual(spend['reserved'], '100.000000')
        self.assertEqual(spend['held'], '100.000000')
        # Redacted: no per-grant rows leak through the spend projection.
        self.assertNotIn('entitlements', response.data)


class SubjectSummaryIsolationTests(TestCase):
    """The portal read: subject-scoped, structurally isolated per account.

    ``resolve_default_account`` is patched to a deterministic owner→account
    map: its own race-safety (Postgres advisory locks, ``hashtext``) is
    core-tested by the PLAT-7006 suite and has no sqlite equivalent. What is
    under test here is the VIEW's contract — resolve the CALLER'S subject
    server-side and return only that account's data.
    """

    def setUp(self):
        self.factory = APIRequestFactory()
        self.account_a = _account(7, 'owner-a')
        self.account_b = _account(8, 'owner-b')
        _rollup(self.account_a, 'inference.input_tokens', '21', day=10)
        _rollup(self.account_b, 'inference.input_tokens', '777', day=10)
        _grant_and_balance(self.account_a, granted='1000', consumed='400', reserved='100')
        _grant_and_balance(self.account_b, granted='9000', consumed='1', reserved='0')
        self._accounts = {7: self.account_a, 8: self.account_b}
        patcher = mock.patch(
            'claws.metering_read_views.resolve_default_account',
            side_effect=lambda *, account_type, owner_user_id, **kw: self._accounts[owner_user_id],
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _summary_for(self, user_id):
        request = self.factory.get('/api/v1/usage/summary')
        force_authenticate(request, user=FederatedUser(
            user_id=user_id, email=f'u{user_id}@example.com', username=f'u{user_id}',
        ))
        return SubjectUsageSummaryView.as_view()(request)

    def test_summary_resolves_the_callers_own_account(self):
        response = self._summary_for(7)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['account']['id'], str(self.account_a.id))
        self.assertEqual(
            [r['gross_quantity'] for r in response.data['usage']['rollups']],
            ['21.000000'],
        )
        self.assertEqual(response.data['entitlements']['entitlements'][0]['granted_amount'],
                         '1000.000000')
        self.assertEqual(response.data['spend']['available'], '500.000000')

    def test_two_account_isolation_is_structural(self):
        """Account B's data never appears in account A's summary — and vice
        versa. The account id never enters the query from client input, so a
        caller cannot name another account (HLD §2/§2.1)."""
        response_a = self._summary_for(7)
        response_b = self._summary_for(8)

        self.assertNotEqual(
            response_a.data['account']['id'], response_b.data['account']['id'],
        )
        self.assertEqual(
            [r['gross_quantity'] for r in response_a.data['usage']['rollups']],
            ['21.000000'],
        )
        self.assertEqual(
            [r['gross_quantity'] for r in response_b.data['usage']['rollups']],
            ['777.000000'],
        )
        self.assertEqual(response_b.data['spend']['available'], '8999.000000')

    def test_unauthenticated_summary_is_rejected(self):
        request = self.factory.get('/api/v1/usage/summary')
        response = SubjectUsageSummaryView.as_view()(request)
        # DRF yields 401 (no authenticate_header on the federated class) or
        # 403 depending on the auth negotiation — both are rejections.
        self.assertIn(response.status_code, (401, 403))
