"""Runtime-fleet aggregation for externally managed agents.

Hive owns k3s-native FleetAgent rows, but the org also has the older host Docker
runtime fleet. The UI should show both through one neutral "agent" surface
without giving Hive lifecycle control over the external runtime.
"""
import logging
import ast
import json
import os
import re
import time
from datetime import datetime, timezone as datetime_timezone
from typing import Any

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .installation_config import effective_agent_env, effective_runtime_config, effective_config_frame
from .parallelism import runtime_agent_parallelism
from .pending_config import pending_config_or_empty

logger = logging.getLogger(__name__)


METHOD_LABELS = {
    'codex_app_server': 'Codex',
    'claude_code_server': 'Claude Code',
    'openclaw_app_server': 'OpenClaw',
    'openclaw_bridge': 'OpenClaw',
    'grok_build': 'Grok Build',
    'shizuha': 'Shizuha CLI',
}

RUNNING_STATUSES = {'running', 'starting', 'provisioning', 'pending'}
FAILED_STATUSES = {'error', 'failed'}
OFFLINE_STATUSES = {'offline'}

# Host kubectl dumps (memcache.go / "must be logged in") are not agent faults.
# 2026-08-14 devops cards painted Failed/Needs help from a fleet-daemon
# kube-auth blip (PLAT-5276 class).
_K8S_CONTROL_PLANE_NOISE = (
    'you must be logged in to the server',
    'the server has asked for the client to provide credentials',
    "couldn't get current server api group list",
    'k8s reconcile start failed',
    'cluster api rejected fleet-daemon credentials',
    'cluster api unreachable from fleet daemon',
)


def is_k8s_control_plane_noise(message: str) -> bool:
    low = (message or '').lower()
    return any(needle in low for needle in _K8S_CONTROL_PLANE_NOISE)

# Mirror of the daemon's platform MCP service registry (PLATFORM_MCP_SERVICES in
# shizuha-beta cli/src/platform/mcp-services.ts). The daemon does not expose this
# over HTTP, so Hive carries the slugs; override with settings.FLEET_PLATFORM_MCP_SERVICES
# without a deploy if the daemon registry grows. HIVE-331: capability validity must
# come from this registry, NOT from which services happen to be attached right now —
# otherwise a service dropped from the last capability that references it can never
# be re-added through the API.
PLATFORM_MCP_SERVICE_SLUGS = [
    'pulse', 'id', 'admin', 'notes', 'wiki', 'drive', 'hive', 'connect',
    'finance', 'books', 'hr', 'time', 'inventory', 'mail', 'scs',
    # Private per-agent service resolved from SHIZUHA_BROWSER_MCP_URL rather
    # than the platform port table. It is nevertheless a valid capability ref.
    'browser',
]


def get_platform_mcp_service_slugs() -> set[str]:
    configured = getattr(settings, 'FLEET_PLATFORM_MCP_SERVICES', None)
    return {str(s).strip() for s in (configured or PLATFORM_MCP_SERVICE_SLUGS) if str(s).strip()}


def _runtime_base_url() -> str:
    return getattr(settings, 'RUNTIME_FLEET_API_URL', '').rstrip('/')


def _runtime_timeout() -> float:
    return float(getattr(settings, 'RUNTIME_FLEET_API_TIMEOUT', 3))


def _primary_method(agent: dict[str, Any]) -> str:
    return (
        agent.get('executionMethod')
        or (agent.get('modelFallbacks') or [{}])[0].get('method')
        or 'shizuha'
    )


def _primary_model(agent: dict[str, Any], method: str) -> str:
    overrides = agent.get('modelOverrides') or {}
    fallback = (agent.get('modelFallbacks') or [{}])[0]
    return overrides.get(method) or fallback.get('model') or agent.get('model') or ''


def _primary_reasoning_effort(agent: dict[str, Any], method: str) -> str:
    overrides = agent.get('modelOverrides') or {}
    fallback = (agent.get('modelFallbacks') or [{}])[0]
    return (
        overrides.get(f'{method}_reasoning_effort')
        or fallback.get('reasoningEffort')
        or agent.get('reasoningEffort')
        or ''
    )


def _model_provider(method: str, model: str) -> str:
    key = (method or '').lower()
    name = (model or '').lower()
    if 'codex' in key or name.startswith(('gpt', 'o1', 'o3', 'o4', 'codex')):
        return 'OpenAI'
    if 'claude' in key or name.startswith(('claude', 'opus', 'sonnet', 'haiku')):
        return 'Anthropic'
    if (
        key in ('shizuha', 'grok_build')
        or name.startswith((
            'glm', 'qwen', 'kimi', 'deepseek', 'mistral', 'minimax', 'nemotron',
            'gpt-oss', 'grok-', 'cortex/', 'xai/grok',
        ))
    ):
        return 'Cortex'
    return 'custom'


def normalize_runtime_agent(agent: dict[str, Any]) -> dict[str, Any]:
    method = _primary_method(agent)
    model = _primary_model(agent, method)
    effort = _primary_reasoning_effort(agent, method)
    enabled = bool(agent.get('enabled'))
    raw_status = (agent.get('status') or '').lower()
    if raw_status in FAILED_STATUSES:
        status = 'failed'
    elif raw_status in OFFLINE_STATUSES:
        status = 'offline'
    elif raw_status in RUNNING_STATUSES or raw_status in {'hibernated', 'stopped', 'disabled'}:
        status = 'stopped' if raw_status == 'disabled' else raw_status
    else:
        status = 'provisioning' if enabled else 'stopped'
    provider = _model_provider(method, model)
    platform_user_id = agent.get('platformUserId')
    username = agent.get('username') or ''
    # Hive fleet agents always place as k3s pods (never host bare_metal/docker).
    # Coerce any legacy daemon/UI value so normalize never reintroduces host pin.
    from .runtime_placement import coerce_hive_fleet_runtime_environment
    _runtime_env = coerce_hive_fleet_runtime_environment(
        agent.get('runtimeEnvironment') or agent.get('runtime_environment'),
    )
    _is_k8s = _runtime_env == 'k8s'
    raw_email = agent.get('email') or ''
    email_synthesized = not bool(raw_email) and bool(username)
    organization_slug = (
        agent.get('organization_slug')
        or agent.get('organizationSlug')
        or agent.get('org_slug')
        or agent.get('orgSlug')
        or ''
    )
    tenant_org_slug = (
        agent.get('tenant_org_slug')
        or agent.get('tenantOrgSlug')
        or organization_slug
        or ''
    )
    parallelism = runtime_agent_parallelism(agent)

    status_message = agent.get('error') or agent.get('lastError') or ''
    if status in RUNNING_STATUSES:
        # Runtime restarts can leave a stale lastError such as "Exited with code
        # 137" in a previous snapshot. Once the live daemon reports the process
        # running again, do not surface that as the current card health.
        status_message = ''
    elif is_k8s_control_plane_noise(str(status_message)):
        # Do not persist a raw kubectl dump or mark the agent Failed for a
        # host/control-plane auth blip. The next successful observe is truth.
        status_message = ''
        if status == 'failed':
            status = 'running' if enabled else 'stopped'

    return {
        'id': agent.get('id'),
        'agent_username': username,
        'agent_id': str(platform_user_id) if platform_user_id is not None else '',
        'display_name': agent.get('name') or username,
        'role': agent.get('role') or 'Agent',
        'email': raw_email or (f'{username}@shizuha.com' if username else ''),
        'email_synthesized': email_synthesized,
        'organization_slug': organization_slug,
        'tenant_org_slug': tenant_org_slug,
        'org_slug': organization_slug,
        'model': model,
        'effective_model': model,
        'model_source': 'runtime_daemon',
        'model_provider': provider,
        'execution_method': method,
        'reasoning_effort': effort,
        # The effort the runtime actually applies, so the Agents UI shows a
        # setting for EVERY agent. Explicit wins; else the per-harness default
        # (Codex/Grok bake in 'high'; Claude Code / Shizuha run at model default).
        'effective_reasoning_effort': (
            effort.strip() if (effort or '').strip()
            else ('high' if method in ('codex_app_server', 'openclaw_app_server', 'grok_build') else 'default')
        ),
        'reasoning_effort_is_default': not (effort or '').strip(),
        'image': '',
        # Hive fleet is k3s-only (never bare_metal host or docker-run placement).
        'runtime_environment': _runtime_env,
        'effective_image': 'rt-fleet k8s runtime',
        'image_source': 'runtime_daemon',
        'image_pull_policy': '',
        'runtime_harness': METHOD_LABELS.get(method, method or 'Runtime'),
        'runtime_provider': provider,
        'runtime_image_tag': '',
        'runtime_summary': ' / '.join([p for p in [METHOD_LABELS.get(method, method), provider, model] if p]),
        'runtime_source': 'rt-fleet',
        'external_runtime': True,
        'lifecycle_managed': False,
        'enabled': enabled,
        'lifecycle_state': (
            agent.get('lifecycleState')
            or agent.get('lifecycle_state')
            or ''
        ),
        'hibernation_reason': agent.get('hibernationReason') or agent.get('hibernation_reason') or '',
        'hibernated_at': agent.get('hibernatedAt') or agent.get('hibernated_at'),
        'allow_parallel_execution': bool(agent.get('allowParallelExecution')),
        'max_concurrent_tasks': agent.get('maxConcurrentTasks'),
        'effective_parallelism': parallelism.as_dict(),
        'parallel_execution_supported': parallelism.supported,
        'parallel_execution_readiness': parallelism.status,
        'parallel_execution_reason': parallelism.reason,
        'resource_profile': agent.get('tier') or 'runtime',
        'resource_profile_spec': agent.get('resourceLimits') or {},
        'resource_overrides': agent.get('resourceLimits') or {},
        'arch': 'arm64' if _is_k8s else 'amd64',
        'requires_longhorn': False,
        'node_selector_overrides': {} if _is_k8s else {'kubernetes.io/hostname': 's1'},
        'isolated': False,
        'namespace': 'shizuha-fleet' if _is_k8s else 'rt-fleet',
        'cohort': '',
        'sandbox_required': False,
        'pvc_size': '',
        'workspace_mount_path': '',
        'sidecar_set': [],
        'credential_grant_scopes': [],
        'skills': agent.get('skills') or [],
        'eager_skills': agent.get('eagerSkills') or agent.get('eager_skills') or [],
        'skill_config': {'skills': agent.get('skills') or [], 'eager_skills': agent.get('eagerSkills') or []},
        'mcp_servers': agent.get('mcpServers') or agent.get('mcp_servers') or [],
        'enabled_mcp_server_ids': [
            (srv.get('slug') or srv.get('name'))
            for srv in (agent.get('mcpServers') or agent.get('mcp_servers') or [])
            if isinstance(srv, dict) and (srv.get('slug') or srv.get('name'))
        ],
        'mcp_config': agent.get('mcpConfig') or agent.get('mcp_config') or {},
        # placement already set above as _runtime_env (k8s); keep one key only
        'env': agent.get('env') or {},
        'context_prompt': agent.get('contextPrompt') or agent.get('context_prompt') or '',
        'personality_traits': agent.get('personalityTraits') or agent.get('personality_traits') or {},
        'model_fallbacks': agent.get('modelFallbacks') or agent.get('model_fallbacks') or [],
        'model_overrides': agent.get('modelOverrides') or agent.get('model_overrides') or {},
        'token_budget': agent.get('tokenBudget') or agent.get('token_budget') or {},
        'agent_memory': agent.get('agentMemory') or agent.get('agent_memory') or '',
        'work_schedule': agent.get('workSchedule') or agent.get('work_schedule') or {},
        'warm_pool_size': agent.get('warmPoolSize') or agent.get('warm_pool_size') or 0,
        'tier': agent.get('tier') or 'normal',
        'failover_chain_id': agent.get('failoverChainId') or agent.get('failover_chain_id') or '',
        'platform_url': '',
        'expose_http': True,
        'http_port': 8016,
        'status': status,
        'status_message': status_message,
        'pod_name': f'agent-{username}' if _is_k8s else 'shizuha-runtime-fleet',
        'created_at': agent.get('createdAt'),
        'updated_at': agent.get('updatedAt'),
        'provisioned_at': agent.get('startedAt'),
        'last_active_at': agent.get('lastActiveAt'),
    }


