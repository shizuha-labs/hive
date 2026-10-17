"""HIVE-314 slice 2 (HIVE-2166) — org-provider-resolve endpoint isolation tests.

Proves the four load-bearing trust boundaries of ``OrgProviderResolveView``
(contract HIVE-1862 §3; runtime carrier HIVE-2166):

1. Broker-gated + non-enumerable: a user/admin JWT gets the same 404 envelope
   as every other refused/absent outcome — the endpoint cannot be probed.
2. The org is server-derived from the broker token's minted claims; a
   body-supplied org is never trusted.
3. Membership is proven against the ADMIN org-tenancy boundary, FAIL-CLOSED:
   Admin unavailable / org absent / principal not a member → the same 404.
4. Pre-HIVE-1889, a membership-proven resolution honestly returns
   ``no_provider_configured`` (the runtime's normal unavailable outcome) and
   the response never carries key material.
"""
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from .authentication import FederatedUser
from .coordinator_views import OrgProviderResolveView

COORD_SETTINGS = dict(
    HIVE_COORDINATOR_AUDIENCE='hive-coordinator',
    HIVE_COORDINATOR_BROKER_SCOPE='coordinator:model-token',
)

factory = APIRequestFactory()


def _user(uid):
    return FederatedUser(user_id=uid, email=f'u{uid}@x.io', username=f'u{uid}')


def _user_claims(uid):
    return {'user_id': uid, 'aud': ['shizuha-id']}


def _org_broker_claims(org_slug, workload_id='sidecar-broker'):
    return {
        'user_id': 0,
        'aud': ['hive-coordinator'],
        'scope': 'coordinator:model-token',
        'owner_kind': 'org',
        'owner_id': org_slug,
        'workload_id': workload_id,
    }


def _call(claims, data=None, user=None):
    req = factory.post('/coordinator/org-provider-resolve', data or {}, format='json')
    force_authenticate(req, user=user or _user(0), token=claims)
    return OrgProviderResolveView.as_view()(req)


@override_settings(**COORD_SETTINGS)
class OrgProviderResolveIsolationTest(TestCase):
    def test_01_user_jwt_refused_with_opaque_404(self):
        """A plain owner JWT is answered with the same envelope as absence (§5.8)."""
        resp = _call(_user_claims(7), {'principal': 'agent@shizuha.com'})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.data, {'error': 'Not found'})

    def test_02_non_org_broker_refused(self):
        """A broker token without an org owner scope has nothing to resolve."""
        claims = _org_broker_claims('shizuha')
        claims['owner_kind'] = 'user'
        claims['owner_id'] = '42'
        resp = _call(claims, {'principal': 'agent@shizuha.com'})
        self.assertEqual(resp.status_code, 404)

    def test_03_missing_principal_refused(self):
        resp = _call(_org_broker_claims('shizuha'), {})
        self.assertEqual(resp.status_code, 404)

    def test_04_membership_unproven_fail_closed(self):
        """Admin unavailable → membership cannot be proven → refused (fail-closed)."""
        with patch(
            'fleet.org_membership_sync._existing_org_member_emails',
            return_value=(None, {'status': 'admin_unavailable', 'detail': 'boom'}),
        ):
            resp = _call(_org_broker_claims('shizuha'), {'principal': 'agent@shizuha.com'})
        self.assertEqual(resp.status_code, 404)

    def test_05_non_member_refused_with_same_envelope(self):
        """A principal outside the org is indistinguishable from an absent org."""
        with patch(
            'fleet.org_membership_sync._existing_org_member_emails',
            return_value=({'other@shizuha.com'}, None),
        ):
            resp = _call(_org_broker_claims('shizuha'), {'principal': 'agent@shizuha.com'})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.data, {'error': 'Not found'})

    def test_06_proven_member_pre_model_no_provider_configured(self):
        """Membership proven + reference registry not landed → honest unavailable."""
        with patch(
            'fleet.org_membership_sync._existing_org_member_emails',
            return_value=({'agent@shizuha.com'}, None),
        ):
            resp = _call(_org_broker_claims('shizuha'), {'principal': 'agent@shizuha.com'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['outcome'], 'unavailable')
        self.assertEqual(resp.data['reason'], 'no_provider_configured')
        self.assertEqual(resp.data['org'], 'shizuha')

    def test_07_body_org_never_trusted(self):
        """The org comes from minted claims; a body org is ignored (§2)."""
        with patch(
            'fleet.org_membership_sync._existing_org_member_emails',
            return_value=({'someone-else@shizuha.com'}, None),
        ) as mock_members:
            resp = _call(
                _org_broker_claims('shizuha'),
                {'principal': 'agent@other-org.com', 'org': 'other-org'},
            )
        # The membership read used the CLAIM org, not the body org.
        mock_members.assert_called_once_with('shizuha')
        # The principal is not a member of the CLAIM org → refused, regardless
        # of the body-supplied org claim.
        self.assertEqual(resp.status_code, 404)

    def test_08_response_never_carries_key_material(self):
        with patch(
            'fleet.org_membership_sync._existing_org_member_emails',
            return_value=({'agent@shizuha.com'}, None),
        ):
            resp = _call(_org_broker_claims('shizuha'), {'principal': 'agent@shizuha.com'})
        body = str(resp.data)
        for marker in ('sk-', 'Bearer', 'api_key', 'secret', 'token_value'):
            self.assertNotIn(marker, body)
