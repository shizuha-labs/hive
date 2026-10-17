"""HIVE-1889 (S3) — org Settings > Providers: org-scoped Cortex provider refs.

Builds against the HIVE-1862 contract (HLD ec7be32b):
  - ``HiveOrgProviderReference`` is a thin READ-ONLY mirror of the LIVE Cortex
    provider contract. No raw key material is stored or rendered in Hive; all
    mutation (validate/activate/rotate) is delegated to Cortex.
  - Resolution chain agent→org→Cortex key mirrors the coordinator TokenPool
    flow; fallback chains stay per-agent.
  - Per-org scoping: every read is owner-filtered; the caller's org is
    server-derived from the JWT membership (never client-claimed); org-owner-only
    visibility.

Endpoints (all under /hive/api/v1/):
  GET    /v1/org-provider-refs?org_id=…            list org-scoped refs
  POST   /v1/org-provider-refs                     upsert a ref (mirror write)
  DELETE /v1/org-provider-refs/<id>                remove a ref (mirror only)
  POST   /v1/org-provider-refs/<id>/validate       live 1-token probe → Cortex

HIVE-2161 lifecycle (the write path — orchestrates the LIVE Cortex listing
lifecycle; the secret key is relayed in-memory only and never stored/logged):
  POST   /v1/org-provider-refs/provision           add a BYO credential
  POST   /v1/org-provider-refs/<id>/rotate         replace the provider key
  POST   /v1/org-provider-refs/<id>/disable        org-scoped disable
"""
from __future__ import annotations

import json
import logging

import requests
from django.conf import settings
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import HiveOrgProviderReference

logger = logging.getLogger(__name__)


def _cortex_base():
    base = getattr(settings, 'CORTEX_BASE_URL', '') or getattr(settings, 'VLLM_BASE_URL', '')
    return (base or '').rstrip('/')


def _cortex_headers(request):
    headers = {'Accept': 'application/json'}
    auth = request.META.get('HTTP_AUTHORIZATION', '')
    if auth:
        headers['Authorization'] = auth
    return headers


def _caller_org_ids(request):
    """Server-derived org ids the caller is a verified member of.

    Authoritative membership comes from the signed JWT
    (organization_memberships = {org_id: role}); staff may read all. This is
    the whole security boundary — a bare org_id in the body/query is never
    trusted on its own.
    """
    if bool(getattr(request.user, 'is_staff', False)):
        return None  # staff: unrestricted (org-owner gate still applies below)
    memberships = getattr(request.user, 'organization_memberships', None) or {}
    ids = set()
    for key in memberships.keys():
        try:
            ids.add(int(key))
        except (TypeError, ValueError):
            continue
    return ids


def _org_owner_role(request, org_id):
    """Return the caller's role in ``org_id`` ('' if not a member)."""
    memberships = getattr(request.user, 'organization_memberships', None) or {}
    return str(memberships.get(str(org_id)) or '').strip().lower()