# Short-TTL cache so the agents page NEVER blocks on the rt-fleet daemon (operator
# 2026-06-24: frontends must not block on sync backend calls). Within the TTL we serve
# the cached list instantly; a cold fetch is capped to a short timeout; on ANY failure
# (daemon slow/respawning/down) we serve the last-known list (stale) instead of hanging
# 15s or returning empty. Keyed by include_disabled. Per-worker is fine.
_RUNTIME_CACHE: dict[bool, tuple[float, list]] = {}
_RUNTIME_CACHE_TTL = 8.0          # seconds to serve a cached list without re-hitting the daemon
_RUNTIME_PAGE_TIMEOUT_CAP = 3.0   # max seconds the page-path fetch may block the response


def _daemon_ws_cache_ready() -> bool:
    """Return True if the daemon-link read model is safe to serve.

    A connected daemon is not enough: during reconnects the registry is marked
    connected before the full snapshot arrives. Serving FleetAgent rows in that
    window exposes stale desired/provisioning state in the UI. Only demote the
    HTTP poll after Hive has received ``state_snapshot_complete``.
    """
    try:
        from .models import DaemonRegistry
        return DaemonRegistry.objects.filter(
            status='connected',
            snapshot_complete=True,
        ).exists()
    except Exception:
        return False


def _fleet_agent_row_to_runtime(agent) -> dict[str, Any]:
    """Project a daemon-managed FleetAgent cache row back into runtime shape."""
    mcp_config = dict(agent.mcp_config or {})
    enabled_mcp = mcp_config.get('enabled_mcp_server_ids') or []
    mcp_servers = mcp_config.get('mcp_servers') or [
        {'slug': slug, 'name': slug} for slug in enabled_mcp if slug
    ]
    skill_config = agent.skill_config or {}
    raw = {
        'id': agent.agent_id or str(agent.id),
        'platformUserId': agent.agent_id or '',
        'username': agent.agent_username,
        'email': f'{agent.agent_username}@shizuha.com' if agent.agent_username else '',
        'name': agent.display_name or agent.agent_username,
        'role': agent.role or 'Agent',
        'status': agent.status,
        'enabled': agent.status in RUNNING_STATUSES,
        'lifecycleState': agent.runtime_lifecycle_state or '',
        'model': agent.model,
        'modelFallbacks': agent.model_fallbacks or [],
        'modelOverrides': agent.model_overrides or {},
        'executionMethod': mcp_config.get('execution_method') or 'shizuha',
        # Hive fleet placement is always k3s (never bare_metal host spawn).
        'runtimeEnvironment': 'k8s',
        'resourceLimits': agent.resource_overrides or {},
        'skills': skill_config.get('skills') or [],
        'eagerSkills': skill_config.get('eager_skills') or [],
        'mcpServers': mcp_servers,
        'mcpConfig': mcp_config,
        'env': effective_agent_env(agent.env),
        'contextPrompt': agent.context_prompt or '',
        'personalityTraits': agent.personality_traits or {},
        'reasoningEffort': agent.reasoning_effort or '',
        'failoverChainId': agent.failover_chain_id or '',
        'lastError': agent.status_message or '',
        'hibernationReason': agent.hibernation_reason or '',
        'hibernatedAt': agent.hibernated_at.isoformat() if agent.hibernated_at else None,
    }
    normalized = normalize_runtime_agent(raw)
    normalized['created_at'] = agent.created_at.isoformat() if agent.created_at else None
    normalized['updated_at'] = agent.updated_at.isoformat() if agent.updated_at else None
    normalized['provisioned_at'] = agent.provisioned_at.isoformat() if agent.provisioned_at else None
    normalized['last_active_at'] = agent.last_active_at.isoformat() if agent.last_active_at else None
    # HIVE-597: surface un-confirmed desired-config keys so the UI can show a
    # "syncing" indicator until the daemon's config_applied lands.
    normalized['pending_config_keys'] = sorted(pending_config_or_empty(
        agent.pending_config,
        agent_identifier=agent.agent_username or agent.pk,
        boundary='runtime_serializer',
    ))
    return normalized


