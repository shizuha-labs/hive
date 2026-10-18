"""
Fleet agent lifecycle API — HIVE-B4.

Exposes add/enable/disable/configure/switch-backend/delete as operator-scoped
REST endpoints so fleet ops are clean API calls, not host-docker daemon surgery.

All routes require is_staff (fleet is an operator surface, not per-user).

  GET    /v1/fleet/agents/                     list all agents
  POST   /v1/fleet/agents/                     add + provision a new agent
  GET    /v1/fleet/agents/{id}/                agent detail
  PATCH  /v1/fleet/agents/{id}/                configure (non-identity fields)
  DELETE /v1/fleet/agents/{id}/                deprovision + delete
  POST   /v1/fleet/agents/{id}/enable/         scale 0 → 1 (start)
  POST   /v1/fleet/agents/{id}/disable/        scale 1 → 0 (stop)
  POST   /v1/fleet/agents/{id}/switch-backend/ change container image + rolling restart
  POST   /v1/fleet/agents/{id}/claim/          claim external/BYO orphan ownership
  GET    /v1/fleet/agents/{id}/events/         audit event log
  GET    /v1/fleet/agents/{id}/status/         live k8s status
  GET    /v1/fleet/efficiency/                 per-agent work-efficiency metrics (HIVE-172)
"""
import logging
import hashlib
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone as datetime_timezone

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.utils.dateparse import parse_datetime
from django.utils import timezone
import requests
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.pagination import PageNumberPagination
from django.utils import timezone

from .activity_snapshot import merge_activity_with_snapshot
from .models import (
    AgentCapabilityOverride,
    BASE_PLATFORM_MCP_SERVERS,
    CapabilityAuditEvent,
    CapabilityDefinition,
    FleetAgent,
    FleetAgentTeamCredentialEligibility,
    FleetAgentArchive,
    FleetAgentEvent,
    DaemonRegistry,
    DaemonIdentityEvent,
    HiveCredential,
    HiveCredentialAudit,
    HiveCredentialRequest,
    LEAN_CONVERSATIONAL_TEAM_SLUGS,
    LEAN_CONVERSATIONAL_USERNAMES,
    TeamMembershipRole,
    TeamRequiredCapability,
    ensure_team_required_capability,
)
from claws.authentication import FederatedJWTAuthentication
from .serializers import (
    AgentCapabilityOverrideSerializer,
    CapabilityAuditEventSerializer,
    CapabilityDefinitionSerializer,
    FleetAgentSerializer,
    FleetAgentCreateSerializer,
    FleetAgentConfigureSerializer,
    RuntimeAgentConfigSerializer,
    SwitchBackendSerializer,
    FleetAgentEventSerializer,
    HiveCredentialSerializer,
    HiveCredentialAuditSerializer,
    HiveCredentialRequestSerializer,
    TeamRequiredCapabilitySerializer,
)
from . import provisioner
from .agent_templates import get_agent_template, search_agent_templates
from . import agent_index_cache  # PLAT-1063: bounded-TTL agents-index read model
from .card_projection import project_agent_cards
from .runtime_fleet import (
    get_runtime_agent, list_runtime_agents,
    set_runtime_agent_enabled, restart_runtime_agent, reset_runtime_agent_session,
    configure_runtime_agent, admit_runtime_lane_update, delete_runtime_agent,
    observed_runtime_lane_payload,
    process_runtime_lane_config_result,
    get_runtime_skills,
    get_platform_mcp_service_slugs,
    get_runtime_agent_activity, get_runtime_activity_rates,
)
from .lifecycle_fleet import (
    K3S_PATCH_LANE_FIELDS,
    apply_k3s_lane_after_patch,
    hibernate_lifecycle_agent,
    list_lifecycle_agents,
    set_lifecycle_agent_enabled,
)
from .capability_signing import attach_effective_capabilities_signature
from .mcp_projection import canonical_projection_envelope
from .context_budget import attach_context_budget, budgets_to_prometheus, context_budget_for_agent
from .external_agents import list_external_agents
from .identity_health import annotate_agents, routing_health_for_membership
from .org_membership_sync import ensure_roster_org_membership
from .recovery import recovery_actions_to_prometheus

logger = logging.getLogger(__name__)

# HIVE-446: missing/ambiguous customer tenant context must fail closed instead
# of taking the legacy internal/no-gating path. This sentinel has no
# TenantSubscription, so resolve_tenant_grant_allowlist() returns empty
# allowlists and the runtime payload gets tenant-scoped MCP refs instead of
# platform-global refs.
_UNKNOWN_CUSTOMER_TENANT_SCOPE = 'unknown-customer-tenant'


RUNTIME_METHOD_OPTIONS = [
    {'id': 'codex_app_server', 'label': 'Codex'},
    {'id': 'claude_code_server', 'label': 'Claude Code'},
    {'id': 'openclaw_app_server', 'label': 'OpenClaw'},
    {'id': 'grok_build', 'label': 'Grok Build'},
    {'id': 'shizuha', 'label': 'Shizuha CLI'},
]

CLAUDE_MODEL_OPTIONS = [
    # 2026-07-02: keep this catalog current when Anthropic ships a new model —
    # the daemon has no Claude-side allowlist (fable-5 ran fine via config
    # PATCH), so a stale list here means the UI silently can't select a model
    # the fleet already runs (operator hit this converting agents to Fable 5).
    # 2026-07-25: Opus 5 added. The runtime-lane registry
    # (runtime_lane_capabilities.json) is the real admission gate — a model
    # listed here but absent there is rejected as UNREGISTERED_MODEL, so add
    # new models to BOTH.
    {'id': 'claude-fable-5', 'label': 'Fable 5 (claude-fable-5)'},
    {'id': 'claude-opus-5', 'label': 'Opus 5 (claude-opus-5)'},
    {'id': 'claude-opus-4-8', 'label': 'Opus 4.8 (claude-opus-4-8)'},
    {'id': 'claude-sonnet-4-6', 'label': 'Sonnet 4.6 (claude-sonnet-4-6)'},
    {'id': 'claude-haiku-4-5', 'label': 'Haiku 4.5 (claude-haiku-4-5)'},
]

# Keep this in sync with the runtime daemon's VALID_CODEX_MODELS. Unsupported
# Codex model ids 400 at the backend and can produce silent empty turns.
CODEX_MODEL_OPTIONS = [
    {'id': 'gpt-6-astra', 'label': 'GPT-6 Astra (gpt-6-astra)'},
    {'id': 'gpt-5.6-sol', 'label': 'GPT-5.6 Sol (gpt-5.6-sol)'},
    {'id': 'gpt-5.5', 'label': 'GPT-5.5 (gpt-5.5)'},
    {'id': 'gpt-5.3-codex-spark', 'label': 'GPT-5.3 Codex Spark (gpt-5.3-codex-spark)'},
]

# Managed Cortex external offers that fleet/staff agents may use via Shizuha CLI
# or the Grok Build harness. Always merge into those catalogs: Hive's
# CORTEX_API_KEY is often a resold_customer key (e.g. whatsapp claw) that cannot
# discover internal-only offers such as xAI Grok after the fleet-only gate.
# SCLI routes these through Cortex (`cortex/` prefix stripped → offer id).
# Auth is SuperGrok/Grok Build OIDC on the Cortex managed xai-grok provider.
# grok-4.7 is the fleet pin. Cortex sends upstream grok-4.7. A grok-4.6 pin
# is rewritten to 4.7 before admission; never send either to grok-4.5.
MANAGED_FLEET_CORTEX_MODELS = [
    {
        'id': 'cortex/grok-4.7',
        'label': 'Grok 4.7 (xAI via Cortex)',
        'provider_id': 'grok-4.7',
    },
    {
        'id': 'cortex/xai/grok-4.7',
        'label': 'Grok 4.7 (xai/ alias via Cortex)',
        'provider_id': 'xai/grok-4.7',
    },
    {
        'id': 'cortex/grok-4.5',
        'label': 'Grok 4.5 (xAI via Cortex)',
        'provider_id': 'grok-4.5',
    },
    {
        'id': 'cortex/xai/grok-4.5',
        'label': 'Grok 4.5 (xai/ alias via Cortex)',
        'provider_id': 'xai/grok-4.5',
    },
    {
        'id': 'cortex/grok-voice-think-fast-2.0',
        'label': 'Grok Voice Think Fast 2.0 (SCLI realtime omni)',
        'provider_id': 'grok-voice-think-fast-2.0',
    },
    {
        'id': 'cortex/grok-voice-latest',
        'label': 'Grok Voice latest (alias → think-fast-2.0)',
        'provider_id': 'grok-voice-latest',
    },
]

# Grok Build harness models (daemon maps grok_build → gateway + Cortex SuperGrok).
GROK_BUILD_MODEL_OPTIONS = [
    {
        'id': 'cortex/grok-4.7',
        'label': 'Grok 4.7 (via Cortex SuperGrok / Grok Build OIDC)',
        'provider_id': 'grok-4.7',
        'default_reasoning_effort': 'medium',
    },
    {
        'id': 'xai:grok-4.7',
        'label': 'Grok 4.7 (direct xAI id — access-only, no refresh copy)',
        'provider_id': 'grok-4.7',
        'default_reasoning_effort': 'medium',
    },
    {
        'id': 'grok-4.7',
        'label': 'Grok 4.7 (bare offer id)',
        'provider_id': 'grok-4.7',
        'default_reasoning_effort': 'medium',
    },
    {
        'id': 'cortex/grok-4.5',
        'label': 'Grok 4.5 high (via Cortex SuperGrok / Grok Build OIDC)',
        'provider_id': 'grok-4.5',
        'default_reasoning_effort': 'high',
    },
    {
        'id': 'xai:grok-4.5',
        'label': 'Grok 4.5 (Hive xAI TokenPool lease — access-only, no refresh copy)',
        'provider_id': 'grok-4.5',
        'default_reasoning_effort': 'high',
    },
    {
        'id': 'grok-4.5',
        'label': 'Grok 4.5 (bare offer id)',
        'provider_id': 'grok-4.5',
        'default_reasoning_effort': 'high',
    },
]


class FleetAgentPagination(PageNumberPagination):
    page_size = 100
    page_size_query_param = 'page_size'
    max_page_size = 250


def _require_staff(request):
    """Return a 403 Response if the caller is not staff, else None."""
    if not getattr(request.user, 'is_staff', False):
        return Response({'error': 'Staff access required'}, status=status.HTTP_403_FORBIDDEN)
    return None


def _require_fleet_read(request):
    """Authorize the temporary fleet-wide read vantage without granting staff.

    Admin remains the grant source of truth. Hive evaluates the caller-bound
    grant at request time so expiry/revocation cannot survive in local session
    or cache state. A dependency failure is distinct from definitive denial.
    """
    if getattr(request.user, 'is_staff', False):
        return None, None
    grant, decision, reason = _active_jit_grant_for_scope(request, 'hive.fleet.read')
    if grant is not None:
        _audit_fleet_read_jit(request, result='allowed', reason='exact active Admin JIT grant', grant=grant)
        return grant, None
    outcome = 'dependency-error' if decision == 'dependency' else 'denied'
    _audit_fleet_read_jit(request, result=outcome, reason=reason)
    http_status = status.HTTP_503_SERVICE_UNAVAILABLE if decision == 'dependency' else status.HTTP_403_FORBIDDEN
    message = (
        'Admin JIT authorization unavailable'
        if decision == 'dependency'
        else 'Staff or active hive.fleet.read JIT access required'
    )
    return None, Response({'error': message}, status=http_status)


def _request_has_valid_daemon_credential(request) -> bool:
    """Authenticate rt-fleet service reads with the daemon-link credential."""
    daemon_id = (request.META.get('HTTP_X_HIVE_DAEMON_ID')
                 or request.META.get('HTTP_X_SHIZUHA_DAEMON_ID')
                 or '').strip()
    token = (request.META.get('HTTP_X_HIVE_DAEMON_TOKEN')
             or request.META.get('HTTP_X_SHIZUHA_DAEMON_LINK_TOKEN')
             or '').strip()
    if not daemon_id or not token:
        return False
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    from .models import DaemonCredential
    return DaemonCredential.objects.filter(
        daemon_id=daemon_id,
        token_hash=token_hash,
        revoked=False,
    ).exists()


def _require_staff_or_daemon_credential(request):
    if _request_has_valid_daemon_credential(request):
        return None
    return _require_staff(request)


def _agents_index_cache_suffix(request, include_external, owner_only, org_slug=''):
    """Cache key segment for the assembled agents index.

    The normal staff inventory is a shared fleet view. Owner-scoped lists must
    include the current user id even for staff/admin callers, otherwise an
    explicit viewed-account surface could reuse another owner's cached agents.
    """
    is_staff = bool(getattr(request.user, 'is_staff', False))
    has_fleet_read = bool(getattr(request, '_hive_fleet_read_grant', None))
    needs_owner_scope = owner_only or not (is_staff or has_fleet_read)
    owner_scope = f'owner{getattr(request.user, "id", "")}' if needs_owner_scope else 'fleet'
    org_scope = org_slug or 'all'
    return f'ext{int(include_external)}:ownerOnly{int(owner_only)}:{owner_scope}:org{org_scope}'


def _get_agent(agent_id):
    """Fetch a non-deleted agent by Hive PK or runtime agent_id.

    Saki-class hybrid seats are addressed by the daemon UUID
    (``FleetAgent.agent_id``) on every Agents URL. Looking up only the Django
    PK 404'd those rows, so activity/reset fell through to the wrong plane
    (or a 400) while the drawer still rendered the roster.
    """
    qs = FleetAgent.objects.exclude(status=FleetAgent.Status.DELETED)
    try:
        return qs.get(id=agent_id)
    except (FleetAgent.DoesNotExist, ValueError, ValidationError):
        pass
    try:
        return qs.get(agent_id=str(agent_id))
    except (FleetAgent.DoesNotExist, ValueError, ValidationError):
        return Response({'error': 'Agent not found'}, status=status.HTTP_404_NOT_FOUND)


def _nondb_agent(agent_id):
    """Resolve an agent id that has no native FleetAgent DB row to its live dict.
    Two non-DB planes exist and BOTH must be checked, or detail/events/status 404
    for agents the LIST view happily shows:
      1. rt-fleet daemon agents — daemon id (incl. the synthetic '0000…0001').
      2. external / BYO agents — id 'ext:<user_id>' (heartbeat-alive account_type=
         agent, surfaced by external_agents). These are NOT in the daemon, so a
         get_runtime_agent-only fallback misses them (the /hive/agents 404 on
         'ext:NN' + the <uuid:agent_id> route that couldn't even match them).
    Returns the enriched dict or None."""
    rt = get_runtime_agent(str(agent_id))
    if rt:
        from .runtime_fleet import enrich_k8s_placement
        return attach_context_budget(enrich_k8s_placement(rt))
    try:
        from .external_agents import list_external_agents
        for a in list_external_agents():
            if str(a.get('id')) == str(agent_id):
                return a  # already telemetry-enriched by external_agents
    except Exception:
        logger.exception('external-agent lookup failed (non-fatal)')
    return None


def _owned_external_agent(request, agent_id):
    """Resolve a non-DB external/k3s agent visible to this caller.

    Runtime fleet agents are an operator surface for staff. External/k3s-native
    agents are owner-scoped, even for staff accounts, so Hritik's agents view
    does not leak Harshita-owned agents.
    """
    if getattr(request.user, 'is_staff', False):
        rt = get_runtime_agent(str(agent_id))
        if rt:
            from .runtime_fleet import enrich_k8s_placement
            return attach_context_budget(enrich_k8s_placement(rt))
    owner_id = getattr(request.user, 'id', None)
    if owner_id is None:
        return None
    try:
        for agent in list_external_agents(owner_id=owner_id):
            if str(agent.get('id')) == str(agent_id):
                return agent
    except Exception:
        logger.exception('owned external-agent lookup failed (non-fatal)')
    return None


def _model_provider_for_model(model):
    model_l = (model or '').lower()
    if model_l.startswith((
        'cortex/', 'deepseek', 'glm', 'qwen', 'kimi', 'mistral', 'minimax',
        'nemotron', 'gpt-oss', 'grok-', 'xai/grok',
    )):
        return 'Cortex'
    if model_l.startswith(('anthropic/', 'claude', 'opus', 'sonnet', 'haiku')):
        return 'Anthropic'
    if model_l.startswith(('openai/', 'gpt', 'o1', 'o3', 'o4', 'codex')):
        return 'OpenAI'
    return 'custom' if model_l else 'runtime default'


def _claw_effective_model(claw):
    model = claw.model or getattr(settings, 'HIVE_CORTEX_DEFAULT_MODEL', '')
    model_l = str(model).lower()
    if model and '/' not in model_l and not model_l.startswith(('gpt', 'claude', 'opus', 'sonnet', 'haiku', 'o1', 'o3', 'o4', 'codex')):
        return f'cortex/{model}'
    return model


def _claw_agent_record(claw):
    """Project owner Claw/Hermes rows into the unified Fleet agents read model."""
    harness = 'Hermes' if claw.agent_type == 'hermes' else 'OpenClaw'
    effective_model = _claw_effective_model(claw)
    provider = _model_provider_for_model(effective_model)
    username = (claw.name or harness or 'agent').strip().lower()
    username = re.sub(r'[^a-z0-9_-]+', '-', username).strip('-') or str(claw.id)[:8]
    return {
        'kind': 'claw',
        'id': f'claw:{claw.id}',
        'claw_id': str(claw.id),
        'agent_username': username,
        'agent_id': f'claw:{claw.id}',
        'display_name': claw.name or harness,
        'role': 'Account agent',
        'model': effective_model,
        'effective_model': effective_model,
        'model_source': 'pinned' if claw.model else 'runtime_default',
        'model_provider': provider,
        'runtime_harness': harness,
        'runtime_provider': provider,
        'runtime_image_tag': '',
        'runtime_summary': ' / '.join([harness, provider, effective_model or 'runtime default']),
        'runtime_source': 'claw',
        'external_runtime': False,
        'lifecycle_managed': False,
        'execution_method': claw.agent_type,
        'enabled': claw.status == 'running',
        'resource_profile': claw.tier_id or 'default',
        'resource_profile_spec': {},
        'arch': 'k3s',
        'namespace': claw.namespace,
        'pod_name': claw.pod_name or 'claw',
        'pvc_size': claw.pvc_size,
        'storage_class': '',
        'workspace_mount_path': '',
        'sidecar_set': [],
        'credential_grant_scopes': [],
        'mcp_config': {},
        'skill_config': {},
        'context_prompt': '',
        'personality_traits': {},
        'model_fallbacks': [],
        'model_overrides': {},
        'context_budget': None,
        'reasoning_effort': '',
        'env': {},
        'failover_chain_id': '',
        'platform_url': claw.platform_url,
        'expose_http': True,
        'http_port': '',
        'status': claw.status,
        'status_message': claw.status_message,
        'created_at': claw.created_at,
        'updated_at': claw.updated_at,
        'provisioned_at': claw.provisioned_at,
        'last_active_at': claw.last_active_at,
        'owner_id': claw.owner_id,
        'owner': claw.owner_username,
        'owner_username': claw.owner_username,
        'owner_email': claw.owner_email,
        'is_account_agent': True,
        'team_names': [],
        'team_memberships': [],
        'workload': {
            'open': 0, 'in_progress': 0, 'in_review': 0,
            'blocked': 0, 'scheduled': 0, 'backlog': 0,
        },
    }


def _owner_claw_agents(owner_id):
    if owner_id is None:
        return []
    try:
        from claws.models import Claw
        qs = (
            Claw.objects
            .exclude(status=Claw.Status.DELETED)
            .filter(owner_id=owner_id)
            .order_by('agent_type', 'name')
        )
        agents = [_claw_agent_record(claw) for claw in qs]
        # HIVE-1690: attach Deployment readiness observation for claimed-running
        # claws so health_summary can refute lifecycle-only ALIVE paint.
        for agent in agents:
            if str(agent.get('status') or '').strip().lower() != 'running':
                agent['runtime_readiness'] = 'ready'  # non-running is honest without probe
                continue
            ns = str(agent.get('namespace') or '').strip()
            agent['runtime_readiness'] = _observe_claw_runtime_readiness(ns)
        return agents
    except Exception:
        logger.exception('owned claw-agent projection failed (non-fatal)')
        return []


def _owned_claw_agent(request, agent_id):
    raw = str(agent_id)
    if not raw.startswith('claw:'):
        return None
    owner_id = getattr(request.user, 'id', None)
    if owner_id is None:
        return None
    try:
        from claws.models import Claw
        claw = Claw.objects.exclude(status=Claw.Status.DELETED).get(id=raw.split(':', 1)[1])
    except Exception:
        return None
    if not getattr(request.user, 'is_staff', False) and claw.owner_id != owner_id:
        return None
    if getattr(request.user, 'is_staff', False) and claw.owner_id != owner_id:
        return None
    return _claw_agent_record(claw)


def _owned_personal_agent(request, agent_id):
    return _owned_external_agent(request, agent_id) or _owned_claw_agent(request, agent_id)


def _owned_native_agent(request, agent_id):
    """The native FleetAgent the caller OWNS (a personal agent, owner_id == caller),
    or None. HIVE-620/624: lets a user read + manage the lifecycle/activity of their
    OWN fleet agent through the same endpoints staff use, scoped to that one agent.
    """
    uid = getattr(getattr(request, 'user', None), 'id', None)
    if uid is None:
        return None
    try:
        return (FleetAgent.objects
                .exclude(status=FleetAgent.Status.DELETED)
                .filter(id=agent_id, owner_id=uid)
                .first())
    except (ValueError, TypeError, ValidationError):
        return None


def _can_manage_agent(request, agent):
    """True if the caller may read/manage ``agent``: staff, or the owner of this
    native personal agent."""
    if bool(getattr(request.user, 'is_staff', False)):
        return True
    uid = getattr(getattr(request, 'user', None), 'id', None)
    return uid is not None and getattr(agent, 'owner_id', None) == uid


def _log(agent, event_type, message):
    FleetAgentEvent.objects.create(agent=agent, event_type=event_type, message=message)


def _actor(request):
    """Staff identity for audit (rt-fleet agents have no FleetAgentEvent row)."""
    u = getattr(request, 'user', None)
    return getattr(u, 'email', None) or getattr(u, 'username', None) or 'unknown'


def _audit_capability(request, *, action, target_type, target_key, before=None, after=None, reason='', approval_ref='', outcome='', diagnostic=''):
    CapabilityAuditEvent.objects.create(
        actor=_actor(request),
        action=action,
        target_type=target_type,
        target_key=str(target_key),
        reason=reason or '',
        before=before or {},
        after=after or {},
        approval_ref=approval_ref or '',
        runtime_reconcile_outcome=outcome or '',
        diagnostic=diagnostic or '',
    )


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _slug_set(items):
    out = set()
    for item in items or []:
        if isinstance(item, dict):
            raw = item.get('slug') or item.get('id') or item.get('name')
        else:
            raw = item
        if raw:
            out.add(str(raw).strip())
    return out


def _runtime_reference_catalog():
    """Best-effort reference catalog for HIVE-273 validation."""
    skills = _slug_set(get_runtime_skills() or [])
    # HIVE-331: the MCP validity universe is the platform service registry, not the
    # set of currently-attached services — using live attachment state made validity
    # depend on runtime state the catalog itself controls (chicken-and-egg lockout
    # once a service was dropped from the last capability referencing it; PLAT-1274).
    mcp_servers = get_platform_mcp_service_slugs()
    try:
        for agent in list_runtime_agents(include_disabled=True):
            for srv in agent.get('mcp_servers') or []:
                if isinstance(srv, dict):
                    slug = srv.get('slug') or srv.get('id') or srv.get('name')
                else:
                    slug = srv
                if slug:
                    mcp_servers.add(str(slug).strip())
    except Exception:
        logger.exception('capability MCP reference catalog failed (non-fatal)')
    try:
        # Lockout guard: a slug already stored on any capability row stays re-addable
        # even if it is missing from the registry mirror and no agent has it attached
        # (e.g. restored via Django shell before the mirror caught up).
        for refs in CapabilityDefinition.objects.values_list('mcp_server_refs', flat=True):
            mcp_servers.update(str(r).strip() for r in (refs or []) if str(r).strip())
    except Exception:
        logger.exception('capability MCP DB-ref catalog failed (non-fatal)')
    profiles = set((getattr(settings, 'FLEET_RESOURCE_PROFILES', {}) or {}).keys())
    # Always allow existing FleetAgent ResourceProfile enum values; runtime profiles
    # may be settings-backed in prod but absent in unit tests.
    profiles.update(choice[0] for choice in FleetAgent.ResourceProfile.choices)
    return skills, mcp_servers, profiles


def _validate_capability_refs(data):
    skills, mcp_servers, profiles = _runtime_reference_catalog()
    errors = {}
    for field in ('skill_refs', 'eager_skill_refs'):
        bad = sorted({ref for ref in _as_list(data.get(field)) if skills and ref not in skills})
        if bad:
            errors[field] = [f'Unknown skill ref(s): {", ".join(bad)}']
    bad_mcp = sorted({ref for ref in _as_list(data.get('mcp_server_refs')) if mcp_servers and ref not in mcp_servers})
    if bad_mcp:
        errors['mcp_server_refs'] = [f'Unknown MCP server ref(s): {", ".join(bad_mcp)}']
    bad_profiles = sorted({ref for ref in _as_list(data.get('inline_profile_refs')) if profiles and ref not in profiles})
    if bad_profiles:
        errors['inline_profile_refs'] = [f'Unknown profile ref(s): {", ".join(bad_profiles)}']
    bad_requires = sorted({
        ref for ref in _as_list(data.get('requires'))
        if ref and not CapabilityDefinition.objects.filter(slug=ref).exists()
    })
    if bad_requires:
        errors['requires'] = [f'Unknown required capability slug(s): {", ".join(bad_requires)}']
    bad_conflicts = sorted({
        ref for ref in _as_list(data.get('conflicts_with'))
        if ref and not CapabilityDefinition.objects.filter(slug=ref).exists()
    })
    if bad_conflicts:
        errors['conflicts_with'] = [f'Unknown conflicting capability slug(s): {", ".join(bad_conflicts)}']
    return errors


_CAPABILITY_REVIEW_FIELDS = (
    'risk_tier',
    'skill_refs',
    'eager_skill_refs',
    'inline_profile_refs',
    'mcp_server_refs',
    'runtime_flags',
    'sensitive_access_flags',
    'default_credential_grant_scopes',
    'conflicts_with',
    'requires',
)


def _capability_payload_is_privileged(data):
    return (
        data.get('risk_tier') in {
            CapabilityDefinition.RiskTier.SENSITIVE,
            CapabilityDefinition.RiskTier.PRIVILEGED,
        }
        or bool(data.get('sensitive_access_flags') or [])
    )


def _capability_review_fields_changed(before, after):
    return any((before.get(field) or [] if field != 'runtime_flags' else before.get(field) or {}) !=
               (after.get(field) or [] if field != 'runtime_flags' else after.get(field) or {})
               for field in _CAPABILITY_REVIEW_FIELDS)


def _effective_agent_identity(agent_id):
    result = _get_agent(agent_id)
    if isinstance(result, Response):
        agent = _nondb_agent(agent_id)
        if not agent:
            return None, result
        email = _agent_email(agent)
        username = agent.get('agent_username') or agent.get('username') or str(agent_id)
        return {
            'id': str(agent_id),
            'username': username,
            'email': email,
            'role': agent.get('display_title') or agent.get('role') or '',
            'display_title': agent.get('display_title') or agent.get('role') or '',
            'source': 'runtime',
            'organization_slug': agent.get('organization_slug') or agent.get('org_slug') or '',
            'tenant_org_slug': agent.get('tenant_org_slug') or agent.get('organization_slug') or agent.get('org_slug') or '',
            'email_synthesized': bool(agent.get('email_synthesized')),
        }, None
    return {
        'id': str(result.id),
        'username': result.agent_username,
        'email': _agent_email({'email': '', 'agent_username': result.agent_username}),
        'role': result.role or '',
        'display_title': result.role or '',
        'source': 'fleetagent',
        # HIVE-2635: v1 fleet roster rows must carry POPULATED org scoping
        # fields — the previous shape omitted them entirely on fleetagent
        # rows, so the contract could not demonstrate org-scoped filtering
        # (it held by accident of single-org data). FleetAgent rows are
        # Hive-managed platform agents of the single internal org; the
        # codebase default 'shizuha' is the resolved slug until multi-org
        # fleet data exists (HIVE-2269 endpoint-shape ruling).
        'organization_slug': 'shizuha',
        'tenant_org_slug': 'shizuha',
    }, None


def _organization_team_memberships(membership_rows, *, organization_scope=None):
    """Canonical, de-duplicated ``(organization, team)`` membership rows.

    Team names are organization-local.  This helper deliberately never accepts
    the flattened capability union because ``customer-x/qa`` must not be able to
    satisfy an internal ``shizuha/qa`` credential gate.
    """
    requested_org = _canon_team_name(organization_scope)
    pairs = set()
    for membership in membership_rows or []:
        if not isinstance(membership, dict):
            continue
        organization_slug = _canon_team_name(
            membership.get('organization_slug')
            or membership.get('org_slug')
            or 'shizuha'
        )
        team_slug = _canon_team_name(
            membership.get('team_slug')
            or membership.get('team_name')
            or membership.get('slug')
        )
        if not organization_slug or not team_slug:
            continue
        if requested_org and organization_slug != requested_org:
            continue
        pairs.add((organization_slug, team_slug))
    return [
        {'organization_slug': organization_slug, 'team_slug': team_slug}
        for organization_slug, team_slug in sorted(pairs)
    ]


def _credential_materialization_contract(agent, membership_rows):
    """Return the signed, handle-only HiveCredential materialization contract.

    Team membership is necessary but intentionally insufficient. Every team
    grant is emitted only when the FleetAgent row has an active eligibility for
    that exact organization/team pair. Revoked/inactive grants or eligibilities
    disappear from the next signed payload, which is the daemon's scrub signal.
    """
    username = str((agent or {}).get('username') or '').strip().lower()
    fleet_agent = FleetAgent.objects.filter(agent_username=username).first()
    if not fleet_agent:
        return [], [], []

    membership_pairs = {
        (row['organization_slug'], row['team_slug'])
        for row in _organization_team_memberships(membership_rows)
    }
    eligible_pairs = {
        (organization_slug, team_slug)
        for organization_slug, team_slug in (
            FleetAgentTeamCredentialEligibility.objects.filter(
                agent=fleet_agent,
                is_active=True,
            ).values_list('organization_slug', 'team_slug')
        )
        if (organization_slug, team_slug) in membership_pairs
    }
    eligible_teams = {team_slug for _, team_slug in eligible_pairs}
    query = Q(scope=HiveCredential.Scope.AGENT, agent=fleet_agent)
    for organization_slug, team_slug in eligible_pairs:
        query |= Q(
            scope=HiveCredential.Scope.TEAM,
            organization_slug=organization_slug,
            team_slug=team_slug,
        )
    credentials = HiveCredential.objects.filter(query, is_active=True).order_by(
        'scope', 'organization_slug', 'team_slug', 'provider', 'purpose', 'id',
    )

    materializations = []
    for credential in credentials:
        materializations.append({
            'grant_id': str(credential.id),
            'scope': credential.scope,
            **(
                {'organization_slug': credential.organization_slug}
                if credential.scope == HiveCredential.Scope.TEAM
                else {}
            ),
            'team_slug': credential.team_slug,
            'provider': credential.provider,
            'purpose': credential.purpose,
            'secret_ref': credential.secret_ref,
            'is_active': True,
        })
    eligible_memberships = [
        {'organization_slug': organization_slug, 'team_slug': team_slug}
        for organization_slug, team_slug in sorted(eligible_pairs)
    ]
    return sorted(eligible_teams), eligible_memberships, materializations


class _OrganizationMembershipSet(set):
    """Historical team-slug set plus the org-qualified rows behind it."""

    def __init__(self, values=(), *, organization_rows=None):
        super().__init__(values)
        self.organization_rows = list(organization_rows or [])


def _cached_pulse_roster_rows_for_daemon(email):
    # HIVE-619: this is rt-fleet's hot read.  Never touch Redis while a fresh
    # process-local last-good snapshot is available: when Redis was blackholed,
    # the caller abandoned after 5s and Daphne's cancellation cleanup joined
    # the still-blocked sync worker on its event-loop thread, freezing even the
    # O(1) health route. Cold or minute-old entries recheck the replica-safe
    # shared copy; the cache backend has sub-second connect/read bounds and
    # failures keep the last-good process copy for up to the stale serve limit.
    email = str(email or '').strip().lower()
    rows, built_at = _roster_process_entry_read(email)
    source = 'process'
    stale_age = time.time() - built_at if built_at else float('inf')
    if rows is None or stale_age >= _ROSTER_PROCESS_RECHECK_AGE_S:
        shared_rows, shared_built_at = _roster_membership_cache_read(email)
        # A recovered Redis may still contain the pre-mutation value when the
        # mutation-serving replica updated its process snapshot but its shared
        # publish failed. Never roll that newer last-good process state back.
        # Ties deterministically keep local state; cold processes hydrate from
        # a valid shared snapshot without requiring a local version to compare.
        if shared_rows is not None and (rows is None or shared_built_at > built_at):
            rows, built_at = shared_rows, shared_built_at
            _roster_process_membership_write(email, rows, built_at)
            source = 'redis'
        elif rows is None:
            index, built_at = _roster_cache_read()
            rows = index.get(email, []) if index is not None else None
            source = 'redis'
    stale_age = time.time() - built_at if built_at else float('inf')
    if rows is None or stale_age >= _ROSTER_INDEX_STALE_MAX_AGE_S:
        return None, None, Response(
            {
                'error': 'Pulse roster cache unavailable for daemon capability read',
                'detail': 'A valid daemon credential was provided, but no fresh Hive roster cache is available.',
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return list(rows), {
        'source': source,
        # Signed effective-capability payloads intentionally carry no floats.
        'age_seconds': max(0, int(stale_age)),
    }, None


def _cached_pulse_roster_memberships_for_daemon(email):
    rows, snapshot, error = _cached_pulse_roster_rows_for_daemon(email)
    if error:
        return None, snapshot, error
    memberships = _OrganizationMembershipSet({
        _canon_team_name(item.get('team_slug') or item.get('team_name'))
        for item in rows
        if item.get('team_slug') or item.get('team_name')
    }, organization_rows=rows)
    return memberships, snapshot, None


def _expand_capability_requires(capabilities):
    resolved = {}
    diagnostics = []

    def visit(cap, stack=None):
        stack = stack or []
        if cap.slug in stack:
            diagnostics.append({'level': 'error', 'message': f'Capability dependency cycle: {" -> ".join(stack + [cap.slug])}'})
            return
        if cap.slug in resolved:
            return
        resolved[cap.slug] = cap
        for dep_slug in cap.requires or []:
            dep = CapabilityDefinition.objects.filter(
                slug=dep_slug,
                activation_status=CapabilityDefinition.ActivationStatus.ACTIVE,
            ).first()
            if dep:
                visit(dep, stack + [cap.slug])
            else:
                diagnostics.append({'level': 'error', 'message': f'{cap.slug} requires inactive/unknown capability {dep_slug}'})

    for cap in capabilities:
        visit(cap)
    return resolved, diagnostics


def _base_platform_mcp_servers():
    """PLAT-1251: the non-removable platform MCP floor (settings-overridable)."""
    return tuple(getattr(settings, 'HIVE_BASE_PLATFORM_MCP_SERVERS', BASE_PLATFORM_MCP_SERVERS))


def _notify_base_mcp_floor_violation(agent_email, missing):
    """PLAT-1251 fail-loud guard (PLAT-1254 standard): a capability union tried to
    drop base platform MCP servers for a fleet agent. The compute-layer floor
    already restored them on the pushed set; this routes an urgent Pulse task to
    DevOps so the offending capability rows get fixed — a log line alone is not a
    notification. Best-effort: never raises (a guard failure must not block the
    reconcile). Duplicate protection is Pulse-server-side (near-duplicate titles
    are refused), so repeated reconciles do not spam tasks.
    """
    try:
        from .provisioner import _id_admin_token  # lazy: keeps module imports acyclic

        platform_url = getattr(settings, 'SHIZUHA_PLATFORM_URL', '').rstrip('/')
        if not platform_url:
            logger.error(
                'PLAT-1251 guard: SHIZUHA_PLATFORM_URL unset; cannot notify devops '
                '(agent=%s missing=%s)', agent_email, missing,
            )
            return
        admin_token = _id_admin_token(platform_url)
        if not admin_token:
            logger.error(
                'PLAT-1251 guard: no admin token; cannot notify devops '
                '(agent=%s missing=%s)', agent_email, missing,
            )
            return
        payload = {
            'title': (
                f'[PLAT-1251 guard] capability union missing base MCP '
                f'({", ".join(missing)}) for {agent_email}'
            ),
            'description': (
                'The fleet capability reconcile computed an effective MCP union for '
                f'`{agent_email}` that was missing base platform server(s) `{missing}`.\n\n'
                'The PLAT-1251 compute-layer floor restored them on the pushed set, so the '
                'agent runtime is NOT degraded — but a capability row granted to one of this '
                "agent's teams dropped part of the base contract and must be repaired, or the "
                'floor is the only thing standing between this agent and a pulse-only runtime.\n\n'
                'Invariant: every enabled fleet agent keeps '
                f'`{sorted(_base_platform_mcp_servers())}` in enabled_mcp_servers.\n'
                'Fix: find the TeamRequiredCapability/CapabilityDefinition rows for this '
                "agent's teams whose mcp_server_refs lack the base set and re-add the refs "
                '(see hive fleet/models.py BASE_PLATFORM_MCP_SERVERS and migration 0013).'
            ),
            'priority': 'urgent',
            'workflow_name': 'simple',
            'assignment_group': 'devops',
        }
        resp = requests.post(
            f'{platform_url}/pulse/api/tasks/',
            json=payload,
            headers={'Authorization': f'Bearer {admin_token}'},
            timeout=15,
        )
        if resp.status_code not in (200, 201):
            logger.error(
                'PLAT-1251 guard: devops notification failed for %s (%d: %s)',
                agent_email, resp.status_code, resp.text[:200],
            )
    except Exception as exc:  # noqa: BLE001
        logger.error('PLAT-1251 guard: notification error for %s: %s', agent_email, exc)


def _notify_base_mcp_floor_violation_async(agent_email, missing):
    threading.Thread(
        target=_notify_base_mcp_floor_violation,
        args=(agent_email, list(missing)),
        daemon=True,
        name='plat1251-guard',
    ).start()


def _fleet_agent_for_parity(identity):
    """Best-effort FleetAgent row for the HIVE-2695 parity check.

    Mirrors the MCP-projection resolution (runtime agent_id char field, then
    username). Returns None when the seat has no native row — the parity check
    treats that as fail-closed (no resolvable backing credential).
    """
    if not identity:
        return None
    row = FleetAgent.objects.exclude(status=FleetAgent.Status.DELETED).filter(
        agent_id=str(identity.get('id') or ''),
    ).first()
    if row is None:
        row = FleetAgent.objects.exclude(status=FleetAgent.Status.DELETED).filter(
            agent_username=str(identity.get('username') or ''),
        ).first()
    return row


def _effective_capability_set(request, agent_id, *, aggregate_organizations=False):
    agent, error = _effective_agent_identity(agent_id)
    if error:
        return None, error
    daemon_hot_read = _request_has_valid_daemon_credential(request) and not _auth_headers_for_admin(request)
    roster_snapshot = None
    from hive_platform.coordinator import is_coordinator_service_claims
    if is_coordinator_service_claims(getattr(request, 'auth', None)):
        # The broker's revocable OAuth grant addresses Hive alone. Forwarding
        # it to Pulse's human roster endpoint fails audience validation. Use the
        # same fresh, org-qualified server projection as capability evidence;
        # this never authorizes from the daemon's 24-hour last-good fallback.
        from fleet.capability_evidence import CapabilityEvidenceError, _fresh_memberships
        try:
            rows, age, _ = _fresh_memberships(agent['email'])
        except CapabilityEvidenceError as exc:
            return None, Response(exc.as_dict(), status=exc.http_status)
        memberships = _OrganizationMembershipSet({
            _canon_team_name(row.get('team_slug') or row.get('team_name'))
            for row in rows if row.get('team_slug') or row.get('team_name')
        }, organization_rows=rows)
        roster_snapshot = {'source': 'fresh_server_projection', 'age_seconds': age}
        roster_error = None
    elif daemon_hot_read:
        memberships, roster_snapshot, roster_error = _cached_pulse_roster_memberships_for_daemon(agent['email'])
    else:
        memberships, _, roster_error = _pulse_roster_memberships(request, agent['email'])
    if roster_error:
        return None, roster_error
    identifiers = {agent['id'], agent['username'], agent['email']}
    # HIVE-417: the capability -> skills/MCP/sensitive-flag computation now has a
    # single source of truth in fleet.effective_capabilities. This view only
    # resolves the request-scoped inputs (agent identity + Pulse roster teams) and
    # then delegates the grant math, so the live HTTP path and the HIVE-398/HIVE-369
    # verification harness can never silently diverge on security-sensitive grants.
    # Lazy import: fleet.effective_capabilities imports helpers from this module, so
    # a top-level import would be circular.
    from fleet.effective_capabilities import compute_effective_capability_grant_for_memberships
    org_slug = _request_org_slug(request)
    membership_rows = getattr(memberships, 'organization_rows', None)
    if membership_rows is None:
        # Backwards-compatible fallback for callers/tests that provide only the
        # historical set-of-team-slugs contract. Real Pulse/cache reads attach
        # authoritative org-qualified rows to the set below.
        inferred_org = (
            org_slug
            or _agent_server_resolved_org_slug(agent)
            or (_UNKNOWN_CUSTOMER_TENANT_SCOPE if not _agent_is_explicit_internal(agent) else 'shizuha')
        )
        membership_rows = [
            {'team_slug': team_slug, 'organization_slug': inferred_org}
            for team_slug in memberships or []
        ]
    grant = compute_effective_capability_grant_for_memberships(
        membership_rows,
        identifiers,
        organization_scope=None if aggregate_organizations else org_slug or None,
    )
    # Re-shape the pure grant into the exact payload this endpoint has always
    # returned: layer on the request-scoped fields (agent, display title, per-team
    # roles) and drop the harness-only additive keys (top-level + per-capability
    # sensitive_access_flags, and team_slugs) so the response stays byte-for-byte
    # stable for existing consumers.
    diagnostics = list(grant['diagnostics'])
    if roster_snapshot:
        diagnostics.append({
            'level': 'info',
            'message': 'daemon capability roster snapshot',
            **roster_snapshot,
        })
    team_slugs = grant['team_slugs']
    source_team_memberships = _organization_team_memberships(
        membership_rows,
        organization_scope=None if aggregate_organizations else org_slug or None,
    )
    (
        team_credential_eligible_teams,
        team_credential_eligible_memberships,
        credential_materializations,
    ) = _credential_materialization_contract(
        agent, source_team_memberships,
    )
    payload = {
        'agent': agent,
        'source_teams': grant['team_slugs'],
        'source_team_memberships': source_team_memberships,
        'display_title': _agent_display_title(agent),
        'roles_by_team': _team_role_rows_for_agent(agent, team_slugs, org_slug=org_slug),
        'capabilities': [
            {
                'slug': cap['slug'],
                'display_name': cap['display_name'],
                'risk_tier': cap['risk_tier'],
                'version': cap['version'],
                'definition_hash': cap['definition_hash'],
                'sources': cap['sources'],
            }
            for cap in grant['capabilities']
        ],
        'agent_overrides': grant['agent_overrides'],
        'skills': grant['skills'],
        'eager_skills': grant['eager_skills'],
        'enabled_mcp_servers': grant['enabled_mcp_servers'],
        'base_mcp_missing_pre_floor': grant['base_mcp_missing_pre_floor'],
        'inline_profiles': grant['inline_profiles'],
        'runtime_flags': grant['runtime_flags'],
        'credential_grant_scopes': grant['credential_grant_scopes'],
        'team_credential_eligible_teams': team_credential_eligible_teams,
        'team_credential_eligible_memberships': team_credential_eligible_memberships,
        'credential_materializations': credential_materializations,
        'diagnostics': diagnostics,
        'computed_at': grant['computed_at'],
        'catalog_version': grant['catalog_version'],
        'definition_hashes': grant['definition_hashes'],
    }
    # HIVE-2695: listing-mint token-scope parity — an advertised credential
    # verb-tag exceeding the seat's actual Origin scopes must be impossible
    # to mint silently (the PLAT-9426 class: whitelisted-merger listing on a
    # read-scoped ORIGIN_TOKEN). The stamp is part of the minted listing so
    # every serve surface (signed daemon payload, reconcile, projection)
    # carries the explicit flag; prompt rendering surfaces it as
    # "⚠ advertised-but-unexercisable". Fail posture: a parity-check failure
    # stamps scope_gap=True (unverified = flagged), never a silent skip.
    # (Resolution order note: the parity check runs on the payload exactly as
    # main defines it; the HIVE-2622 org_provider_grant entry below is added
    # AFTER the parity call so it is invisible to the checker.)
    payload['scope_gap'] = False
    payload['scope_gap_detail'] = []
    try:
        from fleet.scope_parity import check_scope_parity

        parity = check_scope_parity(
            agent=_fleet_agent_for_parity(agent),
            grant=payload,
        )
        payload['scope_gap'] = parity.scope_gap
        payload['scope_gap_detail'] = [g.as_dict() for g in parity.gaps]
        if parity.scope_gap:
            logger.error(
                'HIVE-2695 scope parity: listing for %s advertises unexercisable '
                'verbs — %s',
                (agent or {}).get('email') or agent_id,
                '; '.join(
                    f'{g.tag}:{g.gap_class}' for g in parity.gaps
                    if g.gap_class != 'non_origin_broker_provider'
                ),
            )
    except Exception:  # noqa: BLE001 — unverified parity fails closed, never breaks the serve
        logger.exception('HIVE-2695: scope parity check failed for %s', agent_id)
        payload['scope_gap'] = True
        payload['scope_gap_detail'] = [{
            'tag': '',
            'gap_class': 'parity_check_failed',
            'required_origin_scopes': [],
            'missing_scopes': [],
        }]
    # HIVE-2622 deliverable 2 piece 2: additive signed-payload entry — the
    # org's BYO provider handle (redacted provenance; never key material).
    # Dormant until provider_resolution.LEASE_WIRING_COMPLETE flips: the
    # helper returns None and the key is ABSENT — byte-for-byte today's
    # payload for every existing consumer.
    try:
        from .org_lease import org_provider_grant_handle
        grant_handle = org_provider_grant_handle(organization_slug=org_slug)
    except Exception:
        logger.exception('org_provider_grant_handle failed for %s', org_slug)
        grant_handle = None
    if grant_handle is not None:
        payload['org_provider_grant'] = grant_handle
    return payload, None


def _runtime_capability_reconcile_for_agent_emails(request, member_emails, *, context=None):
    """Push supported effective capability fields into runtime agents.

    The daemon PATCH contract currently supports capability-driven pushes for
    skills/eager_skills/enabled_mcp_server_ids. Inline profiles, runtime flags,
    and credential scopes remain in the signed effective-capabilities payload for
    runtime-side consumers; include them in diagnostics so they are visible, but
    do not send unsupported config keys to the daemon. Capability saves must not
    be blocked by a transient daemon issue, so this is best-effort.
    """
    wanted_emails = {str(email or '').strip().lower() for email in (member_emails or []) if email}
    result = {
        **(context or {}),
        'matched_agents': 0,
        'updated_agents': 0,
        'errors': [],
        'agents': [],
        'base_mcp_floor_violations': [],
    }
    if not wanted_emails:
        return result

    for agent in list_runtime_agents(include_disabled=True):
        if _agent_email(agent) not in wanted_emails:
            continue
        result['matched_agents'] += 1
        agent_id = str(agent.get('id') or '')
        payload, error = _effective_capability_set(
            request,
            agent_id,
            aggregate_organizations=True,
        )
        if error:
            result['errors'].append({'agent': agent_id, 'error': getattr(error, 'data', str(error))})
            continue
        desired_skills = payload.get('skills') or []
        desired_eager = payload.get('eager_skills') or []
        desired_mcp = payload.get('enabled_mcp_servers') or []
        desired_profiles = payload.get('inline_profiles') or []
        desired_runtime_flags = payload.get('runtime_flags') or {}
        desired_scopes = payload.get('credential_grant_scopes') or []
        skill_config = agent.get('skill_config') or {}
        current_skills = sorted(set(agent.get('skills') or skill_config.get('skills') or []))
        current_eager = sorted(set(agent.get('eager_skills') or skill_config.get('eager_skills') or []))
        current_mcp = sorted(set(agent.get('enabled_mcp_server_ids') or []))
        updates = {}
        if current_skills != desired_skills:
            updates['skills'] = desired_skills
        if current_eager != desired_eager:
            updates['eager_skills'] = desired_eager
        if current_mcp != desired_mcp:
            updates['enabled_mcp_server_ids'] = desired_mcp
        base_mcp_missing = payload.get('base_mcp_missing_pre_floor') or []
        agent_diag = {
            'agent': agent_id,
            'email': _agent_email(agent),
            'updates': sorted(updates.keys()),
            'daemon_update_fields': ['skills', 'eager_skills', 'enabled_mcp_server_ids'],
            'effective_inline_profiles': desired_profiles,
            'effective_runtime_flags': desired_runtime_flags,
            'effective_credential_grant_scopes': desired_scopes,
            'base_mcp_missing_pre_floor': base_mcp_missing,
        }
        result['agents'].append(agent_diag)
        if base_mcp_missing:
            # PLAT-1251 fail-loud guard: the floor already repaired the pushed set,
            # but the capability data violating the base contract must be fixed —
            # surface it as an error diagnostic AND notify a concrete owner.
            violation = {'agent': agent_id, 'email': _agent_email(agent), 'missing': base_mcp_missing}
            result['base_mcp_floor_violations'].append(violation)
            logger.error(
                'PLAT-1251 guard: capability union for %s missing base MCP %s '
                '(floor applied; devops notified)', _agent_email(agent), base_mcp_missing,
            )
            _notify_base_mcp_floor_violation_async(_agent_email(agent), base_mcp_missing)
        if not updates:
            continue
        ok, err = configure_runtime_agent(agent_id, updates)
        if ok:
            result['updated_agents'] += 1
        else:
            result['errors'].append({'agent': agent_id, 'error': err, 'updates': updates})
    return result


def _runtime_capability_reconcile_for_team(request, team_key):
    """Push a team's effective capability skill/MCP unions into runtime agents."""
    team_key = _canon_team_name(team_key)
    index = _pulse_roster_team_index(request)
    member_emails = {
        email for email, memberships in index.items()
        if any(_canon_team_name(item.get('team_slug') or item.get('team_name')) == team_key for item in memberships)
    }
    return _runtime_capability_reconcile_for_agent_emails(
        request,
        member_emails,
        context={'team_slug': team_key},
    )


def _capability_dependency_slug_closure(capability):
    """Return slugs reachable from an assigned capability's requires graph.

    Include inactive/missing dependency slugs as leaves so disabling a dependency
    still fans out to teams whose active assigned capability requires it; the
    subsequent effective-capability computation will drop the inactive dependency.
    """
    resolved = set()

    def visit_slug(slug):
        slug = _canon_capability_slug(slug)
        if not slug or slug in resolved:
            return
        resolved.add(slug)
        dep = CapabilityDefinition.objects.filter(slug=slug).first()
        if not dep or dep.activation_status != CapabilityDefinition.ActivationStatus.ACTIVE:
            return
        for child_slug in dep.requires or []:
            visit_slug(child_slug)

    visit_slug(capability.slug if isinstance(capability, CapabilityDefinition) else capability)
    return resolved


def _team_slugs_receiving_capability(cap_slug):
    cap_slug = _canon_capability_slug(cap_slug)
    team_slugs = set()
    requirements = TeamRequiredCapability.objects.select_related('capability').filter(
        enabled=True,
        required=True,
    )
    for req in requirements:
        assigned = req.capability
        if assigned.slug == cap_slug:
            team_slugs.add(req.team_slug)
            continue
        if assigned.activation_status != CapabilityDefinition.ActivationStatus.ACTIVE:
            continue
        if cap_slug in _capability_dependency_slug_closure(assigned):
            team_slugs.add(req.team_slug)
    return sorted(team_slugs)


def _runtime_capability_reconcile_for_capability(request, capability):
    """Push effective config to every runtime agent receiving this capability."""
    cap_slug = capability.slug if isinstance(capability, CapabilityDefinition) else _canon_capability_slug(capability)
    team_slugs = _team_slugs_receiving_capability(cap_slug)
    if not team_slugs:
        return {
            'capability_slug': cap_slug,
            'team_slugs': [],
            'matched_agents': 0,
            'updated_agents': 0,
            'errors': [],
            'agents': [],
        }
    index = _pulse_roster_team_index(request)
    member_emails = {
        email for email, memberships in index.items()
        if any(_canon_team_name(item.get('team_slug') or item.get('team_name')) in team_slugs for item in memberships)
    }
    return _runtime_capability_reconcile_for_agent_emails(
        request,
        member_emails,
        context={'capability_slug': cap_slug, 'team_slugs': team_slugs},
    )

def _credential_snapshot(credential):
    """Redacted credential state for audit/readbacks — never includes secret_ref."""
    agent = getattr(credential, 'agent', None)
    return {
        'id': str(credential.id),
        'scope': credential.scope,
        'agent_id': str(credential.agent_id or ''),
        'agent_username': getattr(agent, 'agent_username', '') if agent else '',
        'team_slug': credential.team_slug,
        'provider': credential.provider,
        'purpose': credential.purpose,
        'has_secret_ref': bool(credential.secret_ref),
        'fingerprint': credential.fingerprint,
        'requires_team_credential_eligible': credential.requires_team_credential_eligible,
        'health': credential.health,
        'is_active': credential.is_active,
        'revoked_at': credential.revoked_at.isoformat() if credential.revoked_at else None,
    }


def _audit_credential(credential, request, action, *, before=None, message=''):
    after = _credential_snapshot(credential)
    HiveCredentialAudit.objects.create(
        credential=credential,
        actor_user_id=_actor(request),
        action=action,
        target_scope=credential.scope,
        target_agent=after.get('agent_username') or after.get('agent_id') or '',
        target_team=credential.team_slug,
        redacted_before=before or {},
        redacted_after=after,
        message=message,
    )


def _is_agent_token(request):
    claims = getattr(request, 'auth', None)
    if isinstance(claims, dict):
        if claims.get('account_type') == 'agent' or claims.get('agent') or claims.get('is_agent'):
            return True
        email = (claims.get('email') or '').lower()
        if email.endswith('@agents.shizuha.io'):
            return True
    email = (getattr(getattr(request, 'user', None), 'email', '') or '').lower()
    return email.endswith('@agents.shizuha.io')


def _agent_identity_candidates(request):
    candidates = set()
    claims = getattr(request, 'auth', None)
    if isinstance(claims, dict):
        for key in ('agent_id', 'agent', 'agent_username', 'username', 'preferred_username', 'sub'):
            value = str(claims.get(key) or '').strip()
            if value:
                candidates.add(value)
        email = str(claims.get('email') or '').strip().lower()
        if email:
            candidates.add(email)
            if email.endswith('@agents.shizuha.io'):
                candidates.add(email.split('@', 1)[0])
    user = getattr(request, 'user', None)
    for value in (
        getattr(user, 'email', ''),
        getattr(user, 'username', ''),
    ):
        value = str(value or '').strip().lower()
        if value:
            candidates.add(value)
            if value.endswith('@agents.shizuha.io'):
                candidates.add(value.split('@', 1)[0])
    return {c for c in candidates if c}


def _resolve_self_credential_agent(request):
    """Resolve the credential self-upsert target.

    Agent tokens may only target themselves. Staff callers may provide an
    explicit agent/agent_id for operator repair; non-staff humans cannot use
    the self-upsert path.
    """
    requested = str(
        request.data.get('agent')
        or request.data.get('agent_id')
        or request.data.get('agent_username')
        or ''
    ).strip()
    is_agent = _is_agent_token(request)
    is_staff = getattr(getattr(request, 'user', None), 'is_staff', False)
    candidates = _agent_identity_candidates(request)
    lookup_terms = set(candidates)

    if is_staff and requested:
        lookup_terms.add(requested)
    elif not is_agent:
        return None, Response({'error': 'Agent identity required for self credential upsert'}, status=status.HTTP_403_FORBIDDEN)

    if not lookup_terms:
        return None, Response({'error': 'Could not resolve agent identity'}, status=status.HTTP_403_FORBIDDEN)

    query = Q()
    for term in lookup_terms:
        query |= Q(agent_username__iexact=term) | Q(agent_id__iexact=term)
        try:
            query |= Q(id=uuid.UUID(term))
        except (TypeError, ValueError, ValidationError, AttributeError):
            pass

    agent = (
        FleetAgent.objects
        .exclude(status=FleetAgent.Status.DELETED)
        .filter(query)
        .order_by('agent_username')
        .first()
    )
    if agent is None:
        return None, Response({'error': 'Agent not found for credential self-upsert'}, status=status.HTTP_404_NOT_FOUND)

    if is_agent and requested:
        requested_l = requested.lower()
        allowed = {str(agent.id).lower(), (agent.agent_id or '').lower(), agent.agent_username.lower(), *{c.lower() for c in candidates}}
        if requested_l not in allowed:
            return None, Response({'error': 'Agent tokens may only upsert their own credentials'}, status=status.HTTP_403_FORBIDDEN)

    return agent, None


def _credential_purpose_from_service(service):
    purpose = re.sub(r'[^a-z0-9_.:-]+', '-', str(service or '').strip().lower()).strip('-')
    return purpose[:100]


def _credential_request_scope_candidates(credential):
    candidates = {
        str(getattr(credential, 'provider', '') or '').strip().lower(),
        str(getattr(credential, 'purpose', '') or '').strip().lower(),
    }
    if credential.provider == HiveCredential.Provider.GENERIC_ENV:
        candidates.add('custom')
    return {c for c in candidates if c}


def _fulfill_matching_credential_requests(credential, request):
    if credential.scope != HiveCredential.Scope.AGENT or not credential.agent_id:
        return 0
    scopes = _credential_request_scope_candidates(credential)
    if not scopes:
        return 0
    qs = HiveCredentialRequest.objects.filter(
        requester=credential.agent,
        scope__in=scopes,
        status=HiveCredentialRequest.Status.PENDING,
    )
    now = timezone.now()
    return qs.update(
        status=HiveCredentialRequest.Status.FULFILLED,
        fulfilled_by_credential=credential,
        resolved_by_user_id=_actor(request),
        resolved_at=now,
        resolution_reason='fulfilled by matching Hive credential grant',
        updated_at=now,
    )


def _rt_fleet_control(request, agent_id, verb):
    """HIVE-171: lifecycle control for a LIVE rt-fleet (docker) agent. verb is one
    of enable|disable|restart|reset-session. Forwards to the runtime-fleet proxy (allow-list +
    shared secret) and records the Hive-side half of the two-sided audit (staff
    user + action). Used as the fall-through when an id is not a native k3s row."""
    external = get_runtime_agent(str(agent_id))
    if not external:
        return Response({'error': 'Agent not found'}, status=status.HTTP_404_NOT_FOUND)
    actor = _actor(request)
    logger.info('rt-fleet control: user=%s action=%s agent_id=%s agent=%s',
                actor, verb, agent_id, external.get('agent_username'))
    if verb == 'restart':
        ok, detail = restart_runtime_agent(str(agent_id))
    elif verb == 'reset-session':
        ok, detail = reset_runtime_agent_session(str(agent_id))
    elif verb == 'disable':
        # Operator 2026-09-01: Hive disable is hibernation. Hard stop is /stop/.
        ok, detail = hibernate_lifecycle_agent(
            str(agent_id),
            reason='Hive disable (hibernate-only; operator stop is separate)',
        )
    else:
        ok, detail = set_runtime_agent_enabled(str(agent_id), verb == 'enable')
    if not ok:
        waiting = _lifecycle_lease_wait_response(detail)
        if waiting is not None:
            return waiting
        logger.warning('rt-fleet control FAILED: user=%s action=%s agent_id=%s detail=%s',
                       actor, verb, agent_id, detail)
        return Response({'error': str(detail)}, status=status.HTTP_502_BAD_GATEWAY)
    agent_index_cache.invalidate(f'rt-fleet {verb}')
    refreshed = get_runtime_agent(str(agent_id)) or external
    return Response(attach_context_budget(refreshed))


def _clear_stale_operator_stop(agent):
    """Native enable must not leave a daemon kill-switch on the Hive row."""
    from .lifecycle_fleet import _best_effort_runtime_toggle

    _best_effort_runtime_toggle(agent, enabled=True)
    if getattr(agent, 'runtime_lifecycle_state', '') != 'enabled':
        agent.runtime_lifecycle_state = 'enabled'
        agent.hibernation_reason = ''
        agent.hibernated_at = None
        try:
            agent.save(update_fields=[
                'runtime_lifecycle_state', 'hibernation_reason',
                'hibernated_at', 'updated_at',
            ])
        except Exception:
            pass


def _lease_wait_response(agent):
    reason = provisioner.lease_start_pending_reason(agent)
    if reason is None:
        return None
    agent_index_cache.invalidate('native lease waiting')
    payload = dict(FleetAgentSerializer(agent).data)
    payload.update({
        'pending_sync': True,
        'pending_sync_reason': reason,
        'lease_status': 'waiting',
    })
    return Response(payload, status=status.HTTP_202_ACCEPTED)


def _lifecycle_lease_wait_response(detail, *, email=None):
    if not isinstance(detail, dict) or detail.get('lease_waiting') is not True:
        return None
    payload = {
        'status': 'lease_waiting', 'pending_sync': True,
        'pending_sync_reason': detail.get('reason') or 'await_reconcile_grant',
    }
    if email is not None:
        payload['email'] = email
    return Response(payload, status=status.HTTP_202_ACCEPTED)


def _with_telemetry(agent):
    """Enrich a single agent dict with the live bridge telemetry snapshot so the
    DETAIL view shows the same EFFECTIVE (running) model + live activity/health as
    the list/card. Without this the detail showed the static configured model
    (e.g. claude-opus-4-8) while the card showed the live fallback model
    (claude-sonnet-4-6) — read as an inconsistency. Non-fatal on any failure."""
    try:
        from .external_agents import agent_telemetry, telemetry_fields
        tele = agent_telemetry()
        snap = tele.get((agent.get('agent_username') or '').lower())
        if snap:
            # Pass pre-merge agent as configured SoT so live process does not
            # clobber Hive-authored harness/model after a convert.
            agent.update(telemetry_fields(snap, configured=dict(agent)))
        _apply_provider_unavailable_health([agent], tele)
        _clear_empty_queue_conversational_needs_help(agent)
        _clear_needs_help_if_currently_working(agent)
    except Exception:
        logger.exception('detail telemetry merge failed (non-fatal)')
    try:
        _attach_model_lease_status([agent])
    except Exception:
        logger.exception('detail model-lease attach failed (non-fatal)')
    try:
        from .activity_phase import annotate_activity
        annotate_activity(agent)
    except Exception:
        logger.exception('detail activity-phase annotate failed (non-fatal)')
    # Budget must run AFTER telemetry overlay. Computing it first left
    # conversation_history deferred=0 while context_used_tokens was 350k.
    try:
        agent['context_budget'] = context_budget_for_agent(agent)
    except Exception:
        logger.exception('detail context-budget refresh failed (non-fatal)')
    return agent


# PLAT-5265: moved to fleet/agent_health.py so the on-demand reconciler applies
# the same enrichment as the read paths. Imported under their original private
# names so every existing call site in this module keeps working unchanged.
from .agent_health import (  # noqa: F401
    _provider_key,
    _snap_provider_key,
    _telemetry_ts_ms,
    agent_provider_key as _agent_provider_key,
    provider_unavailable_from_telemetry as _provider_unavailable_from_telemetry,
    has_recent_explicit_healthy_telemetry as _has_recent_explicit_healthy_telemetry,
    apply_provider_unavailable_health as _apply_provider_unavailable_health,
)


def _id_agent_records_for_health(agents=None):
    """Best-effort Shizuha-ID agent list for identity-health annotations."""
    try:
        from .external_agents import _id_get
        payload = _id_get('/api/internal/agents/list/', {'active': 'true'}) or {}
        records = payload.get('agents') if isinstance(payload, dict) else None
        if not isinstance(records, list):
            return None

        # The inventory intentionally filters account_type=agent. Probe only
        # missing Hive-managed usernames through the single-user endpoint so a
        # real but mis-tagged account is not mistaken for an orphan while the
        # canonical hourly SSOT reconciler is waiting for its next pass.
        known = {
            str(record.get('username') or '').strip().lower()
            for record in records
            if isinstance(record, dict)
        }
        for agent in agents or []:
            username = str(agent.get('agent_username') or '').strip().lower()
            if (
                not username
                or username in known
                or agent.get('kind') == 'claw'
                or agent.get('lifecycle_managed') is False
            ):
                continue
            record = _id_get('/api/internal/agents/', {'username': username}) or {}
            if record.get('exists'):
                records.append(record)
                known.add(username)
        return records
    except Exception:
        logger.exception('identity-health id agent list failed (non-fatal)')
        return None


def _attach_identity_routing_health(agents):
    records = _id_agent_records_for_health(agents)
    if records is None:
        return agents
    return annotate_agents(agents, records)


# HIVE-297: canonical labels for known issue_type values (sourced from identity_health.py
# and provider telemetry). Keys mirror WorkflowService and agent-health-exporter conventions.
_HEALTH_ISSUE_LABELS: dict[str, str] = {
    'roster_unassignable':    'Pulse routing blocked',
    'identity_unresolved':    'Identity drift',
    'identity_orphan_cleanup':'Incomplete identity — retiring',
    'identity_email_mismatch':'Identity drift',
    'identity_missing':       'Identity drift',
    'credential_missing':     'Credentials missing',
    'provider_unavailable':   'Provider unavailable',
    'provider_unavailable_or_quota': 'Provider unavailable',
    'runtime_unhealthy':      'Runtime unhealthy',
    'runtime_config_drift':   'Runtime config drift',
    'runtime_unavailable':    'Runtime unavailable',
    'runtime_health_unknown': 'Runtime health unknown',
    'heartbeat_needs_help':   'Agent needs help',
    'capabilities_drift':     'Capabilities drift',
    'desired_state_not_applied': 'Desired state not applied',
}

# Issue types that are errors (routing-breaking) vs warnings (degraded but functional).
# `heartbeat_needs_help` is deliberately NOT here: the runtime is fine (health_ok
# stays true) and the agent is telling us it cannot make progress on its WORK —
# stuck on ready tasks, or unable to read its Pulse queue. Rendering it in the
# same red as a broken runtime made a healthy-but-stuck agent indistinguishable
# from an auth-failing one, and the operator read the whole fleet as unhealthy
# (2026-08-05: banto/shion were "2 ready task(s) with no progress for 59
# heartbeat(s)" while serving normally). It is still surfaced, still actionable,
# just not a runtime fault.
_HEALTH_ERROR_TYPES: frozenset[str] = frozenset({
    'roster_unassignable',
    'runtime_unhealthy',
    'runtime_config_drift',
    'runtime_unavailable',
    'runtime_health_unknown',
})

# Inference 503s that mean Cortex/vLLM has no room — the agent pod is fine.
# Must not paint "Runtime unhealthy" (2026-09-02 misaki: residency_full 503
# while 2/2 Running + queue_empty).
_CAPACITY_TURN_HINT = re.compile(
    r'residency_full|residency is full|no healthy replica|backend unavailable'
    r'|occupies the slot|realtime session mint failed',
    re.IGNORECASE,
)

# Work-progress signals: real and worth acting on, but the runtime is healthy.
_HEALTH_ATTENTION_TYPES: frozenset[str] = frozenset({
    'heartbeat_needs_help',
})


# HIVE-1690 / binding v1.1 (aoi+sora): desired lifecycle `running` is NOT an
# observation. Health for claimed-running agents is refuted by ONE readiness
# source — the Claw/native Deployment's ready_replicas — never by thresholding
# `last_active_at` (written on provision/start, not a heartbeat).
#
# Callers attach `runtime_readiness` ∈ {ready, unavailable, unknown} before
# summarizing (see `_observe_claw_runtime_readiness` / owner claw projection).
# Pure unit tests pass the field directly.


def _observe_claw_runtime_readiness(namespace: str, *, deployment_name: str = 'claw') -> str:
    """Probe k8s Deployment ready_replicas for a personal Claw/Hermes runtime.

    Returns:
      ready       — ≥1 ready replica
      unavailable — Deployment missing (404) or ready_replicas == 0
      unknown     — probe infrastructure failure (no kubeconfig / API error);
                    fail-open at the delivery gate, but health must not assert ok
    """
    ns = (namespace or '').strip()
    if not ns:
        return 'unknown'
    try:
        from claws.provisioner import DEPLOYMENT_NAME, get_k8s_client
    except Exception:
        return 'unknown'
    name = deployment_name or DEPLOYMENT_NAME
    try:
        _core, apps_v1 = get_k8s_client()
        deploy = apps_v1.read_namespaced_deployment(name, ns)
    except Exception as exc:
        # 404 → no workload → unavailable. Other errors → unknown (infra).
        status = getattr(exc, 'status', None)
        if status == 404:
            return 'unavailable'
        return 'unknown'
    try:
        ready = int(getattr(getattr(deploy, 'status', None), 'ready_replicas', 0) or 0)
    except Exception:
        return 'unknown'
    return 'ready' if ready >= 1 else 'unavailable'


def _agent_is_pulse_queue_obligated(agent) -> bool:
    """True when the seat is expected to expose a Pulse queue on every heartbeat.

    Lean conversational / CEO Office talkable seats are intended to sit idle
    with no assigned Pulse work. They do not declare pulse_get_my_tasks, so a
    queue-blind heartbeat is not a poisoned session.
    """
    env = agent.get('env') if isinstance(agent.get('env'), dict) else {}
    if str(env.get('SHIZUHA_LEAN_MCP') or '').strip() == '1':
        return False
    team_hint = str(env.get('AGENT_TEAM') or '').strip().lower()
    if team_hint in LEAN_CONVERSATIONAL_TEAM_SLUGS:
        return False
    username = str(agent.get('agent_username') or '').strip().lower()
    if username in LEAN_CONVERSATIONAL_USERNAMES:
        return False
    for membership in agent.get('team_memberships') or []:
        if not isinstance(membership, dict):
            continue
        slug = str(membership.get('team_slug') or '').strip().lower()
        if slug in LEAN_CONVERSATIONAL_TEAM_SLUGS:
            return False
    return True


def _heartbeat_ready_count(agent) -> int:
    raw = agent.get('heartbeat_ready_task_count')
    if raw is None:
        return 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def _clear_empty_queue_conversational_needs_help(agent) -> None:
    """Do not paint idle talkable seats as Needs help for an empty Pulse queue.

    Producer contract still owns genuine stuck-on-ready episodes. This only
    retracts the known false-positive class: queue-blind / empty-queue
    needs_help on a seat that is not Pulse-queue-obligated.
    """
    flagged = (
        agent.get('needs_help') is True
        or str(agent.get('heartbeat_outcome') or '').strip().lower() == 'needs_help'
    )
    if not flagged:
        return
    if _heartbeat_ready_count(agent) > 0:
        return
    if _agent_is_pulse_queue_obligated(agent):
        return
    agent['needs_help'] = False
    agent['needs_help_reason'] = ''
    if str(agent.get('heartbeat_outcome') or '').strip().lower() == 'needs_help':
        agent['heartbeat_outcome'] = 'queue_empty'


def _clear_needs_help_if_currently_working(agent) -> None:
    """Ichi 2026-09-15: last heartbeat was ready_no_progress while live
    activity showed pulse_execute_transition / bash on PLAT-7600. Do not
    paint Needs help on a seat that is in a working phase with tools."""
    flagged = (
        agent.get('needs_help') is True
        or str(agent.get('heartbeat_outcome') or '').strip().lower() == 'needs_help'
    )
    if not flagged:
        return
    phase = str(agent.get('activity_phase') or agent.get('activity_state') or '').strip().lower()
    reason = str(agent.get('needs_help_reason') or '').lower()
    queue_blind = 'no pulse queue snapshot' in reason
    # Aoi 2026-09-16: thinking/responding tenure still painted
    # "N consecutive heartbeat(s) exposed no Pulse queue snapshot" because
    # this helper required a live tool name. Queue-blind during a turn is
    # not a stuck seat.
    if queue_blind and phase in {'thinking', 'working', 'responding', 'tool'}:
        agent['needs_help'] = False
        agent['needs_help_reason'] = ''
        if str(agent.get('heartbeat_outcome') or '').strip().lower() == 'needs_help':
            agent['heartbeat_outcome'] = 'worked_task'
        return
    if phase not in {'working', 'responding', 'tool'}:
        return
    if int(agent.get('activity_tool_count') or 0) <= 0 and not agent.get('activity_tool_name'):
        return
    agent['needs_help'] = False
    agent['needs_help_reason'] = ''
    if str(agent.get('heartbeat_outcome') or '').strip().lower() == 'needs_help':
        agent['heartbeat_outcome'] = 'worked_task'


def _clear_needs_help_if_scaled_down(agent) -> None:
    """Sora/Sana 2026-09-16: lease yield hibernates the seat but the last
    heartbeat's needs_help flag stayed on the card. A scaled-to-zero seat
    is not asking for help — it is parked. Retract the work chip."""
    flagged = (
        agent.get('needs_help') is True
        or str(agent.get('heartbeat_outcome') or '').strip().lower() == 'needs_help'
    )
    if not flagged:
        return
    status = str(agent.get('status') or '').strip().lower()
    life = str(agent.get('lifecycle_state') or agent.get('runtime_lifecycle_state') or '').strip().lower()
    if status not in {'hibernated', 'stopped', 'offline'} and life not in {
        'hibernated', 'operator_stopped',
    }:
        return
    agent['needs_help'] = False
    agent['needs_help_reason'] = ''
    if str(agent.get('heartbeat_outcome') or '').strip().lower() == 'needs_help':
        agent['heartbeat_outcome'] = 'queue_empty'


def _compute_agent_health_summary(agent):
    """HIVE-297 (P2): enrich health_summary with structured issues list and severity tiers.

    Merges warnings produced by annotate_agents() (identity/routing from Shizuha-ID)
    with provider-unavailable signals from _apply_provider_unavailable_health(). When
    P1 AgentHealthSnapshot models land, this becomes a thin wrapper over those models.

    Preserves the `warnings` / `ok` / `warning_count` keys for backward-compat with
    any callers that still read the annotate_agents() format.

    Never exposes secret values — only capability/routing/provider health state.
    """
    _clear_empty_queue_conversational_needs_help(agent)
    _clear_needs_help_if_currently_working(agent)
    _clear_needs_help_if_scaled_down(agent)
    # Merge from annotate_agents() output (set by _attach_identity_routing_health before us).
    existing = agent.get('health_summary') or {}
    existing_warnings = existing.get('warnings') or []

    issues: list[dict] = []
    seen_types: set[str] = set()

    # Convert identity/routing warnings (annotate_agents format) → issue chips.
    for w in existing_warnings:
        issue_type = w.get('issue_type') or 'unknown'
        service = w.get('service') or 'unknown'
        label = _HEALTH_ISSUE_LABELS.get(issue_type) or w.get('message') or issue_type
        severity = 'error' if issue_type in _HEALTH_ERROR_TYPES else 'warning'
        if issue_type not in seen_types:
            seen_types.add(issue_type)
            issue = {'type': issue_type, 'severity': severity, 'service': service, 'label': label}
            if w.get('cleanup_action'):
                issue['cleanup_action'] = w['cleanup_action']
            issues.append(issue)

    # Add provider-down from telemetry (not captured by annotate_agents identity check).
    provider_down = (
        agent.get('provider_unavailable') is True
        or agent.get('health_token_pool_unavailable') is True
    )
    if provider_down and 'provider_unavailable_or_quota' not in seen_types and 'provider_unavailable' not in seen_types:
        issues.append({
            'type': 'provider_unavailable_or_quota',
            'severity': 'warning',
            'service': 'provider',
            'label': 'Provider unavailable',
        })
        seen_types.add('provider_unavailable_or_quota')

    runtime_config_drift = agent.get('runtime_config_drift') is True
    if runtime_config_drift and 'runtime_config_drift' not in seen_types:
        issues.append({
            'type': 'runtime_config_drift',
            'severity': 'error',
            'service': 'runtime',
            'label': _HEALTH_ISSUE_LABELS['runtime_config_drift'],
            'detail': 'Configured and running models do not match',
        })
        seen_types.add('runtime_config_drift')

    # A fresh runtime snapshot is authoritative about whether the agent can
    # complete work. Never let the synthesized summary say "ok" while the same
    # response exposes health_ok=false. Provider-specific failures already get
    # the more actionable issue above; all other failures receive a fail-closed
    # runtime issue so routing and the operator UI agree.
    if (
        agent.get('health_ok') is False
        and not provider_down
        and not runtime_config_drift
        and 'runtime_unhealthy' not in seen_types
    ):
        # Carry WHY. The runtime already reports the failing turns and their
        # messages; dropping them left the card asserting "Runtime unhealthy"
        # with detail=None, which is unactionable and reads identical whether an
        # agent is auth-broken or merely failing a prewarm optimisation
        # (operator 2026-08-05: rei was `vLLM error 401: Authentication
        # credentials were not provided` x4 while nami was a soft-drain rehome
        # that did not take — same chip, very different problems).
        _errs = [str(e).strip() for e in (agent.get('recent_errors') or []) if str(e).strip()]
        _consecutive = agent.get('health_consecutive_errors')
        _detail = _errs[-1][:240] if _errs else ''
        if _consecutive:
            _prefix = f'{_consecutive} consecutive error turn(s)'
            _detail = f'{_prefix}: {_detail}' if _detail else _prefix
        # 2026-09-02: Cortex 503 residency_full / "no healthy replica" is
        # serving-capacity, not a dead agent pod. Misaki's card said
        # "Runtime unhealthy" while 2/2 Running and idle on queue_empty —
        # the turn failed because GLM homes were full (tp4a down). Render
        # the same chip as a provider outage so the operator does not
        # restart a healthy runtime.
        _err_blob = '\n'.join(_errs)
        _capacity = bool(
            _CAPACITY_TURN_HINT.search(_err_blob)
            or _CAPACITY_TURN_HINT.search(_detail or '')
        )
        if _capacity:
            issues.append({
                'type': 'provider_unavailable',
                'severity': 'warning',
                'service': 'provider',
                'label': _HEALTH_ISSUE_LABELS['provider_unavailable'],
                'detail': _detail or None,
            })
            seen_types.add('provider_unavailable')
        else:
            issues.append({
                'type': 'runtime_unhealthy',
                'severity': 'error',
                'service': 'runtime',
                'label': 'Runtime unhealthy',
                'detail': _detail or None,
            })

    # Native heartbeat outcomes describe whether the agent can actually see
    # and drain its Pulse queue. Surface that on the same operator health chip;
    # otherwise the card can simultaneously say needs_help=true and ok=true.
    heartbeat_needs_help = (
        agent.get('needs_help') is True
        or str(agent.get('heartbeat_outcome') or '').strip().lower() == 'needs_help'
    )
    if heartbeat_needs_help and 'heartbeat_needs_help' not in seen_types:
        issue = {
            'type': 'heartbeat_needs_help',
            # Not 'error': the runtime is healthy, the WORK is stuck. See
            # _HEALTH_ATTENTION_TYPES.
            'severity': 'attention',
            'service': 'work',
            'label': _HEALTH_ISSUE_LABELS['heartbeat_needs_help'],
        }
        reason = str(agent.get('needs_help_reason') or '').strip()
        if reason:
            issue['detail'] = reason
        issues.append(issue)

    # Snapshot age is informational only (card "about N ago"). NEVER used to
    # threshold health — binding v1.1 forbids last_active_at freshness gates.
    snapshot_age_s = _agent_snapshot_age_seconds(agent)

    # HIVE-1690: Deployment readiness refutes claimed `running`.
    # Desired lifecycle state is not an observation. A Claw row with
    # status=running and ready_replicas=0 (or missing Deployment) must not
    # paint ALIVE. Binding: typed runtime_unavailable / runtime_health_unknown
    # on service=runtime — never a last_active_at age threshold.
    claimed_running = str(agent.get('status') or '').strip().lower() == 'running'
    readiness = str(agent.get('runtime_readiness') or '').strip().lower()
    if claimed_running and readiness == 'unavailable' and 'runtime_unavailable' not in seen_types:
        issues.append({
            'type': 'runtime_unavailable',
            'severity': 'error',
            'service': 'runtime',
            'label': _HEALTH_ISSUE_LABELS['runtime_unavailable'],
            'detail': (
                str(agent.get('runtime_readiness_detail') or '').strip()
                or 'Deployment has no ready replicas; lifecycle running is desired-state only'
            ),
        })
        seen_types.add('runtime_unavailable')
    elif claimed_running and readiness == 'unknown' and 'runtime_health_unknown' not in seen_types:
        issues.append({
            'type': 'runtime_health_unknown',
            'severity': 'error',
            'service': 'runtime',
            'label': _HEALTH_ISSUE_LABELS['runtime_health_unknown'],
            'detail': (
                str(agent.get('runtime_readiness_detail') or '').strip()
                or 'Could not observe Deployment readiness; liveness unproven'
            ),
        })
        seen_types.add('runtime_health_unknown')

    has_error = any(i['severity'] == 'error' for i in issues)
    # 'attention' ranks ABOVE 'warning': the runtime is fine but the agent is
    # making no progress on real work, which needs eyes sooner than a cosmetic
    # degradation. It must never collapse to 'ok' — an agent stuck for 59
    # heartbeats is not ok, it is just not broken.
    has_attention = any(i['severity'] == 'attention' for i in issues)
    has_warning = any(i['severity'] == 'warning' for i in issues)
    highest_severity = (
        'error' if has_error
        else 'attention' if has_attention
        else 'warning' if has_warning
        else 'ok'
    )

    return {
        # HIVE-297 enriched fields.
        'highest_severity': highest_severity,
        'open_issue_count': len(issues),
        'issue_types': [i['type'] for i in issues],
        'issues': issues,
        'blocking_services': list({i['service'] for i in issues if i['severity'] == 'error'}),
        'last_snapshot_age_s': snapshot_age_s,
        'desired_generation': None,
        'applied_generation': None,
        'config_drift': runtime_config_drift,
        'recovery_state': 'none',
        'runtime_readiness': readiness or None,
        # Backward-compat with annotate_agents() / summarize_agent_health() callers.
        # Attention issues still mean "not ok" for the ALIVE counter (needs eyes).
        'ok': not issues,
        'warnings': existing_warnings,
        'warning_count': len(existing_warnings),
    }



def _agent_snapshot_age_seconds(agent):
    observed_at = agent.get('last_telemetry_at') or agent.get('last_active_at')
    if not observed_at:
        return None
    try:
        import time
        from datetime import timezone
        from dateutil import parser

        if isinstance(observed_at, str):
            observed_at = parser.parse(observed_at)
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        return max(0, int(time.time() - observed_at.timestamp()))
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def _attach_health_summaries(agents):
    """HIVE-297: attach health_summary to every agent dict in the list."""
    for agent in agents:
        # HIVE-1690: claw projections may arrive without readiness; observe once.
        if (
            agent.get('kind') == 'claw'
            and not agent.get('runtime_readiness')
            and str(agent.get('status') or '').strip().lower() == 'running'
        ):
            agent['runtime_readiness'] = _observe_claw_runtime_readiness(
                str(agent.get('namespace') or '')
            )
        agent['health_summary'] = _compute_agent_health_summary(agent)
    return agents


def _attach_provider_notes(agents):
    """HIVE-2161 S3-lite: org-scoped BYO provider note for agent cards.

    Loud hold (kumo refinements #2/#5): a BYO org inside the lease-wiring or
    identity-unresolved window never sees a bare unexplained failure — the
    card names the failure, the no-Shizuha-key guarantee, and the org scope.
    Adds `provider_note` ONLY when a note exists, so non-BYO orgs are
    byte-for-byte unchanged. Non-fatal by design: annotation failure never
    breaks the grid.
    """
    if not agents:
        return agents
    try:
        from . import provider_resolution as _pr
        notes = _pr.org_provider_notes(agents)
    except Exception:  # noqa: BLE001 — card annotation is non-fatal
        logger.exception('provider-note annotate failed (non-fatal)')
        return agents
    for agent in agents:
        note = notes.get(str(agent.get('agent_username') or '').strip().lower())
        if note:
            agent['provider_note'] = note
    return agents


def _agent_email(agent):
    email = (agent.get('email') or '').strip().lower()
    if email:
        return email
    username = (agent.get('agent_username') or '').strip().lower()
    if not username:
        return ''
    # PLAT-4220 (operator 2026-07-11): canonical agent email is ALWAYS @shizuha.com
    # — we do not own shizuha.io. The prior k3s-native '@agents.shizuha.io' branch
    # existed only because provisioner-plane agent IDENTITIES were minted on that
    # domain (making the @shizuha.com fallback map to 'Unassigned'). Those
    # identities have now been migrated to @shizuha.com, so the fallback is
    # single-domain again. (New-agent provisioning is being moved to @shizuha.com
    # too — see PLAT-4220; until then a freshly-provisioned agent carries its real
    # `email` field, which is returned above before this fallback is reached.)
    return f'{username}@shizuha.com'


def _agent_display_title(agent):
    return (
        str(agent.get('display_title') or '').strip()
        or str(agent.get('role') or '').strip()
        or 'Agent'
    )


def _agent_role_identifiers(agent):
    values = {
        agent.get('id'),
        agent.get('agent_id'),
        agent.get('agent_username'),
        agent.get('username'),
        _agent_email(agent),
    }
    return {str(value).strip().lower() for value in values if str(value or '').strip()}


def _membership_role_value(value):
    raw = str(value or '').strip().lower().replace('_', '-').replace(' ', '-')
    allowed = {choice[0] for choice in TeamMembershipRole.MembershipRole.choices}
    return raw if raw in allowed else TeamMembershipRole.MembershipRole.CUSTOM


def _team_role_org_scope(org_slug=''):
    return str(org_slug or '').strip().lower()


def _safe_team_role_edges_for_agents(agents, *, org_slug='', enabled_only=True):
    identifiers = set()
    for agent in agents or []:
        identifiers.update(_agent_role_identifiers(agent))
    if not identifiers:
        return {}
    try:
        qs = TeamMembershipRole.objects.filter(
            agent_identifier__in=identifiers,
            organization_slug=_team_role_org_scope(org_slug),
        )
        if enabled_only:
            qs = qs.filter(enabled=True)
        return {(row.agent_identifier, row.team_slug): row for row in qs}
    except Exception as exc:  # pragma: no cover - defensive for no-DB unit harnesses / cold migrations
        if exc.__class__.__name__ != 'DatabaseOperationForbidden':
            logger.warning('team membership role edge lookup failed: %s', exc)
        return {}


def _safe_team_role_edges_for_members(team_slug, emails, *, org_slug='', enabled_only=True):
    identifiers = {str(email or '').strip().lower() for email in emails if str(email or '').strip()}
    team_key = _canon_team_name(team_slug)
    if not identifiers or not team_key:
        return {}
    try:
        qs = TeamMembershipRole.objects.filter(
            agent_identifier__in=identifiers,
            team_slug=team_key,
            organization_slug=_team_role_org_scope(org_slug),
        )
        if enabled_only:
            qs = qs.filter(enabled=True)
        return {(row.agent_identifier, row.team_slug): row for row in qs}
    except Exception as exc:  # pragma: no cover
        if exc.__class__.__name__ != 'DatabaseOperationForbidden':
            logger.warning('team membership role edge lookup failed: %s', exc)
        return {}


def _team_role_edge_for(agent, team_slug, role_edges):
    team_key = _canon_team_name(team_slug)
    for identifier in _agent_role_identifiers(agent):
        edge = role_edges.get((identifier, team_key))
        if edge:
            return edge
    return None


def _team_role_payload(agent, team_slug, *, membership=None, edge=None, selected=True):
    membership = membership or {}
    team_key = _canon_team_name(team_slug)
    if not selected and not edge:
        return {
            'team_slug': team_key,
            'role_title': '',
            'membership_role': None,
            'tier': '',
            'enabled': False,
            'team_role_source': 'fallback',
        }
    role_title = ''
    membership_role = 'member'
    tier = ''
    source = 'fallback'
    enabled = bool(selected)
    if edge:
        role_title = edge.role_title or ''
        membership_role = edge.membership_role or 'member'
        tier = edge.tier or ''
        source = edge.source or 'hive'
        enabled = edge.enabled
    else:
        role_title = str(membership.get('role_title') or '').strip()
        membership_role = membership.get('membership_role') or membership.get('role') or 'member'
        tier = str(membership.get('tier') if membership.get('tier') is not None else membership.get('tier_index') or '').strip()
    if selected and not role_title:
        role_title = _agent_display_title(agent)
    return {
        'team_slug': team_key,
        'role_title': role_title,
        'membership_role': _membership_role_value(membership_role),
        'tier': str(tier or ''),
        'enabled': bool(enabled),
        'team_role_source': source,
    }


def _apply_team_role(agent, team_row, *, membership=None, edge=None, selected=True):
    payload = _team_role_payload(
        agent,
        team_row.get('team_slug') or team_row.get('team_name') or team_row.get('slug') or team_row.get('id'),
        membership=membership or team_row,
        edge=edge,
        selected=selected,
    )
    team_row.update(payload)
    return team_row


def _attach_team_role_summaries(agents, *, org_slug=''):
    if not agents:
        return agents
    role_edges = _safe_team_role_edges_for_agents(agents, org_slug=org_slug)
    for agent in agents:
        agent['display_title'] = _agent_display_title(agent)
        memberships = agent.get('team_memberships') or []
        summary = []
        for membership in memberships:
            team_key = _canon_team_name(membership.get('team_slug') or membership.get('team_name') or membership.get('team_id'))
            if not team_key:
                continue
            edge = _team_role_edge_for(agent, team_key, role_edges)
            _apply_team_role(agent, membership, membership=membership, edge=edge, selected=True)
            summary.append({
                'team_slug': team_key,
                'team_name': membership.get('team_name') or _display_team_name(team_key),
                'role_title': membership.get('role_title') or _agent_display_title(agent),
                'membership_role': membership.get('membership_role') or 'member',
                'tier': str(membership.get('tier') if membership.get('tier') is not None else membership.get('tier_index') or ''),
                'source': membership.get('team_role_source') or membership.get('membership_source') or 'fallback',
            })
        summary.sort(key=lambda item: item['team_slug'])
        agent['team_roles_summary'] = summary
        distinct = {
            (
                item.get('team_slug'),
                item.get('role_title') or '',
                item.get('membership_role') or '',
            )
            for item in summary
        }
        agent['has_multiple_team_roles'] = len(distinct) > 1
    return agents


def _team_role_rows_for_agent(agent, team_slugs, *, org_slug=''):
    role_edges = _safe_team_role_edges_for_agents([agent], org_slug=org_slug)
    rows = []
    for team_slug in sorted({_canon_team_name(slug) for slug in (team_slugs or []) if _canon_team_name(slug)}):
        edge = _team_role_edge_for(agent, team_slug, role_edges)
        payload = _team_role_payload(agent, team_slug, edge=edge, selected=True)
        payload['team_name'] = _display_team_name(team_slug)
        rows.append(payload)
    return rows


def _upsert_team_membership_role(request, *, agent_identifier, team_slug, organization_slug='', action='add', payload=None):
    agent_identifier = str(agent_identifier or '').strip().lower()
    team_key = _canon_team_name(team_slug)
    org_scope = _team_role_org_scope(organization_slug)
    if not agent_identifier or not team_key:
        return
    try:
        if action == 'remove':
            TeamMembershipRole.objects.filter(
                agent_identifier=agent_identifier,
                team_slug=team_key,
                organization_slug=org_scope,
            ).update(
                enabled=False,
                updated_by=_actor(request),
                source='hive',
            )
            return
        data = payload or {}
        membership_role = _membership_role_value(data.get('membership_role') or data.get('role') or 'member')
        defaults = {
            'role_title': str(data.get('role_title') or data.get('display_title') or '').strip(),
            'membership_role': membership_role,
            'tier': str(data.get('tier') if data.get('tier') is not None else '').strip(),
            'enabled': True,
            'source': 'hive',
            'reason': str(data.get('reason') or 'Updated from Hive team placement UI').strip(),
            'updated_by': _actor(request),
        }
        row, created = TeamMembershipRole.objects.update_or_create(
            agent_identifier=agent_identifier,
            team_slug=team_key,
            organization_slug=org_scope,
            defaults=defaults,
        )
        if created and not row.created_by:
            row.created_by = _actor(request)
            row.save(update_fields=['created_by', 'updated_at'])
    except Exception as exc:  # pragma: no cover - display-only edge must not break roster writes
        if exc.__class__.__name__ != 'DatabaseOperationForbidden':
            logger.warning('team membership role edge write failed: %s', exc)


def _auth_headers_for_admin(request):
    token = getattr(request, '_hive_bearer_token', '') or ''
    auth = request.META.get('HTTP_AUTHORIZATION', '')
    if not token and auth.startswith('Bearer '):
        token = auth[7:]
    if not token:
        return None
    return {'Authorization': f'Bearer {token}'}


def _admin_request(request, method, path, *, params=None, json=None):
    headers = _auth_headers_for_admin(request)
    if not headers:
        return None, Response({'error': 'No bearer token available for Admin roster update'}, status=status.HTTP_401_UNAUTHORIZED)
    base = getattr(settings, 'HIVE_ADMIN_API_URL', '').rstrip('/')
    try:
        resp = requests.request(
            method,
            f'{base}{path}',
            headers=headers,
            params=params,
            json=json,
            timeout=5,
        )
    except requests.RequestException as exc:
        logger.warning('admin roster request failed: %s %s: %s', method, path, exc)
        return None, Response({'error': f'Admin API unreachable: {exc}'}, status=status.HTTP_502_BAD_GATEWAY)
    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except Exception:
            detail = {'error': resp.text[:300]}
        return None, Response({'error': 'Admin API rejected roster update', 'detail': detail}, status=resp.status_code)
    if resp.status_code == 204:
        return {}, None
    try:
        return resp.json(), None
    except ValueError:
        return {}, None


def _active_jit_grant_for_scope(request, scope):
    """Resolve one exact, currently-active Admin JIT grant for ``scope``.

    Admin owns the grant lifecycle, so Hive re-checks it at request time with
    the caller's already-validated bearer token instead of trusting a cached
    JWT projection.  ``denied`` is a definitive no-match; ``dependency`` means
    Admin could not provide an unambiguous authorization decision.
    """
    headers = _auth_headers_for_admin(request)
    if not headers:
        return None, 'dependency', 'caller bearer token unavailable'

    base = getattr(settings, 'HIVE_ADMIN_API_URL', '').rstrip('/')
    try:
        response = requests.get(
            f'{base}/jit-elevations/',
            headers=headers,
            params={'status': 'approved'},
            timeout=5,
        )
    except requests.RequestException as exc:
        logger.warning('Admin JIT authorization request failed: %s', exc.__class__.__name__)
        return None, 'dependency', 'Admin JIT authorization unavailable'

    if response.status_code != status.HTTP_200_OK:
        logger.warning('Admin JIT authorization returned HTTP %s', response.status_code)
        return None, 'dependency', 'Admin JIT authorization unavailable'
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return None, 'dependency', 'Admin JIT authorization response malformed'
    if not isinstance(payload, dict) or not isinstance(payload.get('results'), list):
        return None, 'dependency', 'Admin JIT authorization response malformed'
    try:
        result_count = int(payload.get('count'))
    except (TypeError, ValueError):
        return None, 'dependency', 'Admin JIT authorization response malformed'
    if result_count != len(payload['results']):
        # The current Admin list contract caps results.  An incomplete page
        # cannot prove either absence or uniqueness, so never authorize from it.
        return None, 'dependency', 'Admin JIT authorization response incomplete'

    caller_id = getattr(getattr(request, 'user', None), 'id', None)
    caller_email = str(getattr(getattr(request, 'user', None), 'email', '') or '').strip().lower()
    # HIVE-779 reika P1: the Architecture contract binds the grant to caller id
    # AND email. A caller with no resolvable email cannot satisfy that binding,
    # so fail closed with a definitive no-match instead of silently dropping the
    # email check (the old `if caller_email and ...` guard authorized on
    # id+org+scope alone whenever the caller presented no email).
    if not caller_email:
        return None, 'denied', 'caller identity missing required email binding'
    memberships = getattr(getattr(request, 'user', None), 'organization_memberships', {}) or {}
    if not isinstance(memberships, dict):
        memberships = {}
    caller_org_ids = {str(value) for value in memberships.keys()}
    now = timezone.now()
    if timezone.is_naive(now):
        now = timezone.make_aware(now, datetime_timezone.utc)
    matches = []

    for grant in payload['results']:
        if not isinstance(grant, dict):
            return None, 'dependency', 'Admin JIT authorization response malformed'
        if grant.get('scope') != scope:
            continue

        required = {
            'id', 'organization', 'requester_user_id', 'requester_email',
            'status', 'expires_at', 'revoked_at', 'active',
        }
        if not required.issubset(grant):
            return None, 'dependency', 'Admin JIT authorization response malformed'

        try:
            grant_id = int(grant.get('id'))
            grant_org_id = int(grant.get('organization'))
            grant_requester_id = int(grant.get('requester_user_id'))
            expected_caller_id = int(caller_id)
        except (TypeError, ValueError):
            return None, 'dependency', 'Admin JIT authorization response malformed'
        grant_email = str(grant.get('requester_email') or '').strip().lower()
        if grant_requester_id != expected_caller_id:
            continue
        # HIVE-779 reika P1: an empty/missing grant email cannot be bound to the
        # caller — never authorize from it — and the bound emails must match
        # exactly. caller_email is guaranteed non-empty by the pre-loop guard.
        if not grant_email or grant_email != caller_email:
            continue
        if str(grant_org_id) not in caller_org_ids:
            continue
        if grant.get('status') != 'approved' or grant.get('active') is not True:
            continue
        if grant.get('revoked_at') not in (None, ''):
            continue

        expires_at = parse_datetime(str(grant.get('expires_at') or ''))
        if expires_at is None or timezone.is_naive(expires_at):
            return None, 'dependency', 'Admin JIT authorization response malformed'
        if expires_at <= now:
            continue
        normalized_grant = dict(grant)
        normalized_grant['id'] = grant_id
        normalized_grant['organization'] = grant_org_id
        matches.append(normalized_grant)

    if not matches:
        return None, 'denied', 'no exact active Admin JIT grant'
    if len(matches) != 1:
        return None, 'dependency', 'Admin JIT authorization response ambiguous'
    return matches[0], None, ''


def _audit_unarchive_jit(request, *, target, result, reason, scope='', grant=None):
    grant = grant if isinstance(grant, dict) else {}
    grant_id = grant.get('id')
    _audit_capability(
        request,
        action='fleet.agent.unarchive',
        target_type='agent',
        target_key=target,
        approval_ref=str(grant_id or ''),
        outcome=result,
        reason=reason,
        after={
            'action': 'fleet.agent.unarchive',
            'result': result,
            'target': target,
            'scope': scope,
            'grant_id': grant_id,
            'caller_user_id': getattr(getattr(request, 'user', None), 'id', None),
            'caller_email': str(getattr(getattr(request, 'user', None), 'email', '') or ''),
            'organization_id': grant.get('organization'),
        },
    )


def _audit_fleet_read_jit(request, *, result, reason, grant=None):
    """Record a redacted allow/deny decision for the fleet-read boundary."""
    grant = grant if isinstance(grant, dict) else {}
    grant_id = grant.get('id')
    _audit_capability(
        request,
        action='fleet.read',
        target_type='fleet',
        target_key='all',
        approval_ref=str(grant_id or ''),
        outcome=result,
        reason=reason,
        after={
            'action': 'fleet.read',
            'result': result,
            'scope': 'hive.fleet.read',
            'grant_id': grant_id,
            'caller_user_id': getattr(getattr(request, 'user', None), 'id', None),
            'caller_email': str(getattr(getattr(request, 'user', None), 'email', '') or ''),
            'organization_id': grant.get('organization'),
        },
    )


def _request_org_slug(request, data=None):
    source = data if isinstance(data, dict) else {}
    query = getattr(request, 'query_params', None) or getattr(request, 'GET', {})
    raw = (
        query.get('org_slug')
        or query.get('organization_slug')
        or source.get('org_slug')
        or source.get('organization_slug')
        or ''
    )
    slug = str(raw).strip()
    if not slug or slug in {'__all__', 'all', '*'}:
        return ''
    return slug


def _agent_server_resolved_org_slug(agent):
    for key in ('tenant_org_slug', 'organization_slug', 'org_slug'):
        slug = str((agent or {}).get(key) or '').strip()
        if slug and slug not in {'__all__', 'all', '*'}:
            return slug
    return ''


def _agent_is_explicit_internal(agent):
    """Return True only for server-owned platform/internal identities.

    HIVE-446's privilege boundary must not treat a missing customer tenant slug as
    internal. Legacy FleetAgent rows are Hive-managed platform agents, and the
    current internal runtime fleet uses Shizuha-owned email domains. Runtime or
    external agents outside those explicit signals default to the customer-deny
    sentinel until a server-resolved org slug is supplied.
    """
    if (agent or {}).get('source') == 'fleetagent':
        return True
    email = str((agent or {}).get('email') or '').strip().lower()
    if (agent or {}).get('source') == 'runtime' and (agent or {}).get('email_synthesized'):
        return False
    return email.endswith('@shizuha.com') or email.endswith('@agents.shizuha.io')


def _capability_tenant_org_slug(request, agent):
    explicit = _request_org_slug(request)
    if explicit:
        return explicit
    resolved = _agent_server_resolved_org_slug(agent)
    if resolved:
        return resolved
    if _agent_is_explicit_internal(agent):
        return ''
    return _UNKNOWN_CUSTOMER_TENANT_SCOPE


def _admin_service_headers():
    """Service-token headers for Admin's network-internal API (PLAT-4200).

    Internal routes bypass Admin's ServiceAccessMiddleware (which gates the
    public /organizations/ list on the 'admin' service grant) and are
    ClusterIP-only.  The shared token is optional in compat mode
    (ADMIN_INTERNAL_SERVICE_TOKEN_ENFORCE=false); the X-Internal-Service name
    is always required.
    """
    headers = {
        'Content-Type': 'application/json',
        'X-Internal-Service': 'shizuha-hive',
    }
    token = str(getattr(settings, 'ADMIN_INTERNAL_SERVICE_TOKEN', '') or '').strip()
    if token:
        headers['X-Internal-Service-Token'] = token
    return headers


def _admin_organizations(request):
    """Organizations visible to the caller, org-scoped (HIVE-2057).

    Uses Admin's INTERNAL user-organizations endpoint (service-token auth,
    ClusterIP-only, org-scoped) instead of the public /organizations/ list,
    which Admin's ServiceAccessMiddleware gates on the 'admin' service grant.
    A non-staff user with hive entitlement must get their own org-scoped orgs
    (200), not a confusing "Admin API rejected roster update" 403 from the
    staff-gated public endpoint.
    """
    user_id = getattr(request.user, 'id', None)
    if user_id is None:
        return None, Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
    email = getattr(request.user, 'email', '') or ''
    headers = _admin_service_headers()
    base = getattr(settings, 'HIVE_ADMIN_API_URL', '').rstrip('/')
    try:
        resp = requests.get(
            f'{base}/internal/users/{int(user_id)}/organizations/',
            params={'email': email},
            headers=headers,
            timeout=8,
        )
    except requests.RequestException as exc:
        logger.warning('admin internal organizations failed: %s', exc)
        return None, Response({'error': f'Admin API unreachable: {exc}'}, status=status.HTTP_502_BAD_GATEWAY)
    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except Exception:
            detail = {'error': resp.text[:300]}
        return None, Response({'error': 'Admin API rejected roster update', 'detail': detail}, status=resp.status_code)
    try:
        payload = resp.json() or {}
    except ValueError:
        payload = {}
    orgs = payload.get('organizations') or []
    return orgs, None


def _pulse_api_url():
    return getattr(settings, 'HIVE_PULSE_API_URL', '').rstrip('/')


def _normalise_slug(name):
    value = (name or '').strip().lower().replace('_', '-').replace(' ', '-')
    # Collapse repeated separators so "Human  Ops" == "human-ops".
    return re.sub(r'-{2,}', '-', value).strip('-')


def _canon_capability_slug(name):
    """Capabilities are independent tags; team aliases must not rewrite them."""
    return _normalise_slug(name)


def _canon_team_name(name):
    value = _normalise_slug(name)
    return {
        'admin_ops': 'admin-ops',
        # PLS-539: CEO Decisions is a real org-scoped team. Never fold it into
        # physical-only Human Ops — that hid the team on /hive/agents.
        'leads': 'human-ops',
        'triage': 'human-ops',
        'security-triage': 'security',
        # Operator-visible dupe fix (2026-07-05): "Humanops" and "Human Ops"
        # rendered as two separate team groups on /hive/agents — sources disagree
        # on separators (Pulse roster 'human-ops' vs an unseparated 'humanops').
        # Alias the compact forms of known multi-word teams to the canonical
        # slug so the whole class is closed, not just this instance.
        'humanops': 'human-ops',
        'adminops': 'admin-ops',
        'clustermanagers': 'cluster-managers',
        'socialmedia': 'social-media',
        'customersupport': 'customer-support',
        'programmanagement': 'program-management',
        'executivecouncil': 'executive-council',
        'riskvalidation': 'risk-validation',
        'tradingops': 'trading-ops',
        'tradingengineering': 'trading-engineering',
        # Operator 2026-07-16: one Research team. Keep the independent
        # `research`/`analytics` capability tags intact via the separate
        # _canon_capability_slug helper above.
        'analytics': 'research-analytics',
        'research': 'research-analytics',
        'researchanalytics': 'research-analytics',
    }.get(value, value)


def _display_team_name(team_key):
    canonical = _canon_team_name(team_key)
    if canonical == 'research-analytics':
        return 'Research'
    if canonical == 'ceo-decisions':
        return 'CEO Decisions'
    return ' '.join(part.capitalize() for part in (team_key or '').replace('_', '-').split('-') if part)


def _team_key_from_payload(item):
    if not isinstance(item, dict):
        return ''
    raw = (
        item.get('team_name')
        or item.get('slug')
        or item.get('assignment_group')
        or item.get('key')
        or item.get('id')
        or item.get('name')
    )
    return _canon_team_name(str(raw) if raw is not None else '')


def _org_slug_from_payload(item, default=''):
    if not isinstance(item, dict):
        return str(default or '').strip().lower()
    nested = item.get('organization') if isinstance(item.get('organization'), dict) else {}
    raw = (
        item.get('organization_slug')
        or item.get('org_slug')
        or item.get('tenant_org_slug')
        or nested.get('slug')
        or default
        or ''
    )
    slug = str(raw).strip().lower()
    return '' if slug in {'__all__', 'all', '*'} else slug


def _org_id_from_payload(item, default=None):
    if not isinstance(item, dict):
        return default
    nested = item.get('organization') if isinstance(item.get('organization'), dict) else {}
    raw = item.get('organization_id') or item.get('org_id') or nested.get('id')
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _org_name_from_payload(item, default=''):
    if not isinstance(item, dict):
        return str(default or '')
    nested = item.get('organization') if isinstance(item.get('organization'), dict) else {}
    return str(
        item.get('organization_name')
        or item.get('org_name')
        or item.get('tenant_org_name')
        or nested.get('display_name')
        or nested.get('name')
        or default
        or ''
    ).strip()


def _friendly_team_display_name(team_key, display_name=None):
    display = str(display_name or '').strip()
    canonical = _canon_team_name(team_key)
    if display and _canon_team_name(display) != canonical:
        return display
    return _display_team_name(canonical)


def _roster_lead_email(roster):
    if not isinstance(roster, dict):
        return ''
    explicit = str(roster.get('lead_email') or '').strip().lower()
    if explicit:
        return explicit
    for tier in roster.get('tiers') or []:
        for member in tier or []:
            email = _membership_email(member)
            if email:
                return email
    return ''


def _normalise_team_payload(item):
    """Return a Hive UI-friendly TeamRoster shape without changing Pulse SSOT."""
    if not isinstance(item, dict):
        return item
    team = dict(item)
    team_key = _team_key_from_payload(team)
    if not team_key:
        return team
    display = _friendly_team_display_name(team_key, team.get('display_name') or team.get('label') or team.get('name'))
    team['id'] = team_key
    team['slug'] = team_key
    team['team_name'] = team_key
    team.setdefault('name', display)
    team['display_name'] = display
    team['lead_email'] = _roster_lead_email(team)
    return team


def _normalise_and_coalesce_team_rows(rows):
    """Expose one roster per canonical organization/team identity.

    Pulse intentionally retains inactive legacy rows as rollback tombstones.  A
    team alias must not make those rows look like additional canonical teams at
    the Hive boundary.  Prefer an active row, then the row whose persisted name
    is already canonical; keep the first row only as a deterministic fallback.
    """
    selected = {}
    order = []
    for raw in rows or []:
        normalised = _normalise_team_payload(raw)
        if not isinstance(normalised, dict):
            continue
        team_key = _team_key_from_payload(raw)
        org_slug = _org_slug_from_payload(raw, default='shizuha')
        identity = (org_slug, team_key)
        if identity not in selected:
            order.append(identity)
            selected[identity] = (raw, normalised)
            continue

        current_raw, _current = selected[identity]
        raw_is_active = bool(raw.get('is_active')) if isinstance(raw, dict) else False
        current_is_active = bool(current_raw.get('is_active')) if isinstance(current_raw, dict) else False
        raw_is_canonical = _normalise_slug((raw or {}).get('team_name')) == team_key
        current_is_canonical = _normalise_slug((current_raw or {}).get('team_name')) == team_key
        if (raw_is_active, raw_is_canonical) > (current_is_active, current_is_canonical):
            selected[identity] = (raw, normalised)

    return [selected[identity][1] for identity in order]


def _normalise_team_collection_payload(payload):
    if isinstance(payload, list):
        return _normalise_and_coalesce_team_rows(payload)
    if not isinstance(payload, dict):
        return payload or {}

    out = dict(payload)
    source_key = None
    for key in ('teams', 'results', 'rosters'):
        if isinstance(out.get(key), list):
            source_key = key
            break
    if source_key:
        teams = _normalise_and_coalesce_team_rows(out.get(source_key) or [])
        out[source_key] = teams
        # The frontend consumes `teams` first, so expose it even when Pulse returns
        # paginated `results` or legacy `rosters`. Preserve the original key too.
        out['teams'] = teams
        return out

    # Some Pulse endpoints return a single roster detail object rather than a wrapper.
    if out.get('team_name') or out.get('tiers') is not None:
        return _normalise_team_payload(out)
    return out


# Backwards-compatible name used by write paths/tests from the HIVE-271 branch.
def _normalise_team_roster_payload(payload):
    return _normalise_team_collection_payload(payload)


def _membership_email(item):
    if isinstance(item, str):
        return item.strip().lower()
    if isinstance(item, dict):
        return str(
            item.get('email')
            or item.get('user_email')
            or item.get('agent_email')
            or item.get('member_email')
            or item.get('username')
            or ''
        ).strip().lower()
    return ''


def _add_membership(out, seen, member, *, team_key='', tier=None, role='member', is_junior=False):
    email = _membership_email(member)
    if not email or email in seen:
        return
    item = dict(member) if isinstance(member, dict) else {'email': email}
    item['email'] = email
    if team_key:
        item['team_name'] = team_key
    if tier is not None:
        item['tier'] = tier
        item['tier_index'] = tier
    item.setdefault('role', role)
    item['routing_health'] = routing_health_for_membership(item)
    item['is_junior'] = bool(is_junior)
    out.append(item)
    seen.add(email)


def _normalise_team_members_payload(payload, team_key):
    if not isinstance(payload, dict):
        return payload or {}
    out = dict(payload)
    team_key = _canon_team_name(team_key)
    memberships = []
    seen = set()

    for item in out.get('memberships') or []:
        _add_membership(memberships, seen, item, team_key=team_key)

    roster_sources = []
    if isinstance(out.get('roster'), dict):
        roster_sources.append(out['roster'])
    if isinstance(out.get('team'), dict):
        roster_sources.append(out['team'])
    roster_sources.extend(out.get('rosters') or [])

    selected_roster = None
    for roster in roster_sources:
        if not isinstance(roster, dict):
            continue
        roster_key = _team_key_from_payload(roster)
        if team_key and roster_key and roster_key != team_key:
            continue
        selected_roster = _normalise_team_payload(roster)
        junior_emails = {_membership_email(member) for member in roster.get('junior_emails') or []}
        for tier_idx, tier_members in enumerate(roster.get('tiers') or []):
            for member in tier_members or []:
                _add_membership(
                    memberships,
                    seen,
                    member,
                    team_key=team_key,
                    tier=tier_idx,
                    is_junior=_membership_email(member) in junior_emails,
                )
        junior_tier = len(roster.get('tiers') or [])
        for member in roster.get('junior_emails') or []:
            _add_membership(memberships, seen, member, team_key=team_key, tier=junior_tier, is_junior=True)
        for field in ('members', 'member_emails', 'agents', 'users'):
            for member in roster.get(field) or []:
                _add_membership(memberships, seen, member, team_key=team_key)
        # `lead_email` is directory/display metadata.  A lead is a member only
        # when Pulse includes them in tiers/junior_emails (the authoritative
        # roster fields); never manufacture placement from a label.

    out['team_name'] = team_key
    if selected_roster:
        out['display_name'] = selected_roster.get('display_name') or _display_team_name(team_key)
        out['lead_email'] = selected_roster.get('lead_email') or ''
        out['tiers'] = selected_roster.get('tiers') or []
        out['junior_emails'] = selected_roster.get('junior_emails') or []
        if memberships and not (out.get('memberships') or []):
            out['membership_source'] = 'team_roster_tiers'
    role_edges = _safe_team_role_edges_for_members(team_key, [item.get('email') for item in memberships])
    if role_edges:
        for item in memberships:
            email = item.get('email')
            edge = role_edges.get((email, team_key))
            if edge:
                item.update({
                    'role_title': edge.role_title or item.get('role') or 'member',
                    'membership_role': edge.membership_role,
                    'tier': edge.tier or item.get('tier') or '',
                    'team_role_source': edge.source or 'hive',
                })
    out['memberships'] = memberships
    return out


# Backwards-compatible name used by tests from the HIVE-271 branch.
def _team_members_payload_from_rosters(payload, team_key):
    return _normalise_team_members_payload(payload, team_key)


def _pulse_request(request, method, path, *, params=None, json=None):
    headers = _auth_headers_for_admin(request)
    if not headers:
        return None, Response({'error': 'No bearer token available for Pulse roster update'}, status=status.HTTP_401_UNAUTHORIZED)
    base = _pulse_api_url()
    if not base:
        return None, Response({'error': 'Pulse API URL is not configured'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    try:
        resp = requests.request(
            method,
            f'{base}{path}',
            headers=headers,
            params=params,
            json=json,
            timeout=8,
        )
    except requests.RequestException as exc:
        logger.warning('pulse roster request failed: %s %s: %s', method, path, exc)
        return None, Response({'error': f'Pulse roster API unreachable: {exc}'}, status=status.HTTP_502_BAD_GATEWAY)
    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except Exception:
            detail = {'error': resp.text[:300]}
        return None, Response({'error': 'Pulse roster API rejected update', 'detail': detail}, status=resp.status_code)
    if resp.status_code == 204:
        return {}, None
    try:
        return resp.json(), None
    except ValueError:
        return {}, None


def _pulse_roster_membership_rows(request, email, org_slug=''):
    payload, error = _pulse_request(
        request,
        'GET',
        '/admin/team-roster/members/',
        params={'email': email},
    )
    if error:
        return None, None, error
    org_scope = str(org_slug or '').strip().lower()
    membership_rows = [
        item for item in (payload or {}).get('memberships') or []
        if not org_scope or _org_slug_from_payload(item, default='shizuha') == org_scope
    ]
    roster_rows = [
        item for item in (payload or {}).get('rosters') or []
        if not org_scope or _org_slug_from_payload(item, default='shizuha') == org_scope
    ]
    normalized_memberships = [
        {
            **item,
            'email': str(email or '').strip().lower(),
            'team_slug': _canon_team_name(item.get('team_slug') or item.get('team_name')),
            'organization_slug': _org_slug_from_payload(item, default='shizuha'),
        }
        for item in membership_rows
        if _canon_team_name(item.get('team_slug') or item.get('team_name'))
    ]
    return normalized_memberships, roster_rows, None


def _pulse_roster_memberships(request, email, org_slug=''):
    membership_rows, roster_rows, error = _pulse_roster_membership_rows(
        request,
        email,
        org_slug=org_slug,
    )
    if error:
        return None, None, error
    memberships = _OrganizationMembershipSet({
        _canon_team_name(item.get('team_slug') or item.get('team_name'))
        for item in membership_rows
        if item.get('team_slug') or item.get('team_name')
    }, organization_rows=membership_rows)
    roster_teams = {
        _canon_team_name(item.get('team_slug') or item.get('team_name'))
        for item in roster_rows
        if item.get('team_slug') or item.get('team_name')
    }
    return memberships, roster_teams, None


def _pulse_roster_team_options(request, email, org_slug=''):
    payload, error = _pulse_request(
        request,
        'GET',
        '/admin/team-roster/members/',
        params={'email': email},
    )
    if error:
        return None, None, error
    org_scope = str(org_slug or '').strip().lower()
    membership_rows = [
        item for item in (payload or {}).get('memberships') or []
        if not org_scope or _org_slug_from_payload(item, default='shizuha') == org_scope
    ]
    roster_rows = [
        item for item in (payload or {}).get('rosters') or []
        if not org_scope or _org_slug_from_payload(item, default='shizuha') == org_scope
    ]
    membership_keys = {
        (_org_slug_from_payload(item, default='shizuha'), _canon_team_name(item.get('team_name')))
        for item in membership_rows
        if _canon_team_name(item.get('team_name'))
    }
    teams = [
        {
            # Team names are only unique inside one organization.  Keep the
            # global picker identity qualified; scoped pickers retain the
            # convenient team slug expected by their URLs.
            'id': team_key if org_scope else f'{team_org}::{team_key}',
            'name': _display_team_name(team_key),
            'slug': team_key,
            'team_name': team_key,
            'organization_slug': team_org,
            'organization_name': _org_name_from_payload(roster),
            'membership_source': 'pulse_roster',
        }
        for roster in roster_rows
        for team_org, team_key in [(
            _org_slug_from_payload(roster, default='shizuha'),
            _canon_team_name(roster.get('team_name')),
        )]
        if team_key
    ]
    teams.sort(key=lambda item: (item['organization_slug'], item['team_name']))
    return teams, membership_keys, None


_ROSTER_CONFLICT_VERSION_RE = re.compile(r'Roster version is (\d+)')


def _pulse_roster_current_version(request, *, team_name, org_slug):
    """Read the roster row's CAS version for one (org, team); None if unknown.

    Returns 0 when no roster row exists — PLS-745 defines ``expected_version=0``
    as the create-if-absent boundary. Returns None when the version cannot be
    read (fetch error, or a Pulse build that predates ``version`` in the GET
    payload); callers then rely on the conflict-retry to learn the real version.
    """
    payload, error = _pulse_request(
        request,
        'GET',
        '/admin/team-roster/members/',
        params={},
    )
    if error:
        return None
    want = (
        _canon_team_name(team_name),
        str(org_slug or 'shizuha').strip().lower(),
    )
    for roster in _items(payload, 'rosters'):
        key = (
            _canon_team_name(roster.get('team_name')),
            str(roster.get('organization_slug') or 'shizuha').strip().lower(),
        )
        if key == want:
            try:
                return int(roster.get('version'))
            except (TypeError, ValueError):
                return None
    return 0


def _write_pulse_roster_membership(request, *, action, team_name, email, org_slug=''):
    # PLS-745 made this endpoint a payload-bound CAS command: it REQUIRES a UUID
    # ``command_id`` and the roster row's current ``version`` as
    # ``expected_version``. The prior payload sent neither, so every Hive team
    # placement (UI and MCP) failed with command_id_invalid from the moment
    # PLS-745 landed. Read the version first; on a stale-CAS conflict the error
    # detail carries the authoritative version, so retry once with that and a
    # FRESH command_id (a command_id is bound to its exact payload).
    expected = _pulse_roster_current_version(
        request, team_name=team_name, org_slug=org_slug or 'shizuha',
    )
    if expected is None:
        expected = 0
    result = None
    error = None
    for _attempt in range(2):
        payload = {
            'action': action,
            'team_name': team_name,
            'command_id': str(uuid.uuid4()),
            'expected_version': int(expected),
        }
        if email:
            payload['email'] = email
        if org_slug:
            payload['organization_slug'] = str(org_slug).strip().lower()
        result, error = _pulse_request(
            request,
            'POST',
            '/admin/team-roster/members/',
            json=payload,
        )
        if error is None:
            return result, None
        detail = ''
        try:
            data = getattr(error, 'data', None) or {}
            inner = data.get('detail')
            if isinstance(inner, dict):
                detail = str(inner.get('detail') or '')
            else:
                detail = str(inner or '')
        except Exception:
            detail = ''
        match = _ROSTER_CONFLICT_VERSION_RE.search(detail)
        if not match:
            return result, error
        expected = int(match.group(1))
    return result, error


def _create_pulse_roster_team(request, team_name, org_slug=''):
    return _write_pulse_roster_membership(
        request,
        action='create',
        team_name=team_name,
        email='',
        org_slug=org_slug,
    )


def _items(payload, key=None):
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    if key and isinstance(payload.get(key), list):
        return payload[key]
    if isinstance(payload.get('results'), list):
        return payload['results']
    return []


# CON-176: last-known-good roster index, served when the live Pulse fetch fails.
# A transient Pulse 401/timeout used to collapse the ENTIRE fleet view to
# "Unassigned"/zero-workload — silently-wrong data on the primary fleet console
# (operator saw the whole org rostered as Unassigned). A roster fetch failure is
# a fetch failure, not an empty roster: serve the last good copy (age exposed so
# the API/UI can banner the degradation honestly) and log at WARNING, never debug.
# CON-176 v2 (operator directive 2026-07-05: "solve this permanently, extremely
# fault tolerant — hive can maintain a cache of pulse rosters in case pulse is
# not accessible"). The v1 cache was an in-PROCESS dict, so it was EMPTY on every
# hive pod restart/redeploy — which is exactly when the operator kept hitting a
# blank "Unassigned" fleet (a deploy roll = fresh pod = empty stale copy = one
# unlucky Pulse hiccup blanks the console). v2 persists the last-good roster in
# REDIS (django cache, shared across replicas AND surviving restarts), so a
# freshly-rolled pod serves the last-known-good roster on its very first request.
from django.core.cache import cache as _django_cache

_ROSTER_CACHE_KEY = 'hive:pulse-roster-index:v2'
_ROSTER_CACHE_META_KEY = 'hive:pulse-roster-index:v2:meta'
_ROSTER_MEMBERSHIP_CACHE_PREFIX = 'hive:pulse-roster-memberships:v1'
_ROSTER_INDEX_STALE_MAX_AGE_S = 24 * 3600  # persisted copy is trusted up to 24h
_ROSTER_CACHE_TTL_S = 7 * 24 * 3600        # keep in redis a week (>> max serve age)
# The process copy remains the dependency-free hot path, but periodically checks
# the bounded shared cache so another Daphne replica's mutation converges before
# rt-fleet's five-minute repair pass.
_ROSTER_PROCESS_RECHECK_AGE_S = 60
# In-process degradation flag for the CURRENT request cycle only (for the UI banner);
# the DATA itself lives in redis so it survives restarts.
_ROSTER_LAST_SERVE = {'degraded': False, 'built_at': 0.0}
_ROSTER_INDEX_STALE_LOCK = threading.Lock()
_ROSTER_PROCESS_SNAPSHOT = {'index': None, 'built_at': 0.0, 'entry_built_at': {}}


def _roster_process_read():
    """Return the in-process last-good roster without dependency I/O."""
    with _ROSTER_INDEX_STALE_LOCK:
        return _ROSTER_PROCESS_SNAPSHOT['index'], _ROSTER_PROCESS_SNAPSHOT['built_at']


def _roster_process_entry_read(email):
    """Return one process-local roster entry and its most recent refresh time."""
    index, built_at = _roster_process_read()
    if index is None:
        return None, built_at
    email = str(email or '').strip().lower()
    with _ROSTER_INDEX_STALE_LOCK:
        entry_built_at = _ROSTER_PROCESS_SNAPSHOT['entry_built_at'].get(email, built_at)
    return index.get(email, []), entry_built_at


def _roster_process_write(index, built_at):
    """Publish a last-good roster atomically for daemon capability hot reads."""
    if index is None:
        return
    with _ROSTER_INDEX_STALE_LOCK:
        _ROSTER_PROCESS_SNAPSHOT['index'] = index
        _ROSTER_PROCESS_SNAPSHOT['built_at'] = float(built_at or time.time())
        _ROSTER_PROCESS_SNAPSHOT['entry_built_at'] = {}


def _roster_process_membership_write(email, rows, built_at):
    """Atomically overlay one authoritative membership entry in this process."""
    email = str(email or '').strip().lower()
    if not email or rows is None:
        return
    built_at = float(built_at or time.time())
    with _ROSTER_INDEX_STALE_LOCK:
        current = _ROSTER_PROCESS_SNAPSHOT['index'] or {}
        updated = dict(current)
        updated[email] = list(rows)
        _ROSTER_PROCESS_SNAPSHOT['index'] = updated
        _ROSTER_PROCESS_SNAPSHOT['built_at'] = max(
            float(_ROSTER_PROCESS_SNAPSHOT['built_at'] or 0.0), built_at,
        )
        _ROSTER_PROCESS_SNAPSHOT['entry_built_at'][email] = built_at


def _roster_membership_cache_key(email):
    digest = hashlib.sha256(str(email or '').strip().lower().encode()).hexdigest()
    return f'{_ROSTER_MEMBERSHIP_CACHE_PREFIX}:{digest}'


def _roster_membership_cache_read(email):
    """Read one replica-safe membership snapshot from bounded Redis; never raise."""
    try:
        payload = _django_cache.get(_roster_membership_cache_key(email))
        if isinstance(payload, dict) and isinstance(payload.get('rows'), list):
            return payload['rows'], float(payload.get('built_at') or 0.0)
    except Exception as exc:
        logger.warning('roster membership redis read failed (preserving process snapshot): %s', exc)
    return None, 0.0


def _roster_membership_cache_write(email, rows):
    """Publish one membership snapshot for convergence across Daphne replicas."""
    built_at = time.time()
    _roster_process_membership_write(email, rows, built_at)
    try:
        _django_cache.set(
            _roster_membership_cache_key(email),
            {'rows': list(rows), 'built_at': built_at},
            _ROSTER_CACHE_TTL_S,
        )
    except Exception as exc:
        logger.warning('roster membership redis write failed (process snapshot remains fresh): %s', exc)


def _refresh_roster_membership_snapshot(request, email):
    """Fetch and publish one post-mutation Pulse membership snapshot."""
    payload, error = _pulse_request(
        request,
        'GET',
        '/admin/team-roster/members/',
        params={'email': email},
    )
    if error:
        return error
    email = str(email or '').strip().lower()
    rows = [
        {
            'email': email,
            'team_id': _canon_team_name(item.get('team_name')),
            'team_name': _display_team_name(_canon_team_name(item.get('team_name'))),
            'team_slug': _canon_team_name(item.get('team_name')),
            'membership_source': 'pulse_roster',
            'organization_slug': _org_slug_from_payload(item, default='shizuha'),
            'organization_name': _org_name_from_payload(item),
            'organization_id': _org_id_from_payload(item),
        }
        for item in (payload or {}).get('memberships') or []
        if _canon_team_name(item.get('team_name'))
    ]
    _roster_membership_cache_write(email, rows)
    return None


def _roster_cache_read():
    """(index_or_None, built_at_epoch) of the last-good roster from redis; never raises."""
    try:
        idx = _django_cache.get(_ROSTER_CACHE_KEY)
        meta = _django_cache.get(_ROSTER_CACHE_META_KEY) or {}
        built_at = float(meta.get('built_at', 0.0))
        if idx is not None:
            _roster_process_write(idx, built_at)
            return idx, built_at
    except Exception as exc:  # redis down → fall through to live-only, never error the page
        logger.warning('roster redis cache read failed (continuing live-only): %s', exc)
    # Redis is an optimization.  Preserve a process-local last-good snapshot
    # across transient read failures instead of erasing the hot-path state.
    return _roster_process_read()


def _roster_cache_write(index):
    built_at = time.time()
    _roster_process_write(index, built_at)
    try:
        _django_cache.set(_ROSTER_CACHE_KEY, index, _ROSTER_CACHE_TTL_S)
        _django_cache.set(_ROSTER_CACHE_META_KEY, {'built_at': built_at}, _ROSTER_CACHE_TTL_S)
    except Exception as exc:
        logger.warning('roster redis cache write failed (fresh data still served this request): %s', exc)


def pulse_roster_index_degradation():
    """(is_degraded, stale_age_seconds) of the most recent roster index serve."""
    with _ROSTER_INDEX_STALE_LOCK:
        built = _ROSTER_LAST_SERVE['built_at']
        age = (time.time() - built) if built else None
        return bool(_ROSTER_LAST_SERVE['degraded']), age


def _service_pulse_roster_index():
    """email -> TeamRoster memberships built from Pulse's NETWORK-SECURED internal
    roster endpoint (no user token). HIVE-617: lets a NON-STAFF owner see the
    teams their OWN agents are placed in — `_attach_pulse_team_memberships` only
    attaches these to agents already in the (owner-scoped) response, so no
    cross-tenant data is exposed. The user-token'd `/admin/team-roster/members/`
    read 403s for non-staff; this is the fallback that keeps grouping working."""
    base = getattr(settings, 'HIVE_PULSE_API_URL', '').rstrip('/')
    if not base:
        return {}
    cache_key = 'hive:service-pulse-roster-index'
    try:
        cached = _django_cache.get(cache_key)
    except Exception:
        cached = None
    if isinstance(cached, dict):
        return cached
    try:
        resp = requests.get(f'{base}/internal/team-roster/', timeout=2)
        if resp.status_code >= 400:
            logger.info('service roster index: internal endpoint status %s', resp.status_code)
            return {}
        teams = (resp.json() or {}).get('teams') or []
    except (requests.RequestException, ValueError) as exc:
        logger.info('service roster index fetch failed: %s', exc)
        return {}
    index = {}
    for team in teams:
        team_key = _canon_team_name(team.get('team_name'))
        if not team_key:
            continue
        org_slug = str(team.get('organization_slug') or '').strip().lower()
        org_id = _org_id_from_payload(team)
        org_name = str(
            team.get('organization_name')
            or team.get('org_name')
            or org_slug
            or ''
        ).strip()
        emails = []
        seen_emails = set()
        for tier in team.get('tiers') or []:
            for member in tier or []:
                email = (member.get('email') if isinstance(member, dict) else str(member)).strip().lower()
                if email:
                    emails.append(email)
        for member in team.get('junior_emails') or []:
            email = str(member).strip().lower()
            if email:
                emails.append(email)
        for email in emails:
            if not email or email in seen_emails:
                continue
            seen_emails.add(email)
            item = {
                'email': email,
                'team_id': team_key,
                'team_name': _display_team_name(team_key),
                'team_slug': team_key,
                'role': 'member',
                'membership_source': 'pulse_roster',
                'organization_slug': org_slug,
                'organization_name': org_name or org_slug,
                'organization_id': org_id,
            }
            item['routing_health'] = routing_health_for_membership(item)
            index.setdefault(email, []).append(item)
    try:
        _django_cache.set(cache_key, index, 30)
    except Exception:
        pass
    return index


def _pulse_roster_team_index(request):
    """Return email -> Pulse TeamRoster memberships for fleet list grouping."""
    payload, error = _pulse_request(request, 'GET', '/admin/team-roster/members/')
    if error:
        detail = getattr(error, 'data', error)
        status_code = getattr(error, 'status_code', None)
        # HIVE-617: the admin members read is staff-gated; for a non-staff OWNER
        # viewing their own agents, fall back to the network-secured service
        # roster so their agents still group by team (tenant-safe — only their
        # own agents get enriched downstream). Prefer this fresh copy over stale.
        service_index = _service_pulse_roster_index()
        if service_index:
            # Do NOT write this into the shared stale cache — it is slightly
            # leaner than the staff-path data (role=member, org name=slug); keep
            # the shared cache staff-quality and just serve this fresh per build.
            with _ROSTER_INDEX_STALE_LOCK:
                _ROSTER_LAST_SERVE['degraded'] = False
                _ROSTER_LAST_SERVE['built_at'] = time.time()
            return service_index
        stale, built_at = _roster_cache_read()
        stale_age = time.time() - built_at if built_at else float('inf')
        with _ROSTER_INDEX_STALE_LOCK:
            _ROSTER_LAST_SERVE['degraded'] = True
            _ROSTER_LAST_SERVE['built_at'] = built_at
        if stale is not None and stale_age < _ROSTER_INDEX_STALE_MAX_AGE_S:
            logger.warning(
                'Pulse team index fetch failed (status=%s) — serving REDIS-persisted roster from %.0fs ago: %s',
                status_code, stale_age, detail,
            )
            return stale
        logger.warning(
            'Pulse team index fetch failed (status=%s) with no usable persisted copy — fleet list will show Unassigned: %s',
            status_code, detail,
        )
        return {}
    index = {}
    for roster in (payload or {}).get('rosters') or []:
        team_key = _canon_team_name(roster.get('team_name'))
        if not team_key:
            continue
        members = []
        seen_emails = set()
        for tier_idx, tier in enumerate(roster.get('tiers') or []):
            for member in tier or []:
                item = dict(member) if isinstance(member, dict) else {'email': str(member).strip().lower()}
                item.setdefault('tier', tier_idx)
                members.append(item)
        for member in roster.get('junior_emails') or []:
            members.append({'email': str(member).strip().lower()})
        # `lead_email` is descriptive metadata, not a membership edge.  Pulse
        # tiers/junior_emails are the complete authoritative placement answer.
        for member in members:
            email = _membership_email(member)
            if not email or email in seen_emails:
                continue
            seen_emails.add(email)
            org_slug = _org_slug_from_payload(member, default=_org_slug_from_payload(roster))
            org_name = _org_name_from_payload(member, default=_org_name_from_payload(roster))
            item = {
                **member,
                'email': email,
                'team_id': team_key,
                'team_name': _display_team_name(team_key),
                'team_slug': team_key,
                'role': member.get('role') or 'member',
                'membership_source': 'pulse_roster',
                'organization_slug': org_slug,
                'organization_name': org_name,
                'organization_id': _org_id_from_payload(
                    member, default=_org_id_from_payload(roster),
                ),
            }
            item['routing_health'] = routing_health_for_membership(item)
            index.setdefault(email, []).append(item)
    # Empty is a valid authoritative answer (everyone unassigned).  Publishing
    # it is required so a prior cached placement cannot resurrect after removal.
    _roster_cache_write(index)
    with _ROSTER_INDEX_STALE_LOCK:
        _ROSTER_LAST_SERVE['degraded'] = False
        _ROSTER_LAST_SERVE['built_at'] = time.time()
    return index


def _membership_names(memberships):
    return sorted({
        str(item.get('team_slug') or item.get('team_name')).lower()
        for item in memberships
        if item.get('team_slug') or item.get('team_name')
    })


def _memberships_for_agent(index, agent, org_slug=''):
    memberships = list(index.get(_agent_email(agent)) or [])
    if org_slug:
        memberships = [
            item for item in memberships
            if _org_slug_from_payload(item, default='shizuha') == org_slug.lower()
        ]
    return memberships


def _write_agent_memberships(agent, memberships, org_slug=''):
    agent['team_memberships'] = memberships
    agent['team_names'] = _membership_names(memberships)
    if org_slug:
        agent['organization_slug'] = org_slug
    return agent


def _attach_pulse_team_memberships(request, agents, org_slug='', *, drop_unscoped=True):
    index = _pulse_roster_team_index(request)
    if not index:
        if org_slug and drop_unscoped:
            return _attach_team_role_summaries([], org_slug=org_slug)
        for agent in agents:
            _write_agent_memberships(agent, [], org_slug=org_slug)
        return _attach_team_role_summaries(agents, org_slug=org_slug)
    scoped = []
    for agent in agents:
        memberships = _memberships_for_agent(index, agent, org_slug=org_slug)
        if org_slug and drop_unscoped and not memberships:
            continue
        _write_agent_memberships(agent, memberships, org_slug=org_slug)
        scoped.append(agent)
    return _attach_team_role_summaries(scoped, org_slug=org_slug)


def _overlay_live_pulse_memberships(agents, org_slug=''):
    """Re-apply Pulse placement on a cached agents-index payload.

    Last-good cache is unversioned (24h). A roster write + invalidate still
    serves the pre-write snapshot until background rebuild lands, which is
    how Hina stayed Unassigned after Pulse already had her on ceo-decisions.
    Copy-on-write; never mutate the shared cached dicts.
    """
    index = _service_pulse_roster_index()
    if not index:
        return agents
    overlaid = []
    for agent in agents:
        memberships = _memberships_for_agent(index, agent, org_slug=org_slug)
        if _membership_names(agent.get('team_memberships') or []) == _membership_names(memberships):
            overlaid.append(agent)
            continue
        overlaid.append(_write_agent_memberships({**agent}, memberships, org_slug=org_slug))
    return overlaid


_TEAM_COVERAGE_PROVIDERS = (
    ('claude_code', 'Claude Code'),
    ('codex', 'Codex'),
    ('grok_build', 'Grok Build'),
)


def _is_k3s_agent(agent):
    runtime_env = str(agent.get('runtime_environment') or '').strip().lower()
    runtime_source = str(agent.get('runtime_source') or '').strip().lower()
    return (
        runtime_env == 'k8s'
        or runtime_source == 'k3s-native'
        or bool(agent.get('k3s_namespace') or agent.get('k3s_deployment'))
    )


def _team_coverage_provider(agent):
    """Return the target build-provider family configured for one agent."""
    method = str(agent.get('execution_method') or '').strip().lower()
    if method == 'claude_code_server':
        return 'claude_code'
    if method == 'codex_app_server':
        return 'codex'
    if method == 'grok_build':
        return 'grok_build'

    text = ' '.join(str(agent.get(key) or '').lower() for key in (
        'runtime_harness', 'runtime_provider', 'model_provider',
        'model', 'effective_model',
    ))
    if any(value in text for value in ('claude', 'anthropic', 'sonnet', 'opus', 'fable')):
        return 'claude_code'
    if any(value in text for value in ('grok', 'xai')):
        return 'grok_build'
    if (
        'codex' in text
        or 'openai' in text
        or any(part.startswith(('gpt', 'o1', 'o3', 'o4')) for part in text.split())
    ):
        return 'codex'
    return ''


def _has_explicit_provider_capacity_limit(agent):
    """True only for an explicit provider/quota/capacity unavailable signal."""
    health = agent.get('health') or {}
    if (
        agent.get('provider_unavailable') is True
        or agent.get('health_token_pool_unavailable') is True
        or health.get('provider_unavailable') is True
        or health.get('token_pool_unavailable') is True
    ):
        return True

    capacity_values = {
        'capacity-limited', 'capacity_limited', 'provider-down', 'provider_down',
        'provider-unavailable', 'provider_unavailable', 'quota-exhausted', 'quota_exhausted',
    }
    for key in (
        'availability_status', 'availability_reason',
        'unavailability_reason', 'health_reason',
    ):
        if str(agent.get(key) or '').strip().lower() in capacity_values:
            return True
    if str(health.get('reason') or '').strip().lower() in capacity_values:
        return True

    issues = (agent.get('health_summary') or {}).get('issues') or []
    return any(
        isinstance(issue, dict)
        and str(issue.get('type') or '').strip().lower() in {
            'provider_unavailable', 'provider_unavailable_or_quota',
        }
        for issue in issues
    )


def _is_effective_target_provider_agent(agent, provider):
    """Effective coverage requires a live, enabled and provider-usable k3s agent."""
    if _team_coverage_provider(agent) != provider or not _is_k3s_agent(agent):
        return False
    status_value = str(agent.get('status') or '').strip().lower()
    if status_value != 'running':
        return False
    if agent.get('enabled') is False:
        return False
    if _has_explicit_provider_capacity_limit(agent):
        return False
    return True


def _is_live_codex_k3s_agent(agent):
    """HIVE-467 compatibility predicate for effective Codex coverage."""
    return _is_effective_target_provider_agent(agent, 'codex')


def _agent_team_slugs(agent):
    slugs = {_canon_team_name(value) for value in (agent.get('team_names') or [])}
    for membership in agent.get('team_memberships') or []:
        if not isinstance(membership, dict):
            continue
        slugs.add(_canon_team_name(
            membership.get('team_slug')
            or membership.get('team_name')
            or membership.get('team_id')
        ))
    return {slug for slug in slugs if slug}


def _fleet_team_coverage(teams, agents):
    """Return configured and effective provider coverage for active teams.

    The inputs are already sourced from Pulse TeamRoster and the live
    agents index. Deliberately ignore local TeamMembershipRole-only rows for
    coverage so stale Hive SQLite/JSON cannot satisfy the fleet invariant.
    """
    active_teams = []
    for team in teams or []:
        if not isinstance(team, dict):
            continue
        if team.get('is_active') is False or team.get('active') is False:
            continue
        slug = _team_key_from_payload(team)
        if slug:
            active_teams.append({
                'team_slug': slug,
                'team_name': team.get('display_name') or team.get('name') or _display_team_name(slug),
            })

    provider_labels = dict(_TEAM_COVERAGE_PROVIDERS)
    configured_by_team = {
        team['team_slug']: {provider: [] for provider in provider_labels}
        for team in active_teams
    }
    effective_by_team = {
        team['team_slug']: {provider: [] for provider in provider_labels}
        for team in active_teams
    }
    for agent in agents or []:
        if not isinstance(agent, dict) or not _is_k3s_agent(agent):
            continue
        provider = _team_coverage_provider(agent)
        if provider not in provider_labels:
            continue
        summary = {
            'agent_username': agent.get('agent_username') or agent.get('username') or '',
            'email': _agent_email(agent),
            'model': agent.get('effective_model') or agent.get('model') or '',
            'runtime_environment': agent.get('runtime_environment') or '',
            'runtime_source': agent.get('runtime_source') or '',
            'status': agent.get('status') or '',
            'enabled': agent.get('enabled') is not False,
            'provider': provider,
            'provider_unavailable': _has_explicit_provider_capacity_limit(agent),
        }
        for slug in _agent_team_slugs(agent):
            if slug not in configured_by_team:
                continue
            configured_by_team[slug][provider].append(summary)
            if _is_effective_target_provider_agent(agent, provider):
                effective_by_team[slug][provider].append(summary)

    rows = []
    for team in active_teams:
        slug = team['team_slug']
        provider_coverage = {}
        for provider, label in _TEAM_COVERAGE_PROVIDERS:
            configured_agents = sorted(
                configured_by_team[slug][provider],
                key=lambda item: item.get('agent_username') or item.get('email') or '',
            )
            effective_agents = sorted(
                effective_by_team[slug][provider],
                key=lambda item: item.get('agent_username') or item.get('email') or '',
            )
            provider_coverage[provider] = {
                'label': label,
                'configured': bool(configured_agents),
                'effective': bool(effective_agents),
                'configured_agents': configured_agents,
                'effective_agents': effective_agents,
            }
        missing_configured = [
            provider for provider in provider_labels
            if not provider_coverage[provider]['configured']
        ]
        missing_effective = [
            provider for provider in provider_labels
            if not provider_coverage[provider]['effective']
        ]
        # HIVE-467 compatibility: the legacy fields continue to describe
        # effective Codex coverage while callers migrate to provider_coverage.
        codex_agents = provider_coverage['codex']['effective_agents']
        rows.append({
            **team,
            'covered': bool(codex_agents),
            'live_codex_k3s_agents': codex_agents,
            'provider_coverage': provider_coverage,
            'configured_all_target_providers': not missing_configured,
            'effective_all_target_providers': not missing_effective,
            'missing_configured_providers': missing_configured,
            'missing_effective_providers': missing_effective,
        })
    rows.sort(key=lambda row: row['team_slug'])
    uncovered = [row['team_slug'] for row in rows if not row['covered']]
    configured_gap_teams = [
        row['team_slug'] for row in rows if not row['configured_all_target_providers']
    ]
    effective_gap_teams = [
        row['team_slug'] for row in rows if not row['effective_all_target_providers']
    ]
    provider_coverage = {}
    for provider, label in _TEAM_COVERAGE_PROVIDERS:
        configured_gaps = [
            row['team_slug'] for row in rows
            if not row['provider_coverage'][provider]['configured']
        ]
        effective_gaps = [
            row['team_slug'] for row in rows
            if not row['provider_coverage'][provider]['effective']
        ]
        provider_coverage[provider] = {
            'label': label,
            'configured_team_count': len(rows) - len(configured_gaps),
            'effective_team_count': len(rows) - len(effective_gaps),
            'configured_gap_team_slugs': configured_gaps,
            'effective_gap_team_slugs': effective_gaps,
        }
    return {
        'ok': not uncovered,
        'active_team_count': len(rows),
        'covered_team_count': len(rows) - len(uncovered),
        'uncovered_team_count': len(uncovered),
        'uncovered_team_slugs': uncovered,
        'target_providers': [
            {'key': provider, 'label': label}
            for provider, label in _TEAM_COVERAGE_PROVIDERS
        ],
        'provider_coverage': provider_coverage,
        'configured_all_provider_team_count': len(rows) - len(configured_gap_teams),
        'effective_all_provider_team_count': len(rows) - len(effective_gap_teams),
        'configured_provider_gap_team_slugs': configured_gap_teams,
        'effective_provider_gap_team_slugs': effective_gap_teams,
        'configured_coverage_ok': not configured_gap_teams,
        'effective_coverage_ok': not effective_gap_teams,
        'teams': rows,
        'invariant': 'every active Pulse team has at least one live Codex-capable k3s agent',
        'provider_invariant': (
            'every active Pulse team has at least one effective Claude Code, Codex, '
            'and Grok Build k3s agent'
        ),
        'source_of_truth': 'Pulse TeamRoster + live FleetAgent/rt-fleet telemetry',
    }


class FleetTeamCoverageView(APIView):
    """GET /v1/fleet/team-coverage/ — validate HIVE-467 team coverage."""

    def get(self, request):
        denied = _require_staff(request)
        if denied:
            return denied
        org_slug = _request_org_slug(request)
        payload, error = _pulse_request(request, 'GET', '/admin/team-roster/')
        if error:
            return error
        normalised = _normalise_team_collection_payload(payload or {})
        teams = normalised.get('teams') if isinstance(normalised, dict) else normalised
        if org_slug:
            teams = [
                team for team in teams or []
                if _org_slug_from_payload(team, default='shizuha') == org_slug.lower()
            ]

        agents = FleetAgentListView()._build_agents_index(
            request,
            include_external=True,
            owner_only=False,
            org_slug=org_slug,
        )
        result = _fleet_team_coverage(teams, agents)
        result['organization_slug'] = org_slug
        return Response(result)


def _team_placement_response(request, org_slug, agent, *, selected_team_id=None):
    agent_email = _agent_email(agent)
    teams, pulse_memberships, error = _pulse_roster_team_options(
        request, agent_email, org_slug=org_slug,
    )
    if error:
        return error
    active = []
    for team in teams:
        team_id = team.get('id')
        team_key = _canon_team_name(team.get('team_name') or team.get('slug') or str(team_id or ''))
        team_org = _org_slug_from_payload(team, default='shizuha')
        selected = (team_org, team_key) in pulse_memberships
        row = {
            **team,
            'selected': selected,
            'membership_id': None,
            'membership_source': 'pulse_roster',
            'roster_exists': True,
        }
        _apply_team_role(
            agent,
            row,
            edge=_team_role_edge_for(
                agent,
                team_key,
                _safe_team_role_edges_for_agents([agent], org_slug=team_org),
            ),
            selected=selected,
        )
        active.append(row)
    return Response({
        'agent': {
            'id': agent.get('id'),
            'agent_username': agent.get('agent_username'),
            'email': agent_email,
            'display_title': _agent_display_title(agent),
            'employee_profile_id': None,
        },
        'teams': active,
        'selected_team_id': selected_team_id,
        'organization_slug': org_slug,
    })


def _all_agent_budgets():
    # Runtime-wins precedence (see FleetAgentListView): a live host-runtime agent
    # is authoritative for any username present in both planes; a stale native k3s
    # row must not shadow it.
    runtime = {a.get('agent_username'): a for a in list_runtime_agents() if a.get('agent_username')}
    tele = {}
    try:
        from .external_agents import agent_telemetry, telemetry_fields
        tele = agent_telemetry() or {}
    except Exception:
        logger.exception('context-budget telemetry overlay failed (non-fatal)')
        telemetry_fields = None  # type: ignore[assignment]

    def _budget_with_telemetry(agent, username):
        row = dict(agent) if isinstance(agent, dict) else agent
        snap = tele.get((username or '').lower()) if tele else None
        if snap and isinstance(row, dict) and telemetry_fields is not None:
            row = dict(row)
            row.update(telemetry_fields(snap, configured=dict(row)))
        return context_budget_for_agent(row)

    budgets = [
        _budget_with_telemetry(agent, agent.agent_username)
        for agent in FleetAgent.objects.exclude(status=FleetAgent.Status.DELETED).order_by('agent_username')
        if agent.agent_username not in runtime
    ]
    budgets.extend(
        _budget_with_telemetry(agent, agent.get('agent_username'))
        for agent in runtime.values()
    )
    budgets.sort(key=lambda item: (item.get('agent') or '').lower())
    return budgets


class FleetContextBudgetView(APIView):
    """GET /v1/fleet/context-budget/ — safe per-agent context budget JSON."""

    def get(self, request):
        denied = _require_staff(request)
        if denied:
            return denied
        budgets = _all_agent_budgets()
        return Response({'budgets': budgets})


class FleetOrganizationListView(APIView):
    """GET /v1/fleet/organizations/ — organizations visible to the caller."""

    def get(self, request):
        if not getattr(request.user, 'is_authenticated', False):
            return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
        # Do not mint personal agents as a side-effect of listing
        # organizations. That GET is on the Agents chrome and spawned
        # `shizuha-<owner_id>` seats into Unassigned (operator 2026-09-04).
        # Explicit ensure remains on PersonalAgentEnsureView / ID activation.
        orgs, error = _admin_organizations(request)
        if error:
            return error
        orgs = orgs or []
        return Response({'organizations': orgs, 'results': orgs})


class FleetContextBudgetMetricsView(APIView):
    """GET /v1/fleet/context-budget/metrics/ — Prometheus text exposition."""

    def get(self, request):
        denied = _require_staff(request)
        if denied:
            return denied
        return HttpResponse(budgets_to_prometheus(_all_agent_budgets()), content_type='text/plain; version=0.0.4; charset=utf-8')


class FleetRecoveryActionMetricsView(APIView):
    """GET /v1/fleet/recovery-actions/metrics/ — HIVE-277 recovery-action liveness.

    PLAT-9202 (leg 1): accepts the shared internal-service token as a Bearer
    credential (HIVE-614 house pattern — the PodMonitor supplies it from
    hive-secrets/internal-auth-token) so Prometheus can scrape the
    hive_wake_deferred_* families (hive#323) and the HIVE-277 liveness
    metrics, which were staff-gated and therefore unscrapable. Staff access
    is unchanged; the view fails closed when the token is not configured.

    DRF-level authentication is DISABLED (full HIVE-614 house shape): the
    default FederatedJWTAuthentication RAISES AuthenticationFailed on a
    non-JWT bearer (the internal token is not a federated JWT), which would
    reject the scrape before get() runs. Both authorizations therefore live
    inside get(): the internal token via hmac.compare_digest, and the staff
    path via a manual FederatedJWTAuthentication.authenticate() (production
    staff access is federated-JWT-claim based, not Django sessions).
    """

    authentication_classes = []
    permission_classes = []

    def get(self, request):
        import hmac

        from django.conf import settings as django_settings

        expected = str(
            getattr(django_settings, 'HIVE_INTERNAL_AUTH_TOKEN', '') or '')
        authorization = str(request.META.get('HTTP_AUTHORIZATION', '') or '')
        supplied = ''
        if authorization.lower().startswith('bearer '):
            supplied = authorization[7:].strip()
        if not supplied:
            supplied = str(
                request.META.get('HTTP_X_HIVE_INTERNAL_TOKEN', '') or '')
        bearer_admitted = bool(
            expected
            and supplied
            and hmac.compare_digest(supplied, expected)
        )
        if not bearer_admitted:
            # Staff path: validate the federated JWT manually — DRF-level
            # authentication is off so the internal token's non-JWT bearer
            # cannot raise inside the default authenticator first.
            from claws.authentication import FederatedJWTAuthentication

            try:
                result = FederatedJWTAuthentication().authenticate(request)
            except Exception:
                result = None
            user = result[0] if result else None
            if not getattr(user, 'is_staff', False):
                return Response(
                    {'error': 'Staff access required'},
                    status=status.HTTP_403_FORBIDDEN,
                )
        from .runtime_fleet import runtime_lane_outbox_to_prometheus
        # HIVE-615 v8 §5.2: the unresolved harness-upgrade failure fact must be
        # scrapeable so a dedicated rule can page on it.
        from .harness_versions import harness_upgrade_failure_to_prometheus
        # PLAT-9052: deferred connect-wakes must be scrapeable -- the DEFER
        # path previously left no durable trace (19 starved wakes / 25h).
        from .wake_defers import wake_defers_to_prometheus
        return HttpResponse(
            recovery_actions_to_prometheus() + runtime_lane_outbox_to_prometheus()
            + harness_upgrade_failure_to_prometheus()
            + wake_defers_to_prometheus(),
            content_type='text/plain; version=0.0.4; charset=utf-8',
        )


class FleetHarnessVersionsView(APIView):
    """GET /v1/fleet/harness-versions/ — HIVE-600 fleet harness-version tracker.

    Per harness (codex / claude-code / gemini / openclaw): the version the fleet
    is currently running (from the agent-runtime image's OCI labels reported by
    the daemon), the latest published version (npm), whether an upgrade is
    available, and which agents use that harness. Lets an admin see version drift
    and roll the whole fleet when a new release lands. Staff-only.
    """

    def get(self, request):
        denied = _require_staff(request)
        if denied:
            return denied
        from .native_runtime_release import enabled
        if enabled():
            return self._native_versions(request)
        from . import harness_versions as hv
        from .runtime_release import (
            RuntimeReleaseProjectionUnavailable,
            get_authoritative_runtime_release,
        )

        try:
            projection = get_authoritative_runtime_release()
        except RuntimeReleaseProjectionUnavailable as exc:
            return Response(
                {'error': str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        daemon = projection.daemon
        desired_image = projection.release.display_tag
        image_ref = str(daemon.agent_runtime_image or '').strip()
        report = daemon.harness_report if isinstance(daemon.harness_report, dict) else {}
        daemon_hint = report.get('daemon_cli')
        if not isinstance(daemon_hint, dict):
            daemon_hint = {}
        # Authoritative: the daemon resolves the agent-runtime image's OCI labels
        # from the node-local registry (Hive is behind default-deny egress). Fall
        # back to Hive reading the registry directly if it ever can. Optional
        # heartbeat subfields are untrusted JSON; malformed non-maps must degrade
        # to discovery instead of crashing the whole endpoint (HIVE-1891).
        image_labels = report.get('image_harness')
        if not isinstance(image_labels, dict):
            image_labels = {}
        if desired_image and image_ref == desired_image:
            # Prefer labels for the proven desired image when the heartbeat
            # still carries empty/stale labels after a successful roll.
            desired_labels = hv.image_harness_labels(desired_image)
            if desired_labels:
                image_labels = desired_labels
        if not image_labels and image_ref:
            image_labels = hv.image_harness_labels(image_ref)

        # Count agents per harness by execution_method across the live fleet view.
        agents = self._fleet_agents(request)
        by_harness: dict[str, list[str]] = {}
        for a in agents:
            method = a.get('execution_method') or ''
            mapped = hv.HARNESS_BY_METHOD.get(method)
            if mapped:
                by_harness.setdefault(mapped[0], []).append(a.get('agent_username'))

        harnesses = []
        for key, (pkg, label) in hv.HARNESS_KEYS.items():
            current = image_labels.get(key) or daemon_hint.get(key)
            if pkg:
                latest = hv.npm_latest(pkg)
            else:
                # SCLI source commits publish their runtime image through the
                # source-build hook. The daemon's local dist build timestamp is
                # not a release version: rebuilding identical source advances
                # it and used to paint a permanent false upgrade in the UI.
                latest = current
            users = sorted(u for u in by_harness.get(key, []) if u)
            harnesses.append({
                'harness': key,
                'label': label,
                'package': pkg or 'shizuha (internal)',
                'current': current,
                'latest': latest,
                'upgrade_available': hv.upgrade_available(current, latest),
                'source': 'image-label' if image_labels.get(key) else ('daemon-host' if daemon_hint.get(key) else 'unknown'),
                'agent_count': len(users),
                'agents': users,
            })
        # Stable, useful ordering: harnesses with agents first, then by label.
        harnesses.sort(key=lambda h: (-h['agent_count'], h['label']))
        return Response({
            'agent_runtime_image': image_ref,
            'desired_runtime_image': desired_image,
            # Desired image is rollout intent; daemon-reported image is completion
            # evidence. Honest during the paced one-at-a-time roll; clears when
            # the exact authoritative daemon reports that reviewed image.
            'rollout_in_progress': bool(
                desired_image
                and not projection.release.matches_observed_image(image_ref)
            ),
            # Hive no longer derives or writes convergence state. rt-fleet owns
            # rollout progress for the reviewed release.
            'rollout_progress': None,
            'daemon_id': daemon.daemon_id,
            'harnesses': harnesses,
        })
    def _native_versions(self, request):
        from .native_runtime_release import runtime_snapshot
        from .runtime_release import RuntimeReleaseProjectionUnavailable
        from . import harness_versions as hv
        try:
            snapshot = runtime_snapshot()
        except RuntimeReleaseProjectionUnavailable as exc:
            return Response({'error': str(exc), 'scope': 'hive-native'}, status=503)
        authority = snapshot['authority']
        target = authority['image'].rsplit(':', 1)[0] + '@' + authority['image_digest']
        active = [r for r in snapshot['census'] if r['replicas'] > 0]
        images = sorted({p['image'] for r in active for p in r['pods']})
        observed = images[0] if len(images) == 1 else ''
        labels = hv.image_harness_labels(observed) if observed else {}
        methods = {a.agent_username: (a.mcp_config or {}).get('execution_method', '')
                   for a in FleetAgent.objects.filter(pk__in=[r['agent_id'] for r in snapshot['census']])}
        harnesses = []
        for key, (pkg, label) in hv.HARNESS_KEYS.items():
            current = labels.get(key)
            latest = hv.npm_latest(pkg) if pkg else current
            users = sorted(name for name, method in methods.items()
                           if hv.HARNESS_BY_METHOD.get(method, (None,))[0] == key)
            harnesses.append({'harness': key, 'label': label, 'package': pkg or 'shizuha (internal)',
                              'current': current, 'latest': latest,
                              'upgrade_available': hv.upgrade_available(current, latest),
                              'source': 'image-label' if current else 'unknown',
                              'agent_count': len(users), 'agents': users})
        converged = lambda r: (r['image'] == target and len(r['pods']) == r['replicas']
                               and all(p['ready'] and not p['deleting'] and p['image'] == target for p in r['pods']))
        return Response({'scope': 'hive-native', 'desired_runtime_release': authority,
                         'agent_runtime_image': observed, 'observed_runtime_images': images,
                         'desired_runtime_image': authority['image'],
                         'rollout_in_progress': any(not converged(r) for r in active),
                         'rollout_progress': {'active': len(active), 'converged': sum(converged(r) for r in active),
                                              'deferred_until_start': sum(r['deferred_until_start'] for r in snapshot['census'])},
                         'daemon_id': None, 'harnesses': harnesses})

    def _fleet_agents(self, request):
        try:
            runtime = list_runtime_agents(include_disabled=True)
            runtime_usernames = {
                str(agent.get('agent_username') or '').strip().lower()
                for agent in runtime
            }
            native = [
                FleetAgentSerializer(agent).data
                for agent in FleetAgent.objects
                .filter(managing_daemon__isnull=True)
                .exclude(status=FleetAgent.Status.DELETED)
                .order_by('agent_username')
                if agent.agent_username.lower() not in runtime_usernames
            ]
            return runtime + native
        except Exception:
            logger.exception('harness-versions: managed agent list failed (non-fatal)')
            return []


class FleetNativeRuntimeImageView(APIView):
    """Read-only source projection, independent of the legacy daemon release."""
    def get(self, request):
        from .native_runtime_release import get_native_runtime_release
        from .runtime_release import RuntimeReleaseProjectionUnavailable
        denied = _require_staff(request)
        if denied:
            return denied
        try:
            return Response(get_native_runtime_release().as_api_dict())
        except RuntimeReleaseProjectionUnavailable as exc:
            return Response({'error': str(exc), 'scope': 'hive-native'}, status=503)

    def post(self, request):
        denied = _require_staff(request)
        if denied:
            return denied
        return Response({'error': 'Native releases are reviewed source records',
                         'code': 'runtime_release_source_only'}, status=409)


class FleetRuntimeImageView(APIView):
    """Read-only Hive projection of rt-fleet's reviewed release record.

    SCLI-331 used to keep a competing desired image in mutable Django cache.
    That stale cache rolled the live fleet *backwards*.  The authoritative
    DesiredRuntimeRelease now arrives in the daemon register frame and is
    stored inside ``DaemonRegistry.harness_report``.  Hive exposes it here only
    so rt-fleet can compare projections; POST is deliberately disabled.
    """

    def get_permissions(self):
        from rest_framework.permissions import AllowAny, IsAuthenticated
        return [IsAuthenticated()] if self.request.method == 'POST' else [AllowAny()]

    def get(self, request):
        from .runtime_release import (
            RuntimeReleaseProjectionUnavailable,
            get_authoritative_runtime_release,
        )

        try:
            projection = get_authoritative_runtime_release()
        except RuntimeReleaseProjectionUnavailable as exc:
            return Response(
                {'error': str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response({
            **projection.release.as_api_dict(),
            # Broker rollout remains independently paced; exposing its target
            # here does not make Hive a runtime-image authority.
            'broker_image': getattr(settings, 'FLEET_BROKER_IMAGE', '') or '',
        })

    def post(self, request):
        denied = _require_staff(request)
        if denied:
            return denied
        return Response(
            {
                'error': 'runtime releases are reviewed rt-fleet source records; mutable Hive promotion is disabled',
                'code': 'runtime_release_source_only',
            },
            status=status.HTTP_409_CONFLICT,
        )


class FleetHarnessUpgradeView(APIView):
    """POST /v1/fleet/harness-versions/<harness>/upgrade/ — HIVE-600.

    Records an admin's intent to upgrade a harness across the fleet to a target
    version and returns the concrete rollout path. The actual upgrade is a
    runtime-image rebuild+roll (multi-arch CI build of the agent-runtime image
    with the new pin), which is infra-gated — so this endpoint captures the
    request as an auditable event and hands back the exact steps/command rather
    than silently claiming a live change. Staff-only.
    """

    def post(self, request, harness):
        denied = _require_staff(request)
        if denied:
            return denied
        from . import harness_versions as hv

        if harness not in hv.HARNESS_KEYS:
            return Response({'error': f'Unknown harness "{harness}"'}, status=status.HTTP_400_BAD_REQUEST)
        pkg, label = hv.HARNESS_KEYS[harness]
        target = (request.data.get('target_version') or '').strip() or hv.npm_latest(pkg)
        if not target:
            return Response(
                {'error': f'No target version given and latest for {pkg} is unavailable'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        actor = getattr(request.user, 'email', None) or getattr(request.user, 'username', 'staff')
        logger.warning('HIVE-600/SCLI-331 harness upgrade requested by %s: %s -> %s', actor, pkg, target)
        # SCLI-331: dispatch only the Origin CI build. Promotion is deliberately
        # separate and review-gated: a deploy PR appends a higher generation and
        # immutable digest to DesiredRuntimeRelease. Hive then projects that
        # record; it never mutates the target itself.
        result = hv.dispatch_agent_runtime_build({harness: target})
        if not result.get('dispatched'):
            return Response({
                'accepted': True, 'harness': harness, 'package': pkg, 'target_version': target,
                'requested_by': actor, 'auto_build': False,
                'reason': result.get('error', 'no Forgejo token configured'),
                'manual': (
                    'Dispatch build-agent-runtime, then promote its immutable digest '
                    'through a reviewed DesiredRuntimeRelease deploy PR.'
                ),
            }, status=status.HTTP_202_ACCEPTED)
        return Response({
            'accepted': True, 'harness': harness, 'package': pkg, 'target_version': target,
            'requested_by': actor, 'auto_build': True,
            'build_tag': result['tag'],
            'expected_image': result['image'],
            'next': (
                f'CI is building {result["image"]}; after publish, append its immutable '
                'digest as a higher-generation DesiredRuntimeRelease in a reviewed deploy '
                'PR. The daemon rolls agents only after both projections converge.'
            ),
        }, status=status.HTTP_202_ACCEPTED)


class FleetAgentIndexCacheStatsView(APIView):
    """GET /v1/fleet/agents/cache-stats/ — PLAT-1063 read-model cache observability
    (hit/miss/stale counters, hit-rate, last build latency, TTLs)."""

    def get(self, request):
        denied = _require_staff(request)
        if denied:
            return denied
        return Response(agent_index_cache.stats())


class FleetAgentTemplateListView(APIView):
    """GET /v1/fleet/agent-templates/ — staff read-only agent blueprint catalog."""

    def get(self, request):
        denied = _require_staff(request)
        if denied:
            return denied
        query = request.query_params.get('query') or request.query_params.get('q') or ''
        try:
            max_results = int(request.query_params.get('max_results') or request.query_params.get('limit') or 20)
        except (TypeError, ValueError):
            max_results = 20
        templates = search_agent_templates(query=query, max_results=max_results)
        return Response({'templates': templates, 'count': len(templates)})


class FleetAgentTemplateDetailView(APIView):
    """GET /v1/fleet/agent-templates/{slug}/ — staff read-only template detail."""

    def get(self, request, template_slug):
        denied = _require_staff(request)
        if denied:
            return denied
        template = get_agent_template(template_slug)
        if template is None:
            return Response({'error': 'Agent template not found'}, status=status.HTTP_404_NOT_FOUND)
        return Response({'template': template.detail()})


class FleetCredentialListView(APIView):
    """GET/POST /v1/fleet/credentials/ — redacted Hive credential grants."""

    def get(self, request):
        denied = _require_staff(request)
        if denied:
            return denied
        qs = HiveCredential.objects.select_related('agent').order_by(
            'scope', 'team_slug', 'provider', 'purpose'
        )
        filters = {
            'scope': request.query_params.get('scope'),
            'team_slug': request.query_params.get('team_slug'),
            'provider': request.query_params.get('provider'),
            'purpose': request.query_params.get('purpose'),
            'agent_id': request.query_params.get('agent'),
        }
        for key, value in filters.items():
            if value:
                qs = qs.filter(**{key: value})
        active = request.query_params.get('is_active')
        if active is not None:
            qs = qs.filter(is_active=str(active).lower() not in {'0', 'false', 'no'})
        return Response({'credentials': HiveCredentialSerializer(qs, many=True).data})

    def post(self, request):
        denied = _require_staff(request)
        if denied:
            return denied
        serializer = HiveCredentialSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        try:
            credential = serializer.save(
                created_by_user_id=_actor(request),
                updated_by_user_id=_actor(request),
            )
        except ValidationError as exc:
            return Response({'error': exc.message_dict if hasattr(exc, 'message_dict') else str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        _audit_credential(
            credential,
            request,
            HiveCredentialAudit.Action.CREATE,
            message='credential grant created',
        )
        _fulfill_matching_credential_requests(credential, request)
        return Response(HiveCredentialSerializer(credential).data, status=status.HTTP_201_CREATED)


class FleetCredentialSelfUpsertView(APIView):
    """POST /v1/fleet/credentials/self-upsert/ — agent self credential handle upsert.

    This keeps the useful cron-era self-upsert workflow, but Hive remains the
    durable owner and only records non-secret handles. Agent tokens can upsert
    only their own credential; staff can supply agent/agent_id for repair.
    """

    def post(self, request):
        agent, denied = _resolve_self_credential_agent(request)
        if denied:
            return denied

        service = _credential_purpose_from_service(request.data.get('service') or request.data.get('purpose'))
        if not service:
            return Response({'error': 'service is required'}, status=status.HTTP_400_BAD_REQUEST)

        secret_ref = request.data.get('secret_ref') or request.data.get('secretRef')
        if not secret_ref:
            return Response(
                {'error': 'secret_ref is required; plaintext payload is not accepted by Hive'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        fingerprint = request.data.get('fingerprint') or ''
        existing = (
            HiveCredential.objects
            .filter(
                scope=HiveCredential.Scope.AGENT,
                agent=agent,
                provider=HiveCredential.Provider.GENERIC_ENV,
                purpose=service,
                is_active=True,
            )
            .order_by('-updated_at')
            .first()
        )
        payload = {
            'scope': HiveCredential.Scope.AGENT,
            'agent': str(agent.id),
            'provider': HiveCredential.Provider.GENERIC_ENV,
            'purpose': service,
            'secret_ref': secret_ref,
            'fingerprint': fingerprint,
            'requires_team_credential_eligible': False,
        }
        before = _credential_snapshot(existing) if existing else None
        serializer = HiveCredentialSerializer(existing, data=payload, partial=bool(existing))
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            if existing:
                credential = serializer.save(updated_by_user_id=_actor(request))
                action = HiveCredentialAudit.Action.UPDATE
                created = False
                message = 'self credential upsert updated'
                response_status = status.HTTP_200_OK
            else:
                credential = serializer.save(
                    created_by_user_id=_actor(request),
                    updated_by_user_id=_actor(request),
                )
                action = HiveCredentialAudit.Action.CREATE
                created = True
                message = 'self credential upsert created'
                response_status = status.HTTP_201_CREATED
        except ValidationError as exc:
            return Response(
                {'error': exc.message_dict if hasattr(exc, 'message_dict') else str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        _audit_credential(credential, request, action, before=before, message=message)
        _fulfill_matching_credential_requests(credential, request)
        return Response(
            {'credential': HiveCredentialSerializer(credential).data, 'created': created},
            status=response_status,
        )


class FleetCredentialRequestListView(APIView):
    """GET/POST /v1/fleet/credential-requests/ — Hive-owned credential request queue."""

    def get(self, request):
        qs = HiveCredentialRequest.objects.select_related('requester', 'fulfilled_by_credential').all()
        if getattr(getattr(request, 'user', None), 'is_staff', False):
            requester = request.query_params.get('requester') or request.query_params.get('agent')
            if requester:
                qs = qs.filter(
                    Q(requester__agent_username__iexact=requester)
                    | Q(requester__agent_id__iexact=requester)
                    | Q(requester_id=requester if _looks_like_uuid(requester) else None)
                )
        elif _is_agent_token(request):
            agent, denied = _resolve_self_credential_agent(request)
            if denied:
                return denied
            qs = qs.filter(requester=agent)
        else:
            return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)

        req_status = request.query_params.get('status')
        if req_status:
            qs = qs.filter(status=req_status)
        scope = request.query_params.get('scope')
        if scope:
            qs = qs.filter(scope=scope)
        return Response({'credential_requests': HiveCredentialRequestSerializer(qs, many=True).data})

    def post(self, request):
        agent, denied = _resolve_self_credential_agent(request)
        if denied:
            return denied
        scope = str(request.data.get('scope') or '').strip()
        reason = request.data.get('reason')
        expiry_raw = request.data.get('expiry')
        expiry = None
        if expiry_raw:
            expiry = parse_datetime(str(expiry_raw))
            if expiry is None:
                return Response({'error': 'expiry must be an ISO-8601 datetime'}, status=status.HTTP_400_BAD_REQUEST)
        credential_request = HiveCredentialRequest(
            requester=agent,
            requester_username=agent.agent_username,
            requester_role=agent.role,
            scope=scope,
            reason=reason,
            expiry=expiry,
        )
        try:
            credential_request.save()
        except ValidationError as exc:
            return Response({'error': exc.message_dict if hasattr(exc, 'message_dict') else exc.messages},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response(
            {'credential_request': HiveCredentialRequestSerializer(credential_request).data},
            status=status.HTTP_201_CREATED,
        )


def _looks_like_uuid(value):
    try:
        uuid.UUID(str(value))
        return True
    except (TypeError, ValueError, AttributeError):
        return False


class FleetCredentialRequestDenyView(APIView):
    """POST /v1/fleet/credential-requests/{id}/deny/ — deny a pending request."""

    def post(self, request, request_id):
        denied = _require_staff(request)
        if denied:
            return denied
        reason = str(request.data.get('reason') or '').strip()
        if not reason:
            return Response({'error': 'reason is required'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            credential_request = HiveCredentialRequest.objects.get(id=request_id)
        except (HiveCredentialRequest.DoesNotExist, ValueError, ValidationError):
            return Response({'error': 'Credential request not found'}, status=status.HTTP_404_NOT_FOUND)
        if credential_request.status != HiveCredentialRequest.Status.PENDING:
            return Response({'error': f'Credential request is {credential_request.status}, not pending'},
                            status=status.HTTP_400_BAD_REQUEST)
        credential_request.status = HiveCredentialRequest.Status.DENIED
        credential_request.resolved_by_user_id = _actor(request)
        credential_request.resolved_at = timezone.now()
        credential_request.resolution_reason = reason
        credential_request.save(update_fields=[
            'status', 'resolved_by_user_id', 'resolved_at', 'resolution_reason', 'updated_at',
        ])
        return Response({'credential_request': HiveCredentialRequestSerializer(credential_request).data})


class FleetCredentialRequestExpireView(APIView):
    """POST /v1/fleet/credential-requests/expire/ — expire pending requests past expiry."""

    def post(self, request):
        denied = _require_staff(request)
        if denied:
            return denied
        qs = HiveCredentialRequest.objects.filter(
            status=HiveCredentialRequest.Status.PENDING,
            expiry__isnull=False,
            expiry__lte=timezone.now(),
        )
        scope = request.data.get('scope') or request.query_params.get('scope')
        if scope:
            qs = qs.filter(scope=str(scope).strip())
        now = timezone.now()
        count = qs.update(
            status=HiveCredentialRequest.Status.EXPIRED,
            resolved_by_user_id=_actor(request),
            resolved_at=now,
            resolution_reason='expired by Hive credential request expiry sweep',
            updated_at=now,
        )
        return Response({'expired': count})


class FleetCredentialDetailView(APIView):
    """GET/PATCH/DELETE /v1/fleet/credentials/{id}/ — redacted grant CRUD."""

    def _credential(self, credential_id):
        try:
            return HiveCredential.objects.select_related('agent').get(id=credential_id)
        except (HiveCredential.DoesNotExist, ValueError, ValidationError):
            return None

    def get(self, request, credential_id):
        denied = _require_staff(request)
        if denied:
            return denied
        credential = self._credential(credential_id)
        if not credential:
            return Response({'error': 'Credential grant not found'}, status=status.HTTP_404_NOT_FOUND)
        return Response(HiveCredentialSerializer(credential).data)

    def patch(self, request, credential_id):
        denied = _require_staff(request)
        if denied:
            return denied
        credential = self._credential(credential_id)
        if not credential:
            return Response({'error': 'Credential grant not found'}, status=status.HTTP_404_NOT_FOUND)
        before = _credential_snapshot(credential)
        serializer = HiveCredentialSerializer(credential, data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        try:
            credential = serializer.save(updated_by_user_id=_actor(request))
        except ValidationError as exc:
            return Response({'error': exc.message_dict if hasattr(exc, 'message_dict') else str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        _audit_credential(
            credential,
            request,
            HiveCredentialAudit.Action.UPDATE,
            before=before,
            message='credential grant updated',
        )
        _fulfill_matching_credential_requests(credential, request)
        return Response(HiveCredentialSerializer(credential).data)

    def delete(self, request, credential_id):
        denied = _require_staff(request)
        if denied:
            return denied
        credential = self._credential(credential_id)
        if not credential:
            return Response({'error': 'Credential grant not found'}, status=status.HTTP_404_NOT_FOUND)
        before = _credential_snapshot(credential)
        credential.is_active = False
        credential.revoked_at = timezone.now()
        credential.updated_by_user_id = _actor(request)
        credential.save(update_fields=['is_active', 'revoked_at', 'updated_by_user_id', 'updated_at'])
        _audit_credential(
            credential,
            request,
            HiveCredentialAudit.Action.REVOKE,
            before=before,
            message='credential grant revoked',
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class FleetCredentialAuditView(APIView):
    """GET /v1/fleet/credentials/{id}/audit/ — redacted grant audit events."""

    def get(self, request, credential_id):
        denied = _require_staff(request)
        if denied:
            return denied
        try:
            credential = HiveCredential.objects.get(id=credential_id)
        except (HiveCredential.DoesNotExist, ValueError, ValidationError):
            return Response({'error': 'Credential grant not found'}, status=status.HTTP_404_NOT_FOUND)
        events = credential.audit_events.all()
        return Response({'audit_events': HiveCredentialAuditSerializer(events, many=True).data})


def _activity_epoch(value):
    """Return a comparable epoch for an ISO-8601/datetime activity value.

    Activity clocks are stored/transported as naive UTC (see fleet/timeutils).
    ``datetime.timestamp()`` interprets a naive value as TIME_ZONE-local, so
    calling it directly put these epochs 5-6h off the value every other reader
    (_within_grace_window, snapshot-age) derives from the same field — enough to
    invert a freshness comparison between two agents on different planes. Stamp
    UTC onto naive values first so all readers agree.
    """
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except (ValueError, TypeError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime_timezone.utc)
    return parsed.timestamp()


def _drop_retired_host_spawn_failure(row):
    """Native k3s row must not keep a host-daemon Failed paint."""
    from .runtime_placement import is_retired_host_spawn_observation
    if not is_retired_host_spawn_observation(row):
        return row
    cleaned = dict(row)
    if str(cleaned.get('status') or '').lower() == 'failed':
        lifecycle = str(cleaned.get('lifecycle_state') or '').lower()
        if lifecycle == 'operator_stopped':
            cleaned['status'] = 'stopped'
        elif lifecycle == 'hibernated' or cleaned.get('hibernation_reason'):
            cleaned['status'] = 'hibernated'
        else:
            cleaned['status'] = 'running'
        cleaned['enabled'] = cleaned['status'] == 'running'
    cleaned['status_message'] = ''
    return cleaned


def _native_row_is_live(native_row, runtime_row):
    """Choose the k3s-native row for a migrated duplicate when it is fresher.

    PLAT-4159 forbids generic activity freshness as final plane authority.  This
    selector is therefore an explicitly temporary, opt-in containment gate; the
    safe default preserves the existing daemon/runtime owner until the accepted
    owner/epoch/resource-fenced reducer replaces it.
    """
    from .runtime_placement import is_retired_host_spawn_observation
    # Host-daemon refusal is not a live plane. Prefer the native k3s row even
    # when HIVE-609 freshness is off — otherwise six healthy k3s pods painted
    # Failed on the Agents page (Ichi/Ni/Kai/Mio/Sara/Aoi, 2026-08-31).
    if is_retired_host_spawn_observation(runtime_row):
        return True
    native_status = str((native_row or {}).get('status') or '').lower()
    runtime_status = str((runtime_row or {}).get('status') or '').lower()
    runtime_life = str((runtime_row or {}).get('lifecycle_state') or '').lower()
    # A live native Deployment beats a stale daemon kill-switch. Hive
    # provisioned GLM seats 2026-09-01 while rt-fleet still reported
    # operator_stopped, so the Agents page painted Stopped over running pods.
    if native_status == 'running' and (
        runtime_status in {'stopped', 'hibernated', 'offline', 'disabled'}
        or runtime_life in {'operator_stopped', 'hibernated'}
    ):
        return True
    if not getattr(settings, 'HIVE609_TEMP_FRESHNESS_PRECEDENCE', False):
        return False
    native_ts = _activity_epoch((native_row or {}).get('last_active_at'))
    runtime_ts = _activity_epoch((runtime_row or {}).get('last_active_at'))
    if native_ts is not None and runtime_ts is not None:
        if native_ts != runtime_ts:
            return native_ts > runtime_ts
    elif native_ts is not None or runtime_ts is not None:
        return native_ts is not None
    return str((native_row or {}).get('status') or '').lower() == 'running'


def _merge_native_and_runtime(native, runtime_by_username):
    """Merge native and daemon rows without letting stale legacy rows shadow
    an active migrated k3s agent. Runtime-only rows remain visible.

    The selected runtime row owns observed placement/lifecycle/telemetry, but
    Hive's native FleetAgent row remains the exclusive desired-configuration
    source of truth.  In particular, a stopped runtime cannot acknowledge a
    model change immediately; rendering its stale daemon model here used to
    make the Agents UI appear to revert the edit and let model-lease membership
    drift back to the retired model while RuntimeChange was still ROLLING.
    """
    desired_config_fields = {
        'display_name', 'display_title', 'role',
        # HIVE-1242: the portrait URL is a desired-identity field that ONLY
        # exists on the native row (runtime dicts never carry it). Without it
        # here, every runtime-merged agent dropped its avatar and the Agents UI
        # showed initials for everyone.
        'avatar_url',
        'model', 'effective_model', 'model_source', 'model_provider',
        'execution_method',
        'reasoning_effort', 'effective_reasoning_effort',
        'reasoning_effort_is_default',
        'image', 'effective_image', 'image_source', 'image_pull_policy', 'desired_runtime_release', 'observed_runtime_image',
        'resource_profile', 'resource_profile_spec', 'resource_overrides',
        'arch', 'requires_longhorn', 'node_selector_overrides', 'node_pool',
        'tolerations',
        'context_prompt', 'personality_traits',
        'model_fallbacks', 'model_overrides', 'token_budget', 'agent_memory',
        'work_schedule', 'tier', 'failover_chain_id', 'platform_url',
        'skills', 'eager_skills', 'skill_config',
        'mcp_servers', 'enabled_mcp_server_ids', 'mcp_config',
    }

    def runtime_with_hive_desired(runtime_row, native_row):
        merged_row = dict(runtime_row)
        for field in desired_config_fields:
            if field in native_row:
                merged_row[field] = native_row[field]
        merged_row['desired_config_source'] = 'hive'
        merged_row['observed_runtime_model'] = (
            runtime_row.get('effective_model') or runtime_row.get('model') or ''
        )
        return merged_row

    native_by_username = {}
    for row in native:
        username = row.get('agent_username')
        if username and username not in native_by_username:
            native_by_username[username] = row
    merged = []
    for username, native_row in native_by_username.items():
        runtime_row = runtime_by_username.get(username)
        if runtime_row is None or _native_row_is_live(native_row, runtime_row):
            merged.append(_drop_retired_host_spawn_failure(native_row))
        else:
            merged.append(runtime_with_hive_desired(runtime_row, native_row))
    merged.extend(
        row for username, row in runtime_by_username.items()
        if username not in native_by_username
    )
    return merged


def _attach_model_lease_status(agents):
    """Attach Cortex lease state to the cached staff fleet read model.

    Hive's lifecycle status says whether an agent is running, hibernated, or
    explicitly stopped.  Cortex's board says which governed agent currently
    owns the model sprint.  The Agents UI needs both facts to explain a blue
    hibernated row without implying failure or operator disablement.

    Fail soft: lease observation must never hide the fleet inventory.
    """
    try:
        from .model_lease import lease_status_by_username
        lease_by_username = lease_status_by_username(agents=agents)
    except Exception:
        logger.exception('model lease status merge failed (non-fatal)')
        return agents
    if not lease_by_username:
        return agents
    for agent in agents:
        username = str(agent.get('agent_username') or '').strip().lower()
        lease = lease_by_username.get(username)
        if lease:
            agent['model_lease'] = lease
    return agents


class FleetAgentListView(APIView):
    """GET /v1/fleet/agents/ — list; POST — add+provision.

    Org scoping contract (HIVE-2635): every roster row carries populated
    ``organization_slug`` / ``tenant_org_slug`` fields. The fleet is
    single-org today (the Shizuha internal fleet, slug ``shizuha``) —
    FleetAgent rows and internal runtime seats resolve to that slug, and
    non-internal runtime seats with no server-resolved org keep the empty
    string (the customer-deny sentinel per HIVE-446). Row-level org
    isolation is therefore demonstrated by the contract, not by accident of
    single-org data; a second org on the fleet must populate these fields
    and extend the HIVE-2607 cert cadence to assert correct scoping.
    """

    def get(self, request):
        if not getattr(request.user, 'is_authenticated', False):
            return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
        # Ordinary non-staff calls remain owner-scoped. The explicit opt-in is
        # what makes this a narrow temporary QA vantage rather than ambient
        # staff authority.
        fleet_read_requested = request.query_params.get('fleet_read', '').lower() in ('1', 'true', 'yes')
        if fleet_read_requested and not getattr(request.user, 'is_staff', False):
            grant, denied = _require_fleet_read(request)
            if denied:
                return denied
            request._hive_fleet_read_grant = grant
        # PLAT-1063: serve the agents-index from a bounded-TTL read-model cache so
        # warm loads don't re-run the serial fan-out (runtime list + external +
        # telemetry + output-rates + Pulse rosters) that caused the 5-10s loads.
        # ?nocache=1 forces a live rebuild; pagination is applied AFTER the cache
        # so the cached object is the full assembled list (page params don't key
        # it).
        include_external = request.query_params.get('include_external', 'true').lower() != 'false'
        owner_only = request.query_params.get('owner_only', '').lower() in ('1', 'true', 'yes')
        if fleet_read_requested:
            owner_only = False
        nocache = request.query_params.get('nocache', '').lower() in ('1', 'true', 'yes')
        org_slug = _request_org_slug(request)
        suffix = _agents_index_cache_suffix(request, include_external, owner_only, org_slug)
        if nocache:
            agent_index_cache.note_bypass()
            agents = self._build_agents_index(request, include_external, owner_only, org_slug)
            cache_state = 'bypass'
        else:
            agents, cache_state = agent_index_cache.get_or_build(
                suffix, lambda: self._build_agents_index(request, include_external, owner_only, org_slug))
        if cache_state == 'building' and not agents:
            resp = Response(
                {'error': 'agents index rebuilding', 'retry': True},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
            resp['X-Hive-Cache'] = cache_state
            resp['Retry-After'] = '1'
            return resp
        # Serve-time authoritative overlay (operator 2026-07-12 "near instant,
        # no delays"): archive/unarchive/delete must be visible IMMEDIATELY.
        # The stale-while-revalidate cache serves the last-good payload after a
        # mutation, so archived/deleted state from build time can be seconds
        # stale — exactly the "I archived it but nothing happened" report. Two
        # tiny indexed queries per request make those states authoritative on
        # EVERY response regardless of payload age. Copy-on-write (never mutate
        # the shared cached dicts). Non-fatal on error.
        from .native_lifecycle_projection import overlay_completed_native_lifecycle
        try:
            agents = overlay_completed_native_lifecycle(agents)
        except Exception:
            logger.exception('serve-time native lifecycle receipt unavailable')
        try:
            from .models import archived_usernames
            live_archived = {str(u).lower() for u in archived_usernames()}
            live_deleted = {
                str(u).lower() for u in FleetAgent.objects.filter(
                    status=FleetAgent.Status.DELETED).values_list('agent_username', flat=True)
            }
            overlaid = []
            for a in agents:
                u = (a.get('agent_username') or '').lower()
                if u in live_deleted and not (
                    a.get('runtime_source') == 'external'
                    and str(a.get('id') or '').startswith('ext:')
                ):
                    continue
                if u in live_archived and not a.get('archived'):
                    a = {**a, 'archived': True}
                elif u not in live_archived and a.get('archived'):
                    a = {**a, 'archived': False}
                overlaid.append(a)
            agents = overlaid
        except Exception:
            logger.exception('serve-time archive/deleted overlay failed (non-fatal)')
        try:
            agents = _overlay_live_pulse_memberships(agents, org_slug)
        except Exception:
            logger.exception('serve-time Pulse membership overlay failed (non-fatal)')
        agents = [
            {**agent, 'health_summary': {
                **agent['health_summary'],
                'last_snapshot_age_s': _agent_snapshot_age_seconds(agent),
            }} if isinstance(agent.get('health_summary'), dict) else agent
            for agent in agents
        ]
        paginator = FleetAgentPagination()
        page = paginator.paginate_queryset(agents, request, view=self)
        resp = (paginator.get_paginated_response(page)
                if page is not None else Response({'agents': agents}))
        resp['X-Hive-Cache'] = cache_state
        # CON-176: expose roster-source degradation so the UI can banner stale/
        # missing team data instead of silently rendering "Unassigned" for all.
        degraded, stale_age = pulse_roster_index_degradation()
        if degraded:
            resp['X-Hive-Roster-Degraded'] = '1'
            if stale_age is not None:
                resp['X-Hive-Roster-Stale-Age'] = str(int(stale_age))
            if isinstance(resp.data, dict):
                resp.data['roster_degraded'] = True
                resp.data['roster_stale_age_seconds'] = int(stale_age) if stale_age is not None else None
        return resp


    def _build_agents_index(self, request, include_external, owner_only=False, org_slug=''):
        """PLAT-1063: the expensive live aggregation behind the agents-index,
        split out of get() so it can be served from a bounded-TTL cache. Returns
        the fully-assembled, sorted list of agent dicts."""
        is_staff = bool(getattr(request.user, 'is_staff', False))
        has_fleet_read = bool(getattr(request, '_hive_fleet_read_grant', None))
        if not (is_staff or has_fleet_read) or owner_only:
            owner_id = getattr(request.user, 'id', None)
            owned_native = FleetAgent.objects.filter(
                owner_id=owner_id,
            ).exclude(status=FleetAgent.Status.DELETED)
            native = FleetAgentSerializer(owned_native, many=True).data
            native_usernames = {(item.get('agent_username') or '').lower() for item in native}
            agents = native + list_external_agents(owner_id=owner_id, exclude_usernames=native_usernames)
            agents.extend(_owner_claw_agents(owner_id))
            agents.sort(key=lambda item: (item.get('agent_username') or '').lower())
            # HIVE-617: owner-scoped inventory is already tenant-filtered above,
            # but it still needs the same Pulse-owned placement enrichment as the
            # staff fleet. Returning here without it made every agent appear under
            # Unassigned for ordinary account owners even while Pulse's canonical
            # TeamRoster remained healthy. The service-roster fallback inside
            # _attach_pulse_team_memberships is expressly scoped to these already-
            # authorized rows, so it reveals no cross-owner inventory.
            agents = _attach_pulse_team_memberships(request, agents, org_slug=org_slug)
            agents = _attach_identity_routing_health(agents)
            agents = _attach_health_summaries(agents)
            return agents
        qs = FleetAgent.objects.exclude(status=FleetAgent.Status.DELETED).order_by('agent_username')
        # Isolated personal defaults (`shizuha-<owner_id>`) are the owner's
        # private chat seat, not org-fleet inventory. Including them made the
        # staff Agents page dump a stack of identically named "Shizuha" cards
        # under Unassigned (operator 2026-09-04).
        qs = qs.exclude(isolated=True, agent_username__startswith='shizuha-')
        native = FleetAgentSerializer(qs, many=True).data
        # HIVE-609 temporary containment: for duplicate usernames, a fresher
        # native row may win while the explicit feature flag is enabled. Without
        # the flag, the prior runtime-wins rule remains fail-closed. PLAT-4159's
        # owner/epoch/resource-fenced reducer will replace this temporary choice.
        runtime_by_username = {}
        if include_external:
            # A daemon-managed row remains daemon-managed while stopped. Filtering
            # stopped Cortex/empty-model observations here made the native fallback
            # win and falsely rendered rt-fleet agents as k3s-native lifecycle rows.
            # Hive owns whether a row exists; provider/model is not provenance.
            for agent in list_runtime_agents(include_disabled=True):
                uname = agent.get('agent_username')
                if not uname:
                    continue
                runtime_by_username[uname] = attach_context_budget(agent)
        agents = _merge_native_and_runtime(native, runtime_by_username)
        # HIVE-177: external / BYO agents — heartbeat-alive account_type=agent
        # accounts (e.g. a k3s fleet on the v4 MacBook) not represented by a
        # FleetAgent row or rt-fleet runtime. Surfaced read-only, attributed to
        # their shizuha-id owner (created_by). Precedence: native/runtime win, so
        # exclude any username already present.
        if include_external:
            try:
                present = {(a.get('agent_username') or '').lower() for a in agents}
                # External/k3s-native agents are owner-scoped. In the staff
                # fleet-backed view, include only the caller's own external
                # agents; FleetAgent/rt-fleet/Pulse data represent the account's
                # managed runtime agents above.
                agents.extend(list_external_agents(
                    exclude_usernames=present,
                    owner_id=getattr(request.user, 'id', None),
                ))
            except Exception:
                logger.exception('HIVE-177: external-agent merge failed (non-fatal)')
        # ADR-0080: Claw/Hermes is retired. Do not merge those rows into the
        # org Agents page (they painted as Unassigned "Account agent" / Hermes).
        # The legacy Claws surface remains for reclaim/delete.
        # Telemetry: enrich EVERY agent (native + runtime + external) with the live
        # bridge telemetry reported over Connect — model / context% / tok-per-sec /
        # health. external_agents already merges its own; here we cover the local
        # fleet too so all cards show the same live metrics. Non-fatal on failure.
        try:
            from .external_agents import agent_telemetry, telemetry_fields
            tele = agent_telemetry()
            if tele:
                for a in agents:
                    snap = tele.get((a.get('agent_username') or '').lower())
                    if snap:
                        a.update(telemetry_fields(snap, configured=dict(a)))
                    _clear_empty_queue_conversational_needs_help(a)
                    _clear_needs_help_if_currently_working(a)
                _apply_provider_unavailable_health(agents, tele)
                for a in agents:
                    a['context_budget'] = context_budget_for_agent(a)
        except Exception:
            logger.exception('telemetry merge failed (non-fatal)')
        # Archive markers (operator 2026-07-10): stamp every agent dict so the
        # UI can segregate archived agents and hide start affordances.
        try:
            from .models import archived_usernames
            archived = archived_usernames()
        except Exception:
            logger.exception('archived-usernames lookup failed (display only)')
            archived = set()
        if archived:
            for a in agents:
                if (a.get('agent_username') or '').lower() in archived:
                    a['archived'] = True
        agents.sort(key=lambda item: (item.get('agent_username') or '').lower())
        # HIVE-172 efficiency is fetched by the frontend via /fleet/efficiency/.
        # Keep the hot agents-list path focused on roster/runtime data.
        # HIVE-175: attach recent output rate (≈ tok/s) so cards show live
        # throughput — a cheap "is this agent actually producing right now?" signal.
        try:
            rates = get_runtime_activity_rates(window=600)
            if rates:
                for a in agents:
                    rate = rates.get(a.get('id')) or rates.get(str(a.get('id')))
                    if rate:
                        a['activity_rate'] = rate
        except Exception:
            pass
        agents = _attach_pulse_team_memberships(request, agents, org_slug=org_slug)
        agents = _attach_identity_routing_health(agents)
        agents = _attach_health_summaries(agents)
        from .native_lifecycle_projection import overlay_completed_native_lifecycle
        agents = overlay_completed_native_lifecycle(agents)
        agents = _attach_provider_notes(agents)
        agents = _attach_model_lease_status(agents)
        try:
            from .activity_phase import annotate_activity
            for a in agents:
                annotate_activity(a)
        except Exception:
            logger.exception('activity-phase annotate failed (non-fatal)')
        return agents

    def post(self, request):
        denied = _require_staff(request)
        if denied:
            return denied
        serializer = FleetAgentCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        from .runtime_lane import RuntimeLaneError, validate_chat_model_output
        try:
            validate_chat_model_output(serializer.validated_data.get('model', ''))
            agent = serializer.save()
        except RuntimeLaneError as exc:
            return Response({'error': exc.message, 'code': exc.code}, status=status.HTTP_400_BAD_REQUEST)
        except ValidationError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

        agent_password = request.data.get('agent_password', '')
        ok = provisioner.provision_agent(agent, agent_password=agent_password)
        if not ok:
            waiting = _lease_wait_response(agent)
            if waiting is not None:
                return waiting
            return Response(
                {'error': 'Provisioning failed; agent row created. Check events.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        agent_index_cache.invalidate('provision')
        return Response(FleetAgentSerializer(agent).data, status=status.HTTP_201_CREATED)


class FleetAgentCardListView(FleetAgentListView):
    """GET /v1/fleet/agent-cards/ — compressed Agents-grid projection.

    This shares the authoritative cached aggregation and pagination contract
    with ``FleetAgentListView`` but strips fields used only after the drawer's
    separate detail fetch.  The projection is copy-on-write, so it never
    mutates the shared last-good index.
    """

    def get(self, request):
        response = super().get(request)
        if response.status_code != status.HTTP_200_OK:
            return response
        if not isinstance(response.data, dict):
            return response
        key = 'results' if isinstance(response.data.get('results'), list) else 'agents'
        if isinstance(response.data.get(key), list):
            response.data[key] = project_agent_cards(response.data[key])
        return response


def _maybe_ensure_default_personal_agent(request):
    """Park one isolated default agent for an eligible human. Fail-soft."""
    from .personal_agents import (
        eligible_for_default_personal_agent,
        ensure_personal_shizuha_agent,
        PersonalShizuhaProvisionError,
    )
    user = getattr(request, 'user', None)
    if not eligible_for_default_personal_agent(user):
        return
    try:
        ensure_personal_shizuha_agent(
            owner_id=int(user.id),
            owner_username=str(getattr(user, 'username', '') or ''),
        )
    except PersonalShizuhaProvisionError:
        logger.warning('default personal agent ensure refused for user %s', getattr(user, 'id', None))
    except Exception:
        logger.exception('default personal agent ensure failed for user %s', getattr(user, 'id', None))


class PersonalAgentEnsureView(APIView):
    """GET/POST /v1/fleet/personal-agent — the caller's default Shizuha.

    Eligible humans get one isolated hibernated agent (operator 2026-08-19).
    Staff, fleet agents, and test/service accounts are not auto-assigned
    (operator 2026-08-05: no garbage spawns / no extra Shizuha for Hritik).
    """

    def get(self, request):
        if not getattr(request.user, 'is_authenticated', False):
            return Response({'error': 'Authentication required'},
                            status=status.HTTP_401_UNAUTHORIZED)
        from .personal_agents import (
            eligible_for_default_personal_agent,
            ensure_personal_shizuha_agent,
            get_personal_agent,
            PersonalShizuhaProvisionError,
        )
        agent = get_personal_agent(request.user.id)
        created = False
        if agent is not None or eligible_for_default_personal_agent(request.user):
            try:
                result = ensure_personal_shizuha_agent(
                    owner_id=int(request.user.id),
                    owner_username=str(getattr(request.user, 'username', '') or ''),
                )
                agent = result.agent
                created = result.created
            except PersonalShizuhaProvisionError as exc:
                if agent is None:
                    return Response(
                        {'error': str(exc), 'runtime_ready': False},
                        status=status.HTTP_409_CONFLICT,
                    )
        if agent is None:
            return Response(
                {
                    'error': 'No personal agent exists for this account.',
                    'runtime_ready': False,
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        mcp = agent.mcp_config if isinstance(agent.mcp_config, dict) else {}
        return Response({
            'id': str(agent.id),
            'stable_id': f'hive:fleet:{agent.id}',
            'source': 'hive',
            'kind': 'personal_agent',
            'agent_username': agent.agent_username,
            'display_name': agent.display_name or 'Shizuha',
            'model': agent.model,
            'execution_method': mcp.get('execution_method') or '',
            'identity_user_id': agent.identity_user_id,
            'status': agent.status,
            'hibernation_reason': agent.hibernation_reason,
            'owner_id': agent.owner_id,
            'isolated': bool(agent.isolated),
            'created': created,
            'runtime_ready': agent.status == FleetAgent.Status.RUNNING,
        }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    def post(self, request):
        return self.get(request)


class InternalEnsurePersonalAgentView(APIView):
    """POST /v1/fleet/internal/ensure-personal-agent — ID activation hook.

    Network-gated (path contains /internal/). Parks one hibernated isolated
    default for an eligible human. Staff, existing owners, agents, and QA
    accounts are skipped. Never starts a pod.
    """
    authentication_classes: list = []
    permission_classes: list = []

    def post(self, request):
        from .personal_agents import (
            eligible_for_default_personal_agent,
            ensure_personal_shizuha_agent,
            owner_record,
            PersonalShizuhaProvisionError,
        )
        raw_owner = request.data.get('owner_id')
        try:
            owner_id = int(raw_owner)
        except (TypeError, ValueError):
            return Response({'error': 'owner_id required'}, status=status.HTTP_400_BAD_REQUEST)
        if owner_id <= 0:
            return Response({'error': 'owner_id required'}, status=status.HTTP_400_BAD_REQUEST)
        username = str(request.data.get('username') or '')
        record = owner_record(
            owner_id=owner_id,
            username=username,
            email=str(request.data.get('email') or ''),
            is_staff=bool(request.data.get('is_staff')),
            is_superuser=bool(request.data.get('is_superuser')),
            is_agent=bool(request.data.get('is_agent')),
            account_type=str(request.data.get('account_type') or 'human'),
        )
        if not eligible_for_default_personal_agent(record):
            return Response({
                'skipped': True,
                'reason': 'ineligible',
                'owner_id': owner_id,
            })
        try:
            result = ensure_personal_shizuha_agent(
                owner_id=owner_id,
                owner_username=username,
            )
        except PersonalShizuhaProvisionError as exc:
            return Response(
                {'error': str(exc), 'skipped': True, 'owner_id': owner_id},
                status=status.HTTP_409_CONFLICT,
            )
        except Exception:
            logger.exception('internal default personal agent ensure failed for user %s', owner_id)
            return Response(
                {'error': 'ensure failed', 'owner_id': owner_id},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response({
            'skipped': False,
            'created': result.created,
            'owner_id': owner_id,
            'agent_username': result.agent.agent_username,
            'status': result.agent.status,
            'isolated': bool(result.agent.isolated),
            'hibernation_reason': result.agent.hibernation_reason,
        }, status=status.HTTP_201_CREATED if result.created else status.HTTP_200_OK)


class FleetAgentDetailView(APIView):
    """GET/PATCH/DELETE /v1/fleet/agents/{id}/"""

    def get(self, request, agent_id):
        if not getattr(request.user, 'is_authenticated', False):
            return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
        result = _get_agent(agent_id)
        if isinstance(result, Response):
            # The fleet list deliberately includes live daemon/BYO agents that
            # may not have a native FleetAgent row yet. Detail must resolve the
            # same visible inventory instead of returning a transient 404 while
            # daemon-link reconciliation catches up.
            external = _owned_external_agent(request, agent_id)
            if external is not None:
                payload = _with_telemetry(dict(external))
                if getattr(request.user, 'is_staff', False):
                    _attach_identity_routing_health([payload])
                    _attach_health_summaries([payload])
                return Response(payload)
            personal = _owned_personal_agent(request, agent_id)
            if personal is not None:
                return Response(_with_telemetry(personal))
            return result
        if not getattr(request.user, 'is_staff', False):
            if result.owner_id != getattr(request.user, 'id', None):
                return Response({'error': 'Staff access required'}, status=status.HTTP_403_FORBIDDEN)
            payload = FleetAgentSerializer(result).data
            from .runtime_fleet import enrich_k8s_placement
            payload = enrich_k8s_placement(payload, force=True)
            return Response(_with_telemetry(payload))
        payload = FleetAgentSerializer(result).data
        from .runtime_fleet import enrich_k8s_placement
        payload = _with_telemetry(enrich_k8s_placement(payload, force=True))
        from .models import RuntimeChange
        from .runtime_fleet import runtime_change_payload
        latest_runtime_change = RuntimeChange.objects.filter(agent=result).first()
        if latest_runtime_change is not None:
            payload['runtime_change'] = runtime_change_payload(latest_runtime_change)
        _attach_identity_routing_health([payload])
        _attach_health_summaries([payload])
        # Same Pulse placement as the grid. Detail used to omit team_memberships,
        # so opening the drawer replaced the card snapshot with Unassigned.
        _attach_pulse_team_memberships(
            request, [payload], org_slug=_request_org_slug(request), drop_unscoped=False,
        )
        return Response(payload)

    def patch(self, request, agent_id):
        denied = _require_staff(request)
        if denied:
            return denied
        result = _get_agent(agent_id)
        if isinstance(result, Response):
            # Top-down config (Hive as source of truth): a runtime (rt-fleet)
            # agent has no native DB row, so push the edit to the daemon, which
            # persists agents.json and reloads the agent. Config flows
            # Hive -> daemon -> agent, unifying the two planes.
            external = get_runtime_agent(str(agent_id))
            cfg = RuntimeAgentConfigSerializer(data=request.data)
            if not cfg.is_valid():
                return Response(cfg.errors, status=status.HTTP_400_BAD_REQUEST)
            updates = dict(cfg.validated_data)
            if not updates:
                return Response({'error': 'no config fields provided'}, status=status.HTTP_400_BAD_REQUEST)
            if external:
                from .runtime_lane import RUNTIME_LANE_FIELDS, lane_payload

                base_generation = updates.pop('base_generation', None)
                lane_change = bool(RUNTIME_LANE_FIELDS.intersection(updates))
                previous_model = str(external.get('model') or external.get('effective_model') or '')
                from .runtime_fleet import _find_runtime_agent_row
                runtime_row = _find_runtime_agent_row(str(agent_id))
                observed_before = (
                    observed_runtime_lane_payload(runtime_row)
                    if runtime_row is not None
                    else {'known': False, 'state': 'UNKNOWN'}
                )
                if lane_change:
                    ok, admission = admit_runtime_lane_update(
                        str(agent_id),
                        updates,
                        current_agent=external,
                        actor=str(_actor(request)),
                        base_generation=base_generation,
                    )
                    if not ok:
                        code = admission.get('code') or 'RUNTIME_LANE_REJECTED'
                        response_status = (
                            status.HTTP_409_CONFLICT if code == 'GENERATION_CONFLICT'
                            else status.HTTP_502_BAD_GATEWAY if code.startswith('ACTUATOR_')
                            else status.HTTP_400_BAD_REQUEST
                        )
                        return Response({
                            'error': admission.get('error') or 'Runtime lane rejected',
                            'code': code,
                        }, status=response_status)
                    desired_lane = lane_payload(admission['lane'])
                    fresh = get_runtime_agent(str(agent_id)) or dict(external)
                    fresh.update(desired_lane)
                    fresh['effective_model'] = desired_lane['model']
                    resp = _with_telemetry(attach_context_budget(fresh))
                    resp.update({
                        'desired': {
                            **desired_lane,
                            'generation': admission.get('generation'),
                            'registry_version': admission['lane'].get('registry_version'),
                            'lane_digest': admission.get('lane_digest'),
                        },
                        'observed': observed_before,
                        'rollout_state': admission['rollout_state'],
                        'change_id': admission.get('change_id'),
                        'rollout_state_url': admission.get('rollout_state_url'),
                        'credential_preflight': admission['credential_preflight'],
                        'derivations': admission['lane'].get('derivations') or [],
                        'pending_sync': True,
                        'pending_sync_reason': admission.get('pending_reason') or '',
                    })
                    agent_index_cache.invalidate('configure runtime lane')
                    if previous_model and previous_model != desired_lane.get('model'):
                        from types import SimpleNamespace
                        from .lifecycle_fleet import drop_previous_model_occupancy
                        drop_previous_model_occupancy(
                            SimpleNamespace(
                                agent_username=(
                                    external.get('username')
                                    or external.get('agent_username')
                                    or ''
                                ),
                                agent_id=external.get('id') or agent_id,
                                model=desired_lane.get('model') or '',
                            ),
                            previous_model,
                            reset_session=False,
                        )
                    return Response(resp, status=status.HTTP_202_ACCEPTED)
                # Hive Agents UI is SoT: PATCH here → daemon agents.json → reload.
                # Never dual-write from ad-hoc control-proxy scripts.
                from .runtime_fleet import normalize_runtime_config_updates
                desired = normalize_runtime_config_updates(updates)
                ok, err = configure_runtime_agent(str(agent_id), desired)
                if not ok:
                    return Response({'error': f'Config push to runtime failed: {err}'},
                                    status=status.HTTP_502_BAD_GATEWAY)
                # HIVE-601: bust the agents-index read model so the Agents GRID
                # (served from agent_index_cache) reflects the new config on the
                # next poll instead of serving the stale cached model for up to
                # FRESH_TTL. The native-row PATCH branch below already does this
                # ('configure'); the rt-fleet branch previously only overlaid the
                # DETAIL response, so the drawer updated but the grid lagged.
                agent_index_cache.invalidate('configure runtime')
                queued = err and err.startswith('queued')
                # Index may lag one poll; overlay desired fields so the Agents UI
                # shows the conversion immediately after save.
                fresh = get_runtime_agent(str(agent_id)) or dict(external)
                for key in (
                    'execution_method', 'model_overrides', 'model_fallbacks',
                    'role', 'context_prompt', 'skills', 'eager_skills', 'model',
                ):
                    if key in desired:
                        fresh[key] = desired[key]
                if desired.get('model'):
                    fresh['effective_model'] = desired['model']
                resp = _with_telemetry(attach_context_budget(fresh))
                if queued:
                    resp['pending_sync'] = True
                    resp['pending_sync_reason'] = err
                    return Response(resp, status=status.HTTP_202_ACCEPTED)
                return Response(resp)
            # HIVE-195 Phase 3: BYO/containerized agent (no daemon row) → push over
            # Connect WS so config flows top-down the same way for every agent.
            from .external_agents import configure_agent_via_connect, telemetry_username_for_id
            uname = telemetry_username_for_id(str(agent_id))
            if not uname:
                return result  # genuinely unknown agent (404)
            ok, err = configure_agent_via_connect(uname, updates)
            if not ok:
                return Response({'error': f'Connect config push failed: {err}'},
                                status=status.HTTP_502_BAD_GATEWAY)
            # HIVE-601: same index-lag fix for BYO/containerized agents pushed
            # over Connect — bust the read model so the grid reflects the change.
            agent_index_cache.invalidate('configure external')
            return Response({'ok': True, 'agent': uname, 'pushed': list(updates.keys()),
                             'transport': 'connect-ws'})
        agent = result
        serializer = FleetAgentConfigureSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        data = dict(serializer.validated_data)
        requested_fields = set(data)
        base_generation = data.pop('base_generation', None)
        from .runtime_lane import RUNTIME_LANE_FIELDS, lane_payload
        lane_change = bool(RUNTIME_LANE_FIELDS.intersection(data))
        previous_model = str(getattr(agent, 'model', '') or '')
        observed_before = observed_runtime_lane_payload(agent)
        lane_admission = None
        # A daemon-managed FleetAgent is both Hive's durable desired-state row
        # and the daemon-link cache row.  Persisting it is not enough: without a
        # config_update frame, the running agent keeps the old model until the
        # daemon happens to reconnect.  Retain the flat runtime fields before
        # folding capability conveniences into Hive's JSON documents, then push
        # those accepted edits through the same Hive-owned actuator used by
        # runtime-only rows above.
        runtime_field_names = {
            'role', 'model', 'reasoning_effort', 'model_overrides',
            'model_fallbacks', 'execution_method', 'skills', 'eager_skills',
            'enabled_mcp_server_ids', 'context_prompt', 'personality_traits',
            'env', 'failover_chain_id', 'warm_pool_size',
        }
        runtime_updates = {
            field: value for field, value in data.items()
            if field in runtime_field_names
        }
        if lane_change:
            ok, lane_admission = admit_runtime_lane_update(
                str(agent.id),
                runtime_updates,
                actor=str(_actor(request)),
                base_generation=base_generation,
            )
            if not ok:
                code = lane_admission.get('code') or 'RUNTIME_LANE_REJECTED'
                response_status = (
                    status.HTTP_409_CONFLICT if code == 'GENERATION_CONFLICT'
                    else status.HTTP_502_BAD_GATEWAY if code.startswith('ACTUATOR_')
                    else status.HTTP_400_BAD_REQUEST
                )
                return Response({
                    'error': lane_admission.get('error') or 'Runtime lane rejected',
                    'code': code,
                }, status=response_status)
            # Admission applied every runtime field in one locked transaction and
            # emitted one complete config frame. Do not persist/send those fields
            # a second time through the legacy partial-update path below.
            agent.refresh_from_db()
            for field in runtime_field_names:
                data.pop(field, None)
            runtime_updates = {}

        # Native FleetAgent rows store capabilities in the established JSON
        # documents. The editor intentionally uses the same flat payload as the
        # rt-fleet/Connect branches; merge it here so a successful PATCH cannot
        # silently discard capability edits, and preserve config owned by other
        # controllers (profiles, flags, server descriptors, etc.).
        if 'skills' in data or 'eager_skills' in data:
            skill_config = dict(agent.skill_config or {})
            if 'skills' in data:
                skill_config['skills'] = data.pop('skills') or []
            if 'eager_skills' in data:
                skill_config['eager_skills'] = data.pop('eager_skills') or []
            data['skill_config'] = skill_config
        if 'enabled_mcp_server_ids' in data:
            ids = data.pop('enabled_mcp_server_ids') or []
            mcp_config = dict(agent.mcp_config or {})
            mcp_config['enabled_mcp_server_ids'] = ids
            mcp_config['mcp_servers'] = [
                {'slug': slug, 'name': slug} for slug in ids if slug
            ]
            data['mcp_config'] = mcp_config
        if 'execution_method' in data:
            method = data.pop('execution_method')
            # Native daemon-managed rows persist runtime method inside the same
            # mcp_config document the harness consumes. Keep unrelated MCP
            # settings intact while the flat editor field also flows through
            # runtime_updates to the live daemon below.
            mcp_config = dict(data.get('mcp_config') or agent.mcp_config or {})
            mcp_config['execution_method'] = method
            data['mcp_config'] = mcp_config
        if 'warm_pool_size' in data:
            # Flat editor field — FleetAgent has no column. Fold into mcp_config
            # so a PATCH cannot setattr a phantom field and then fail save, and
            # so on-demand keep-warm can read the reservation.
            mcp_config = dict(data.get('mcp_config') or agent.mcp_config or {})
            mcp_config['warm_pool_size'] = int(data.pop('warm_pool_size') or 0)
            data['mcp_config'] = mcp_config
        update_fields = []
        for field, value in data.items():
            setattr(agent, field, value)
            update_fields.append(field)
        if update_fields:
            update_fields.append('updated_at')
            try:
                agent.save(update_fields=update_fields)
            except ValidationError as e:
                return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
            _log(agent, 'configured', f'Updated: {", ".join(f for f in update_fields if f != "updated_at")}')
            agent_index_cache.invalidate('configure')
            if agent.hostplane_ssh and 'hostplane_ssh' in update_fields:
                from . import provisioner as fleet_provisioner
                try:
                    fleet_provisioner.apply_hostplane_ssh(agent)
                except Exception as exc:  # noqa: BLE001
                    logger.exception('hostplane_ssh apply failed for %s: %s', agent.agent_username, exc)
        k3s_apply = None
        if requested_fields & K3S_PATCH_LANE_FIELDS:
            # Hive-rendered Deployments are the actuator. Daemon config_update
            # never reaches them (2026-09-05: kai/nagi Hive grok-4.6, pods GLM).
            try:
                agent.refresh_from_db()
                k3s_apply = apply_k3s_lane_after_patch(
                    agent, previous_model=previous_model,
                )
                if (
                    k3s_apply.get('applied')
                    and lane_admission is not None
                    and lane_admission.get('generation') is not None
                    and lane_admission.get('lane_digest')
                ):
                    process_runtime_lane_config_result(agent, {
                        'ok': True,
                        'desired_generation': lane_admission.get('generation'),
                        'runtime_lane_digest': lane_admission.get('lane_digest'),
                    })
            except Exception:
                logger.exception(
                    'k3s lane converge after Hive PATCH failed for %s',
                    agent.agent_username,
                )
                k3s_apply = {'applied': False, 'reason': 'converge_exception'}
        if agent.managing_daemon_id and runtime_updates:
            from .runtime_fleet import normalize_runtime_config_updates
            desired = normalize_runtime_config_updates(runtime_updates)
            ok, err = configure_runtime_agent(str(agent.id), desired)
            if not ok:
                return Response(
                    {
                        'error': f'Config saved in Hive but immediate runtime push failed: {err}',
                        'pending_sync': True,
                    },
                    status=status.HTTP_502_BAD_GATEWAY,
                )
            if err and err.startswith('queued'):
                payload = dict(FleetAgentSerializer(agent).data)
                payload['pending_sync'] = True
                payload['pending_sync_reason'] = err
                if k3s_apply is not None:
                    payload['k3s_lane_apply'] = k3s_apply
                return Response(payload, status=status.HTTP_202_ACCEPTED)
        payload = dict(FleetAgentSerializer(agent).data)
        if k3s_apply is not None:
            payload['k3s_lane_apply'] = k3s_apply
        if lane_admission is not None:
            desired_lane = lane_payload(lane_admission['lane'])
            rollout_state = lane_admission['rollout_state']
            pending_reason = lane_admission.get('pending_reason') or ''
            pending_sync = True
            if k3s_apply and k3s_apply.get('applied'):
                from .models import RuntimeChange
                change = RuntimeChange.objects.filter(
                    id=lane_admission.get('change_id'),
                ).first()
                if change is not None:
                    rollout_state = change.state
                if rollout_state != 'ACTUATOR_PENDING':
                    pending_sync = False
                    pending_reason = (
                        'k3s Deployment lane env reconciled; pod rolling onto Hive model'
                    )
            payload.update({
                'desired': {
                    **desired_lane,
                    'generation': lane_admission.get('generation'),
                    'registry_version': lane_admission['lane'].get('registry_version'),
                    'lane_digest': lane_admission.get('lane_digest'),
                },
                'observed': observed_before,
                'rollout_state': rollout_state,
                'change_id': lane_admission.get('change_id'),
                'rollout_state_url': lane_admission.get('rollout_state_url'),
                'credential_preflight': lane_admission['credential_preflight'],
                'derivations': lane_admission['lane'].get('derivations') or [],
                'pending_sync': pending_sync,
                'pending_sync_reason': pending_reason,
            })
            return Response(payload, status=status.HTTP_202_ACCEPTED)
        return Response(payload)

    def delete(self, request, agent_id):
        denied = _require_staff(request)
        if denied:
            return denied
        result = _get_agent(agent_id)
        if isinstance(result, Response):
            return result
        agent = result
        if agent.status in {FleetAgent.Status.DELETING, FleetAgent.Status.DELETED}:
            return Response({'error': 'Agent is already being deleted'}, status=status.HTTP_409_CONFLICT)
        if agent.managing_daemon_id:
            from .lifecycle import retire_fleet_agent
            retire_fleet_agent(agent, actor=_actor(request), reason='deleted')
            return Response(status=status.HTTP_204_NO_CONTENT)
        was_running = agent.status == FleetAgent.Status.RUNNING
        ok = provisioner.deprovision_agent(agent)
        if not ok:
            # Operator 2026-07-12 ("near instant, no delays"): a failed/pending/
            # stopped or daemon-managed row often has NOTHING k3s-native to tear
            # down, so deprovision "failure" must not wedge deletion behind a
            # teardown that can never succeed (e.g. the failed `claude` seat:
            # no OAuth token, rt-fleet runtime → undeletable forever). Force-
            # delete: best-effort daemon-plane disable, then mark the row
            # DELETED. Only a RUNNING agent whose real teardown failed keeps
            # the hard error — silently orphaning live pods is worse.
            if was_running:
                return Response(
                    {'error': 'Deprovisioning failed for a running agent; check events for details.'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
            try:
                set_runtime_agent_enabled(str(agent.id), False)
            except Exception:
                logger.exception('force-delete: daemon disable best-effort failed for %s', agent.agent_username)
            agent.status = FleetAgent.Status.DELETED
            agent.save(update_fields=['status'])
            FleetAgentEvent.objects.create(
                agent=agent, event_type='deleted',
                message=f'force-deleted by {_actor(request)} (deprovision inapplicable/failed; non-running)',
            )
        # A deleted agent must not linger in the archive list.
        try:
            FleetAgentArchive.objects.filter(agent_username=agent.agent_username).delete()
        except Exception:
            logger.exception('archive-marker cleanup failed for %s (non-fatal)', agent.agent_username)
        agent_index_cache.invalidate('deprovision/delete')
        return Response(status=status.HTTP_204_NO_CONTENT)


class FleetAgentClaimView(APIView):
    """POST /v1/fleet/agents/{id}/claim/ — claim an orphan external/BYO agent.

    Ownership is derived from the authenticated Hive caller, never from an agent
    token or request body owner id. Staff may reassign/unclaim via action.
    """

    def post(self, request, agent_id):
        if _is_agent_token(request):
            return Response({'error': 'Agent tokens cannot claim ownership'}, status=status.HTTP_403_FORBIDDEN)
        external = _nondb_agent(agent_id)
        if not external or not external.get('is_external_byo'):
            return Response({'error': 'Claim is only supported for external/BYO agents'}, status=status.HTTP_404_NOT_FOUND)
        actor_user_id = getattr(request.user, 'id', None)
        if not actor_user_id:
            return Response({'error': 'Authenticated human user required'}, status=status.HTTP_403_FORBIDDEN)
        action = request.data.get('action') or 'claim'
        if action in ('unclaim', 'clear', 'reassign') and not getattr(request.user, 'is_staff', False):
            return Response({'error': 'Only staff can unclaim/reassign agents'}, status=status.HTTP_403_FORBIDDEN)
        agent_user_id = external.get('agent_id') or str(agent_id).replace('ext:', '')
        from .external_agents import claim_external_agent
        ok, detail = claim_external_agent(
            agent_user_id,
            actor_user_id=actor_user_id,
            actor_email=getattr(request.user, 'email', ''),
            is_staff=getattr(request.user, 'is_staff', False),
            action=action,
        )
        if not ok:
            code = status.HTTP_409_CONFLICT if isinstance(detail, dict) and detail.get('reason') == 'owned_by_another_user' else status.HTTP_502_BAD_GATEWAY
            if isinstance(detail, dict) and detail.get('error'):
                code = status.HTTP_403_FORBIDDEN if 'Only staff' in str(detail.get('error')) else code
            return Response({'error': detail}, status=code)
        logger.warning('external-agent claim: actor=%s action=%s agent=%s result=%s',
                       _actor(request), action, external.get('agent_username'), detail)
        agent_index_cache.invalidate(f'claim {action}')
        refreshed = _nondb_agent(agent_id) or external
        return Response({'ok': True, 'claim': detail, 'agent': refreshed})


class FleetAgentEnableView(APIView):
    """POST /v1/fleet/agents/{id}/enable/ — scale 0 → 1."""

    def post(self, request, agent_id):
        denied = _require_staff(request)
        if denied:
            return denied
        result = _get_agent(agent_id)
        # Archived agents must never start — not even manually — until
        # unarchived (operator 2026-07-10).
        username = result.agent_username if not isinstance(result, Response) else (
            (get_runtime_agent(str(agent_id)) or {}).get('agent_username') or '')
        if _is_archived_username(username):
            return Response(
                {'error': f'Agent {username} is archived — unarchive it before starting.'},
                status=status.HTTP_409_CONFLICT,
            )
        if isinstance(result, Response):
            # Not a native k3s row -> fall through to the live rt-fleet plane (HIVE-171).
            return _rt_fleet_control(request, agent_id, 'enable')
        agent = result
        if agent.status == FleetAgent.Status.RUNNING:
            live = provisioner.get_agent_status(agent)
            if live.get('pod_status') == 'Running' and live.get('ready_containers', 0) > 0:
                return Response({'status': agent.status, 'message': 'Agent is already running'})
            logger.warning(
                'agent %s marked running but live pod is not ready; re-scaling deployment',
                agent.agent_username,
            )
            ok = provisioner.enable_or_provision_agent(agent)
            if not ok:
                waiting = _lease_wait_response(agent)
                if waiting is not None:
                    return waiting
                return Response(
                    {'error': provisioner.start_failure_message(agent)},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
            _clear_stale_operator_stop(agent)
            agent_index_cache.invalidate('native enable (rescale)')
            agent.refresh_from_db()
            return Response(FleetAgentSerializer(agent).data)
        if agent.sandbox_required:
            from .provisioner import _sandbox_substitute_configured
            if not _sandbox_substitute_configured():
                return Response(
                    {'error': 'sandbox_required agent cannot be enabled: no sandbox substitute configured'},
                    status=status.HTTP_409_CONFLICT,
                )
        ok = provisioner.enable_or_provision_agent(agent)
        if not ok:
            waiting = _lease_wait_response(agent)
            if waiting is not None:
                return waiting
            return Response(
                {'error': provisioner.start_failure_message(agent)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        _clear_stale_operator_stop(agent)
        agent_index_cache.invalidate('native enable')
        agent.refresh_from_db()
        return Response(FleetAgentSerializer(agent).data)


class FleetAgentTeamPlacementView(APIView):
    """GET/POST /v1/fleet/agents/{id}/team-placement/ — staff roster placement.

    Reads/writes Pulse TeamRoster, the same source Pulse routing and Hive list
    grouping use. This intentionally does not infer teams from runtime role/profile
    and does not touch runtime lifecycle.
    """

    def _agent(self, agent_id):
        result = _get_agent(agent_id)
        if isinstance(result, Response):
            agent = _nondb_agent(agent_id)
            if not agent:
                return None, result
            return agent, None
        return FleetAgentSerializer(result).data, None

    def get(self, request, agent_id):
        denied = _require_staff(request)
        if denied:
            return denied
        agent, error = self._agent(agent_id)
        if error:
            return error
        org_slug = _request_org_slug(request)
        return _team_placement_response(request, org_slug, agent)

    def post(self, request, agent_id):
        denied = _require_staff(request)
        if denied:
            return denied
        agent, error = self._agent(agent_id)
        if error:
            return error
        org_slug = _request_org_slug(request, request.data)
        action = (request.data.get('action') or 'add').lower()
        team_id = request.data.get('team_id')
        role = request.data.get('membership_role') or request.data.get('role') or 'member'
        if action not in {'add', 'remove', 'update_role'}:
            return Response({'error': 'action must be add, remove, or update_role'}, status=status.HTTP_400_BAD_REQUEST)
        if not team_id:
            return Response({'error': 'team_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        agent_email = _agent_email(agent)
        teams, pulse_memberships, error = _pulse_roster_team_options(
            request, agent_email, org_slug=org_slug,
        )
        if error:
            return error
        exact_matches = [t for t in teams if str(t.get('id')) == str(team_id)]
        slug_matches = [
            t for t in teams
            if _canon_team_name(t.get('team_name') or t.get('slug') or t.get('name'))
            == _canon_team_name(str(team_id))
        ]
        if not org_slug and not exact_matches and len(slug_matches) > 1:
            return Response(
                {'error': 'team_id is ambiguous across organizations; use the organization-qualified id'},
                status=status.HTTP_409_CONFLICT,
            )
        selected_team = (exact_matches or slug_matches or [None])[0]
        if not selected_team:
            return Response({'error': 'team_id does not match an active Pulse roster team'}, status=status.HTTP_404_NOT_FOUND)

        selected_team_key = selected_team.get('team_name') or selected_team.get('slug') or str(team_id)
        selected_team_org = _org_slug_from_payload(selected_team, default=org_slug or 'shizuha')
        if action in {'add', 'remove'}:
            # Pulse TeamRoster is the single membership source for Hive roster
            # placement, Pulse org-chart display, and Pulse task routing. Admin
            # receives only the parent organization membership required by the
            # cross-service tenancy boundary; never an Admin TeamMembership.
            if action == 'add':
                org_result = ensure_roster_org_membership(
                    email=agent_email,
                    organization_slug=selected_team_org,
                    display_name=str(agent.get('display_name') or agent.get('agent_username') or ''),
                )
                if not org_result.ok:
                    code = (
                        status.HTTP_409_CONFLICT
                        if org_result.status == 'identity_not_found'
                        else status.HTTP_502_BAD_GATEWAY
                    )
                    return Response({
                        'error': 'Agent organization membership could not be established',
                        'reason': org_result.status,
                        'detail': org_result.detail,
                    }, status=code)
            _, error = _write_pulse_roster_membership(
                request,
                action=action,
                team_name=selected_team_key,
                email=agent_email,
                org_slug=selected_team_org,
            )
            if error:
                return error
        if action == 'remove':
            _upsert_team_membership_role(
                request,
                agent_identifier=agent_email,
                team_slug=selected_team_key,
                organization_slug=selected_team_org,
                action='remove',
            )
        else:
            if action == 'update_role' and (
                selected_team_org, _canon_team_name(selected_team_key)
            ) not in pulse_memberships:
                return Response({'error': 'cannot update team role for a non-member'}, status=status.HTTP_404_NOT_FOUND)
            _upsert_team_membership_role(
                request,
                agent_identifier=agent_email,
                team_slug=selected_team_key,
                organization_slug=selected_team_org,
                action='add',
                payload={**request.data, 'membership_role': role},
            )

        agent_index_cache.invalidate(f'team-placement {action}')
        if action in {'add', 'remove'}:
            snapshot_error = _refresh_roster_membership_snapshot(request, agent_email)
            if snapshot_error:
                logger.warning(
                    'post-mutation roster snapshot refresh failed for %s: %s',
                    agent_email,
                    getattr(snapshot_error, 'data', snapshot_error),
                )
        # HIVE-619: roster mutations are capability state-change events. Push
        # the recomputed skill/MCP overlay through the existing daemon-link
        # config path now; the rt-fleet periodic scan is only a repair backstop.
        _runtime_capability_reconcile_for_agent_emails(
            request,
            {agent_email},
            context={'reason': f'team-placement:{action}', 'team_slug': selected_team_key},
        )
        return _team_placement_response(request, org_slug, agent, selected_team_id=team_id)


def _service_roster_teams():
    """List of Pulse TeamRoster teams (normalised, incl organization_slug) fetched
    via the NETWORK-SECURED internal endpoint — no user token. HIVE-617: backs the
    member-scoped team-structure read for non-staff org members."""
    base = getattr(settings, 'HIVE_PULSE_API_URL', '').rstrip('/')
    if not base:
        return []
    try:
        resp = requests.get(f'{base}/internal/team-roster/', timeout=5)
        if resp.status_code >= 400:
            logger.info('service roster teams: internal endpoint status %s', resp.status_code)
            return []
        payload = resp.json() or {}
    except (requests.RequestException, ValueError) as exc:
        logger.info('service roster teams fetch failed: %s', exc)
        return []
    out = _normalise_team_collection_payload(payload)
    teams = out.get('teams') if isinstance(out, dict) else out
    return teams if isinstance(teams, list) else []


def _caller_member_org_slugs(request):
    """Slugs of the orgs the caller is a VERIFIED member of, or None for staff
    (who may read all). HIVE-617: authoritative membership comes from the signed
    JWT (organization_memberships = {org_id: role}); admin supplies id->slug for
    the caller's own orgs. Intersecting the two means neither source alone can
    widen the set — this is the whole security boundary for member team reads."""
    if bool(getattr(request.user, 'is_staff', False)):
        return None
    memberships = getattr(request.user, 'organization_memberships', None) or {}
    member_ids = set()
    for key in memberships.keys():
        try:
            member_ids.add(int(key))
        except (TypeError, ValueError):
            continue
    if not member_ids:
        return set()
    orgs, error = _admin_organizations(request)
    if error or not orgs:
        return set()
    slugs = set()
    for org in orgs:
        try:
            oid = int(org.get('id'))
        except (TypeError, ValueError):
            continue
        if oid in member_ids:
            slug = str(org.get('slug') or '').strip().lower()
            if slug:
                slugs.add(slug)
    return slugs


class FleetTeamRosterView(APIView):
    """GET /v1/fleet/teams/ — team structure. Staff see all orgs; a non-staff
    MEMBER sees the teams of the org(s) they belong to (multi-org), and ONLY those.
    POST /v1/fleet/teams/ — create an empty canonical Pulse TeamRoster row (staff)."""

    def get(self, request):
        if not getattr(request.user, 'is_authenticated', False):
            return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
        org_slug = _request_org_slug(request)
        member_orgs = _caller_member_org_slugs(request)
        if member_orgs is not None:
            # HIVE-617: non-staff org member — scope strictly to their verified
            # orgs. Mutations (POST/PATCH/DELETE) remain staff-only; operational
            # internals (fleet snapshot workload/health) stay on their own
            # staff-gated endpoints. Membership is the entire boundary.
            if not member_orgs:
                return Response({'teams': [], 'source': 'member_scoped'})
            if org_slug and org_slug.lower() not in member_orgs:
                return Response({'error': 'Not a member of this organization'},
                                status=status.HTTP_403_FORBIDDEN)
            teams = [
                t for t in _service_roster_teams()
                if str(t.get('organization_slug') or '').strip().lower() in member_orgs
                and (not org_slug or str(t.get('organization_slug') or '').strip().lower() == org_slug.lower())
            ]
            return Response({'teams': teams, 'source': 'member_scoped'})
        # ---- staff path (unchanged) ----
        payload, error = _pulse_request(request, 'GET', '/admin/team-roster/')
        if error:
            return error
        out = _normalise_team_collection_payload(payload or {})
        if isinstance(out, dict):
            if org_slug:
                out['teams'] = [
                    team for team in out.get('teams') or []
                    if _org_slug_from_payload(team, default='shizuha') == org_slug.lower()
                ]
                out['organization_slug'] = org_slug
            out.setdefault('source', 'pulse_team_roster')
        return Response(out)

    def post(self, request):
        denied = _require_staff(request)
        if denied:
            return denied
        team_name = request.data.get('team_name') or request.data.get('name') or ''
        team_key = _canon_team_name(team_name)
        if not team_key:
            return Response({'error': 'team_name is required'}, status=status.HTTP_400_BAD_REQUEST)
        org_slug = _request_org_slug(request, request.data)
        payload, error = _create_pulse_roster_team(request, team_key, org_slug=org_slug)
        if error:
            return error
        ensure_team_required_capability(team_key, actor=_actor(request), reason='Created with Hive team roster.')
        return Response({
            'team': {
                'id': team_key,
                'name': _display_team_name(team_key),
                'slug': team_key,
                'team_name': team_key,
            },
            'pulse': payload or {},
        }, status=status.HTTP_201_CREATED)


class FleetTeamDetailView(APIView):
    """GET/PATCH/DELETE /v1/fleet/teams/<team_name>/ — proxy to Pulse TeamRoster detail."""

    def get(self, request, team_name):
        denied = _require_staff(request)
        if denied:
            return denied
        org_slug = _request_org_slug(request)
        team_key = _canon_team_name(team_name)
        payload, error = _pulse_request(
            request, 'GET', f'/admin/team-roster/{team_key}/',
            params={'organization_slug': org_slug} if org_slug else None,
        )
        if error:
            return error
        return Response(_normalise_team_payload(payload or {}))

    def patch(self, request, team_name):
        denied = _require_staff(request)
        if denied:
            return denied
        org_slug = _request_org_slug(request, request.data)
        team_key = _canon_team_name(team_name)
        data = dict(request.data)
        if org_slug:
            data['organization_slug'] = org_slug
        payload, error = _pulse_request(
            request, 'PATCH', f'/admin/team-roster/{team_key}/', json=data,
            params={'organization_slug': org_slug} if org_slug else None,
        )
        if error:
            return error
        return Response(_normalise_team_roster_payload(payload or {}))

    def delete(self, request, team_name):
        denied = _require_staff(request)
        if denied:
            return denied
        org_slug = _request_org_slug(request, getattr(request, 'data', None))
        team_key = _canon_team_name(team_name)
        payload, error = _pulse_request(
            request, 'DELETE', f'/admin/team-roster/{team_key}/',
            params={'organization_slug': org_slug} if org_slug else None,
        )
        if error:
            return error
        if not payload:
            return Response(status=status.HTTP_204_NO_CONTENT)
        return Response(payload)


class FleetTeamMembersView(APIView):
    """GET /v1/fleet/teams/<team_name>/members/ — list members for a team.
    POST — add or remove a member (action: add|remove, email required)."""

    def get(self, request, team_name):
        denied = _require_staff(request)
        if denied:
            return denied
        org_slug = _request_org_slug(request)
        team_key = _canon_team_name(team_name)
        payload, error = _pulse_request(
            request, 'GET', '/admin/team-roster/members/',
            params={'team_name': team_key},
        )
        if error:
            return error
        if org_slug:
            payload = dict(payload or {})
            payload['rosters'] = [
                roster for roster in payload.get('rosters') or []
                if _org_slug_from_payload(roster, default='shizuha') == org_slug.lower()
            ]
        return Response(_normalise_team_members_payload(payload or {}, team_key))

    def post(self, request, team_name):
        denied = _require_staff(request)
        if denied:
            return denied
        team_key = _canon_team_name(team_name)
        action = (request.data.get('action') or 'add').lower()
        email = (request.data.get('email') or '').strip()
        if action not in {'add', 'remove', 'update_role'}:
            return Response({'error': 'action must be add, remove, or update_role'}, status=status.HTTP_400_BAD_REQUEST)
        if not email:
            return Response({'error': 'email is required'}, status=status.HTTP_400_BAD_REQUEST)
        org_slug = _request_org_slug(request, request.data)
        payload = {}
        if action in {'add', 'remove'}:
            payload, error = _write_pulse_roster_membership(
                request, action=action, team_name=team_key, email=email,
                org_slug=org_slug,
            )
            if error:
                return error
        if action == 'remove':
            _upsert_team_membership_role(
                request,
                agent_identifier=email.lower(),
                team_slug=team_key,
                organization_slug=org_slug,
                action='remove',
            )
        else:
            _upsert_team_membership_role(
                request,
                agent_identifier=email.lower(),
                team_slug=team_key,
                organization_slug=org_slug,
                action='add',
                payload=request.data,
            )
        agent_index_cache.invalidate(f'team-members {action}')
        if action in {'add', 'remove'}:
            snapshot_error = _refresh_roster_membership_snapshot(request, email.lower())
            if snapshot_error:
                logger.warning(
                    'post-mutation roster snapshot refresh failed for %s: %s',
                    email.lower(),
                    getattr(snapshot_error, 'data', snapshot_error),
                )
        _runtime_capability_reconcile_for_agent_emails(
            request,
            {email.lower()},
            context={'reason': f'team-members:{action}', 'team_slug': team_key},
        )
        return Response(payload or {'changed': action == 'update_role', 'email': email.lower(), 'team_name': team_key})


class CapabilityListView(APIView):
    """GET/POST /v1/fleet/capabilities/ — Hive capability catalog."""

    def get(self, request):
        denied = _require_staff(request)
        if denied:
            return denied
        rows = CapabilityDefinition.objects.all()
        return Response({'capabilities': CapabilityDefinitionSerializer(rows, many=True).data})

    def post(self, request):
        denied = _require_staff(request)
        if denied:
            return denied
        serializer = CapabilityDefinitionSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        ref_errors = _validate_capability_refs(serializer.validated_data)
        if ref_errors:
            return Response(ref_errors, status=status.HTTP_400_BAD_REQUEST)
        try:
            row = serializer.save(created_by=_actor(request), updated_by=_actor(request))
        except ValidationError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        reconcile = _runtime_capability_reconcile_for_capability(request, row)
        after = CapabilityDefinitionSerializer(row).data
        _audit_capability(
            request,
            action='capability.create',
            target_type='capability',
            target_key=row.slug,
            after=after,
            reason=request.data.get('reason', ''),
            outcome='runtime-reconciled' if not reconcile.get('errors') else 'runtime-reconcile-partial',
            diagnostic=json.dumps(reconcile, sort_keys=True),
        )
        return Response({**after, 'runtime_reconcile': reconcile}, status=status.HTTP_201_CREATED)


class CapabilityDetailView(APIView):
    """GET/PATCH/DELETE /v1/fleet/capabilities/<slug>/."""

    def _row(self, slug):
        return CapabilityDefinition.objects.filter(slug=_canon_capability_slug(slug)).first()

    def get(self, request, slug):
        denied = _require_staff(request)
        if denied:
            return denied
        row = self._row(slug)
        if not row:
            return Response({'error': 'Capability not found'}, status=status.HTTP_404_NOT_FOUND)
        return Response(CapabilityDefinitionSerializer(row).data)

    def patch(self, request, slug):
        denied = _require_staff(request)
        if denied:
            return denied
        row = self._row(slug)
        if not row:
            return Response({'error': 'Capability not found'}, status=status.HTTP_404_NOT_FOUND)
        before = CapabilityDefinitionSerializer(row).data
        serializer = CapabilityDefinitionSerializer(row, data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        merged = dict(before)
        merged.update(serializer.validated_data)
        ref_errors = _validate_capability_refs(merged)
        if ref_errors:
            return Response(ref_errors, status=status.HTTP_400_BAD_REQUEST)
        requires_fresh_security_review = (
            row.activation_status == CapabilityDefinition.ActivationStatus.ACTIVE
            and (_capability_payload_is_privileged(before) or _capability_payload_is_privileged(merged))
            and _capability_review_fields_changed(before, merged)
        )
        save_kwargs = {'updated_by': _actor(request)}
        if requires_fresh_security_review:
            # HIVE-306: the bundle editor may change the exact skills/MCP/credential/runtime
            # surface a security reviewer approved. Keep the edit, but revoke the stale
            # active/reviewed state so the activation workflow must re-approve the new hash.
            save_kwargs.update({
                'activation_status': CapabilityDefinition.ActivationStatus.PENDING_SECURITY,
                'approved_by': '',
                'approved_at': None,
                'security_reviewed_by': '',
                'security_reviewed_at': None,
            })
        try:
            row = serializer.save(**save_kwargs)
        except ValidationError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        reconcile = _runtime_capability_reconcile_for_capability(request, row)
        after = CapabilityDefinitionSerializer(row).data
        _audit_capability(
            request,
            action='capability.update',
            target_type='capability',
            target_key=row.slug,
            before=before,
            after=after,
            reason=request.data.get('reason', ''),
            outcome=(
                'security_review_required'
                if requires_fresh_security_review
                else 'runtime-reconciled' if not reconcile.get('errors') else 'runtime-reconcile-partial'
            ),
            diagnostic=json.dumps(reconcile, sort_keys=True),
        )
        return Response({**after, 'runtime_reconcile': reconcile})

    def delete(self, request, slug):
        denied = _require_staff(request)
        if denied:
            return denied
        row = self._row(slug)
        if not row:
            return Response({'error': 'Capability not found'}, status=status.HTTP_404_NOT_FOUND)
        before = CapabilityDefinitionSerializer(row).data
        row.lifecycle_status = CapabilityDefinition.LifecycleStatus.DISABLED
        row.activation_status = CapabilityDefinition.ActivationStatus.DISABLED
        row.updated_by = _actor(request)
        try:
            row.save()
        except ValidationError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        reconcile = _runtime_capability_reconcile_for_capability(request, row)
        after = CapabilityDefinitionSerializer(row).data
        _audit_capability(
            request,
            action='capability.disable',
            target_type='capability',
            target_key=row.slug,
            before=before,
            after=after,
            reason=request.data.get('reason', ''),
            outcome='runtime-reconciled' if not reconcile.get('errors') else 'runtime-reconcile-partial',
            diagnostic=json.dumps(reconcile, sort_keys=True),
        )
        return Response({**after, 'runtime_reconcile': reconcile})


class CapabilityActionView(APIView):
    """POST /v1/fleet/capabilities/<slug>/<action>/ — activation workflow."""

    def post(self, request, slug, action):
        denied = _require_staff(request)
        if denied:
            return denied
        row = CapabilityDefinition.objects.filter(slug=_canon_capability_slug(slug)).first()
        if not row:
            return Response({'error': 'Capability not found'}, status=status.HTTP_404_NOT_FOUND)
        before = CapabilityDefinitionSerializer(row).data
        actor = _actor(request)
        if action == 'reconcile':
            reconcile = _runtime_capability_reconcile_for_capability(request, row)
            current = CapabilityDefinitionSerializer(row).data
            _audit_capability(
                request,
                action='capability.reconcile',
                target_type='capability',
                target_key=row.slug,
                before=before,
                after=current,
                reason=request.data.get('reason', ''),
                outcome='runtime-reconciled' if not reconcile.get('errors') else 'runtime-reconcile-partial',
                diagnostic=json.dumps(reconcile, sort_keys=True),
            )
            code = status.HTTP_207_MULTI_STATUS if reconcile.get('errors') else status.HTTP_200_OK
            return Response({**current, 'runtime_reconcile': reconcile}, status=code)
        if action == 'submit-security-review':
            row.activation_status = CapabilityDefinition.ActivationStatus.PENDING_SECURITY
            row.approval_task_key = request.data.get('approval_task_key', row.approval_task_key)
            row.approval_wiki_page_id = request.data.get('approval_wiki_page_id', row.approval_wiki_page_id)
        elif action == 'approve':
            row.activation_status = CapabilityDefinition.ActivationStatus.APPROVED
            row.approved_by = actor
            row.approved_at = timezone.now()
            if row.is_privileged:
                row.security_reviewed_by = actor
                row.security_reviewed_at = row.approved_at
        elif action == 'activate':
            ref_errors = _validate_capability_refs(row.hash_payload())
            if ref_errors:
                return Response(ref_errors, status=status.HTTP_400_BAD_REQUEST)
            if row.is_privileged and not (row.security_reviewed_by and row.security_reviewed_at):
                return Response({'error': 'Security review is required before activation'}, status=status.HTTP_400_BAD_REQUEST)
            row.activation_status = CapabilityDefinition.ActivationStatus.ACTIVE
            if row.lifecycle_status == CapabilityDefinition.LifecycleStatus.DRAFT:
                row.lifecycle_status = CapabilityDefinition.LifecycleStatus.DRAFT
        elif action == 'disable':
            row.activation_status = CapabilityDefinition.ActivationStatus.DISABLED
            row.lifecycle_status = CapabilityDefinition.LifecycleStatus.DISABLED
        else:
            return Response({'error': 'Unknown capability action'}, status=status.HTTP_400_BAD_REQUEST)
        row.updated_by = actor
        try:
            row.save()
        except ValidationError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        reconcile = _runtime_capability_reconcile_for_capability(request, row)
        after = CapabilityDefinitionSerializer(row).data
        _audit_capability(
            request,
            action=f'capability.{action}',
            target_type='capability',
            target_key=row.slug,
            before=before,
            after=after,
            reason=request.data.get('reason', ''),
            approval_ref=request.data.get('approval_task_key', '') or row.approval_task_key,
            outcome='runtime-reconciled' if not reconcile.get('errors') else 'runtime-reconcile-partial',
            diagnostic=json.dumps(reconcile, sort_keys=True),
        )
        return Response({**after, 'runtime_reconcile': reconcile})


class TeamCapabilitiesView(APIView):
    """GET/PUT /v1/fleet/teams/<team>/capabilities/."""

    def get(self, request, team_name):
        denied = _require_staff(request)
        if denied:
            return denied
        team_key = _canon_team_name(team_name)
        rows = TeamRequiredCapability.objects.select_related('capability').filter(team_slug=team_key, enabled=True)
        return Response({'team_slug': team_key, 'capabilities': TeamRequiredCapabilitySerializer(rows, many=True).data})

    def put(self, request, team_name):
        denied = _require_staff(request)
        if denied:
            return denied
        team_key = _canon_team_name(team_name)
        specs = request.data.get('capabilities', request.data.get('capability_slugs', []))
        if not isinstance(specs, list):
            return Response({'error': 'capabilities must be a list'}, status=status.HTTP_400_BAD_REQUEST)
        before_rows = TeamRequiredCapabilitySerializer(
            TeamRequiredCapability.objects.select_related('capability').filter(team_slug=team_key),
            many=True,
        ).data
        wanted = {}
        for item in specs:
            if isinstance(item, str):
                slug, reason = item, ''
            else:
                slug, reason = item.get('capability_slug') or item.get('slug'), item.get('reason', '')
            slug = _canon_capability_slug(slug)
            cap = CapabilityDefinition.objects.filter(slug=slug).first()
            if not cap:
                return Response({'error': f'Unknown capability {slug}'}, status=status.HTTP_400_BAD_REQUEST)
            if cap.activation_status != CapabilityDefinition.ActivationStatus.ACTIVE:
                return Response({'error': f'Capability {slug} is not active'}, status=status.HTTP_400_BAD_REQUEST)
            wanted[slug] = (cap, reason)
        with transaction.atomic():
            TeamRequiredCapability.objects.filter(team_slug=team_key).exclude(capability__slug__in=wanted.keys()).update(
                enabled=False, updated_by=_actor(request),
            )
            for slug, (cap, reason) in wanted.items():
                req, _ = TeamRequiredCapability.objects.update_or_create(
                    team_slug=team_key,
                    capability=cap,
                    defaults={
                        'required': True,
                        'enabled': True,
                        'reason': reason,
                        'updated_by': _actor(request),
                    },
                )
                if not req.created_by:
                    req.created_by = _actor(request)
                    req.save(update_fields=['created_by'])
        rows = TeamRequiredCapability.objects.select_related('capability').filter(team_slug=team_key, enabled=True)
        after = TeamRequiredCapabilitySerializer(rows, many=True).data
        reconcile = _runtime_capability_reconcile_for_team(request, team_key)
        _audit_capability(
            request,
            action='team_capabilities.set',
            target_type='team',
            target_key=team_key,
            before={'capabilities': before_rows},
            after={'capabilities': after},
            reason=request.data.get('reason', ''),
            outcome='runtime-reconciled' if not reconcile.get('errors') else 'runtime-reconcile-partial',
            diagnostic=json.dumps(reconcile, sort_keys=True),
        )
        return Response({'team_slug': team_key, 'capabilities': after, 'runtime_reconcile': reconcile})


class AgentEffectiveCapabilitiesView(APIView):
    """GET /v1/fleet/agents/<id>/effective-capabilities/.

    Two callers: the fleet daemon (X-Hive-Daemon-* credential) and the Hive UI
    (staff JWT). Auth must run so a staff JWT resolves request.user.is_staff —
    an empty authentication_classes left request.user Anonymous, so the UI's
    _require_staff check ALWAYS 403'd ("Effective capabilities unavailable").
    permission_classes stays empty: the view's own _require_staff_or_daemon_credential
    gate authorizes both callers (JWTAuthentication returns None for the header-
    less daemon request, so it falls through to the daemon-credential path)."""
    authentication_classes = [FederatedJWTAuthentication]
    permission_classes = []

    def get(self, request, agent_id):
        denied = _require_staff_or_daemon_credential(request)
        if denied:
            return denied
        payload, error = _effective_capability_set(request, agent_id)
        if error:
            return error
        # PLAT-4000: HMAC-sign the emitted payload so the fleet daemon's fail-closed
        # trust gate can verify it and stop withholding credential scopes + sensitive
        # MCP. Signed here (not in _effective_capability_set) so the internal reconcile
        # path and the HIVE-417 delegation contract keep the unsigned dict; only the
        # emitted HTTP payload the daemon fetches carries the signature. No-op (unsigned)
        # until the shared secret is provisioned to Hive.
        payload = attach_effective_capabilities_signature(payload)
        return Response(payload)


class AgentMcpProjectionView(APIView):
    """POST a fresh nonce and receive Hive's current signed MCP ceiling.

    Only the broker/workload credential reaches this route. The agent audience
    is resolved from Hive's agent row and never accepted from the request body.
    Every response is computed live; any auth/read/signing failure emits no
    signed envelope, so a silent/stale producer cannot extend authority.
    """
    authentication_classes = [FederatedJWTAuthentication]
    permission_classes = []

    def post(self, request, agent_id):
        from claws import coordinator
        if not coordinator.is_broker_request(request):
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        if not coordinator.broker_identity(request):
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        nonce = str((request.data or {}).get('nonce') or '')
        payload, error = _effective_capability_set(request, agent_id)
        if error:
            return error
        identity, identity_error = _effective_agent_identity(agent_id)
        if identity_error:
            return identity_error
        try:
            projection_agent = FleetAgent.objects.exclude(status=FleetAgent.Status.DELETED).filter(
                agent_id=str(identity['id']),
            ).first()
            if projection_agent is None:
                projection_agent = FleetAgent.objects.exclude(status=FleetAgent.Status.DELETED).filter(
                    agent_username=str(identity.get('username') or ''),
                ).first()
            if projection_agent is None:
                raise FleetAgent.DoesNotExist
        except FleetAgent.DoesNotExist:
            # Native rows use UUID primary keys as the projection state key;
            # daemon-managed rows resolve by their immutable runtime agent_id.
            try:
                projection_agent = FleetAgent.objects.exclude(
                    status=FleetAgent.Status.DELETED,
                ).get(id=str(identity['id']))
            except (FleetAgent.DoesNotExist, ValueError, ValidationError):
                return Response({'error': 'projection unavailable'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        # Shared org broker credentials intentionally address many agent pods,
        # so the credential subject is not the per-agent audience. The route
        # path is daemon-owned configuration, and the signed envelope binds its
        # resolved immutable runtime ID; writable request fields cannot replace it.
        try:
            envelope = canonical_projection_envelope(
                payload,
                agent_id=str(projection_agent.id),
                # State is keyed by Hive PK; the consuming pod binds AGENT_ID.
                # Hybrid seats have a distinct immutable runtime identity.
                agent_audience=str(projection_agent.agent_id or projection_agent.id),
                nonce=nonce,
            )
        except (RuntimeError, ValueError, FleetAgent.DoesNotExist) as exc:
            logger.warning('MCP projection refused for %s: %s', agent_id, exc)
            return Response({'error': 'projection unavailable'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return HttpResponse(envelope, content_type='application/json')


class AgentRuntimeLaneView(APIView):
    """GET /v1/fleet/agents/<id>/runtime-lane/ -- daemon SSOT backstop.

    This deliberately exposes only the fields the runtime controller must
    refresh before planning.  It accepts the same scoped daemon-link credential
    as effective-capabilities so a long-lived daemon never depends on a retained
    staff/user bearer.
    """
    authentication_classes = [FederatedJWTAuthentication]
    permission_classes = []

    def get(self, request, agent_id):
        denied = _require_staff_or_daemon_credential(request)
        if denied:
            return denied

        from .runtime_fleet import _find_runtime_agent_row
        agent = _find_runtime_agent_row(str(agent_id))
        if agent is None:
            return Response({'error': 'Agent not found'}, status=status.HTTP_404_NOT_FOUND)

        mcp_config = dict(agent.mcp_config or {})
        return Response({
            'model': agent.model or '',
            'execution_method': mcp_config.get('execution_method') or 'shizuha',
            'model_fallbacks': list(agent.model_fallbacks or []),
            'model_overrides': dict(agent.model_overrides or {}),
            'reasoning_effort': agent.reasoning_effort or '',
        })


class DaemonIdentityRosterView(APIView):
    """GET /v1/fleet/daemon/identity-roster/ — narrow daemon trust roster.

    The runtime controller uses this read only to cross-check the immutable
    ``agent_id`` and username from its local config before granting S10 seed
    permissions.  Keep this endpoint deliberately smaller than the UI agents
    index: it must not invoke runtime/telemetry/Pulse fan-out, and it must not
    expose agent configuration or credentials.
    """
    authentication_classes = [FederatedJWTAuthentication]
    permission_classes = []

    def get(self, request):
        denied = _require_staff_or_daemon_credential(request)
        if denied:
            return denied

        agents = []
        seen_ids = set()
        rows = (
            FleetAgent.objects
            .exclude(status=FleetAgent.Status.DELETED)
            .exclude(agent_id='')
            .order_by('agent_username')
            .values('agent_id', 'agent_username', 'identity_user_id', 'status')
        )
        for row in rows:
            agent_id = row['agent_id']
            if agent_id in seen_ids:
                logger.error('duplicate agent_id %s in daemon identity roster', agent_id)
                return Response(
                    {'error': 'Fleet identity roster is inconsistent'},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            seen_ids.add(agent_id)
            agents.append({
                'id': agent_id,
                'username': row['agent_username'],
                'status': row['status'],
                'user_id': row['identity_user_id'],
            })
        return Response({'count': len(agents), 'agents': agents})


class AgentCapabilityOverridesView(APIView):
    """GET/PUT /v1/fleet/agents/<id>/capability-overrides/."""

    def get(self, request, agent_id):
        denied = _require_staff(request)
        if denied:
            return denied
        agent, error = _effective_agent_identity(agent_id)
        if error:
            return error
        identifiers = {agent['id'], agent['username'], agent['email']}
        rows = AgentCapabilityOverride.objects.select_related('capability').filter(agent_identifier__in=identifiers)
        return Response({'agent': agent, 'overrides': AgentCapabilityOverrideSerializer(rows, many=True).data})

    def put(self, request, agent_id):
        denied = _require_staff(request)
        if denied:
            return denied
        agent, error = _effective_agent_identity(agent_id)
        if error:
            return error
        identifier = agent['email'] or agent['username'] or agent['id']
        specs = request.data.get('overrides', [])
        if not isinstance(specs, list):
            return Response({'error': 'overrides must be a list'}, status=status.HTTP_400_BAD_REQUEST)
        before = AgentCapabilityOverrideSerializer(
            AgentCapabilityOverride.objects.select_related('capability').filter(agent_identifier=identifier),
            many=True,
        ).data
        wanted = {}
        for item in specs:
            slug = _canon_capability_slug(item.get('capability_slug') or item.get('slug'))
            cap = CapabilityDefinition.objects.filter(slug=slug).first()
            if not cap:
                return Response({'error': f'Unknown capability {slug}'}, status=status.HTTP_400_BAD_REQUEST)
            action = item.get('action') or AgentCapabilityOverride.Action.ADD
            if action not in {AgentCapabilityOverride.Action.ADD, AgentCapabilityOverride.Action.REMOVE}:
                return Response({'error': 'override action must be add or remove'}, status=status.HTTP_400_BAD_REQUEST)
            if not item.get('reason'):
                return Response({'error': f'Override reason required for {slug}'}, status=status.HTTP_400_BAD_REQUEST)
            expires_at = item.get('expires_at')
            if isinstance(expires_at, str) and expires_at:
                expires_at = parse_datetime(expires_at)
                if expires_at is None:
                    return Response({'error': f'Invalid expires_at for {slug}'}, status=status.HTTP_400_BAD_REQUEST)
            approved_by = item.get('approved_by', '')
            approved_at = item.get('approved_at')
            if isinstance(approved_at, str) and approved_at:
                approved_at = parse_datetime(approved_at)
                if approved_at is None:
                    return Response({'error': f'Invalid approved_at for {slug}'}, status=status.HTTP_400_BAD_REQUEST)
            if cap.is_privileged and action == AgentCapabilityOverride.Action.ADD:
                approved_by = approved_by or _actor(request)
                approved_at = approved_at or timezone.now()
            wanted[slug] = {
                'capability': cap,
                'action': action,
                'reason': item.get('reason'),
                'expires_at': expires_at,
                'approved_by': approved_by,
                'approved_at': approved_at,
                'updated_by': _actor(request),
            }
        with transaction.atomic():
            AgentCapabilityOverride.objects.filter(agent_identifier=identifier).exclude(capability__slug__in=wanted.keys()).delete()
            for slug, defaults in wanted.items():
                row, _ = AgentCapabilityOverride.objects.update_or_create(
                    agent_identifier=identifier,
                    capability=defaults.pop('capability'),
                    defaults=defaults,
                )
                if not row.created_by:
                    row.created_by = _actor(request)
                    row.save(update_fields=['created_by'])
        rows = AgentCapabilityOverride.objects.select_related('capability').filter(agent_identifier=identifier)
        after = AgentCapabilityOverrideSerializer(rows, many=True).data
        _audit_capability(
            request,
            action='agent_overrides.set',
            target_type='agent',
            target_key=identifier,
            before={'overrides': before},
            after={'overrides': after},
            reason=request.data.get('reason', ''),
            outcome='pending-runtime-reconcile',
        )
        return Response({'agent': agent, 'overrides': after})


class CapabilityAuditView(APIView):
    """GET /v1/fleet/capabilities/audit/ — recent capability audit rows."""

    def get(self, request):
        denied = _require_staff(request)
        if denied:
            return denied
        target = request.query_params.get('target_key')
        rows = CapabilityAuditEvent.objects.all()
        if target:
            rows = rows.filter(target_key=target)
        return Response({'audit': CapabilityAuditEventSerializer(rows[:100], many=True).data})


# CON-176: last-known-good Pulse snapshot, served when Pulse/Admin hiccups so
# workload/team counters never silently collapse to zero on the agents page.
_PULSE_SNAPSHOT_STALE = {}
_PULSE_SNAPSHOT_STALE_LOCK = threading.Lock()
_PULSE_SNAPSHOT_STALE_MAX_AGE_S = 6 * 3600


def _pulse_snapshot_cache_key(org_slug=''):
    return f"org:{_team_role_org_scope(org_slug)}" if org_slug else 'global'


def _copy_snapshot_payload(payload):
    try:
        return json.loads(json.dumps(payload or {}))
    except Exception:
        return dict(payload or {})


def _snapshot_is_complete(payload):
    """PLAT-4169 (operator, 2026-07-11): a snapshot with nodes but an empty
    ``workload`` AND ``team_workload`` is a FAILED sub-aggregation (transient
    Pulse sub-fetch/auth failure), not a truthful "no work anywhere" — one build
    was cached fresh+non-degraded with ``workload={}`` while Pulse truth had 54
    assignees, so every viewer briefly saw "no work" as clean data. Treat such a
    build as incomplete so it is never cached as fresh or remembered as last-good;
    silently-wrong is worse than flagged-stale."""
    if not isinstance(payload, dict):
        return False
    if not payload.get('nodes'):
        return True  # no nodes -> nothing to cross-check against; not this mode
    return bool(payload.get('workload')) or bool(payload.get('team_workload'))


def _remember_pulse_snapshot(payload, *, org_slug=''):
    if not _snapshot_is_complete(payload):
        # Never let an empty-workload build become the last-good we serve later.
        return
    with _PULSE_SNAPSHOT_STALE_LOCK:
        _PULSE_SNAPSHOT_STALE[_pulse_snapshot_cache_key(org_slug)] = {
            'payload': _copy_snapshot_payload(payload),
            'built_at': time.time(),
        }


def _stale_pulse_snapshot_response(*, org_slug='', error=None, source='pulse'):
    key = _pulse_snapshot_cache_key(org_slug)
    with _PULSE_SNAPSHOT_STALE_LOCK:
        cached = _PULSE_SNAPSHOT_STALE.get(key)
    if not cached:
        return None
    age = time.time() - cached.get('built_at', 0.0)
    if age > _PULSE_SNAPSHOT_STALE_MAX_AGE_S:
        return None
    detail = getattr(error, 'data', error)
    logger.warning(
        'Pulse snapshot %s fetch failed for %s — serving stale snapshot from %.0fs ago: %s',
        source, key, age, detail,
    )
    payload = _copy_snapshot_payload(cached.get('payload') or {})
    payload['pulse_degraded'] = True
    payload['pulse_stale_age_seconds'] = int(age)
    payload['pulse_degraded_source'] = source
    return Response(payload)


def _restore_assignee_workload_totals(view, request, payload) -> None:
    """An agent's own workload must not shrink because a different org is selected.

    Operator 2026-08-05, after two rounds of "the card still shows 0 open":

        still don't see the active tasks assigned to banto nor on the team header
        ...
        note that the card and the teams headers should never under-report

    Pulse's org-scoped projection counts only items in THAT org's projects.
    banto is placed in Shizuha Global > Accounting, but BKSD-10 (todo) and
    BKSD-8/9 (blocked) are Digital LLP filings, so selecting Shizuha Global
    zeroed them:

        global  -> banto@shizuha.com  open 1, blocked 2
        shizuha -> banto@shizuha.com  open 0, blocked 0

    The org lens is meant to choose WHICH agents and teams are displayed — not to
    misreport the workload of an agent it displays. A card that says "0 open" for
    an agent holding a high-priority todo is worse than one that shows work from
    elsewhere: it reads as "nothing to do" and hides real assignments.

    Per-bucket max against the global per-assignee totals, so this can only ever
    raise a number toward the truth. Team-level counts stay org-scoped: a team
    genuinely belongs to one org. Fails soft — if the global snapshot is
    unavailable the org payload is returned unchanged, which is no worse than
    before.
    """
    scoped = payload.get('workload')
    if not isinstance(scoped, dict):
        return
    try:
        global_workload = view._global_assignee_workload(request)
    except Exception:
        logger.exception('pulse-snapshot: global workload merge failed; serving org-scoped totals')
        return
    if not isinstance(global_workload, dict):
        return
    for email, totals in global_workload.items():
        if not isinstance(totals, dict):
            continue
        current = scoped.get(email)
        if not isinstance(current, dict):
            scoped[email] = dict(totals)
            continue
        for bucket, value in totals.items():
            try:
                if int(value or 0) > int(current.get(bucket, 0) or 0):
                    current[bucket] = value
            except (TypeError, ValueError):
                continue


class FleetPulseSnapshotView(APIView):
    """GET /v1/fleet/pulse-snapshot/ — same-origin proxy for Pulse org-chart data."""

    _DONE_STATUSES = {
        'completed', 'cancelled', 'canceled', 'closed', 'done', 'resolved',
        'rejected', 'duplicate', 'wont_fix', 'merged', 'failed', 'expired', 'deferred',
    }

    def get(self, request):
        grant, denied = _require_fleet_read(request)
        if denied:
            return denied
        if grant is not None:
            request._hive_fleet_read_grant = grant
        org_slug = _request_org_slug(request)

        # Instant-page contract (operator 2026-07-10): the agents page must never
        # block on the Pulse round-trips. Serve the shared (Redis) read-model
        # instantly and revalidate off-path; only a true cold start builds inline.
        from . import read_model_cache

        def build():
            resp = self._build_live_snapshot(request, org_slug)
            if getattr(resp, 'status_code', 500) >= 400:
                raise RuntimeError(f'pulse snapshot build failed: {resp.status_code}')
            if not _snapshot_is_complete(resp.data):
                # PLAT-4169 (operator): nodes present but empty workload/team_workload
                # is a failed sub-aggregation. Raise so get_or_revalidate treats it as
                # a failed build — the prior good last-good keeps serving (flagged
                # degraded via revalidate_failing), and this payload is never cached
                # as fresh.
                raise RuntimeError('pulse snapshot workload sub-aggregation empty; treating as failed build')
            return resp.data

        try:
            payload, state, age = read_model_cache.get_or_revalidate(
                f'pulse-snapshot:{_pulse_snapshot_cache_key(org_slug)}', build, fresh_ttl=10)
        except Exception:
            # Cold start + failing/incomplete upstream (build() raised, no usable
            # last-good): re-run the live attempt for a best-effort body, but
            # PLAT-4169 — if that body is ALSO incomplete (nodes + empty
            # workload/team_workload), it must NOT be presented as a clean current
            # success (the "failed/flagged, never silently fresh" invariant). Flag
            # it degraded so the UI banners it rather than showing "no work" as
            # truth. An already-degraded stale/error response passes through
            # unchanged (its data is not a complete-looking snapshot).
            resp = self._build_live_snapshot(request, org_slug)
            data = getattr(resp, 'data', None)
            if isinstance(data, dict) and not data.get('pulse_degraded') and not _snapshot_is_complete(data):
                data['pulse_degraded'] = True
                data['pulse_degraded_source'] = 'incomplete-build'
            return resp
        payload = _copy_snapshot_payload(payload)
        # PLAT-4169: "degraded" must mean the background revalidate is actually
        # FAILING, not merely that nobody visited for >5min. A demand-driven
        # cache legitimately serves an old last-good on the first hit after idle
        # while a healthy revalidate runs off-path; flagging that as degraded
        # (amber "roster degraded" banner) reads like an outage to the operator
        # when the fetcher is fine. Only alarm when the last-good is stale AND
        # the fetcher can't refresh it.
        cache_key = f'pulse-snapshot:{_pulse_snapshot_cache_key(org_slug)}'
        if age > read_model_cache.DEGRADED_AGE_S and read_model_cache.revalidate_failing(cache_key):
            payload['pulse_degraded'] = True
            payload['pulse_stale_age_seconds'] = int(age)
            payload['pulse_degraded_source'] = 'read-model-cache'
        resp = Response(payload)
        resp['X-Hive-Cache'] = state
        return resp

    def _build_live_snapshot(self, request, org_slug):
        if org_slug:
            return self._org_scoped_snapshot(request, org_slug)
        # HIVE-609: the global Hive view is labeled "All Organizations" — ask Pulse
        # to fan workload out across every org instead of its env-default org.
        payload, error = _pulse_request(
            request, 'GET', '/admin/org-chart/snapshot/',
            params={'organization_slug': 'all'},
        )
        if error:
            stale = _stale_pulse_snapshot_response(error=error, source='global')
            return stale or error
        payload = payload or {}
        _remember_pulse_snapshot(payload)
        return Response(payload)

    def _org_scoped_snapshot(self, request, org_slug):
        # Pulse owns both placement and workload. Returning its org-scoped
        # projection directly prevents Admin directory membership from becoming
        # a second answer merely because the UI selected one organization.
        payload, error = _pulse_request(
            request, 'GET', '/admin/org-chart/snapshot/',
            params={'organization_slug': org_slug},
        )
        if error:
            stale = _stale_pulse_snapshot_response(org_slug=org_slug, error=error, source='pulse-snapshot')
            return stale or error
        payload = payload or {}
        payload['organization_slug'] = org_slug
        _restore_assignee_workload_totals(self, request, payload)
        _remember_pulse_snapshot(payload, org_slug=org_slug)
        return Response(payload)

    def _global_assignee_workload(self, request):
        payload, error = _pulse_request(
            request, 'GET', '/admin/org-chart/snapshot/',
            params={'organization_slug': 'all'},
        )
        if error:
            return None
        return (payload or {}).get('workload') or None


    def _global_availability(self, request):
        payload, error = _pulse_request(request, 'GET', '/admin/org-chart/snapshot/')
        if error:
            return {}
        return (payload or {}).get('availability') or {}

    def _pulse_projects_for_org(self, request, org_slug):
        projects = []
        page = 1
        for _ in range(20):
            payload, error = _pulse_request(
                request,
                'GET',
                '/projects/',
                params={'organization_slug': org_slug, 'page_size': 250, 'page': page},
            )
            if error:
                return [], error
            batch = _items(payload)
            projects.extend(batch)
            if not isinstance(payload, dict) or not payload.get('next') or not batch:
                break
            page += 1
        return projects, None

    def _pulse_items_for_projects(self, request, projects):
        items = []
        for project in projects:
            project_id = project.get('id')
            if project_id is None:
                continue
            page = 1
            for _ in range(50):
                payload, error = _pulse_request(
                    request,
                    'GET',
                    '/items/',
                    params={
                        'mode': 'task',
                        'project': project_id,
                        'include_archived': 'true',
                        'page_size': 500,
                        'page': page,
                    },
                )
                if error:
                    return [], error
                batch = _items(payload)
                items.extend(batch)
                if not isinstance(payload, dict) or not payload.get('next') or not batch:
                    break
                page += 1
        return items, None

    @classmethod
    def _empty_workload(cls):
        return {'open': 0, 'in_progress': 0, 'blocked': 0, 'in_review': 0, 'scheduled': 0, 'backlog': 0, 'completed': 0, 'overdue': 0}

    @staticmethod
    def _item_datetime(value):
        # Normalize to the SAME awareness as timezone.now() (naive when USE_TZ is
        # off) — a mixed compare raised TypeError inside _aggregate_items, which
        # zeroed the workload for the ENTIRE fleet view (operator-visible outage).
        dt = parse_datetime(str(value or ''))
        if not dt:
            return None
        now_is_aware = timezone.is_aware(timezone.now())
        if now_is_aware and timezone.is_naive(dt):
            return timezone.make_aware(dt, timezone.get_current_timezone())
        if not now_is_aware and timezone.is_aware(dt):
            return timezone.make_naive(dt, timezone.get_current_timezone())
        return dt

    def _bucket(self, item):
        status_value = str(item.get('status') or '').strip().lower()
        status_category = str(item.get('status_category') or '').strip().lower()
        if status_value in self._DONE_STATUSES or status_category == 'done':
            return 'completed'
        if status_value == 'backlog' or status_category == 'backlog':
            return 'backlog'
        if status_value in {'blocked', 'waiting', 'on_hold', 'stalled'} or status_category == 'blocked':
            return 'blocked'
        if status_value in {
            'in_review', 'review', 'awaiting_merge', 'awaiting_review',
            'awaiting_deploy', 'verification',
        } or status_category in {'in_review', 'review'}:
            return 'in_review'
        if status_value in {'in_progress', 'started', 'active', 'doing'} or status_category == 'in_progress':
            return 'in_progress'
        due = self._item_datetime(item.get('due_date'))
        if due and due > timezone.now():
            return 'scheduled'
        return 'open'

    def _aggregate_items(self, items):
        workload = {}
        team_workload = {}
        workload_by_assignee_team = {}
        for item in items:
            try:
                self._aggregate_one(item, workload, team_workload, workload_by_assignee_team)
            except Exception:  # one malformed item must never zero the whole fleet
                logger.exception('workload aggregation skipped a malformed item (id=%s)', item.get('id'))
        return workload, team_workload, workload_by_assignee_team

    def _aggregate_one(self, item, workload, team_workload, workload_by_assignee_team):
        if True:
            bucket = self._bucket(item)
            email = str(item.get('assignee_email') or '').strip().lower()
            group = _canon_team_name(item.get('assignment_group') or '')
            if email:
                workload.setdefault(email, self._empty_workload())[bucket] += 1
            if group:
                team_workload.setdefault(group, self._empty_workload())[bucket] += 1
            if email and group:
                workload_by_assignee_team.setdefault(f'{email}::{group}', self._empty_workload())[bucket] += 1
            if bucket != 'completed':
                due = self._item_datetime(item.get('due_date'))
                if due and due < timezone.now():
                    if email:
                        workload.setdefault(email, self._empty_workload())['overdue'] += 1
                    if group:
                        team_workload.setdefault(group, self._empty_workload())['overdue'] += 1
                    if email and group:
                        workload_by_assignee_team.setdefault(f'{email}::{group}', self._empty_workload())['overdue'] += 1


class FleetRuntimeOptionsView(APIView):
    """GET /v1/fleet/runtime-options/ — catalogs for runtime config editors."""

    def _cortex_models(self):
        base = (
            os.environ.get('CORTEX_PUBLIC_BASE_URL')
            or getattr(settings, 'CORTEX_BASE_URL', '')
            or getattr(settings, 'VLLM_BASE_URL', '')
            or 'https://cortex.shizuha.com/v1'
        ).rstrip('/')
        url = f'{base}/models' if base.endswith('/v1') else f'{base}/v1/models'
        headers = {}
        api_key = getattr(settings, 'CORTEX_API_KEY', '')
        if api_key:
            headers['Authorization'] = f'Bearer {api_key}'
        models = []
        seen = set()
        error = ''
        try:
            resp = requests.get(url, headers=headers, timeout=2)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            logger.warning('failed to load Cortex model catalog from %s: %s', url, exc)
            payload = {'data': []}
            error = str(exc)

        from .model_serviceability import explicitly_nontext_model
        for item in payload.get('data') or []:
            if isinstance(item, dict) and explicitly_nontext_model(item):
                continue
            raw_id = item.get('id') if isinstance(item, dict) else None
            if not raw_id:
                continue
            model_id = str(raw_id)
            value = model_id if model_id.startswith('cortex/') else f'cortex/{model_id}'
            if value in seen:
                continue
            seen.add(value)
            # HIVE-1863 (S2): surface the viewer-aware catalog fields the HLD
            # contract names (data[].available / status / x_cortex.{providers,
            # auth_classes, quality_tier}) so the agent-editor dropdown can show
            # which provider/auth_class serves each model and whether it is
            # currently available — not just a bare id list.
            x_cortex = (item.get('x_cortex') or {}) if isinstance(item, dict) else {}
            models.append({
                'id': value,
                'label': f'Cortex: {model_id.removeprefix("cortex/")}',
                'provider_id': model_id,
                'available': bool(item.get('available', True)) if isinstance(item, dict) else True,
                'status': (item.get('status') or 'available') if isinstance(item, dict) else 'available',
                'providers': x_cortex.get('providers') or ['cortex-self'],
                'auth_classes': x_cortex.get('auth_classes') or ['api_key'],
                'quality_tier': x_cortex.get('quality_tier') or '',
            })
        # Always expose managed fleet-only Cortex routes (Grok) even when the
        # catalog key cannot see internal offers.
        for managed in MANAGED_FLEET_CORTEX_MODELS:
            if managed['id'] in seen:
                continue
            seen.add(managed['id'])
            entry = dict(managed)
            entry.setdefault('available', True)
            entry.setdefault('status', 'available')
            entry.setdefault('providers', ['xai-grok'])
            entry.setdefault('auth_classes', ['oauth_subscription'])
            entry.setdefault('quality_tier', 'managed')
            models.append(entry)
        return sorted(models, key=lambda item: item['provider_id'].lower()), error

    def get(self, request):
        denied = _require_staff(request)
        if denied:
            return denied
        runtime_agents = list_runtime_agents(include_disabled=True)
        mcp_servers = {}
        tiers = set()
        # Hive fleet placement is k3s-only; never advertise host bare_metal.
        runtime_environments = {'k8s'}
        for agent in runtime_agents:
            if agent.get('tier'):
                tiers.add(agent.get('tier'))
            if agent.get('runtime_environment') == 'k8s':
                runtime_environments.add('k8s')
            for srv in agent.get('mcp_servers') or []:
                if not isinstance(srv, dict):
                    continue
                slug = srv.get('slug') or srv.get('name')
                if slug:
                    mcp_servers[str(slug)] = {
                        'slug': str(slug),
                        'name': srv.get('name') or str(slug),
                    }
        profiles = getattr(settings, 'FLEET_RESOURCE_PROFILES', {}) or {}
        cortex_models, cortex_error = self._cortex_models()
        from .runtime_lane import (
            RUNTIME_CAPABILITY_MANIFEST, runtime_registry_method_models,
        )
        method_models = runtime_registry_method_models(cortex_models)
        return Response({
            'runtime_methods': RUNTIME_METHOD_OPTIONS,
            'skills': get_runtime_skills(),
            'mcp_servers': sorted(mcp_servers.values(), key=lambda item: item['slug']),
            'cortex_models': cortex_models,
            'cortex_models_error': cortex_error,
            'grok_build_models': GROK_BUILD_MODEL_OPTIONS,
            'method_models': method_models,
            'runtime_lane_registry': {
                'version': RUNTIME_CAPABILITY_MANIFEST['registry_version'],
                'runtime_release_id': RUNTIME_CAPABILITY_MANIFEST['runtime_release_id'],
                'snapshot_source': RUNTIME_CAPABILITY_MANIFEST['snapshot_source'],
            },
            'resource_profiles': [
                {'id': key, 'name': _display_team_name(key), 'spec': spec}
                for key, spec in sorted(profiles.items())
            ],
            'runtime_environments': sorted(runtime_environments),
            'tiers': sorted(tiers or {'normal'}),
        })


class FleetRuntimeChangeView(APIView):
    """Read the authoritative desired-vs-rollout state for one RuntimeChange."""

    def get(self, request, agent_id, change_id):
        denied = _require_staff(request)
        if denied:
            return denied
        from .models import RuntimeChange
        from .runtime_fleet import _find_runtime_agent_row, runtime_change_payload

        agent = _find_runtime_agent_row(str(agent_id))
        if agent is None:
            return Response({'error': 'Runtime change not found'}, status=status.HTTP_404_NOT_FOUND)
        change = (
            RuntimeChange.objects.select_related('agent', 'rollback_of')
            .filter(id=change_id, agent=agent)
            .first()
        )
        if change is None:
            return Response({'error': 'Runtime change not found'}, status=status.HTTP_404_NOT_FOUND)
        return Response(runtime_change_payload(change))


class FleetAgentDisableView(APIView):
    """POST /v1/fleet/agents/{id}/disable/ — hibernate (scale 1 → 0).

    Operator 2026-09-01: Hive may hibernate, never hard-stop. Hard stop is
    POST /stop/ and requires the operator (is_superuser).
    """

    def post(self, request, agent_id):
        denied = _require_staff(request)
        if denied:
            return denied
        result = _get_agent(agent_id)
        if isinstance(result, Response):
            # Not a native k3s row -> fall through to the live rt-fleet plane (HIVE-171).
            return _rt_fleet_control(request, agent_id, 'disable')
        agent = result
        if agent.status == FleetAgent.Status.HIBERNATED:
            return Response({'status': agent.status, 'message': 'Agent is already hibernated'})
        # Operator 2026-09-04: Hive may recover a STOPPED GLM/org seat into
        # hibernated so it stays auto-wakeable. Entering hard-stop remains
        # operator-only POST /stop/. Leaving hard-stop via disable is the
        # hibernate-only recovery the Agents page requires.
        ok, detail = hibernate_lifecycle_agent(
            str(agent.id),
            reason='Hive disable (hibernate-only; operator stop is separate)',
        )
        if not ok:
            return Response(
                {'error': f'Disable/hibernate failed: {detail}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        agent_index_cache.invalidate('native disable hibernate')
        agent.refresh_from_db()
        return Response(FleetAgentSerializer(agent).data)


class FleetAgentStopView(APIView):
    """POST /v1/fleet/agents/{id}/stop/ — operator-only hard stop."""

    def post(self, request, agent_id):
        denied = _require_staff(request)
        if denied:
            return denied
        if not getattr(request.user, 'is_superuser', False):
            return Response(
                {
                    'error': (
                        'Only the operator may hard-stop an agent. '
                        'Hive disable hibernates so the seat stays auto-wakeable.'
                    ),
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        ok, detail = set_lifecycle_agent_enabled(str(agent_id), False)
        if not ok:
            return Response(
                {'error': str(detail)},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        agent_index_cache.invalidate('operator hard stop')
        result = _get_agent(agent_id)
        if not isinstance(result, Response):
            return Response(FleetAgentSerializer(result).data)
        return Response({'status': 'stopped', 'detail': detail})


class FleetAgentRestartView(APIView):
    """POST /v1/fleet/agents/{id}/restart/ -- restart a live rt-fleet (docker) agent (HIVE-171).

    Restart is an rt-fleet capability (graceful SIGTERM+drain, daemon-side; agents
    recover session from disk). Native k3s agents use disable+enable / switch-backend,
    so a native id returns 400.
    """

    def post(self, request, agent_id):
        denied = _require_staff(request)
        if denied:
            return denied
        result = _get_agent(agent_id)
        if isinstance(result, Response):
            return _rt_fleet_control(request, agent_id, 'restart')
        return Response(
            {'error': 'restart is only supported for rt-fleet (docker) agents; use disable+enable or switch-backend for k3s agents'},
            status=status.HTTP_400_BAD_REQUEST,
        )


class FleetAgentResetSessionView(APIView):
    """POST /v1/fleet/agents/{id}/reset-session/ -- clear runtime session state.

    Hive-owned lifecycle-control surface. Hybrid FleetAgent + rt-fleet k8s
    seats (Saki-class) have a native row AND a daemon session: prefer the
    runtime plane. The daemon resets the PVC sqlite (including wire-prefix)
    rather than the host workspace. Pure native rows with no runtime agent
    still 400 until a non-rt-fleet PVC path exists.
    """

    def post(self, request, agent_id):
        denied = _require_staff(request)
        if denied:
            return denied
        # Daemon-link cache lists k3s-native seats as runtime_source=rt-fleet.
        # Prefer the daemon when it actually owns the session; a 404/Agent-not-
        # found must fall through to the native PVC wipe (Nova 2026-09-09:
        # Hive MCP reset 502'd while the 241k GLM transcript occupied Metal).
        if get_runtime_agent(str(agent_id)):
            resp = _rt_fleet_control(request, agent_id, 'reset-session')
            err = ''
            if hasattr(resp, 'data') and isinstance(resp.data, dict):
                err = str(resp.data.get('error') or '')
            if resp.status_code < 400:
                return resp
            if 'Agent not found' not in err and '404' not in err:
                return resp
        result = _get_agent(agent_id)
        if isinstance(result, Response):
            return result
        agent = result
        try:
            from .provisioner import reset_k8s_native_runtime_session
            payload = reset_k8s_native_runtime_session(agent)
        except Exception as exc:
            return Response(
                {'error': f'k3s-native session reset failed: {exc}'},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(payload, status=status.HTTP_200_OK)


class FleetAgentSwitchBackendView(APIView):
    """POST /v1/fleet/agents/{id}/switch-backend/ — change container image."""

    def post(self, request, agent_id):
        denied = _require_staff(request)
        if denied:
            return denied
        result = _get_agent(agent_id)
        if isinstance(result, Response):
            return result
        agent = result
        serializer = SwitchBackendSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        old_image = agent.image
        agent.image = serializer.validated_data['image']
        agent.image_pull_policy = serializer.validated_data.get('image_pull_policy', 'IfNotPresent')
        agent.save(update_fields=['image', 'image_pull_policy', 'updated_at'])
        _log(
            agent, 'backend_switched',
            f'image: {old_image!r} → {agent.image!r} (pull: {agent.image_pull_policy})',
        )

        # Apply the new image to the live Deployment (rolling restart).
        if agent.status == FleetAgent.Status.RUNNING:
            try:
                _, apps_v1, _ = provisioner.get_k8s_client()
                ns = provisioner._live_ns(agent)
                deploy = apps_v1.read_namespaced_deployment(agent.deployment_name, ns)
                containers = deploy.spec.template.spec.containers
                for c in containers:
                    if c.name in {'agent', agent.agent_username}:
                        c.image = agent.image
                        c.image_pull_policy = agent.image_pull_policy
                apps_v1.patch_namespaced_deployment(
                    agent.deployment_name, ns, deploy,
                )
                _log(agent, 'backend_applied', f'Deployment patched with new image {agent.image!r}')
            except Exception as e:
                logger.warning('switch-backend patch failed for %s: %s', agent.agent_username, e)
                _log(agent, 'backend_patch_failed', str(e))

        agent_index_cache.invalidate('switch-backend')
        agent.refresh_from_db()
        return Response(FleetAgentSerializer(agent).data)


class FleetAgentEventsView(APIView):
    """GET /v1/fleet/agents/{id}/events/"""

    def get(self, request, agent_id):
        if not getattr(request.user, 'is_authenticated', False):
            return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
        result = _get_agent(agent_id)
        if isinstance(result, Response):
            # Daemon/BYO agents have runtime activity but no native lifecycle
            # event table. Return the honest empty event stream rather than a
            # list/detail contract-breaking 404.
            if _owned_external_agent(request, agent_id) is not None:
                return Response({'events': []})
            personal = _owned_personal_agent(request, agent_id)
            if personal is not None:
                raw = str(agent_id)
                if raw.startswith('claw:'):
                    from claws.models import ClawEvent
                    from claws.serializers import ClawEventSerializer
                    events = ClawEvent.objects.filter(claw_id=raw.split(':', 1)[1]).order_by('-created_at')[:100]
                    return Response({'events': ClawEventSerializer(events, many=True).data})
                return Response({'events': []})
            return result
        if not getattr(request.user, 'is_staff', False):
            return Response({'error': 'Staff access required'}, status=status.HTTP_403_FORBIDDEN)
        events = FleetAgentEvent.objects.filter(agent=result).order_by('-created_at')[:100]
        return Response({'events': FleetAgentEventSerializer(events, many=True).data})


def _pick_live_pod(pods):
    """Compatibility wrapper around the shared placement/activity selector."""
    from .runtime_fleet import pick_live_k8s_pod
    return pick_live_k8s_pod(pods)


def _normalise_native_audit_event(event):
    """Map bridge audit rows onto the established activity API contract.

    Native bridge rows are ``before``/``after`` audit pairs with ``timestamp``
    and summary fields. The drawer consumes ``ts`` plus ``tool_call`` /
    ``tool_result`` and must never receive the raw audit shape.
    """
    if not isinstance(event, dict):
        return None
    phase = str(event.get('phase') or '').lower()
    timestamp = event.get('timestamp')
    tool = event.get('tool') or ''
    if phase == 'before':
        return {
            'id': event.get('id'),
            'ts': timestamp,
            'type': 'tool_call',
            'tool': tool,
            'input': event.get('inputSummary') or '',
            'agent': event.get('agent') or '',
        }
    if phase == 'after':
        return {
            'id': event.get('id'),
            'ts': timestamp,
            'type': 'tool_result',
            'tool': tool,
            'output': event.get('resultSummary') or '',
            'agent': event.get('agent') or '',
        }
    return None


def _normalise_native_telemetry_turn(event):
    """Map gateway llm-turn spans onto the drawer contract (behaviour first)."""
    if not isinstance(event, dict) or event.get('name') != 'llm-turn':
        return None
    ts = event.get('endTime') or event.get('startTime')
    if not ts:
        return None
    result = event.get('result') or {}
    metadata = event.get('metadata') or {}
    duration_ms = event.get('durationMs')
    model = str(metadata.get('model') or 'model')
    tool_calls = result.get('toolCalls') or 0
    input_tokens = result.get('inputTokens') or 0
    output_tokens = result.get('outputTokens') or 0
    duration = f'{round(duration_ms / 1000, 1)}s' if isinstance(duration_ms, (int, float)) else 'unknown duration'
    status = 'failed' if event.get('status') == 'error' else 'completed'
    tool_names = result.get('toolNames') if isinstance(result.get('toolNames'), list) else []
    excerpt = str(result.get('assistantExcerpt') or '').strip()
    behaviour_bits = []
    if excerpt:
        clipped = excerpt[:300] + ('…' if len(excerpt) > 300 else '')
        behaviour_bits.append(f'“{clipped}”')
    if tool_names:
        behaviour_bits.append('tools: ' + ', '.join(str(n) for n in tool_names))
    elif not tool_calls:
        behaviour_bits.append('no tools')
    behaviour = ' · '.join(behaviour_bits)
    text = (
        f'LLM turn {status}: {model}, {duration}, {input_tokens} input tokens, '
        f'{output_tokens} output tokens, {tool_calls} tool calls'
    )
    if behaviour:
        text = f'{text} — {behaviour}'
    return {
        'ts': ts,
        'type': 'turn_summary',
        'text': text,
        'status': status,
        'model': model,
        'duration_ms': duration_ms,
        'input_tokens': input_tokens,
        'output_tokens': output_tokens,
        'tool_calls': tool_calls,
        'tool_names': tool_names,
        'assistant_excerpt': excerpt,
    }


# Bounded so a hung exec cannot occupy a gunicorn worker and blank every
# other Agents-drawer poll (San 2026-09-15: intermittent "source degraded").
_NATIVE_ACTIVITY_K8S_TIMEOUT = (3, 8)
_NATIVE_ACTIVITY_EXEC_ATTEMPTS = 2


def _read_exec_stdout(resp):
    """stdout from a WSClient opened with _preload_content=False.

    A timeout that never sets returncode is a failed read, not an empty
    session. An empty string with returncode 0 means the files were read
    and are empty (reset or a pod that has not turned yet).
    """
    resp.run_forever(timeout=_NATIVE_ACTIVITY_K8S_TIMEOUT[1])
    code = getattr(resp, 'returncode', 0)
    raw = resp.read_all()
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8', errors='replace')
    elif not isinstance(raw, str):
        raw = ''
    if code is None and not raw:
        raise TimeoutError('activity tail did not finish')
    return raw


def _close_exec_stream(resp):
    close = getattr(resp, 'close', None)
    if not callable(close):
        return
    try:
        close()
    except Exception:
        logger.debug('native audit exec close failed', exc_info=True)


def _exec_native_audit_tail(core_v1, pod_name, namespace, limit):
    """Tail audit + telemetry. '' is a real empty session. None is a failed read.

    stream(preload) calls .decode() on a None websocket body for every
    multi-container fleet pod. Returning that failure as '' made the drawer
    say the session had just started and hid the last good transcript.
    """
    from kubernetes.stream import stream
    n = max(1, min(int(limit), 500))
    command = [
        'sh', '-c',
        f'tail -n {n} /home/agent/.shizuha/.audit-log.jsonl 2>/dev/null; '
        f'echo __TELEMETRY__; '
        f'tail -n {n} /home/agent/.shizuha/.telemetry.jsonl 2>/dev/null || true',
    ]
    last_exc = None
    for attempt in range(1, _NATIVE_ACTIVITY_EXEC_ATTEMPTS + 1):
        resp = None
        try:
            resp = stream(
                core_v1.connect_get_namespaced_pod_exec,
                pod_name,
                namespace,
                container='agent',
                command=command,
                stderr=False,
                stdin=False,
                stdout=True,
                tty=False,
                _preload_content=False,
                _request_timeout=_NATIVE_ACTIVITY_K8S_TIMEOUT,
            )
            return _read_exec_stdout(resp)
        except Exception as exc:
            last_exc = exc
            logger.warning(
                'native audit exec(%s/%s) attempt %s/%s failed: %s',
                namespace, pod_name, attempt, _NATIVE_ACTIVITY_EXEC_ATTEMPTS, exc,
            )
        finally:
            if resp is not None:
                _close_exec_stream(resp)
    logger.warning(
        'native audit exec(%s/%s) giving up after %s attempts: %s',
        namespace, pod_name, _NATIVE_ACTIVITY_EXEC_ATTEMPTS, last_exc,
    )
    return None


def parse_native_activity_raw(raw, limit=100):
    """Turn an audit+telemetry tail into drawer events. DB-free."""
    events = []
    section = 'audit'
    for line in str(raw or '').splitlines():
        if line.strip() == '__TELEMETRY__':
            section = 'telemetry'
            continue
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if section == 'telemetry':
            normalised = _normalise_native_telemetry_turn(event)
        else:
            normalised = _normalise_native_audit_event(event)
        if normalised is not None:
            events.append(normalised)
    events.sort(key=lambda event: str(event.get('ts') or ''), reverse=True)
    newest_event_at = None
    for event in events:
        ts = event.get('ts')
        if not ts:
            continue
        try:
            parsed = datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
        except (TypeError, ValueError):
            continue
        if newest_event_at is None or parsed > newest_event_at:
            newest_event_at = parsed
    capped = events[: max(1, min(int(limit), 500))]
    return capped, newest_event_at


def _get_native_agent_activity(agent, limit=100):
    """Read the selected k3s-native agent's current bounded audit tail.

    A FleetAgent UUID is a Hive identity, not an rt-fleet daemon identity. Never
    fall back to the legacy proxy for a native row: lookup/exec/parse failures
    return an honest empty feed rather than stale activity from the dead plane.

    HIVE-1552: satisfies the same versioned source receipt contract as the
    rt-fleet daemon activity endpoint so the drawer can distinguish a live
    transcript from a degraded/unavailable source.
    """
    observed_at = datetime.now(datetime_timezone.utc).isoformat().replace('+00:00', 'Z')
    try:
        core_v1, _, _ = provisioner.get_k8s_client()
        namespace = provisioner._live_ns(agent)
        username = agent.agent_username
        pods = core_v1.list_namespaced_pod(
            namespace,
            label_selector=f'shizuha.io/agent={username}',
            _request_timeout=_NATIVE_ACTIVITY_K8S_TIMEOUT,
        ).items
        if not pods:
            # Shape-B Deployments (Saki) only carry app=agent-<user>.
            pods = core_v1.list_namespaced_pod(
                namespace,
                label_selector=f'app=agent-{username}',
                _request_timeout=_NATIVE_ACTIVITY_K8S_TIMEOUT,
            ).items
        pod = _pick_live_pod(pods)
        if pod is None:
            return {
                'source_state': 'unavailable',
                'source': 'k8s-native-audit',
                'resource_uid': None,
                'observed_at': observed_at,
                'newest_event_at': None,
                'events': [],
                'history_events': [],
                'total': 0,
            }
        raw = _exec_native_audit_tail(
            core_v1,
            pod.metadata.name,
            namespace,
            limit,
        )
        if raw is None:
            return {
                'source_state': 'unavailable',
                'source': 'k8s-native-audit',
                'resource_uid': None,
                'observed_at': observed_at,
                'newest_event_at': None,
                'events': [],
                'history_events': [],
                'total': 0,
                'pod_name': pod.metadata.name,
            }
        events, newest_event_at = parse_native_activity_raw(raw, limit=limit)
        pod_uid = getattr(pod.metadata, 'uid', None)
        if hasattr(pod_uid, 'value'):
            pod_uid = pod_uid.value
        return {
            # Empty tail on a live pod is a fresh/reset session, not a
            # broken reader (Kumo 2026-09-15: session reset → "degraded"
            # with 0 events). Degraded is only when exec/parse fails.
            'source_state': 'live',
            'source': 'k8s-native-audit+telemetry',
            'resource_uid': str(pod_uid) if pod_uid else None,
            'observed_at': observed_at,
            'newest_event_at': newest_event_at.isoformat().replace('+00:00', 'Z')
            if newest_event_at is not None else None,
            'events': events[: max(1, min(int(limit), 500))],
            'history_events': [],
            'total': len(events),
            'pod_name': pod.metadata.name,
            'node_name': getattr(pod.spec, 'node_name', None),
        }
    except AttributeError as exc:
        # list_namespaced_pod / watch can raise the same kubernetes-client
        # ``NoneType.decode`` as exec when the API body is empty (timeout).
        # The exec stream() handler does not see that path; without this
        # catch the outer log looks like a generic activity failure.
        logger.warning(
            'native fleet activity(%s) kubernetes-client decode miss: %s',
            agent.agent_username, exc,
        )
        return {
            'source_state': 'unavailable',
            'source': 'k8s-native-audit',
            'resource_uid': None,
            'observed_at': observed_at,
            'newest_event_at': None,
            'events': [],
            'history_events': [],
            'total': 0,
        }
    except Exception as exc:
        logger.warning('native fleet activity(%s) failed: %s', agent.agent_username, exc)
        return {
            'source_state': 'unavailable',
            'source': 'k8s-native-audit',
            'resource_uid': None,
            'observed_at': observed_at,
            'newest_event_at': None,
            'events': [],
            'history_events': [],
            'total': 0,
        }


class FleetAgentActivityView(APIView):
    """GET /v1/fleet/agents/{id}/activity/ — live runtime activity transcript
    (messages, tool calls, tool results) from the agent's session, so the operator
    can inspect exactly what the agent is doing. Proxied from the rt-fleet daemon
    via the control proxy. Staff-only (fleet is an operator surface)."""

    def get(self, request, agent_id):
        if not getattr(request.user, 'is_authenticated', False):
            return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
        if not getattr(request.user, 'is_staff', False):
            # HIVE-620/624: the owner of a native personal agent sees its REAL live
            # activity (same transcript staff see); external/claw personal agents
            # keep the empty stub; anyone else is denied.
            if _owned_native_agent(request, agent_id) is None:
                if _owned_personal_agent(request, agent_id) is not None:
                    return Response({'events': [], 'total': 0})
                return Response({'error': 'Staff access required'}, status=status.HTTP_403_FORBIDDEN)
        try:
            limit = int(request.query_params.get('limit', 100))
        except (TypeError, ValueError):
            limit = 100
        result = _get_agent(agent_id)
        if not isinstance(result, Response):
            data = _get_native_agent_activity(result, limit=limit)
            data = merge_activity_with_snapshot(result, data, limit=limit)
        else:
            data = get_runtime_agent_activity(str(agent_id), limit=limit)
        if not data:
            return Response({'events': [], 'total': 0, 'source_state': 'unavailable'})
        # HIVE-1552: never relabel a degraded/unavailable transcript as "Live
        # activity". The receipt's source_state is authoritative; a missing
        # receipt from the runtime plane is rendered explicitly as unavailable.
        # ``persisted`` is a separate honest state: Hive is serving last-known
        # history because the live pod could not be read (hibernate/restart).
        source_state = data.get('source_state')
        if source_state is None:
            data = dict(data)
            data['source_state'] = 'unavailable'
        return Response(data)


class FleetAgentStatusView(APIView):
    """GET /v1/fleet/agents/{id}/status/ — live k8s status."""

    def get(self, request, agent_id):
        if not getattr(request.user, 'is_authenticated', False):
            return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
        result = _get_agent(agent_id)
        if isinstance(result, Response):
            external = _owned_personal_agent(request, agent_id)
            if external is not None:
                return Response({
                    'status': external.get('status'),
                    'namespace': external.get('namespace') or external.get('k3s_namespace'),
                    'pod_status': external.get('status'),
                    'pod_ip': None,
                    'pod_name': external.get('pod_name') or external.get('k3s_deployment'),
                    'ready_containers': external.get('k3s_ready_replicas') or (1 if external.get('status') == 'running' else 0),
                    'runtime_source': external.get('runtime_source'),
                })
            return result
        if not getattr(request.user, 'is_staff', False):
            return Response({'error': 'Staff access required'}, status=status.HTTP_403_FORBIDDEN)
        live = provisioner.get_agent_status(result)
        return Response(live)


# ── HIVE-172: per-agent work-efficiency metrics ───────────────────────────────

_EXPORTER_URL = os.environ.get(
    'TEAM_THROUGHPUT_EXPORTER_URL',
    'http://team-throughput-exporter.shizuha.svc.cluster.local:9101',
)

_EFFICIENCY_METRIC_MAP = {
    'agent_tasks_completed_24h': 'tasks_completed_24h',
    'agent_avg_lead_time_seconds': 'avg_lead_time_seconds',
    'agent_wip_depth': 'wip_depth',
    'agent_backlog_depth': 'backlog_depth',
    'agent_blocked_depth': 'blocked_depth',
    'agent_overdue_tasks': 'overdue_tasks',
    'agent_oldest_open_item_age_seconds': 'oldest_open_item_age_seconds',
}

_PROM_LINE_RE = re.compile(r'^(\w+)\{([^}]*)\}\s+([\d.eE+\-]+)')
_PROM_LABEL_RE = re.compile(r'(\w+)="([^"]*)"')


def _parse_efficiency_metrics(text):
    """Parse Prometheus text format, return {agent_email: {short_name: float}}."""
    by_email = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        m = _PROM_LINE_RE.match(line)
        if not m:
            continue
        metric_name, label_str, value_str = m.groups()
        if metric_name not in _EFFICIENCY_METRIC_MAP:
            continue
        labels = dict(_PROM_LABEL_RE.findall(label_str))
        # The exporter currently emits ``agent`` while older releases emitted
        # ``agent_email``. Accept both and keep this API's stable, canonical
        # lower-case email key for frontend joins.
        email = (labels.get('agent_email') or labels.get('agent') or '').strip().lower()
        if not email:
            continue
        try:
            value = float(value_str)
        except ValueError:
            continue
        short = _EFFICIENCY_METRIC_MAP[metric_name]
        by_email.setdefault(email, {})[short] = value
    return by_email


class FleetEfficiencyView(APIView):
    """
    GET /v1/fleet/efficiency/ — per-agent work-efficiency metrics proxied from
    the team-throughput-exporter Prometheus scrape endpoint (:9101).

    Returns:
      { "agents": { "<email>": { "tasks_completed_24h": N, "avg_lead_time_seconds": N,
                                  "wip_depth": N, "backlog_depth": N, "blocked_depth": N,
                                  "overdue_tasks": N, "oldest_open_item_age_seconds": N } },
        "error": "<reason>" | null }

    Requires staff. Gracefully returns an empty agents dict when the exporter
    is unreachable (exporter downtime should not break the Fleet UI).
    """

    def get(self, request):
        denied = _require_staff(request)
        if denied:
            return denied

        import requests as _req
        url = _EXPORTER_URL.rstrip('/') + '/metrics'
        try:
            resp = _req.get(url, timeout=5)
            resp.raise_for_status()
            agents = _parse_efficiency_metrics(resp.text)
        except Exception as exc:
            logger.warning('FleetEfficiencyView: exporter fetch failed (%s): %s', url, exc)
            return Response({'agents': {}, 'error': 'exporter_unavailable'})

        return Response({'agents': agents, 'error': None})


# ---------------------------------------------------------------------------
# HIVE-254 P5: Daemon identity-health surface
# ---------------------------------------------------------------------------

class DaemonIdentityHealthView(APIView):
    """GET /v1/fleet/daemon/identity-health/ — daemon status + identity violations.

    Returns the health summary for all registered fleet daemons:
    - daemon status (connected/degraded/down/disconnected)
    - identity_ok: True iff no unresolved rejected identity events exist
    - violations: list of unresolved rejected DaemonIdentityEvent records

    Staff-only (same gate as the rest of the fleet API).
    """

    def get(self, request):
        denied = _require_staff(request)
        if denied:
            return denied

        daemons = DaemonRegistry.objects.all().order_by('daemon_id')
        result = []
        for daemon in daemons:
            violations_qs = DaemonIdentityEvent.objects.filter(
                daemon=daemon,
                verdict=DaemonIdentityEvent.Verdict.REJECTED,
                resolved_at__isnull=True,
            ).order_by('-seen_at')

            violations = [
                {
                    'child_id': ev.child_id,
                    'username': ev.username,
                    'reason': ev.reason,
                    'seen_at': ev.seen_at.isoformat(),
                }
                for ev in violations_qs
            ]

            result.append({
                'daemon_id': daemon.daemon_id,
                'fleet_id': daemon.fleet_id,
                'runtime': daemon.runtime,
                'status': daemon.status,
                'last_seen': daemon.last_seen.isoformat() if daemon.last_seen else None,
                'identity_ok': len(violations) == 0,
                'violations': violations,
            })

        return Response({'daemons': result})


def _agent_username_for_id(agent_id):
    """Resolve the stable username for either agent kind (native row or rt-fleet)."""
    result = _get_agent(agent_id)
    if not isinstance(result, Response):
        return result.agent_username, result
    external = get_runtime_agent(str(agent_id))
    if external:
        return (external.get('agent_username') or external.get('username') or ''), None
    return '', None


def _is_archived_username(username):
    if not username:
        return False
    return FleetAgentArchive.objects.filter(agent_username__iexact=username).exists()


class FleetAgentArchiveView(APIView):
    """POST /v1/fleet/agents/{id}/archive/ — stop + park an agent durably.

    Operator 2026-07-10: archiving replaces deletion for "keep it, but NEVER
    auto-start it". The agent is stopped best-effort and its username recorded
    in FleetAgentArchive; every automatic starter (on-demand reconciler,
    enable endpoint, daemon boot via enabled=false) refuses archived agents
    until unarchive.
    """

    def post(self, request, agent_id):
        denied = _require_staff(request)
        if denied:
            return denied
        username, native = _agent_username_for_id(agent_id)
        if not username:
            return Response({'error': 'Agent not found'}, status=status.HTTP_404_NOT_FOUND)
        reason = str((request.data or {}).get('reason') or '')[:300]
        # Best-effort stop first so the archived state matches reality.
        stop_detail = ''
        if native is not None:
            if native.status == FleetAgent.Status.RUNNING:
                if not provisioner.stop_agent(native):
                    stop_detail = 'native stop failed — agent archived anyway; stop manually'
            FleetAgentEvent.objects.create(
                agent=native, event_type='archived',
                message=f'archived by {_actor(request)}' + (f': {reason}' if reason else ''),
            )
        else:
            ok, detail = set_runtime_agent_enabled(str(agent_id), False)
            if not ok:
                stop_detail = f'rt-fleet disable failed ({detail}) — agent archived anyway'
        marker, created = FleetAgentArchive.objects.get_or_create(
            agent_username=username,
            defaults={'archived_by': str(_actor(request) or ''), 'reason': reason},
        )
        agent_index_cache.invalidate('agent archived')
        logger.info('agent archived: user=%s agent=%s created=%s', _actor(request), username, created)
        body = {'archived': True, 'agent_username': username, 'archived_at': marker.archived_at}
        if stop_detail:
            body['warning'] = stop_detail
        return Response(body)


def _agent_username_for_unarchive(agent_id):
    """Resolve username for unarchive, including DELETED native rows.

    HIVE-1255 live gap: archive parks agents that may later be DELETED or
    absent from the non-deleted index. Unarchive must still lift the marker
    when the caller presents the canonical UUID/username. Prefer live
    non-deleted rows, then deleted native rows, then rt-fleet/external, then
    a direct username match against FleetAgentArchive.
    """
    username, native = _agent_username_for_id(agent_id)
    if username:
        return username, native

    # Include DELETED native rows (excluded by _get_agent).
    try:
        agent = FleetAgent.objects.get(id=agent_id)
        return agent.agent_username, agent
    except (FleetAgent.DoesNotExist, ValueError, ValidationError):
        pass
    try:
        agent = FleetAgent.objects.filter(agent_username__iexact=str(agent_id).strip()).first()
        if agent is not None:
            return agent.agent_username, agent
    except Exception:
        pass

    # Archive marker keyed by username — allow unarchive by username when the
    # UUID is no longer resolvable but the park record remains.
    key = str(agent_id or '').strip()
    if key and FleetAgentArchive.objects.filter(agent_username__iexact=key).exists():
        return key, None
    return '', None


class FleetAgentUnarchiveView(APIView):
    """POST /v1/fleet/agents/{id}/unarchive/ — lift the archive marker.

    The agent stays stopped; start it explicitly (or let the on-demand
    reconciler pick it up again now that it is eligible).
    """

    def post(self, request, agent_id):
        username, _native = _agent_username_for_unarchive(agent_id)
        is_staff = bool(getattr(request.user, 'is_staff', False))
        if not username:
            if not is_staff:
                _audit_unarchive_jit(
                    request,
                    target=str(agent_id),
                    result='denied',
                    reason='agent not found',
                )
            return Response({'error': 'Agent not found'}, status=status.HTTP_404_NOT_FOUND)

        canonical_username = str(username).strip().lower()
        jit_scope = f'hive.fleet.agent.unarchive:{canonical_username}'
        grant = None
        if not is_staff:
            grant, decision, reason = _active_jit_grant_for_scope(request, jit_scope)
            if grant is None:
                result = 'dependency-error' if decision == 'dependency' else 'denied'
                _audit_unarchive_jit(
                    request,
                    target=canonical_username,
                    result=result,
                    reason=reason,
                    scope=jit_scope,
                )
                http_status = (
                    status.HTTP_503_SERVICE_UNAVAILABLE
                    if decision == 'dependency'
                    else status.HTTP_403_FORBIDDEN
                )
                error = (
                    'Admin JIT authorization unavailable'
                    if decision == 'dependency'
                    else 'Active scoped JIT grant required'
                )
                return Response({'error': error}, status=http_status)

        deleted, _ = FleetAgentArchive.objects.filter(agent_username__iexact=canonical_username).delete()
        agent_index_cache.invalidate('agent unarchived')
        if grant is not None:
            _audit_unarchive_jit(
                request,
                target=canonical_username,
                result='allowed',
                reason='exact active Admin JIT grant',
                scope=jit_scope,
                grant=grant,
            )
        logger.info('agent unarchived: user=%s agent=%s existed=%s jit=%s',
                    _actor(request), canonical_username, bool(deleted), grant is not None)
        return Response({'archived': False, 'agent_username': canonical_username})


class ConnectWakeAgentView(APIView):
    """HIVE-602 follow-through (operator 2026-07-10): event-driven wake for a
    STOPPED agent that just received a Connect DM.

    Connect's DM fanout calls this service-to-service with a shared secret the
    moment a message lands, so a sleeping recipient boots and the WS
    missed-message replay delivers the DM into its loop — instead of the
    message waiting for a Pulse-workload poll that never looks at Connect.
    Wake-only (never stops anything); idempotent for running agents.
    """
    authentication_classes: list = []
    permission_classes: list = []

    def post(self, request):
        import hmac as _hmac
        expected = (os.environ.get('HIVE_CONNECT_WAKE_TOKEN') or '').strip()
        supplied = (request.headers.get('X-Hive-Wake-Token') or '').strip()
        if not expected or not supplied or not _hmac.compare_digest(supplied, expected):
            return Response({'error': 'forbidden'}, status=403)
        from fleet.on_demand import (
            agent_enabled as _agent_enabled,
            agent_eligible_to_start as _agent_eligible_to_start,
            canon_email as _canon,
            lifecycle_agent_emails as _lifecycle_emails,
        )
        email = _canon(request.data.get('email'))
        reason = str(request.data.get('reason') or 'connect_dm')[:200]
        if not email:
            return Response({'error': 'email required'}, status=400)
        try:
            agents = list_lifecycle_agents(include_disabled=True)
        except Exception as exc:
            return Response({'status': 'error', 'detail': f'runtime snapshot failed: {exc}'[:200]}, status=502)
        agent = next((a for a in agents if email in _lifecycle_emails(a)), None)
        if agent is None:
            return Response({'status': 'unknown_agent', 'email': email}, status=404)
        username = agent.get('agent_username') or agent.get('username') or ''
        if _is_archived_username(username):
            # PLAT-9052: archived seats cannot be woken; drop any stale
            # deferral so the registry tracks starvation, not history.
            from fleet.wake_defers import clear_wake_defer
            clear_wake_defer(email)
            return Response({'status': 'archived', 'email': email}, status=409)
        if _agent_enabled(agent):
            # PLAT-9052: the seat is up -- an outstanding deferral is stale.
            from fleet.wake_defers import clear_wake_defer
            clear_wake_defer(email)
            return Response({'status': 'already_running', 'email': email})
        if not _agent_eligible_to_start(agent):
            # PLAT-9052: operator-stopped seats are not wakeable by design;
            # drop the deferral so it cannot age into a permanent page.
            from fleet.wake_defers import clear_wake_defer
            clear_wake_defer(email)
            return Response({
                'status': 'operator_stopped',
                'email': email,
                'detail': 'explicitly stopped agents are not auto-wakeable',
            }, status=409)
        # Lease-aware wake (operator 2026-08-08): never boot a governed
        # agent into a full model-lease pool. Waking on a 7/7 board makes the
        # agent cold-prefill, sit lease-gated, and get re-hibernated by the
        # next 10-min reconcile ("does not hold the model lease") — the
        # confirmed kei/ryo flap. Defer ordinary wakes instead.
        # CEO Office / keep-resident seats are not exempt (operator 2026-08-18):
        # they stay up by holding a real lease, not by bypassing the board.
        from fleet.model_lease import lease_wake_available
        wake_ok, wake_reason = lease_wake_available(agent=agent)
        if not wake_ok:
            if not provisioner.LeaseStartDecision(False, wake_reason).pending:
                return Response({
                    'status': 'error', 'email': email, 'reason': wake_reason,
                    'detail': 'Model lease authority could not be confirmed',
                }, status=status.HTTP_503_SERVICE_UNAVAILABLE)
            # PLAT-9052: the deferral must outlive the log line -- this
            # registry is what the deferred-wake observability series and the
            # "reconcile grants when a slot frees" promise are accountable to.
            from fleet.wake_defers import record_wake_defer
            record_wake_defer(email, reason=wake_reason)
            # PLAT-9052 direction-2 (PLAT-7032 doctrine): before starving a
            # work-holding seat behind a full pool whose grants may never
            # release (the apply-time idle fence can defer revokes forever),
            # attempt one fenced preemption — evict a quiet, non-reserved,
            # personally-idle holder with no personal signal and hand the
            # wakee a real grant. Every fence fails closed to the defer below.
            preempted, preempt_detail = False, wake_reason
            if wake_reason == 'lease_pool_full':
                from fleet.model_lease import wake_preempt_lease_slot
                preempted, preempt_detail = wake_preempt_lease_slot(
                    agent=agent, reason=reason, agents=agents,
                )
                if preempted:
                    logger.info(
                        'connect-wake: PREEMPT %s (reason=%s detail=%s)',
                        email, reason, preempt_detail,
                    )
            if not preempted:
                logger.info(
                    'connect-wake: DEFER %s (reason=%s detail=%s): wake_delay %s',
                    email, reason, wake_reason,
                    (
                        f'preempt unavailable ({preempt_detail}); '
                        'reconcile grants when a slot frees'
                        if wake_reason == 'lease_pool_full'
                        else 'lease pool full; reconcile grants when a slot frees'
                    ),
                )
                return Response({
                    'status': 'lease_waiting',
                    'email': email,
                    'detail': (
                        f'model lease pool full; wake deferred until a slot frees '
                        f'({wake_reason})'
                    ),
                }, status=202)
        ok, detail = set_lifecycle_agent_enabled(str(agent.get('id') or ''), True)
        if not ok:
            waiting = _lifecycle_lease_wait_response(detail, email=email)
            if waiting is not None:
                return waiting
        logger.info('connect-wake: %s -> %s (reason=%s detail=%s)',
                    email, 'started' if ok else 'FAILED', reason, str(detail)[:120])
        if ok:
            # PLAT-9052: the seat is up; the deferral it was waiting on is
            # resolved. A FAILED start keeps the entry -- the seat is still
            # down and the starvation clock keeps running.
            from fleet.wake_defers import clear_wake_defer
            clear_wake_defer(email)
        return Response({'status': 'started' if ok else 'error',
                         'email': email, 'detail': str(detail)[:200]},
                        status=200 if ok else 502)


class FleetCompanyStateView(APIView):
    """GET /api/internal/company-state/fleet/v1/ (HIVE-1569).

    Ingress-blocked, GET-only. Authenticated with a dedicated Pulse→Hive read
    token (constant-time comparison). Unknown/wrong org fails closed.
    Initially only COMPANY_STATE_INFRA_ORG_SLUG=shizuha may receive the
    platform aggregate; every other org receives explicit not-configured.
    """

    authentication_classes = []
    permission_classes = []

    def get(self, request):
        import hmac
        from .company_state import CompanyStateSourceUnavailable, SCHEMA, build_fleet_aggregate

        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        expected_token = (os.environ.get('COMPANY_STATE_READ_TOKEN') or '').strip()
        supplied_token = auth_header[7:].strip() if auth_header.startswith('Bearer ') else ''
        if not expected_token or not supplied_token or not hmac.compare_digest(supplied_token, expected_token):
            return Response({'error': 'forbidden'}, status=403)

        org_slug = request.query_params.get('org_slug', '').strip().lower()
        infra_org = (os.environ.get('COMPANY_STATE_INFRA_ORG_SLUG') or 'shizuha').strip().lower()

        if not org_slug or org_slug != infra_org:
            return Response({
                'schema': SCHEMA,
                'schema_version': SCHEMA,
                'observed_at': timezone.now().isoformat(),
                'scope': 'not_configured',
                'status': 'not_configured',
            })

        try:
            return Response(build_fleet_aggregate())
        except CompanyStateSourceUnavailable as exc:
            return Response({
                'schema': SCHEMA,
                'schema_version': SCHEMA,
                'observed_at': timezone.now().isoformat(),
                'scope': 'platform_aggregate',
                'status': 'source_unavailable',
                'source': exc.source,
            }, status=503)


class FleetLifecycleAvailabilityView(APIView):
    """GET /internal/v1/fleet/lifecycle-availability/ — Pulse routing fold.

    Same dedicated read token as company-state. Returns the Hive lifecycle
    status of every managed seat so Pulse can mark hibernated agents
    unavailable and drain their ready work to a live teammate.
    """

    authentication_classes = []
    permission_classes = []

    def get(self, request):
        import hmac

        from .lifecycle_fleet import list_lifecycle_agents

        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        expected_token = (os.environ.get('COMPANY_STATE_READ_TOKEN') or '').strip()
        supplied_token = auth_header[7:].strip() if auth_header.startswith('Bearer ') else ''
        if not expected_token or not supplied_token or not hmac.compare_digest(supplied_token, expected_token):
            return Response({'error': 'forbidden'}, status=403)

        agents = []
        for agent in list_lifecycle_agents(include_disabled=True):
            username = str(
                agent.get('agent_username') or agent.get('username') or ''
            ).strip().lower()
            if not username:
                continue
            email = str(agent.get('email') or '').strip().lower()
            if not email:
                email = f'{username}@shizuha.com'
            agents.append({
                'username': username,
                'email': email,
                'lifecycle_status': str(agent.get('status') or '').strip().lower(),
                'enabled': bool(agent.get('enabled')),
            })
        return Response({
            'schema': 'hive.lifecycle_availability.v1',
            'observed_at': timezone.now().isoformat(),
            'agents': agents,
        })


class FleetSeatLifecycleMetricsView(APIView):
    """GET /internal/v1/fleet/seat-lifecycle-metrics — PLAT-8834.

    Prometheus text exposition of seat-lifecycle transition telemetry:
    ``hive_seat_lifecycle_transitions_total{seat, from_status, to_status}``
    and ``hive_seat_last_transition_unixtime{seat}``. Unauthenticated like
    the other internal metrics endpoints (claw-idle, codex-refresh) — the
    payload is aggregate counters only, no seat state detail beyond the
    label values, and the monitoring plane scrapes it in-cluster.
    """

    authentication_classes = []
    permission_classes = []

    def get(self, request):
        from . import seat_lifecycle_metrics
        return HttpResponse(
            seat_lifecycle_metrics.to_prometheus(),
            content_type='text/plain; version=0.0.4; charset=utf-8',
        )

