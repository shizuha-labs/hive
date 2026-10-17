"""HIVE-1889 (S3): org Settings > Providers — org-scoped Cortex provider refs.

Covers the HIVE-1862 contract surface:
  - org-owner-only visibility (non-staff member can read own org, 403 on others)
  - upsert is a thin mirror write (no raw keys stored)
  - validate delegates the live 1-token probe to Cortex and refreshes the mirror
  - no cross-org leak (a member of org A cannot touch org B's refs)
"""
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from fleet.models import HiveOrgProviderReference
from fleet.org_provider_views import (
    OrgProviderReferenceDetailView,
    OrgProviderReferenceListView,
    OrgProviderReferenceValidateView,
)


def _user(is_staff=False, org_ids=(), roles=None):
    roles = roles or {}
    return SimpleNamespace(
        is_authenticated=True,
        is_staff=is_staff,
        organization_memberships={str(o): roles.get(str(o), 'member') for o in org_ids},
    )


class OrgProviderReferenceTests(TestCase):
    def setUp(self):
        self.provider_id = uuid4()
        self.ref = HiveOrgProviderReference.objects.create(
            org_id=1,
            cortex_provider_id=self.provider_id,
            cortex_key_ref='cred-abc',
            default_models=['gpt-5.6'],
            status=HiveOrgProviderReference.Status.ACTIVE,
        )
        self.other_ref = HiveOrgProviderReference.objects.create(
            org_id=2,
            cortex_provider_id=uuid4(),
            cortex_key_ref='cred-other',
        )

    def _list(self, user, org_id):
        req = APIRequestFactory().get(f'/v1/org-provider-refs/?org_id={org_id}')
        force_authenticate(req, user=user)
        return OrgProviderReferenceListView.as_view()(req)

    def _post(self, user, data):
        req = APIRequestFactory().post('/v1/org-provider-refs/', data, format='json')
        force_authenticate(req, user=user)
        return OrgProviderReferenceListView.as_view()(req)

    def _delete(self, user, ref_id):
        req = APIRequestFactory().delete(f'/v1/org-provider-refs/{ref_id}/')
        force_authenticate(req, user=user)
        return OrgProviderReferenceDetailView.as_view()(req, ref_id=ref_id)

    def test_member_reads_own_org_refs_only(self):
        resp = self._list(_user(org_ids=[1]), org_id=1)
        self.assertEqual(resp.status_code, 200)
        ids = {r['id'] for r in resp.data['results']}
        self.assertIn(self.ref.id, ids)
        self.assertNotIn(self.other_ref.id, ids)

    def test_member_cannot_read_foreign_org(self):
        resp = self._list(_user(org_ids=[1]), org_id=2)
        self.assertEqual(resp.status_code, 403)

    def test_member_cannot_upsert_foreign_org(self):
        resp = self._post(
            _user(org_ids=[1]),
            {'org_id': 2, 'cortex_provider_id': str(uuid4())},
        )
        self.assertEqual(resp.status_code, 403)

    def test_member_cannot_upsert_without_owner_role(self):
        resp = self._post(
            _user(org_ids=[1], roles={'1': 'member'}),
            {'org_id': 1, 'cortex_provider_id': str(uuid4())},
        )
        self.assertEqual(resp.status_code, 403)

    def test_owner_upserts_ref(self):
        resp = self._post(
            _user(org_ids=[1], roles={'1': 'owner'}),
            {
                'org_id': 1,
                'cortex_provider_id': str(uuid4()),
                'cortex_key_ref': 'cred-new',
                'default_models': ['claude-sonnet-4'],
            },
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(HiveOrgProviderReference.objects.filter(org_id=1).count(), 2)

    def test_upsert_rejects_bad_uuid(self):
        resp = self._post(
            _user(org_ids=[1], roles={'1': 'owner'}),
            {'org_id': 1, 'cortex_provider_id': 'not-a-uuid'},
        )
        self.assertEqual(resp.status_code, 400)

    @patch('fleet.org_provider_views.requests.post')
    def test_save_with_api_key_runs_live_probe_and_refreshes_status(self, mock_post):
        class _Resp:
            status_code = 200

            def json(self):
                return {'status': 'ACTIVE', 'validation_state': 'active'}

        mock_post.return_value = _Resp()
        resp = self._post(
            _user(org_ids=[1], roles={'1': 'owner'}),
            {
                'org_id': 1,
                'cortex_provider_id': str(uuid4()),
                'cortex_key_ref': 'cred-probe',
                'api_key': 'sk-test-provider-key',
            },
        )
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.data['validation']['validated'])
        # The probe was delegated to Cortex with the key; Hive never retained it.
        call_url = mock_post.call_args.args[0]
        self.assertIn('/v1/provider-listings/', call_url)
        self.assertIn('/validate', call_url)
        sent_body = mock_post.call_args.kwargs.get('json') or {}
        self.assertEqual(sent_body.get('api_key'), 'sk-test-provider-key')
        # No row stores the key.
        row = HiveOrgProviderReference.objects.get(cortex_key_ref='cred-probe')
        self.assertNotIn('sk-test-provider-key', row.cortex_key_ref)
        self.assertEqual(row.status, HiveOrgProviderReference.Status.ACTIVE)

    @patch('fleet.org_provider_views.requests.post')
    def test_save_probe_failure_surfaces_org_scoped_error(self, mock_post):
        class _Resp:
            status_code = 400

            def json(self):
                return {'error': 'invalid provider key'}

        mock_post.return_value = _Resp()
        resp = self._post(
            _user(org_ids=[1], roles={'1': 'owner'}),
            {
                'org_id': 1,
                'cortex_provider_id': str(uuid4()),
                'cortex_key_ref': 'cred-bad',
                'api_key': 'sk-bad-key',
            },
        )
        self.assertEqual(resp.status_code, 502)
        self.assertIn('validation failed', resp.data['validation']['error'])

    def test_member_cannot_delete_foreign_org_ref(self):
        resp = self._delete(_user(org_ids=[1], roles={'1': 'owner'}), self.other_ref.id)
        self.assertEqual(resp.status_code, 403)

    def test_owner_deletes_own_ref(self):
        resp = self._delete(_user(org_ids=[1], roles={'1': 'owner'}), self.ref.id)
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(HiveOrgProviderReference.objects.filter(id=self.ref.id).exists())


