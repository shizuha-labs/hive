"""
Hive Claw API views.

Aligned to the accepted HLD "Hive UI" §5 contract (Sora co-signed, v4):

    GET    /v1/claws                  list the caller's remaining claws
    POST   /v1/claws                  retired (ADR-0080) — 410; use FleetAgent
    GET    /v1/claws/<id>             claw detail
    PATCH  /v1/claws/<id>             rename (display-name only)  {name}
    POST   /v1/claws/<id>/start       scale 0 -> 1
    POST   /v1/claws/<id>/stop        scale 1 -> 0
    DELETE /v1/claws/<id>?confirm=<name>   two-phase delete
    POST   /v1/claws/<id>/undo-delete      undo within the 60s window
    GET    /v1/claws/<id>/events      lifecycle events

Authorization is explicit per record (HLD §6.2): there is no list-all route;
every ``<id>`` route returns 404 unless the caller owns the claw (staff excepted).

Out of scope for this PR (separate HLD subtasks / follow-up PRs):
- credits/entitlement provisioning gate (Books integration, HLD §5.2, subtask #4)
- dashboard-url token minting + auth-state handshake (HLD §6.4, subtask #6)
- two-phase delete reclaim CronJob + SSE event stream (HLD §4.2, subtask #2)
"""
import hmac
import logging
import os
import threading
import time
import hashlib
import base64
import json
import uuid
import secrets
import requests
from datetime import timedelta, timezone as datetime_timezone
from types import SimpleNamespace
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, Prefetch, Q
from django.core.cache import cache
from django.middleware.csrf import get_token
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination
from rest_framework.throttling import UserRateThrottle
from django.http import HttpResponse, HttpResponseRedirect
from django.utils.html import escape

from .models import Claw, ClawEvent, ClawExportJob, ClawHardDeleteFailure, ClawCancelDelete, WhatsAppLink, OrgTemplate, TenantProvisioningRun, MarketplaceTemplate
from hive_platform.models import TokenEntry, TokenPool
from .serializers import (
    ClawSerializer, ClawConnectAgentSerializer, ClawCreateSerializer, ClawRenameSerializer,
    ClawEventSerializer, LegalHoldSerializer,
    ClawExportRequestSerializer, ClawExportJobSerializer,
    TokenPoolSerializer, TokenPoolCreateSerializer, TokenPoolUpdateSerializer,
    TokenEntrySerializer, TokenEntryCreateSerializer, TokenEntryUpdateSerializer,
    WhatsAppLinkSerializer, OrgTemplateSummarySerializer, OrgTemplateDetailSerializer, MarketplaceTemplateSerializer,
    TenantProvisioningRunCreateSerializer, TenantProvisioningRunSerializer,
)
from . import lifecycle, provisioner
from .usage_metering import record_agent_turn, record_cortex_token_usage
from .stepup import StepUpVerificationError, verify_step_up_token

logger = logging.getLogger(__name__)



def _visible_marketplace_templates(request):
    qs = MarketplaceTemplate.objects.filter(status=MarketplaceTemplate.Status.PUBLISHED)
    if getattr(request.user, 'is_staff', False):
        return qs
    entitlements = set(getattr(request.user, 'entitlements', []) or [])
    allowed = [MarketplaceTemplate.Visibility.PUBLIC]
    if 'hive:design-partner' in entitlements:
        allowed.append(MarketplaceTemplate.Visibility.DESIGN_PARTNER)
    return qs.filter(visibility__in=allowed)


