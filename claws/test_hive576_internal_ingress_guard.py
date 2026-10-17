"""HIVE-576: hive's /v1/internal/ service-to-service API must be unreachable
through the public nginx ingress.

The internal views run with authentication_classes=[]/permission_classes=[] and
are network-gated by design, yet the public `/hive/api/` Prefix ingress also
exposes them at `/hive/api/v1/internal/…`. InternalApiIngressGuardMiddleware
404s any internal-path request that arrived through the ingress (detected by the
nginx-injected X-Forwarded-For / X-Real-IP headers), while direct in-cluster
Service calls (no such headers) still reach the view. Mirrors Connect's CON-208
guard / shizuha-id's internal_api_guard.py (same ID-52 class).
"""
from django.http import JsonResponse
from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory

from claws.middleware import InternalApiIngressGuardMiddleware


class InternalApiIngressGuardTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    def _call(self, path, **extra):
        sentinel = object()
        middleware = InternalApiIngressGuardMiddleware(lambda request: sentinel)
        request = self.factory.get(path, **extra)
        return middleware(request), sentinel

    def test_ingress_borne_internal_request_is_404(self):
        # X-Forwarded-For present == arrived through the public ingress.
        resp, sentinel = self._call(
            '/hive/api/v1/internal/connect/delivery/metrics',
            HTTP_X_FORWARDED_FOR='203.0.113.7')
        self.assertIsInstance(resp, JsonResponse)
        self.assertEqual(resp.status_code, 404)
        self.assertIsNot(resp, sentinel)

    def test_x_real_ip_also_blocks(self):
        resp, _ = self._call(
            '/hive/api/v1/internal/usage-metering/metrics',
            HTTP_X_REAL_IP='203.0.113.7')
        self.assertIsInstance(resp, JsonResponse)
        self.assertEqual(resp.status_code, 404)


    def test_codex_refresh_metrics_path_is_guarded(self):
        resp, _ = self._call(
            '/hive/api/v1/internal/codex-refresh/metrics',
            HTTP_X_FORWARDED_FOR='203.0.113.7')
        self.assertIsInstance(resp, JsonResponse)
        self.assertEqual(resp.status_code, 404)

    def test_in_cluster_internal_call_reaches_view(self):
        # No XFF / X-Real-IP == direct in-cluster Service call (e.g. Connect via
        # shizuha-hive.shizuha.svc): the guard must NOT block it.
        resp, sentinel = self._call('/api/v1/internal/connect/messages')
        self.assertIs(resp, sentinel)

    def test_non_internal_path_is_never_guarded(self):
        # A public path that arrived through the ingress must pass untouched.
        resp, sentinel = self._call(
            '/hive/api/v1/claws', HTTP_X_FORWARDED_FOR='203.0.113.7')
        self.assertIs(resp, sentinel)
