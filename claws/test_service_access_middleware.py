from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory

from claws.middleware import ServiceAccessMiddleware


@override_settings(
    HIVE_COORDINATOR_AUDIENCE='hive-coordinator',
    HIVE_COORDINATOR_BROKER_SCOPE='coordinator:model-token',
)
class ServiceAccessMiddlewareTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    def _call(self, claims):
        middleware = ServiceAccessMiddleware(lambda request: 'ok')
        request = self.factory.get(
            '/hive/api/v1/coordinator/model-token',
            HTTP_AUTHORIZATION='Bearer test-token',
        )
        with patch('claws.middleware.FederatedJWTAuthentication.validate_token',
                   return_value=('validated-token', claims)):
            return middleware(request)

    def test_restrictive_user_token_without_hive_service_is_rejected(self):
        response = self._call({
            'user_id': 10,
            'enabled_services': ['pulse'],
        })

        self.assertEqual(response.status_code, 403)

    def test_user_token_with_hive_service_is_allowed(self):
        response = self._call({
            'user_id': 10,
            'enabled_services': ['hive'],
        })

        self.assertEqual(response, 'ok')

    def test_coordinator_broker_token_bypasses_end_user_service_grant(self):
        response = self._call({
            'user_id': 0,
            'aud': 'hive-coordinator',
            'scope': 'coordinator:model-token',
            'org_workload': True,
            'enabled_services': [],
        })

        self.assertEqual(response, 'ok')