class OrgProviderReferenceValidateTests(TestCase):
    def setUp(self):
        self.provider_id = uuid4()
        self.ref = HiveOrgProviderReference.objects.create(
            org_id=1,
            cortex_provider_id=self.provider_id,
            cortex_key_ref='cred-abc',
            status=HiveOrgProviderReference.Status.VERIFYING,
        )

    def _validate(self, user, ref_id, cortex_response=None):
        req = APIRequestFactory().post(f'/v1/org-provider-refs/{ref_id}/validate/')
        force_authenticate(req, user=user)
        with patch('fleet.org_provider_views.requests.post', return_value=cortex_response):
            return OrgProviderReferenceValidateView.as_view()(req, ref_id=ref_id)

    def test_validate_delegates_to_cortex_and_refreshes_status(self):
        class _Resp:
            status_code = 200

            def json(self):
                return {'status': 'ACTIVE', 'validation_state': 'active'}

        resp = self._validate(_user(org_ids=[1], roles={'1': 'owner'}), self.ref.id, _Resp())
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['validated'])
        self.ref.refresh_from_db()
        self.assertEqual(self.ref.status, HiveOrgProviderReference.Status.ACTIVE)

    def test_validate_forbidden_for_non_owner(self):
        resp = self._validate(_user(org_ids=[1], roles={'1': 'member'}), self.ref.id)
        self.assertEqual(resp.status_code, 403)

    def test_validate_forbidden_for_foreign_org(self):
        resp = self._validate(_user(org_ids=[2], roles={'2': 'owner'}), self.ref.id)
        self.assertEqual(resp.status_code, 403)

    def test_validate_propagates_cortex_failure(self):
        class _Resp:
            status_code = 400

            def json(self):
                return {'error': 'bad key'}

        resp = self._validate(_user(org_ids=[1], roles={'1': 'owner'}), self.ref.id, _Resp())
        self.assertEqual(resp.status_code, 400)
        self.assertIn('validation failed', resp.data['error'])