def _resolve_org_id(request, data=None):
    """Resolve the target org_id from query/body, enforcing membership.

    Returns (org_id, None) on success or (None, Response) on denial.
    """
    source = data if isinstance(data, dict) else {}
    query = getattr(request, 'query_params', None) or getattr(request, 'GET', {})
    raw = (
        query.get('org_id')
        or source.get('org_id')
        or query.get('organization_id')
        or source.get('organization_id')
        or ''
    )
    try:
        org_id = int(raw)
    except (TypeError, ValueError):
        return None, Response(
            {'error': 'org_id is required and must be an integer.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if bool(getattr(request.user, 'is_staff', False)):
        return org_id, None

    member_ids = _caller_org_ids(request)
    if org_id not in (member_ids or set()):
        return None, Response(
            {'error': 'Not a member of this organization.'},
            status=status.HTTP_403_FORBIDDEN,
        )
    return org_id, None


class OrgProviderReferenceListView(APIView):
    """GET  /v1/org-provider-refs?org_id=…  — list org-scoped refs (mirror).
    POST /v1/org-provider-refs              — upsert a ref (mirror write)."""

    def get(self, request):
        if not getattr(request.user, 'is_authenticated', False):
            return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
        org_id, error = _resolve_org_id(request)
        if error:
            return error
        qs = HiveOrgProviderReference.objects.filter(org_id=org_id).order_by('cortex_provider_id')
        return Response({'results': [_serialize(r) for r in qs]})

    def post(self, request):
        if not getattr(request.user, 'is_authenticated', False):
            return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
        org_id, error = _resolve_org_id(request, data=request.data)
        if error:
            return error
        role = _org_owner_role(request, org_id)
        if not bool(getattr(request.user, 'is_staff', False)) and role not in {'owner', 'admin'}:
            return Response(
                {'error': 'Only org owners/admins may configure provider references.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        cortex_provider_id = request.data.get('cortex_provider_id')
        if not cortex_provider_id:
            return Response(
                {'error': 'cortex_provider_id is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            from uuid import UUID
            provider_uuid = UUID(str(cortex_provider_id))
        except (ValueError, TypeError):
            return Response(
                {'error': 'cortex_provider_id must be a valid UUID.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        defaults = {
            'cortex_key_ref': str(request.data.get('cortex_key_ref') or '').strip(),
            'default_models': request.data.get('default_models') or [],
            'status': request.data.get('status') or HiveOrgProviderReference.Status.ACTIVE,
            'per_agent_overrides': request.data.get('per_agent_overrides') or {},
        }
        if defaults['status'] not in HiveOrgProviderReference.Status.values:
            return Response(
                {'error': f"Invalid status: {defaults['status']}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # HIVE-2161 invariant — an override may only reference same-org
        # credentials; cross-org references are rejected at write time (the
        # resolver re-checks per the HLD).
        overrides = request.data.get('per_agent_overrides') or {}
        override_error = _validate_overrides_same_org(org_id, overrides)
        if override_error:
            return override_error
        defaults['per_agent_overrides'] = overrides
        principal = _request_principal_name(request)
        defaults['updated_by_principal'] = principal
        ref, created = HiveOrgProviderReference.objects.update_or_create(
            org_id=org_id,
            cortex_provider_id=provider_uuid,
            defaults=defaults,
        )
        if created:
            ref.created_by_principal = principal
            ref.save(update_fields=['created_by_principal'])
        payload = _serialize(ref)
        # HIVE-1889 acceptance: "Save runs a live 1-token validation probe
        # against Cortex". When the caller supplies a provider api_key on save
        # (the one-time key from the form), delegate the probe to Cortex. Hive
        # never retains the key — it is sent only to Cortex's validate endpoint
        # and dropped. The mirror status is refreshed from the listing.
        api_key = request.data.get('api_key')
        if api_key:
            probe = _run_validate_probe(request, ref, api_key)
            if probe is not None:
                if probe.get('error'):
                    payload['validation'] = probe
                    return Response(payload, status=status.HTTP_502_BAD_GATEWAY)
                payload['validation'] = {'validated': True}
                payload['status'] = ref.status
        return Response(
            payload,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class OrgProviderReferenceDetailView(APIView):
    """DELETE /v1/org-provider-refs/<id> — remove a mirror ref (org-owner)."""

    def _get_ref(self, request, ref_id):
        try:
            return HiveOrgProviderReference.objects.get(id=ref_id)
        except HiveOrgProviderReference.DoesNotExist:
            return None

    def delete(self, request, ref_id):
        if not getattr(request.user, 'is_authenticated', False):
            return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
        ref = self._get_ref(request, ref_id)
        if ref is None:
            return Response({'error': 'Provider reference not found.'}, status=status.HTTP_404_NOT_FOUND)
        if not bool(getattr(request.user, 'is_staff', False)):
            member_ids = _caller_org_ids(request) or set()
            if ref.org_id not in member_ids:
                return Response(
                    {'error': 'Not a member of this organization.'},
                    status=status.HTTP_403_FORBIDDEN,
                )
            role = _org_owner_role(request, ref.org_id)
            if role not in {'owner', 'admin'}:
                return Response(
                    {'error': 'Only org owners/admins may remove provider references.'},
                    status=status.HTTP_403_FORBIDDEN,
                )
        ref.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


def _run_validate_probe(request, ref, api_key=''):
    """Delegate the live 1-token probe to Cortex.

    Returns a dict: ``{'ok': True}`` on success (mirror status refreshed), or
    ``{'error': ..., 'detail': ...}`` on failure. Returns None when Cortex is
    not configured (caller decides how to surface).
    """
    base = _cortex_base()
    if not base:
        return {'error': 'Cortex inference endpoint is not configured.'}
    url = f'{base}/v1/provider-listings/{ref.cortex_provider_id}/validate'
    body = {}
    if api_key:
        body['api_key'] = api_key
    try:
        resp = requests.post(
            url, headers=_cortex_headers(request), json=body, timeout=20,
        )
    except requests.RequestException as exc:
        logger.warning('cortex provider validate %s failed: %s', url, exc)
        return {'error': 'Could not reach the Cortex provider validation service.'}
    try:
        payload = resp.json()
    except ValueError:
        payload = {'detail': resp.text[:500]}
    if resp.status_code >= 400:
        return {'error': 'Cortex provider validation failed.', 'detail': payload}
    # Refresh the mirror status from the live listing (source of truth).
    new_status = _listing_status(payload)
    if new_status:
        ref.status = new_status
        ref.save(update_fields=['status', 'updated_at'])
    return {'ok': True, 'listing': payload}


class OrgProviderReferenceValidateView(APIView):
    """POST /v1/org-provider-refs/<id>/validate — live 1-token probe → Cortex.

    Delegates to Cortex ``POST /v1/provider-listings/<uuid>/validate`` (the
    exact endpoint named in HIVE-1862 §4). The provider key is sent to Cortex
    only; Hive never retains it. On success the mirror's status is refreshed
    from the returned listing.
    """

    def post(self, request, ref_id):
        if not getattr(request.user, 'is_authenticated', False):
            return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
        try:
            ref = HiveOrgProviderReference.objects.get(id=ref_id)
        except HiveOrgProviderReference.DoesNotExist:
            return Response({'error': 'Provider reference not found.'}, status=status.HTTP_404_NOT_FOUND)
        if not bool(getattr(request.user, 'is_staff', False)):
            member_ids = _caller_org_ids(request) or set()
            if ref.org_id not in member_ids:
                return Response(
                    {'error': 'Not a member of this organization.'},
                    status=status.HTTP_403_FORBIDDEN,
                )
            role = _org_owner_role(request, ref.org_id)
            if role not in {'owner', 'admin'}:
                return Response(
                    {'error': 'Only org owners/admins may validate provider references.'},
                    status=status.HTTP_403_FORBIDDEN,
                )

        probe = _run_validate_probe(request, ref, request.data.get('api_key') or '')
        if probe is None:
            return Response(
                {'error': 'Cortex inference endpoint is not configured.'},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        if probe.get('error'):
            return Response(
                {'error': probe['error'], 'detail': probe.get('detail')},
                status=status.HTTP_502_BAD_GATEWAY if probe.get('error') == 'Could not reach the Cortex provider validation service.' else status.HTTP_400_BAD_REQUEST,
            )
        return Response({'validated': True, 'listing': probe.get('listing')})


def _listing_status(payload):
    """Map a Cortex listing payload to a mirror status, or None if unknown."""
    if not isinstance(payload, dict):
        return None
    raw = str(payload.get('status') or payload.get('validation_state') or '').strip().lower()
    mapping = {
        'active': HiveOrgProviderReference.Status.ACTIVE,
        'verifying': HiveOrgProviderReference.Status.VERIFYING,
        'probation': HiveOrgProviderReference.Status.PROBATION,
        'disabled': HiveOrgProviderReference.Status.DISABLED,
    }
    return mapping.get(raw)


def _serialize(ref):
    return {
        'id': ref.id,
        'org_id': ref.org_id,
        'cortex_provider_id': str(ref.cortex_provider_id),
        'cortex_key_ref': ref.cortex_key_ref,
        'default_models': ref.default_models or [],
        'status': ref.status,
        'per_agent_overrides': ref.per_agent_overrides or {},
        'provider_type': ref.provider_type,
        'display_name': ref.display_name,
        'last_validation_result': ref.last_validation_result,
        'created_by_principal': ref.created_by_principal,
        'updated_by_principal': ref.updated_by_principal,
        'created_at': ref.created_at.isoformat() if ref.created_at else None,
        'updated_at': ref.updated_at.isoformat() if ref.updated_at else None,
    }


def _request_principal_name(request):
    """Caller principal for the immutable audit fields (HIVE-2161)."""
    user = getattr(request, 'user', None)
    return str(getattr(user, 'email', '') or getattr(user, 'username', '') or '')


def _validate_overrides_same_org(org_id, overrides):
    """HIVE-2161 invariant — overrides reference same-org credentials only.

    The per_agent_overrides JSON lives on an org-scoped row; any entry naming
    a provider_credential_id must resolve to a HiveOrgProviderReference row
    with the SAME org_id. Cross-org references are rejected at write time
    (HTTP 403); unknown references at 400. The resolver re-checks per the HLD.
    """
    if not isinstance(overrides, dict):
        return Response(
            {'error': 'per_agent_overrides must be an object keyed by agent id.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    for agent_id, entry in overrides.items():
        if not isinstance(entry, dict):
            continue
        ref_id = entry.get('provider_credential_id')
        if not ref_id:
            continue
        try:
            target = HiveOrgProviderReference.objects.get(cortex_provider_id=ref_id)
        except (HiveOrgProviderReference.DoesNotExist, ValueError, TypeError):
            return Response(
                {'error': f'Override for {agent_id} references unknown '
                          f'provider_credential_id {ref_id}.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if target.org_id != org_id:
            return Response(
                {'error': f'Override for {agent_id} references a credential from '
                          f'another organization — cross-org references are rejected.'},
                status=status.HTTP_403_FORBIDDEN,
            )
    return None


def _cortex_post(url, headers, body, timeout=20):
    """POST to a Cortex endpoint with the shared error handling.

    Returns (payload, None) on a 2xx, or (None, {'error', 'detail', 'status'})
    on any failure. Never logs or returns request bodies (they may carry the
    one-time provider key).
    """
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=timeout)
    except requests.RequestException as exc:
        logger.warning('cortex POST %s failed: %s', url, type(exc).__name__)
        return None, {'error': 'Could not reach the Cortex provider service.',
                      'detail': None, 'status': 502}
    try:
        payload = resp.json()
    except ValueError:
        payload = {'detail': resp.text[:500]}
    if resp.status_code >= 400:
        return None, {'error': 'Cortex provider request failed.',
                      'detail': payload, 'status': resp.status_code}
    return payload, None


def _outcome_code(status_code, detail):
    """Map a Cortex failure to a redacted HLD outcome code (never a raw body)."""
    if status_code in (401, 403):
        return 'auth_failed'
    if status_code == 429:
        return 'rate_limited'
    if isinstance(detail, dict) and 'schema' in json.dumps(detail)[:200].lower():
        return 'schema_mismatch'
    return 'endpoint_unreachable'


# Review must-fix (kumo, Origin review 4399 on hive#278): canonical provider
# types have well-known endpoints — the display name must NEVER be passed as
# the Cortex endpoint_url (Cortex's _provider_listing_endpoint_error would
# reject it or materialize a bogus endpoint).
CANONICAL_PROVIDER_ENDPOINTS = {
    HiveOrgProviderReference.ProviderType.OPENAI: 'https://api.openai.com/v1',
    HiveOrgProviderReference.ProviderType.ANTHROPIC: 'https://api.anthropic.com/v1',
}


def _redact_secret(obj, secret):
    """Recursively scrub the relayed secret from a failure detail.

    Review advisory (kumo): self-contained redaction — some providers echo
    the request body in error payloads, so the detail must be scrubbed before
    it reaches any response envelope.
    """
    if not secret:
        return obj
    if isinstance(obj, str):
        return obj.replace(secret, '[redacted]')
    if isinstance(obj, dict):
        return {k: _redact_secret(v, secret) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_secret(v, secret) for v in obj]
    return obj


class OrgProviderReferenceProvisionView(APIView):
    """POST /v1/org-provider-refs/provision — add a BYO provider credential.

    HIVE-2161 write path: orchestrates the LIVE Cortex listing lifecycle
    (HIVE-1862 §4 endpoints, verified against cortex master c9f56d30):
      1. POST /v1/provider-listings              — draft listing
      2. POST /v1/provider-listings/<id>/submit  — api_key relayed in-memory
      3. POST /v1/provider-listings/<id>/validate — 1-token probe
    then persists the thin mirror row. The secret key NEVER touches Hive
    storage or logs — it exists only in the relayed request bodies and is
    dropped after the lifecycle completes.
    """

    def post(self, request):
        if not getattr(request.user, 'is_authenticated', False):
            return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
        org_id, error = _resolve_org_id(request, data=request.data)
        if error:
            return error
        role = _org_owner_role(request, org_id)
        if not bool(getattr(request.user, 'is_staff', False)) and role not in {'owner', 'admin'}:
            return Response(
                {'error': 'Only org owners/admins may add provider credentials.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        data = request.data if isinstance(request.data, dict) else {}
        provider_type = data.get('provider_type') or HiveOrgProviderReference.ProviderType.OPENAI_COMPATIBLE
        if provider_type not in HiveOrgProviderReference.ProviderType.values:
            return Response({'error': f'Invalid provider_type: {provider_type}'},
                            status=status.HTTP_400_BAD_REQUEST)
        display_name = str(data.get('display_name') or '').strip()
        if not display_name:
            return Response({'error': 'display_name is required.'},
                            status=status.HTTP_400_BAD_REQUEST)
        base_url = str(data.get('base_url') or '').strip()
        if provider_type == HiveOrgProviderReference.ProviderType.OPENAI_COMPATIBLE and not base_url:
            return Response({'error': 'base_url is required for openai_compatible providers.'},
                            status=status.HTTP_400_BAD_REQUEST)
        models = data.get('models') or []
        if not isinstance(models, list) or not models:
            return Response({'error': 'At least one model entry is required.'},
                            status=status.HTTP_400_BAD_REQUEST)
        secret_key = str(data.get('secret_key') or '').strip()
        if not secret_key:
            return Response({'error': 'secret_key is required.'},
                            status=status.HTTP_400_BAD_REQUEST)

        base = _cortex_base()
        if not base:
            return Response({'error': 'Cortex inference endpoint is not configured.'},
                            status=status.HTTP_502_BAD_GATEWAY)
        headers = _cortex_headers(request)

        # 1. Draft listing on Cortex (source of truth for the credential).
        endpoint_url = base_url or CANONICAL_PROVIDER_ENDPOINTS.get(provider_type, '')
        if not endpoint_url:
            return Response({'error': 'endpoint_url could not be determined.'},
                            status=status.HTTP_400_BAD_REQUEST)
        draft_body = {'display_name': display_name, 'endpoint_url': endpoint_url,
                      'models': models}
        listing, failure = _cortex_post(f'{base}/v1/provider-listings', headers, draft_body)
        if failure:
            return Response({'error': failure['error'], 'detail': _redact_secret(failure['detail'], secret_key)},
                            status=status.HTTP_502_BAD_GATEWAY)
        listing_id = listing.get('id')
        if not listing_id:
            return Response({'error': 'Cortex did not return a listing id.',
                             'detail': listing}, status=status.HTTP_502_BAD_GATEWAY)

        # 2. Submit the one-time key (relayed; Hive never retains it).
        submitted, failure = _cortex_post(
            f'{base}/v1/provider-listings/{listing_id}/submit', headers,
            {'api_key': secret_key})
        if failure:
            # The draft exists on Cortex but the credential failed to
            # materialize — record the failure state honestly (org-scoped).
            ref = HiveOrgProviderReference.objects.create(
                org_id=org_id, cortex_provider_id=listing_id,
                provider_type=provider_type, display_name=display_name,
                status=HiveOrgProviderReference.Status.INVALID,
                last_validation_result=_outcome_code(failure['status'], failure['detail']),
                created_by_principal=_request_principal_name(request),
                updated_by_principal=_request_principal_name(request),
            )
            return Response({'ref': _serialize(ref), 'error': failure['error'],
                             'detail': _redact_secret(failure['detail'], secret_key)},
                            status=status.HTTP_502_BAD_GATEWAY)
        provider_id = str((submitted.get('validation_state') or {}).get('provider_id') or '')

        # 3. Live 1-token probe.
        validated, failure = _cortex_post(
            f'{base}/v1/provider-listings/{listing_id}/validate', headers,
            {'api_key': secret_key})
        if failure:
            outcome = _outcome_code(failure['status'], failure['detail'])
            ref = HiveOrgProviderReference.objects.create(
                org_id=org_id, cortex_provider_id=listing_id,
                cortex_key_ref=provider_id, provider_type=provider_type,
                display_name=display_name,
                status=HiveOrgProviderReference.Status.VERIFYING,
                last_validation_result=outcome,
                created_by_principal=_request_principal_name(request),
                updated_by_principal=_request_principal_name(request),
            )
            return Response({'ref': _serialize(ref), 'error': failure['error'],
                             'detail': _redact_secret(failure['detail'], secret_key)},
                            status=status.HTTP_502_BAD_GATEWAY)

        mirror_status = _listing_status(validated) or HiveOrgProviderReference.Status.VERIFYING
        ref = HiveOrgProviderReference.objects.create(
            org_id=org_id, cortex_provider_id=listing_id,
            cortex_key_ref=provider_id, provider_type=provider_type,
            display_name=display_name, status=mirror_status,
            last_validation_result='ok',
            created_by_principal=_request_principal_name(request),
            updated_by_principal=_request_principal_name(request),
        )
        return Response({'ref': _serialize(ref), 'validation': {'validated': True}},
                        status=status.HTTP_201_CREATED)


class OrgProviderReferenceRotateView(APIView):
    """POST /v1/org-provider-refs/<id>/rotate — replace the provider key.

    Re-runs the Cortex submit (new api_key, relayed in-memory only) + the
    1-token probe, then refreshes the mirror. Rotation is the HLD's
    credential-hygiene contract; the key never touches Hive storage or logs.
    """

    def post(self, request, ref_id):
        if not getattr(request.user, 'is_authenticated', False):
            return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
        try:
            ref = HiveOrgProviderReference.objects.get(id=ref_id)
        except HiveOrgProviderReference.DoesNotExist:
            return Response({'error': 'Provider reference not found.'}, status=status.HTTP_404_NOT_FOUND)
        if not bool(getattr(request.user, 'is_staff', False)):
            member_ids = _caller_org_ids(request) or set()
            if ref.org_id not in member_ids:
                return Response({'error': 'Not a member of this organization.'},
                                status=status.HTTP_403_FORBIDDEN)
            role = _org_owner_role(request, ref.org_id)
            if role not in {'owner', 'admin'}:
                return Response({'error': 'Only org owners/admins may rotate provider credentials.'},
                                status=status.HTTP_403_FORBIDDEN)
        secret_key = str((request.data or {}).get('secret_key') or '').strip()
        if not secret_key:
            return Response({'error': 'secret_key is required.'}, status=status.HTTP_400_BAD_REQUEST)

        base = _cortex_base()
        if not base:
            return Response({'error': 'Cortex inference endpoint is not configured.'},
                            status=status.HTTP_502_BAD_GATEWAY)
        headers = _cortex_headers(request)
        submitted, failure = _cortex_post(
            f'{base}/v1/provider-listings/{ref.cortex_provider_id}/submit', headers,
            {'api_key': secret_key})
        if failure:
            ref.last_validation_result = _outcome_code(failure['status'], failure['detail'])
            # Review advisory: a failed rotation must not silently re-classify
            # a disabled credential — the outcome code carries the failure.
            if ref.status != HiveOrgProviderReference.Status.DISABLED:
                ref.status = HiveOrgProviderReference.Status.INVALID
            ref.updated_by_principal = _request_principal_name(request)
            ref.save(update_fields=['last_validation_result', 'status',
                                    'updated_by_principal', 'updated_at'])
            return Response({'ref': _serialize(ref), 'error': failure['error'],
                             'detail': _redact_secret(failure['detail'], secret_key)},
                            status=status.HTTP_502_BAD_GATEWAY)
        validated, failure = _cortex_post(
            f'{base}/v1/provider-listings/{ref.cortex_provider_id}/validate', headers,
            {'api_key': secret_key})
        if failure:
            ref.last_validation_result = _outcome_code(failure['status'], failure['detail'])
            ref.updated_by_principal = _request_principal_name(request)
            ref.save(update_fields=['last_validation_result', 'updated_by_principal', 'updated_at'])
            return Response({'ref': _serialize(ref), 'error': failure['error'],
                             'detail': _redact_secret(failure['detail'], secret_key)},
                            status=status.HTTP_502_BAD_GATEWAY)
        # Review advisory: rotation replaces the key but must not silently
        # re-activate a disabled credential — the admin re-enables explicitly.
        if ref.status != HiveOrgProviderReference.Status.DISABLED:
            ref.status = _listing_status(validated) or HiveOrgProviderReference.Status.VERIFYING
        ref.last_validation_result = 'ok'
        ref.updated_by_principal = _request_principal_name(request)
        ref.save(update_fields=['status', 'last_validation_result',
                                'updated_by_principal', 'updated_at'])
        return Response({'ref': _serialize(ref), 'rotated': True})


class OrgProviderReferenceDisableView(APIView):
    """POST /v1/org-provider-refs/<id>/disable — org-scoped disable.

    Sets the mirror status to disabled: the resolver's lookup filters
    non-enabled refs, so nothing routes against the credential (a stale
    mirror never routes). Cortex-side credential revocation has no verified
    surface in HIVE-1862 §4 — the credential stays but is unrouted; revocation
    is a Cortex contract follow-up if needed.
    """

    def post(self, request, ref_id):
        if not getattr(request.user, 'is_authenticated', False):
            return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
        try:
            ref = HiveOrgProviderReference.objects.get(id=ref_id)
        except HiveOrgProviderReference.DoesNotExist:
            return Response({'error': 'Provider reference not found.'}, status=status.HTTP_404_NOT_FOUND)
        if not bool(getattr(request.user, 'is_staff', False)):
            member_ids = _caller_org_ids(request) or set()
            if ref.org_id not in member_ids:
                return Response({'error': 'Not a member of this organization.'},
                                status=status.HTTP_403_FORBIDDEN)
            role = _org_owner_role(request, ref.org_id)
            if role not in {'owner', 'admin'}:
                return Response({'error': 'Only org owners/admins may disable provider credentials.'},
                                status=status.HTTP_403_FORBIDDEN)
        ref.status = HiveOrgProviderReference.Status.DISABLED
        ref.updated_by_principal = _request_principal_name(request)
        ref.save(update_fields=['status', 'updated_by_principal', 'updated_at'])
        return Response({'ref': _serialize(ref), 'disabled': True})


class OrgProviderLeaseView(APIView):
    """POST /v1/org-provider-lease — request-time org-scoped credential lease.

    HIVE-2622 S4 deliverable 2 (runtime consumption wiring): the BYO runtime
    (claude-bridge / codex-bridge / shizuha-cli) resolves the
    agent→org→provider chain at request time by presenting its verified
    broker/workload credential here and receiving an
    :class:`~fleet.org_lease.OrgLeaseGrant` — a Hive-issued, TTL-bound
    consumer token whose claims pin the same-org Cortex reference. The org's
    real key material never leaves Cortex; the runtime calls the pinned
    router surface with the consumer token and Cortex's router enforces the
    BYO tenant-lock server-side (HLD Router Core v18 §5).

    Security boundary (mirrors the coordinator broker flow):
    - the caller must be a verified broker/workload token (Coordinator
      audience + broker role) — a user/admin JWT is refused with 404 so the
      endpoint is non-enumerable to ordinary callers;
    - the org is SERVER-derived from the minted token owner scope
      (``coordinator.resolve_owner``) — never read from the request body;
    - the synthetic fleet-org scope is not a customer org and is refused;
    - failures are org-scoped typed surfaces (the S3-lite reason enum) —
      never a fleet-wide paint, never a platform-pool fallback for a BYO org.

    Loud gate: refuses with ``lease_wiring_pending`` until
    ``provider_resolution.LEASE_WIRING_COMPLETE`` flips (deliverable 3, gated
    on end-to-end BYO comms verification) — the runtime wiring must never
    half-arm before the surface is verified.
    """

    def post(self, request):
        from claws import coordinator
        from .provider_resolution import LEASE_WIRING_COMPLETE, REASON_LEASE_WIRING_PENDING
        from .org_lease import OrgLeaseUnavailable, issue_org_lease

        if not coordinator.is_broker_request(request):
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        agent_identity = coordinator.broker_identity(request)
        if not agent_identity:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)

        if not LEASE_WIRING_COMPLETE:
            return Response(
                {
                    'ok': False,
                    'reason': REASON_LEASE_WIRING_PENDING,
                    'detail': 'org credential lease wiring is not armed yet '
                              '(deliverable 3 flip pending end-to-end verification)',
                },
                status=status.HTTP_409_CONFLICT,
            )

        owner = coordinator.resolve_owner(request)
        if (
            owner.owner_kind != 'org'
            or owner.owner_id == 'org'
            or not str(owner.owner_id or '').strip().isdigit()
        ):
            # Not a customer-org workload (user token or synthetic fleet scope):
            # the BYO chain does not apply — org-scoped refusal, non-enumerable.
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        organization_id = int(owner.owner_id)

        provider = str((request.data or {}).get('provider') or '').strip()
        runtime_class = str((request.data or {}).get('runtime_class') or '').strip()

        try:
            grant = issue_org_lease(
                organization_id=organization_id,
                provider=provider,
                runtime_class=runtime_class,
                agent_identity=agent_identity,
            )
        except OrgLeaseUnavailable as exc:
            # Org-scoped typed failure surface: the org's own runtime learns
            # WHY (redacted provenance only) and keeps its per-agent fallback
            # chain — never a platform-pool fallback for a BYO org.
            return Response(
                {'ok': False, 'reason': exc.reason, 'detail': exc.detail},
                status=status.HTTP_409_CONFLICT,
            )
        return Response({
            'ok': True,
            'grant_id': grant.grant_id,
            'lease_id': grant.lease_id,
            'organization_id': grant.organization_id,
            'reference_id': grant.reference_id,
            'cortex_provider_id': grant.cortex_provider_id,
            'provider': grant.provider,
            'model_id': grant.model_id,
            'router_base_url': grant.router_base_url,
            'consumer_token': grant.consumer_token,
            'expires_at': grant.expires_at.isoformat(),
        })
