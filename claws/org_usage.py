"""HIVE-314 S4 — per-org usage/consumption view proxied from the metering pipeline.

Reads org-scoped ``UsageDailyRollup`` rows produced by the HIVE-319 metering
pipeline (reused, NOT forked). Org-owner-only visibility: the caller's org
context is resolved from the authenticated user and validated against the
shizuha-admin authority, so a caller can only ever see usage for an org they
belong to — no cross-org leak. A bare ``X-Organization-ID`` header is never
trusted on its own; membership is always checked against admin.
"""
from __future__ import annotations

import logging
from datetime import timedelta

import requests
from django.conf import settings
from django.core.cache import cache
from django.db.models import Sum
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status

from hive_platform.models import UsageDailyRollup

logger = logging.getLogger(__name__)

_MAX_DAYS = 90
_DEFAULT_DAYS = 30


def _clamp_days(raw: str | None) -> int:
    try:
        value = int(raw or _DEFAULT_DAYS)
    except (TypeError, ValueError):
        value = _DEFAULT_DAYS
    return max(1, min(value, _MAX_DAYS))


def _admin_headers() -> dict[str, str]:
    headers = {'X-Internal-Service': 'shizuha-hive'}
    token = getattr(settings, 'ADMIN_INTERNAL_SERVICE_TOKEN', '')
    if token:
        headers['X-Internal-Service-Token'] = token
    return headers


def _caller_org_ids(owner_id: int) -> list[int]:
    """Resolve the set of orgs the caller belongs to (admin is the authority)."""
    cache_key = f'hive:s4:user-orgs:{owner_id}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    base = getattr(settings, 'HIVE_ADMIN_API_URL', '').rstrip('/')
    if not base:
        return []
    try:
        response = requests.get(
            f'{base}/internal/users/{int(owner_id)}/organizations/',
            headers=_admin_headers(),
            timeout=4,
        )
        if response.status_code == 200:
            org_ids = []
            for organization in (response.json() or {}).get('organizations', []):
                try:
                    org_ids.append(int(organization['id']))
                except (TypeError, ValueError, KeyError):
                    continue
            cache.set(cache_key, org_ids, 60)
            return org_ids
        logger.warning('HIVE-314 S4 org lookup failed owner=%s status=%s', owner_id, response.status_code)
    except (requests.RequestException, TypeError, ValueError) as exc:
        logger.warning('HIVE-314 S4 org lookup error owner=%s: %s', owner_id, exc)
    return []


def _resolve_request_org_id(request) -> int | None:
    """Resolve the caller's org context, validating membership against admin.

    Returns the org id the caller may view usage for, or ``None`` when no org
    context can be established (fail closed). A client-supplied
    ``X-Organization-ID`` is honored only if the caller is a member; otherwise
    the caller's personal org is used when unambiguous.
    """
    owner_id = getattr(request.user, 'id', None)
    if owner_id is None:
        return None
    org_ids = _caller_org_ids(owner_id)
    header = request.META.get('HTTP_X_ORGANIZATION_ID')
    if header:
        try:
            requested = int(header)
        except (TypeError, ValueError):
            return None
        if requested in org_ids:
            return requested
        # Caller is not a member of the requested org — fail closed (no leak).
        return None
    # No header: personal org only (single unambiguous personal org).
    personal = [oid for oid in org_ids if _is_personal_org(owner_id, oid)]
    if len(personal) == 1:
        return personal[0]
    return None


def _is_personal_org(owner_id: int, org_id: int) -> bool:
    cache_key = f'hive:s4:personal-org:{owner_id}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached == org_id
    base = getattr(settings, 'HIVE_ADMIN_API_URL', '').rstrip('/')
    try:
        response = requests.get(
            f'{base}/internal/users/{int(owner_id)}/organizations/',
            headers=_admin_headers(),
            timeout=4,
        )
        if response.status_code == 200:
            for organization in (response.json() or {}).get('organizations', []):
                if (organization.get('is_personal') is True
                        and str(organization.get('owner_id')) == str(int(owner_id))
                        and int(organization['id']) == org_id):
                    cache.set(cache_key, org_id, 60)
                    return True
    except (requests.RequestException, TypeError, ValueError, KeyError) as exc:
        logger.warning('HIVE-314 S4 personal-org check failed owner=%s: %s', owner_id, exc)
    return False


class OrgUsageView(APIView):
    """Per-org usage/consumption view (HIVE-314 S4).

    Returns the caller's org-scoped usage/consumption aggregated from the
    metering pipeline (``UsageDailyRollup``). Org-owner-only visibility: the
    org context is validated against admin membership, so there is no
    cross-org leak. Query params: ``days`` (1-90, default 30).
    """

    def get(self, request):
        org_id = _resolve_request_org_id(request)
        if org_id is None:
            return Response(
                {'error': 'organization context required or membership not verified'},
                status=status.HTTP_403_FORBIDDEN,
            )
        days = _clamp_days(request.query_params.get('days'))
        since = timezone.now().date() - timedelta(days=days - 1)
        rows = (
            UsageDailyRollup.objects
            .filter(tenant_org_id=str(org_id), day__gte=since)
            .values('day', 'metric', 'model', 'unit')
            .annotate(quantity=Sum('quantity'), event_count=Sum('event_count'))
            .order_by('day', 'metric', 'model')
        )
        days_map: dict[str, dict] = {}
        for row in rows:
            day = str(row['day'])
            entry = days_map.setdefault(day, {'day': day, 'metrics': {}})
            metric = row['metric']
            m = entry['metrics'].setdefault(metric, {'metric': metric, 'unit': row['unit'], 'total': 0, 'events': 0, 'models': {}})
            m['total'] += float(row['quantity'] or 0)
            m['events'] += int(row['event_count'] or 0)
            model = row['model'] or 'unattributed'
            m['models'][model] = m['models'].get(model, 0) + float(row['quantity'] or 0)
        summary = {
            'org_id': org_id,
            'days': days,
            'since': str(since),
            'usage': sorted(days_map.values(), key=lambda d: d['day']),
        }
        return Response(summary)
