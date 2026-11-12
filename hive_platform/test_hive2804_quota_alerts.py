"""HIVE-2804: one open alert per (org, entry, condition); clear resolves it."""

from threading import Barrier, Thread
from unittest import mock

from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase

from hive_platform.models import ProviderQuotaAlert, TokenEntry, TokenPool
from hive_platform.quota_alerts import sync_quota_alerts


class QuotaAlertIdempotencyTests(TestCase):
    def setUp(self):
        self.pool = TokenPool.objects.create(
            owner_kind='org', owner_id='org-a', provider='anthropic',
        )
        self.entry = TokenEntry.objects.create(
            pool=self.pool,
            label='a',
            token_ciphertext='x',
            wrapped_dek='y',
            dek_key_id='k',
            token_fingerprint='fp',
        )

    def test_concurrent_shape_opens_once_and_resolves(self):
        snap = {'windows': [{'used_percent': 90}], 'fetched_at': 't1'}
        sync_quota_alerts(self.entry, snap)
        sync_quota_alerts(self.entry, snap)
        open_rows = ProviderQuotaAlert.objects.filter(
            org_id='org-a', entry=self.entry, condition='low_quota', state='open',
        )
        self.assertEqual(open_rows.count(), 1)
        sync_quota_alerts(self.entry, {'windows': [{'used_percent': 10}]})
        self.assertEqual(open_rows.count(), 0)
        self.assertEqual(
            ProviderQuotaAlert.objects.filter(
                org_id='org-a', entry=self.entry, condition='low_quota', state='resolved',
            ).count(),
            1,
        )

    def test_other_org_does_not_see_this_condition(self):
        other = TokenPool.objects.create(owner_kind='org', owner_id='org-b', provider='anthropic')
        entry_b = TokenEntry.objects.create(
            pool=other, label='b', token_ciphertext='x', wrapped_dek='y',
            dek_key_id='k', token_fingerprint='fp2',
        )
        sync_quota_alerts(self.entry, {'detail': 'revoked'})
        self.assertFalse(
            ProviderQuotaAlert.objects.filter(org_id='org-b', entry=entry_b).exists()
        )
        self.assertEqual(
            ProviderQuotaAlert.objects.get(entry=self.entry).diagnostic,
            'revoked_token',
        )

    def test_org_alias_collapses_and_does_not_cross_tenants(self):
        from hive_platform.quota_alerts import canonical_org_id
        self.assertEqual(canonical_org_id('Org-A'), canonical_org_id(' org-a '))
        self.assertNotEqual(canonical_org_id('Org-A'), canonical_org_id('org-b'))
        aliased = TokenPool.objects.create(
            owner_kind='org', owner_id='Org-A', provider='anthropic',
        )
        entry_alias = TokenEntry.objects.create(
            pool=aliased, label='alias', token_ciphertext='x', wrapped_dek='y',
            dek_key_id='k', token_fingerprint='fp3',
        )
        sync_quota_alerts(entry_alias, {'detail': 'revoked'})
        self.assertTrue(
            ProviderQuotaAlert.objects.filter(org_id='org-a', entry=entry_alias).exists()
        )
        self.assertFalse(
            ProviderQuotaAlert.objects.filter(org_id='Org-A').exists()
        )
        self.assertFalse(
            ProviderQuotaAlert.objects.filter(org_id='org-b', entry=entry_alias).exists()
        )

    def test_clear_comments_existing_pulse_item(self):
        snap = {'windows': [{'used_percent': 90}]}
        sync_quota_alerts(self.entry, snap)
        row = ProviderQuotaAlert.objects.get(entry=self.entry, condition='low_quota')
        row.pulse_item_key = 'HIVE-1'
        row.save(update_fields=['pulse_item_key'])
        with mock.patch('requests.post') as post:
            with self.settings(HIVE_PULSE_API_URL='http://pulse.local', HIVE_PULSE_API_TOKEN='t'):
                sync_quota_alerts(self.entry, {'windows': [{'used_percent': 10}]})
        post.assert_called_once()
        self.assertIn('/tasks/HIVE-1/comments/', post.call_args.args[0])
        row.refresh_from_db()
        self.assertEqual(row.last_transition, 'resolved')
        self.assertEqual(row.service, 'hive')

    def test_open_row_without_pulse_key_refiles(self):
        sync_quota_alerts(self.entry, {'windows': [{'used_percent': 90}]})
        row = ProviderQuotaAlert.objects.get(entry=self.entry, condition='low_quota')
        self.assertEqual(row.pulse_item_key, '')
        self.assertEqual(row.actor, 'hive-quota-refresh')
        resp = mock.Mock(status_code=201)
        resp.json.return_value = {'item_key': 'HIVE-9'}
        with mock.patch('requests.post', return_value=resp) as post:
            with self.settings(HIVE_PULSE_API_URL='http://pulse.local', HIVE_PULSE_API_TOKEN='t'):
                sync_quota_alerts(self.entry, {'windows': [{'used_percent': 91}]})
        post.assert_called_once()
        row.refresh_from_db()
        self.assertEqual(row.pulse_item_key, 'HIVE-9')
        self.assertEqual(row.last_transition, 'filed')
        with mock.patch('requests.post') as post:
            with self.settings(HIVE_PULSE_API_URL='http://pulse.local', HIVE_PULSE_API_TOKEN='t'):
                sync_quota_alerts(self.entry, {'windows': [{'used_percent': 92}]})
        post.assert_not_called()

    def test_credential_health_hides_other_org_alerts(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws.views import CredentialHealthOverviewView

        User = get_user_model()
        user_a = User.objects.create_user(username='org-a-user', email='a@example.com')
        user_b = User.objects.create_user(username='org-b-user', email='b@example.com')
        pool_b = TokenPool.objects.create(
            owner_kind='user', owner_id=str(user_b.id), provider='anthropic',
        )
        entry_b = TokenEntry.objects.create(
            pool=pool_b, label='b', token_ciphertext='x', wrapped_dek='y',
            dek_key_id='k', token_fingerprint='fp-b',
        )
        sync_quota_alerts(entry_b, {'detail': 'revoked'})
        request = APIRequestFactory().get('/v1/credential-health/')
        force_authenticate(request, user=user_a)
        response = CredentialHealthOverviewView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        body = str(response.data)
        self.assertNotIn(str(entry_b.id), body)
        self.assertNotIn('revoked_token', body)
        request_b = APIRequestFactory().get('/v1/credential-health/')
        force_authenticate(request_b, user=user_b)
        response_b = CredentialHealthOverviewView.as_view()(request_b)
        row = next(a for a in response_b.data['accounts'] if a['entry_id'] == str(entry_b.id))
        self.assertEqual(row['open_alert_conditions'], ['revoked_token'])


class QuotaAlertConcurrentInsertTests(TransactionTestCase):
    def test_two_threads_open_one_row(self):
        pool = TokenPool.objects.create(owner_kind='org', owner_id='org-a', provider='anthropic')
        entry = TokenEntry.objects.create(
            pool=pool, label='race', token_ciphertext='x', wrapped_dek='y',
            dek_key_id='k', token_fingerprint='fp-race',
        )
        entry_id = entry.pk
        barrier = Barrier(2)
        errors = []

        def worker():
            close_old_connections()
            try:
                from hive_platform.models import TokenEntry as Entry
                row = Entry.objects.get(pk=entry_id)
                barrier.wait(timeout=5)
                sync_quota_alerts(row, {'windows': [{'used_percent': 95}]})
            except Exception as exc:
                errors.append(exc)
            finally:
                connection.close()

        threads = [Thread(target=worker), Thread(target=worker)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(
            ProviderQuotaAlert.objects.filter(
                org_id='org-a', entry_id=entry_id, condition='low_quota', state='open',
            ).count(),
            1,
        )
