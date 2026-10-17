"""HIVE-2057: /v1/fleet/organizations/ must not surface a confusing
"Admin API rejected roster update" 403 for non-staff.

Root cause: Hive's ``_admin_organizations`` forwarded the caller's USER bearer
token to Admin's PUBLIC /organizations/ list, which Admin's
ServiceAccessMiddleware gates on the 'admin' service grant. A non-staff user
with hive entitlement (but no 'admin' service) was rejected 403 by Admin, and
Hive wrapped it as "Admin API rejected roster update".

Fix: route through Admin's INTERNAL user-organizations endpoint
(/api/internal/users/<id>/organizations/) with the Hive service-token headers.
Internal routes bypass ServiceAccessMiddleware and return org-scoped orgs for
the caller (no cross-tenant leak) — restoring the 08-07 org-scoped 200.
"""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from fleet.views import _admin_organizations


def _response(status_code, payload=None):
    resp = Mock(status_code=status_code)
    resp.json.return_value = payload if payload is not None else {}
    return resp


@override_settings(
    HIVE_ADMIN_API_URL='http://admin/api',
    ADMIN_INTERNAL_SERVICE_TOKEN='service-token',
)
class AdminOrganizationsInternalEndpointTests(SimpleTestCase):
    """The org list must use Admin's INTERNAL endpoint with service headers."""

    def _request(self, *, user_id=13, email='kai@shizuha.com', is_staff=False):
        return SimpleNamespace(user=SimpleNamespace(
            id=user_id, email=email, is_staff=is_staff, is_authenticated=True))

    @patch('fleet.views.requests.get')
    def test_non_staff_uses_internal_endpoint_with_service_headers(self, get):
        get.return_value = _response(200, {'count': 2, 'organizations': [
            {'id': 9, 'slug': 'personal-13', 'name': "Kai's Workspace"},
            {'id': 1, 'slug': 'shizuha', 'name': 'Shizuha Global Pvt. Ltd'},
        ]})

        orgs, error = _admin_organizations(self._request())

        self.assertIsNone(error)
        self.assertEqual([o['slug'] for o in orgs], ['personal-13', 'shizuha'])
        # Must hit the INTERNAL endpoint, never the public staff-gated list.
        get.assert_called_once()
        args, kwargs = get.call_args
        self.assertIn('/internal/users/13/organizations/', args[0])
        self.assertNotIn('/api/organizations/', args[0])
        self.assertNotIn('/organizations/', args[0].replace('/internal/users/13/organizations/', ''))
        self.assertEqual(kwargs['headers']['X-Internal-Service'], 'shizuha-hive')
        self.assertEqual(kwargs['headers']['X-Internal-Service-Token'], 'service-token')
        self.assertEqual(kwargs['params'], {'email': 'kai@shizuha.com'})

    @patch('fleet.views.requests.get')
    def test_staff_also_uses_internal_endpoint(self, get):
        get.return_value = _response(200, {'count': 1, 'organizations': [
            {'id': 1, 'slug': 'shizuha', 'name': 'Shizuha Global Pvt. Ltd'},
        ]})

        orgs, error = _admin_organizations(self._request(is_staff=True))

        self.assertIsNone(error)
        self.assertEqual([o['slug'] for o in orgs], ['shizuha'])
        args, _ = get.call_args
        self.assertIn('/internal/users/13/organizations/', args[0])

    @patch('fleet.views.requests.get')
    def test_admin_403_surfaces_typed_error_not_roster_update(self, get):
        # Even if Admin rejects, the detail must be surfaced transparently
        # (never a bare "roster update" on a GET list).
        get.return_value = _response(403, {
            'error': 'You do not have access to this service.',
            'service': 'admin',
            'code': 'service_access_denied',
        })

        orgs, error = _admin_organizations(self._request())

        self.assertIsNone(orgs)
        self.assertEqual(error.status_code, 403)
        self.assertIn('Admin API rejected roster update', error.data['error'])
        self.assertEqual(error.data['detail']['code'], 'service_access_denied')

    @patch('fleet.views.requests.get')
    def test_admin_unreachable_returns_502(self, get):
        from requests import RequestException
        get.side_effect = RequestException('boom')

        orgs, error = _admin_organizations(self._request())

        self.assertIsNone(orgs)
        self.assertEqual(error.status_code, 502)
        self.assertIn('Admin API unreachable', error.data['error'])

    def test_unauthenticated_returns_401(self):
        request = SimpleNamespace(user=SimpleNamespace(id=None, email=''))
        orgs, error = _admin_organizations(request)
        self.assertIsNone(orgs)
        self.assertEqual(error.status_code, 401)