def _list_daemon_link_runtime_agents(include_disabled: bool = False) -> list[dict[str, Any]]:
    from .models import DaemonRegistry, FleetAgent

    rows = (
        FleetAgent.objects
        .filter(managing_daemon__status=DaemonRegistry.Status.CONNECTED)
        .exclude(status=FleetAgent.Status.DELETED)
        .select_related('managing_daemon')
        .order_by('agent_username')
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        agent = _fleet_agent_row_to_runtime(row)
        raw_status = (agent.get('status') or '').lower()
        is_active = bool(agent.get('enabled')) or raw_status in RUNNING_STATUSES or raw_status in FAILED_STATUSES
        if not include_disabled and not is_active:
            continue
        if agent.get('id') and agent.get('agent_username'):
            out.append(agent)
    return out


def _overlay_hive_hibernation_intent(agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Restore Hive's semantic zero-state over an rt-fleet process snapshot.

    The daemon truthfully reports only ``disabled``/``stopped``. Its snapshot
    cannot decide whether the operator disabled the agent or Hive temporarily
    yielded its lease, so the persisted Hive desired-state row supplies that
    distinction when the process is not running.
    """
    if not agents:
        return agents
    try:
        from .models import FleetAgent
        rows = {
            row.agent_username.lower(): row
            for row in FleetAgent.objects.filter(
                managing_daemon__isnull=False,
                status=FleetAgent.Status.HIBERNATED,
            )
        }
    except Exception:
        return agents
    for agent in agents:
        if agent.get('enabled'):
            continue
        row = rows.get(str(agent.get('agent_username') or '').strip().lower())
        if row is None:
            continue
        if str(agent.get('status') or '').lower() not in {'stopped', 'offline', 'hibernated'}:
            continue
        agent['status'] = FleetAgent.Status.HIBERNATED
        agent['hibernation_reason'] = row.hibernation_reason
        agent['hibernated_at'] = row.hibernated_at.isoformat() if row.hibernated_at else None
    return agents


def list_runtime_agents(include_disabled: bool = False) -> list[dict[str, Any]]:
    # HIVE-235/SCLI-199: when a daemon-link WS has completed its snapshot, the
    # daemon's snapshot and deltas populate FleetAgent rows locally. If the WS is
    # merely connected/reconnecting but snapshot_complete is false, keep using the
    # secret-gated HTTP snapshot so a partial daemon-link seed cannot make healthy
    # agents appear stuck in Provisioning/Changing.
    if _daemon_ws_cache_ready():
        logger.debug('runtime-fleet: daemon WS snapshot complete — serving daemon-link cache')
        return _list_daemon_link_runtime_agents(include_disabled=include_disabled)

    base_url = _runtime_base_url()
    if not base_url:
        return []

    now = time.monotonic()
    cached = _RUNTIME_CACHE.get(include_disabled)
    if cached and (now - cached[0]) < _RUNTIME_CACHE_TTL:
        return _overlay_hive_hibernation_intent([dict(agent) for agent in cached[1]])

    try:
        # Cap the timeout: a slow/respawning daemon must degrade fast, not hang the page.
        # Readonly proxy is Hive-secret-gated (operator 2026-07-09) — same shared
        # secret as control; without it cluster list calls 403.
        headers = {}
        secret = _control_secret()
        if secret:
            headers['X-Fleet-Control-Secret'] = secret
        response = requests.get(
            f'{base_url}/v1/agents',
            headers=headers,
            timeout=min(_runtime_timeout(), _RUNTIME_PAGE_TIMEOUT_CAP),
        )
        response.raise_for_status()
        payload = response.json() or {}
    except Exception as exc:
        # Serve the last-known list (stale) rather than blocking or returning empty.
        logger.warning('runtime-fleet list failed (%s); serving %s', exc,
                       'cached' if cached else 'empty')
        return (
            _overlay_hive_hibernation_intent([dict(agent) for agent in cached[1]])
            if cached else []
        )

    out = []
    for agent in payload.get('agents') or []:
        raw_status = (agent.get('status') or '').lower()
        is_active = bool(agent.get('enabled')) or raw_status in RUNNING_STATUSES or raw_status in FAILED_STATUSES
        if not include_disabled and not is_active:
            continue
        normalized = normalize_runtime_agent(agent)
        if normalized.get('id') and normalized.get('agent_username'):
            out.append(normalized)
    out = _overlay_hive_hibernation_intent(out)
    _RUNTIME_CACHE[include_disabled] = (now, out)
    return out


def get_runtime_agent(agent_id: str) -> dict[str, Any] | None:
    for agent in list_runtime_agents(include_disabled=True):
        if str(agent.get('id')) == str(agent_id):
            return agent
    return None


def _control_secret() -> str:
    return getattr(settings, 'RUNTIME_FLEET_CONTROL_SECRET', '') or ''


def _post_control(path: str, payload: dict, label: str) -> tuple[bool, Any]:
    """POST a lifecycle control action to the secret-gated runtime-fleet control
    proxy (HIVE-171). `path` is the daemon control route under /v1/ (e.g.
    'agents/toggle' or 'agents/<id>/restart'); `payload` is the JSON body; `label`
    is the human verb for logs/errors. Sends the mandatory shared secret header;
    the proxy enforces the allow-list + secret server-side.

    A JSON body is ALWAYS sent: the control proxy forces
    `content-type: application/json` on every POST, so a bodyless request makes the
    daemon's JSON parser reject it with 500 "Invalid JSON". The daemon's real
    control contract is POST /v1/agents/toggle (enable/disable) and
    POST /v1/agents/<id>/restart — NOT per-verb /enable|/disable routes (those 404).
    Returns (ok, detail) where detail is the proxy JSON or an error string."""
    base_url = _runtime_control_url()
    if not base_url:
        return False, 'RUNTIME_FLEET_CONTROL_URL unset'
    secret = _control_secret()
    if not secret:
        return False, 'runtime-fleet control secret not configured'
    try:
        response = requests.post(
            f'{base_url}/v1/{path}',
            headers={'X-Fleet-Control-Secret': secret},
            json=payload,
            timeout=_runtime_timeout(),
        )
    except Exception as exc:
        logger.warning('runtime-fleet control %s failed: %s', label, exc)
        return False, f'runtime-fleet unreachable: {exc}'
    if response.status_code >= 400:
        detail = ''
        try:
            detail = response.text[:200]
        except Exception:
            pass
        return False, f'control {label} returned {response.status_code}: {detail}'
    try:
        return True, response.json()
    except Exception:
        return True, {'status': label}


def _control_unreachable(detail: Any) -> bool:
    """True only for connection-level control failures (daemon/proxy gone).

    HTTP 4xx/5xx from the proxy (bad secret, contract error) must stay loud —
    the fallback is for a DOWN control plane (CTX-496), never for auth or
    contract failures.
    """
    return isinstance(detail, str) and detail.startswith('runtime-fleet unreachable')


def _fleet_agent_for_fallback(agent_id: str):
    """Resolve the Hive SSOT row for a daemon agent (by agent_id, then username)."""
    from .models import FleetAgent

    return (
        FleetAgent.objects.filter(agent_id=agent_id).first()
        or FleetAgent.objects.filter(agent_username=agent_id).first()
    )


def _daemon_down_scale_fallback(agent_id: str, enabled: bool) -> tuple[bool, str]:
    """PLAT-4902 second declarative control path (CTX-496 item 7).

    The daemon and its proxies are single replicas node-bound to s1; while they
    are down every lifecycle action fails and demanded on-demand starts stall
    (2026-07-18: ~5.5h). Daemon toggle semantics are exactly scale-to-N on the
    agent's Deployment and Hive is the desired-state SSOT, so scaling the
    EXISTING Deployment directly is idempotent with what the daemon will
    converge to when it returns. Provisioning stays daemon-only: no Deployment
    (or no FleetAgent row) → no fallback.
    """
    from . import provisioner

    try:
        agent = _fleet_agent_for_fallback(agent_id)
    except Exception as exc:
        return False, f'fallback lookup failed: {exc}'
    if agent is None:
        return False, 'no FleetAgent row for agent — provisioning is daemon-only'
    try:
        ok = provisioner.emergency_scale(agent, 1 if enabled else 0)
    except Exception as exc:
        return False, f'fallback scale failed: {exc}'
    if not ok:
        return False, f'fallback scale of {agent.deployment_name} failed (see provisioner log)'
    logger.warning(
        'runtime-fleet control down — executed %s for %s via k8s scale fallback (PLAT-4902)',
        'enable' if enabled else 'disable',
        agent.agent_username,
    )
    return True, f'scaled {agent.deployment_name} to {1 if enabled else 0}'


def set_runtime_agent_enabled(
    agent_id: str,
    enabled: bool,
    *,
    lifecycle_state: str = 'operator_stopped',
) -> tuple[bool, Any]:
    """Enable (start) or disable (stop) a live rt-fleet (docker) agent via the
    secret-gated control proxy. Uses the daemon's POST /v1/agents/toggle route with
    overrideKillSwitch=true so a Hive Start clears the daemon kill-switch (SCLI-110)
    — the same daemon contract the pre-HIVE-171 path used.

    PLAT-4902: if the control plane is unreachable at connection level, fall
    back to scaling the agent's existing Deployment declaratively so a single
    node loss cannot stall demanded lifecycle actions (CTX-496 item 7)."""
    if enabled:
        # Every daemon enable, including the direct UI and post-provision
        # paths, must observe the same authority as native start_agent.
        # A board read never reserves a free slot for this caller.
        from . import provisioner
        from .model_lease import lease_wake_available
        try:
            row = _find_runtime_agent_row(agent_id)
            agent = _fleet_agent_row_to_runtime(row) if row is not None else get_runtime_agent(agent_id)
            if agent is None:
                return False, {'error': 'runtime start identity unavailable'}
            if (not str(agent.get('effective_model') or agent.get('model') or '').strip()
                    and agent.get('execution_method', '') in {'', 'shizuha', 'grok_build'}):
                return False, {'error': 'runtime model authority unavailable',
                               'reason': 'model_unresolved'}
            allowed, reason = lease_wake_available(agent=agent)
            decision = provisioner.LeaseStartDecision(allowed, reason)
            if not decision.allowed:
                if row is not None:
                    provisioner._record_lease_start_refusal(row, decision)
                return False, {
                    'lease_waiting': decision.pending, 'reason': decision.reason,
                    'error': 'runtime start requires a confirmed model lease',
                }
        except Exception:  # noqa: BLE001 — unknown authority cannot enable
            logger.exception('runtime lease authority unavailable for %s', agent_id)
            return False, {'error': 'runtime lease authority unavailable',
                           'reason': 'gate_error_fail_closed'}
    payload = {
        'agent_id': agent_id,
        'enabled': enabled,
        'overrideKillSwitch': True,
    }
    if not enabled:
        if lifecycle_state not in {'hibernated', 'operator_stopped'}:
            raise ValueError(
                'lifecycle_state must be hibernated or operator_stopped'
            )
        payload['lifecycle_state'] = lifecycle_state
    ok, detail = _post_control(
        'agents/toggle',
        payload,
        'enable' if enabled else 'disable',
    )
    if ok or not _control_unreachable(detail):
        return ok, detail
    fb_ok, fb_detail = _daemon_down_scale_fallback(agent_id, enabled)
    if fb_ok:
        return True, {'plane': 'k8s-scale-fallback', 'detail': fb_detail, 'control_error': detail}
    return False, f'{detail}; {fb_detail}'


def restart_runtime_agent(agent_id: str) -> tuple[bool, Any]:
    """Restart a live rt-fleet (docker) agent via the secret-gated control proxy
    (graceful, daemon-side). Sends an empty JSON body ({}) so the proxy-forced
    application/json content-type still parses on the daemon."""
    return _post_control(f'agents/{agent_id}/restart', {}, 'restart')


def reset_runtime_agent_session(agent_id: str) -> tuple[bool, Any]:
    """Clear a live rt-fleet agent's durable runtime session via the daemon.

    The daemon owns both docker workspaces and k8s PVC sqlite (including the
    wire-prefix snapshot that would otherwise replay a poisoned transcript).
    """
    return _post_control(f'agents/{agent_id}/reset-session', {}, 'reset-session')


def _control_daemon_id() -> str:
    """The daemon_id fronted by RUNTIME_FLEET_CONTROL_URL. The control proxy is a
    single Service in front of ONE daemon, so top-down provision must refuse any
    other daemon (never provision user A's agent onto user B's daemon). Per-daemon
    control endpoints / a daemon-link provision frame lift this restriction later.
    """
    return (
        getattr(settings, 'RUNTIME_FLEET_CONTROL_DAEMON_ID', '')
        or os.environ.get('RUNTIME_FLEET_CONTROL_DAEMON_ID', '')
        or 'rt-fleet-s1'
    )


def provision_runtime_agent_via_daemon(agent, daemon) -> tuple[bool, Any]:
    """HIVE-247 top-down provision: materialize a Hive-owned FleetAgent row on a
    live fleet daemon and start it — the UNIFIED creation path (operator
    2026-07-13: every agent is the same kind of thing, owned by a user, managed
    by that user's fleet daemon; "personal" is config, not a separate plane).

    Sequence: control-proxy POST /v1/agents/provision (daemon-side identity gate
    + idempotent op_id, HIVE-247) → bind the returned runtime agent_id +
    managing_daemon on the row → push Hive's desired env/MCP config through the
    normal configure path (pending_config-protected) → toggle-on (spawns the pod
    via the daemon's own spawn path, k8s backend on rt-fleet).

    Idempotency at the Hive level: callers must not re-invoke this for a row that
    already has agent_id + managing_daemon — the daemon would materialize a
    SECOND roster entry for the same username. Use set_runtime_agent_enabled to
    (re)start an already-materialized agent instead.
    """
    from .models import DaemonRegistry, FleetAgent

    if not runtime_control_enabled():
        return False, 'RUNTIME_FLEET_CONTROL_URL unset'
    if daemon is None or daemon.status != DaemonRegistry.Status.CONNECTED:
        return False, 'owner daemon not connected'
    if daemon.daemon_id != _control_daemon_id():
        return False, f'no control endpoint for daemon {daemon.daemon_id}'
    if agent.agent_id and agent.managing_daemon_id:
        return False, 'agent already materialized on a daemon (use toggle/start, not provision)'

    mcp = agent.mcp_config or {}
    skill = agent.skill_config or {}
    method = str(mcp.get('execution_method') or 'shizuha')
    fallbacks = list(agent.model_fallbacks or [])
    if agent.model and not any(
        isinstance(fb, dict) and fb.get('model') == agent.model for fb in fallbacks
    ):
        head: dict[str, Any] = {'method': method, 'model': agent.model}
        if agent.reasoning_effort:
            head['reasoningEffort'] = agent.reasoning_effort
        fallbacks = [head] + fallbacks
    # op_id: stable across network-blip retries (a lost 201 replays the same
    # materialization) but VARIES with the identity-binding epoch — the daemon
    # caches REJECTS too (1h TTL), so a 'no-canonical-shizuha-id' reject issued
    # before the ID account existed must not be replayed after the identity is
    # (re)bound. Canary-observed 2026-07-13 ("duplicate":true on retry).
    _epoch = ''
    if getattr(agent, 'identity_provisioned_at', None):
        _epoch = f'-{int(agent.identity_provisioned_at.timestamp())}'
    body = {
        'op_id': f'hive-provision-{agent.pk}{_epoch}',
        'username': agent.agent_username,
        'name': agent.display_name or agent.agent_username,
        # Canonical owner-derived email: shizuha-id REQUIRES it for personal-
        # default identities (reconcile 400s on mismatch — canary 2026-07-13).
        # Mirrors provisioner._provision_platform_identity_details exactly.
        'email': f'{agent.agent_username}@agents.shizuha.io' if agent.owner_id is not None
        else f'{agent.agent_username}@shizuha.com',
        'role': agent.role or '',
        'executionMethod': method,
        'runtimeEnvironment': 'k8s',
        'skills': list(skill.get('skills') or []),
        'personalityTraits': agent.personality_traits or {},
        'modelOverrides': agent.model_overrides or {},
        'modelFallbacks': fallbacks,
        'contextPrompt': agent.context_prompt or '',
    }
    ok, detail = _post_control('agents/provision', body, 'provision')
    if not ok:
        return False, str(detail)
    runtime_id = ''
    if isinstance(detail, dict):
        if detail.get('admitted') is False:
            return False, f"daemon rejected provision (identity gate): {detail.get('reasons')}"
        runtime_id = str(detail.get('agent_id') or '')
    if not runtime_id:
        return False, f'daemon returned no agent_id: {detail}'

    FleetAgent.objects.filter(pk=agent.pk).update(
        agent_id=runtime_id[:64],
        managing_daemon=daemon,
        status=FleetAgent.Status.PROVISIONING,
        status_message='',
    )
    agent.refresh_from_db()

    # Desired config the provision body cannot carry (MCP servers, env) — pushed
    # through the same daemon-link configure path the Agents UI uses, so it is
    # pending_config-protected and replays on daemon reconnect. Fail-soft.
    config_updates: dict[str, Any] = {}
    if effective_agent_env(agent.env):
        # Persist only the explicit agent edit; output boundaries add defaults.
        config_updates['env'] = dict(agent.env or {})
    if mcp.get('enabled_mcp_server_ids'):
        config_updates['enabled_mcp_server_ids'] = mcp.get('enabled_mcp_server_ids')
    if mcp.get('mcp_servers'):
        config_updates['mcp_servers'] = mcp.get('mcp_servers')
    if agent.reasoning_effort:
        config_updates['reasoning_effort'] = agent.reasoning_effort
    if config_updates:
        try:
            configure_runtime_agent(runtime_id, config_updates)
        except Exception:
            logger.exception('post-provision config push failed for %s', agent.agent_username)

    ok2, d2 = set_runtime_agent_enabled(runtime_id, True)
    if not ok2:
        if isinstance(d2, dict) and d2.get('lease_waiting') is True:
            # The daemon materializes with desiredEnabled=False. Keep that
            # cold identity bound while the ordinary planner grants a lease;
            # PROVISIONING would incorrectly project as already running and
            # suppress its later START when no Deployment exists yet.
            FleetAgent.objects.filter(pk=agent.pk).update(
                status=FleetAgent.Status.HIBERNATED,
                hibernation_reason='awaiting model lease grant',
                hibernated_at=timezone.now(),
            )
            agent.refresh_from_db()
            return False, d2
        return False, f'materialized but enable failed: {d2}'
    return True, ''

# --------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Lifecycle control for the host-docker runtime fleet (HIVE-171).
#
# The read-only list/get path uses RUNTIME_FLEET_API_URL (the readonly proxy).
# enable/disable/restart go through the secret-gated control-aware proxy at
# RUNTIME_FLEET_API_URL (see _post_control above; HIVE-171 Option C / #114).
# configure/activity below use RUNTIME_FLEET_CONTROL_URL — a SEPARATE var that is
# currently UNSET on the Hive deploy, so those paths are dormant no-ops until
# wired up; when wired, they must also be secret-gated (tracked as a follow-up).
# Only explicit allow-listed actions are forwarded — never arbitrary passthrough;
# callers (views) must already have enforced staff auth.
# ---------------------------------------------------------------------------
def _runtime_control_url() -> str:
    # settings.py wires RUNTIME_FLEET_API_URL from env but NOT this one, so reading
    # settings alone always returned '' → every control-proxy op (enable/disable,
    # restart, activity transcript, config push) silently no-op'd. Honour the env
    # var the deployment actually sets. (Same settings-vs-env trap as the heartbeat
    # redis / connect URLs.)
    return (
        getattr(settings, 'RUNTIME_FLEET_CONTROL_URL', '')
        or os.environ.get('RUNTIME_FLEET_CONTROL_URL', '')
    ).rstrip('/')


def runtime_control_enabled() -> bool:
    return bool(_runtime_control_url())


def _apply_cached_runtime_config(agent, updates: dict, save: bool = True) -> None:
    """Apply Hive-side desired config to the daemon-link cache row immediately."""
    if 'role' in updates:
        agent.role = updates['role'] or ''
    if 'model' in updates:
        agent.model = updates['model'] or ''
    if 'model_fallbacks' in updates:
        agent.model_fallbacks = updates['model_fallbacks'] or []
        if updates['model_fallbacks']:
            agent.model = (updates['model_fallbacks'][0] or {}).get('model') or agent.model
    if 'model_overrides' in updates:
        agent.model_overrides = updates['model_overrides'] or {}
    if 'context_prompt' in updates:
        agent.context_prompt = updates['context_prompt'] or ''
    if 'personality_traits' in updates:
        agent.personality_traits = updates['personality_traits'] or {}
    if 'env' in updates:
        agent.env = updates['env'] or {}
    if 'resource_limits' in updates:
        agent.resource_overrides = updates['resource_limits'] or {}
    if 'reasoning_effort' in updates:
        agent.reasoning_effort = updates['reasoning_effort'] or ''
    if 'failover_chain_id' in updates:
        agent.failover_chain_id = updates['failover_chain_id'] or ''

    skill_config = dict(agent.skill_config or {})
    if 'skills' in updates:
        skill_config['skills'] = updates['skills'] or []
    if 'eager_skills' in updates:
        skill_config['eager_skills'] = updates['eager_skills'] or []
    agent.skill_config = skill_config

    mcp_config = dict(agent.mcp_config or {})
    if 'execution_method' in updates:
        mcp_config['execution_method'] = updates['execution_method'] or 'shizuha'
    # Always k8s for Hive fleet — ignore bare_metal/host writes.
    from .runtime_placement import coerce_hive_fleet_runtime_environment
    mcp_config['runtime_environment'] = coerce_hive_fleet_runtime_environment(
        updates.get('runtime_environment', mcp_config.get('runtime_environment')),
    )
    if 'enabled_mcp_server_ids' in updates:
        ids = updates['enabled_mcp_server_ids'] or []
        mcp_config['enabled_mcp_server_ids'] = ids
        mcp_config['mcp_servers'] = [{'slug': slug, 'name': slug} for slug in ids if slug]
    for key in ('work_schedule', 'max_concurrent_tasks', 'allow_parallel_execution', 'token_budget', 'agent_memory', 'warm_pool_size', 'tier'):
        if key in updates:
            mcp_config[key] = updates[key]
    agent.mcp_config = mcp_config
    if save:
        agent.save()


def _find_runtime_agent_row(agent_id: str, *, for_update: bool = False):
    """Locate the daemon-link FleetAgent cache row by agent_id or pk."""
    from .models import FleetAgent

    filters = Q(agent_id=str(agent_id))
    try:
        import uuid
        uuid.UUID(str(agent_id))
        filters |= Q(id=agent_id)
    except (TypeError, ValueError):
        pass
    rows = FleetAgent.objects
    if for_update:
        # ``managing_daemon`` is nullable and select_related emits an outer
        # join; PostgreSQL cannot lock the nullable side. Lock only FleetAgent.
        rows = rows.select_for_update(of=('self',))
    return (
        rows
        .select_related('managing_daemon')
        .filter(filters)
        .exclude(status=FleetAgent.Status.DELETED)
        .first()
    )


def _persist_runtime_lane_generation(
    agent,
    lane: dict,
    actor: str,
    *,
    base_generation=None,
    previous_lane: dict | None = None,
    requested_patch: dict | None = None,
    rollback_of=None,
):
    """Persist desired state, RuntimeChange, and outbox in one transaction."""
    from .models import AgentRuntimeDesiredState, RuntimeChange, RuntimeLaneOutbox
    from .runtime_lane import lane_config, lane_digest, lane_payload

    desired, created = AgentRuntimeDesiredState.objects.select_for_update().get_or_create(
        agent=agent,
        defaults={
            'platform_user_id': str(agent.identity_user_id or agent.agent_id or ''),
            'canonical_username': agent.agent_username,
            'canonical_email': f'{agent.agent_username}@shizuha.com',
            'desired_generation': 1,
        },
    )
    current_generation = 0 if created else int(desired.desired_generation or 1)
    if base_generation is not None and int(base_generation) != current_generation:
        from .runtime_lane import RuntimeLaneError
        raise RuntimeLaneError(
            'GENERATION_CONFLICT',
            f'Runtime lane generation changed from {base_generation} to {current_generation}; refetch and retry.',
        )
    next_generation = current_generation + 1
    _supersede_older_runtime_lane_changes_locked(agent, next_generation)
    prior_profile_lane = dict((desired.runtime_profile or {}).get('runtime_lane') or {})
    rollback_lane = (
        prior_profile_lane if prior_profile_lane.get('runtime_release_id')
        else dict(previous_lane or {})
    )
    desired.previous_generation = None if created else current_generation
    desired.desired_generation = next_generation
    desired.desired_config_hash = lane_digest(lane)
    desired.runtime_profile = {
        'runtime_lane': lane_payload(lane),
        'previous_runtime_lane': rollback_lane,
        'registry_version': lane.get('registry_version'),
        'lane_digest': desired.desired_config_hash,
    }
    desired.source = AgentRuntimeDesiredState.Source.HIVE_UI
    desired.source_actor = str(actor or '')[:150]
    desired.save()
    change = RuntimeChange.objects.create(
        agent=agent,
        base_generation=current_generation,
        desired_generation=next_generation,
        requested_patch=dict(requested_patch or {}),
        resolved_lane=lane_payload(lane),
        previous_lane=rollback_lane,
        registry_version=lane['registry_version'],
        runtime_release_id=lane['runtime_release_id'],
        lane_digest=desired.desired_config_hash,
        state=RuntimeChange.State.ACTUATOR_PENDING,
        rollback_of=rollback_of,
    )
    frame = {
        'type': 'config_update',
        'agent_id': agent.agent_id or str(agent.id),
        'child_id': agent.agent_id or str(agent.id),
        'agent_username': agent.agent_username,
        'change_id': str(change.id),
        'desired_generation': next_generation,
        'runtime_lane_registry_version': lane['registry_version'],
        'runtime_release_id': lane['runtime_release_id'],
        'runtime_lane_digest': desired.desired_config_hash,
        'runtime_lane': lane_payload(lane),
        'config': {**dict(requested_patch or {}), **lane_config(lane)},
    }
    from .config_fingerprints import config_fingerprints
    frame['config_fingerprints'] = config_fingerprints(frame['config'])
    RuntimeLaneOutbox.objects.create(change=change, payload=frame)
    return desired, change


def _supersede_older_runtime_lane_changes_locked(agent, desired_generation: int) -> int:
    """Retire older unacknowledged lanes before a newer generation becomes truth.

    A transient send failure can leave generation N in ACTUATOR_PENDING.  If a
    later generation is accepted and applied, replaying N forever is both noisy
    and unsafe: the daemon correctly rejects it as stale, while Hive previously
    kept treating it as retryable.  The caller already holds the agent/desired
    state transaction; lock the affected outboxes and make supersession a
    terminal, explicit state.
    """
    from django.utils import timezone as tz
    from .models import RuntimeChange, RuntimeLaneOutbox

    rows = list(
        RuntimeLaneOutbox.objects.select_for_update()
        .select_related('change')
        .filter(
            change__agent=agent,
            change__desired_generation__lt=desired_generation,
            delivered_at__isnull=True,
        )
    )
    if not rows:
        return 0
    now = tz.now()
    for row in rows:
        row.delivered_at = now
        row.last_error = f'SUPERSEDED_BY_GENERATION_{desired_generation}'
        row.save(update_fields=['delivered_at', 'last_error', 'updated_at'])
        change = row.change
        change.state = RuntimeChange.State.SUPERSEDED
        change.failure_code = 'SUPERSEDED_BY_NEWER_GENERATION'
        change.failure_evidence = (
            f'Generation {change.desired_generation} was superseded by '
            f'generation {desired_generation} before exact acknowledgement.'
        )
        change.save(update_fields=[
            'state', 'failure_code', 'failure_evidence', 'updated_at',
        ])
    return len(rows)


def supersede_stale_runtime_lane_outboxes(*, daemon_id=None) -> int:
    """Repair legacy orphan outboxes whose agent already desires a newer lane.

    This is invoked by both reconnect replay and heartbeat retry, so rows left
    behind before the write-time supersession invariant was introduced converge
    automatically without an operator/database cleanup pass.
    """
    from .models import AgentRuntimeDesiredState, RuntimeLaneOutbox

    candidates = RuntimeLaneOutbox.objects.filter(delivered_at__isnull=True)
    if daemon_id is not None:
        candidates = candidates.filter(change__agent__managing_daemon_id=daemon_id)
    agent_ids = list(candidates.values_list('change__agent_id', flat=True).distinct())
    if not agent_ids:
        return 0
    repaired = 0
    with transaction.atomic():
        desired_rows = (
            AgentRuntimeDesiredState.objects.select_for_update()
            .select_related('agent')
            .filter(agent_id__in=agent_ids)
        )
        for desired in desired_rows:
            repaired += _supersede_older_runtime_lane_changes_locked(
                desired.agent, int(desired.desired_generation or 1),
            )
    return repaired


def runtime_change_payload(change) -> dict:
    return {
        'change_id': str(change.id),
        'generation': change.desired_generation,
        'state': change.state,
        'failure_code': change.failure_code,
        'failure_evidence': change.failure_evidence,
        'registry_version': change.registry_version,
        'runtime_release_id': change.runtime_release_id,
        'lane_digest': change.lane_digest,
        'resolved_lane': change.resolved_lane,
        'previous_lane': change.previous_lane,
        'rollback_of': str(change.rollback_of_id) if change.rollback_of_id else None,
    }


def runtime_lane_outbox_to_prometheus() -> str:
    """Fail-loud liveness for the durable lane dispatcher (engineering-owned)."""
    from django.db.models import Min, Sum
    from .models import RuntimeLaneOutbox

    pending = RuntimeLaneOutbox.objects.filter(delivered_at__isnull=True)
    summary = pending.aggregate(
        oldest=Min('created_at'), attempts=Sum('attempt_count'),
    )
    oldest = summary['oldest']
    age = max(0.0, (timezone.now() - oldest).total_seconds()) if oldest else 0.0
    return '\n'.join([
        '# HELP hive_runtime_lane_outbox_pending Durable runtime-lane events awaiting exact acknowledgement.',
        '# TYPE hive_runtime_lane_outbox_pending gauge',
        f'hive_runtime_lane_outbox_pending{{owner_team="engineering"}} {pending.count()}',
        '# HELP hive_runtime_lane_outbox_oldest_seconds Age of the oldest pending runtime-lane event.',
        '# TYPE hive_runtime_lane_outbox_oldest_seconds gauge',
        f'hive_runtime_lane_outbox_oldest_seconds{{owner_team="engineering"}} {age:.3f}',
        '# HELP hive_runtime_lane_outbox_attempts Dispatch attempts among pending runtime-lane events.',
        '# TYPE hive_runtime_lane_outbox_attempts gauge',
        f'hive_runtime_lane_outbox_attempts{{owner_team="engineering"}} {int(summary["attempts"] or 0)}',
        '',
    ])


def _redact_runtime_evidence(value: str) -> str:
    """Sanitize structured and string-shaped failures before persistence."""
    secret_key = re.compile(
        r'(?i)(?:authorization|bearer|token|api[_-]?key|secret|access[_-]?token|refresh[_-]?token)'
    )

    def sanitize(item):
        if isinstance(item, dict):
            return {
                str(key): ('[REDACTED]' if secret_key.search(str(key)) else sanitize(child))
                for key, child in item.items()
            }
        if isinstance(item, (list, tuple)):
            return [sanitize(child) for child in item]
        return item

    structured = value
    if isinstance(value, str):
        candidate = value.strip()
        if candidate[:1] in {'{', '['}:
            for parser in (json.loads, ast.literal_eval):
                try:
                    parsed = parser(candidate)
                except (ValueError, SyntaxError, TypeError, json.JSONDecodeError):
                    continue
                if isinstance(parsed, (dict, list, tuple)):
                    structured = parsed
                    break
    if isinstance(structured, (dict, list, tuple)):
        return json.dumps(sanitize(structured), sort_keys=True, separators=(',', ':'))[:1000]

    evidence = str(value or '')[:2000]
    # Quoted JSON/Python-object values must be handled before the broad token
    # fallback; otherwise punctuation lets the secret survive unchanged.
    evidence = re.sub(
        r'''(?ix)(["']?(?:authorization|token|api[_-]?key|secret|access[_-]?token|refresh[_-]?token)["']?\s*:\s*)(["'])(.*?)(\2)''',
        r'\1\2[REDACTED]\4',
        evidence,
    )
    evidence = re.sub(r'(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer [REDACTED]', evidence)
    evidence = re.sub(
        r'(?i)\b(authorization|token|api[_-]?key|secret|access[_-]?token|refresh[_-]?token)\b'
        r'\s*(?:=|:)\s*[^\s,;}]+',
        r'\1=[REDACTED]',
        evidence,
    )
    return evidence[:1000]


def observed_runtime_lane_payload(agent) -> dict:
    """Return only a lane proven healthy by an exact correlated health frame."""
    from .models import RuntimeChange

    observed = (
        RuntimeChange.objects.filter(
            agent=agent,
            state__in=[RuntimeChange.State.HEALTHY, RuntimeChange.State.ROLLED_BACK],
        )
        .order_by('-desired_generation')
        .first()
    )
    if observed is None:
        return {'known': False, 'state': 'UNKNOWN'}
    return {
        'known': True,
        **dict(observed.resolved_lane or {}),
        'generation': observed.desired_generation,
        'lane_digest': observed.lane_digest,
        'state': observed.state,
    }


def _create_runtime_lane_rollback_locked(change, *, code: str, evidence: str):
    """Create at most one rollback generation for a failed forward change."""
    from .models import AgentRuntimeDesiredState, FleetAgent, RuntimeChange
    from .runtime_lane import lane_config, resolve_runtime_lane

    evidence = _redact_runtime_evidence(evidence)
    if change.rollback_of_id:
        change.state = RuntimeChange.State.ROLLBACK_FAILED
        change.failure_code = code
        change.failure_evidence = evidence
        change.save(update_fields=['state', 'failure_code', 'failure_evidence', 'updated_at'])
        parent = change.rollback_of
        parent.state = RuntimeChange.State.ROLLBACK_FAILED
        parent.failure_code = code
        parent.failure_evidence = evidence
        parent.save(update_fields=['state', 'failure_code', 'failure_evidence', 'updated_at'])
        return None
    try:
        existing = change.rollback_change
    except RuntimeChange.DoesNotExist:
        existing = None
    if existing is not None:
        return existing
    if not change.previous_lane:
        change.state = RuntimeChange.State.ROLLBACK_FAILED
        change.failure_code = 'NO_PREVIOUS_RUNTIME_LANE'
        change.failure_evidence = evidence
        change.save(update_fields=['state', 'failure_code', 'failure_evidence', 'updated_at'])
        return None

    agent = FleetAgent.objects.select_for_update().get(pk=change.agent_id)
    rollback_lane = resolve_runtime_lane({}, change.previous_lane)
    desired = AgentRuntimeDesiredState.objects.select_for_update().get(agent=agent)
    if desired.desired_generation != change.desired_generation:
        # A newer operator change superseded this failure; never roll it back.
        change.state = RuntimeChange.State.FAILED
        change.failure_code = 'SUPERSEDED_ROLLOUT_FAILURE'
        change.failure_evidence = evidence
        change.save(update_fields=['state', 'failure_code', 'failure_evidence', 'updated_at'])
        return None

    change.state = RuntimeChange.State.ROLLBACK_PENDING
    change.failure_code = code
    change.failure_evidence = evidence
    change.save(update_fields=['state', 'failure_code', 'failure_evidence', 'updated_at'])
    _apply_cached_runtime_config(agent, lane_config(rollback_lane), save=False)
    pending = pending_config_or_empty(
        agent.pending_config,
        agent_identifier=agent.agent_username or agent.pk,
        boundary='runtime_lane_rollback',
    )
    pending.update(lane_config(rollback_lane))
    agent.pending_config = pending
    agent.save()
    _desired, rollback = _persist_runtime_lane_generation(
        agent,
        rollback_lane,
        'hive:runtime-lane-rollback',
        base_generation=change.desired_generation,
        previous_lane=change.resolved_lane,
        requested_patch=lane_config(rollback_lane),
        rollback_of=change,
    )
    return rollback


def _terminally_ack_runtime_lane_outbox_locked(change, *, error: str = '') -> None:
    """Retire the exact lane frame once daemon evidence proves it was observed.

    Both config-result and health frames carry the immutable generation+digest
    fence.  Health may legitimately win that race, so it must retire the same
    durable outbox before advancing the change out of its retryable state.
    """
    from django.utils import timezone as tz

    outbox = change.outbox
    if outbox.delivered_at is None:
        outbox.delivered_at = tz.now()
    outbox.last_error = error
    outbox.save(update_fields=['delivered_at', 'last_error', 'updated_at'])


def process_runtime_lane_config_result(agent, frame: dict) -> dict:
    """Fence a daemon result and advance or roll back the matching change."""
    from .models import RuntimeChange

    generation = frame.get('desired_generation')
    digest = str(frame.get('runtime_lane_digest') or '')
    if generation is None or not digest:
        return {'matched': False, 'reason': 'missing_runtime_lane_fence'}
    try:
        generation = int(generation)
    except (TypeError, ValueError):
        return {'matched': False, 'reason': 'invalid_runtime_lane_generation'}
    with transaction.atomic():
        change = RuntimeChange.objects.select_for_update().filter(
            agent=agent, desired_generation=generation,
        ).first()
        if change is None or change.lane_digest != digest:
            return {'matched': False, 'reason': 'stale_or_digest_mismatch'}
        if change.state not in {
            RuntimeChange.State.ACTUATOR_PENDING,
            RuntimeChange.State.ROLLING,
        }:
            return {'matched': True, 'state': change.state, 'idempotent': True}
        if frame.get('ok'):
            change.state = RuntimeChange.State.ROLLING
            change.failure_code = ''
            change.failure_evidence = ''
            change.save(update_fields=['state', 'failure_code', 'failure_evidence', 'updated_at'])
            _terminally_ack_runtime_lane_outbox_locked(change)
            return {'matched': True, 'state': change.state}
        evidence = str(frame.get('error') or frame.get('last_apply_error') or 'daemon apply failed')
        # The forward frame was delivered and terminally rejected. Leaving its
        # outbox row pending while retry filters exclude non-ACTUATOR_PENDING
        # changes makes the stuck-age alert fire forever after an expected
        # rollback. Mark delivery terminal before creating the new rollback
        # generation; only the rollback outbox remains retryable/pending.
        _terminally_ack_runtime_lane_outbox_locked(change, error='ACTUATOR_REJECTED')
        rollback = _create_runtime_lane_rollback_locked(
            change, code='ACTUATOR_REJECTED', evidence=evidence,
        )
        return {
            'matched': True,
            'state': change.state,
            'rollback_frame': effective_config_frame(rollback.outbox.payload) if rollback is not None else None,
        }


def process_runtime_lane_health(agent, frame: dict) -> dict:
    """Correlate an exact observed generation+digest with rollout health."""
    from .models import AgentHealthSnapshot, RuntimeChange

    generation = frame.get('observed_generation') or frame.get('applied_generation')
    # observed_config_hash is the daemon's legacy whole-config fingerprint and
    # is never interchangeable with the immutable RuntimeLane digest.
    digest = str(frame.get('runtime_lane_digest') or '')
    if generation is None or not digest:
        return {'matched': False}
    try:
        generation = int(generation)
    except (TypeError, ValueError):
        return {'matched': False}
    with transaction.atomic():
        change = RuntimeChange.objects.select_for_update().filter(
            agent=agent, desired_generation=generation,
        ).first()
        if change is None or change.lane_digest != digest:
            return {'matched': False}
        provider_health = frame.get('provider_health') or {}
        readiness = (
            frame.get('workload_ready'),
            frame.get('container_ready'),
            frame.get('harness_ready'),
        )
        healthy = (
            str(frame.get('apply_status') or '').lower() == AgentHealthSnapshot.ApplyStatus.OK
            and all(value is True for value in readiness)
            and provider_health.get('available') is True
            and provider_health.get('quota_ok') is True
            and provider_health.get('in_backoff') is False
            and not frame.get('last_apply_error')
            and not frame.get('missing_grants')
            and not (frame.get('credential_grant_health') or {}).get('missing')
        )
        if healthy and change.state in {
            RuntimeChange.State.ACTUATOR_PENDING, RuntimeChange.State.ROLLING,
        }:
            # Health is independently fenced and can arrive before (or instead
            # of) config_result.  Retire the forward frame in this transaction
            # before the state transition makes reconnect/heartbeat retry skip
            # it permanently.
            _terminally_ack_runtime_lane_outbox_locked(change)
            change.state = (
                RuntimeChange.State.ROLLED_BACK if change.rollback_of_id
                else RuntimeChange.State.HEALTHY
            )
            change.save(update_fields=['state', 'updated_at'])
            if change.rollback_of_id:
                parent = change.rollback_of
                parent.state = RuntimeChange.State.ROLLED_BACK
                parent.save(update_fields=['state', 'updated_at'])
            return {'matched': True, 'state': change.state}
        failed = (
            str(frame.get('apply_status') or '').lower() == AgentHealthSnapshot.ApplyStatus.FAILED
            or bool(frame.get('last_apply_error'))
            or bool((frame.get('credential_grant_health') or {}).get('missing'))
            or any(value is False for value in readiness)
            or provider_health.get('available') is False
            or provider_health.get('quota_ok') is False
            or provider_health.get('in_backoff') is True
        )
        if failed and change.state in {
            RuntimeChange.State.ACTUATOR_PENDING, RuntimeChange.State.ROLLING,
        }:
            _terminally_ack_runtime_lane_outbox_locked(
                change, error='RUNTIME_HEALTH_FAILED',
            )
            rollback = _create_runtime_lane_rollback_locked(
                change,
                code='RUNTIME_HEALTH_FAILED',
                evidence=str(frame.get('last_apply_error') or 'runtime readiness/capability probe failed'),
            )
            return {
                'matched': True,
                'state': change.state,
                'rollback_frame': effective_config_frame(rollback.outbox.payload) if rollback is not None else None,
            }
        return {'matched': True, 'state': change.state, 'idempotent': True}


def admit_runtime_lane_update(
    agent_id: str,
    patch: dict,
    *,
    current_agent: dict | None = None,
    actor: str = '',
    base_generation=None,
) -> tuple[bool, dict]:
    """Validate, preflight, persist, and enqueue one complete runtime lane.

    The database row, pending outbox payload, and desired generation are written
    in one transaction.  A daemon frame is emitted only after commit and always
    contains the full lane plus generation/digest fencing metadata.
    """
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer

    from . import agent_index_cache
    from .models import DaemonRegistry, FleetAgent
    from .runtime_lane import (
        RuntimeLaneError,
        lane_config,
        preflight_runtime_lane,
        resolve_runtime_lane,
        runtime_lane_from_agent,
    )

    row = _find_runtime_agent_row(agent_id)
    if row is None:
        return False, {
            'code': 'UNMANAGED_RUNTIME_LANE',
            'error': 'Runtime-lane changes require a Hive-owned durable agent row.',
        }

    try:
        with transaction.atomic():
            locked = (
                # Lock only FleetAgent. PostgreSQL rejects FOR UPDATE across the
                # nullable managing_daemon outer join that select_related()
                # would add; the daemon row is read-only routing metadata here.
                FleetAgent.objects.select_for_update().get(pk=row.pk)
            )
            current_lane = runtime_lane_from_agent(locked)
            if not current_lane.get('model') and current_agent is not None:
                current_lane = runtime_lane_from_agent(current_agent)
            # Normalize legacy/incomplete rows into a safe pinned rollback lane.
            # Treat the current model as the requested anchor so an impossible
            # historical method is canonically derived rather than preserved.
            #
            # A model that has since been REMOVED from the registry must not
            # strand the agent. Resolving the rollback anchor used to raise
            # UNREGISTERED_MODEL for the *current* model and reject the whole
            # update — so an agent left on a retired model (e.g. gpt-5.6-terra,
            # dropped by registry 2026-07-25.1 while 3 agents still ran it)
            # could never be moved to ANY valid model, and the error named the
            # old model, making it read like the caller's request was bad.
            # When the current lane is unresolvable but the patch supplies a
            # model that resolves, anchor on the patch instead: there is no
            # valid prior lane to roll back to, and saying so is honest.
            try:
                previous_lane = resolve_runtime_lane(
                    current_lane, {'model': current_lane.get('model')},
                )
            except RuntimeLaneError:
                if not str(patch.get('model') or '').strip():
                    raise
                previous_lane = resolve_runtime_lane({}, {'model': patch['model']})
                logger.warning(
                    'runtime lane: agent %s is on unregistered model %r; anchoring '
                    'rollback on the requested model instead of stranding it',
                    agent_id, current_lane.get('model'),
                )
            lane = resolve_runtime_lane(previous_lane, patch)
            preflight = preflight_runtime_lane(locked, lane)
            payload = {**patch, **lane_config(lane)}
            _apply_cached_runtime_config(locked, payload, save=False)
            pending = pending_config_or_empty(
                locked.pending_config,
                agent_identifier=locked.agent_username or locked.pk,
                boundary='runtime_lane_admission',
            )
            pending.update(payload)
            locked.pending_config = pending
            locked.save()
            desired, change = _persist_runtime_lane_generation(
                locked,
                lane,
                actor,
                base_generation=base_generation,
                previous_lane=previous_lane,
                requested_patch=patch,
            )
            generation = desired.desired_generation
            digest = desired.desired_config_hash
            daemon = locked.managing_daemon
    except RuntimeLaneError as exc:
        return False, {'code': exc.code, 'error': exc.message}

    channel_layer = get_channel_layer()
    connected = bool(daemon and daemon.status == DaemonRegistry.Status.CONNECTED)
    pending_reason = ''
    if connected and channel_layer is not None:
        frame = effective_config_frame(change.outbox.payload)
        try:
            async_to_sync(channel_layer.group_send)(
                f'daemon-link-{daemon.daemon_id}',
                {'type': 'daemon.config', 'frame': frame},
            )
            change.outbox.attempt_count += 1
            change.outbox.last_attempt_at = timezone.now()
            change.outbox.last_error = ''
            change.outbox.save(update_fields=['attempt_count', 'last_attempt_at', 'last_error', 'updated_at'])
            pending_reason = 'complete lane committed; awaiting exact daemon acknowledgement and pod health'
        except Exception as exc:  # durable outbox remains pending for reconnect retry
            change.outbox.attempt_count += 1
            change.outbox.last_attempt_at = timezone.now()
            change.outbox.last_error = str(exc)[:1000]
            change.outbox.save(update_fields=['attempt_count', 'last_attempt_at', 'last_error', 'updated_at'])
            pending_reason = 'queued: channel dispatch failed; durable lane will retry on reconnect'
    else:
        why = 'daemon-link not connected' if not connected else 'channel layer unavailable'
        pending_reason = f'queued: {why}; durable complete lane will replay on reconnect'

    agent_index_cache.invalidate('runtime lane admitted')
    return True, {
        'lane': lane,
        'credential_preflight': preflight,
        'generation': generation,
        'lane_digest': digest,
        'change_id': str(change.id),
        'rollout_state': change.state,
        'rollout_state_url': f'/v1/fleet/agents/{locked.id}/runtime-changes/{change.id}',
        'pending_reason': pending_reason,
    }


def _configure_runtime_agent_via_daemon_link(agent_id: str, updates: dict) -> tuple[bool, str]:
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer

    from .config_fingerprints import config_fingerprints
    from .models import DaemonRegistry
    from . import agent_index_cache

    # Serialize top-level edits against config_result acknowledgement. Without
    # this lock, push B can race an acknowledgement for push A and lose either
    # B's desired value or its pending fingerprint fence.
    with transaction.atomic():
        agent = _find_runtime_agent_row(agent_id, for_update=True)
        if not agent or not agent.managing_daemon:
            return False, 'no daemon-link cache row for agent'

        # HIVE-597 instant-CRUD contract: the cache row (what the Agents UI reads)
        # is updated SYNCHRONOUSLY on every accepted config push, and the updates
        # are recorded in pending_config until the daemon confirms persistence via
        # a config_result fingerprint (legacy daemons use config_applied deltas).
        _apply_cached_runtime_config(agent, updates, save=False)
        pending = pending_config_or_empty(
            agent.pending_config,
            agent_identifier=agent.agent_username or agent.pk,
            boundary='runtime_configure',
        )
        pending.update(updates)
        agent.pending_config = pending
        agent.save()
        daemon_id = agent.managing_daemon.daemon_id
        connected = agent.managing_daemon.status == DaemonRegistry.Status.CONNECTED
        runtime_id = agent.agent_id or str(agent.id)
        agent_username = agent.agent_username

    channel_layer = get_channel_layer()
    if not connected or channel_layer is None:
        agent_index_cache.invalidate('daemon-link configure (queued)')
        why = ('daemon-link not connected' if not connected else 'channel layer unavailable')
        return True, f'queued: {why}; desired config saved and will replay on daemon reconnect'

    outgoing_updates = effective_runtime_config(updates)
    frame = {
        'type': 'config_update',
        'agent_id': runtime_id,
        'child_id': runtime_id,
        'agent_username': agent_username,
        'config': outgoing_updates,
        # HIVE-597: values can be redacted from observed state (for example a
        # non-secret tuning key whose name contains ``TOKEN``).  The daemon
        # echoes these opaque per-key fingerprints only after persistence, so
        # pending state can converge without returning sensitive values.
        'config_fingerprints': config_fingerprints(outgoing_updates),
    }
    async_to_sync(channel_layer.group_send)(
        f'daemon-link-{daemon_id}',
        {'type': 'daemon.config', 'frame': frame},
    )
    agent_index_cache.invalidate('daemon-link configure')
    # group_send is enqueue evidence, not receiver evidence. Keep the write in
    # a truthful queued state until config_result clears its fingerprints; the
    # heartbeat retry path re-drives pending_config if this send was lost.
    return True, 'queued: awaiting daemon config_result; desired config saved'


def normalize_runtime_config_updates(updates: dict) -> dict:
    """Normalize Hive-originated runtime config before push to the daemon.

    Hive (``PATCH /v1/fleet/agents/{id}/`` / Agents UI) is the operator-facing
    source of truth. The normal contract is one operator-selected primary
    ``execution_method`` + ``model`` pair. Caller-authored fallback maps are
    cleared; Hive may append the bounded platform-managed Grok-to-DeepSeek tail
    defined by ``runtime_lane.managed_model_fallbacks``.
    """
    out = dict(updates or {})
    # Non-empty legacy maps are no longer allowed to synthesize a primary.
    # They are hidden, disabled inputs and may be stale; promoting their head
    # caused fallback-only reapplications to overwrite Hive's explicit model.
    # An explicit model/method remains valid and the legacy maps are cleared.
    # Hive exposes one primary model and one reasoning control. Discard legacy
    # implementation maps before saving or sending desired config, then derive
    # the sole platform-managed fallback from an explicit lane selection.
    if 'model_fallbacks' in out and not (
        'model' in out or 'execution_method' in out
    ):
        # A stale fallback-only daemon snapshot cannot synthesize or overwrite a
        # primary lane. Public callers still cannot author fallback chains.
        out['model_fallbacks'] = []
    if 'model' in out and 'execution_method' in out:
        from .runtime_lane import managed_model_fallbacks
        out['model_fallbacks'] = managed_model_fallbacks(
            out['model'], out['execution_method'],
        )
    if any(key in out for key in (
        'model', 'reasoning_effort', 'execution_method', 'model_overrides',
    )):
        out['model_overrides'] = {}
    return out


def configure_runtime_agent(agent_id: str, updates: dict) -> tuple[bool, str]:
    """Top-down config push for a host-runtime agent (Hive SoT).

    Called only from Hive fleet API (Agents UI / ``PATCH /v1/fleet/agents/{id}/``).
    Prefer the authenticated daemon-link WebSocket (SCLI-199). It writes Hive's
    desired config to the local FleetAgent cache row, sends a config_update frame
    to the live daemon, and the daemon persists/reloads the agent then reports a
    confirming config_result/state_delta. The legacy HTTP control proxy remains the fallback
    while rollout is in progress.

    Do not call the runtime control proxy from operators/agents bypassing Hive —
    that creates a second write path and desyncs the Agents UI.
    """
    if not updates:
        return True, ''  # nothing to change

    from . import agent_index_cache

    updates = normalize_runtime_config_updates(updates)

    ok, err = _configure_runtime_agent_via_daemon_link(agent_id, updates)
    if ok:
        agent_index_cache.invalidate('hive configure daemon-link')
        # HIVE-597: propagate queued detail so the API can return 202
        return True, err or ''

    base = _runtime_control_url()
    if not base:
        return False, err or 'RUNTIME_FLEET_CONTROL_URL unset'
    try:
        secret = _control_secret()
        if not secret:
            return False, 'runtime-fleet control secret not configured'
        r = requests.patch(
            f'{base}/v1/agents/{agent_id}',
            json=effective_runtime_config(updates),
            headers={'X-Fleet-Control-Secret': secret},
            timeout=_runtime_timeout(),
        )
        if r.status_code >= 400:
            return False, f'daemon returned {r.status_code}: {r.text[:200]}'
        # The daemon accepted the config via the control proxy. Mirror it into the
        # daemon-link cache row too — the Agents UI reads that row, so skipping this
        # left the UI showing the old model indefinitely after a fallback push.
        agent = _find_runtime_agent_row(agent_id)
        if agent is not None:
            _apply_cached_runtime_config(agent, updates)
        agent_index_cache.invalidate('hive configure control-proxy')
        return True, ''
    except Exception as exc:
        logger.warning('runtime-fleet configure(%s) failed: %s', agent_id, exc)
        return False, str(exc)


def delete_runtime_agent(agent) -> tuple[bool, str]:
    """Apply Hive's durable delete tombstone to a daemon-managed agent.

    The control proxy is attempted first so currently deployed daemons delete
    their authoritative state-store row and workload immediately.  If that path
    is unavailable, the Hive row remains the durable tombstone and a daemon-link
    frame is sent now (when connected) and replayed on every reconnect.
    """
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer

    from .models import DaemonRegistry

    runtime_id = str(agent.agent_id or agent.id)
    errors: list[str] = []
    base = _runtime_control_url()
    secret = _control_secret()
    if base and secret:
        try:
            response = requests.delete(
                f'{base}/v1/agents/{runtime_id}',
                headers={'X-Fleet-Control-Secret': secret},
                # The control proxy forwards JSON content headers. An empty
                # DELETE body is rejected by the daemon parser as invalid JSON
                # before the route can remove its desired-state row.
                json={},
                timeout=_runtime_timeout(),
            )
            if response.status_code < 400 or response.status_code == 404:
                return True, ''
            errors.append(f'daemon returned {response.status_code}: {response.text[:200]}')
        except Exception as exc:
            errors.append(str(exc))
    elif not base:
        errors.append('RUNTIME_FLEET_CONTROL_URL unset')
    else:
        errors.append('runtime-fleet control secret not configured')

    daemon = getattr(agent, 'managing_daemon', None)
    if daemon is None:
        return False, '; '.join(errors)

    connected = daemon.status == DaemonRegistry.Status.CONNECTED
    channel_layer = get_channel_layer()
    if connected and channel_layer is not None:
        async_to_sync(channel_layer.group_send)(
            f'daemon-link-{daemon.daemon_id}',
            {
                'type': 'daemon.delete_agent',
                'frame': {
                    'type': 'delete_agent',
                    'agent_id': runtime_id,
                    'child_id': runtime_id,
                    'agent_username': agent.agent_username,
                    'reason': 'hive_desired_deleted',
                },
            },
        )
        return True, 'queued: control proxy unavailable; delete sent over daemon-link'

    why = 'daemon-link not connected' if not connected else 'channel layer unavailable'
    return True, f'queued: {why}; Hive tombstone will replay on reconnect'


def get_runtime_skills() -> list[dict[str, Any]]:
    """Return daemon-discovered skills for Hive config pickers.

    The daemon scans ~/.shizuha/skills and exposes frontmatter through /v1/skills.
    Hive treats this as read-only catalog metadata; selected skills still flow
    through PATCH /v1/agents/{id}.
    """
    bases = []
    control = _runtime_control_url()
    readonly = _runtime_base_url()
    if control:
        bases.append((control, True))
    if readonly and readonly != control:
        bases.append((readonly, False))
    if not bases:
        return []
    last_error = None
    for base, needs_secret in bases:
        try:
            headers = {}
            secret = _control_secret()
            if needs_secret and secret:
                headers['X-Fleet-Control-Secret'] = secret
            r = requests.get(f'{base}/v1/skills', headers=headers, timeout=_runtime_timeout())
            r.raise_for_status()
            payload = r.json() or {}
            return payload.get('skills') or []
        except Exception as exc:
            last_error = exc
            continue
    if last_error:
        logger.warning('runtime-fleet skills catalog failed: %s', last_error)
    return []


def get_runtime_agent_activity(agent_id: str, limit: int = 100) -> dict:
    """Per-agent activity transcript (messages / tool calls / results) from the
    daemon's session-JSONL-backed /activity endpoint, via the control proxy.
    Returns {'events': [...], 'total': N, 'sessionFile': str} or {} on failure."""
    base = _runtime_control_url()
    if not base:
        return {}
    try:
        limit = max(1, min(int(limit), 500))
        secret = _control_secret()
        if not secret:
            return {}
        r = requests.get(
            f'{base}/v1/agents/{agent_id}/activity',
            params={'limit': limit},
            headers={'X-Fleet-Control-Secret': secret},
            timeout=_runtime_timeout(),
        )
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        logger.warning('runtime-fleet activity(%s) failed: %s', agent_id, exc)
        return {}


def get_runtime_activity_rates(window: int = 600) -> dict:
    """Per-agent recent output rate (≈ tok/s) keyed by agent id, from the daemon's
    activity-rates endpoint. Returns {agent_id: {words_per_sec, recent_words,
    window_sec, last_activity_ts}} or {} on failure."""
    base = _runtime_control_url()
    if not base:
        return {}
    try:
        secret = _control_secret()
        if not secret:
            return {}
        r = requests.get(
            f'{base}/v1/agents/activity-rates',
            params={'window': int(window)},
            headers={'X-Fleet-Control-Secret': secret},
            timeout=_runtime_timeout(),
        )
        r.raise_for_status()
        return (r.json() or {}).get('rates', {})
    except Exception as exc:
        logger.warning('runtime-fleet activity-rates failed: %s', exc)
        return {}


# ── HIVE-609: live k8s placement for daemon-managed k8s agents ───────────────
_PLACEMENT_CACHE: dict[str, tuple[float, dict]] = {}
_PLACEMENT_TTL_S = 30.0


def _pod_created_at(pod):
    value = getattr(getattr(pod, 'metadata', None), 'creation_timestamp', None)
    return value or datetime.min.replace(tzinfo=datetime_timezone.utc)


def _pod_is_terminating(pod):
    return bool(getattr(getattr(pod, 'metadata', None), 'deletion_timestamp', None))


def _pod_is_ready_running(pod):
    pod_status = getattr(pod, 'status', None)
    if getattr(pod_status, 'phase', '') != 'Running':
        return False
    return any(
        getattr(condition, 'type', None) == 'Ready'
        and str(getattr(condition, 'status', '')).lower() == 'true'
        for condition in (getattr(pod_status, 'conditions', None) or [])
    )


def pick_live_k8s_pod(pods):
    """Select the observed workload pod used by placement and activity reads.

    A terminating rollout pod must never beat its non-terminating replacement.
    Within the active set, Ready+Running wins, followed by Running, with newest
    creation time as the deterministic tie-breaker.
    """
    ordered = sorted(pods or [], key=_pod_created_at, reverse=True)
    if not ordered:
        return None
    active = [pod for pod in ordered if not _pod_is_terminating(pod)] or ordered
    ready = [pod for pod in active if _pod_is_ready_running(pod)]
    if ready:
        return ready[0]
    running = [
        pod for pod in active
        if getattr(getattr(pod, 'status', None), 'phase', '') == 'Running'
    ]
    return running[0] if running else active[0]


def enrich_k8s_placement(agent: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    """For a runtime agent with runtime_environment == 'k8s', resolve the LIVE
    k8s placement (pod, node, arch, image, workspace PVC) so the detail page
    shows the real runtime instead of daemon-plane placeholders. Detail-path
    only (one k8s lookup per view, 30s cached) — never called on the index.
    Fails soft: on any error the agent dict is returned unchanged."""
    import time as _time
    if not force and (agent or {}).get('runtime_environment') != 'k8s':
        return agent
    username = (agent.get('agent_username') or '').lower()
    if not username:
        return agent
    now = _time.monotonic()
    namespace_hint = (agent.get('namespace') or '').strip()
    # Keep the presentation plane in the key: a native detail read and a
    # legacy daemon detail read for the same migrated username share observed
    # placement, but they must not leak each other's runtime_source label.
    cache_key = f'{"native" if force else "runtime"}:{namespace_hint}:{username}'
    hit = _PLACEMENT_CACHE.get(cache_key)
    if hit and now - hit[0] < _PLACEMENT_TTL_S:
        agent.update(hit[1])
        return agent
    try:
        from .provisioner import get_k8s_client, fleet_namespace
        core, apps, _ = get_k8s_client()
        ns = namespace_hint or fleet_namespace()
        placement: dict[str, Any] = {}
        deployment = None
        native_owned = False
        try:
            dep = apps.read_namespaced_deployment(f'agent-{username}', ns)
            deployment = dep
            from .native_runtime_release import enabled, get_native_runtime_release, assert_owned_deployment
            native_owned = enabled() and (dep.metadata.labels or {}).get('app.kubernetes.io/managed-by') == 'shizuha-hive'
            if native_owned:
                try:
                    from .models import FleetAgent
                    row = FleetAgent.objects.exclude(status__in=['deleted', 'deleting']).get(agent_username=username)
                    assert_owned_deployment(row, dep)
                    placement['desired_runtime_release'] = get_native_runtime_release().as_api_dict()
                except Exception:
                    placement['desired_runtime_release'] = {'scope': 'hive-native', 'unavailable': True}
            for c in (dep.spec.template.spec.containers or []):
                if c.name == 'agent':
                    placement['image'] = c.image or ''
                    placement['effective_image'] = c.image or 'rt-fleet k8s runtime'
                    placement['runtime_image_tag'] = (c.image or '').rsplit(':', 1)[-1]
                    break
            for v in (dep.spec.template.spec.volumes or []):
                pvc = getattr(v, 'persistent_volume_claim', None)
                if pvc is not None:
                    placement['workspace_pvc'] = pvc.claim_name
                    break
        except Exception:  # noqa: BLE001 — deployment may be scaled away mid-toggle
            pass
        pods = core.list_namespaced_pod(
            ns,
            label_selector=f'shizuha.io/agent={username}',
            limit=3,
        )
        live = pick_live_k8s_pod(pods.items)
        # HIVE-1326 / PLAT-5041: the canonical `shizuha.io/agent` label is absent
        # on migrated Shape-B deployments (template labels `app: agent-<name>` +
        # `shizuha.io/runtime=k3s-native`). When the canonical selector finds no
        # live pod, fall back to the deployment's own pod selector so the detail
        # drawer resolves the exact live pod/node/arch instead of showing the
        # generic daemon placeholder for an otherwise-healthy live agent.
        if live is None and deployment is not None:
            try:
                match = (deployment.spec.selector.match_labels or {}) if (
                    getattr(deployment, 'spec', None) is not None
                    and getattr(deployment.spec, 'selector', None) is not None
                ) else {}
                fallback = ','.join(f'{k}={v}' for k, v in (match or {}).items()) or None
            except Exception:  # noqa: BLE001
                fallback = None
            if fallback:
                pods = core.list_namespaced_pod(
                    ns,
                    label_selector=fallback,
                    limit=3,
                )
                live = pick_live_k8s_pod(pods.items)
        if live is not None:
            placement['pod_name'] = live.metadata.name
            placement['node_name'] = live.spec.node_name or ''
            if native_owned:
                for container in live.spec.containers or []:
                    if container.name == 'agent':
                        observed = str(container.image or '')
                        placement['observed_runtime_image'] = observed
                        placement['image'] = observed
                        placement['effective_image'] = observed
                        placement['runtime_image_tag'] = ('sha256:' + observed.split('@sha256:', 1)[1][:12]
                            if '@sha256:' in observed else observed.rsplit(':', 1)[-1])
            # Deployment evidence normally supplies these fields. During a
            # rollout/read race, project the same selected live pod instead of
            # falling back to persisted desired metadata.
            if not placement.get('workspace_pvc'):
                for volume in (getattr(live.spec, 'volumes', None) or []):
                    pvc = getattr(volume, 'persistent_volume_claim', None)
                    if pvc is not None and getattr(pvc, 'claim_name', None):
                        placement['workspace_pvc'] = pvc.claim_name
                        break
            if not placement.get('image'):
                for container in (getattr(live.spec, 'containers', None) or []):
                    if getattr(container, 'name', None) == 'agent':
                        image = getattr(container, 'image', None) or ''
                        placement['image'] = image
                        placement['effective_image'] = image or 'k3s-native runtime'
                        placement['runtime_image_tag'] = image.rsplit(':', 1)[-1]
                        break
            try:
                node = core.read_node(live.spec.node_name)
                placement['arch'] = (node.metadata.labels or {}).get('kubernetes.io/arch', '')
            except Exception:  # noqa: BLE001
                placement['arch'] = ''
        if placement:
            placement['namespace'] = ns
            placement['runtime_source'] = (
                'k3s-native'
                if force or agent.get('runtime_source') == 'k3s-native'
                else 'rt-fleet (k8s)'
            )
            _PLACEMENT_CACHE[cache_key] = (now, placement)
            agent.update(placement)
    except Exception:  # noqa: BLE001 — placement is cosmetic; never break the page
        pass
    return agent
