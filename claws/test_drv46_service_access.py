import json
from unittest.mock import patch

from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase
from rest_framework.exceptions import AuthenticationFailed

from claws.authentication import FederatedUser
from claws.middleware import ServiceAccessMiddleware


class ServiceAccessMiddlewareTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _middleware(self):
        return ServiceAccessMiddleware(lambda request: HttpResponse('ok'))

    def test_drive_only_token_is_denied_at_hive_boundary(self):
        request = self.factory.get('/api/v1/me', HTTP_AUTHORIZATION='Bearer token')
        with patch(
            'claws.middleware.FederatedJWTAuthentication.validate_token',
            return_value=(object(), {'enabled_services': ['id', 'drive']}),
        ):
            response = self._middleware()(request)

        self.assertEqual(response.status_code, 403)
        payload = json.loads(response.content)
        self.assertEqual(payload['code'], 'service_access_denied')
        self.assertEqual(payload['service'], 'hive')


    def test_drive_only_token_is_denied_at_public_hive_prefix(self):
        request = self.factory.get('/hive/api/v1/me', HTTP_AUTHORIZATION='Bearer token')
        with patch(
            'claws.middleware.FederatedJWTAuthentication.validate_token',
            return_value=(object(), {'enabled_services': ['id', 'drive']}),
        ):
            response = self._middleware()(request)

        self.assertEqual(response.status_code, 403)
        payload = json.loads(response.content)
        self.assertEqual(payload['code'], 'service_access_denied')
        self.assertEqual(payload['service'], 'hive')

    def test_drive_only_cookie_token_is_denied_at_hive_boundary(self):
        request = self.factory.get('/api/v1/me')
        request.COOKIES['shizuha-access-token'] = 'cookie-token'
        with patch(
            'claws.middleware.FederatedJWTAuthentication.validate_token',
            return_value=(object(), {'enabled_services': ['id', 'drive']}),
        ) as validate_token:
            response = self._middleware()(request)

        self.assertEqual(response.status_code, 403)
        validate_token.assert_called_once_with('cookie-token')

    def test_hive_enabled_token_is_allowed(self):
        request = self.factory.get('/api/v1/me', HTTP_AUTHORIZATION='Bearer token')
        with patch(
            'claws.middleware.FederatedJWTAuthentication.validate_token',
            return_value=(object(), {'enabled_services': ['id', 'hive']}),
        ):
            response = self._middleware()(request)

        self.assertEqual(response.status_code, 200)

    def test_legacy_token_without_enabled_services_is_allowed(self):
        request = self.factory.get('/api/v1/me', HTTP_AUTHORIZATION='Bearer token')
        with patch(
            'claws.middleware.FederatedJWTAuthentication.validate_token',
            return_value=(object(), {}),
        ):
            response = self._middleware()(request)

        self.assertEqual(response.status_code, 200)

    def test_invalid_token_is_left_for_drf_authentication(self):
        request = self.factory.get('/api/v1/me', HTTP_AUTHORIZATION='Bearer bad')
        with patch(
            'claws.middleware.FederatedJWTAuthentication.validate_token',
            side_effect=AuthenticationFailed('bad token'),
        ):
            response = self._middleware()(request)

        self.assertEqual(response.status_code, 200)

    def test_federated_user_preserves_legacy_none_enabled_services(self):
        user = FederatedUser(user_id=1, email='u@example.com', username='u')
        self.assertIsNone(user.enabled_services)