class MarketplaceTemplateListView(APIView):
    def get(self, request):
        if not getattr(request.user, 'is_authenticated', False):
            return Response({'detail': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
        return Response(MarketplaceTemplateSerializer(_visible_marketplace_templates(request), many=True).data)


class MarketplaceTemplateDetailView(APIView):
    def get(self, request, template_slug):
        if not getattr(request.user, 'is_authenticated', False):
            return Response({'detail': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
        try:
            obj = _visible_marketplace_templates(request).get(slug=template_slug)
        except MarketplaceTemplate.DoesNotExist:
            return Response({'detail': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        return Response(MarketplaceTemplateSerializer(obj).data)


_FLEET_READ_JIT_SCOPE = 'hive.fleet.read' 

# HLD §4.2 — owner has 60s to undo a delete before teardown proceeds.
UNDO_DELETE_WINDOW = lifecycle.ORDINARY_DELETE_UNDO_WINDOW

# ADR-0080 / HIVE-2007: Claw is not a customer product. New rows are forbidden;
# existing rows stay readable/stoppable until drained.
CLAW_PROVISIONING_RETIRED = {
    'error': 'Claw provisioning is retired',
    'detail': (
        'Hive agents are FleetAgents. A personal assistant is an isolated '
        'owned FleetAgent, not a Claw. See ADR-0080 / HIVE-2007.'
    ),
}


def _claw_provisioning_retired_response():
    return Response(CLAW_PROVISIONING_RETIRED, status=status.HTTP_410_GONE)


def _get_owned_claw(request, claw_id):
    """
    Fetch a live claw the caller is allowed to see, else a Response to return.

    Non-owners get 404 (not 403) so claw existence is not leaked (HLD §6.2).
    Staff may access any claw.
    """
    try:
        claw = Claw.objects.exclude(status=Claw.Status.DELETED).get(id=claw_id)
    except (Claw.DoesNotExist, ValueError):
        return Response({'error': 'Claw not found'}, status=status.HTTP_404_NOT_FOUND)
    try:
        owner_id = int(request.user.id)
    except (TypeError, ValueError):
        owner_id = request.user.id
    if not getattr(request.user, 'is_staff', False) and claw.owner_id != owner_id:
        return Response({'error': 'Claw not found'}, status=status.HTTP_404_NOT_FOUND)
    return claw



def _actor(request):
    username = getattr(request.user, 'username', '') or getattr(request.user, 'email', '')
    return username or str(getattr(request.user, 'id', 'unknown'))


def _get_admin_claw(request, claw_id):
    """Fetch any live claw for staff/admin control-plane endpoints."""
    if not getattr(request.user, 'is_staff', False):
        return Response({'error': 'Admin privileges required'}, status=status.HTTP_403_FORBIDDEN)
    try:
        return Claw.objects.exclude(status=Claw.Status.DELETED).get(id=claw_id)
    except (Claw.DoesNotExist, ValueError):
        return Response({'error': 'Claw not found'}, status=status.HTTP_404_NOT_FOUND)


def uuid_safe(value):
    return value.replace('-', '')



def build_export_manifest(job):
    """Build the minimal export manifest for the current safe export class.

    HIVE-561 containment/fix: until a PVC/archive exporter exists, owner/admin
    exports are a manifest-only snapshot of the claw control-plane row. The job
    reaches a terminal state synchronously with checksum/byte metadata so live
    offboarding can prove the export path is not an inert queue. Session/PVC and
    credit-ledger payloads are explicitly marked N/A rather than silently absent.
    """
    claw = job.claw
    return {
        'schema': 'hive.claw-export.manifest.v1',
        'job_id': str(job.id),
        'claw_id': str(claw.id),
        'owner_id': claw.owner_id,
        'requested_by': job.requested_by,
        'requested_by_actor': job.requested_by_actor,
        'snapshot_name': job.snapshot_name,
        'created_at': job.created_at.isoformat() if job.created_at else None,
        'claw': {
            'name': claw.name,
            'namespace': claw.namespace,
            'status': claw.status,
            'tier_id': claw.tier_id,
            'agent_type': claw.agent_type,
            'model': claw.model,
            'runtime_image': claw.runtime_image,
            'runtime_compat_mode': claw.runtime_compat_mode,
            'legal_hold': claw.legal_hold,
        },
        'payloads': {
            'control_plane': 'included',
            'session': 'not_included' if not job.include_session else 'not_available_manifest_only',
            'credits': 'not_available_manifest_only' if job.include_credits else 'not_included',
        },
    }


def complete_export_job(job, request=None):
    """Synchronously complete a manifest-only ClawExportJob."""
    manifest = build_export_manifest(job)
    encoded = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode('utf-8')
    job.status = ClawExportJob.Status.DONE
    job.byte_count = len(encoded)
    job.sha256 = hashlib.sha256(encoded).hexdigest()
    job.completed_at = timezone.now()
    # Status endpoint is the current retrievable artifact until archive storage lands.
    path = f'/hive/api/v1/owner/claws/{job.claw_id}/export/{job.id}'
    if job.requested_by == ClawExportJob.RequestedBy.OPERATOR:
        path = f'/hive/api/v1/admin/claws/{job.claw_id}/export/{job.id}'
    job.share_url = request.build_absolute_uri(path) if request is not None else path
    job.save(update_fields=['status', 'byte_count', 'sha256', 'completed_at', 'share_url', 'updated_at'])
    ClawEvent.objects.create(
        claw=job.claw,
        event_type='export_completed',
        message=(
            f'Export job {job.id} completed as manifest-only snapshot; '
            f'sha256={job.sha256} bytes={job.byte_count} artifact={job.share_url}'
        ),
    )
    return job

def export_snapshot_name(claw_id):
    return f'hive-export-{uuid_safe(str(claw_id))}-{uuid.uuid4().hex}'


def owner_export_step_up_available():
    backend = getattr(settings, 'HIVE_STEPUP_BACKEND', 'stub')
    return backend == 'shizuha-id' or (backend == 'stub' and getattr(settings, 'DEBUG', False))


def parse_export_job_id(value):
    """Parse an export job UUID path component without leaking ORM validation errors."""
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _get_internal_claw(request, claw_id):
    """Fetch a live claw for service-authenticated in-cluster endpoints.

    OpenClaw pods call internal handshake endpoints with a per-claw scoped token
    mounted from their namespace Secret. Owner/staff bearer JWTs are deliberately
    not accepted here: the auth-state check is a service-to-service control-plane
    RPC, not an owner API.
    """
    supplied = request.META.get('HTTP_X_HIVE_INTERNAL_TOKEN', '')
    # Auth-fails-closed BEFORE leaking resource existence (HIVE-204 QA gate):
    # the per-claw token can only be computed from an existing claw, so a missing,
    # malformed, or non-existent-claw token are all "unauthenticated" and MUST return
    # an identical 403 — never a 404 that reveals whether a claw_id exists. Returning
    # 404 here (the old behaviour) let a caller with any bad token enumerate claw UUIDs.
    if not supplied:
        return Response({'error': 'Internal service authentication required'}, status=status.HTTP_403_FORBIDDEN)
    try:
        claw = Claw.objects.exclude(status=Claw.Status.DELETED).get(id=claw_id)
    except (Claw.DoesNotExist, ValueError):
        return Response({'error': 'Internal service authentication required'}, status=status.HTTP_403_FORBIDDEN)
    expected = lifecycle.internal_auth_token(claw)
    if not expected or not hmac.compare_digest(supplied, expected):
        return Response({'error': 'Internal service authentication required'}, status=status.HTTP_403_FORBIDDEN)
    return claw

def _bearer_token(request):
    auth_header = request.META.get('HTTP_AUTHORIZATION', '')
    if auth_header.startswith('Bearer '):
        return auth_header[7:]
    return (
        getattr(request, '_hive_bearer_token', '')
        or getattr(getattr(request, '_request', None), '_hive_bearer_token', '')
        or ''
    )


def _public_platform_url(request) -> str:
    """Public HTTPS platform URL safe for browser/WebView channel launches.

    SHIZUHA_PLATFORM_URL is intentionally internal in k8s so pods can reach ID,
    Connect, etc. over cluster DNS. That internal http://*.svc URL must never be
    returned to Android/WebView clients; prefer an explicit public setting and
    only fall back to the request origin when it is already HTTPS.
    """
    configured = (getattr(settings, 'HIVE_PUBLIC_PLATFORM_URL', '') or '').rstrip('/')
    if configured:
        return configured
    origin = request.build_absolute_uri('/').rstrip('/')
    if origin.startswith('https://'):
        return origin
    platform_url = (getattr(settings, 'SHIZUHA_PLATFORM_URL', '') or '').rstrip('/')
    if platform_url.startswith('https://'):
        return platform_url
    return 'https://shizuha.com'


def _csrf_input(request):
    return f'<input type="hidden" name="csrfmiddlewaretoken" value="{escape(get_token(request))}">'



PLATFORM_APP_SERVICES = [
    {'id': 'home', 'name': 'Home', 'href': '/'},
    {'id': 'hive', 'name': 'Hive', 'href': '/hive'},
    {'id': 'pulse', 'name': 'Pulse', 'href': '/pulse'},
    {'id': 'wiki', 'name': 'Wiki', 'href': '/wiki'},
    {'id': 'connect', 'name': 'Connect', 'href': '/connect'},
    {'id': 'notes', 'name': 'Notes', 'href': '/notes'},
    {'id': 'admin', 'name': 'Admin', 'href': '/admin', 'staff_only': True},
]


def _enabled_service_ids_for_user(user):
    """Return the explicit app/service ids this user may see in the switcher.

    Shizuha ID is the source of truth: modern JWTs carry `enabled_services`;
    `None` means a legacy/staff unrestricted token and is expanded explicitly so
    the frontend never interprets an empty/unknown signal as "show all".
    """
    if getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False):
        return [service['id'] for service in PLATFORM_APP_SERVICES]
    raw = getattr(user, 'enabled_services', None)
    if raw is None:
        return [service['id'] for service in PLATFORM_APP_SERVICES if not service.get('staff_only')]
    if isinstance(raw, str):
        values = {raw}
    else:
        values = {str(value) for value in (raw or [])}
    return [
        service['id']
        for service in PLATFORM_APP_SERVICES
        if (service['id'] == 'home' or service['id'] in values) and not service.get('staff_only')
    ]


def _service_access_payload(request):
    enabled_ids = _enabled_service_ids_for_user(request.user)
    enabled = set(enabled_ids)
    services = [
        {k: v for k, v in service.items() if k != 'staff_only'}
        for service in PLATFORM_APP_SERVICES
        if service['id'] in enabled
    ]
    return {
        'user_id': getattr(request.user, 'id', None),
        'organization_id': request.META.get('HTTP_X_ORGANIZATION_ID') or None,
        'service_ids': enabled_ids,
        'services': services,
        'source': 'shizuha-id.enabled_services',
    }

def _can_provision(user):
    entitlement_values = set(
        getattr(user, 'entitlements', None)
        or getattr(user, 'enabled_services', [])
        or []
    )
    return 'hive:provision' in entitlement_values or 'hive' in entitlement_values


def _safe_hive_next_path(raw_next):
    parsed = urlsplit(raw_next or '')
    if parsed.scheme or parsed.netloc:
        return '/hive/assistants'
    path = parsed.path or '/hive/assistants'
    if path != '/hive' and not path.startswith('/hive/'):
        return '/hive/assistants'
    query = f'?{parsed.query}' if parsed.query else ''
    return f'{path}{query}'


def _is_hive_authenticated(request):
    return bool(getattr(request.user, 'is_authenticated', False))


def _hive_login_redirect(request):
    next_path = quote(_safe_hive_next_path(request.get_full_path()), safe='/')
    return HttpResponseRedirect(f'/hive/login?next={next_path}')


def _require_hive_owner(request):
    if not _is_hive_authenticated(request):
        return _hive_login_redirect(request)
    return None


def _append_query(url, params):
    parsed = urlsplit(url)
    query = parsed.query
    extra = urlencode(params)
    query = f'{query}&{extra}' if query else extra
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))


def _dashboard_token(claw, expires_epoch):
    payload = f'{claw.id}:{claw.owner_id}:{expires_epoch}'
    key = str(getattr(settings, 'HIVE_DASHBOARD_LINK_SIGNING_KEY', settings.SECRET_KEY)).encode()
    sig = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
    encoded = base64.urlsafe_b64encode(payload.encode()).rstrip(b'=').decode()
    return f'{encoded}.{sig}'


def _dashboard_url(claw):
    expires_epoch = int((timezone.now() + timedelta(seconds=300)).timestamp())
    token = _dashboard_token(claw, expires_epoch)
    gateway_token = lifecycle.gateway_auth_token(claw)
    template = getattr(settings, 'HIVE_OPENCLAW_DASHBOARD_URL_TEMPLATE', '/hive/openclaw/{claw_id}/')
    url = template.format(
        claw_id=claw.id,
        namespace=claw.namespace,
        owner_id=claw.owner_id,
    )
    return _append_query(url, {
        'hive_dashboard_token': token,
        'expires': expires_epoch,
    }) + f'#token={quote(gateway_token, safe="")}'


def _is_openclaw_assistant(claw):
    return getattr(claw, 'agent_type', Claw.AgentType.CLAW) == Claw.AgentType.CLAW


def _openclaw_only_response(claw, feature='OpenClaw dashboard'):
    return Response(
        {
            'error': f'{feature} unavailable',
            'detail': 'This assistant type does not expose the OpenClaw surface.',
            'agent_type': getattr(claw, 'agent_type', ''),
        },
        status=status.HTTP_404_NOT_FOUND,
    )


def _dashboard_redirect_path(claw):
    return f'/hive/assistants/{claw.id}/dash'


def _dashboard_api_redirect_path(claw):
    return f'/hive/api/v1/claws/{claw.id}/dashboard-url?redirect=1'


_CHANNEL_TOKEN_TTL = 120  # seconds (HIVE-60 §4)


def _channel_token_signing_key():
    return str(getattr(settings, 'HIVE_DASHBOARD_LINK_SIGNING_KEY', settings.SECRET_KEY)).encode()


def _mint_channel_token(claw):
    """Mint a D6 single-use channel token (HIVE-60 M1).

    Format: base64url({jti}:{claw_id}:{owner_id}:channel:{exp_epoch}).{hmac_sha256_hex}
    Single-use enforced via jti in Django cache.
    Returns (token, exp_epoch).
    """
    jti = uuid.uuid4().hex
    exp_epoch = int((timezone.now() + timedelta(seconds=_CHANNEL_TOKEN_TTL)).timestamp())
    payload = f'{jti}:{claw.id}:{claw.owner_id}:channel:{exp_epoch}'
    key = _channel_token_signing_key()
    sig = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
    encoded = base64.urlsafe_b64encode(payload.encode()).rstrip(b'=').decode()
    token = f'{encoded}.{sig}'
    cache.set(f'channel_tok:{jti}', 1, timeout=_CHANNEL_TOKEN_TTL + 10)
    return token, exp_epoch


def _verify_channel_token(token, expected_claw_id):
    """Verify a D6 channel token and consume it (single-use).

    Raises ValueError on any failure. Returns (owner_id, claw_id) on success.
    """
    try:
        encoded, sig = token.rsplit('.', 1)
    except ValueError:
        raise ValueError('malformed token')
    try:
        payload = base64.urlsafe_b64decode(encoded + '==').decode()
    except Exception:
        raise ValueError('malformed token encoding')
    parts = payload.split(':')
    if len(parts) != 5:
        raise ValueError('malformed token payload')
    jti, claw_id, owner_id, action, exp_epoch = parts
    if action != 'channel':
        raise ValueError('wrong token action')
    key = _channel_token_signing_key()
    expected_sig = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        raise ValueError('invalid signature')
    if int(exp_epoch) < int(timezone.now().timestamp()):
        raise ValueError('token expired')
    if str(expected_claw_id) != str(claw_id):
        raise ValueError('claw_id mismatch')
    cache_key = f'channel_tok:{jti}'
    if not cache.delete(cache_key):
        raise ValueError('token already used or not found')
    return int(owner_id), claw_id


# --- HIVE-201 E1: WhatsApp number linking ----------------------------------

_WA_LINK_TTL = 600  # seconds — a minted LINK code is valid 10 minutes (HIVE-201 §7 E1)


def _mint_wa_link_code(claw):
    """Mint a single-use, short-lived WhatsApp link code bound to ``claw``.

    The code travels through a WhatsApp text message, so it is a short random
    token (not an HMAC blob) and is persisted in the :class:`WhatsAppLink` model
    where single-use is enforced by the pending -> linked status transition.
    Returns the code string.
    """
    code = secrets.token_hex(6)  # 12 hex chars — typeable / tappable in a WA message
    WhatsAppLink.objects.create(
        code=code,
        claw=claw,
        owner_id=claw.owner_id,
        expires_at=timezone.now() + timedelta(seconds=_WA_LINK_TTL),
    )
    return code


def _bind_wa_link(code, phone_jid):
    """Bind a whatsmeow-authenticated ``phone_jid`` to the claw for a pending code.

    The whatsmeow session is the ownership proof (the inbound ``phone_jid`` is
    cryptographically bound to that WA account by Ori's authenticated session —
    no Meta webhook / HMAC; HLD §6 reconciled on HIVE-201). Single-use + atomic:
    a code is consumed exactly once. Idempotent on a re-send from the SAME number.

    Returns ``(result, claw_id|None)`` where result is one of
    ``linked`` | ``already`` | ``expired`` | ``invalid``.
    """
    code = (code or '').strip()
    phone_jid = (phone_jid or '').strip()
    if not code or not phone_jid:
        return 'invalid', None
    try:
        with transaction.atomic():
            link = WhatsAppLink.objects.select_for_update().get(code=code)
            if link.status == WhatsAppLink.Status.LINKED:
                # Idempotent re-send from the same verified number; a different
                # number cannot consume an already-bound code.
                if link.wa_phone_jid == phone_jid:
                    return 'already', str(link.claw_id)
                return 'invalid', None
            if link.expires_at <= timezone.now():
                if link.status != WhatsAppLink.Status.EXPIRED:
                    link.status = WhatsAppLink.Status.EXPIRED
                    link.save(update_fields=['status'])
                return 'expired', None
            link.wa_phone_jid = phone_jid
            link.status = WhatsAppLink.Status.LINKED
            link.linked_at = timezone.now()
            link.save(update_fields=['wa_phone_jid', 'status', 'linked_at'])
            return 'linked', str(link.claw_id)
    except WhatsAppLink.DoesNotExist:
        return 'invalid', None


_WHATSAPP_SESSION_TTL = 24 * 60 * 60
_WHATSAPP_HISTORY_LIMIT = 12


def _parse_whatsapp_message_time(raw):
    if raw in (None, ''):
        return timezone.now()
    try:
        if isinstance(raw, (int, float)) or str(raw).isdigit():
            return timezone.datetime.fromtimestamp(int(raw), tz=datetime_timezone.utc) if getattr(settings, 'USE_TZ', False) else timezone.datetime.fromtimestamp(int(raw))
    except Exception:
        return timezone.now()
    parsed = parse_datetime(str(raw))
    if parsed is None:
        return timezone.now()
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, datetime_timezone.utc)
    return parsed


def _whatsapp_session_key(claw_id, phone_jid):
    digest = hashlib.sha256(str(phone_jid).encode()).hexdigest()[:32]
    return f'hive:wa_session:{claw_id}:{digest}'


def _mask_whatsapp_identity(phone_jid):
    raw = str(phone_jid or '').split('@', 1)[0]
    digits = ''.join(ch for ch in raw if ch.isdigit())
    if len(digits) <= 4:
        return 'wa:****'
    return f'wa:***{digits[-4:]}'


def _whatsapp_daily_limit():
    try:
        return int(getattr(settings, 'HIVE_WHATSAPP_DAILY_MESSAGE_LIMIT', 100))
    except (TypeError, ValueError):
        return 100


def _whatsapp_rate_limit_key(claw_id, phone_jid, now=None):
    now = now or timezone.now()
    day = now.strftime('%Y%m%d')
    digest = hashlib.sha256(str(phone_jid).encode()).hexdigest()[:16]
    return f'hive:wa_daily:{claw_id}:{day}:{digest}'


def _whatsapp_rate_limited(claw_id, phone_jid, now=None):
    limit = _whatsapp_daily_limit()
    if limit <= 0:
        return False, 0, limit
    key = _whatsapp_rate_limit_key(claw_id, phone_jid, now=now)
    current = cache.get(key) or 0
    if current >= limit:
        return True, current, limit
    try:
        if cache.add(key, 1, timeout=_WHATSAPP_SESSION_TTL + 60):
            current = 1
        else:
            current = cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=_WHATSAPP_SESSION_TTL + 60)
        current = 1
    return current > limit, current, limit


def _cortex_chat_url():
    base = (getattr(settings, 'CORTEX_BASE_URL', '') or getattr(settings, 'VLLM_BASE_URL', '')).rstrip('/')
    if not base:
        return ''
    return f'{base}/chat/completions' if base.endswith('/v1') else f'{base}/v1/chat/completions'


def _whatsapp_system_prompt(claw):
    name = getattr(claw, 'name', '') or "the user's assistant"
    return (
        f"You are {name}, the owner's Shizuha Claw assistant replying over WhatsApp. "
        'Answer concisely, helpfully, and safely. Do not claim to have actions or data you do not have. '
        'If a request needs the dashboard or another channel, say so briefly.'
    )


def _generate_whatsapp_reply(claw, phone_jid, body, source_event_id=None):
    url = _cortex_chat_url()
    if not url:
        raise RuntimeError('cortex endpoint not configured')
    session_key = _whatsapp_session_key(claw.id, phone_jid)
    history = cache.get(session_key) or []
    messages = [{'role': 'system', 'content': _whatsapp_system_prompt(claw)}]
    messages.extend(history[-_WHATSAPP_HISTORY_LIMIT:])
    messages.append({'role': 'user', 'content': body})
    payload = {
        'model': getattr(claw, 'model', '') or getattr(settings, 'HIVE_CORTEX_DEFAULT_MODEL', 'DeepSeek-V4-Flash'),
        'messages': messages,
        'temperature': 0.2,
        'max_tokens': 512,
    }
    headers = {'Content-Type': 'application/json'}
    api_key = getattr(settings, 'CORTEX_API_KEY', '')
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'
    resp = requests.post(url, json=payload, headers=headers, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f'cortex rejected request: {resp.status_code}')
    data = resp.json()
    reply = (
        (((data.get('choices') or [{}])[0].get('message') or {}).get('content'))
        or ((data.get('choices') or [{}])[0].get('text'))
        or ''
    ).strip()
    if not reply:
        raise RuntimeError('cortex returned empty reply')

    source_event_id = source_event_id or hashlib.sha256(f'{claw.id}:{phone_jid}:{body}'.encode()).hexdigest()[:32]
    try:
        with transaction.atomic():
            record_agent_turn(
                claw=claw,
                source_event_id=source_event_id,
                metadata={'channel': 'whatsapp'},
            )
            record_cortex_token_usage(
                claw=claw,
                source_event_id=source_event_id,
                usage=data.get('usage') or {},
                model=payload['model'],
                metadata={'channel': 'whatsapp'},
            )
    except Exception as exc:
        logger.exception('usage metering failed for whatsapp claw reply: claw=%s', claw.id)
        raise RuntimeError('usage metering failed before whatsapp reply could be acknowledged') from exc

    next_history = (history + [
        {'role': 'user', 'content': body},
        {'role': 'assistant', 'content': reply},
    ])[-_WHATSAPP_HISTORY_LIMIT:]
    cache.set(session_key, next_history, timeout=_WHATSAPP_SESSION_TTL)
    return reply[:4000]


def _service_internal_auth(request):
    """Authenticate a trusted in-cluster service caller (HIVE-201 E1 bind).

    Unlike :func:`_get_internal_claw` (per-claw scoped — the claw is not known
    until the code is resolved), the WA bind endpoint authenticates the CALLER
    (Ori) as a trusted service via the shared ``HIVE_INTERNAL_AUTH_TOKEN``.
    Fail-closed: an empty secret or mismatched token is rejected.
    """
    supplied = request.META.get('HTTP_X_HIVE_INTERNAL_TOKEN', '')
    secret = getattr(settings, 'HIVE_INTERNAL_AUTH_TOKEN', '')
    return bool(secret) and bool(supplied) and hmac.compare_digest(supplied, secret)


def _tier_by_id(tier_id):
    for tier in hive_tiers():
        if str(tier.get('id', '')) == str(tier_id or ''):
            return tier
    return None


def _tier_pvc_size(tier_id):
    tier = _tier_by_id(tier_id)
    if tier is None:
        return None
    pvc = str(tier.get('pvc', '')).strip()
    return pvc or None


def _normalise_tier_id(tier_id):
    return (tier_id or 'free').strip().lower() or 'free'


def _resolved_tier_pvc_size(tier_id):
    return provisioner.resolve_tier(tier_id).pvc_size


def _active_quota_statuses():
    return [
        Claw.Status.PROVISIONING,
        Claw.Status.RUNNING,
        Claw.Status.STOPPED,
        Claw.Status.SUSPENDED_OWNER_REMOVED,
        Claw.Status.SUSPENDED_NO_CREDITS,
        Claw.Status.DELETING,
    ]


def _active_owner_tier_claws(owner_id, tier_id):
    return Claw.objects.filter(
        owner_id=owner_id,
        tier_id=_normalise_tier_id(tier_id),
        status__in=_active_quota_statuses(),
    )


def _create_owner_claw(request, *, name, tier_id, agent_type=Claw.AgentType.CLAW, model=''):
    """Create the owner-scoped Claw row; return (claw, created).

    The partial unique index added for HIVE-83 is the race-proof quota gate. If a
    concurrent request wins, fetch and return that existing active claw instead
    of creating a duplicate. Provisioning happens after this DB step.

    ``agent_type`` selects the runtime (OpenClaw vs SCLI) and ``model`` overrides
    the per-owner Cortex model; both are owner-driven inputs, never identity.
    """
    tier_id = _normalise_tier_id(tier_id)
    try:
        with transaction.atomic():
            claw = Claw.objects.create(
                owner_id=request.user.id,
                owner_username=request.user.username,
                owner_email=request.user.email,
                name=name,
                tier_id=tier_id,
                agent_type=agent_type or Claw.AgentType.CLAW,
                model=model or '',
                platform_url=settings.SHIZUHA_PLATFORM_URL,
                pvc_size=_resolved_tier_pvc_size(tier_id),
            )
        return claw, True
    except IntegrityError:
        claw = _active_owner_tier_claws(request.user.id, tier_id).order_by('-created_at').first()
        if claw is None:
            raise
        return claw, False


def _quota_conflict_response(claw):
    return Response(
        {'error': 'Active claw already exists for this tier', 'claw': ClawSerializer(claw).data},
        status=status.HTTP_409_CONFLICT,
    )


def _hermes_runtime_unavailable_response(agent_type):
    if agent_type != Claw.AgentType.HERMES:
        return None
    try:
        provisioner.resolve_claw_runtime(SimpleNamespace(agent_type=Claw.AgentType.HERMES))
    except provisioner.HermesRuntimeUnavailable as exc:
        return Response(
            {
                'error': 'Hermes runtime unavailable',
                'detail': str(exc),
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return None


def _step_up_message(user_id, claw_id, action: str, timestamp: int) -> bytes:
    return f"{user_id}:{claw_id}:{action}:{timestamp}".encode()


def make_step_up_proof(user_id, claw_id, action: str, timestamp: int) -> str:
    """Return the HMAC proof shape expected by owner step-up protected actions.

    This is a concrete placeholder for the future Shizuha ID step-up minting
    endpoint: the UI can request/sign a short-lived proof, then send it with
    the destructive action. The API never accepts a normal owner JWT alone for
    hard delete.
    """
    key = getattr(settings, 'HIVE_STEP_UP_SIGNING_KEY', settings.SECRET_KEY)
    return hmac.new(
        str(key).encode(),
        _step_up_message(user_id, claw_id, action, timestamp),
        hashlib.sha256,
    ).hexdigest()


def _validate_step_up(request, claw: Claw, action: str, ttl_seconds: int) -> bool:
    supplied_at = request.META.get('HTTP_X_HIVE_STEP_UP_AT', '')
    supplied_proof = request.META.get('HTTP_X_HIVE_STEP_UP_PROOF', '')
    if not supplied_at or not supplied_proof:
        step_up_token = getattr(request, 'data', {}).get('step_up_token', '')
        if not step_up_token:
            return False
        try:
            return verify_step_up_token(
                str(step_up_token),
                owner_id=claw.owner_id,
                claw_id=str(claw.id),
                action=action,
            )
        except StepUpVerificationError:
            return False
    try:
        timestamp = int(supplied_at)
    except (TypeError, ValueError):
        return False
    now_ts = int(timezone.now().timestamp())
    if timestamp > now_ts + 5:
        return False
    if now_ts - timestamp > ttl_seconds:
        return False
    expected = make_step_up_proof(request.user.id, claw.id, action, timestamp)
    return hmac.compare_digest(supplied_proof, expected)


class ClawProvisionRateThrottle(UserRateThrottle):
    """Throttle provisioning POSTs to one claw per owner per short window."""
    scope = 'claw-provision'


class ClawStartRateThrottle(UserRateThrottle):
    """Throttle start POSTs to one claw start per owner per short window."""
    scope = 'claw-start'


def _builtin_catalog_template(definition):
    """Return an unsaved built-in OrgTemplate for first-run catalog previews."""

    fields = dict(definition)
    return OrgTemplate(
        **fields,
        scrubbed_by='hive:hive-370-catalog-preview',
        scrubbed_at=timezone.now(),
    )


def _catalog_templates():
    """Active DB templates plus any built-ins not seeded yet, sorted for cards."""

    from .org_templates import builtin_template_definitions

    by_slug = {
        template.slug: template
        for template in OrgTemplate.objects.filter(status=OrgTemplate.Status.ACTIVE)
    }
    for definition in builtin_template_definitions():
        by_slug.setdefault(definition['slug'], _builtin_catalog_template(definition))
    return sorted(by_slug.values(), key=lambda t: (t.category or '', t.name or t.slug))


def _catalog_template_or_none(slug):
    slug = (slug or '').strip().lower()
    if not slug:
        return None
    try:
        return OrgTemplate.objects.get(slug=slug, status=OrgTemplate.Status.ACTIVE)
    except OrgTemplate.DoesNotExist:
        from .org_templates import get_builtin_template_definition

        definition = get_builtin_template_definition(slug)
        return _builtin_catalog_template(definition) if definition else None


class OrgTemplateListView(APIView):
    """GET /v1/org-templates — customer-safe HIVE-368 starter catalog."""

    def get(self, request):
        templates = _catalog_templates()
        return Response({
            'count': len(templates),
            'results': OrgTemplateSummarySerializer(templates, many=True).data,
        })


class OrgTemplateDetailView(APIView):
    """GET /v1/org-templates/<slug> — preview the resolved customer plan."""

    def get(self, request, template_slug):
        template = _catalog_template_or_none(template_slug)
        if template is None:
            return Response({'error': 'Template not found'}, status=status.HTTP_404_NOT_FOUND)
        context = {
            'org_name': request.query_params.get('org_name') or 'Your Organization',
            'org_slug': request.query_params.get('org_slug') or 'your-organization',
            'plan_slug': request.query_params.get('plan_slug') or template.recommended_tier or 'pilot',
        }
        try:
            payload = OrgTemplateDetailSerializer(template, context=context).data
        except Exception as exc:
            logger.exception('template plan preview failed: template=%s', template_slug)
            return Response({'error': 'Template preview failed', 'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload)


def _owned_provisioning_run_or_response(request, run_id):
    try:
        run = TenantProvisioningRun.objects.get(id=run_id)
    except (TenantProvisioningRun.DoesNotExist, ValueError):
        return Response({'error': 'Provisioning run not found'}, status=status.HTTP_404_NOT_FOUND)
    if not getattr(request.user, 'is_staff', False) and run.requester_user_id != request.user.id:
        return Response({'error': 'Provisioning run not found'}, status=status.HTTP_404_NOT_FOUND)
    return run


class OrgProvisioningRunListCreateView(APIView):
    """List/create HIVE-320 provisioning runs for the authenticated owner."""

    def get(self, request):
        runs = TenantProvisioningRun.objects.filter(requester_user_id=request.user.id).order_by('-created_at')[:25]
        return Response({
            'count': len(runs),
            'results': TenantProvisioningRunSerializer(runs, many=True).data,
        })

    def post(self, request):
        serializer = TenantProvisioningRunCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if _catalog_template_or_none(data['template_slug']) is None:
            return Response({'template_slug': ['Unknown organization template.']}, status=status.HTTP_400_BAD_REQUEST)

        idempotency_key = (
            data.get('idempotency_key')
            or request.META.get('HTTP_IDEMPOTENCY_KEY')
            or None
        )
        try:
            from .tenant_provisioning import (
                ProvisioningIdempotencyConflict,
                advance_preview,
                create_or_get_run,
            )

            run, created = create_or_get_run(
                requester_user_id=request.user.id,
                requester_email=getattr(request.user, 'email', '') or '',
                org_name=data['org_name'],
                org_slug=data['org_slug'],
                template_slug=data['template_slug'],
                plan_slug=data.get('plan_slug') or '',
                dry_run=data.get('dry_run', True),
                idempotency_key=idempotency_key,
            )
            if data.get('advance', True):
                run = advance_preview(run)
        except ProvisioningIdempotencyConflict:
            return Response(
                {'error': 'Idempotency key has already been used for a different provisioning request.'},
                status=status.HTTP_409_CONFLICT,
            )
        except IntegrityError:
            return Response(
                {'error': 'An active provisioning run already exists for this organization slug.'},
                status=status.HTTP_409_CONFLICT,
            )

        status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return Response(TenantProvisioningRunSerializer(run).data, status=status_code)


class OrgProvisioningRunDetailView(APIView):
    """GET /v1/org-provisioning-runs/<id> — poll live provisioning progress."""

    def get(self, request, run_id):
        run = _owned_provisioning_run_or_response(request, run_id)
        if isinstance(run, Response):
            return run
        return Response(TenantProvisioningRunSerializer(run).data)


class OrgProvisioningRunAdvanceView(APIView):
    """POST /v1/org-provisioning-runs/<id>/advance — idempotent saga retry."""

    def post(self, request, run_id):
        run = _owned_provisioning_run_or_response(request, run_id)
        if isinstance(run, Response):
            return run
        from .tenant_provisioning import advance_preview

        run = advance_preview(run)
        return Response(TenantProvisioningRunSerializer(run).data)


class OrgProvisioningRunDeprovisionView(APIView):
    """POST /v1/org-provisioning-runs/<id>/deprovision — HIVE-2697 ruling 2.

    Deprovision path for saga-parked (manual_review) provisioning runs: the
    golden-path validator's stage-6 teardown must end every run with zero
    qafixture litter, and until now a manual_review-parked saga had no
    terminal cleanup path at all. Best-effort cleanup of created resources up
    to the park point, then a terminal torn_down close (see
    tenant_provisioning.deprovision_parked_run). Idempotent.
    """

    def post(self, request, run_id):
        run = _owned_provisioning_run_or_response(request, run_id)
        if isinstance(run, Response):
            return run
        from .tenant_provisioning import ProvisioningRunNotDeprovisionable, deprovision_parked_run

        try:
            run = deprovision_parked_run(run)
        except ProvisioningRunNotDeprovisionable as exc:
            return Response({'error': str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(TenantProvisioningRunSerializer(run).data)


class OrgProvisioningPlanView(APIView):
    """POST /v1/org-provisioning-plans — VEN-178 S2 dry-run deploy plan for a
    function-pod template into an EXISTING org.

    Planner-only, zero writes: validates template visibility + org admin/owner
    authz on the target org, resolves the template plan, REJECTS (fail closed)
    any sensitive-access capability targeting a customer tenant, and returns a
    deterministic plan with an explicit capability/risk delta list and a
    preview hash for later audit binding of preview → deploy.
    """

    def post(self, request):
        from .tenant_provisioning import DryRunPlanRejected, build_dry_run_plan

        org_slug = (request.data.get('org_slug') or '').strip().lower()
        template_slug = (request.data.get('template_slug') or '').strip().lower()
        plan_slug = (request.data.get('plan_slug') or '').strip().lower()
        tenant_kind = (request.data.get('tenant_kind') or 'customer').strip().lower()
        inputs = request.data.get('inputs') or {}

        if not org_slug:
            return Response({'org_slug': ['This field is required.']}, status=status.HTTP_400_BAD_REQUEST)
        if not template_slug:
            return Response({'template_slug': ['This field is required.']}, status=status.HTTP_400_BAD_REQUEST)
        if _catalog_template_or_none(template_slug) is None:
            return Response({'template_slug': ['Unknown organization template.']}, status=status.HTTP_400_BAD_REQUEST)

        try:
            plan = build_dry_run_plan(
                requester=request.user,
                org_slug=org_slug,
                template_slug=template_slug,
                plan_slug=plan_slug,
                inputs=inputs,
                tenant_kind=tenant_kind,
            )
        except DryRunPlanRejected as exc:
            return Response({'error': str(exc)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        return Response(plan, status=status.HTTP_200_OK)


class ClawListView(APIView):
    """GET — caller's remaining claws. POST — retired (ADR-0080)."""
    throttle_classes = [ClawProvisionRateThrottle]

    def get_throttles(self):
        if self.request.method == 'POST':
            return super().get_throttles()
        return []

    def get(self, request):
        # 1:N ownership — a caller can own many claws, so paginate. Returns the
        # standard DRF envelope {count, next, previous, results:[...]} (page_size 50).
        claws = Claw.objects.exclude(status=Claw.Status.DELETED).filter(
            owner_id=request.user.id
        )
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(claws, request, view=self)
        return paginator.get_paginated_response(ClawSerializer(page, many=True).data)

    def post(self, request):
        return _claw_provisioning_retired_response()


class ClawConnectAgentsView(APIView):
    """Owner-scoped Hive personal agents discoverable by Connect."""

    def get(self, request):
        # HIVE-1957: SCLI claws are now Connect-ready (WS ConnectClient auth
        # wired at provision time) — include them in discovery alongside
        # OpenClaw/Hermes so owners can DM their personal SCLI assistant.
        claws = (
            Claw.objects
            .exclude(status=Claw.Status.DELETED)
            .filter(
                owner_id=request.user.id,
                agent_type__in=[
                    Claw.AgentType.CLAW,
                    Claw.AgentType.HERMES,
                    Claw.AgentType.SCLI,
                ],
            )
            .order_by('agent_type', 'name', 'created_at')
        )
        return Response({
            'count': claws.count(),
            'results': ClawConnectAgentSerializer(claws, many=True).data,
        })


class ClawMineView(APIView):
    """GET — remaining current-user claw, if any. Never provisions or auto-starts.

    ADR-0080: lazy-auto `GET /claws/mine` was the free-Claw GTM path. New
    humans get a FleetAgent personal seat instead.
    """

    def get(self, request):
        claw = (
            Claw.objects
            .exclude(status=Claw.Status.DELETED)
            .filter(owner_id=request.user.id)
            .order_by('-created_at')
            .first()
        )
        if claw is None:
            body = dict(CLAW_PROVISIONING_RETIRED)
            body['error'] = 'No Claw exists for this account'
            return Response(body, status=status.HTTP_404_NOT_FOUND)
        if claw.status == Claw.Status.DELETING:
            return Response(
                {'error': 'Claw is pending deletion', 'claw': ClawSerializer(claw).data},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(ClawSerializer(claw).data)


class ClawDetailView(APIView):
    """GET — detail. PATCH — rename. DELETE — two-phase delete."""

    def get(self, request, claw_id):
        claw = _get_owned_claw(request, claw_id)
        if isinstance(claw, Response):
            return claw
        return Response(ClawSerializer(claw).data)

    def patch(self, request, claw_id):
        claw = _get_owned_claw(request, claw_id)
        if isinstance(claw, Response):
            return claw
        if claw.legal_hold:
            return Response(
                {'error': 'Legal hold active', 'detail': 'Held claws cannot be renamed.'},
                status=status.HTTP_409_CONFLICT,
            )
        serializer = ClawRenameSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # Display-name only (HLD M4) — no k8s resource is renamed.
        claw.name = serializer.validated_data['name']
        claw.save(update_fields=['name', 'updated_at'])
        return Response(ClawSerializer(claw).data)

    def delete(self, request, claw_id):
        claw = _get_owned_claw(request, claw_id)
        if isinstance(claw, Response):
            return claw
        if claw.legal_hold:
            return Response(
                {'error': 'Legal hold active', 'detail': 'Held claws cannot be deleted.'},
                status=status.HTTP_409_CONFLICT,
            )
        if claw.status == Claw.Status.DELETING:
            return Response(
                {'error': 'Claw is already pending deletion'},
                status=status.HTTP_409_CONFLICT,
            )
        # HLD §5 — confirm-required: ?confirm=<claw name>
        confirm = request.query_params.get('confirm', '')
        if confirm != claw.name:
            return Response(
                {'error': 'Confirmation required', 'detail': 'Pass ?confirm=<claw name> to delete.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Phase 1 (HLD §4.2): scale to 0, then mark deleting; owner has 60s to undo.
        # Namespace teardown + 24h Longhorn reclaim is the lifecycle/cron path (follow-up).
        # If the scale-down fails, begin_delete_claw does NOT enter the DELETING
        # state — surface that instead of falsely reporting the claw as deleting.
        if not provisioner.begin_delete_claw(claw):
            return Response(
                {'error': 'Failed to schedule deletion',
                 'detail': 'Could not stop the claw workload; no state change.'},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(
            {'message': 'Claw scheduled for deletion', 'status': claw.status,
             'undo_window_seconds': int(UNDO_DELETE_WINDOW.total_seconds())},
            status=status.HTTP_200_OK,
        )


class ClawUndoDeleteView(APIView):
    """POST — undo a delete within the 60s window (HLD §4.2)."""

    def post(self, request, claw_id):
        claw = _get_owned_claw(request, claw_id)
        if isinstance(claw, Response):
            return claw
        if claw.status != Claw.Status.DELETING or not claw.delete_requested_at:
            return Response({'error': 'Claw is not pending deletion'}, status=status.HTTP_409_CONFLICT)
        if timezone.now() - claw.delete_requested_at > UNDO_DELETE_WINDOW:
            return Response({'error': 'Undo window has passed'}, status=status.HTTP_410_GONE)
        if claw.reclaim_now:
            lifecycle.restore_from_reclaim(claw, actor=_actor(request))
        else:
            provisioner.undo_delete_claw(claw)
        return Response(ClawSerializer(claw).data)


class ClawStartView(APIView):
    """POST — scale 0 -> 1 (HLD §5)."""
    throttle_classes = [ClawStartRateThrottle]


    def post(self, request, claw_id):
        claw = _get_owned_claw(request, claw_id)
        if isinstance(claw, Response):
            return claw
        # A claw in the 60s delete window must not be started — that would scale
        # the workload back up while delete_requested_at is still set, breaking
        # the undo flow (HLD §4.2). Caller must undo-delete first.
        if claw.status == Claw.Status.DELETING:
            return Response(
                {'error': 'Claw is pending deletion',
                 'detail': 'Undo the delete (POST .../undo-delete/) before starting.'},
                status=status.HTTP_409_CONFLICT,
            )
        if not provisioner.start_claw(claw):
            return Response({'error': 'Failed to start claw'}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({'message': 'Claw starting', 'status': claw.status})


class ClawStopView(APIView):
    """POST — scale 1 -> 0, PVC retained (HLD §5)."""

    def post(self, request, claw_id):
        claw = _get_owned_claw(request, claw_id)
        if isinstance(claw, Response):
            return claw
        # Mirror the start guard (HLD §4.2): a claw in the 60s delete window must
        # not be stopped — stop_claw would set STOPPED without clearing
        # delete_requested_at, so undo-delete then 409s while the stale timestamp
        # lingers for the reclaim path. Caller must undo-delete first.
        if claw.status == Claw.Status.DELETING:
            return Response(
                {'error': 'Claw is pending deletion',
                 'detail': 'Undo the delete (POST .../undo-delete/) before stopping.'},
                status=status.HTTP_409_CONFLICT,
            )
        if not provisioner.stop_claw(claw):
            return Response({'error': 'Failed to stop claw'}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({'message': 'Claw stopping', 'status': claw.status})


class ClawEventsView(APIView):
    """GET — lifecycle events (HLD §5 specifies SSE; JSON list here, SSE is follow-up)."""

    def get(self, request, claw_id):
        claw = _get_owned_claw(request, claw_id)
        if isinstance(claw, Response):
            return claw
        events = claw.events.all()[:50]
        return Response(ClawEventSerializer(events, many=True).data)


class ClawStatusView(APIView):
    """GET — live k8s status of a claw (operational; not part of the §5 UI contract)."""

    def get(self, request, claw_id):
        claw = _get_owned_claw(request, claw_id)
        if isinstance(claw, Response):
            return claw
        return Response(provisioner.get_claw_status(claw))


def hive_tiers():
    """ConfigMap-backed tier list surface for HIVE-3 §5.1."""
    return list(getattr(settings, 'HIVE_TIERS', [
        {
            'id': 'small', 'name': 'Small', 'cpu': '500m', 'memory': '1Gi',
            'pvc': '5Gi',
            'llm_quota': {'cluster_local_tok_per_day': 100000, 'remote_tok_per_day': 5000},
            'price': 100,
        },
        {
            'id': 'med', 'name': 'Medium', 'cpu': '1000m', 'memory': '2Gi',
            'pvc': '20Gi',
            'llm_quota': {'cluster_local_tok_per_day': 500000, 'remote_tok_per_day': 50000},
            'price': 400,
        },
    ]))




def _token_pool_counts_queryset():
    return TokenPool.objects.prefetch_related(
        Prefetch('entries', queryset=TokenEntry.objects.filter(deleted_at__isnull=True)),
    ).annotate(
        entry_count=Count('entries', filter=Q(entries__deleted_at__isnull=True)),
        active_entry_count=Count(
            'entries',
            filter=Q(
                entries__active=True,
                entries__agent_leasing_enabled=True,
                entries__deleted_at__isnull=True,
            ),
        ),
    )


def _token_pool_scope(request, requested_scope='user'):
    """Resolve the token-pool owner from the authenticated caller.

    Owner identity is always derived from the JWT-backed request user. The API
    never accepts owner_id from a request body, so a caller cannot ask for or
    mutate another owner's pool by supplying an id.
    """
    scope = (requested_scope or 'user').lower()
    if scope == 'org':
        if not getattr(request.user, 'is_staff', False):
            return Response({'error': 'Admin privileges required'}, status=status.HTTP_403_FORBIDDEN)
        return TokenPool.OwnerKind.ORG, TokenPool.ORG_OWNER_ID
    return TokenPool.OwnerKind.USER, str(request.user.id)


def _allowed_token_pools(request):
    qs = _token_pool_counts_queryset().filter(
        owner_kind=TokenPool.OwnerKind.USER,
        owner_id=str(request.user.id),
    )
    if getattr(request.user, 'is_staff', False):
        org_qs = _token_pool_counts_queryset().filter(
            owner_kind=TokenPool.OwnerKind.ORG,
            owner_id=TokenPool.ORG_OWNER_ID,
        )
        return qs | org_qs
    return qs


def _get_owned_token_pool(request, pool_id):
    try:
        return _allowed_token_pools(request).get(id=pool_id)
    except (TokenPool.DoesNotExist, ValueError):
        return Response({'error': 'Token pool not found'}, status=status.HTTP_404_NOT_FOUND)


def _get_owned_token_entry(request, pool_id, entry_id):
    pool = _get_owned_token_pool(request, pool_id)
    if isinstance(pool, Response):
        return pool, None
    try:
        return pool, pool.entries.get(id=entry_id, deleted_at__isnull=True)
    except (TokenEntry.DoesNotExist, ValueError):
        return pool, Response({'error': 'Token entry not found'}, status=status.HTTP_404_NOT_FOUND)


class TokenPoolListView(APIView):
    """Owner-scoped token-pool config for the HIVE Token Coordinator (HIVE-126)."""

    def get(self, request):
        requested_scope = request.query_params.get('scope')
        qs = _allowed_token_pools(request).order_by('owner_kind', 'provider')
        if requested_scope:
            resolved = _token_pool_scope(request, requested_scope)
            if isinstance(resolved, Response):
                return resolved
            owner_kind, owner_id = resolved
            qs = qs.filter(owner_kind=owner_kind, owner_id=owner_id)
        return Response(TokenPoolSerializer(qs, many=True).data)

    def post(self, request):
        serializer = TokenPoolCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        resolved = _token_pool_scope(request, data.get('scope', 'user'))
        if isinstance(resolved, Response):
            return resolved
        owner_kind, owner_id = resolved
        pool, created = TokenPool.objects.get_or_create(
            owner_kind=owner_kind,
            owner_id=owner_id,
            provider=data['provider'],
            defaults={
                'display_name': data.get('display_name', ''),
                'org_fallback_allowed': bool(data.get('org_fallback_allowed', False)),
            },
        )
        if not created:
            changed = []
            if 'display_name' in data and pool.display_name != data.get('display_name', ''):
                pool.display_name = data.get('display_name', '')
                changed.append('display_name')
            if 'org_fallback_allowed' in data and pool.org_fallback_allowed != data['org_fallback_allowed']:
                pool.org_fallback_allowed = data['org_fallback_allowed']
                changed.append('org_fallback_allowed')
            if changed:
                changed.append('updated_at')
                pool.save(update_fields=changed)
        pool = _token_pool_counts_queryset().get(id=pool.id)
        return Response(TokenPoolSerializer(pool).data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class TokenPoolDetailView(APIView):
    def get(self, request, pool_id):
        pool = _get_owned_token_pool(request, pool_id)
        if isinstance(pool, Response):
            return pool
        return Response(TokenPoolSerializer(pool).data)

    def patch(self, request, pool_id):
        pool = _get_owned_token_pool(request, pool_id)
        if isinstance(pool, Response):
            return pool
        serializer = TokenPoolUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        changed = []
        data = serializer.validated_data
        if 'display_name' in data:
            pool.display_name = data['display_name']
            changed.append('display_name')
        if 'org_fallback_allowed' in data:
            pool.org_fallback_allowed = data['org_fallback_allowed']
            changed.append('org_fallback_allowed')
        if changed:
            changed.append('updated_at')
            pool.save(update_fields=changed)
        pool = _token_pool_counts_queryset().get(id=pool.id)
        return Response(TokenPoolSerializer(pool).data)


class TokenEntryListView(APIView):
    def post(self, request, pool_id):
        pool = _get_owned_token_pool(request, pool_id)
        if isinstance(pool, Response):
            return pool
        serializer = TokenEntryCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        entry = TokenEntry(
            pool=pool,
            label=data['label'],
            account_email=data.get('account_email', ''),
            tier=data.get('tier', ''),
            priority=data.get('priority', 0),
        )
        entry.set_token(data['token'])
        try:
            entry.save()
        except IntegrityError:
            return Response(
                {'error': 'Token label or token already exists in this pool'},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(TokenEntrySerializer(entry).data, status=status.HTTP_201_CREATED)


class TokenEntryDetailView(APIView):
    def patch(self, request, pool_id, entry_id):
        pool, result = _get_owned_token_entry(request, pool_id, entry_id)
        if isinstance(pool, Response):
            return pool
        if isinstance(result, Response):
            return result
        entry = result
        serializer = TokenEntryUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        changed = []
        for field in [
            'label', 'account_email', 'active', 'tier', 'priority',
        ]:
            if field in serializer.validated_data:
                setattr(entry, field, serializer.validated_data[field])
                changed.append(field)
        if changed:
            changed.append('updated_at')
            try:
                entry.save(update_fields=changed)
            except IntegrityError:
                return Response(
                    {'error': 'Token label already exists in this pool'},
                    status=status.HTTP_409_CONFLICT,
                )
        if 'agent_leasing_enabled' in serializer.validated_data:
            from .token_coordinator import set_agent_leasing_policy
            policy = set_agent_leasing_policy(
                entry,
                serializer.validated_data['agent_leasing_enabled'],
                actor=_actor(request),
                source='owner_api',
            )
            entry = policy['entry']
            logger.info(
                'token entry agent leasing changed owner_kind=%s owner_id=%s '
                'provider=%s entry=%s enabled=%s actor=%s changed=%s '
                'revoked_lease_count=%s',
                pool.owner_kind,
                pool.owner_id,
                pool.provider,
                entry.id,
                entry.agent_leasing_enabled,
                _actor(request),
                policy['changed'],
                policy['revoked_lease_count'],
            )
        return Response(TokenEntrySerializer(entry).data)

    def delete(self, request, pool_id, entry_id):
        pool, result = _get_owned_token_entry(request, pool_id, entry_id)
        if isinstance(pool, Response):
            return pool
        if isinstance(result, Response):
            return result
        entry = result
        from .token_coordinator import tombstone_token_entry
        actor = str(getattr(request.user, 'email', '') or request.user.id)
        deletion = tombstone_token_entry(entry, actor=actor)
        logger.info(
            'token entry deleted owner_kind=%s owner_id=%s provider=%s entry=%s label=%s actor=%s',
            pool.owner_kind,
            pool.owner_id,
            pool.provider,
            entry.id,
            entry.label,
            actor,
        )
        if deletion['active_leases']:
            return Response(
                {
                    'deleted': True,
                    'active_leases': deletion['active_leases'],
                    'lease_expires_at': (
                        deletion['lease_expires_at'].isoformat()
                        if deletion['lease_expires_at'] else None
                    ),
                    'detail': (
                        'Credential removed from Hive. Existing access leases may '
                        'remain usable until the reported expiry.'
                    ),
                },
                status=status.HTTP_202_ACCEPTED,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)


class TokenEntryDisableView(APIView):
    def post(self, request, pool_id, entry_id):
        pool, result = _get_owned_token_entry(request, pool_id, entry_id)
        if isinstance(pool, Response):
            return pool
        if isinstance(result, Response):
            return result
        entry = result
        if entry.active:
            entry.active = False
            entry.save(update_fields=['active', 'updated_at'])
        return Response(TokenEntrySerializer(entry).data)


class TokenEntryCheckView(APIView):
    """POST — operator "Check now": re-verify credential / clear expired cool.

    OpenAI/Codex + xAI refresh only when their access token is actually stale.
    A health check must not rotate a fresh single-use refresh-token generation.
    Claude OAT is observational only (raw probe false-kills). Returns updated
    entry serializer + check outcome.
    """

    def post(self, request, pool_id, entry_id):
        from . import token_coordinator as tc

        pool, result = _get_owned_token_entry(request, pool_id, entry_id)
        if isinstance(pool, Response):
            return pool
        if isinstance(result, Response):
            return result
        entry = result
        try:
            check = tc.check_entry_health(entry, force_refresh=False)
        except Exception as exc:
            logger.exception('token entry check failed')
            return Response(
                {'error': f'check_failed: {type(exc).__name__}: {exc}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        entry.refresh_from_db()
        return Response({
            'check': check,
            'entry': TokenEntrySerializer(entry).data,
        })


class TokenEntryRedeemResetView(APIView):
    """POST — operator "Redeem reset": consume ONE banked Codex rate-limit
    reset credit on this OpenAI entry to immediately reset its usage window
    (2026-07-14). Owner-scoped like Check now. Body: {credit_id?} (defaults to
    the first available credit on the snapshot)."""

    def post(self, request, pool_id, entry_id):
        from . import token_coordinator as tc

        pool, result = _get_owned_token_entry(request, pool_id, entry_id)
        if isinstance(pool, Response):
            return pool
        if isinstance(result, Response):
            return result
        entry = result
        credit_id = str(request.data.get('credit_id') or '').strip()
        try:
            res = tc.redeem_entry_reset_credit(entry, credit_id=credit_id)
        except Exception as exc:
            logger.exception('reset-credit redeem failed')
            return Response(
                {'error': f'redeem_failed: {type(exc).__name__}: {exc}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        entry.refresh_from_db()
        http = status.HTTP_200_OK if res.get('ok') else status.HTTP_409_CONFLICT
        return Response({'redeem': res, 'entry': TokenEntrySerializer(entry).data}, status=http)


class CredentialHealthOverviewView(APIView):
    """GET — multi-provider credential health board (Claude / Codex / Grok).

    Codecs.bar-style aggregate: all accounts you can see, operational status,
    cooldown ETAs. Not a substitute for provider billing dashboards.
    """

    def get(self, request):
        from . import token_coordinator as tc

        requested_scope = request.query_params.get('scope')
        qs = _allowed_token_pools(request)
        if requested_scope:
            resolved = _token_pool_scope(request, requested_scope)
            if isinstance(resolved, Response):
                return resolved
            owner_kind, owner_id = resolved
            qs = qs.filter(owner_kind=owner_kind, owner_id=owner_id)
        overview = tc.build_credential_health_overview(owner_scope=qs)
        # Router telemetry (providers, routes, API keys, usage) is Cortex-domain
        # and lives in the Cortex dashboard — Hive shows only the credential/
        # subscription state it owns (operator directive 2026-07-10).
        response = Response(overview)
        response['Cache-Control'] = 'no-store'
        return response

    def post(self, request):
        """POST — force-refresh every visible account's quota snapshot.

        The dashboard's 15s poll only re-reads STORED snapshots; the background
        loop skips anything it considers fresh, and an account stamped
        ``confidence=unsupported`` is skipped for a full 24h. That left rows
        reading "no usage API" while a per-row `Check now` returned real numbers
        immediately, because Check now forces the probe (operator 2026-08-04).

        This is the same force, applied to every account the caller can see, so
        the operator never has to click through rows one at a time.
        """
        from . import token_coordinator as tc

        requested_scope = request.query_params.get('scope') or request.data.get('scope')
        qs = _allowed_token_pools(request)
        if requested_scope:
            resolved = _token_pool_scope(request, requested_scope)
            if isinstance(resolved, Response):
                return resolved
            owner_kind, owner_id = resolved
            qs = qs.filter(owner_kind=owner_kind, owner_id=owner_id)

        providers = sorted({p.provider for p in qs})
        summary = {
            'quota_checked': 0, 'quota_updated': 0,
            'quota_skipped_fresh': 0, 'quota_failed': 0, 'quota_errors': [],
        }
        for provider in providers:
            result = tc.refresh_subscription_quota_snapshots(provider=provider, force=True)
            for key in ('quota_checked', 'quota_updated', 'quota_skipped_fresh', 'quota_failed'):
                summary[key] += int(result.get(key) or 0)
            summary['quota_errors'].extend(result.get('quota_errors') or [])
        summary['providers'] = providers
        overview = tc.build_credential_health_overview(owner_scope=qs)
        return Response({'refresh': summary, **overview})


class TierListView(APIView):
    """GET /v1/tiers — HIVE-3 §5.1 tier model consumed by the UI."""

    def get(self, request):
        return Response({'tiers': hive_tiers()})


class MeView(APIView):
    """Owner context for the Hive UI shell (HIVE-3 API contract)."""

    def get(self, request):
        # Cookie JWT auth is ambient browser authority and therefore keeps
        # Django's CSRF enforcement enabled.  Mint/renew the matching CSRF
        # cookie during the shell's normal auth probe so subsequent SPA
        # mutations can send X-CSRFToken without weakening that protection.
        get_token(request)
        service_access = _service_access_payload(request)
        fleet_read_grant = None
        fleet_read_decision = None
        if not getattr(request.user, 'is_staff', False):
            # Expose the read capability—not a counterfeit staff bit—so the SPA
            # can opt into only the read APIs protected by the same live grant.
            from fleet.views import _active_jit_grant_for_scope
            fleet_read_grant, fleet_read_decision, _reason = _active_jit_grant_for_scope(
                request, _FLEET_READ_JIT_SCOPE,
            )
        return Response({
            'id': request.user.id,
            'username': request.user.username,
            'email': request.user.email,
            # The API is authoritative for authorization.  The browser may be
            # authenticated by the shared ID cookie (or hold a JWT whose
            # payload is not locally decodable), so client-side JWT claims
            # cannot be the only source for rendering privileged workflows.
            'is_staff': bool(getattr(request.user, 'is_staff', False)),
            'is_superuser': bool(getattr(request.user, 'is_superuser', False)),
            'fleet_read_access': bool(
                getattr(request.user, 'is_staff', False) or fleet_read_grant
            ),
            'fleet_read_access_source': (
                'staff' if getattr(request.user, 'is_staff', False)
                else 'jit' if fleet_read_grant
                else 'unavailable' if fleet_read_decision == 'dependency'
                else 'none'
            ),
            'entitlements': list(
                getattr(request.user, 'entitlements', None)
                or getattr(request.user, 'enabled_services', [])
                or []
            ),
            'services': service_access['services'],
            'service_ids': service_access['service_ids'],
            'credits': {'available': None, 'source': 'books-integration-pending'},
        })


class MeServicesView(APIView):
    """Per-user platform service/app access for the app switcher (HIVE-454)."""

    def get(self, request):
        return Response(_service_access_payload(request))


class ClawDashboardUrlView(APIView):
    """Owner-only OpenClaw dashboard redirect-token attachment point."""

    def get(self, request, claw_id):
        claw = _get_owned_claw(request, claw_id)
        if isinstance(claw, Response):
            return claw
        if not _is_openclaw_assistant(claw):
            return _openclaw_only_response(claw)
        state = lifecycle.auth_state(claw)
        if not state['authorized']:
            return Response({'error': 'Dashboard unavailable', 'auth_state': state}, status=status.HTTP_403_FORBIDDEN)
        # HIVE-1711: a non-RUNNING claw has no dashboard upstream (STOPPED =
        # deployment scaled to 0; PROVISIONING/FAILED/DELETING similar). Minting
        # a signed redirect anyway sent the owner to a bare nginx 502 with no
        # explanation — the exact "Aw, Snap"-adjacent dead end this route's QA
        # probe hit. Return the honest runtime state instead; the signed-token
        # flow resumes the moment the claw is RUNNING again.
        if claw.status != Claw.Status.RUNNING:
            return Response({
                'error': 'Dashboard unavailable — agent is not running',
                'claw_status': claw.status,
                'status_message': str(getattr(claw, 'status_message', '') or ''),
                'auth_state': state,
            }, status=status.HTTP_409_CONFLICT)
        dashboard_url = _dashboard_url(claw)
        if request.query_params.get('redirect') in {'1', 'true', 'yes'}:
            return HttpResponseRedirect(dashboard_url)
        return Response({
            'claw_id': str(claw.id),
            'dashboard_url': dashboard_url,
            'expires_in_seconds': 300,
            'auth_state': state,
        })


class OwnerDataActionsView(APIView):
    """Per-claw Data tab extension-point consumed by the Hive UI/HIVE-7."""

    def get(self, request, claw_id):
        claw = _get_owned_claw(request, claw_id)
        if isinstance(claw, Response):
            return claw
        hold = lifecycle.owner_hold_state(claw)
        pending_delete = claw.status == Claw.Status.DELETING or bool(claw.recycle_after or claw.reclaim_now)
        ordinary_delete_window_expired = (
            claw.status == Claw.Status.DELETING
            and claw.delete_requested_at
            and not claw.reclaim_now
            and timezone.now() - claw.delete_requested_at > UNDO_DELETE_WINDOW
        )
        cancel_delete_enabled = pending_delete and not ordinary_delete_window_expired
        hard_delete_pending_deletion = claw.status == Claw.Status.DELETING
        hard_delete_enabled = not claw.legal_hold and not hard_delete_pending_deletion
        if claw.legal_hold:
            hard_delete_disabled_reason = 'legal_hold_active'
        elif hard_delete_pending_deletion:
            hard_delete_disabled_reason = 'pending_deletion'
        else:
            hard_delete_disabled_reason = ''
        export_enabled = owner_export_step_up_available()
        return Response({
            'claw_id': str(claw.id),
            'data_tab_path': f'/hive/assistants/{claw.id}/data',
            'auth_state': lifecycle.auth_state(claw),
            'hold_state': hold,
            'actions': {
                'export': {
                    'enabled': export_enabled,
                    'requires_step_up': True,
                    'step_up_ttl_seconds': 300,
                    'endpoint': f'/hive/api/v1/owner/claws/{claw.id}/export',
                    'disabled_reason': '' if export_enabled else 'step_up_unavailable',
                },
                'hard_delete': {
                    'enabled': hard_delete_enabled,
                    'requires_step_up': True,
                    'step_up_ttl_seconds': 60,
                    'endpoint': f'/hive/api/v1/owner/claws/{claw.id}/hard-delete',
                    'disabled_reason': hard_delete_disabled_reason,
                },
                'cancel_delete': {
                    'enabled': cancel_delete_enabled,
                    'requires_step_up': False,
                    'endpoint': f'/hive/api/v1/owner/claws/{claw.id}/cancel-delete',
                    'disabled_reason': 'undo_window_passed' if pending_delete and ordinary_delete_window_expired else '',
                },
            },
        })


class InternalAuthStateView(APIView):
    """Internal OpenClaw handshake endpoint with a 5s cache contract."""
    authentication_classes = []
    permission_classes = []

    def get(self, request, claw_id):
        claw = _get_internal_claw(request, claw_id)
        if isinstance(claw, Response):
            return claw
        return Response(lifecycle.auth_state(claw))


# ── HIVE-198 E1 — claw proactive-event source (openclaw → hive-api → ID push) ──
# Per-claw, per-event_type rate caps (HLD §3.3). Fixed-window counters in the
# Redis-backed cache; on exceed we DROP (429), never queue. Separate from the
# free-tier conversation limit — proactive pushes are more spam-sensitive.
_NOTIFY_RATE_LIMITS = {
    'assistant_initiated': [(5, 3600), (20, 86400)],  # 5/hour + 20/day
    'scheduler_digest': [(1, 86400)],                 # 1/day
    'scheduler_nudge': [(2, 86400)],                  # 2/day
    'channel_health': [(2, 3600), (6, 86400)],        # CON-161: link-drop / channel watchdog
}
_NOTIFY_DEFAULT_EVENT = 'assistant_initiated'


def _normalize_notify_event_type(event_type):
    event_type = (event_type or _NOTIFY_DEFAULT_EVENT).strip() or _NOTIFY_DEFAULT_EVENT
    if event_type not in _NOTIFY_RATE_LIMITS:
        return _NOTIFY_DEFAULT_EVENT
    return event_type


def _notify_rate_limited(claw_id, event_type):
    """Return True if any per-claw cap for `event_type` is exceeded (→ drop/429).

    Two-pass: check all caps first (so a partial increment can't half-count a
    dropped notification), then increment every cap's window counter on allow.
    Keys are per-claw + per-event_type + per-window so one noisy claw/type can't
    affect others (HLD §3.3).
    """
    caps = _NOTIFY_RATE_LIMITS.get(event_type, _NOTIFY_RATE_LIMITS[_NOTIFY_DEFAULT_EVENT])
    now = int(timezone.now().timestamp())
    keys = []
    for max_count, window in caps:
        key = f'hive:claw_notify:{claw_id}:{event_type}:{window}:{now // window}'
        if (cache.get(key) or 0) >= max_count:
            return True
        keys.append((key, window))
    for key, window in keys:
        try:
            if not cache.add(key, 1, timeout=window + 10):
                cache.incr(key)
        except ValueError:
            cache.set(key, 1, timeout=window + 10)
    return False


class ClawNotifyView(APIView):
    """HIVE-198 E1: a claw proactively pings its owner.

    openclaw → ``POST /hive/api/v1/claws/{claw_id}/notify/`` → ID
    ``internal/push/send`` → FCM → app (the M4 ``claw_channel`` deep-link).

    Security (HLD §3.2/§4): service-auth via the per-claw ``X-Hive-Internal-Token``
    (verified by ``_get_internal_claw`` — a claw can only hit its OWN claw_id, so
    the self-claw assert is structural). The owner is resolved server-side from
    the claw (``user_id`` is NEVER caller-supplied → IDOR is impossible), and
    ``data.type='claw_channel'`` is injected here, not trusted from the caller.
    """
    authentication_classes = []
    permission_classes = []

    def post(self, request, claw_id):
        claw = _get_internal_claw(request, claw_id)
        if isinstance(claw, Response):
            return claw  # 403 (missing/invalid token or not self-claw) or 404 (unknown claw)

        title = (request.data.get('title') or '').strip()
        body = (request.data.get('body') or '').strip()
        if not title or not body:
            return Response({'error': 'title and body are required'}, status=status.HTTP_400_BAD_REQUEST)
        event_type = _normalize_notify_event_type(request.data.get('event_type'))

        # Owner resolution — server-side, un-spoofable (HLD §4). Never caller-supplied.
        owner_id = claw.owner_id
        if not owner_id:
            return Response({'error': 'claw has no owner'}, status=status.HTTP_403_FORBIDDEN)

        # Rate limit (HLD §3.3): drop (429) on exceed, never queue-and-burst.
        if _notify_rate_limited(str(claw.id), event_type):
            logger.info(f"claw notify rate-limited: claw={claw.id} event_type={event_type}")
            return Response(
                {'error': 'rate limited', 'event_type': event_type},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        # Mandatory server-injected routing key for the M4 ConnectNotificationPolicy
        # deep-link (NOT trusted from the caller). Any caller `data` (e.g. an opaque
        # deep-link target the WebView understands) is preserved underneath it.
        data = dict(request.data.get('data') or {})
        data['type'] = 'claw_channel'
        data['event_type'] = event_type

        push_url = getattr(settings, 'SHIZUHA_ID_INTERNAL_PUSH_URL', '') or (
            f"{settings.SHIZUHA_PLATFORM_URL.rstrip('/')}/shizuha-id/api/internal/push/send/"
        )
        payload = {
            'user_id': owner_id,
            'title': title,
            'body': body,
            'data': data,
            'channel_id': 'claw_channel',
        }
        try:
            resp = requests.post(push_url, json=payload, timeout=10)
        except requests.RequestException as exc:
            logger.warning(f"claw notify push delivery failed (claw={claw.id}): {exc}")
            return Response({'error': 'push delivery failed'}, status=status.HTTP_502_BAD_GATEWAY)
        if resp.status_code >= 400:
            logger.warning(
                f"claw notify push rejected (claw={claw.id}): {resp.status_code} {resp.text[:200]}"
            )
            return Response(
                {'error': 'push delivery rejected', 'upstream_status': resp.status_code},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response({'status': 'accepted', 'event_type': event_type}, status=status.HTTP_202_ACCEPTED)


def _hive_status_label(status_value):
    return str(status_value).replace('_', ' ').replace('-', ' ').title()


def _hive_downtime_banner(claw):
    '''Owner-visible downtime disclosure from HIVE-3 §4.4 / M3.'''
    if claw.status == Claw.Status.RUNNING:
        return ''
    reference = claw.last_active_at or claw.updated_at
    if not reference:
        return ''
    downtime = timezone.now() - reference
    if downtime.total_seconds() < 5 * 60:
        return ''
    level = 'danger' if downtime.total_seconds() >= 24 * 60 * 60 else 'warning'
    message = (
        'Inbound WhatsApp messages may be delayed; messages older than ~24h will be lost.'
        if level == 'warning'
        else 'This assistant has been down for over 24h; inbound WhatsApp messages may already be lost.'
    )
    return f'<div class="banner {level}" role="status">{escape(message)}</div>'


def _hive_shell(title, body, request=None):
    user_label = ''
    if request is not None and getattr(request, 'user', None) and getattr(request.user, 'is_authenticated', False):
        user_label = f'<span class="user">{escape(getattr(request.user, "username", ""))}</span>'
    html = f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{ color-scheme: dark; --bg:#09111f; --panel:#111d2f; --muted:#93a4ba; --text:#eef5ff; --line:#26344a; --accent:#8bd3ff; --ok:#68d391; --warn:#f6c453; --danger:#ff8b8b; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; min-height:100vh; font:16px/1.45 system-ui,-apple-system,Segoe UI,sans-serif; background:radial-gradient(circle at top left,#173154,var(--bg) 42rem); color:var(--text); }}
    header, main {{ width:min(1120px, calc(100% - 32px)); margin:0 auto; }}
    header {{ display:flex; align-items:center; justify-content:space-between; padding:24px 0; }}
    a {{ color:var(--accent); }}
    .brand {{ color:var(--text); font-weight:800; letter-spacing:.02em; text-decoration:none; }}
    .nav {{ display:flex; gap:16px; align-items:center; color:var(--muted); }}
    .panel {{ background:rgba(17,29,47,.88); border:1px solid var(--line); border-radius:18px; padding:24px; box-shadow:0 20px 60px rgba(0,0,0,.24); }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:16px; }}
    .card {{ display:block; color:var(--text); text-decoration:none; background:#0d1728; border:1px solid var(--line); border-radius:16px; padding:18px; }}
    .muted {{ color:var(--muted); }}
    .pill {{ display:inline-flex; align-items:center; border-radius:999px; border:1px solid var(--line); padding:2px 10px; font-size:12px; text-transform:uppercase; letter-spacing:.04em; }}
    .pill.running {{ border-color:rgba(104,211,145,.5); color:var(--ok); }}
    .pill.failed, .pill.suspended-owner-removed, .pill.suspended-no-credits {{ border-color:rgba(255,139,139,.5); color:var(--danger); }}
    .pill.deleting, .pill.provisioning {{ border-color:rgba(246,196,83,.5); color:var(--warn); }}
    .actions {{ display:flex; flex-wrap:wrap; gap:10px; margin-top:16px; }}
    .button, button {{ border:0; border-radius:12px; padding:10px 14px; background:var(--accent); color:#06101d; font-weight:700; text-decoration:none; cursor:pointer; }}
    .button.secondary, button.secondary {{ background:#20324d; color:var(--text); border:1px solid var(--line); }}
    .button.danger {{ background:var(--danger); }}
    input, select {{ width:100%; border-radius:12px; border:1px solid var(--line); background:#09111f; color:var(--text); padding:10px 12px; }}
    label {{ display:grid; gap:6px; margin:12px 0; color:var(--muted); }}
    nav.tabs {{ display:flex; gap:10px; border-bottom:1px solid var(--line); margin:18px 0; }}
    nav.tabs a {{ padding:10px 12px; text-decoration:none; color:var(--muted); }}
    nav.tabs a[aria-current="page"] {{ color:var(--text); border-bottom:2px solid var(--accent); }}
    .banner {{ border-radius:14px; padding:12px 14px; margin:14px 0; }}
    .banner.warning {{ color:#332400; background:var(--warn); }}
    .banner.danger {{ color:#330808; background:var(--danger); }}
    code {{ color:#c9d7ff; }}
    .launch-shell {{ min-height: min(720px, calc(100vh - 150px)); display:grid; gap:16px; align-content:start; }}
    .launch-hero {{ display:grid; gap:12px; }}
    .launch-status {{ min-height:170px; display:grid; gap:12px; align-content:center; }}
    .spinner {{ width:42px; height:42px; border-radius:999px; border:4px solid rgba(139,211,255,.2); border-top-color:var(--accent); animation:spin 1s linear infinite; }}
    .skeleton {{ display:grid; gap:10px; }}
    .skeleton span {{ display:block; height:14px; border-radius:999px; background:linear-gradient(90deg, #16243a, #253b5b, #16243a); background-size:180% 100%; animation:pulse 1.4s ease-in-out infinite; }}
    .skeleton span:nth-child(2) {{ width:72%; }}
    .skeleton span:nth-child(3) {{ width:48%; }}
    .empty-state {{ border:1px dashed rgba(139,211,255,.45); border-radius:16px; padding:18px; background:rgba(139,211,255,.06); }}
    .launch-actions {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; }}
    @keyframes spin {{ to {{ transform:rotate(360deg); }} }}
    @keyframes pulse {{ 0%,100% {{ opacity:.62; background-position:0 0; }} 50% {{ opacity:1; background-position:100% 0; }} }}
    @media (max-width: 480px) {{
      body {{ font-size:15px; background:var(--bg); }}
      header, main {{ width:min(100% - 20px, 1120px); }}
      header {{ align-items:flex-start; gap:12px; padding:16px 0; }}
      .nav {{ flex-wrap:wrap; justify-content:flex-end; gap:8px 12px; font-size:14px; }}
      .panel {{ border-radius:16px; padding:18px; }}
      .grid {{ grid-template-columns:1fr; }}
      .actions, .launch-actions {{ flex-direction:column; align-items:stretch; }}
      .button, button {{ width:100%; text-align:center; padding:12px 14px; }}
      nav.tabs {{ overflow-x:auto; padding-bottom:2px; }}
      .launch-shell {{ min-height:calc(100vh - 112px); }}
    }}
  </style>
</head>
<body>
  <header>
    <a class="brand" href="/hive/assistants">Hive</a>
    <nav class="nav" aria-label="Primary">
      <a href="/hive/assistants">My Assistants</a>
      <a href="/hive/assistants/new">Provision</a>
      {user_label}
    </nav>
  </header>
  <main>{body}</main>
</body>
</html>'''
    return HttpResponse(html)


class HiveLoginView(APIView):
    '''Unauthenticated entry route for the Shizuha-ID login journey.'''
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        next_path = quote(_safe_hive_next_path(request.query_params.get('next')), safe='/')
        login_href = f'/id/accounts/login/?next={next_path}'
        body = f'''
<section class="panel">
  <p class="muted">Shizuha-ID session required</p>
  <h1>Sign in to manage your Hive assistants</h1>
  <p>Hive uses your Shizuha-ID owner identity for the My Assistants list, provisioning, lifecycle controls, and owner-only OpenClaw dashboard links. Browser routes accept the Shizuha-ID JWT cookie set during sign-in.</p>
  <p><a class="button" href="{escape(login_href)}">Continue with Shizuha-ID</a></p>
</section>'''
        return _hive_shell('Hive Login', body)


class HiveAssistantsListShellView(APIView):
    '''Server-rendered My Assistants list for the HIVE-3/HIVE-8 owner UI.'''
    permission_classes = []

    def get(self, request):
        login = _require_hive_owner(request)
        if login is not None:
            return login
        claws = list(Claw.objects.exclude(status=Claw.Status.DELETED).filter(owner_id=request.user.id)[:50])
        if claws:
            cards = []
            for claw in claws:
                status_slug = str(claw.status).replace('_', '-')
                last_seen = claw.last_active_at or claw.updated_at
                last_seen_text = last_seen.isoformat() if last_seen else 'unknown'
                cards.append(f'''
<a class="card" href="/hive/assistants/{claw.id}" data-claw-id="{claw.id}">
  <h2>{escape(claw.name)}</h2>
  <p><span class="pill {escape(status_slug)}">{escape(_hive_status_label(claw.status))}</span></p>
  <p class="muted">Tier: {escape(claw.tier_id or 'unassigned')} · Last seen: {escape(last_seen_text)}</p>
  <p class="muted">Credits burn-rate: pending Books integration</p>
</a>''')
            content = f'<div class="grid">{"".join(cards)}</div>'
        else:
            empty_body = '''
  <h2>No Claw tenants remain</h2>
  <p class="muted">Hive agents are FleetAgents. Open the Agents page to talk to your personal assistant. Claw provisioning is retired (ADR-0080).</p>
  <p><a class="button" href="/hive/">Open Agents</a></p>'''
            content = f'<section class="panel">{empty_body}\n</section>'
        body = f'''
<section class="panel">
  <p class="muted">My Assistants</p>
  <h1>Your Hive assistants</h1>
  <p>Remaining OpenClaw tenants for {escape(request.user.username)} (read/stop/delete only). New agents are FleetAgents.</p>
</section>
<br>
{content}'''
        return _hive_shell('Hive Assistants', body, request)


class HiveClawLaunchShellView(APIView):
    """Hive-owned mobile-friendly Claw launch wrapper (HIVE-189)."""
    permission_classes = []

    def get(self, request):
        login = _require_hive_owner(request)
        if login is not None:
            return login
        body = f"""
<section class="panel launch-shell" data-testid="claw-launch-shell">
  <div class="launch-hero">
    <p class="muted">Hive · Free Claw</p>
    <h1>Talk to your Claw</h1>
    <p class="muted">Ask your Claw anything… Hive will wake or provision your assistant, mint a short-lived channel token, then open the OpenClaw chat surface.</p>
  </div>
  <div id="claw-launch-status" class="launch-status" role="status" aria-live="polite">
    <div class="spinner" aria-hidden="true"></div>
    <div>
      <h2>Getting your Claw ready</h2>
      <p class="muted">Checking your assistant and provisioning it if this is your first visit.</p>
    </div>
    <div class="skeleton" aria-hidden="true"><span></span><span></span><span></span></div>
  </div>
  <div class="launch-actions">
    <button id="claw-launch-open" type="button" disabled>Open Claw chat</button>
    <button id="claw-launch-retry" class="secondary" type="button" hidden>Retry</button>
    <a class="button secondary" href="/hive/assistants">Manage assistants</a>
  </div>
</section>
<script>
(() => {{
  const statusEl = document.getElementById('claw-launch-status');
  const openBtn = document.getElementById('claw-launch-open');
  const retryBtn = document.getElementById('claw-launch-retry');
  const csrf = {get_token(request)!r};
  let launchUrl = '';

  function clearStatus() {{
    while (statusEl.firstChild) statusEl.removeChild(statusEl.firstChild);
  }}

  function appendText(tagName, text, className = '') {{
    const node = document.createElement(tagName);
    if (className) node.className = className;
    node.textContent = text;
    return node;
  }}

  function setStatus(kind, title, detail) {{
    const tone = kind === 'error' ? 'banner danger' : kind === 'empty' ? 'empty-state' : 'banner';
    const container = document.createElement('div');
    container.className = tone;
    container.appendChild(appendText('h2', title));
    container.appendChild(appendText('p', detail));
    clearStatus();
    statusEl.appendChild(container);
    retryBtn.hidden = kind !== 'error';
    openBtn.disabled = kind !== 'ready';
  }}

  function setLoading() {{
    const spinner = document.createElement('div');
    spinner.className = 'spinner';
    spinner.setAttribute('aria-hidden', 'true');
    const copy = document.createElement('div');
    copy.appendChild(appendText('h2', 'Getting your Claw ready'));
    copy.appendChild(appendText('p', 'Checking your assistant and provisioning it if this is your first visit.', 'muted'));
    const skeleton = document.createElement('div');
    skeleton.className = 'skeleton';
    skeleton.setAttribute('aria-hidden', 'true');
    skeleton.appendChild(document.createElement('span'));
    skeleton.appendChild(document.createElement('span'));
    skeleton.appendChild(document.createElement('span'));
    clearStatus();
    statusEl.append(spinner, copy, skeleton);
  }}

  function validateLaunchUrl(rawUrl) {{
    const target = new URL(rawUrl, window.location.origin);
    if (!['http:', 'https:'].includes(target.protocol)) {{
      throw new Error('OpenClaw returned an unsupported launch URL.');
    }}
    const allowedHosts = new Set([window.location.host]);
    const configuredPlatform = {getattr(settings, 'SHIZUHA_PLATFORM_URL', '')!r};
    if (configuredPlatform) {{
      try {{ allowedHosts.add(new URL(configuredPlatform, window.location.origin).host); }} catch (_) {{}}
    }}
    if (!allowedHosts.has(target.host)) {{
      throw new Error('OpenClaw returned an unexpected launch host.');
    }}
    return target;
  }}

  async function requestJson(url, options = {{}}) {{
    const response = await fetch(url, {{ credentials: 'same-origin', ...options }});
    const text = await response.text();
    let data = {{}};
    try {{ data = text ? JSON.parse(text) : {{}}; }} catch (_) {{ data = {{ detail: text }}; }}
    if (!response.ok) {{
      const message = data.detail || data.error || `Request failed (${{response.status}})`;
      const err = new Error(message);
      err.status = response.status;
      throw err;
    }}
    return data;
  }}

  async function prepareClaw() {{
    openBtn.disabled = true;
    retryBtn.hidden = true;
    setLoading();
    try {{
      const claw = await requestJson('/hive/api/v1/claws/mine');
      setStatus('empty', 'Ready for your first message', `Ask your Claw anything… ${{claw.name || 'Your assistant'}} is warming up now.`);
      const token = await requestJson('/hive/api/v1/claws/mine/channel-token', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json', 'X-CSRFToken': csrf }},
        body: '{{}}',
      }});
      const target = validateLaunchUrl(token.platform_url);
      target.searchParams.set('st', token.token);
      launchUrl = target.toString();
      setStatus('ready', 'Your Claw is ready', 'Open the chat when you are ready. Hive does not proxy chat content; OpenClaw will render the conversation.');
    }} catch (err) {{
      if (err.status === 404 || err.status === 403) {{
        setStatus('empty', 'No Claw is available yet', 'Your account may still be waiting for Free Claw access. You can retry or manage assistants from Hive.');
      }} else if (err.status === 409) {{
        setStatus('error', 'Your Claw is not ready yet', err.message || 'Provisioning is still in progress. Try again in a moment.');
      }} else {{
        setStatus('error', 'We could not open your Claw', err.message || 'Check your connection and retry.');
      }}
    }}
  }}

  openBtn.addEventListener('click', () => {{ if (launchUrl) window.location.assign(launchUrl); }});
  retryBtn.addEventListener('click', prepareClaw);
  prepareClaw();
}})();
</script>"""
        return _hive_shell('Talk to your Claw', body, request)


class HiveNewAssistantShellView(APIView):
    '''Provision form shell backed by the HLD §5 POST /v1/claws flow.'''
    permission_classes = []
    throttle_classes = [ClawProvisionRateThrottle]

    def get_throttles(self):
        if self.request.method == 'POST':
            return super().get_throttles()
        return []

    def _body(self, request, error=''):
        can_provision = _can_provision(request.user)
        disabled_note = '' if can_provision else '<p class="banner warning">Provisioning requires the <code>hive:provision</code> entitlement and credits balance.</p>'
        error_note = f'<p class="banner danger">{escape(error)}</p>' if error else ''
        options = ''.join(
            '<option value="{id}">{name} — {cpu} / {memory} / {pvc} · {price} credits/day</option>'.format(
                id=escape(str(tier.get('id', ''))),
                name=escape(str(tier.get('name') or tier.get('id', ''))),
                cpu=escape(str(tier.get('cpu', ''))),
                memory=escape(str(tier.get('memory', ''))),
                pvc=escape(str(tier.get('pvc', ''))),
                price=escape(str(tier.get('price', tier.get('price_credits_per_day', '')))),
            )
            for tier in hive_tiers()
        )
        return f'''
<section class="panel">
  <p class="muted">Provision</p>
  <h1>Create a Hive assistant</h1>
  {disabled_note}
  {error_note}
  <form action="/hive/assistants/new" method="post" data-endpoint="/hive/api/v1/claws">
    {_csrf_input(request)}
    <label>Assistant name <input name="name" maxlength="100" placeholder="My Assistant" required></label>
    <label>Tier <select name="tier_id" required>{options}</select></label>
    <p class="muted">Tier options are rendered from the same source as <code>GET /hive/api/v1/tiers</code>, backed by the operator-managed <code>hive-tiers</code> ConfigMap.</p>
    <button type="submit" {'disabled' if not can_provision else ''}>Provision assistant</button>
  </form>
</section>'''

    def get(self, request):
        login = _require_hive_owner(request)
        if login is not None:
            return login
        body = '''
<section class="panel">
  <p class="muted">Provision</p>
  <h1>Claw provisioning is retired</h1>
  <p>Hive agents are FleetAgents. A personal assistant is created as an isolated owned agent, not an OpenClaw tenant. See ADR-0080.</p>
  <p><a class="button" href="/hive/">Open Agents</a></p>
</section>'''
        return _hive_shell('Provision Hive Assistant', body, request)

    def post(self, request):
        login = _require_hive_owner(request)
        if login is not None:
            return login
        return _claw_provisioning_retired_response()


class HiveAssistantShellView(APIView):
    '''Server-rendered owner dashboard shell for an individual assistant.'''
    permission_classes = []

    def get(self, request, claw_id, tab='overview'):
        login = _require_hive_owner(request)
        if login is not None:
            return login
        claw = _get_owned_claw(request, claw_id)
        if isinstance(claw, Response):
            return claw
        active_tab = tab if tab in {'overview', 'data', 'dashboard'} else 'overview'
        overview_link = f'/hive/assistants/{claw.id}'
        data_link = f'/hive/assistants/{claw.id}/data'
        dashboard_link = f'/hive/assistants/{claw.id}/dashboard'
        rename_link = f'/hive/assistants/{claw.id}/rename'
        status_slug = str(claw.status).replace('_', '-')
        panel = f'''
<section id="overview-tab" class="panel">
  <h2>Lifecycle controls</h2>
  <p class="muted">Controls call the owner-scoped HLD §5 API endpoints. Start/stop are idempotent; delete requires name confirmation and keeps the 60s undo window.</p>
  <div class="actions" id="lifecycle-controls">
    <form action="/hive/assistants/{claw.id}/start" method="post" data-endpoint="/hive/api/v1/claws/{claw.id}/start">{_csrf_input(request)}<button type="submit">Start</button></form>
    <form action="/hive/assistants/{claw.id}/stop" method="post" data-endpoint="/hive/api/v1/claws/{claw.id}/stop">{_csrf_input(request)}<button class="secondary" type="submit">Stop</button></form>
    <form action="/hive/assistants/{claw.id}/undo-delete" method="post" data-endpoint="/hive/api/v1/claws/{claw.id}/undo-delete">{_csrf_input(request)}<button class="secondary" type="submit">Undo delete</button></form>
    <form action="/hive/assistants/{claw.id}/delete" method="post" data-endpoint="/hive/api/v1/claws/{claw.id}" data-method="DELETE">{_csrf_input(request)}<label>Type assistant name to delete <input name="confirm" placeholder="{escape(claw.name)}" autocomplete="off" required></label><button class="danger" type="submit">Delete</button></form>
    <a class="button secondary" href="{rename_link}">Rename</a>
  </div>
</section>'''
        if active_tab == 'data':
            actions = [
                ('Export data', f'/hive/api/v1/owner/claws/{claw.id}/export'),
                ('Delete now', f'/hive/api/v1/owner/claws/{claw.id}/hard-delete'),
                ('Cancel scheduled delete', f'/hive/api/v1/owner/claws/{claw.id}/cancel-delete'),
            ]
            buttons = ''.join(
                f'<li><code>{escape(endpoint)}</code> — {escape(label)}</li>'
                for label, endpoint in actions
            )
            panel = f'<section id="data-tab" class="panel"><h2>Data</h2><p class="muted">Data export/offboarding actions are step-up gated.</p><ul>{buttons}</ul></section>'
        elif active_tab == 'dashboard':
            state = lifecycle.auth_state(claw)
            is_suspended = claw.status in {
                Claw.Status.SUSPENDED_OWNER_REMOVED,
                Claw.Status.SUSPENDED_NO_CREDITS,
            }
            is_stopped = claw.status == Claw.Status.STOPPED
            if is_suspended:
                status_msg = (
                    'Your assistant has been suspended due to a billing issue. '
                    'Please check your credits and contact support if this continues.'
                    if claw.status == Claw.Status.SUSPENDED_NO_CREDITS
                    else 'Your assistant has been suspended — your account may have been removed. '
                    'Contact support to restore access.'
                )
                dashboard_content = f'<p class="banner danger">{escape(status_msg)}</p>'
            elif is_stopped:
                dashboard_content = f'''
<p>Your assistant is stopped. Start it to open the dashboard.</p>
<form action="/hive/assistants/{claw.id}/start" method="post"
      data-endpoint="/hive/api/v1/claws/{claw.id}/start">
  {_csrf_input(request)}
  <button type="submit">Start assistant</button>
</form>'''
            elif not state['authorized']:
                dashboard_content = '<p class="banner warning">Dashboard unavailable — assistant is not ready yet. Try again in a moment.</p>'
            else:
                dashboard_content = f'<p><a class="button" href="/hive/assistants/{claw.id}/dash">Open dashboard</a></p>'
            panel = f'''
<section id="dashboard-tab" class="panel">
  <h2>OpenClaw dashboard</h2>
  <p class="muted">Owner-only access. Hive mints a short-lived signed token so only you can open your assistant's dashboard.</p>
  {dashboard_content}
</section>'''
        body = f'''
<section class="panel" data-claw-id="{claw.id}" data-owner-id="{claw.owner_id}">
  <p class="muted"><a href="/hive/assistants">My Assistants</a> / {escape(claw.name)}</p>
  <h1>{escape(claw.name)}</h1>
  <p><span class="pill {escape(status_slug)}">{escape(_hive_status_label(claw.status))}</span></p>
  {_hive_downtime_banner(claw)}
  <p class="muted">Namespace: <code>{escape(claw.namespace)}</code> · Tier: {escape(claw.tier_id or 'unassigned')} · PVC: {escape(claw.pvc_size)}</p>
  <nav class="tabs" aria-label="Assistant tabs">
    <a href="{overview_link}" aria-current="{'page' if active_tab == 'overview' else 'false'}">Overview</a>
    <a href="{data_link}" aria-current="{'page' if active_tab == 'data' else 'false'}">Data</a>
    <a href="{dashboard_link}" aria-current="{'page' if active_tab == 'dashboard' else 'false'}">OpenClaw dashboard</a>
  </nav>
</section>
<br>
{panel}'''
        return _hive_shell(f'Hive Assistant - {claw.name}', body, request)


class HiveAssistantActionView(APIView):
    """Browser form adapters for HIVE-3 lifecycle controls."""
    permission_classes = []
    throttle_classes = [ClawStartRateThrottle]

    def post(self, request, claw_id, action):
        login = _require_hive_owner(request)
        if login is not None:
            return login
        claw = _get_owned_claw(request, claw_id)
        if isinstance(claw, Response):
            return claw
        if action == 'start':
            if claw.status == Claw.Status.DELETING or not provisioner.start_claw(claw):
                return _hive_shell('Hive Assistant', f'<section class="panel"><h1>Start failed</h1><p><a href="/hive/assistants/{claw.id}">Back</a></p></section>', request)
        elif action == 'stop':
            if claw.status == Claw.Status.DELETING or not provisioner.stop_claw(claw):
                return _hive_shell('Hive Assistant', f'<section class="panel"><h1>Stop failed</h1><p><a href="/hive/assistants/{claw.id}">Back</a></p></section>', request)
        elif action == 'undo-delete':
            if claw.status != Claw.Status.DELETING or not claw.delete_requested_at:
                return HttpResponseRedirect(f'/hive/assistants/{claw.id}')
            if timezone.now() - claw.delete_requested_at > UNDO_DELETE_WINDOW:
                return _hive_shell('Hive Assistant', f'<section class="panel"><h1>Undo window has passed</h1><p><a href="/hive/assistants/{claw.id}">Back</a></p></section>', request)
            if claw.reclaim_now:
                lifecycle.restore_from_reclaim(claw, actor=_actor(request))
            else:
                provisioner.undo_delete_claw(claw)
        elif action == 'delete':
            if claw.legal_hold or claw.status == Claw.Status.DELETING:
                return HttpResponseRedirect(f'/hive/assistants/{claw.id}')
            if (request.data.get('confirm') or '') != claw.name:
                return _hive_shell('Hive Assistant', f'<section class="panel"><h1>Confirmation required</h1><p><a href="/hive/assistants/{claw.id}">Back</a></p></section>', request)
            if not provisioner.begin_delete_claw(claw):
                return _hive_shell('Hive Assistant', f'<section class="panel"><h1>Delete failed</h1><p><a href="/hive/assistants/{claw.id}">Back</a></p></section>', request)
        else:
            return Response({'error': 'unknown action'}, status=status.HTTP_404_NOT_FOUND)
        return HttpResponseRedirect(f'/hive/assistants/{claw.id}')


class HiveAssistantRenameShellView(APIView):
    """Display-name mutation route from HIVE-3 §3 / §5."""
    permission_classes = []

    def get(self, request, claw_id):
        login = _require_hive_owner(request)
        if login is not None:
            return login
        claw = _get_owned_claw(request, claw_id)
        if isinstance(claw, Response):
            return claw
        body = f'''
<section class="panel">
  <p class="muted"><a href="/hive/assistants/{claw.id}">{escape(claw.name)}</a> / Rename</p>
  <h1>Rename assistant</h1>
  <form action="/hive/assistants/{claw.id}/rename" method="post" data-endpoint="/hive/api/v1/claws/{claw.id}" data-method="PATCH">
    {_csrf_input(request)}
    <label>Display name <input name="name" maxlength="100" value="{escape(claw.name)}" required></label>
    <button type="submit">Save name</button>
  </form>
</section>'''
        return _hive_shell('Rename Hive Assistant', body, request)

    def post(self, request, claw_id):
        login = _require_hive_owner(request)
        if login is not None:
            return login
        claw = _get_owned_claw(request, claw_id)
        if isinstance(claw, Response):
            return claw
        if claw.legal_hold:
            return Response({'error': 'Legal hold active'}, status=status.HTTP_409_CONFLICT)
        serializer = ClawRenameSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        claw.name = serializer.validated_data['name']
        claw.save(update_fields=['name', 'updated_at'])
        return HttpResponseRedirect(f'/hive/assistants/{claw.id}')


class HiveAssistantDashRedirectView(APIView):
    """Owner-only `/dash` route from HIVE-3 §3; redirects to the dashboard URL."""
    permission_classes = []

    def get(self, request, claw_id):
        login = _require_hive_owner(request)
        if login is not None:
            return login
        claw = _get_owned_claw(request, claw_id)
        if isinstance(claw, Response):
            return claw
        if not _is_openclaw_assistant(claw):
            body = f'''
<section class="panel">
  <p class="muted"><a href="/hive/assistants/{claw.id}">← {escape(claw.name)}</a></p>
  <h1>Dashboard unavailable</h1>
  <p class="banner warning">{escape(claw.get_agent_type_display())} does not expose an OpenClaw dashboard.</p>
</section>'''
            response = _hive_shell('Dashboard Unavailable', body, request)
            response.status_code = 404
            return response
        state = lifecycle.auth_state(claw)
        if not state['authorized']:
            reason = state.get('reason', '')
            if reason in {
                str(Claw.Status.SUSPENDED_NO_CREDITS),
                str(Claw.Status.SUSPENDED_OWNER_REMOVED),
            }:
                message = 'Your assistant has been suspended. Check your account settings or contact support.'
            else:
                message = 'The dashboard is not available right now. Your assistant may still be starting up — try again in a moment.'
            body = f'''
<section class="panel">
  <p class="muted"><a href="/hive/assistants/{claw.id}">← {escape(claw.name)}</a></p>
  <h1>Dashboard unavailable</h1>
  <p class="banner warning">{escape(message)}</p>
  <div class="actions">
    <a class="button secondary" href="/hive/assistants/{claw.id}/dashboard">Check status</a>
  </div>
</section>'''
            return _hive_shell('Dashboard Unavailable', body, request)
        return HttpResponseRedirect(_dashboard_url(claw))


class OwnerClawExportView(APIView):
    """Owner export job creation/status (HIVE-4 §4.1 / §5)."""

    def get(self, request, claw_id, job_id=None):
        claw = _get_owned_claw(request, claw_id)
        if isinstance(claw, Response):
            return claw
        jobs = claw.export_jobs.all()
        if job_id is not None:
            parsed_job_id = parse_export_job_id(job_id)
            if parsed_job_id is None:
                return Response({'error': 'Export job not found'}, status=status.HTTP_404_NOT_FOUND)
            jobs = jobs.filter(id=parsed_job_id, requested_by=ClawExportJob.RequestedBy.OWNER)
            job = jobs.first()
            if not job:
                return Response({'error': 'Export job not found'}, status=status.HTTP_404_NOT_FOUND)
            return Response(ClawExportJobSerializer(job).data)
        return Response(ClawExportJobSerializer(
            jobs.filter(requested_by=ClawExportJob.RequestedBy.OWNER)[:50],
            many=True,
        ).data)

    def post(self, request, claw_id):
        claw = _get_owned_claw(request, claw_id)
        if isinstance(claw, Response):
            return claw
        serializer = ClawExportRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            if not verify_step_up_token(
                data.get('step_up_token', ''),
                owner_id=claw.owner_id,
                claw_id=str(claw.id),
                action='export',
            ):
                return Response({'error': 'Step-up required'}, status=status.HTTP_403_FORBIDDEN)
        except StepUpVerificationError as exc:
            return Response({'error': 'Step-up verification failed', 'detail': str(exc)}, status=status.HTTP_403_FORBIDDEN)
        job = ClawExportJob.objects.create(
            claw=claw,
            owner_id=claw.owner_id,
            requested_by=ClawExportJob.RequestedBy.OWNER,
            requested_by_actor=_actor(request),
            include_session=data['include_session'],
            include_credits=data['include_credits'],
            snapshot_name=export_snapshot_name(claw.id),
        )
        job = complete_export_job(job, request=request)
        return Response(ClawExportJobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


class OwnerHardDeleteView(APIView):
    """Owner hard-delete/reclaim request entrypoint (HIVE-4/HIVE-7)."""

    def post(self, request, claw_id):
        claw = _get_owned_claw(request, claw_id)
        if isinstance(claw, Response):
            return claw
        if claw.legal_hold:
            return Response(
                {'error': 'Legal hold active', 'hold_state': lifecycle.owner_hold_state(claw)},
                status=status.HTTP_409_CONFLICT,
            )
        if claw.status == Claw.Status.DELETING:
            return Response(
                {'error': 'Claw is already pending deletion'},
                status=status.HTTP_409_CONFLICT,
            )
        if not _validate_step_up(request, claw, 'hard_delete', ttl_seconds=60):
            return Response(
                {'error': 'Recent step-up proof required', 'step_up_ttl_seconds': 60},
                status=status.HTTP_403_FORBIDDEN,
            )
        if not provisioner.stop_claw_for_reclaim(claw):
            ClawHardDeleteFailure.objects.create(
                claw=claw,
                owner_id=claw.owner_id,
                requested_by=_actor(request),
                failure_class='scale_down_failed',
                requested_at=timezone.now(),
            )
            return Response(
                {
                    'error': 'Failed to queue hard delete',
                    'detail': 'Could not stop the claw workload; reclaim was not queued.',
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )
        if claw.status == Claw.Status.RUNNING:
            # The workload is now scaled to zero; record the post-scale state as
            # the cancel target so cancel-delete doesn't claim a stopped pod is
            # still running unless a future path also scales it back up.
            claw.status = Claw.Status.STOPPED
        claw, accepted = lifecycle.mark_reclaim_now(
            claw,
            reason='Owner hard-delete requested',
            actor=_actor(request),
        )
        if not accepted:
            return Response(
                {'error': 'Legal hold active', 'hold_state': lifecycle.owner_hold_state(claw)},
                status=status.HTTP_409_CONFLICT,
            )
        return Response({'message': 'Hard delete queued', 'hold_state': lifecycle.owner_hold_state(claw)})


class OwnerCancelDeleteView(APIView):
    """Cancel pending reclaim/delete intent."""

    def post(self, request, claw_id):
        claw = _get_owned_claw(request, claw_id)
        if isinstance(claw, Response):
            return claw
        has_pending_cancel_target = (
            claw.status == Claw.Status.DELETING
            or bool(claw.delete_requested_at or claw.recycle_after or claw.reclaim_now)
        )
        if not has_pending_cancel_target:
            return Response(
                {'error': 'No pending delete or reclaim to cancel'},
                status=status.HTTP_409_CONFLICT,
            )
        session_id = request.META.get('HTTP_X_SESSION_ID', '')
        if len(session_id) > 128:
            return Response(
                {'error': 'Session id is too long', 'max_length': 128},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if (
            claw.status == Claw.Status.DELETING
            and claw.delete_requested_at
            and not claw.reclaim_now
        ):
            if timezone.now() - claw.delete_requested_at > UNDO_DELETE_WINDOW:
                return Response({'error': 'Undo window has passed'}, status=status.HTTP_410_GONE)
        lifecycle.restore_from_reclaim(claw, actor=_actor(request))
        state = getattr(claw, '_state', None)
        if state is not None and not state.adding:
            ClawCancelDelete.objects.create(
                claw=claw,
                owner_id=claw.owner_id,
                session_id=session_id,
            )
        return Response(ClawSerializer(claw).data)


class AdminLegalHoldSetView(APIView):
    """Set/update legal hold on any claw (admin-only)."""

    def post(self, request, claw_id):
        claw = _get_admin_claw(request, claw_id)
        if isinstance(claw, Response):
            return claw
        serializer = LegalHoldSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        lifecycle.set_legal_hold(
            claw,
            reason=data.get('reason', ''),
            case_id=data.get('case_id', ''),
            actor=_actor(request),
        )
        return Response(lifecycle.hold_state(claw))


class AdminLegalHoldClearView(APIView):
    """Clear legal hold on any claw (admin-only)."""

    def post(self, request, claw_id):
        claw = _get_admin_claw(request, claw_id)
        if isinstance(claw, Response):
            return claw
        lifecycle.clear_legal_hold(claw, actor=_actor(request))
        return Response(lifecycle.hold_state(claw))


class AdminReclaimRestoreView(APIView):
    """Admin backstop endpoint to cancel reclaim and restore retained state."""

    def post(self, request, claw_id):
        claw = _get_admin_claw(request, claw_id)
        if isinstance(claw, Response):
            return claw
        lifecycle.restore_from_reclaim(claw, actor=_actor(request))
        return Response(lifecycle.hold_state(claw))


class ChannelTokenRateThrottle(UserRateThrottle):
    """Per-owner mint throttle for the channel-token endpoint (HIVE-68 §Security).

    UserRateThrottle keys on the authenticated user's pk (== owner_id, derived
    from the verified JWT), so this rate-limits mints PER OWNER. A channel open
    is interactive, not bursty; the rate caps abusive repeat-mints that would
    thrash start_claw (k8s scale) and grow the single-use-jti cache. Rate is set
    via DEFAULT_THROTTLE_RATES['channel-token'] in settings.
    """
    scope = 'channel-token'


class ClawChannelTokenView(APIView):
    """POST /v1/claws/mine/channel-token — mint a D6 single-use channel token (HIVE-68/HIVE-60 M1).

    Resolves the caller's active claw, starts it if STOPPED, mints a short-lived
    HMAC token, and returns it. The mobile app opens OpenClaw's WebView at
    platform_url?st=<token>; OpenClaw verifies via the internal verify endpoint.
    """

    throttle_classes = [ChannelTokenRateThrottle]

    _NON_STARTABLE = frozenset({
        Claw.Status.DELETING,
        Claw.Status.FAILED,
        Claw.Status.PROVISIONING,
        Claw.Status.SUSPENDED_OWNER_REMOVED,
        Claw.Status.SUSPENDED_NO_CREDITS,
    })

    def post(self, request):
        owner_id = request.user.id
        claw = (
            Claw.objects
            .exclude(status=Claw.Status.DELETED)
            .filter(owner_id=owner_id)
            .order_by('-created_at')
            .first()
        )
        if claw is None:
            return Response(
                {'error': 'No claw found', 'detail': 'Provision a claw first.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        if claw.status in self._NON_STARTABLE:
            return Response(
                {'error': 'Claw not ready', 'status': claw.status},
                status=status.HTTP_409_CONFLICT,
            )
        if claw.status == Claw.Status.STOPPED:
            if not provisioner.start_claw(claw):
                return Response({'error': 'Failed to start claw'}, status=status.HTTP_502_BAD_GATEWAY)
        token, exp_epoch = _mint_channel_token(claw)
        return Response({
            'token': token,
            'platform_url': _public_platform_url(request),
            'expires_at': exp_epoch,
        })


def _claw_native_whatsapp_pairing_payload(claw):
    """Return the Hive-facing instructions for OpenClaw-native WA pairing.

    Operator decision on HIVE-201 (2026-06-25): do not use Ori/whatsmeow
    ``LINK-<code>`` as the Claw WhatsApp mechanism. Each Claw already owns its
    OpenClaw WhatsApp channel and pairing infrastructure, so Hive's job is to
    route the owner to that native surface instead of minting a cross-service
    bind code.
    """
    state = lifecycle.auth_state(claw)
    dashboard_url = _dashboard_redirect_path(claw) if state.get('authorized') else ''
    return {
        'mode': 'claw_native_pairing',
        'claw_id': str(claw.id),
        'status': claw.status,
        # Return a Hive owner-authenticated redirect path, not the raw
        # /hive/openclaw/<id>?hive_dashboard_token=... URL. In production the
        # /hive/ prefix is served by the SPA; handing that raw OpenClaw URL to
        # the browser makes the SPA boot without owner session context and call
        # /hive/api/v1/claws/ unauthenticated (QA saw a 403/login shell instead
        # of the Claw WhatsApp channel). The /dash route mints the short-lived
        # OpenClaw token server-side for the logged-in owner at click time.
        'dashboard_url': dashboard_url,
        'dashboard_path': _dashboard_redirect_path(claw),
        'dashboard_api_path': _dashboard_api_redirect_path(claw),
        'auth_state': state,
        'steps': [
            'Start the assistant if it is stopped.',
            'Open the assistant dashboard from Hive while signed in as the owner.',
            "Use the assistant's WhatsApp channel/pairing screen; OpenClaw will verify the WhatsApp sender and bind it to this Claw.",
            'After pairing, send WhatsApp messages directly to this assistant; no Ori LINK code is required.',
        ],
    }


class ClawWhatsAppPairingView(APIView):
    """POST /v1/claws/<id>/whatsapp/pairing — return Claw-native WA pairing.

    This intentionally does not mint a ``LINK-<code>`` or target the shared Ori
    number. HIVE-201 was re-scoped by the operator to use each Claw's native
    OpenClaw WhatsApp pairing infrastructure as-is.
    """

    throttle_classes = [ChannelTokenRateThrottle]

    def post(self, request, claw_id):
        claw = _get_owned_claw(request, claw_id)
        if isinstance(claw, Response):
            return claw
        if not _is_openclaw_assistant(claw):
            return _openclaw_only_response(claw, 'WhatsApp pairing')
        return Response(_claw_native_whatsapp_pairing_payload(claw), status=status.HTTP_200_OK)


class ClawWhatsAppLinkView(APIView):
    """Compatibility endpoint for the old dashboard action.

    HIVE-201 no longer mints an Ori ``LINK-<code>``. Keep the URL stable for any
    deployed frontend while returning the same Claw-native pairing payload.
    """

    throttle_classes = [ChannelTokenRateThrottle]

    def post(self, request):
        owner_id = request.user.id
        claw = (
            Claw.objects
            .exclude(status=Claw.Status.DELETED)
            .filter(owner_id=owner_id, agent_type=Claw.AgentType.CLAW)
            .order_by('-created_at')
            .first()
        )
        if claw is None:
            return Response(
                {'error': 'No claw found', 'detail': 'Provision a claw first.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(_claw_native_whatsapp_pairing_payload(claw), status=status.HTTP_200_OK)


def _send_wa_unlink_confirmation(phone_jid):
    """Best-effort unlink confirmation via the configured WhatsApp/Ori sender.

    HIVE-187 E2 requires a WhatsApp confirmation on unlink. Deployments that have
    not wired the Ori send bridge yet still perform the self-service unlink and
    log the pending notification instead of failing the owner's revocation.
    """
    phone_jid = str(phone_jid or '').strip()
    if not phone_jid:
        return False
    url = str(getattr(settings, 'HIVE_WA_SEND_URL', '') or '').strip()
    token = str(getattr(settings, 'HIVE_INTERNAL_AUTH_TOKEN', '') or '').strip()
    payload = {
        'phone_jid': phone_jid,
        'text': 'Your WhatsApp is now unlinked from your Claw',
    }
    if not url:
        logger.info('wa unlink confirmation queued/no-send-url: phone=%s', phone_jid)
        return False
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['X-Hive-Internal-Token'] = token
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=6)
        if r.status_code >= 400:
            logger.warning('wa unlink confirmation failed: phone=%s status=%s body=%s', phone_jid, r.status_code, r.text[:200])
            return False
        return True
    except Exception as exc:
        logger.warning('wa unlink confirmation failed: phone=%s error=%s', phone_jid, exc)
        return False


class ClawWhatsAppLinksView(APIView):
    """GET /v1/claws/{id}/whatsapp/links — owner-scoped WA link management."""

    def get(self, request, claw_id):
        claw = _get_owned_claw(request, claw_id)
        if isinstance(claw, Response):
            return claw
        qs = WhatsAppLink.objects.filter(claw=claw, owner_id=claw.owner_id).exclude(status=WhatsAppLink.Status.PENDING)
        serializer = WhatsAppLinkSerializer(qs, many=True)
        return Response({'results': serializer.data})


class ClawWhatsAppLinkDetailView(APIView):
    """PATCH/DELETE /v1/claws/{id}/whatsapp/links/{code}."""

    def _get_link(self, request, claw_id, code):
        claw = _get_owned_claw(request, claw_id)
        if isinstance(claw, Response):
            return claw, None
        try:
            link = WhatsAppLink.objects.get(code=code, claw=claw, owner_id=claw.owner_id)
        except WhatsAppLink.DoesNotExist:
            return Response({'error': 'WhatsApp link not found'}, status=status.HTTP_404_NOT_FOUND), None
        return claw, link

    def patch(self, request, claw_id, code):
        _, link = self._get_link(request, claw_id, code)
        if link is None:
            return _
        requested = str(request.data.get('status') or '').strip().lower()
        mapping = {
            'active': WhatsAppLink.Status.LINKED,
            'linked': WhatsAppLink.Status.LINKED,
            'paused': WhatsAppLink.Status.PAUSED,
        }
        new_status = mapping.get(requested)
        if not new_status:
            return Response({'error': 'status must be active or paused'}, status=status.HTTP_400_BAD_REQUEST)
        if not link.wa_phone_jid:
            return Response({'error': 'Cannot update an unverified WhatsApp link'}, status=status.HTTP_409_CONFLICT)
        link.status = new_status
        link.save(update_fields=['status'])
        logger.info('wa link status changed: actor=%s claw=%s code=%s status=%s', _actor(request), link.claw_id, link.code[:6], new_status)
        return Response(WhatsAppLinkSerializer(link).data)

    def delete(self, request, claw_id, code):
        _, link = self._get_link(request, claw_id, code)
        if link is None:
            return _
        phone_jid = link.wa_phone_jid
        link.delete()
        confirmation_sent = _send_wa_unlink_confirmation(phone_jid)
        logger.info('wa link unlinked: actor=%s claw=%s phone=%s confirmation_sent=%s', _actor(request), claw_id, phone_jid, confirmation_sent)
        return Response({'ok': True, 'confirmation_sent': confirmation_sent}, status=status.HTTP_200_OK)


class InternalWhatsAppMessageView(APIView):
    """POST /v1/internal/claws/<id>/whatsapp/message — native WA → Claw reply.

    Called by the Claw/OpenClaw-native WhatsApp channel after it has verified the
    sender/session. Hive authenticates the caller with the per-claw internal token,
    resolves the owner from the claw, preserves a short 24h session cache keyed by
    (claw, WhatsApp identity), calls Cortex using the claw's configured model, and
    returns a WhatsApp-safe reply for the channel to send. Errors return HTTP 200
    with a friendly reply so the WA bridge does not retry-storm.
    """
    authentication_classes = []
    permission_classes = []

    def post(self, request, claw_id):
        claw = _get_internal_claw(request, claw_id)
        if isinstance(claw, Response):
            if claw.status_code == status.HTTP_403_FORBIDDEN:
                logger.warning(
                    'whatsapp internal auth rejected: claw=%s remote=%s',
                    claw_id,
                    request.META.get('REMOTE_ADDR', ''),
                )
            return claw

        phone_jid = (
            request.data.get('phone_jid')
            or request.data.get('wa_phone_jid')
            or request.data.get('wa_number')
            or ''
        ).strip()
        body = (request.data.get('body') or request.data.get('text') or '').strip()
        paired = request.data.get('paired', True)
        if isinstance(paired, str):
            paired = paired.strip().lower() not in ('false', '0', 'no')

        if not paired:
            return Response({
                'status': 'not_linked',
                'reply': 'This WhatsApp number is not linked to a Shizuha Claw yet. Open Hive and use Link WhatsApp from your assistant dashboard.',
                'claw_id': str(claw.id),
            }, status=status.HTTP_200_OK)
        if not phone_jid or not body:
            return Response({
                'status': 'ignored',
                'reply': 'I could not read that WhatsApp message. Please try again.',
                'claw_id': str(claw.id),
            }, status=status.HTTP_200_OK)

        message_time = _parse_whatsapp_message_time(
            request.data.get('message_timestamp') or request.data.get('timestamp')
        )
        now = timezone.now()
        if timezone.is_naive(now) and timezone.is_aware(message_time):
            message_time = timezone.make_naive(message_time, datetime_timezone.utc)
        elif timezone.is_aware(now) and timezone.is_naive(message_time):
            message_time = timezone.make_aware(message_time, datetime_timezone.utc)
        if now - message_time > timedelta(seconds=_WHATSAPP_SESSION_TTL):
            return Response({
                'status': 'template_required',
                'reply': "This conversation is outside WhatsApp's 24-hour reply window. Please send a new message to continue.",
                'claw_id': str(claw.id),
                'template_required': True,
            }, status=status.HTTP_200_OK)

        limited, count, limit = _whatsapp_rate_limited(claw.id, phone_jid, now=now)
        if limited:
            logger.info(
                'whatsapp claw daily limit reached: claw=%s wa=%s count=%s limit=%s',
                claw.id, _mask_whatsapp_identity(phone_jid), count, limit,
            )
            return Response({
                'status': 'rate_limited',
                'reply': "You've reached today's WhatsApp limit for your Claw. It resets at midnight.",
                'claw_id': str(claw.id),
                'retry_after': 'midnight',
            }, status=status.HTTP_200_OK)

        source_event_id = (
            request.data.get('message_id')
            or request.data.get('wamid')
            or request.data.get('event_id')
            or hashlib.sha256(f'{claw.id}:{phone_jid}:{int(message_time.timestamp())}:{body}'.encode()).hexdigest()[:32]
        )

        try:
            reply = _generate_whatsapp_reply(claw, phone_jid, body, source_event_id=source_event_id)
        except Exception as exc:
            logger.warning('whatsapp claw reply failed: claw=%s wa=%s err=%s', claw.id, _mask_whatsapp_identity(phone_jid), exc)
            return Response({
                'status': 'upstream_unavailable',
                'reply': 'Your Claw is temporarily unavailable. Please try again in a moment.',
                'claw_id': str(claw.id),
            }, status=status.HTTP_200_OK)

        return Response({
            'status': 'replied',
            'reply': reply,
            'claw_id': str(claw.id),
            'session_ttl_seconds': _WHATSAPP_SESSION_TTL,
        }, status=status.HTTP_200_OK)


# ── HIVE-355: Connect DM → personal-agent (OpenClaw/Hermes) delivery ──────────
# Connect's dispatch_hive_agent_message POSTs here (HIVE_CONNECT_DELIVERY_URL,
# see connect/services/hive_agents.py) with
#   {stable_id, conversation_id, message_id, sender_id, content, client_message_id}
# and expects a FAST 2xx handoff ack (its request timeout is 5s). The assistant
# reply is produced asynchronously (cortex reply-gen alone is ~30s) and posted
# BACK to Connect's internal agent-message endpoint, keyed for idempotency so a
# replay never double-replies. Before this route existed the handoff 404'd and no
# assistant response was produced (the reported bug).
_CONNECT_DELIVERY_DEDUPE_TTL = 6 * 60 * 60
_CONNECT_DELIVERY_MAX_BODY_BYTES = 64 * 1024
_CONNECT_DELIVERY_MAX_CONTENT_CHARS = 16 * 1024
_CONNECT_DELIVERY_RATE_WINDOW_SECONDS = 60
_CONNECT_DELIVERY_RATE_LIMIT = 30
_CONNECT_DELIVERY_RESERVATION_LOCK = threading.Lock()

# A delivery tuple must be claimed in the same Redis transition that charges
# its owner+claw budget.  Splitting GET/INCR/ADD lets an overlapping retry lose
# the race after the first request was admitted and surface a false 429.
_CONNECT_DELIVERY_RESERVATION_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 1 then
    return 2
end
local count = redis.call('INCR', KEYS[2])
if count == 1 then
    redis.call('EXPIRE', KEYS[2], tonumber(ARGV[1]))
end
if count > tonumber(ARGV[2]) then
    return 0
end
redis.call('SET', KEYS[1], ARGV[3], 'EX', tonumber(ARGV[4]))
return 1
"""


def _connect_internal_base_url() -> str:
    # _post_connect_agent_reply appends /connect/api/... because it talks via the
    # platform/nginx route. Do not default to the raw shizuha-connect Service:
    # that service exposes /api/... directly and returns 404 for /connect/api/...
    # (the CON-130 live fixture failure).
    return (getattr(settings, 'SHIZUHA_CONNECT_INTERNAL_URL', '')
            or getattr(settings, 'SHIZUHA_PLATFORM_URL', '')
            or 'http://shizuha-nginx.shizuha.svc.cluster.local').rstrip('/')


def _hive_agent_user_id(stable_id) -> int:
    """Deterministic pseudo user_id for a Hive agent — MUST match Connect's
    hive_agent_user_id() (connect/services/hive_agents.py) so the reply is
    attributed to the same agent participant Connect created in the conversation."""
    digest = hashlib.sha256(str(stable_id).encode('utf-8')).hexdigest()
    return 1_500_000_000 + (int(digest[:8], 16) % 500_000_000)


def _claw_from_stable_id(stable_id):
    """Resolve the Claw for a ``hive:<agent_type>:<claw_uuid>`` stable id, or None."""
    parts = str(stable_id or '').split(':')
    if len(parts) != 3 or parts[0] != 'hive':
        return None
    agent_type = parts[1]
    if agent_type not in Claw.AgentType.values:
        return None
    try:
        claw_id = uuid.UUID(parts[2])
    except (ValueError, TypeError):
        return None
    try:
        # HIVE-613: bind the authenticated stable-id type as well as its UUID.
        # Resolving by UUID alone lets a caller relabel a claw's runtime class.
        return Claw.objects.exclude(status=Claw.Status.DELETED).get(
            id=claw_id,
            agent_type=agent_type,
        )
    except Claw.DoesNotExist:
        return None


def _owner_personal_organization_id(owner_id):
    """Resolve the canonical organization that owns a personal Claw.

    Personal agents are provisioned into the owner's personal organization.
    Admin is the authority for that binding; a signed caller assertion alone is
    never sufficient.  Fail closed when the authority is unavailable.
    """
    cache_key = f'hive:con613:personal-org:{owner_id}'
    cached = cache.get(cache_key)
    if cached is not None:
        return int(cached) if cached else None
    base = getattr(settings, 'HIVE_ADMIN_API_URL', '').rstrip('/')
    headers = {'X-Internal-Service': 'shizuha-hive'}
    token = getattr(settings, 'ADMIN_INTERNAL_SERVICE_TOKEN', '')
    if token:
        headers['X-Internal-Service-Token'] = token
    try:
        response = requests.get(
            f'{base}/internal/users/{int(owner_id)}/organizations/',
            headers=headers,
            timeout=4,
        )
        if response.status_code == 200:
            candidates = []
            for organization in (response.json() or {}).get('organizations', []):
                if (organization.get('is_personal') is True and
                        str(organization.get('owner_id')) == str(int(owner_id))):
                    candidates.append(int(organization['id']))
            if len(candidates) == 1:
                cache.set(cache_key, candidates[0], 60)
                return candidates[0]
            logger.warning(
                'HIVE-613 canonical personal organization ambiguous owner=%s candidates=%s',
                owner_id, len(candidates),
            )
    except (requests.RequestException, TypeError, ValueError, KeyError) as exc:
        logger.warning('HIVE-613 personal organization lookup failed owner=%s: %s', owner_id, exc)
    return None


def _connect_delivery_rate_limited(owner_id, claw_id):
    """Fixed-window tuple rate limit for the authenticated Connect service."""
    window = int(time.time()) // _CONNECT_DELIVERY_RATE_WINDOW_SECONDS
    digest = hashlib.sha256(f'{owner_id}\0{claw_id}'.encode()).hexdigest()
    key = f'hive:con613:rate:{window}:{digest}'
    if cache.add(key, 1, timeout=_CONNECT_DELIVERY_RATE_WINDOW_SECONDS + 1):
        return False
    try:
        return cache.incr(key) > _CONNECT_DELIVERY_RATE_LIMIT
    except ValueError:
        return False


def _reserve_connect_delivery(owner_id, claw_id, dedupe_key):
    """Atomically reserve a new tuple and its owner+claw rate budget.

    Production uses one Redis script, so another request can observe either no
    reservation or the complete reservation, never the old increment-before-
    claim gap.  The process lock is only for non-Redis test/dev cache backends.
    """
    window = int(time.time()) // _CONNECT_DELIVERY_RATE_WINDOW_SECONDS
    digest = hashlib.sha256(f'{owner_id}\0{claw_id}'.encode()).hexdigest()
    rate_key = f'hive:con613:rate:{window}:{digest}'
    redis_cache = getattr(cache, '_cache', None)
    get_client = getattr(redis_cache, 'get_client', None)
    serializer = getattr(redis_cache, '_serializer', None)
    if callable(get_client) and serializer is not None:
        try:
            result = get_client(dedupe_key, write=True).eval(
                _CONNECT_DELIVERY_RESERVATION_SCRIPT,
                2,
                cache.make_and_validate_key(dedupe_key),
                cache.make_and_validate_key(rate_key),
                _CONNECT_DELIVERY_RATE_WINDOW_SECONDS + 1,
                _CONNECT_DELIVERY_RATE_LIMIT,
                serializer.dumps('reserved'),
                _CONNECT_DELIVERY_DEDUPE_TTL,
            )
        except Exception as exc:
            logger.error('HIVE-613 atomic delivery reservation failed: %s', exc)
            return 'unavailable'
        return {0: 'rate_limited', 1: 'reserved', 2: 'duplicate'}.get(
            int(result), 'unavailable')

    with _CONNECT_DELIVERY_RESERVATION_LOCK:
        if cache.get(dedupe_key) is not None:
            return 'duplicate'
        if _connect_delivery_rate_limited(owner_id, claw_id):
            return 'rate_limited'
        if not cache.add(
                dedupe_key, 'reserved', timeout=_CONNECT_DELIVERY_DEDUPE_TTL):
            return 'duplicate'
        return 'reserved'


def _connect_delivery_audit(outcome, *, owner_id='', organization_id='',
                            stable_id='', conversation_id='', message_id=''):
    def safe(value):
        # Audit fields can be caller-controlled. Keep them single-line and
        # bounded so an authenticated denial cannot forge or flood log records.
        value = str(value or '')[:128]
        return ''.join(ch if 32 <= ord(ch) < 127 else '?' for ch in value)

    logger.info(
        'HIVE-613 delivery audit outcome=%s service=shizuha-connect owner_id=%s '
        'organization_id=%s stable_id=%s conversation_id=%s message_id=%s',
        safe(outcome), safe(owner_id), safe(organization_id), safe(stable_id),
        safe(conversation_id), safe(message_id),
    )


def _connect_delivery_duplicate_response(*, owner_id, organization_id, stable_id,
                                         conversation_id, message_id):
    """Acknowledge an already-accepted authenticated delivery tuple."""
    from . import connect_delivery_metrics as _metrics
    _metrics.record('duplicate')
    _connect_delivery_audit(
        'duplicate', owner_id=owner_id,
        organization_id=organization_id, stable_id=stable_id,
        conversation_id=conversation_id, message_id=message_id,
    )
    return Response(
        {'status': 'duplicate', 'message_id': message_id, 'stable_id': stable_id},
        status=status.HTTP_200_OK,
    )


def _claw_runtime_bridge_url(claw):
    """Return the in-cluster URL for this claw's OpenClaw/Hermes runtime bridge.

    Each claw runs in its own namespace (``claw-<owner_id>-<claw_id_hex>``) with
    a k8s Service named ``claw`` on port ``HIVE_CLAW_HTTP_PORT`` (default 18789).
    The runtime bridge serves ``/v1/connect/messages`` for Connect DM delivery.

    HIVE-1957: SCLI claws do NOT serve this bridge — they receive DMs via the
    runtime's WebSocket ConnectClient (see ``_process_connect_delivery``), so
    this URL is only used for OpenClaw/Hermes claws.
    """
    ns = getattr(claw, 'namespace', '')
    if not ns:
        raise RuntimeError(f'claw {claw.id} has no namespace — cannot route to runtime bridge')
    http_port = int(getattr(settings, 'HIVE_CLAW_HTTP_PORT', 18789))
    return f'http://claw.{ns}.svc.cluster.local:{http_port}/v1/connect/messages'


def _claw_delivery_token(claw):
    """Return the delivery auth token for this claw's runtime bridge.

    HIVE-1276: derive via ``lifecycle.internal_auth_token`` (HMAC of the root
    internal secret over ``{claw.id}:{namespace}``). That is the same value
    provisioner injects into the claw-config secret as ``internal-auth-token``
    / ``CONNECT_RUNTIME_DELIVERY_TOKEN`` — no live k8s Secret read, so a hung
    apiserver cannot stall the Connect delivery daemon thread.
    """
    token = lifecycle.internal_auth_token(claw)
    if not token:
        raise RuntimeError(
            f'failed to derive delivery token for claw {claw.id}: '
            'HIVE_INTERNAL_AUTH_TOKEN unset or empty'
        )
    return token


def _claw_runtime_deliverable(claw):
    """Return ``(deliverable, reason)`` for a status=running claw.

    HIVE-1276 D1: Connect accepts only when the claw Deployment has ≥1 ready
    replica. Empty ``pod_name`` is NOT a signal — provision/start never set it
    (only adopt/fleet paths do), so healthy claws commonly have blank pod_name.

    Fail-open on probe infrastructure errors (no kubeconfig in unit tests / CI
    without a cluster) so acceptance still works offline. Fail-closed on a
    definitive undeliverable observation (missing Deployment, 0 ready replicas).
    """
    ns = (getattr(claw, 'namespace', '') or '').strip()
    if not ns:
        return False, 'no_namespace'
    try:
        from kubernetes.client.rest import ApiException

        from .provisioner import DEPLOYMENT_NAME, get_k8s_client

        _core_v1, apps_v1 = get_k8s_client()[:2]
        try:
            # Short timeout: this runs on the accept path before 202.
            deploy = apps_v1.read_namespaced_deployment(
                DEPLOYMENT_NAME, ns, _request_timeout=2,
            )
        except ApiException as exc:
            if getattr(exc, 'status', None) == 404:
                return False, 'deployment_missing'
            logger.warning(
                'HIVE-1276 deliverability probe API error claw=%s ns=%s status=%s',
                getattr(claw, 'id', ''), ns, getattr(exc, 'status', None),
            )
            return True, f'api_error_fail_open:{getattr(exc, "status", "?")}'
        ready = 0
        if getattr(deploy, 'status', None) is not None:
            ready = int(deploy.status.ready_replicas or 0)
        if ready < 1:
            return False, f'ready_replicas={ready}'
        return True, 'ready'
    except Exception as exc:  # noqa: BLE001 — probe infra must never 500 accept
        logger.warning(
            'HIVE-1276 deliverability probe unavailable claw=%s: %s',
            getattr(claw, 'id', ''), exc,
        )
        return True, f'probe_unavailable_fail_open:{type(exc).__name__}'


def _connect_delivery_failure_text(stable_id, error=None) -> str:
    """HIVE-1276 D2: user-visible terminal failure copy.

    Mirrors Connect's ``hive_agent_delivery_error_text`` so owners see one
    prompt failure instead of a silent accepted message when Hive cannot
    hand the DM to the personal runtime bridge.
    """
    sid = str(stable_id or 'this Hive agent').strip() or 'this Hive agent'
    detail = str(error or '').strip()
    detail_l = detail.lower()
    if 'stop' in detail_l:
        return (
            f'Delivery failed: Hive agent {sid} is currently stopped. '
            'Start it in Hive and try again.'
        )
    if detail:
        short = detail if len(detail) <= 180 else f'{detail[:177]}...'
        return (
            f'Delivery failed: Hive agent {sid} could not accept this turn '
            f'({short}). Check Hive runtime status and try again.'
        )
    return (
        f'Delivery failed: Hive agent {sid} is currently unavailable. '
        'Check Hive runtime status and try again.'
    )


def _post_connect_delivery_failure(
    conversation_id, stable_id, *, agent_id, reply_to_id, error=None,
):
    """HIVE-1276 D2: post one terminal failure into the Connect conversation.

    Used when Hive cannot forward to the runtime bridge (unreachable, auth,
    reject). The runtime bridge owns terminal outcomes for turns it accepted;
    this covers the pre-bridge failure leg so accepted work never stays silent.
    Idempotent via ``client_message_id = hive-con1276-fail-<message_id>``.
    """
    url = (
        f'{_connect_internal_base_url()}/connect/api/internal/'
        f'conversations/{conversation_id}/agent-message/'
    )
    payload = {
        'agent_id': str(agent_id),
        'agent_user_id': _hive_agent_user_id(stable_id),
        'content': _connect_delivery_failure_text(stable_id, error),
        'reply_to_id': reply_to_id,
        # Distinct from any success-path key the runtime may later use.
        'client_message_id': f'hive-con1276-fail-{reply_to_id}',
    }
    resp = requests.post(
        url, json=payload,
        # Connect's internal agent-message contract allowlists "shizuha-agent".
        headers={
            'Content-Type': 'application/json',
            'X-Internal-Service': 'shizuha-agent',
        },
        timeout=10,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f'connect terminal-failure post rejected: {resp.status_code} '
            f'{resp.text[:200]}'
        )
    return resp


def _process_connect_delivery(
        claw, stable_id, conversation_id, message_id, content, dedupe_key=None):
    """Background worker: forward the message to the owner's personal OpenClaw/
    Hermes runtime bridge, which generates the reply and posts it back to Connect.

    HIVE-285/HIVE-610: the runtime bridge (``/v1/connect/messages`` on the claw's
    k8s Service) runs the owner's personal agent with its own session, tools,
    memory, and model — not a generic Hive→Cortex shortcut. The bridge owns
    reply idempotency via its own dedupe key for turns it accepts (202).

    HIVE-1276 D2: when the bridge is unreachable/rejects, Hive posts exactly one
    user-visible terminal failure into the conversation and keeps the dedupe key
    so retries cannot produce error-plus-late-reply duplication. On bridge HTTP
    accept the runtime owns the eventual user-visible reply-or-failure; Hive
    records ``bridge_accepted`` (not ``reply_posted``) so metrics do not claim a
    user-visible reply that has not happened yet.

    HIVE-360 fail-loud: the Hive-side outcome (bridge_accepted / reply_failed with
    the failing STAGE) is recorded so the metrics endpoint + the reply_failed
    PrometheusRule surface a persistent failure to the owning team (not log-only)."""
    from . import connect_delivery_metrics as _metrics
    stage = 'runtime_bridge'  # which stage we're in, for fail-loud attribution
    try:
        # HIVE-1957: SCLI claws receive DMs via the runtime's WebSocket
        # ConnectClient (Connect broadcasts the DM to the owner's agent WS), NOT
        # the HTTP /v1/connect/messages bridge — the SCLI runtime does not serve
        # that endpoint. Delivery for SCLI claws routes through the WS
        # ConnectClient path; the claw's ConnectChannel owns the reply. We
        # acknowledge here so the conversation is not double-delivered.
        if getattr(claw, 'agent_type', Claw.AgentType.CLAW) == Claw.AgentType.SCLI:
            logger.info(
                'HIVE-1957 connect delivery routed via WS ConnectClient '
                'stable_id=%s conversation=%s message=%s (SCLI claw has no '
                '/v1/connect/messages bridge; runtime WS owns the reply)',
                stable_id, conversation_id, message_id,
            )
            _metrics.record('reply_posted')
            return
        bridge_url = _claw_runtime_bridge_url(claw)
        token = _claw_delivery_token(claw)
        payload = {
            'stable_id': stable_id,
            'conversation_id': conversation_id,
            'message_id': message_id,
            'content': content,
        }
        resp = requests.post(
            bridge_url, json=payload,
            headers={
                'Content-Type': 'application/json',
                'X-Connect-Runtime-Secret': token,
            },
            timeout=30,
        )
        if resp.status_code >= 400:
            body = resp.text[:500]
            raise RuntimeError(
                f'runtime bridge rejected delivery: HTTP {resp.status_code} {body}')
        # Bridge accepted (typically 202). Runtime owns the eventual user-visible
        # reply or terminal failure post. Do NOT record reply_posted here — that
        # lied that a Connect reply already landed (HIVE-1276 metrics handoff).
        _metrics.record('bridge_accepted')
    except Exception as exc:
        # HIVE-360: level:error with full triage context + stage-labelled failure
        # counter. HIVE-1276 D2: also post one user-visible terminal failure so
        # the conversation does not stay silent-accepted when the bridge never
        # saw the message. Keep the dedupe key after a visible terminal so a
        # redelivery cannot produce a second failure (or a late reply after
        # failure).
        logger.error(
            'HIVE-355/360/1276 connect reply FAILED stage=%s stable_id=%s '
            'conversation=%s message=%s: %s',
            stage, stable_id, conversation_id, message_id, exc)
        _metrics.record('reply_failed', stage=stage)
        try:
            stage = 'connect_post'
            _post_connect_delivery_failure(
                conversation_id, stable_id,
                agent_id=claw.id,
                reply_to_id=message_id,
                error=str(exc),
            )
        except Exception as post_exc:
            # Terminal post itself failed — clear dedupe so a later redelivery
            # can still attempt to surface something to the owner.
            logger.error(
                'HIVE-1276 connect terminal-failure post FAILED stable_id=%s '
                'conversation=%s message=%s: %s',
                stable_id, conversation_id, message_id, post_exc,
            )
            _metrics.record('reply_failed', stage=stage)
            if dedupe_key:
                cache.delete(dedupe_key)
            else:
                cache.delete(f'hive:con355:delivered:{message_id}')
    finally:
        # HIVE-614: a clean terminal path removes the durable Redis pending
        # member. A hard worker/process death skips this finally block, leaving
        # the member for the pending-age Prometheus alert to detect.
        _metrics.finish_pending(message_id)


HIVE613_CONNECT_VERIFIER_CONTRACT = 'signed-owner-bound-v1'


class InternalConnectMessageView(APIView):
    """POST /v1/internal/connect/messages/ — Connect DM → personal-agent delivery.

    HIVE-355: resolve the target Claw from ``stable_id``, ack immediately (202),
    and produce + post the assistant reply back to Connect asynchronously (cortex
    reply-gen exceeds Connect's 5s handoff timeout).

    HIVE-613: Authenticated via HMAC-SHA256 signature (``X-Hive-Signature-256``
    header) using a shared secret between Connect and Hive. The signature covers
    the request body + timestamp to prevent replay. The target claw is verified
    to belong to the correct owner/org before processing.
    """
    authentication_classes = []
    permission_classes = []

    def _verify_hmac(self, request) -> bool:
        """Verify HMAC-SHA256 signature on the request.

        The signature is computed over ``timestamp||body`` where ``||`` is
        concatenation. The timestamp is taken from the ``X-Hive-Timestamp`` header
        and must be within 30s of the server's clock (replay window).
        """
        secret = getattr(settings, 'HIVE_CONNECT_HMAC_SECRET', '')
        if not secret:
            # Fail closed when no secret is configured.
            return False

        signature = request.META.get('HTTP_X_HIVE_SIGNATURE_256', '')
        timestamp = request.META.get('HTTP_X_HIVE_TIMESTAMP', '')
        if not signature or not timestamp:
            return False

        # Replay window: 30 seconds
        try:
            ts = int(timestamp)
            now = int(time.time())
            if abs(now - ts) > 30:
                return False
        except (ValueError, TypeError):
            return False

        body = request.body
        if isinstance(body, bytes):
            body = body.decode('utf-8', errors='replace')

        expected = hmac.new(
            secret.encode('utf-8'),
            f'{timestamp}||{body}'.encode('utf-8'),
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(expected, signature)

    @staticmethod
    def _connect_delivery_probe_marker(request):
        """PLAT-7972: attribute HIVE-725 ring-release harness negative probes.

        The harness sends intentional negative probes carrying `release_probe=`
        / `cutover_probe=` query params (unsigned -> 401, tenant-mismatched
        signed -> 403). Attributing them routes the outcomes to dedicated
        probe series (probe="release"/"cutover") so the security alerting
        series (probe="") stays free of rollout noise while the events remain
        visible in metrics; real delivery traffic is unaffected.
        """
        params = request.query_params
        if 'release_probe' in params:
            return 'release'
        if 'cutover_probe' in params:
            return 'cutover'
        return ''

    def post(self, request):
        from . import connect_delivery_metrics as _metrics
        probe = self._connect_delivery_probe_marker(request)

        try:
            declared_length = int(request.META.get('CONTENT_LENGTH') or 0)
        except (TypeError, ValueError):
            declared_length = 0
        if declared_length > _CONNECT_DELIVERY_MAX_BODY_BYTES:
            _metrics.record('invalid', probe=probe)
            _connect_delivery_audit('body_too_large')
            return Response(
                {'status': 'body_too_large'}, status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

        # HIVE-613: HMAC authentication — fail closed if signature is missing/invalid.
        if not self._verify_hmac(request):
            _metrics.record('auth_failed', probe=probe)
            _connect_delivery_audit('auth_failed')
            return Response(
                {'status': 'auth_failed', 'error': 'Invalid or missing HMAC signature'},
                status=status.HTTP_401_UNAUTHORIZED)
        if len(request.body) > _CONNECT_DELIVERY_MAX_BODY_BYTES:
            _metrics.record('invalid', probe=probe)
            _connect_delivery_audit('body_too_large')
            return Response(
                {'status': 'body_too_large'}, status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

        data = request.data if isinstance(request.data, dict) else {}
        stable_id = str(data.get('stable_id') or '').strip()
        conversation_id = str(data.get('conversation_id') or '').strip()
        message_id = str(data.get('message_id') or '').strip()
        content = (data.get('content') or '').strip()
        # HIVE-613: owner/org binding — Connect must supply the expected owner
        # so Hive can verify the target claw belongs to the right owner.
        expected_owner_id = str(data.get('owner_id') or '').strip()
        try:
            expected_organization_id = int(data.get('organization_id'))
            if expected_organization_id <= 0:
                expected_organization_id = None
        except (TypeError, ValueError):
            expected_organization_id = None
        expected_agent_participant_id = str(
            data.get('agent_participant_id') or '').strip()

        if not stable_id or not conversation_id or not message_id:
            _metrics.record('binding_required', probe=probe)
            _connect_delivery_audit(
                'binding_required', owner_id=expected_owner_id,
                organization_id=expected_organization_id, stable_id=stable_id,
                conversation_id=conversation_id, message_id=message_id,
            )
            return Response(
                {'status': 'ignored',
                 'error': 'stable_id, conversation_id and message_id are required'},
                status=status.HTTP_400_BAD_REQUEST)

        claw = _claw_from_stable_id(stable_id)
        if claw is None:
            logger.warning('HIVE-355 delivery for unknown agent stable_id=%s', stable_id)
            _metrics.record('unknown_agent', probe=probe)
            _connect_delivery_audit(
                'unknown_agent', owner_id=expected_owner_id,
                organization_id=expected_organization_id, stable_id=stable_id,
                conversation_id=conversation_id, message_id=message_id,
            )
            return Response(
                {'status': 'unknown_agent', 'stable_id': stable_id},
                status=status.HTTP_404_NOT_FOUND)

        # HIVE-613: verify owner binding — the caller MUST supply the expected
        # owner_id and it MUST match the claw's owner. This prevents cross-owner
        # delivery even with a valid HMAC (the HMAC secret is shared, but the
        # owner binding is per-request).
        if not expected_owner_id:
            logger.warning(
                'HIVE-613 owner_id missing: stable_id=%s', stable_id,
            )
            _metrics.record('binding_required', probe=probe)
            _connect_delivery_audit(
                'owner_required', organization_id=expected_organization_id,
                stable_id=stable_id, conversation_id=conversation_id,
                message_id=message_id,
            )
            return Response(
                {'status': 'owner_required', 'stable_id': stable_id},
                status=status.HTTP_400_BAD_REQUEST)
        if str(claw.owner_id) != expected_owner_id:
            logger.warning(
                'HIVE-613 owner mismatch: stable_id=%s claw_owner=%s expected_owner=%s',
                stable_id, claw.owner_id, expected_owner_id,
            )
            _metrics.record('owner_mismatch', claw_owner=claw.owner_id, probe=probe)
            _connect_delivery_audit(
                'owner_mismatch', owner_id=expected_owner_id,
                organization_id=expected_organization_id, stable_id=stable_id,
                conversation_id=conversation_id, message_id=message_id,
            )
            return Response(
                {'status': 'owner_mismatch', 'stable_id': stable_id},
                status=status.HTTP_403_FORBIDDEN)

        # The caller signs the organization assertion, but Hive independently
        # resolves the personal Claw's canonical organization from Admin and
        # requires an exact match.  A merely non-empty caller list is not an
        # authorization decision.
        authoritative_organization_id = _owner_personal_organization_id(claw.owner_id)
        if expected_organization_id is None:
            logger.warning(
                'HIVE-613 organization_id missing: stable_id=%s conversation=%s',
                stable_id, conversation_id,
            )
            _metrics.record('binding_required', probe=probe)
            _connect_delivery_audit(
                'organization_scope_required', owner_id=expected_owner_id,
                stable_id=stable_id, conversation_id=conversation_id,
                message_id=message_id,
            )
            return Response(
                {'status': 'organization_scope_required'},
                status=status.HTTP_400_BAD_REQUEST)
        if (authoritative_organization_id is None or
                expected_organization_id != authoritative_organization_id):
            logger.warning(
                'HIVE-613 organization mismatch: stable_id=%s expected=%s authoritative=%s',
                stable_id, expected_organization_id, authoritative_organization_id,
            )
            _metrics.record('organization_mismatch', claw_owner=claw.owner_id, probe=probe)
            _connect_delivery_audit(
                'organization_mismatch', owner_id=expected_owner_id,
                organization_id=expected_organization_id, stable_id=stable_id,
                conversation_id=conversation_id, message_id=message_id,
            )
            return Response(
                {'status': 'organization_mismatch'},
                status=status.HTTP_403_FORBIDDEN)

        # Connect derives this participant identity from stable_id when it
        # creates the conversation. Requiring the signed value and recomputing
        # it here binds delivery to that exact agent participant instead of only
        # trusting caller-supplied conversation/message UUIDs.
        actual_agent_participant_id = str(_hive_agent_user_id(stable_id))
        if expected_agent_participant_id != actual_agent_participant_id:
            logger.warning(
                'HIVE-613 agent participant mismatch: stable_id=%s expected=%s actual=%s',
                stable_id, expected_agent_participant_id, actual_agent_participant_id,
            )
            _metrics.record('participant_mismatch', claw_owner=claw.owner_id, probe=probe)
            _connect_delivery_audit(
                'participant_mismatch', owner_id=expected_owner_id,
                organization_id=expected_organization_id, stable_id=stable_id,
                conversation_id=conversation_id, message_id=message_id,
            )
            return Response(
                {'status': 'participant_mismatch'},
                status=status.HTTP_403_FORBIDDEN)

        if not content:
            _metrics.record('ignored', probe=probe)
            return Response(
                {'status': 'ignored', 'reason': 'empty content', 'stable_id': stable_id},
                status=status.HTTP_200_OK)
        if len(content) > _CONNECT_DELIVERY_MAX_CONTENT_CHARS:
            _metrics.record('invalid', probe=probe)
            _connect_delivery_audit(
                'content_too_large', owner_id=expected_owner_id,
                organization_id=expected_organization_id, stable_id=stable_id,
                conversation_id=conversation_id, message_id=message_id,
            )
            return Response(
                {'status': 'content_too_large'},
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

        # Atomically claim the authenticated tuple and charge its new-work
        # budget. An overlapping retry must see the reservation and return a
        # duplicate acknowledgement; it must never race the admitted request
        # into a 429 and create a second visible Connect terminal.
        dedupe_tuple = (
            f'{expected_organization_id}\0{expected_owner_id}\0{claw.id}'
            f'\0{conversation_id}\0{message_id}'
        )
        dedupe_digest = hashlib.sha256(dedupe_tuple.encode('utf-8')).hexdigest()
        dedupe_key = f'hive:con355:delivered:{dedupe_digest}'
        reservation = _reserve_connect_delivery(
            expected_owner_id, claw.id, dedupe_key)
        if reservation == 'duplicate':
            return _connect_delivery_duplicate_response(
                owner_id=expected_owner_id,
                organization_id=expected_organization_id,
                stable_id=stable_id,
                conversation_id=conversation_id,
                message_id=message_id,
            )

        if reservation == 'rate_limited':
            _metrics.record('rate_limited', probe=probe)
            _connect_delivery_audit(
                'rate_limited', owner_id=expected_owner_id,
                organization_id=expected_organization_id, stable_id=stable_id,
                conversation_id=conversation_id, message_id=message_id,
            )
            return Response(
                {'status': 'rate_limited'}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        if reservation != 'reserved':
            _metrics.record('reply_failed', stage='reservation_store', probe=probe)
            return Response(
                {'status': 'unavailable', 'error': 'delivery state unavailable'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        # Non-running agent → a visible status instead of a silent no-response.
        # Release the dedupe reservation so a later start+retry can redeliver.
        if getattr(claw, 'status', None) != Claw.Status.RUNNING:
            logger.info('HIVE-355 delivery to non-running agent stable_id=%s status=%s',
                        stable_id, getattr(claw, 'status', None))
            cache.delete(dedupe_key)
            _metrics.record('agent_not_running', probe=probe)
            return Response(
                {'status': 'agent_not_running', 'stable_id': stable_id,
                 'claw_status': getattr(claw, 'status', None)},
                status=status.HTTP_202_ACCEPTED)

        # HIVE-1276 D1: status=running is desired-state, not deliverability.
        # Probe the claw Deployment for ≥1 ready replica before accepting async
        # work. Connect already terminals agent_not_running synchronously
        # (record_hive_agent_delivery_error) — reuse that shape so the owner
        # sees one prompt failure instead of silent accepted.
        deliverable, deliver_reason = _claw_runtime_deliverable(claw)
        if not deliverable:
            logger.info(
                'HIVE-1276 delivery to undeliverable running agent '
                'stable_id=%s claw=%s reason=%s',
                stable_id, getattr(claw, 'id', ''), deliver_reason,
            )
            cache.delete(dedupe_key)
            _metrics.record('agent_not_running', probe=probe)
            return Response(
                {'status': 'agent_not_running', 'stable_id': stable_id,
                 'claw_status': getattr(claw, 'status', None),
                 'reason': deliver_reason},
                status=status.HTTP_202_ACCEPTED)

        # Persist the accepted→terminal state before starting the daemon thread.
        # Redis outlives this worker, so a process crash remains observable.
        if not _metrics.mark_pending(message_id):
            cache.delete(dedupe_key)
            _metrics.record('reply_failed', stage='pending_store', probe=probe)
            logger.error(
                'HIVE-614 refused Connect delivery without durable pending record '
                'stable_id=%s conversation=%s message=%s',
                stable_id, conversation_id, message_id,
            )
            return Response(
                {'status': 'unavailable', 'error': 'delivery state unavailable'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        try:
            cache.set(
                dedupe_key, 'accepted', timeout=_CONNECT_DELIVERY_DEDUPE_TTL)
        except Exception as exc:
            # The atomic reservation already exists. A cosmetic state-label
            # update must not strand the durable pending record without its
            # dispatch thread.
            logger.warning(
                'HIVE-613 reservation accepted-state update failed: %s', exc)
        try:
            threading.Thread(
                target=_process_connect_delivery,
                args=(claw, stable_id, conversation_id, message_id, content, dedupe_key),
                daemon=True,
            ).start()
        except Exception as exc:
            _metrics.finish_pending(message_id)
            cache.delete(dedupe_key)
            _metrics.record('reply_failed', stage='thread_start', probe=probe)
            logger.error(
                'HIVE-614 Connect delivery worker start FAILED stable_id=%s '
                'conversation=%s message=%s: %s',
                stable_id, conversation_id, message_id, exc,
            )
            return Response(
                {'status': 'unavailable', 'error': 'delivery worker unavailable'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        _metrics.record('accepted', probe=probe)
        _connect_delivery_audit(
            'accepted', owner_id=expected_owner_id,
            organization_id=expected_organization_id, stable_id=stable_id,
            conversation_id=conversation_id, message_id=message_id,
        )

        return Response(
            {'status': 'accepted', 'stable_id': stable_id, 'claw_id': str(claw.id),
             'message_id': message_id},
            status=status.HTTP_202_ACCEPTED)


class InternalConnectDeliveryMetricsView(APIView):
    """GET /v1/internal/connect/delivery/metrics — HIVE-360 liveness/invariant
    observability for the HIVE-355 Connect delivery path (Prometheus text). Each
    inbound records exactly one terminal outcome; ``reply_failed`` is the fail-loud
    counter the PrometheusRule alerts on. HIVE-614 fails closed on the shared
    internal-service token; the PodMonitor supplies it as a Bearer credential."""
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        expected = str(getattr(settings, 'HIVE_INTERNAL_AUTH_TOKEN', '') or '')
        authorization = str(request.META.get('HTTP_AUTHORIZATION', '') or '')
        supplied = ''
        if authorization.lower().startswith('bearer '):
            supplied = authorization[7:].strip()
        if not supplied:
            supplied = str(request.META.get('HTTP_X_HIVE_INTERNAL_TOKEN', '') or '')
        if not expected or not supplied or not hmac.compare_digest(supplied, expected):
            return Response(
                {'error': 'Internal service authentication required'},
                status=status.HTTP_403_FORBIDDEN,
            )
        from . import connect_delivery_metrics as _metrics
        from fleet import grok_provider_metrics, runtime_contract_metrics
        return HttpResponse(
            _metrics.to_prometheus()
            + runtime_contract_metrics.to_prometheus()
            + grok_provider_metrics.to_prometheus(),
            content_type='text/plain; version=0.0.4; charset=utf-8',
        )


class InternalUsageMeteringMetricsView(APIView):
    """GET /v1/internal/usage-metering/metrics — HIVE-408 fail-loud observability
    for the HIVE-345/HIVE-319 billing usage-metering paths (Prometheus text).
    ``hive_usage_metering_failure_total{source=…}`` counts missed/at-risk billing
    events; the PrometheusRule (k8s/hive-usage-metering-alerts.yaml) alerts on any
    increase, routed to the billing/analytics owner (PLAT-1254)."""
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        from . import usage_metering_metrics as _metrics
        return HttpResponse(
            _metrics.to_prometheus(),
            content_type='text/plain; version=0.0.4; charset=utf-8',
        )


class InternalCodexRefreshMetricsView(APIView):
    """GET /v1/internal/codex-refresh/metrics — HIVE-585 fail-loud observability
    for revoked OpenAI/Codex refresh tokens. The PrometheusRule alerts on
    ``hive_codex_refresh_401_total`` increases and carries a Hive re-auth link."""
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        from . import codex_refresh_metrics as _metrics
        return HttpResponse(
            _metrics.to_prometheus(),
            content_type='text/plain; version=0.0.4; charset=utf-8',
        )


class InternalClawIdleMetricsView(APIView):
    """GET /v1/internal/claw-idle/metrics — HIVE-2011 fail-loud observability
    for the Claw idle-recycle worker (ADR-0080 stale-Running guard).

    ``hive_claw_idle_running{claw_id,namespace}`` is 1 for every Running Claw
    past its idle TTL that the scheduled reconcile could not stop — the exact
    "scale-to-zero silently failed for months" failure mode. The
    ``HiveClawIdleRunning`` PrometheusRule alerts on it, and
    ``hive_claw_idle_stop_failure_total`` drives ``HiveClawIdleStopFailed``."""
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        from . import idle_recycle_metrics as _metrics
        return HttpResponse(
            _metrics.to_prometheus(),
            content_type='text/plain; version=0.0.4; charset=utf-8',
        )


class InternalWhatsAppBindView(APIView):
    """POST /v1/internal/wa/bind (HIVE-201 E1) — bind a whatsmeow-authenticated
    ``phone_jid`` to a claw for a pending ``LINK`` code.

    Called by Ori/daemon when a ``LINK-<code>`` message arrives. Service-
    authenticated via ``X-Hive-Internal-Token`` (shared ``HIVE_INTERNAL_AUTH_TOKEN``;
    the claw isn't known until the code is resolved, so this is service-scoped,
    not per-claw). Body: ``{code, phone_jid}``. Returns
    ``{status: linked|already|expired|invalid, claw_id?}`` (fail-closed).
    """

    authentication_classes = []
    permission_classes = []

    def post(self, request):
        return Response(
            {
                'error': 'Deprecated WhatsApp LINK-code binding path',
                "detail": "HIVE-201 uses each Claw's native OpenClaw WhatsApp pairing surface; Ori LINK-code binding is intentionally disabled.",
                'mode': 'claw_native_pairing',
            },
            status=status.HTTP_410_GONE,
        )


class InternalChannelTokenVerifyView(APIView):
    """POST /v1/internal/claws/<uuid:claw_id>/verify-channel-token (HIVE-68/HIVE-60 M1).

    OpenClaw calls this on WebView load with {token} in the request body. Verifies
    HMAC, TTL, claw ownership, and single-use JTI; returns {valid, owner_id, claw_id}
    or 401 on any failure (fail-closed per HLD §4).
    """
    authentication_classes = []
    permission_classes = []

    def post(self, request, claw_id):
        claw = _get_internal_claw(request, claw_id)
        if isinstance(claw, Response):
            return claw
        token = request.data.get('token', '')
        if not token:
            return Response({'error': 'token required'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            owner_id, verified_claw_id = _verify_channel_token(token, claw_id)
        except ValueError as exc:
            logger.warning('channel token verification failed for claw %s: %s', claw_id, exc)
            return Response({'error': 'invalid token'}, status=status.HTTP_401_UNAUTHORIZED)
        return Response({'valid': True, 'owner_id': owner_id, 'claw_id': verified_claw_id})


class InternalHoldStateView(APIView):
    """Pod-accessible hold-state endpoint with owner-safe legal metadata.

    This uses the same per-claw credential as OpenClaw auth-state, so it must
    not expose internal legal case details. Admin/internal worker routes that
    need full legal metadata should use admin hold-state endpoints until HIVE-7
    introduces a separate worker-only credential.
    """
    authentication_classes = []
    permission_classes = []

    def get(self, request, claw_id):
        claw = _get_internal_claw(request, claw_id)
        if isinstance(claw, Response):
            return claw
        return Response(lifecycle.owner_hold_state(claw))


class AdminClawExportView(OwnerClawExportView):
    """Admin export route; same queue as owner export, no owner step-up."""

    def _admin_or_response(self, request, claw_id):
        return _get_admin_claw(request, claw_id)

    def get(self, request, claw_id, job_id=None):
        claw = self._admin_or_response(request, claw_id)
        if isinstance(claw, Response):
            return claw
        jobs = claw.export_jobs.all()
        if job_id is not None:
            parsed_job_id = parse_export_job_id(job_id)
            if parsed_job_id is None:
                return Response({'error': 'Export job not found'}, status=status.HTTP_404_NOT_FOUND)
            job = jobs.filter(id=parsed_job_id).first()
            if not job:
                return Response({'error': 'Export job not found'}, status=status.HTTP_404_NOT_FOUND)
            return Response(ClawExportJobSerializer(job).data)
        return Response(ClawExportJobSerializer(jobs[:50], many=True).data)

    def post(self, request, claw_id):
        claw = self._admin_or_response(request, claw_id)
        if isinstance(claw, Response):
            return claw
        serializer = ClawExportRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        job = ClawExportJob.objects.create(
            claw=claw,
            owner_id=claw.owner_id,
            requested_by=ClawExportJob.RequestedBy.OPERATOR,
            requested_by_actor=_actor(request),
            include_session=data['include_session'],
            include_credits=data['include_credits'],
            snapshot_name=export_snapshot_name(claw.id),
        )
        job = complete_export_job(job, request=request)
        return Response(ClawExportJobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


class HealthView(APIView):
    """Liveness/readiness health check — no auth required, O(1), and it must
    NEVER gate on tenant state. Kubelet readiness+liveness probes point here
    with a 1s timeout (HIVE-342): coupling this endpoint to the claw isolation
    audit let a single drifted tenant namespace (or audit latency alone) take
    every hive-api pod NotReady. The HIVE-326 drift signal lives on
    IsolationHealthView (/api/health/isolation/), which only the synthetic
    monitor consumes.
    """
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        return Response({'status': 'ok', 'service': 'shizuha-hive'})


class IsolationHealthView(APIView):
    """HIVE-326 claw namespace-isolation drift signal, decoupled per HIVE-342.

    Monitoring-only endpoint — never wire kubelet probes here. Returns 503
    while any active claw namespace is missing an isolation control, so the
    blackbox Probe + Alertmanager route fails loud to the owning team without
    coupling tenant drift to pod lifecycle. The underlying audit costs ~5
    sequential k8s API calls per active claw, so results are cached for
    HIVE_ISOLATION_HEALTH_TTL seconds (default 60) instead of recomputing per
    hit (HIVE-342 finding 2).
    """
    authentication_classes = []
    permission_classes = []

    CACHE_KEY = 'claw_isolation_health_summary'

    def get(self, request):
        ttl = int(os.environ.get('HIVE_ISOLATION_HEALTH_TTL', '60'))
        isolation = cache.get(self.CACHE_KEY)
        if isolation is None:
            isolation = provisioner.summarize_claw_namespace_isolation_health()
            cache.set(self.CACHE_KEY, isolation, ttl)
        ok = isolation.get('ok') is True
        payload = {
            'status': 'ok' if ok else 'degraded',
            'service': 'shizuha-hive',
            'cache_ttl_seconds': ttl,
            'claw_namespace_isolation': isolation,
        }
        return Response(payload, status=status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE)


# ── Codex device-code auth (HIVE-581) ──────────────────────────────────────
# Staff-gated browser path replacing host-shell `shizuha auth codex`: mint a
# fresh fleet Codex refresh token into the platform owner's personal pool. The server is
# stateless across start/poll (the client holds device_auth_id + user_code),
# so any Hive replica can serve either call.

class CodexDeviceAuthStartView(APIView):
    """POST → begin the OpenAI device-code flow (staff only)."""

    def post(self, request):
        if not getattr(request.user, 'is_staff', False):
            return Response({'error': 'Admin privileges required'}, status=status.HTTP_403_FORBIDDEN)
        from . import codex_device_auth
        try:
            data = codex_device_auth.request_device_code()
        except codex_device_auth.CodexDeviceAuthError as exc:
            retryable = isinstance(exc, codex_device_auth.CodexDeviceAuthTransientError)
            logger.warning(
                '[codex-device-auth] stage=start outcome=failed actor=%s retryable=%s error=%s',
                _actor(request), retryable, exc,
            )
            return Response(
                {'error': str(exc), 'retryable': retryable},
                status=(
                    status.HTTP_503_SERVICE_UNAVAILABLE
                    if retryable else status.HTTP_502_BAD_GATEWAY
                ),
            )
        logger.info(
            '[codex-device-auth] stage=start outcome=created actor=%s interval=%s expires_in=%s',
            _actor(request), data.get('interval'), data.get('expires_in'),
        )
        return Response(data, status=status.HTTP_201_CREATED)


class CodexDeviceAuthPollView(APIView):
    """POST {device_auth_id, user_code} → poll once; on authorization,
    exchange + ingest the account into the personal OpenAI/Codex pool (staff only)."""

    def post(self, request):
        if not getattr(request.user, 'is_staff', False):
            return Response({'error': 'Admin privileges required'}, status=status.HTTP_403_FORBIDDEN)
        from . import codex_device_auth
        body = request.data or {}
        device_auth_id = str(body.get('device_auth_id') or '')
        user_code = str(body.get('user_code') or '')
        if not device_auth_id or not user_code:
            return Response({'error': 'device_auth_id and user_code are required'},
                            status=status.HTTP_400_BAD_REQUEST)
        target_pool_id = str(body.get('target_pool_id') or '').strip()
        target_entry_id = str(body.get('target_entry_id') or '').strip()
        if bool(target_pool_id) != bool(target_entry_id):
            return Response(
                {'error': 'target_pool_id and target_entry_id must be provided together'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        target_entry = None
        target_info = {}
        if target_pool_id and target_entry_id:
            pool, result = _get_owned_token_entry(request, target_pool_id, target_entry_id)
            if isinstance(pool, Response):
                return pool
            if isinstance(result, Response):
                return result
            if pool.provider != TokenPool.Provider.OPENAI:
                return Response(
                    {'error': 'Selected token entry is not an OpenAI credential'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            target_entry = result
            from .codex_refresh_metrics import entry_observability
            target_info = entry_observability(target_entry)
        try:
            polled = codex_device_auth.poll_device_token(device_auth_id, user_code)
            if polled.get('status') != 'authorized':
                return Response(polled)
            tokens = codex_device_auth.exchange_authorization_code(
                polled['authorization_code'], polled.get('code_verifier', ''))
            ingested = codex_device_auth.ingest_codex_account(
                tokens,
                actor=str(getattr(request.user, 'email', '') or request.user.id),
                target_entry=target_entry,
                expected_email=str(target_info.get('email') or ''),
                expected_account_id=str(target_info.get('account_id') or ''),
            )
        except codex_device_auth.CodexDeviceAuthTargetMismatch as exc:
            logger.warning(
                '[codex-device-auth] stage=account_binding outcome=mismatch actor=%s '
                'target_entry=%s error=%s',
                _actor(request), target_entry_id or '-', exc,
            )
            return Response(
                {'error': str(exc), 'retryable': False},
                status=status.HTTP_409_CONFLICT,
            )
        except codex_device_auth.CodexDeviceAuthError as exc:
            retryable = isinstance(exc, codex_device_auth.CodexDeviceAuthTransientError)
            logger.warning(
                '[codex-device-auth] stage=poll_or_exchange outcome=failed actor=%s '
                'target_entry=%s retryable=%s error=%s',
                _actor(request), target_entry_id or '-', retryable, exc,
            )
            return Response(
                {'error': str(exc), 'retryable': retryable},
                status=(
                    status.HTTP_503_SERVICE_UNAVAILABLE
                    if retryable else status.HTTP_502_BAD_GATEWAY
                ),
            )
        return Response({'status': 'authorized', **ingested})


# ── xAI / Grok device-code auth (SuperGrok / Grok Build OIDC) ──────────────
# Fleet-owner browser path mirroring Codex device auth: mint SuperGrok OAuth
# material into the platform owner's personal xAI TokenPool. Staff retain
# access for operational recovery.

def _can_manage_fleet_grok_accounts(user):
    if getattr(user, 'is_staff', False):
        return True
    fleet_owner = str(
        getattr(settings, 'HIVE_FLEET_OWNER_USER_ID', '') or ''
    ).strip()
    return bool(fleet_owner) and str(getattr(user, 'id', '')) == fleet_owner


class GrokDeviceAuthStartView(APIView):
    """POST → begin xAI device auth for staff or the configured fleet owner."""

    def post(self, request):
        if not _can_manage_fleet_grok_accounts(request.user):
            return Response({'error': 'Fleet owner privileges required'}, status=status.HTTP_403_FORBIDDEN)
        from . import grok_device_auth
        try:
            data = grok_device_auth.request_device_code()
        except grok_device_auth.GrokDeviceAuthError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(data, status=status.HTTP_201_CREATED)


class GrokDeviceAuthPollView(APIView):
    """POST {device_code|device_auth_id} → poll once; on authorization ingest
    into the fleet owner's personal xAI/Grok TokenPool.

    ``available_to_agents`` controls whether the completed account is eligible
    for new coordinator leases. It defaults to true for backwards compatibility.
    """

    def post(self, request):
        if not _can_manage_fleet_grok_accounts(request.user):
            return Response({'error': 'Fleet owner privileges required'}, status=status.HTTP_403_FORBIDDEN)
        from . import grok_device_auth
        body = request.data or {}
        device_code = str(body.get('device_code') or body.get('device_auth_id') or '')
        available_value = body.get('available_to_agents', True)
        if isinstance(available_value, str):
            available_to_agents = available_value.strip().lower() not in {
                '0', 'false', 'no', 'off',
            }
        else:
            available_to_agents = bool(available_value)
        if not device_code:
            return Response(
                {'error': 'device_code (or device_auth_id) is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            polled = grok_device_auth.poll_device_token(device_code)
            if polled.get('status') != 'authorized':
                return Response(polled)
            ingested = grok_device_auth.ingest_grok_account(
                polled,
                actor=str(getattr(request.user, 'email', '') or request.user.id),
                available_to_agents=available_to_agents,
            )
        except grok_device_auth.GrokDeviceAuthError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({'status': 'authorized', **ingested})


# ── Claude / Anthropic OAuth code-paste auth ────────────────────────────────
# Staff-gated browser path mirroring Codex/Grok device auth: the UI shows the
# OFFICIAL Claude Code sign-in link, the operator pastes back the code#state
# from Anthropic's callback page, and Hive exchanges + ingests the bundle into
# the org Anthropic TokenPool (full scopes → quota bars + account identity).

class ClaudeOAuthStartView(APIView):
    """POST → mint PKCE material + the official Claude Code sign-in link (staff only)."""

    def post(self, request):
        if not getattr(request.user, 'is_staff', False):
            return Response({'error': 'Admin privileges required'}, status=status.HTTP_403_FORBIDDEN)
        from . import claude_oauth
        try:
            data = claude_oauth.start_login()
        except claude_oauth.ClaudeOAuthError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(data, status=status.HTTP_201_CREATED)


class ClaudeOAuthExchangeView(APIView):
    """POST {code[, state]} → exchange the pasted code#state, ingest the account
    into the org Anthropic pool with account email + quota snapshot (staff only)."""

    def post(self, request):
        if not getattr(request.user, 'is_staff', False):
            return Response({'error': 'Admin privileges required'}, status=status.HTTP_403_FORBIDDEN)
        from . import claude_oauth
        body = request.data or {}
        code = str(body.get('code') or '')
        state = str(body.get('state') or '')
        if not code:
            return Response({'error': 'code is required'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            tokens = claude_oauth.exchange_code(code, state)
            ingested = claude_oauth.ingest_claude_account(
                tokens, actor=str(getattr(request.user, 'email', '') or request.user.id))
        except claude_oauth.ClaudeOAuthError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({'status': 'authorized', **ingested})


class AntigravityOAuthStartView(APIView):
    """POST → mint PKCE material + the official Google Antigravity sign-in link."""

    def post(self, request):
        user = getattr(request, 'user', None)
        if not user or not getattr(user, 'is_authenticated', False):
            return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
        from . import antigravity_oauth
        try:
            data = antigravity_oauth.start_login()
        except antigravity_oauth.AntigravityOAuthError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(data, status=status.HTTP_201_CREATED)


class AntigravityOAuthExchangeView(APIView):
    """POST {code[, state]} → exchange the pasted authorization code, ingest the account
    into the user's platform Google pool with account email + quota snapshot."""

    def post(self, request):
        user = getattr(request, 'user', None)
        if not user or not getattr(user, 'is_authenticated', False):
            return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
        from . import antigravity_oauth
        body = request.data or {}
        code = str(body.get('code') or '')
        state = str(body.get('state') or '')
        if not code:
            return Response({'error': 'code is required'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            user_id = str(getattr(user, 'id', '') or getattr(user, 'pk', '') or '')
            ingested = antigravity_oauth.exchange_code(code, state, owner_id=user_id if user_id else None)
        except antigravity_oauth.AntigravityOAuthError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({'status': 'authorized', **ingested})


class AntigravityTokenImportView(APIView):
    """POST {token_json} → import a raw token bundle from agy's oauth_creds.json.

    This is the recommended path: the operator runs ``agy`` locally, signs in
    with Google (the CLI handles the loopback OAuth flow), then pastes the
    contents of ``~/.gemini/oauth_creds.json`` here.
    """

    def post(self, request):
        user = getattr(request, 'user', None)
        if not user or not getattr(user, 'is_authenticated', False):
            return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
        from . import antigravity_oauth
        body = request.data or {}
        token_json = str(body.get('token_json') or '')
        if not token_json:
            return Response({'error': 'token_json is required'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            user_id = str(getattr(user, 'id', '') or getattr(user, 'pk', '') or '')
            ingested = antigravity_oauth.import_token_json(token_json, owner_id=user_id if user_id else None)
        except antigravity_oauth.AntigravityOAuthError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'status': 'imported', **ingested})


class CodexTokenView(APIView):

    """GET /v1/codex/token — source Codex/OpenAI tokens from TokenEntry.

    Service-authenticated (X-Hive-Internal-Token). Returns the selected org
    OpenAI/Codex access lease (access_token, email/account_id, plan_type).
    The rotating refresh token and ID token remain encrypted in Hive and are
    never copied to a workload. Selection and near-expiry refresh are delegated
    to the existing Token Coordinator so device-auth ingestion (HIVE-582) and
    the HA refresh loop (HIVE-583) share one source of truth.

    This replaces the legacy host ``credentials.json`` import path (HIVE-584).
    """

    authentication_classes = []
    permission_classes = []

    def get(self, request):
        if not _service_internal_auth(request):
            return Response(
                {'error': 'Internal service authentication required'},
                status=status.HTTP_403_FORBIDDEN,
            )

        from django.conf import settings as dj_settings
        from . import token_coordinator

        # Personal-ownership model (operator 2026-07-11): the platform owner's
        # PERSONAL pool is the primary credential residence; the org pool stays
        # as an explicit fallback so this works before AND after the org→user
        # migration (migrate_org_pool_to_user).
        selection = None
        fleet_owner = str(getattr(dj_settings, 'HIVE_FLEET_OWNER_USER_ID', '') or '').strip()
        if fleet_owner:
            selection = token_coordinator.select_token_for_owner(
                fleet_owner,
                TokenPool.Provider.OPENAI,
                owner_kind=TokenPool.OwnerKind.USER,
                allow_org_fallback=False,
            )
        if selection is None:
            selection = token_coordinator.select_token_for_owner(
                TokenPool.ORG_OWNER_ID,
                TokenPool.Provider.OPENAI,
                owner_kind=TokenPool.OwnerKind.ORG,
                allow_org_fallback=False,
            )
        try:
            payload = json.loads(selection.token) if selection else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        if not payload.get('access_token'):
            return Response(
                {'error': 'No active Codex TokenEntry found'},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(payload)
