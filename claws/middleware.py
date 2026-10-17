"""Service-boundary authorization for federated Shizuha ID JWTs."""

from django.conf import settings
from django.http import JsonResponse
from django.middleware.gzip import GZipMiddleware

from .authentication import FederatedJWTAuthentication


def _claim_values(raw):
    if not raw:
        return []
    if isinstance(raw, str):
        return raw.split()
    return list(raw)


def _is_coordinator_broker_claims(claims):
    from hive_platform.coordinator import is_coordinator_broker_claims
    return is_coordinator_broker_claims(claims)


# HIVE-576: hive mounts its service-to-service routes under `.../v1/internal/…`
# (see claws/urls.py); the `/internal/` marker is hive's own convention for an
# internal path (ServiceAccessMiddleware below already bypasses service-auth on
# it). Match that same marker so every internal route is covered.
_INTERNAL_MARKER = '/internal/'


def request_via_ingress(request) -> bool:
    """True when the request was proxied by the public nginx ingress.

    nginx-ingress always appends X-Forwarded-For / X-Real-IP to proxied requests
    and an external client cannot make it omit them; direct in-cluster Service
    calls (the only legitimate callers of the internal service-to-service API,
    e.g. Connect via ``shizuha-hive.shizuha.svc.cluster.local``) carry neither.
    """
    meta = request.META
    return bool(meta.get('HTTP_X_FORWARDED_FOR') or meta.get('HTTP_X_REAL_IP'))


class InternalApiIngressGuardMiddleware:
    """404 any ingress-borne request to the internal service-to-service API (HIVE-576).

    Hive's ``/v1/internal/…`` endpoints (Connect DM delivery, delivery/usage
    metrics, claw control-plane, WA bind, …) run with ``authentication_classes=[]``
    / ``permission_classes=[]`` and are network-gated by design, yet the public
    ``/hive/api/`` Prefix ingress also exposes them at ``/hive/api/v1/internal/…``.
    This mirrors shizuha-id's ``internal_api_guard.py`` / Connect's CON-208 guard
    (the correct ID-52-class mitigation): block internal paths that arrived through
    the ingress. An in-cluster caller that spoofs XFF simply locks itself out —
    fail closed. In-cluster direct calls (no XFF/X-Real-IP) are unaffected.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if _INTERNAL_MARKER in request.path and request_via_ingress(request):
            # 404 (not 403) — don't advertise that the path exists publicly.
            return JsonResponse({'error': 'Not found'}, status=404)
        return self.get_response(request)


class AgentCardGZipMiddleware(GZipMiddleware):
    """Compress only the non-secret, polling projection used by Agents cards.

    The full agent API intentionally remains outside this middleware because it
    contains edit-drawer configuration.  Django's GZipMiddleware also applies
    HTB random padding to reduce compression side-channel risk.
    """

    _PATH_SUFFIXES = (
        '/v1/fleet/agent-cards',
        '/v1/fleet/agent-cards/',
    )

    def process_response(self, request, response):
        if not request.path.endswith(self._PATH_SUFFIXES):
            return response
        return super().process_response(request, response)


class ServiceAccessMiddleware:
    SERVICE_SLUG = 'hive'
    API_PREFIXES = ('/api/', '/hive/api/')

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        ordinary_api = request.path.startswith(self.API_PREFIXES) and '/internal/' not in request.path
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        token = ''
        if auth_header.startswith('Bearer '):
            token = auth_header[7:]
        else:
            cookie_name = getattr(request, 'COOKIES', {}).get('shizuha-access-token')
            token = cookie_name or getattr(request, 'COOKIES', {}).get('access_token', '')

        if not token:
            return self.get_response(request)

        try:
            _, claims = FederatedJWTAuthentication.validate_token(token)
        except Exception:
            # Authentication still owns invalid-token handling; this middleware
            # only enforces service access after canonical validation succeeds.
            return self.get_response(request)

        # The revocable workload client has exactly three broker POST operations;
        # it is not an owner-admin session, including on internal/non-API paths.
        from hive_platform.coordinator import COORDINATOR_SERVICE_CLIENT_ID, is_coordinator_service_claims
        if claims.get('client_id') == COORDINATOR_SERVICE_CLIENT_ID:
            from django.urls import resolve, Resolver404
            try:
                route = resolve(request.path_info.rstrip('/') + '/').url_name
            except Resolver404:
                route = None
            if (not is_coordinator_service_claims(claims) or request.method != 'POST' or
                    route not in {'coordinator-model-token', 'coordinator-report-status',
                                  'fleet-agent-mcp-projection'}):
                return JsonResponse({'error': 'Not found'}, status=404)
            return self.get_response(request)
        if not ordinary_api:
            return self.get_response(request)

        if _is_coordinator_broker_claims(claims):
            return self.get_response(request)
        if claims.get('is_staff') or claims.get('is_superuser'):
            return self.get_response(request)
        enabled_services = claims.get('enabled_services')
        if enabled_services is not None and self.SERVICE_SLUG not in enabled_services:
            return JsonResponse(
                {
                    'error': 'You do not have access to this service.',
                    'service': self.SERVICE_SLUG,
                    'code': 'service_access_denied',
                },
                status=403,
            )

        return self.get_response(request)
