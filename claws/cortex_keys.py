"""
HIVE-76 — same-origin proxy for the caller's own Cortex inference API keys.

The Hive SPA must let a logged-in user mint/list/revoke their personal
``sk-cortex-`` key (for scli / external tools). Cortex's user-self key endpoint
(``/v1/api-keys``) lives on a different origin and Cortex ships no CORS config,
so a direct browser call would be blocked. Instead the SPA calls these
same-origin Hive routes, which forward to Cortex in-cluster with the caller's
own shizuha-id JWT — so Cortex acts as the user and only ever touches that
user's own keys. No key material is stored or logged here; the body is relayed
verbatim (the one-time raw key on create passes straight through).
"""
import logging

import requests
from django.conf import settings
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response

logger = logging.getLogger(__name__)

# Cortex root (no /v1 suffix). Reuses the in-cluster inference URL the rest of
# Hive already targets; override with CORTEX_BASE_URL if the key API ever splits
# off from the inference endpoint.
def _cortex_base():
    base = getattr(settings, 'CORTEX_BASE_URL', '') or getattr(settings, 'VLLM_BASE_URL', '')
    return (base or '').rstrip('/')


def _forward_headers(request):
    headers = {'Accept': 'application/json'}
    auth = request.META.get('HTTP_AUTHORIZATION', '')
    if auth:
        headers['Authorization'] = auth
    return headers


def _proxy(method, suffix, request, json_body=None):
    base = _cortex_base()
    if not base:
        return Response(
            {'error': 'Cortex inference endpoint is not configured.'},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    url = f'{base}/v1/api-keys{suffix}'
    try:
        resp = requests.request(
            method, url, headers=_forward_headers(request),
            json=json_body, timeout=15,
        )
    except requests.RequestException as exc:
        logger.warning('cortex api-keys proxy %s %s failed: %s', method, url, exc)
        return Response(
            {'error': 'Could not reach the Cortex key service.'},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    # Relay Cortex's JSON + status verbatim (incl. the one-time raw key on POST).
    if resp.status_code == 204 or not resp.content:
        return Response(status=resp.status_code)
    try:
        return Response(resp.json(), status=resp.status_code)
    except ValueError:
        return Response({'detail': resp.text[:500]}, status=resp.status_code)


class CortexApiKeysView(APIView):
    """
    GET  /hive/api/v1/cortex/api-keys  → cortex GET  /v1/api-keys  (list own keys)
    POST /hive/api/v1/cortex/api-keys  → cortex POST /v1/api-keys  (mint, {name})
    """

    def get(self, request):
        return _proxy('GET', '', request)

    def post(self, request):
        body = {'name': (request.data.get('name') or 'API Key')}
        return _proxy('POST', '', request, json_body=body)


class CortexApiKeyDetailView(APIView):
    """
    DELETE /hive/api/v1/cortex/api-keys/<id> → cortex DELETE /v1/api-keys/<id>
    (revoke one of the caller's own keys).
    """

    def delete(self, request, key_id):
        return _proxy('DELETE', f'/{key_id}', request)
