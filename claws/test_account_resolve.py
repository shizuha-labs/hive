"""PLAT-7006 (R1B) — server-derived default BillingAccount resolution.

Producers never derive or choose a payer (HLD §2/§2.1): they name the owner
subject and the core resolves the canonical default account.  These tests pin
the resolve contract: idempotent same-owner resolution, deterministic default
when legacy accounts exist, strict owner-kind validation, and service-token
auth on the HTTP surface.
"""
from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory

from .metering_core import MeteringCoreError, resolve_default_account
from .metering_models import BillingAccount
from .metering_views import InternalAccountResolveView


class ResolveDefaultAccountServiceTests(TestCase):
    def test_individual_resolve_creates_then_reuses_same_account(self):
        first = resolve_default_account(account_type='individual', owner_user_id=42)
        second = resolve_default_account(account_type='individual', owner_user_id=42)
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.account_type, BillingAccount.AccountType.INDIVIDUAL)
        self.assertEqual(first.owner_user_id, 42)
        self.assertEqual(BillingAccount.objects.filter(owner_user_id=42).count(), 1)

    def test_organization_resolve_creates_then_reuses_same_account(self):
        first = resolve_default_account(account_type='organization', owner_org_id='7')
        second = resolve_default_account(account_type='organization', owner_org_id='7')
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.owner_org_id, '7')
        self.assertEqual(BillingAccount.objects.filter(owner_org_id='7').count(), 1)

    def test_default_is_oldest_existing_account_for_owner(self):
        legacy = BillingAccount.objects.create(
            account_type=BillingAccount.AccountType.INDIVIDUAL, owner_user_id=42
        )
        BillingAccount.objects.create(
            account_type=BillingAccount.AccountType.INDIVIDUAL, owner_user_id=42
        )
        resolved = resolve_default_account(account_type='individual', owner_user_id=42)
        self.assertEqual(resolved.id, legacy.id)

    def test_owner_kinds_are_strictly_separated(self):
        with self.assertRaises(MeteringCoreError):
            resolve_default_account(account_type='individual', owner_user_id=None)
        with self.assertRaises(MeteringCoreError):
            resolve_default_account(
                account_type='individual', owner_user_id=42, owner_org_id='7'
            )
        with self.assertRaises(MeteringCoreError):
            resolve_default_account(account_type='organization', owner_org_id='')
        with self.assertRaises(MeteringCoreError):
            resolve_default_account(
                account_type='organization', owner_org_id='7', owner_user_id=42
            )
        with self.assertRaises(MeteringCoreError):
            resolve_default_account(account_type='partnership', owner_user_id=42)

    def test_different_owners_resolve_to_different_accounts(self):
        a = resolve_default_account(account_type='individual', owner_user_id=42)
        b = resolve_default_account(account_type='individual', owner_user_id=43)
        self.assertNotEqual(a.id, b.id)


@override_settings(METERING_CORE_SERVICE_TOKEN='test-service-token')
class AccountResolveEndpointTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    def _post(self, body, token='test-service-token'):
        request = self.factory.post(
            '/internal/v1/accounts:resolve',
            body,
            format='json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        response = InternalAccountResolveView.as_view()(request)
        response.render()
        return response

    def test_requires_service_authentication(self):
        request = self.factory.post(
            '/internal/v1/accounts:resolve',
            {'account_type': 'individual', 'owner_user_id': 42},
            format='json',
        )
        response = InternalAccountResolveView.as_view()(request)
        self.assertEqual(response.status_code, 403)

    def test_resolve_is_idempotent_over_http(self):
        first = self._post({'account_type': 'individual', 'owner_user_id': 42})
        self.assertEqual(first.status_code, 200)
        second = self._post({'account_type': 'individual', 'owner_user_id': 42})
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.data['billing_account_id'], second.data['billing_account_id'])
        self.assertEqual(BillingAccount.objects.filter(owner_user_id=42).count(), 1)

    def test_invalid_owner_kind_is_a_clean_400(self):
        response = self._post({'account_type': 'individual'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['error'], 'resolve_failed')

    def test_non_integer_owner_user_id_is_a_clean_400(self):
        response = self._post({'account_type': 'individual', 'owner_user_id': 'abc'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['error'], 'invalid_owner')
