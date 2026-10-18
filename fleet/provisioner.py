"""
Fleet provisioner — imperative k3s actuation for ``FleetAgent`` (HLD §4/§10).

Generalizes the proven ``claws/provisioner.py`` templating (Namespace / Secret /
PVC / Deployment[replicas=1, Recreate] / Service) from claw-only to *any* agent,
emitting a per-agent native pod from a declarative ``FleetAgent`` row.

Pod-spec invariants enforced here (HLD §3-§4):
  * ``automountServiceAccountToken: false`` fleet-wide (Q1a — no kube API path).
  * nodeSelector ``kubernetes.io/arch`` + ``storage.longhorn.io/ready`` (§3/§5).
  * podAntiAffinity (preferred, hostname) over ``shizuha.io/agent`` (§3 spread).
  * ``terminationGracePeriodSeconds: 30`` (§4/§8).
  * Docker is declarative: only agents with ``"dind"`` in ``sidecar_set`` get a
    privileged DinD sidecar + docker CLI/socket wiring. AGENT_PASSWORD never lands
    on the agent container — it is mounted only into the broker sidecar (§5,
    ADR-PLAT-002, PLAT-149).

Namespace model (Q1/Q1b): ordinary agents share ``settings.SHIZUHA_FLEET_NAMESPACE``
(default ``shizuha-fleet``); ``isolated`` agents (DevOps ichi/ni) get their own ns.
Deprovision therefore deletes *per-agent resources* in the shared ns, but tears
down the whole namespace for an isolated agent.

Additive: this never touches the rt-fleet host-docker runtime (PLAT-146 scope).
"""
import ast
import base64
import hashlib
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import shlex
import threading
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass

import requests
from django.conf import settings
from django.utils import timezone
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream

from .models import (
    DaemonRegistry,
    FleetAgent,
    FleetAgentEvent,
    LEAN_CONVERSATIONAL_MCP_SERVERS,
    LEAN_CONVERSATIONAL_TEAM_SLUGS,
    LEAN_CONVERSATIONAL_USERNAMES,
)
from .timeutils import naive_utc_now

logger = logging.getLogger(__name__)

# Env keys that the per-agent `env` override dict must never inject.
# These are either secret-bearing (must only reach the broker/Secret path),
# platform-identity keys owned by Hive, or behavioural fields that have a
# dedicated FleetAgent model field and are injected through it.
_ENV_DENYLIST: frozenset[str] = frozenset({
    # Platform identity — Hive is the authoritative source
    'AGENT_ID', 'AGENT_USERNAME', 'AGENT_NAME',
    'SHIZUHA_AGENT_ID', 'SHIZUHA_AGENT_USERNAME', 'SHIZUHA_CACHE_TTL',
    'SHIZUHA_PLATFORM_URL', 'BACKEND_URL',
    # Secret-bearing — must never appear in the agent container env
    'AGENT_PASSWORD', 'SHIZUHA_AGENT_TOKEN', 'VLLM_API_KEY',
    'FORGEJO_TOKEN', 'ORIGIN_TOKEN', 'OPENAI_API_KEY', 'CORTEX_API_KEY',
    'MCP_AUTH_PROXY_COORDINATOR_OAUTH_TOKEN_URL', 'MCP_AUTH_PROXY_COORDINATOR_OAUTH_CLIENT_ID',
    'MCP_AUTH_PROXY_COORDINATOR_OAUTH_CLIENT_SECRET', 'MCP_AUTH_PROXY_COORDINATOR_OAUTH_SCOPE',
    # Runtime keys owned by the Hive provisioner
    'VLLM_STREAM_WITH_TOOLS', 'MCP_AUTH_PROXY_SOCKET',
    # Behavioural fields with dedicated FleetAgent model fields
    'AGENT_ROLE', 'MODEL', 'REASONING_EFFORT', 'CONTEXT_PROMPT',
    'FAILOVER_CHAIN_ID', 'MODEL_FALLBACKS', 'MODEL_OVERRIDES', 'PERSONALITY_TRAITS',
    # Stale k3s leftovers: runtime may read these as MODEL / fallback pins.
    'SHIZUHA_K8S_PRIMARY_MODEL',
    'SHIZUHA_MODEL_FALLBACKS',
})


def _is_lean_conversational_agent(agent) -> bool:
    username = (agent.agent_username or '').strip().lower()
    existing_env = agent.env if isinstance(getattr(agent, 'env', None), dict) else {}
    team_hint = str(existing_env.get('AGENT_TEAM') or '').strip().lower()
    return username in LEAN_CONVERSATIONAL_USERNAMES or team_hint in LEAN_CONVERSATIONAL_TEAM_SLUGS


def _agent_effective_mcp_services(agent) -> list[str]:
    """Hive SoT MCP allow-list for the in-pod gateway (PLAT-3625).

    k3s-native seats have no provisioned ``~/.mcp.json``. The gateway then
    derives every platform MCP server from ``SHIZUHA_PLATFORM_URL`` and the
    role matrix. Inject the capability grant so the running process connects
    what Hive already decided. CEO Office uses that declared list; if it is
    empty, fall back to the talk-seat floor (platform base + Hive).
    """
    mcp = agent.mcp_config if isinstance(getattr(agent, 'mcp_config', None), dict) else {}
    slugs: list[str] = []
    raw_ids = mcp.get('enabled_mcp_server_ids') or []
    if isinstance(raw_ids, list):
        slugs.extend(str(item).strip().lower() for item in raw_ids if str(item).strip())
    if not slugs:
        for server in mcp.get('mcp_servers') or []:
            if isinstance(server, dict):
                value = str(server.get('slug') or server.get('name') or '').strip().lower()
            else:
                value = str(server or '').strip().lower()
            if value.startswith('shizuha-'):
                value = value[len('shizuha-'):]
            if value:
                slugs.append(value)
    out: list[str] = []
    for slug in slugs:
        if slug and slug not in out:
            out.append(slug)
    if not out and _is_lean_conversational_agent(agent):
        return list(LEAN_CONVERSATIONAL_MCP_SERVERS)
    if _is_lean_conversational_agent(agent):
        for slug in LEAN_CONVERSATIONAL_MCP_SERVERS:
            if slug not in out:
                out.append(slug)
    return out


def _apply_mcp_allow_list_env(agent, env) -> None:
    slugs = _agent_effective_mcp_services(agent)
    if not slugs:
        return
    joined = ','.join(slugs)
    by_name = {item.name: idx for idx, item in enumerate(env)}
    for key in ('AGENT_EFFECTIVE_MCP_SERVICES', 'SHIZUHA_MCP_SERVICES'):
        var = client.V1EnvVar(name=key, value=joined)
        if key in by_name:
            env[by_name[key]] = var
        else:
            env.append(var)


def _apply_lean_conversational_runtime_env(agent, env):
    """Inject long-idle + lean-MCP env for CEO Office talkable seats."""
    if not _is_lean_conversational_agent(agent):
        return
    existing_env = agent.env if isinstance(getattr(agent, 'env', None), dict) else {}
    team_hint = str(existing_env.get('AGENT_TEAM') or '').strip().lower()
    present = {item.name: idx for idx, item in enumerate(env)}
    # First beat used to be 30m (same as idle) — that never warmed prefix
    # cache. 8s is a silent model prefill; later beats stay at 30m.
    stale_first = {'1800000'}
    stale_talk_minimal = {'1', 'true', 'on', 'minimal'}
    # Auto-relay of turn text + message_user("Replied.") dual-wrote every
    # CEO Office grok-4.x Live turn. Voice Think Fast replies as session
    # text/audio and often never calls message_user, so Connect would
    # ack the inbound and stay silent (Hina 2026-08-17).
    model = str(getattr(agent, 'model', '') or '').strip().lower()
    voice_omni = 'grok-voice' in model
    stale_autoreply = set() if voice_omni else {'1', 'true', 'on'}
    desired = {
        'AGENT_TEAM': team_hint or 'ceo-office',
        'SHIZUHA_IDLE_HEARTBEAT_MS': '1800000',
        'SHIZUHA_HEARTBEAT_DEBOUNCE_MS': '1800000',
        'SHIZUHA_FIRST_HEARTBEAT_MS': '8000',
        'SHIZUHA_LEAN_MCP': '1',
        'SHIZUHA_TALK_MINIMAL_PROMPT': '0',
        'SHIZUHA_CONNECT_AUTOREPLY': '1' if voice_omni else '0',
        'VLLM_STREAM_STALL_MS': '20000',
    }
    for key, value in desired.items():
        existing = str((existing_env or {}).get(key) or '')
        if key == 'SHIZUHA_FIRST_HEARTBEAT_MS' and existing in stale_first:
            existing = ''
        if key == 'SHIZUHA_TALK_MINIMAL_PROMPT' and existing.strip().lower() in stale_talk_minimal:
            existing = ''
        if key == 'SHIZUHA_CONNECT_AUTOREPLY' and existing.strip().lower() in stale_autoreply:
            existing = ''
        if existing:
            continue
        var = client.V1EnvVar(name=key, value=value)
        if key in present:
            env[present[key]] = var
        else:
            env.append(var)
            present[key] = len(env) - 1


# Work-agent idle cadence. Operator directive 2026-09-15: agents must
# continuously seek and drain ready work — the 30m clamp (2026-09-14) left the
# fleet silent (1/16 admitted) with ready tasks queued. Restore the 90s
# work-agent standard; the wrap-up spin class (Kumo/Ren) is prevented in the
# harness by the fast-rearm no-progress cap and loop-break fallback, and by
# ce19cfe's empty-heartbeat lease stop, not by starving the work-seeking beat.
WORK_AGENT_IDLE_HEARTBEAT_MS = '90000'
WORK_AGENT_FIRST_HEARTBEAT_MS = '8000'
WORK_AGENT_HEARTBEAT_DEBOUNCE_MS = '90000'
STALE_WORK_AGENT_HEARTBEAT_MS = frozenset({'1800000', '900000'})


def _apply_work_agent_runtime_env(agent, env):
    """Rewrite leftover 15m/30m work-agent heartbeats so leased seats start turns."""
    if _is_lean_conversational_agent(agent):
        return
    existing_env = agent.env if isinstance(getattr(agent, 'env', None), dict) else {}
    present = {item.name: idx for idx, item in enumerate(env)}
    desired = {
        'SHIZUHA_IDLE_HEARTBEAT_MS': WORK_AGENT_IDLE_HEARTBEAT_MS,
        'SHIZUHA_HEARTBEAT_DEBOUNCE_MS': WORK_AGENT_HEARTBEAT_DEBOUNCE_MS,
        'SHIZUHA_FIRST_HEARTBEAT_MS': WORK_AGENT_FIRST_HEARTBEAT_MS,
    }
    for key, value in desired.items():
        existing = str((existing_env or {}).get(key) or '')
        if existing in STALE_WORK_AGENT_HEARTBEAT_MS:
            existing = ''
        if existing:
            continue
        var = client.V1EnvVar(name=key, value=value)
        if key in present:
            env[present[key]] = var
        else:
            env.append(var)
            present[key] = len(env) - 1


def heal_stale_work_agent_heartbeat_env(agent) -> bool:
    """Persist the work-agent heartbeat floor onto FleetAgent.env (Hive SoT)."""
    if _is_lean_conversational_agent(agent):
        return False
    raw = agent.env if isinstance(getattr(agent, 'env', None), dict) else {}
    env = dict(raw)
    desired = {
        'SHIZUHA_IDLE_HEARTBEAT_MS': WORK_AGENT_IDLE_HEARTBEAT_MS,
        'SHIZUHA_HEARTBEAT_DEBOUNCE_MS': WORK_AGENT_HEARTBEAT_DEBOUNCE_MS,
        'SHIZUHA_FIRST_HEARTBEAT_MS': WORK_AGENT_FIRST_HEARTBEAT_MS,
    }
    changed = False
    for key, value in desired.items():
        current = str(env.get(key) or '')
        if current in STALE_WORK_AGENT_HEARTBEAT_MS or not current:
            if current != value:
                env[key] = value
                changed = True
    if not changed:
        return False
    agent.env = env
    return True


MANAGED_BY = 'shizuha-hive'

# Pod-local UDS dir shared between the broker sidecar (mcp-auth-proxy) and the
# agent container (PLAT-149 §). The broker serves the minted JWT at
# <dir>/proxy.sock GET /token (SO_PEERCRED-gated to the agent uid).
BROKER_SOCK_DIR = '/run/shizuha/mcp-auth-proxy'
MCP_PROJECTION_TRUST_DIR = '/run/shizuha/mcp-projection'
MCP_PROJECTION_URL = (
    'http://hive-direct.shizuha-hive.svc.cluster.local:8030/'
    'hive/api/v1/fleet/agents/{agent_id}/mcp-projection'
)
MCP_PROJECTION_SIGNER_SECRET = 'hive-capability-evidence-signer'
MCP_PROJECTION_PUBLIC_SECRET = f'{MCP_PROJECTION_SIGNER_SECRET}-public'
BROKER_SOCK_PATH = f'{BROKER_SOCK_DIR}/proxy.sock'
DOCKER_HOST = 'unix:///var/run/docker.sock'

# PLAT-1056: fleet-wide RWX PVC (Longhorn, shared fleet ns) mounted at /shared
# for agents with shared_workspace=True. A shared resource: created once by ops
# (exists live since 2026-06-29), never created/deleted per-agent here.
SHARED_WORKSPACE_PVC_NAME = 'agent-shared-workspace'

# The default shizuha-agent-runtime image's entrypoint (tini + the env-driven
# launcher). The broker start-gate wraps THIS — so it's only applied when the
# FleetAgent uses the default runtime image; a custom `image` keeps its own
# entrypoint (review P2: don't hardcode this onto an arbitrary image).
DEFAULT_AGENT_ENTRYPOINT = ['/usr/bin/tini', '--', '/usr/local/bin/agent-runtime-entrypoint.sh']
# PVC mount. SCLI stores the eternal session at `{cwd}/.shizuha-state.db`.
# Image default cwd is /home/agent (overlay). Hibernate then creates a new
# session and cold-prefills (agent-scout 2026-09-01: 93K → 12K, 0% cache).
AGENT_WORKSPACE_CWD = '/home/agent/.shizuha'


def _agent_launch_command(agent: FleetAgent, *, wait_for_broker: bool) -> list[str] | None:
    """Pin gateway --cwd to the workspace PVC for native runtime images.

    Broker seats already did this inside the /readyz start-gate. Scout-class
    seats without a broker sidecar used the image entrypoint and wrote the
    eternal session onto the overlay, which dies on hibernate.
    """
    if not _uses_native_runtime_image(agent):
        return None
    bridge = _resolve_bridge(agent)
    # Context is document text, including newlines and large skill bodies.
    # The CLI's inline option deliberately accepts only short printable text;
    # use its file transport without weakening that public preflight boundary.
    context_file = (
        'context_prompt_file=; '
        'if [ -n "${CONTEXT_PROMPT:-}" ]; then '
        'context_prompt_file=$(umask 077; mktemp "${TMPDIR:-/tmp}/shizuha-context.XXXXXX") || exit 1; '
        'if ! printf %s "$CONTEXT_PROMPT" > "$context_prompt_file"; then '
        'rm -f "$context_prompt_file"; exit 1; fi; '
        'fi; '
    )
    launch = (
        context_file + f'exec {" ".join(DEFAULT_AGENT_ENTRYPOINT)} {bridge} '
        '--agent-id "$AGENT_ID" --agent-name "${AGENT_NAME:-$AGENT_USERNAME}" '
        '--agent-username "$AGENT_USERNAME" --port "${PORT:-8080}" '
        f'--cwd {AGENT_WORKSPACE_CWD} '
        '${MODEL:+--model "$MODEL"} '
        '${REASONING_EFFORT:+--effort "$REASONING_EFFORT"} '
        '${context_prompt_file:+--context-prompt-file "$context_prompt_file"}'
    )
    if wait_for_broker:
        readyz_check = f'curl -fsS --unix-socket {BROKER_SOCK_PATH} http://localhost/readyz'
        return [
            'sh', '-c',
            f'until {readyz_check} >/dev/null 2>&1; do echo waiting-for-broker-readyz; sleep 1; done; '
            f'{launch}',
        ]
    return ['sh', '-c', launch]

DEFAULT_RESOURCE_PROFILES = {
    'small': {'requests': {'cpu': '100m', 'memory': '256Mi'}, 'limits': {'cpu': '1', 'memory': '1Gi'}},
    'standard': {'requests': {'cpu': '250m', 'memory': '512Mi'}, 'limits': {'cpu': '2', 'memory': '6Gi'}},
    'heavy': {'requests': {'cpu': '500m', 'memory': '1Gi'}, 'limits': {'cpu': '4', 'memory': '8Gi'}},
}


def _coordinator_model_token_enabled() -> bool:
    """Whether fleet brokers should serve HIVE-146 /model-token from Coordinator."""
    return bool(
        getattr(settings, 'FLEET_COORDINATOR_MODEL_TOKEN_URL', '')
        and (getattr(settings, 'FLEET_COORDINATOR_OAUTH_SECRET_NAME', '')
             or (getattr(settings, 'FLEET_COORDINATOR_TOKEN_SECRET_NAME', '')
                 and getattr(settings, 'FLEET_COORDINATOR_TOKEN_SECRET_KEY', 'token')))
    )


CORTEX_KEY_FINGERPRINT_ANNOTATION = 'shizuha.io/cortex-key-fingerprint'
FORGEJO_TOKEN_FINGERPRINT_ANNOTATION = 'shizuha.io/forgejo-token-fingerprint'

# Keep in sync with reconcile_agent_github_identity.WRITE_TEAMS. Token *scope*
# is not repo permission: org teams still gate push/merge. Review/security/QA
# seats need write:repository to post formal Origin reviews (PLS-595) while
# remaining on agents-readers (push=false).
_FORGEJO_WRITE_TEAMS = frozenset({
    'merge', 'devops', 'platform-ops', 'engineering',
    'securities-engineering', 'trading-engineering',
})
_FORGEJO_REVIEW_TEAMS = frozenset({'review', 'security', 'qa'})
_FORGEJO_IDENTITY_ALIASES = {
    # Never project the Origin admin login into a fleet pod.
    'shizuha': 'shizuha-agent',
}
_FORGEJO_WRITE_SCOPES = (
    'write:repository', 'write:issue', 'read:user', 'read:organization',
)
_FORGEJO_READ_SCOPES = (
    'read:repository', 'read:issue', 'read:user', 'read:organization',
)
_FORGEJO_ADMIN_CREDS_SECRET = 'forgejo-admin-creds'
_FORGEJO_ADMIN_CREDS_NS = 'origin'

# HIVE-1922: one namespace-projected Secret contract for the runtime signer
# and the private browser-worker verifier. Kubernetes Secret refs cannot cross
# namespaces, so Hive copies this exact key into both namespaces and renders
# both consumers against the copy.
BROWSER_MCP_JWT_SECRET_NAME = 'shizuha-browser-jwt'
BROWSER_MCP_JWT_SECRET_KEY = 'jwt-secret'
BROWSER_MCP_JWT_SOURCE_SECRET_NAME = 'shizuha-secrets'


def get_k8s_client():
    """Get k8s API clients. Uses in-cluster config when running in k8s."""
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return client.CoreV1Api(), client.AppsV1Api(), client.NetworkingV1Api()


def fleet_namespace() -> str:
    return getattr(settings, 'SHIZUHA_FLEET_NAMESPACE', 'shizuha-fleet')


def agent_namespace(agent: FleetAgent) -> str:
    return agent.resolve_namespace(fleet_namespace())


def _live_ns(agent: FleetAgent) -> str:
    """Namespace for lifecycle ops on an ALREADY-provisioned agent (review #3).

    Use the ns persisted at provision time, so a later change to
    ``SHIZUHA_FLEET_NAMESPACE`` or ``isolated`` cannot make stop/deprovision/status
    target a different ns and orphan the live resources. Falls back to the
    resolver only if nothing was persisted yet.
    """
    return agent.namespace or agent_namespace(agent)


_NATIVE_SESSION_RESET_PY = r'''
import json, os, sqlite3
from datetime import datetime, timezone
sid = os.environ["SESSION_ID"]
dbs = [
    "/home/agent/.shizuha/.shizuha-state.db",
    "/home/agent/.shizuha/.codex-state.db",
]
tables = [
    ("messages_fts", "session_id"),
    ("messages", "session_id"),
    ("session_message_transcript", "session_id"),
    ("session_interrupt_checkpoints", "session_id"),
    ("session_wire_prefix", "session_id"),
    ("session_provider_prefix_snapshots", "session_id"),
    ("session_provider_prefix_heads", "session_id"),
    ("session_context_token_anchors", "session_id"),
    ("session_inbound_processing", "session_id"),
    ("session_recovery_heads", "session_id"),
    ("session_recovery_deferred", "session_id"),
    ("sessions", "id"),
]
arch_dir = "/home/agent/.shizuha/archived-sessions"
os.makedirs(arch_dir, exist_ok=True)
ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
report = {"session_id": sid, "archived": [], "deleted": {}}
for db in dbs:
    if not os.path.exists(db):
        continue
    arch = os.path.join(arch_dir, f"{os.path.basename(db)}-{sid}-{ts}.db")
    try:
        src = sqlite3.connect(db)
        src.execute("VACUUM INTO ?", (arch,))
        src.close()
        report["archived"].append(arch)
    except Exception as exc:
        report.setdefault("archive_errors", []).append(f"{db}: {exc}")
    con = sqlite3.connect(db)
    for table, col in tables:
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
            (table,),
        ).fetchone()
        if not exists:
            continue
        cur = con.execute(f"DELETE FROM {table} WHERE {col}=?", (sid,))
        report["deleted"][f"{os.path.basename(db)}:{table}"] = cur.rowcount
    con.commit()
    con.close()
claude = "/home/agent/.shizuha/.claude-session-id"
if os.path.exists(claude):
    os.remove(claude)
    report["removed_claude_session_id"] = True
rotated_logs = []
for name in (".telemetry.jsonl", ".audit-log.jsonl"):
    src = os.path.join("/home/agent/.shizuha", name)
    if os.path.exists(src) and os.path.getsize(src) > 0:
        dest = os.path.join(arch_dir, f"{name}-{sid}-{ts}")
        os.replace(src, dest)
        open(src, "a").close()
        rotated_logs.append(dest)
if rotated_logs:
    report["rotated_logs"] = rotated_logs
print(json.dumps(report))
'''


def reset_k8s_native_runtime_session(agent: FleetAgent) -> dict:
    """Wipe the PVC session sqlite then SIGKILL-recycle the native pod.

    Same contract as SCLI ``resetK8sAgentRuntimeSession``: a graceful stop can
    flush the poisoned transcript back onto the PVC, so the pod must die
    immediately after the DELETE. Session id is ``agent-session-<runtime uuid>``.
    """
    runtime_id = (agent.agent_id or '').strip()
    username = (agent.agent_username or '').strip().lower()
    if not runtime_id or not username:
        raise ValueError('native session reset requires agent_id and agent_username')
    session_id = f'agent-session-{runtime_id}'
    core_v1, _, _ = get_k8s_client()
    ns = _live_ns(agent)
    pods = []
    for selector in (
        f'shizuha.io/agent={username}',
        f'app=agent-{username}',
    ):
        pods = core_v1.list_namespaced_pod(ns, label_selector=selector).items
        if pods:
            break
    ready = [
        p for p in pods
        if p.status and (p.status.phase or '') == 'Running'
    ]
    if not ready:
        raise RuntimeError(f'no Running native pod for {username} in {ns}')
    pod_name = ready[0].metadata.name
    output = stream(
        core_v1.connect_get_namespaced_pod_exec,
        pod_name,
        ns,
        container='agent',
        command=['env', f'SESSION_ID={session_id}', 'python3', '-c', _NATIVE_SESSION_RESET_PY],
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
    )
    report: dict = {}
    try:
        line = str(output).strip().splitlines()[-1]
        report = json.loads(line)
    except Exception:
        report = {'raw': str(output)[:500]}
    body = client.V1DeleteOptions(grace_period_seconds=0, propagation_policy='Background')
    for pod in pods:
        try:
            core_v1.delete_namespaced_pod(
                pod.metadata.name, ns, body=body, grace_period_seconds=0,
            )
        except ApiException as exc:
            if exc.status != 404:
                raise
    _log_event(
        agent, 'session_reset',
        f'Cleared {session_id} on {pod_name} and SIGKILL-recycled native pod',
    )
    return {
        'status': 'session_reset',
        'plane': 'k8s-native-pvc',
        'agent_id': str(agent.id),
        'session_id': session_id,
        'pod': pod_name,
        'report': report,
    }


def _sandbox_substitute_configured() -> bool:
    """Whether a native code-exec sandbox substitute is configured (HLD R1 / PLAT-153).

    The native pod has no inner-docker exec; until a gVisor/Kata runtimeClass or a
    shared sandbox service is wired up, ``sandbox_required`` agents cannot run.
    """
    return bool(
        getattr(settings, 'FLEET_SANDBOX_RUNTIME_CLASS', '')
        or getattr(settings, 'FLEET_SANDBOX_SERVICE', '')
    )


# ── Platform identity helpers (HIVE-71) ────────────────────────────────────

def _generate_agent_password() -> str:
    # 32 URL-safe bytes (~43 chars) + forced mixed-case/digit/symbol suffix so
    # shizuha-id's password validator always passes (mirrors agent-accounts.ts).
    return secrets.token_urlsafe(32) + '!A1'


def _fleet_provisioner_token() -> str:
    """Return the scoped shizuha-id fleet provisioner token."""
    return getattr(settings, 'HIVE_ID_FLEET_PROVISIONER_TOKEN', '') or ''


@dataclass(frozen=True)
class FleetIdentityBinding:
    """Exact immutable ID rows returned by the scoped provisioner."""

    user_id: int
    profile_id: int
    owner_user_id: int | None
    owner_profile_id: int | None
    ownership_class: str


def _provision_platform_identity_details(
    agent: 'FleetAgent', password: str, platform_url: str,
) -> FleetIdentityBinding | None:
    """Create/reconnect an agent identity through shizuha-id's scoped fleet API.

    HIVE-75 replaces the broad HIVE_ADMIN_USERNAME/PASSWORD flow with a
    least-privilege service token. The id-side endpoint is allowed to manage
    only @agents.shizuha.io accounts with account_type=agent; it cannot approve
    or reset human accounts.
    """
    token = _fleet_provisioner_token()
    if not token:
        logger.error('HIVE-75: HIVE_ID_FLEET_PROVISIONER_TOKEN not configured; refusing broad-admin fallback')
        return None

    # HIVE-628 follow-up: /id/api/internal/* is NOT routable through the nginx
    # front (ingress-guard, 2026-07-06 triple-outage fix) — every provision got
    # a nginx 404 and new agents failed identity provisioning. Call shizuha-id's
    # in-cluster service directly, like the fleet daemon does (SHIZUHA_ID_INTERNAL_URL).
    id_base = settings.SHIZUHA_ID_INTERNAL_URL.rstrip('/')
    owner_id = getattr(agent, 'owner_id', None)
    email_domain = 'agents.shizuha.io' if owner_id is not None else 'shizuha.com'
    email = f'{agent.agent_username}@{email_domain}'
    display = agent.display_name or agent.agent_username

    # `owner_user_id` declares "this is the PERSONAL-DEFAULT identity for that
    # human", and shizuha-id enforces the matching contract: username and email
    # must be the canonical owner-derived `shizuha-<owner_id>` pair. Hive sent it
    # for every agent that merely HAS an owner, so an ordinary fleet agent's
    # identity re-provision always came back
    #
    #   400 Personal-default identity must use its canonical owner-derived
    #       username/email
    #
    # which turned any PLAT-4884 credential drift into a permanent DRIFT_HOLD —
    # the agent could never be enabled again. rei sat unstartable that way, and
    # 42 of 58 agents were one drift away from the same fate.
    #
    # The personal-default concept itself is retired (operator 2026-08-05: "this
    # is a garbage concept .. all agents should be equivalent"), so Hive never
    # claims it. Every agent provisions its identity the same way.
    payload = {
        'username': agent.agent_username,
        'email': email,
        'password': password,
        'display_name': display,
        'runtime_id': getattr(agent, 'agent_id', '') or '',
    }

    try:
        resp = requests.post(
            f'{id_base}/api/internal/fleet/provision-agent/',
            json=payload,
            headers={'X-Fleet-Provisioner-Token': token},
            timeout=15,
        )
        if resp.ok:
            data = resp.json() or {}
            logger.info(
                'HIVE-75: %s platform identity for agent %s (user_id=%s)',
                data.get('action', 'provisioned'), agent.agent_username, data.get('user_id')
            )
            try:
                binding = FleetIdentityBinding(
                    user_id=int(data['user_id']),
                    profile_id=int(data['profile_id']),
                    owner_user_id=(
                        int(data['owner_user_id']) if data.get('owner_user_id') is not None else None
                    ),
                    owner_profile_id=(
                        int(data['owner_profile_id']) if data.get('owner_profile_id') is not None else None
                    ),
                    ownership_class=str(data.get('ownership_class') or ''),
                )
            except (KeyError, TypeError, ValueError):
                logger.error(
                    'HIVE-620: ID provision response omitted immutable profile binding for %s',
                    agent.agent_username,
                )
                return None
            # No personal-default binding is ever requested (see above), so
            # there is nothing to validate here. This check used to fire for any
            # agent with an owner_id: even after the REQUEST stopped
            # over-claiming, rei's successful reconnect (user_id=106) was
            # rejected here and fell straight back into DRIFT_HOLD. Two guards,
            # one wrong assumption — removing only the first fixed nothing.
            return binding
        detail = ''
        try:
            detail = str(resp.json())
        except Exception:
            detail = resp.text[:500]
        logger.error(
            'HIVE-75: scoped fleet provision failed for %s (%d): %s',
            agent.agent_username, resp.status_code, detail,
        )
        return None
    except Exception as exc:
        logger.error('HIVE-75: scoped fleet provision error for %s: %s', agent.agent_username, exc)
        return None


def _provision_platform_identity(agent: 'FleetAgent', password: str, platform_url: str) -> bool:
    """Compatibility wrapper for legacy fleet provisioning call sites."""
    binding = _provision_platform_identity_details(agent, password, platform_url)
    if binding is None:
        return False
    persist_identity_binding(agent, binding)
    return True


def persist_identity_binding(agent: 'FleetAgent', binding: FleetIdentityBinding) -> bool:
    """Write the ID user/profile binding without clobbering an existing id."""
    from django.utils import timezone

    if not binding or not getattr(binding, 'user_id', None):
        return False
    existing = getattr(agent, 'identity_user_id', None)
    if existing is not None and int(existing) != int(binding.user_id):
        logger.warning(
            'identity binding for %s already set to %s; refusing overwrite with %s',
            agent.agent_username, existing, binding.user_id,
        )
        return False
    agent.identity_user_id = int(binding.user_id)
    agent.identity_profile_id = int(binding.profile_id)
    if binding.owner_profile_id is not None:
        agent.owner_profile_id = int(binding.owner_profile_id)
    # UUIDField defaults assign a pk on construction; unsaved rows still
    # report `_state.adding` (SimpleTestCase identity tests use those).
    if getattr(getattr(agent, '_state', None), 'adding', True):
        return True
    now = timezone.now()
    update = {
        'identity_user_id': int(binding.user_id),
        'identity_profile_id': int(binding.profile_id),
        'identity_provisioned_at': now,
    }
    if binding.owner_profile_id is not None:
        update['owner_profile_id'] = int(binding.owner_profile_id)
    FleetAgent.objects.filter(pk=agent.pk).update(**update)
    agent.refresh_from_db()
    return True


def bind_existing_identity_user(agent: 'FleetAgent') -> bool:
    """Persist identity_user_id when Shizuha-ID already has the agent user."""
    if getattr(agent, 'identity_user_id', None):
        return True
    try:
        from .personal_agents import resolve_agent_user_id
        existing = resolve_agent_user_id(agent)
    except Exception:
        existing = None
    if not existing:
        return False
    if getattr(getattr(agent, '_state', None), 'adding', True):
        agent.identity_user_id = int(existing)
        return True
    FleetAgent.objects.filter(pk=agent.pk, identity_user_id__isnull=True).update(
        identity_user_id=int(existing),
    )
    agent.refresh_from_db()
    return bool(getattr(agent, 'identity_user_id', None))


def ensure_fleet_identity(agent: 'FleetAgent', password: str = '') -> bool:
    """Create or reconnect the Shizuha-ID agent user without needing k8s.

    Home search and Connect DMs resolve people through Shizuha-ID. A pending
    FleetAgent whose reviewed runtime image cannot bind (rt-fleet stale) used
    to stay invisible on shizuha.com. Identity is independent of that gate.
    """
    if getattr(agent, 'identity_user_id', None):
        return True
    try:
        from .personal_agents import resolve_agent_user_id
        existing = resolve_agent_user_id(agent)
    except Exception:
        existing = None
    if existing:
        FleetAgent.objects.filter(pk=agent.pk, identity_user_id__isnull=True).update(
            identity_user_id=int(existing),
        )
        agent.refresh_from_db()
        if agent.identity_user_id:
            return True
    platform_url = getattr(settings, 'SHIZUHA_PLATFORM_URL', '') or ''
    secret = password or _generate_agent_password()
    binding = _provision_platform_identity_details(agent, secret, platform_url)
    if binding is None:
        return False
    persist_identity_binding(agent, binding)
    _log_event(agent, 'identity_provisioned', 'Shizuha-ID agent user bound (runtime-independent)')
    return bool(getattr(agent, 'identity_user_id', None))


def enable_or_provision_agent(agent: 'FleetAgent') -> bool:
    """Enable a fleet agent. Never-provisioned rows get a first-time provision.

    Identity is ensured first so the agent is searchable even if runtime
    actuation cannot bind a reviewed image.

    Default personal agents are parked ``hibernated`` with ``provisioned_at``
    unset (no Deployment yet). First Connect-wake / Hive enable must provision,
    not scale a missing Deployment.
    """
    if not getattr(agent, 'identity_user_id', None):
        ensure_fleet_identity(agent)
        try:
            agent.refresh_from_db()
        except Exception:
            pass
    if not (getattr(agent, 'agent_id', None) or '').strip():
        FleetAgent.objects.filter(pk=agent.pk).filter(agent_id='').update(
            agent_id=str(uuid.uuid4()),
        )
        try:
            agent.refresh_from_db()
        except Exception:
            pass
    ok = provision_agent(agent) if agent.provisioned_at is None else start_agent(agent)
    if not ok and lease_start_pending_reason(agent) and agent.status == FleetAgent.Status.STOPPED:
        # This entrypoint is explicit Enable intent (API/UI), unlike automatic
        # start_agent callers. Arm a sleeping waiter for the ordinary planner;
        # do not start it or clear the operator stop on unknown/positive state.
        try:
            _, apps_v1, _ = get_k8s_client()
            cold = _cold_deployment_state(apps_v1, _live_ns(agent), agent.deployment_name)
            cold_browser = (
                _cold_deployment_state(apps_v1, browser_worker_namespace(), _browser_worker_name(agent))
                if agent_needs_browser_mcp(agent) else None
            )
        except Exception:
            _record_lease_start_refusal(agent, LeaseStartDecision(False, 'deployment_state_unreadable'))
            return False
        if cold.can_stage and (cold_browser is None or cold_browser.can_stage):
            agent.status = FleetAgent.Status.HIBERNATED
            agent.hibernation_reason = 'explicit enable awaiting model lease grant'
            agent.hibernated_at = timezone.now()
            agent.runtime_lifecycle_state = 'hibernated'
            agent.save(update_fields=[
                'status', 'hibernation_reason', 'hibernated_at', 'runtime_lifecycle_state',
            ])
    return ok


def _id_admin_token(platform_url: str) -> str:
    """Mint an admin bearer token for non-critical onboarding automation.

    Identity provisioning itself uses the scoped fleet token above. This legacy
    admin-login path is only for PLAT-397's optional Pulse onboarding task, so it
    returns an empty token on any configuration/API failure and lets the caller
    skip onboarding without affecting k8s provisioning.
    """
    username = getattr(settings, 'HIVE_ADMIN_USERNAME', '') or ''
    password = getattr(settings, 'HIVE_ADMIN_PASSWORD', '') or ''
    if not (platform_url and username and password):
        return ''
    try:
        resp = requests.post(
            f'{platform_url.rstrip("/")}/id/api/auth/login/',
            json={'email': username, 'password': password},
            timeout=15,
        )
        if not resp.ok:
            logger.warning('PLAT-397: admin login failed for onboarding (%d)', resp.status_code)
            return ''
        data = resp.json() or {}
        tokens = data.get('tokens') or data
        return tokens.get('access') or ''
    except Exception as exc:  # noqa: BLE001
        logger.warning('PLAT-397: admin login error for onboarding: %s', exc)
        return ''


# ── Onboarding (PLAT-397) ──────────────────────────────────────────────────

def _trigger_onboarding(agent: 'FleetAgent') -> None:
    """Create a Pulse onboarding task for a newly provisioned fleet agent.

    Non-blocking: logs on any failure but never raises — a Pulse API problem
    must not roll back the k8s provisioning that already succeeded.

    Called in a daemon thread from provision_agent() so the provisioning HTTP
    response is not delayed by the Pulse API round-trip.
    """
    platform_url = getattr(settings, 'SHIZUHA_PLATFORM_URL', '').rstrip('/')
    if not platform_url:
        logger.warning('PLAT-397: SHIZUHA_PLATFORM_URL not set; skipping onboarding for %s', agent.agent_username)
        return

    if not getattr(settings, 'HIVE_PULSE_ONBOARDING_ENABLED', True):
        return

    try:
        admin_token = _id_admin_token(platform_url)
        if not admin_token:
            logger.warning('PLAT-397: no admin token; skipping onboarding task for %s', agent.agent_username)
            return

        agent_email = f'{agent.agent_username}@shizuha.com'
        role_note = f' ({agent.role})' if agent.role else ''
        display = agent.display_name or agent.agent_username

        payload = {
            'title': f'[Onboarding] {display} — complete your Shizuha Connect profile',
            'description': (
                f'Welcome to the Shizuha platform, {display}{role_note}!\n\n'
                'Please complete your Connect profile so the team and users can discover your capabilities:\n\n'
                '1. Set your headline (one-line summary of what you do)\n'
                '2. Write a short bio (your role, capabilities, typical tasks)\n'
                '3. Add your skills (e.g. coding, security-analysis, data-analysis)\n'
                '4. Create your first post — introduce yourself to the community\n\n'
                'Use the `connect_update_my_profile`, `connect_list_skills`, and '
                '`connect_create_post` tools available via your MCP server.'
            ),
            'assignee_email': agent_email,
            'direct_assignment_reason': 'Automated onboarding task for newly provisioned fleet agent (PLAT-397)',
            'priority': 'normal',
            'workflow_name': 'simple',
        }

        resp = requests.post(
            f'{platform_url}/pulse/api/tasks/',
            json=payload,
            headers={'Authorization': f'Bearer {admin_token}'},
            timeout=15,
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            task_key = data.get('item_key') or data.get('id', '?')
            logger.info('PLAT-397: onboarding task %s created for %s', task_key, agent.agent_username)
            _log_event(agent, 'onboarding_triggered', f'Pulse onboarding task {task_key} created for {agent_email}')
        else:
            logger.warning(
                'PLAT-397: failed to create onboarding task for %s (%d: %s)',
                agent.agent_username, resp.status_code, resp.text[:200],
            )

    except Exception as exc:  # noqa: BLE001
        logger.warning('PLAT-397: onboarding trigger error for %s: %s', agent.agent_username, exc)


def _trigger_onboarding_async(agent: 'FleetAgent') -> None:
    """Dispatch _trigger_onboarding in a daemon thread to avoid blocking the caller."""
    t = threading.Thread(target=_trigger_onboarding, args=(agent,), daemon=True, name=f'onboard-{agent.agent_username}')
    t.start()


# ── Lifecycle ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class LeaseStartDecision:
    allowed: bool
    reason: str

    @property
    def pending(self) -> bool:
        return not self.allowed and self.reason in {
            'lease_pool_full', 'kv_residency_full', 'await_reconcile_grant',
            # HIVE-2161 S3-lite: an active BYO reference holds the wake until
            # the TokenPool lease slice lands — a waiting state, not a fault.
            'lease_wiring_pending',
        }


def _lease_start_decision(agent: FleetAgent) -> LeaseStartDecision:
    """Read start authority without granting or renewing a model lease."""
    try:
        from .model_lease import lease_wake_available
        # HIVE-2161 S3-lite (gate decision 2026-09-10, fail-closed): consult
        # the org BYO resolution BEFORE any lease authority. An active BYO
        # reference holds the wake (lease_wiring_pending) — starting now would
        # run the customer org's agent on Shizuha-owned keys, the fallback
        # HLD Router Core v18 §5 forbids. An unresolved/ambiguous identity
        # holds LOUD (identity_unresolved). Non-BYO agents resolve
        # no_provider_configured and proceed byte-for-byte unchanged.
        from . import provider_resolution as _pr
        try:
            resolution = _pr.resolve_agent_org_provider(
                agent, runtime_class=_resolve_bridge(agent),
            )
        except _pr.IdentityUnresolved:
            decision = LeaseStartDecision(False, _pr.REASON_IDENTITY_UNRESOLVED)
            agent._lease_start_decision = decision
            return decision
        except _pr.InvariantViolation:
            # N3 (kumo review 2026-09-12): preserve the typed reason — a
            # cross-org/ambiguous reference is fail-closed with
            # invariant_violation, not an untyped gate_error.
            decision = LeaseStartDecision(False, _pr.REASON_INVARIANT_VIOLATION)
            agent._lease_start_decision = decision
            return decision
        # HIVE-2161 F1 (kumo review 2026-09-12, blocking): hold the wake for
        # EVERY typed unavailable reason except no_provider_configured (the
        # only signal that means genuinely non-BYO). Lifecycle states
        # (verifying/probation/invalid/exhausted/disabled/mismatch) and
        # platform holds must never silently start the agent on
        # Shizuha-owned keys — the ruled hazard. Fault-class reasons
        # (key_invalid, provider_quota_exhausted, provider_disabled,
        # invariant_violation) are operator attention, not `pending`.
        if resolution.unavailable and resolution.reason != _pr.REASON_NO_PROVIDER_CONFIGURED:
            decision = LeaseStartDecision(False, resolution.reason)
            agent._lease_start_decision = decision
            return decision
        # Use the launcher's default/remapping, not only the optional row
        # value. Otherwise a blank model can launch a governed default without
        # ever consulting its board. Native gateway defaults cannot establish
        # provider authority when neither source resolves a model; external
        # bridges retain their own established provider/model defaults.
        model = _resolve_model(agent)
        if resolution.ok and resolution.model_id:
            model = resolution.model_id
        if not model and _resolve_bridge(agent) == 'gateway':
            decision = LeaseStartDecision(False, 'model_unresolved')
        else:
            allowed, reason = lease_wake_available(agent={
                'agent_username': agent.agent_username,
                'model': model, 'effective_model': model,
            })
            decision = LeaseStartDecision(allowed, reason)
    except Exception:  # noqa: BLE001 — unknown authority never permits a start
        logger.exception('Lease authority unavailable for %s', agent.agent_username)
        decision = LeaseStartDecision(False, 'gate_error_fail_closed')
    agent._lease_start_decision = decision
    return decision


def lease_start_pending_reason(agent: FleetAgent) -> str | None:
    """Typed result of this start attempt; persisted prose is not a status API."""
    decision = getattr(agent, '_lease_start_decision', None)
    return decision.reason if isinstance(decision, LeaseStartDecision) and decision.pending else None


def _record_lease_start_refusal(agent: FleetAgent, decision: LeaseStartDecision) -> None:
    agent._lease_start_decision = decision
    agent.status_message = (
        f'HIVE-1713: start deferred — no confirmed model lease ({decision.reason}); '
        'the ordinary lease planner grants before runtime start'
    )
    agent.save(update_fields=['status_message'])
    _log_event(agent, 'lease_waiting' if decision.pending else 'error', agent.status_message)


@dataclass(frozen=True)
class _ColdDeploymentState:
    can_stage: bool
    resource_version: str | None = None


def _cold_deployment_state(apps_v1, ns: str, name: str) -> _ColdDeploymentState:
    """Confirm absence/zero replicas and retain the version for a fenced write."""
    try:
        existing = apps_v1.read_namespaced_deployment(name, ns)
    except ApiException as exc:
        if exc.status == 404:
            return _ColdDeploymentState(True)
        raise
    replicas = getattr(existing.spec, 'replicas', None)
    version = getattr(existing.metadata, 'resource_version', None)
    if not isinstance(replicas, int) or replicas < 0 or not isinstance(version, str) or not version:
        raise ValueError('Deployment state is unreadable')
    return _ColdDeploymentState(replicas == 0, version)


def _daemon_routing_guard(agent: 'FleetAgent') -> None:
    """HIVE-253 P4: visibility guard — logs when a registered k3s daemon should be
    the provisioning route instead of direct k8s API (P3 control-channel, deferred).
    Does NOT block provisioning; enables P3 flip once the control-channel is built."""
    try:
        daemon = DaemonRegistry.objects.filter(
            runtime=DaemonRegistry.RuntimeType.K3S,
            status__in=(DaemonRegistry.Status.CONNECTED, DaemonRegistry.Status.DEGRADED),
        ).first()
        if daemon:
            logger.info(
                'HIVE-253: direct k8s provision for %s while k3s daemon %s is registered '
                '(status=%s). New provisions should route via daemon control-channel once P3 '
                '(HIVE-244) is implemented.',
                agent.agent_username, daemon.daemon_id, daemon.status,
            )
    except Exception:
        pass  # Never block provisioning on a guard failure


_STALE_PERSONAL_ISOLATION_MARKERS = (
    'refusing to actuate a personal agent into the shared fleet namespace',
    'personal agent must run in its own isolated namespace',
)


def _cross_tenant_daemon_placement(agent: 'FleetAgent') -> bool:
    """True only when this agent would be placed onto another owner's daemon."""
    d = getattr(agent, 'managing_daemon', None)
    daemon_owner = getattr(d, 'owner_subject', None) if d is not None else None
    if daemon_owner is None:
        return False
    agent_owner = getattr(agent, 'owner_id', None)
    if agent_owner is None or str(daemon_owner) == str(agent_owner):
        return False
    return True


def _personal_runtime_isolation_gate(agent: 'FleetAgent') -> bool:
    """Refuse only genuine cross-tenant daemon placement.

    Operator 2026-09-01: there is no separate "org agent" vs "personal agent"
    class. Agents belong to people and may work in orgs those people belong
    to. Shared fleet (``DaemonRegistry.owner_subject`` NULL) is the org
    workspace. Isolated per-owner namespaces remain allowed. The only
    fail-closed case is actuating owner A's agent onto owner B's dedicated
    daemon.
    """
    if not _cross_tenant_daemon_placement(agent):
        return False
    agent.status = FleetAgent.Status.BLOCKED
    agent.status_message = (
        'Agent belongs to a different owner than this fleet daemon; '
        'refusing to actuate into another owner\'s fleet.'
    )
    agent.save(update_fields=['status', 'status_message'])
    _log_event(agent, 'blocked', 'cross-tenant daemon placement refused')
    return True


def clear_stale_personal_isolation_blocks() -> int:
    """Unblock seats left BLOCKED by the retired org-vs-personal isolation gate.

    3bfb067 rewrote the gate but did not migrate existing rows. 23 GLM org
    seats stayed BLOCKED with the old message and rendered as Stopped/Disabled
    on the Agents page (2026-09-01). The current gate would pass them into
    the shared fleet. Restore HIBERNATED so lease/on-demand can wake them;
    never STOP.
    """
    from .models import FleetAgent

    n = 0
    qs = FleetAgent.objects.filter(status=FleetAgent.Status.BLOCKED)
    for agent in qs.iterator():
        msg = (agent.status_message or '').lower()
        if not any(marker in msg for marker in _STALE_PERSONAL_ISOLATION_MARKERS):
            continue
        if _cross_tenant_daemon_placement(agent):
            continue
        agent.status = FleetAgent.Status.HIBERNATED
        agent.status_message = ''
        if not (agent.hibernation_reason or '').strip():
            agent.hibernation_reason = 'does not hold the model lease'
        agent.save(update_fields=['status', 'status_message', 'hibernation_reason'])
        _log_event(agent, 'hibernated', 'cleared stale personal-isolation block')
        n += 1
    return n





def provision_agent(agent: FleetAgent, agent_password: str = '', user_token: str = '', *, _preserve_native_intent: bool = False) -> bool:
    """
    Provision a full k3s-native agent: namespace (shared or isolated), per-agent
    identity Secret, Longhorn workspace PVC, Deployment (Recreate),
    and (only if ``expose_http`` or a grok-voice S2S seat) a Service.

    A governed agent awaiting a planner grant is provisioned cold (replicas=0)
    with its existing onboarding demand. ``provisioned_at`` records completed
    resources; the return value is True only when the runtime was started.

    ``agent_password`` is written ONLY into the identity Secret (mounted into the
    broker sidecar, never the agent container).

    HIVE-71: if ``agent_password`` is empty, one is auto-generated and the agent
    account is registered + approved on shizuha-id using the scoped fleet
    provisioner token in settings.HIVE_ID_FLEET_PROVISIONER_TOKEN. Callers may still supply a
    known password (e.g. re-provisioning an existing account) to skip auto-generate.

    R1 gate (review #1): a ``sandbox_required`` agent uses inner-docker exec, which
    the native pod deliberately does NOT provide. Refuse and mark it ``blocked``
    until the substitute (gVisor/Kata runtimeClass or shared sandbox service,
    HLD §11 R1 / PLAT-153) is configured — provisioning anyway would silently cut
    the agent over to a runtime missing its required sandbox capability.
    """
    agent._lease_start_decision = None
    original_status = agent.status
    first_provision = agent.provisioned_at is None
    # HIVE-644 fail-closed rollout gate. A personal object/identity may exist,
    # but it cannot actuate through the legacy shared/DevOps fleet provisioner.
    # The later HLD §15 runtime slice replaces this with the owner-namespace,
    # gVisor, admission, ServiceAccount and NetworkPolicy materializer.
    if _personal_runtime_isolation_gate(agent):
        return False

    if agent.sandbox_required and not _sandbox_substitute_configured():
        agent.status = FleetAgent.Status.BLOCKED
        agent.status_message = (
            'sandbox_required but no native sandbox substitute configured '
            '(HLD §11 R1 / PLAT-153)'
        )
        agent.save(update_fields=['status', 'status_message'])
        _log_event(agent, 'blocked', 'Sandbox-required agent gated on R1 substitute; not provisioned')
        return False

    # HIVE-1502: AGENT_ID is a REQUIRED input, not an optional one. The runtime
    # entrypoint guards it with `${AGENT_ID:?AGENT_ID required}`, which fires on
    # empty as well as unset, so an agent with no `agent_id` yields a pod that can
    # never start.
    #
    # Both render sites previously coerced it (`agent.agent_id or ''`), which made
    # a missing required input into a syntactically VALID Deployment: provisioning
    # reported success, `kubectl apply` succeeded, the ReplicaSet scaled, and the
    # only symptom was a crash loop nothing in the roll path inspects. The
    # shizuha-116 seat sat unstartable for 4 days across 10 ReplicaSet revisions
    # and 9 image rolls, having never once produced a Ready agent container.
    #
    # `FleetAgent.agent_id` is `blank=True` with no default and nothing outside
    # tests ever populates it after creation, so an id absent at creation is absent
    # permanently. Refuse at the provisioning boundary — the point where the
    # requirement actually exists — rather than at creation, because a non-
    # provisioned/external agent legitimately has no container to carry the env.
    if not (agent.agent_id or '').strip():
        agent.status = FleetAgent.Status.BLOCKED
        agent.status_message = (
            'agent_id is empty; the runtime entrypoint requires a non-empty '
            'AGENT_ID and would CrashLoopBackOff. Set FleetAgent.agent_id via '
            'Hive (the config source of truth) and re-provision (HIVE-1502).'
        )
        agent.save(update_fields=['status', 'status_message'])
        _log_event(
            agent, 'blocked',
            'Refused to provision: agent_id is empty; rendering would produce an '
            'unstartable pod (HIVE-1502)',
        )
        return False

    # Bind an ALREADY-EXISTING Shizuha-ID user before the reviewed-image gate.
    # Do not mint a new password here — that would rotate the ID password out
    # of band of the identity Secret reuse path (PLAT-673).
    if not getattr(agent, 'identity_user_id', None):
        try:
            bind_existing_identity_user(agent)
        except Exception:
            logger.exception('existing-identity bind failed for %s; continuing', agent.agent_username)

    # Bind reviewed runtime intent after all non-provisionable input gates and
    # before the first cluster/credential mutation. Every real create path now
    # carries this validated projection explicitly into pure render helpers.
    _bind_reviewed_runtime_image(agent, preserve_native_intent=_preserve_native_intent)
    from .native_lifecycle_projection import retire_before_start
    retire_before_start(agent)

    # HIVE-253 P4 daemon-routing guard: if a registered k3s fleet daemon exists,
    # new provisioning should ideally route through the daemon's control channel
    # (P3/§5.1). Since P3 is deferred, we log a visibility warning and continue
    # with the direct k8s path. This ensures the routing intent is surfaced in logs.
    _daemon_routing_guard(agent)

    auto_provision_identity = False
    platform_url = ''

    # HIVE-71/HIVE-75: auto-provision platform identity when no password is
    # supplied. PLAT-673 keeps the k8s Secret and shizuha-id password in lockstep:
    # for an existing identity Secret, reuse its AGENT_PASSWORD for id-side
    # self-heal instead of rotating the Secret first; for a first-time/no-password
    # Secret, generate once, write the Secret first, then set shizuha-id. If that
    # id-side write fails, restore the previous Secret (or delete the new one).
    if not agent_password and not user_token:
        platform_url = getattr(settings, 'SHIZUHA_PLATFORM_URL', '').rstrip('/')
        if platform_url:
            auto_provision_identity = True
        else:
            logger.warning(
                'HIVE-71: SHIZUHA_PLATFORM_URL not set; provisioning without AGENT_PASSWORD '
                '(broker will not be able to mint JWTs — set agent_password explicitly or configure platform URL)'
            )

    core_v1, apps_v1, networking_v1 = get_k8s_client()
    ns = agent_namespace(agent)

    try:
        agent.status = FleetAgent.Status.PROVISIONING
        agent.status_message = ''
        agent.save(update_fields=['status', 'status_message'])
        _log_event(agent, 'provisioning', f'Provisioning in namespace {ns}')

        _ensure_namespace(core_v1, ns, agent)
        secret_snapshot = None
        reused_existing_identity_secret = False
        if auto_provision_identity:
            secret_snapshot = _read_identity_secret_snapshot(core_v1, ns, agent)
            existing_password = _secret_data_text(secret_snapshot, 'AGENT_PASSWORD') if secret_snapshot else ''
            if existing_password:
                agent_password = existing_password
                reused_existing_identity_secret = True
            else:
                agent_password = _generate_agent_password()

        if not reused_existing_identity_secret:
            _create_identity_secret(core_v1, ns, agent, agent_password, user_token)
        if auto_provision_identity:
            ok = _provision_platform_identity(agent, agent_password, platform_url)
            if not ok:
                if reused_existing_identity_secret:
                    status_message = 'shizuha-id identity provisioning failed (PLAT-673); identity Secret left unchanged'
                    event_message = (
                        'Identity provisioning on shizuha-id failed while reusing the existing Secret password; '
                        'left identity Secret unchanged and stopped'
                    )
                else:
                    _restore_identity_secret(core_v1, ns, agent, secret_snapshot)
                    status_message = 'shizuha-id identity provisioning failed (PLAT-673); identity Secret restored'
                    event_message = (
                        'Identity provisioning on shizuha-id failed after Secret write; '
                        'restored previous Secret and stopped'
                    )
                agent.status = FleetAgent.Status.FAILED
                agent.status_message = status_message
                agent.save(update_fields=['status', 'status_message'])
                _log_event(agent, 'error', event_message)
                return False
        _create_workspace_pvc(core_v1, ns, agent)
        # P1-A/PLAT-174 + Codex P1: isolated agents get NetworkPolicies emitted
        # BEFORE the Deployment so the pod starts with isolation already in place.
        # Order matters: default-deny lands first, then allowlists, then the pod.
        if agent.isolated:
            _create_ssh_egress_network_policies(networking_v1, ns, agent)
        # HIVE-1377: project the coordinator broker token Secret BEFORE the
        # Deployment renders a broker sidecar that references it with
        # optional=False — a missing Secret here is a CreateContainerConfigError
        # crash loop (agent-shizuha-116), not a degraded pod. Fail closed at the
        # render boundary when the shared source Secret is absent.
        if not _ensure_coordinator_broker_token(core_v1, ns, agent):
            agent.status = FleetAgent.Status.FAILED
            agent.status_message = (
                f'coordinator broker token Secret '
                f'({getattr(settings, "FLEET_COORDINATOR_TOKEN_SECRET_NAME", "") or "hive-coordinator-broker-token"}) '
                f'missing; refused to render a Deployment whose broker could not '
                f'start (HIVE-1377)'
            )
            agent.save(update_fields=['status', 'status_message'])
            _log_event(
                agent, 'error',
                'Refused to render Deployment without the coordinator broker token Secret (HIVE-1377)',
            )
            return False
        # The agent container signs browser-worker JWTs at startup. Materialize
        # the same key into both namespaces BEFORE rendering either consumer so
        # an initial provision can never start in a split-secret state.
        if agent_needs_browser_mcp(agent):
            _ensure_browser_namespace(core_v1, browser_worker_namespace())
            if not _ensure_browser_mcp_jwt_secrets(core_v1, agent):
                agent.status = FleetAgent.Status.FAILED
                agent.status_message = (
                    'browser MCP JWT source missing; refused to render divergent '
                    'signer/verifier Deployments (HIVE-1922)'
                )
                agent.save(update_fields=['status', 'status_message'])
                _log_event(
                    agent, 'error',
                    'Refused to render browser MCP without a shared JWT contract (HIVE-1922)',
                )
                return False
        # PLAT-5275: the multiplexer verifies a signed Hive MCP projection
        # against a pinned public keyring; fail closed if it is unavailable.
        if not _ensure_projection_keyring(core_v1, ns, agent):
            agent.status = FleetAgent.Status.FAILED
            agent.status_message = 'MCP projection public keyring unavailable; refused to render a fail-open runtime'
            agent.save(update_fields=['status', 'status_message'])
            _log_event(agent, 'error', 'Refused to render Deployment without MCP projection public keyring (PLAT-5275)')
            return False
        # Deployment env refs <username>-agent-creds as optional=False.
        # Mint before first apply or the pod lands CreateContainerConfigError.
        ensure_agent_cortex_key(agent)
        # PLAT-7406: Cortex-key create used to persist a CORTEX_API_KEY-only
        # Secret after a wipe. Origin refs stay optional, so the pod started
        # tokenless. Mint Forgejo into the same Secret before first apply.
        try:
            ensure_agent_forgejo_token(agent)
        except Exception as exc:  # noqa: BLE001 — Origin mint must not block Cortex boot
            logger.error('forgejo-token: provision-time mint failed for %s: %s', agent.agent_username, exc)
        # PLAT-5372: re-establish role-scoped credential projections (e.g. the
        # accounting-portal material) on a fresh provision — the renderer only
        # emits AGENT_PASSWORD/GITHUB_TOKEN/CONTEXT_PROMPT, so without this a
        # deprovision/reprovision silently drops the projection.
        try:
            ensure_agent_role_credentials(agent)
        except Exception as exc:  # noqa: BLE001 — a projection failure must not block provisioning
            logger.error('role-creds: provision-time projection failed for %s: %s', agent.agent_username, exc)
        decision = _lease_start_decision(agent)
        cold_agent = cold_browser = None
        if not decision.allowed:
            # Resource/identity bootstrap precedes runtime authority. A newly
            # provisioned sleeping agent still needs its existing onboarding
            # demand so the ordinary planner can select and grant it a lease.
            # Never replace a positive/unknown Deployment with a cold template.
            if not decision.pending:
                agent.status = original_status
                agent.save(update_fields=['status'])
                _record_lease_start_refusal(agent, decision)
                return False
            try:
                cold_agent = _cold_deployment_state(apps_v1, ns, agent.deployment_name)
                if agent_needs_browser_mcp(agent):
                    cold_browser = _cold_deployment_state(
                        apps_v1, browser_worker_namespace(), _browser_worker_name(agent),
                    )
            except Exception:  # noqa: BLE001 — do not invent a stopped runtime
                agent.status = original_status
                agent.save(update_fields=['status'])
                _record_lease_start_refusal(
                    agent, LeaseStartDecision(False, 'deployment_state_unreadable'),
                )
                return False
            if not cold_agent.can_stage or (cold_browser is not None and not cold_browser.can_stage):
                agent.status = original_status
                agent.save(update_fields=['status'])
                _record_lease_start_refusal(agent, decision)
                return False
        cold_options = {'cold_state': cold_agent} if cold_agent is not None else {}
        _create_deployment(apps_v1, ns, agent, **cold_options)
        if agent.expose_http or wants_voice_s2s(agent):
            _create_service(core_v1, ns, agent)
        else:
            # P2 (aoi): if expose_http was turned OFF on a reprovision, remove any
            # Service left over from a prior provision so it doesn't dangle.
            # grok-voice seats keep the ClusterIP so Voice can reach SCLI S2S.
            _delete_if_exists(core_v1.delete_namespaced_service, agent.deployment_name, ns)

        # Private per-agent browser MCP worker (never the shared shizuha-browser
        # Deployment). Must land before the agent starts heavy browser work so
        # SHIZUHA_BROWSER_MCP_URL resolves.
        if agent_needs_browser_mcp(agent):
            browser_options = {'cold_state': cold_browser} if cold_browser is not None else {}
            if not provision_browser_worker(agent, jwt_secret_ready=True, **browser_options):
                agent._lease_start_decision = None
                agent.status = FleetAgent.Status.FAILED
                agent.status_message = (
                    'private browser worker provisioning failed; refused to leave '
                    'an agent Deployment pointing at an unavailable verifier '
                    '(HIVE-1922)'
                )
                agent.save(update_fields=['status', 'status_message'])
                _log_event(
                    agent, 'browser_worker_failed',
                    'Private browser worker failed after JWT contract convergence (HIVE-1922)',
                )
                return False
            # Persist URL into agent.env so daemon/k8s reconcile keeps it.
            env = dict(agent.env or {})
            env.update(browser_mcp_env_for_agent(agent))
            # Drop meta key that is not a real env var for the container.
            env.pop('SHIZUHA_BROWSER_MCP_JWT_SECRET_FROM', None)
            agent.env = env
            agent.save(update_fields=['env', 'updated_at'])
            # Rebuild Deployment so new env is live (Recreate strategy).
            # The first cold write changed its resourceVersion. Re-observe
            # before the env refresh; a concurrent start must win over staging.
            if cold_agent is not None:
                cold_agent = _cold_deployment_state(apps_v1, ns, agent.deployment_name)
                if not cold_agent.can_stage:
                    agent.status = original_status
                    agent.save(update_fields=['status'])
                    _record_lease_start_refusal(agent, decision)
                    return False
            cold_options = {'cold_state': cold_agent} if cold_agent is not None else {}
            _create_deployment(apps_v1, ns, agent, **cold_options)
            _log_event(agent, 'browser_worker', f'Private browser MCP at {browser_worker_service_url(agent)}')

        agent.status = (FleetAgent.Status.RUNNING if decision.allowed else
                        FleetAgent.Status.STOPPED if original_status == FleetAgent.Status.STOPPED else
                        FleetAgent.Status.HIBERNATED)
        agent.namespace = ns
        # This records completed identity/storage/Deployment provisioning,
        # including a cold Deployment; it does not assert a running runtime.
        agent.provisioned_at = timezone.now()
        # last_active_at is stored in naive UTC (see fleet/timeutils) — NOT the
        # naive TIME_ZONE wall clock timezone.now() returns. Writing local here
        # made a freshly provisioned agent read six hours stale to
        # _within_grace_window, the exact "can stop an active agent" hazard.
        fields = ['status', 'namespace', 'provisioned_at']
        if decision.allowed:
            agent.last_active_at = naive_utc_now()
            fields.append('last_active_at')
        elif agent.status == FleetAgent.Status.HIBERNATED:
            agent.hibernation_reason = 'awaiting model lease grant'
            agent.hibernated_at = timezone.now()
            fields.extend(['hibernation_reason', 'hibernated_at'])
        agent.save(update_fields=fields)
        _log_event(agent, 'provisioned', 'Agent provisioned successfully')
        # PLAT-397: trigger onboarding flow for newly provisioned agents
        if first_provision:
            _trigger_onboarding_async(agent)
        if not decision.allowed:
            _record_lease_start_refusal(agent, decision)
        return decision.allowed

    except Exception as e:
        logger.error(f'Provisioning failed for fleet agent {agent.agent_username}: {e}')
        decision = getattr(agent, '_lease_start_decision', None)
        agent.status = (original_status if isinstance(decision, LeaseStartDecision) and not decision.allowed
                        else FleetAgent.Status.FAILED)
        agent._lease_start_decision = None
        agent.status_message = str(e)
        agent.save(update_fields=['status', 'status_message'])
        _log_event(agent, 'error', f'Provisioning failed: {e}')
        return False


def deprovision_agent(agent: FleetAgent) -> bool:
    """
    Tear an agent down. Isolated agent → delete its namespace. Shared-ns agent →
    delete only its own Deployment, Service, PVC, and Secret (never the shared ns).
    """
    core_v1, apps_v1, _ = get_k8s_client()
    ns = _live_ns(agent)
    try:
        agent.status = FleetAgent.Status.DELETING
        agent.save(update_fields=['status'])

        # Always tear down private browser worker (other ns); never leave shared
        # browser state behind for a deleted agent.
        deprovision_browser_worker(agent)

        # P1 blast-radius guard (aoi round-2): only delete the namespace when it
        # is genuinely THIS agent's own isolated ns (fleet-<user>), never the
        # shared fleet ns. `isolated` is mutable — if an agent was provisioned in
        # the shared ns and `isolated` is later flipped True, a naive
        # `if agent.isolated: delete ns` would nuke the SHARED ns and take down
        # every fleet agent. Decide on the ACTUAL ns, not the current flag.
        own_isolated_ns = (ns != fleet_namespace() and ns == f'fleet-{agent.agent_username}')
        if own_isolated_ns:
            _delete_namespace(core_v1, ns)  # refuses the shared ns as defense-in-depth
        else:
            _delete_if_exists(apps_v1.delete_namespaced_deployment, agent.deployment_name, ns)
            _delete_if_exists(core_v1.delete_namespaced_service, agent.deployment_name, ns)
            _delete_if_exists(core_v1.delete_namespaced_persistent_volume_claim, agent.workspace_pvc_name, ns)
            _delete_if_exists(core_v1.delete_namespaced_secret, agent.identity_secret_name, ns)

            # Daemon-owned k3s agents use `<username>-workspace` and
            # `<username>-agent-creds`, and always materialize in the shared
            # fleet namespace. A stale daemon observation can project the Hive
            # row as `namespace=rt-fleet`; permanent deletion must still remove
            # the actual named workload instead of succeeding against the wrong
            # namespace and leaving an orphan to run forever.
            if agent.managing_daemon_id:
                daemon_ns = fleet_namespace()
                if daemon_ns != ns:
                    _delete_if_exists(apps_v1.delete_namespaced_deployment, agent.deployment_name, daemon_ns)
                    _delete_if_exists(core_v1.delete_namespaced_service, agent.deployment_name, daemon_ns)
                _delete_if_exists(
                    core_v1.delete_namespaced_persistent_volume_claim,
                    f'{agent.agent_username}-workspace', daemon_ns,
                )
                _delete_if_exists(
                    core_v1.delete_namespaced_secret,
                    f'{agent.agent_username}-agent-creds', daemon_ns,
                )

        # PLAT-153 (review P2): tear down the agent's per-agent sandbox namespace
        # (sbx-<agent>) too — any exec created it with its own RBAC/NetworkPolicy/
        # quota, and leaving it after the FleetAgent is deleted orphans those objects
        # (and a stale runner SA) on the cluster. Best-effort; never block teardown.
        try:
            from .sandbox import teardown_sandbox_namespace
            teardown_sandbox_namespace(agent)
        except Exception as _e:
            logger.warning(f'deprovision: sandbox-ns teardown failed for {agent.agent_username}: {_e}')

        agent.status = FleetAgent.Status.DELETED
        agent.save(update_fields=['status'])
        _log_event(agent, 'destroyed', 'Agent deprovisioned')
        return True
    except ApiException as e:
        logger.error(f'Deprovision failed for {agent.agent_username} ({ns}): {e}')
        agent.status = FleetAgent.Status.FAILED
        agent.status_message = str(e)
        agent.save(update_fields=['status', 'status_message'])
        return False



# ── PLAT-4884 account↔secret drift reconcile (Architecture contract) ───────
# Event-driven on start/enable: probe login against the identity Secret; only
# the exact Shizuha-ID invalid-credentials 401 authorizes one scoped reconcile
# via the existing fleet provisioner path. Timeouts/429/403/5xx mutate nothing.

_LOGIN_OUTCOME_OK = 'ok'
_LOGIN_OUTCOME_INVALID = 'invalid_credentials'
_LOGIN_OUTCOME_INDETERMINATE = 'indeterminate'

# Dedup fail-loud signals per agent (process-local; one emission per drift hold).
_drift_hold_emitted: set[str] = set()


def _classify_id_login_response(resp) -> str:
    """Map an ID login HTTP response to the PLAT-4573/4884 typed outcome union."""
    if resp is None:
        return _LOGIN_OUTCOME_INDETERMINATE
    code = getattr(resp, 'status_code', None)
    if code == 200:
        return _LOGIN_OUTCOME_OK
    if code == 401:
        try:
            body = resp.json() if hasattr(resp, 'json') else {}
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        err = str(body.get('error') or body.get('detail') or '')
        # Canonical PLAT-3997/4573 contract: HTTP 401 + Invalid credentials.
        if err.strip().lower() in {'invalid credentials', 'invalid credential'}:
            return _LOGIN_OUTCOME_INVALID
        if 'invalid credential' in err.lower():
            return _LOGIN_OUTCOME_INVALID
        return _LOGIN_OUTCOME_INDETERMINATE
    # 403/account-policy, 429, 5xx, transport-shaped non-responses → no mutation.
    return _LOGIN_OUTCOME_INDETERMINATE


def _probe_id_login(username: str, password: str, *, platform_url: str = '') -> str:
    """One login probe. Never logs password bytes. Returns typed outcome."""
    if not username or not password:
        return _LOGIN_OUTCOME_INDETERMINATE
    base = (platform_url or getattr(settings, 'SHIZUHA_PLATFORM_URL', '') or '').rstrip('/')
    if base:
        login_url = f'{base}/id/api/auth/login/'
    else:
        id_base = (getattr(settings, 'SHIZUHA_ID_INTERNAL_URL', '') or '').rstrip('/')
        login_url = f'{id_base}/api/auth/login/' if id_base else ''
    if not login_url.startswith('http'):
        return _LOGIN_OUTCOME_INDETERMINATE
    try:
        resp = requests.post(
            login_url,
            json={'username': username, 'password': password},
            timeout=10,
        )
        return _classify_id_login_response(resp)
    except Exception as exc:  # noqa: BLE001 — transport/timeout → indeterminate
        logger.warning(
            'PLAT-4884: login probe indeterminate for %s: %s',
            username, type(exc).__name__,
        )
        return _LOGIN_OUTCOME_INDETERMINATE


def _emit_drift_hold(username: str, generation: str, *, reason: str) -> None:
    """One deduplicated owner-routed fail-loud signal; never a retry storm."""
    if username in _drift_hold_emitted:
        return
    _drift_hold_emitted.add(username)
    logger.error(
        'PLAT-4884 DRIFT_HOLD agent=%s gen=%s reason=%s — owner route: engineering/fleet; '
        'no further automatic password mutation this process',
        username, generation or 'unknown', reason,
    )


def ensure_agent_id_password_aligned(agent: 'FleetAgent') -> str:
    """Ensure the identity Secret password authenticates to Shizuha ID.

    Architecture PLAT-4884 contract (event-driven on start/enable):
      1. Read AGENT_PASSWORD from the per-agent identity Secret (no mint).
      2. Probe login once.
      3. Only exact invalid-credentials 401 → one scoped provision-agent reconcile.
      4. Verify with one fresh login probe; second 401 → fail-loud hold, no loop.
      5. Timeout/429/403/5xx → zero mutation.

    Returns one of: ``ok``, ``reconciled``, ``hold``, ``skipped``.
    """
    username = (agent.agent_username or '').strip()
    if not username:
        return 'skipped'

    platform_url = getattr(settings, 'SHIZUHA_PLATFORM_URL', '').rstrip('/')
    try:
        core_v1, _apps, _net = get_k8s_client()
        try:
            ns = _live_ns(agent)
        except Exception:
            ns = agent_namespace(agent)
        snap = _read_identity_secret_snapshot(core_v1, ns, agent)
        password = _secret_data_text(snap, 'AGENT_PASSWORD') if snap else ''
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            'PLAT-4884: cannot read identity Secret for %s (%s); skipping reconcile',
            username, type(exc).__name__,
        )
        return 'skipped'

    if not password:
        logger.info('PLAT-4884: no AGENT_PASSWORD in Secret for %s; skip', username)
        return 'skipped'

    generation = ''
    try:
        generation = str(getattr(getattr(snap, 'metadata', None), 'resource_version', '') or '')
    except Exception:
        generation = ''

    outcome = _probe_id_login(username, password, platform_url=platform_url)
    if outcome == _LOGIN_OUTCOME_OK:
        return 'ok'
    if outcome != _LOGIN_OUTCOME_INVALID:
        logger.warning(
            'PLAT-4884: login probe %s for %s gen=%s — zero mutation',
            outcome, username, generation or 'unknown',
        )
        return 'skipped'

    logger.warning(
        'PLAT-4884: confirmed credential drift for %s gen=%s — one reconcile',
        username, generation or 'unknown',
    )
    # platform_url may be empty when using internal ID URL for probe; provision
    # path uses SHIZUHA_ID_INTERNAL_URL internally and ignores platform_url body.
    ok = _provision_platform_identity(agent, password, platform_url or 'internal')
    if not ok:
        _emit_drift_hold(username, generation, reason='reconcile_failed')
        return 'hold'

    verify = _probe_id_login(username, password, platform_url=platform_url)
    if verify == _LOGIN_OUTCOME_OK:
        logger.info(
            'PLAT-4884: reconciled %s gen=%s — login ok',
            username, generation or 'unknown',
        )
        _drift_hold_emitted.discard(username)
        return 'reconciled'

    _emit_drift_hold(username, generation, reason=f'verify_{verify}')
    return 'hold'


def start_agent(agent: FleetAgent) -> bool:
    """Scale the agent Deployment to 1.

    PLAT-4884: before scale-up, ensure the identity Secret password still
    authenticates to Shizuha ID (event-driven drift reconcile on enable/start).

    HIVE-1713 (operator 2026-08-11): the lease invariant is enforced HERE,
    preemptively — "there would have been no reason to stop them if they
    hadn't been started in the first place." An agent whose start would
    run without a confirmed lease never scales up. Native start paths use
    this guard; first-time provisioning applies the same authority check
    before its Deployment write. Confirmed holders pass ('already_holder'); ungoverned
    models pass ('no_lease_pool'). Free capacity is not a reservation;
    a missing holder or unreadable board defers the start — the agent stays
    hibernated, its queued work keeps it lease-worthy, and the 10-min lease
    reconcile wakes it the moment a slot frees.
    """
    agent._lease_start_decision = None
    if _personal_runtime_isolation_gate(agent):
        return False
    # A wake can recreate/patch the pod template, so it is a provisioning
    # boundary and must consume fresh reviewed intent too. Bind after the
    # non-provisionable personal-isolation gate, before any k8s mutation.
    _bind_reviewed_runtime_image(agent)
    decision = _lease_start_decision(agent)
    if not decision.allowed:
        _record_lease_start_refusal(agent, decision)
        return False
    from .native_lifecycle_projection import retire_before_start
    retire_before_start(agent)
    # Drift hold must not provision the fleet (Architecture contract §4).
    align = ensure_agent_id_password_aligned(agent)
    if align == 'hold':
        agent.status_message = (
            'PLAT-4884: account↔secret drift hold — identity Secret password '
            'does not authenticate after one reconcile; owner intervention required'
        )
        try:
            agent.save(update_fields=['status_message'])
        except Exception:
            pass
        _log_event(agent, 'error', agent.status_message)
        return False
    # A stopped native Deployment keeps its old pod template. The harness
    # updater advances FleetAgent.image while it sleeps, so apply that desired
    # image before scaling or the first wake would boot the retired harness.
    native_options = {'_preserve_native_intent': True} if getattr(agent, '_native_runtime_release', None) else {}
    if not ensure_agent_image(agent, **native_options):
        msg = str(getattr(agent, 'status_message', '') or '')
        # rt-fleet-era Hive rows have no k3s Deployment. Reconcile used to
        # abort the wake on read_deployment 404, so 13 GLM lease holders
        # stayed hibernated while Cortex advertised 20 seats (2026-09-01).
        if 'stage read_deployment' in msg and '404' in msg:
            logger.info(
                'start_agent: no Deployment for %s; first-time native provision',
                agent.agent_username,
            )
            return provision_agent(agent, **native_options)
        return False
    if agent_needs_browser_mcp(agent) and not _start_browser_worker(agent):
        return False
    if not _scale(agent, 1):
        return False
    previous_status = agent.status
    agent.status = FleetAgent.Status.RUNNING
    agent.status_message = ''
    agent.hibernation_reason = ''
    agent.hibernated_at = None
    agent.runtime_lifecycle_state = 'enabled'
    # Naive UTC — see fleet/timeutils for why timezone.now() is wrong here.
    agent.last_active_at = naive_utc_now()
    agent.save(update_fields=[
        'status', 'status_message', 'hibernation_reason', 'hibernated_at',
        'runtime_lifecycle_state', 'last_active_at',
    ])
    _log_event(agent, 'started', 'Scaled to 1')
    # PLAT-8834: serve/resume transition telemetry (best-effort).
    from . import seat_lifecycle_metrics
    seat_lifecycle_metrics.record_transition(
        agent.agent_username, previous_status, FleetAgent.Status.RUNNING,
    )
    return True


def _scale_agent_to_zero(
    agent: FleetAgent,
    *,
    status: str,
    lifecycle_reason: str,
    event_type: str,
) -> bool:
    """Scale an agent to zero and persist the requested lifecycle intent.

    ``HIBERNATED`` is automatic and auto-resumable. ``STOPPED`` is an explicit
    operator kill switch. Both release Cortex ownership and retain the PVC.
    """
    from .native_lifecycle_projection import begin_inactive, complete_inactive
    previous_status = agent.status
    try:
        begin_inactive(agent, status)
    except Exception:
        logger.exception('Native inactive lifecycle source unavailable for %s', agent.agent_username)
        return False
    # Snapshot the live transcript while the pod is still up so the Agents
    # drawer can show history after hibernate/restart (no pod to exec).
    try:
        from .activity_snapshot import capture_native_activity_snapshot
        capture_native_activity_snapshot(agent)
    except Exception:
        logger.warning(
            'pre-scale activity snapshot failed for %s',
            getattr(agent, 'agent_username', '?'),
            exc_info=True,
        )
    if not _scale(agent, 0):
        return False
    # Drop model leases BEFORE staging image / status write so a reconciler tick
    # that races this stop cannot re-read a live board + stopped status and leave
    # the pin in place until the next idle window.
    try:
        from .model_lease import release_leases_for_agent
        lease_out = release_leases_for_agent(
            agent.agent_username,
            reason=f'native {event_type} {agent.agent_username}',
        )
        if lease_out.get('released'):
            logger.info(
                'Released model lease(s) %s for %s agent %s',
                lease_out['released'], event_type, agent.agent_username,
            )
            # HIVE-2909: an automatic hibernate that frees a lease must not
            # cost the seat its coverage priority — if it re-enables within
            # one reconcile cycle, it wins the refill over unrelated idle
            # seats (the floor seats coverage, not the stalest idle).
            from .model_lease import mark_returning_floor_holder
            mark_returning_floor_holder(agent.agent_username)
            # PLAT-9052: slot(s) freed — honor the DEFER promise by granting
            # the oldest outstanding deferred connect-wake, if any.
            from .wake_defers import reconcile_deferred_wakes
            reconcile_deferred_wakes()
            _log_event(
                agent, 'model_lease_released',
                f"Released Cortex lease(s): {', '.join(lease_out['released'])}",
            )
        for err in lease_out.get('errors') or []:
            logger.warning(
                'model-lease release on %s %s: %s',
                event_type, agent.agent_username, err,
            )
            # HIVE-2909: release errors are not silent residue — surface a
            # typed event so the Agents drawer shows why the KV home may
            # still be booked after the seat went to zero.
            _log_event(
                agent, 'model_lease_release_error',
                f'model-lease release error: {err}',
            )
    except Exception as exc:  # noqa: BLE001 — never block stop on lease board
        logger.warning(
            'model-lease release on %s %s failed open: %s',
            event_type, agent.agent_username, exc,
        )
    # Once stopped it is safe to stage any newer desired harness image without
    # interrupting a turn. A later start also retries this fail-loud.
    # ensure_agent_image clears any sticky runtime-contract failure banner when
    # staging succeeds, so a clean hibernate does not keep showing a prior
    # aborted-wake 409 on the Agents UI (sato 2026-07-22).
    if not ensure_agent_image(agent):
        logger.warning(
            'Scaled %s to zero but could not stage desired image %s',
            agent.agent_username, agent.image,
        )
    agent.status = status
    update_fields = ['status', 'hibernation_reason', 'hibernated_at']
    if status == FleetAgent.Status.HIBERNATED:
        agent.hibernation_reason = str(lifecycle_reason or 'automatic idle policy')[:255]
        agent.hibernated_at = naive_utc_now()
        # Native hibernate must clear operator_stopped or the agent is
        # never auto-wakeable (mika 2026-08-31: QA 390 ready, no GLM seat).
        agent.runtime_lifecycle_state = 'hibernated'
        update_fields.append('runtime_lifecycle_state')
        # Sora/Sana 2026-09-16: last heartbeat's needs_help survived yield.
        agent.needs_help = False
        agent.needs_help_reason = ''
        update_fields.extend(['needs_help', 'needs_help_reason'])
    else:
        agent.hibernation_reason = ''
        agent.hibernated_at = None
        agent.runtime_lifecycle_state = 'operator_stopped'
        update_fields.append('runtime_lifecycle_state')
    agent.save(update_fields=update_fields)
    complete_inactive(agent)
    _log_event(agent, event_type, lifecycle_reason or 'Scaled to 0')
    # PLAT-8834: seat-loss transition telemetry (best-effort). HIBERNATED is
    # the automatic seat-loss signal the Team-Outage alert rates on.
    from . import seat_lifecycle_metrics
    seat_lifecycle_metrics.record_transition(
        agent.agent_username, previous_status, status,
    )
    return True


def hibernate_agent(agent: FleetAgent, *, reason: str = '') -> bool:
    """Automatically scale to zero while preserving auto-wake eligibility."""
    return _scale_agent_to_zero(
        agent,
        status=FleetAgent.Status.HIBERNATED,
        lifecycle_reason=reason or 'Hive automatic hibernation policy',
        event_type='hibernated',
    )


def stop_agent(agent: FleetAgent) -> bool:
    """Explicitly stop an agent; automatic controllers must use hibernate_agent."""
    return _scale_agent_to_zero(
        agent,
        status=FleetAgent.Status.STOPPED,
        lifecycle_reason='Explicitly stopped by operator',
        event_type='stopped',
    )


def scale_agent(agent: FleetAgent, replicas: int) -> bool:
    """Explicit scale (0 or 1 — single-active-instance invariant, HLD §8).

    Scale-to-zero is hibernation. Hard stop is ``stop_agent`` / operator
    ``/stop/`` only (operator 2026-09-01).
    """
    if replicas not in (0, 1):
        raise ValueError('FleetAgent is single-active-instance: replicas must be 0 or 1 (HLD §8)')
    return start_agent(agent) if replicas == 1 else hibernate_agent(
        agent, reason='explicit scale to zero',
    )


def reconcile_stopped_agent_drift(*, dry_run: bool = False) -> dict:
    """PLAT-4722: converge k8s observation TO Hive's authoritative intent.

    The on-demand reconciler only acts on agents needing a state CHANGE, so a
    FleetAgent that Hive already thinks is stopped/hibernated (intended) but whose
    k8s Deployment is still scaled to 1 (orphan — e.g. a harness roll that scaled
    it down then lost the write, or a manual scale that never persisted) is never
    corrected: Hive sees stopped=intended, the harness roller skips it, and the
    pod runs forever on a stale harness (observed 2026-07-16: rei/reika/mika).

    This sweep audits every STOPPED/HIBERNATED agent and scales any live
    Deployment back to 0 (Hive-authoritative). Returns a summary dict.
    """
    _, apps_v1, _ = get_k8s_client()
    summary = {'checked': 0, 'drifted': 0, 'scaled': 0, 'errors': 0}
    for agent in FleetAgent.objects.filter(
        status__in=(FleetAgent.Status.STOPPED, FleetAgent.Status.HIBERNATED)
    ):
        summary['checked'] += 1
        try:
            ns = _live_ns(agent)
            dep = apps_v1.read_namespaced_deployment(agent.deployment_name, ns)
            replicas = dep.spec.replicas or 0
            if replicas <= 0:
                continue
            summary['drifted'] += 1
            logger.warning(
                'PLAT-4722 drift: agent %s status=%s but Deployment %s/%s replicas=%s — scaling to 0',
                agent.agent_username, agent.status, ns, agent.deployment_name, replicas,
            )
            if dry_run:
                print(f'WOULD-SCALE-0 {agent.agent_username} ns={ns} replicas={replicas}')
                continue
            apps_v1.patch_namespaced_deployment_scale(
                name=agent.deployment_name,
                namespace=ns,
                body={'spec': {'replicas': 0}},
            )
            summary['scaled'] += 1
            _log_event(
                agent,
                'drift_reconciled',
                f'k8s Deployment replicas {replicas}->0 to match Hive {agent.status} intent (PLAT-4722)',
            )
        except ApiException as e:
            if e.status == 404:
                continue  # no Deployment — nothing to drift
            summary['errors'] += 1
            logger.error('PLAT-4722 drift check failed for %s: %s', agent.agent_username, e)
        except Exception as exc:  # noqa: BLE001 — never let one agent break the sweep
            summary['errors'] += 1
            logger.error('PLAT-4722 drift check failed for %s: %s', agent.agent_username, exc)
    return summary


def emergency_scale(agent: FleetAgent, replicas: int) -> bool:
    """PLAT-4902: daemon-plane declarative fallback — scale ONLY.

    Unlike start_agent/stop_agent (native-plane actuators), this writes no
    FleetAgent status and never touches the pod template: for rt-fleet agents
    the daemon owns runtime state and converges from Hive desired state when it
    returns, so the only safe emergency action is the same idempotent
    replicas write the daemon itself performs on toggle.
    """
    if replicas not in (0, 1):
        raise ValueError('FleetAgent is single-active-instance: replicas must be 0 or 1 (HLD §8)')
    return _scale(agent, replicas)


# Kubernetes returns HTTP 409 when another writer races the Deployment between
# our read and patch (on-demand wake vs stop, harness stage, etc.). Retry with a
# fresh read rather than sticky-failing the Agents UI with an operator-hostile
# "wake aborted" message for a transient optimistic-concurrency conflict.
_RUNTIME_CONTRACT_PATCH_ATTEMPTS = 3
NATIVE_RUNTIME_LABEL = 'shizuha.io/runtime'
NATIVE_RUNTIME_LABEL_VALUE = 'k3s-native'
NATIVE_RUNTIME_OWNER_ANNOTATION = 'shizuha.io/runtime-owner'
NATIVE_RUNTIME_OWNER = 'hive'
NATIVE_RUNTIME_METRICS_PORT = 9103

# Runtime-lane fields rendered by ``_agent_container``.  A native Deployment
# persists while scaled to zero, so changing FleetAgent.model alone is not
# enough: the next wake otherwise boots the old MODEL from its stale pod
# template.  Keep this list narrow and declarative; unrelated environment owned
# by other controllers is preserved.
_RUNTIME_LANE_ENV_NAMES = {
    'AGENT_ROLE',
    'AGENT_TEAM',
    'MODEL',
    'SHIZUHA_K8S_PRIMARY_MODEL',
    'REASONING_EFFORT',
    'CONTEXT_PROMPT',
    'FAILOVER_CHAIN_ID',
    'MODEL_FALLBACKS',
    'SHIZUHA_MODEL_FALLBACKS',
    'MODEL_OVERRIDES',
    'PERSONALITY_TRAITS',
    # Browser MCP signer refs are part of the live runtime contract. Including
    # them here lets ordinary reconcile/wake heal pre-HIVE-1922 Deployments.
    'SHIZUHA_BROWSER_MCP',
    'SHIZUHA_BROWSER_MCP_URL',
    'SHIZUHA_BROWSER_MCP_JWT_SECRET',
    'JWT_SECRET_KEY',
    'SHIZUHA_IDLE_HEARTBEAT_MS',
    'SHIZUHA_HEARTBEAT_DEBOUNCE_MS',
    'SHIZUHA_FIRST_HEARTBEAT_MS',
    'SHIZUHA_LEAN_MCP',
    'SHIZUHA_TALK_MINIMAL_PROMPT',
    'SHIZUHA_CONNECT_AUTOREPLY',
    'VLLM_STREAM_STALL_MS',
    # Hive-owned GLM stream contract. Without this name, `_lane_env_drifted`
    # ignores VLLM_STREAM_WITH_TOOLS and running seats stay on the old stamp
    # (20 GLM replicas were STREAM=0 after the 2026-09-06 default flipped to 1).
    'VLLM_STREAM_WITH_TOOLS',
    # PLAT-3625: k3s-native pods have no ~/.mcp.json. Without this allow-list
    # the gateway derives every platform MCP server then applies the fat role
    # matrix (Hina connected admin/id/scs + 23 Pulse tools). Hive SoT must
    # land here so lane converge can trim a running seat.
    'AGENT_EFFECTIVE_MCP_SERVICES',
    'SHIZUHA_MCP_SERVICES',
}


def _uses_native_runtime_image(agent: FleetAgent) -> bool:
    """Whether Hive owns the image's runtime command and start gate.

    Harness promotion persists an immutable ``shizuha-agent-runtime`` tag in
    ``FleetAgent.image``.  A non-empty image therefore does not necessarily
    mean "custom image"; treating every persisted tag as custom leaves the
    previous bridge command behind when the agent's model lane changes.
    Match the repository basename exactly so similarly named custom images
    retain ownership of their entrypoint.
    """
    from .runtime_release import is_managed_runtime_image_ref

    image = str(agent.image or '').strip()
    return not image or is_managed_runtime_image_ref(image)


def _decode_exec_mapping(value) -> dict:
    """Normalize Kubernetes exec JSON across websocket-client return shapes.

    The Kubernetes Python client sometimes returns a native ``dict`` and
    sometimes returns that dict's Python representation as a string (single
    quotes / ``True`` / ``False``). Prefer strict JSON, then safely accept the
    literal representation. Anything else remains fail-closed.
    """
    if isinstance(value, dict):
        return value
    candidate = str(value).strip()
    for decoder in (json.loads, ast.literal_eval):
        try:
            decoded = decoder(candidate)
        except (ValueError, SyntaxError, TypeError, json.JSONDecodeError):
            continue
        if isinstance(decoded, dict):
            return decoded
    raise ValueError('Kubernetes exec response was not a mapping')


@dataclass(frozen=True)
class NativeRuntimeRollReceipt:
    pod_name: str
    pod_uid: str
    target_image: str
    deployment_uid: str
    source_generation: int
    document_hash: str
    request_id: str
    lease_until_ms: float
    expires_monotonic: float
    container_id: str
    admission_version: int


def _native_runtime_inactive_history(pod, apps, namespace, deployment_uid):
    """Prove retained history has no container process, rather than trusting phase.

    Failed can also mean a lost node. Only complete, explicit kubelet terminal
    observations for every declared process qualify, with the same Deployment
    owner chain as the live bridge. Any incomplete/uncertain history stays in
    the process census and therefore refuses mutation.
    """
    try:
        if (not deployment_uid or not pod.metadata.uid or pod.metadata.deletion_timestamp
                or pod.status.phase not in {'Failed', 'Succeeded'}):
            return False
        ready = [c.status for c in (pod.status.conditions or []) if c.type == 'Ready']
        if ready != ['False']:
            return False
        owners = [o for o in (pod.metadata.owner_references or []) if o.controller]
        if len(owners) != 1 or owners[0].kind != 'ReplicaSet' or not owners[0].uid:
            return False
        replica_set = apps.read_namespaced_replica_set(owners[0].name, namespace, _request_timeout=(3, 5))
        parent = [o for o in (replica_set.metadata.owner_references or []) if o.controller]
        if (replica_set.metadata.uid != owners[0].uid or replica_set.metadata.deletion_timestamp
                or len(parent) != 1 or parent[0].kind != 'Deployment'
                or parent[0].uid != deployment_uid):
            return False
        groups = (
            (pod.spec.containers, pod.status.container_statuses),
            (pod.spec.init_containers, pod.status.init_container_statuses),
            (pod.spec.ephemeral_containers, pod.status.ephemeral_container_statuses),
        )
        if not pod.spec.containers:
            return False
        seen_names, seen_ids = set(), set()
        for declared, observed in groups:
            names = [c.name for c in (declared or [])]
            statuses = observed or []
            if (any(not name for name in names) or len(set(names)) != len(names)
                    or len(statuses) != len(names) or {s.name for s in statuses} != set(names)):
                return False
            if seen_names.intersection(names):
                return False
            seen_names.update(names)
            for status in statuses:
                if status.container_id in seen_ids:
                    return False
                seen_ids.add(status.container_id)
                state = status.state
                ended = state.terminated if state else None
                if (status.ready is not False or status.started is not False or not ended
                        or state.running is not None or state.waiting is not None
                        or not status.container_id or ended.container_id != status.container_id
                        or ended.reason == 'ContainerStatusUnknown'
                        or isinstance(ended.exit_code, bool) or not isinstance(ended.exit_code, int)
                        or not ended.started_at or not ended.finished_at
                        or ended.finished_at < ended.started_at):
                    return False
        return True
    except Exception:  # schema/read uncertainty is never evidence of process absence
        return False


def _native_runtime_process_pods(pods, apps, namespace, deployment_uid):
    return [pod for pod in pods
            if not _native_runtime_inactive_history(pod, apps, namespace, deployment_uid)]


def native_agent_runtime_roll_ready(
    agent: FleetAgent, target_image: str, *,
    deployment_uid: str | None = None, return_receipt: bool = False,
    source_generation: int | None = None, document_hash: str = '',
    continue_wait=None,
) -> bool | NativeRuntimeRollReceipt:
    """Reserve or prove a safe turn boundary before rolling a native agent.

    New bridges implement drain-v1 and stop admitting work before returning
    ``ready``. Older bridges fall back to two consecutive live ``busy=false``
    observations, matching the daemon-managed compatibility fence. Every
    transport, schema, and lookup failure is fail-closed.
    """
    try:
        core_v1, apps_v1, _networking_v1 = get_k8s_client()
        ns = _live_ns(agent)
        pods = core_v1.list_namespaced_pod(
            ns,
            label_selector=f'shizuha.io/agent={agent.agent_username}',
            field_selector='' if return_receipt else 'status.phase=Running',
        ).items
        if return_receipt:
            pods = _native_runtime_process_pods(pods, apps_v1, ns, deployment_uid)
        ready_pods = [
            pod for pod in pods
            if not pod.metadata.deletion_timestamp
            and any(
                condition.type == 'Ready' and condition.status == 'True'
                for condition in (pod.status.conditions or [])
            )
        ]
        if len(ready_pods) != 1 or (return_receipt and len(pods) != 1):
            logger.info(
                'Native runtime roll deferred for %s: expected one Ready pod, found %s',
                agent.agent_username,
                len(ready_pods),
            )
            return False
        pod_name = ready_pods[0].metadata.name
        pod_uid = getattr(ready_pods[0].metadata, 'uid', None)
        if return_receipt:
            owners = [owner for owner in (ready_pods[0].metadata.owner_references or [])
                      if owner.kind == 'ReplicaSet' and owner.controller]
            if not pod_uid or not deployment_uid or len(owners) != 1 or not source_generation or not document_hash:
                return False
            replica_set = apps_v1.read_namespaced_replica_set(owners[0].name, ns)
            if replica_set.metadata.uid != owners[0].uid or not any(
                owner.kind == 'Deployment' and owner.controller and owner.uid == deployment_uid
                for owner in (replica_set.metadata.owner_references or [])
            ):
                return False
            container_ids = [s.container_id for s in (ready_pods[0].status.container_statuses or [])
                             if s.name == 'agent' and s.ready and s.container_id]
            if len(container_ids) != 1:
                return False
        request_key = f'{agent.agent_id or agent.pk}\0{target_image}'
        if return_receipt:
            request_key += f'\0{source_generation}\0{document_hash}\0{deployment_uid}\0{pod_uid}'
        request_id = hashlib.sha256(
            request_key.encode(),
        ).hexdigest()[:32]
        payload = json.dumps({
            'requestId': request_id,
            'targetImage': target_image,
            'leaseMs': 60_000,
        }, separators=(',', ':'))
        marker = '__SHIZUHA_HTTP_STATUS__:'
        command = [
            'sh', '-c',
            'curl -sS --max-time 3 -X POST '
            '-H "Content-Type: application/json" '
            f'--data-binary {shlex.quote(payload)} '
            f'-w "\\n{marker}%{{http_code}}" '
            'http://127.0.0.1:8080/v1/runtime/rollout-drain',
        ]
        lease_started = time.monotonic()
        output = stream(
            core_v1.connect_get_namespaced_pod_exec,
            pod_name,
            ns,
            container='agent',
            command=command,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
        )
        body, separator, status_raw = str(output).rpartition(f'\n{marker}')
        if not separator:
            logger.info(
                'Native runtime roll deferred for %s: drain response lacked status',
                agent.agent_username,
            )
            return False
        status = int(status_raw.strip())
        if status in {404, 405}:
            if return_receipt:
                # Two quiet observations are not an ingress reservation. The
                # separately scoped native release requires an actual fence;
                # retain the existing compatibility helper for old callers.
                return False
            # Legacy bridges cannot reserve ingress, so require a stable quiet
            # boundary across two independent live observations.
            for observation in range(2):
                health_raw = stream(
                    core_v1.connect_get_namespaced_pod_exec,
                    pod_name,
                    ns,
                    container='agent',
                    command=[
                        'sh', '-c',
                        'curl -fsS --max-time 3 http://127.0.0.1:8080/health',
                    ],
                    stderr=True,
                    stdin=False,
                    stdout=True,
                    tty=False,
                )
                # The websocket client may return JSON, a native dict, or a
                # string containing the dict's Python repr. Normalize all
                # mapping shapes without weakening the quiet-boundary check.
                health = _decode_exec_mapping(health_raw)
                if not isinstance(health, dict) or health.get('busy') is not False:
                    return False
                if observation == 0:
                    time.sleep(0.2)
            return True
        if status < 200 or status >= 300:
            return False
        response = _decode_exec_mapping(body)
        if return_receipt:
            # One arm, then read-only observations within the SAME lease.
            # Re-POSTing while busy would extend the runtime's lease each time.
            armed_until = response.get('leaseUntil')
            def valid_reservation(snapshot):
                return (isinstance(snapshot, dict)
                        and snapshot.get('requestId') == request_id
                        and snapshot.get('targetImage') == target_image
                        and snapshot.get('protocol') in {1, 2}
                        and not isinstance(armed_until, bool)
                        and isinstance(armed_until, (int, float))
                        and math.isfinite(armed_until)
                        and snapshot.get('leaseUntil') == armed_until
                        and armed_until > time.time() * 1000
                        and time.monotonic() < lease_started + 60)
            if not valid_reservation(response):
                return False
            while response.get('state') == 'draining':
                if continue_wait is None or not continue_wait():
                    return False
                time.sleep(min(1.0, max(0, (armed_until - time.time() * 1000) / 1000)))
                if not valid_reservation(response):
                    return False
                current = core_v1.read_namespaced_pod(pod_name, ns)
                # PLS-1247: a degenerate pod read (metadata/status None) used to
                # surface only as "live fence failed: AttributeError" — one bare
                # class name, unattributable across this 100+ line try block.
                # Fail closed with an attributable message instead.
                if current.metadata is None or current.status is None:
                    logger.info(
                        'Native runtime roll deferred for %s: pod re-read lacked metadata/status',
                        agent.agent_username,
                    )
                    return False
                ids = [s.container_id for s in (current.status.container_statuses or [])
                       if s.name == 'agent' and s.ready]
                if current.metadata.uid != pod_uid or current.metadata.deletion_timestamp or ids != container_ids:
                    return False
                raw = stream(core_v1.connect_get_namespaced_pod_exec, pod_name, ns,
                             container='agent', command=['sh', '-c',
                             'curl -fsS --max-time 3 http://127.0.0.1:8080/health'],
                             stderr=True, stdin=False, stdout=True, tty=False)
                response = _decode_exec_mapping(raw).get('runtimeRollDrain')
                if not valid_reservation(response):
                    return False
        if not (
            isinstance(response, dict)
            and response.get('requestId') == request_id
            and response.get('targetImage') == target_image
            and response.get('state') == 'ready'
            and response.get('busy') is False
            and response.get('acceptingTurns') is False
        ):
            return False
        protocol = response.get('protocol')
        if protocol == 1:
            allowed = response.get('pendingAcceptedTurns') == 0
        # drain-v2: the bridge FENCES ingress at the safe boundary and — by
        # design — RETAINS the checkpointed in-flight inbox row so the upstream
        # exchange redelivers to the replacement pod ("exact inbox row
        # retained"). Requiring protocol==1 AND pendingAcceptedTurns==0 here
        # meant any agent that was mid-turn when the drain armed (exactly the
        # agents the drain protocol exists for) could NEVER pass this fence:
        # the fenced inbox admits nothing new, the retained row is never
        # processed while drained, and the 60s lease just expired and re-armed
        # forever. Kumo sat wedged in that loop for 33 hours on a 2-day-old
        # harness with 4 ready tasks (operator 2026-08-06: "let's fix whatever
        # is wrong with kumo"). With ingress provably fenced, retained rows are
        # safe by construction — that is the entire point of the fence.
        elif protocol == 2:
            allowed = response.get('ingressFenced') is True
        else:
            return False
        if not allowed:
            return False
        if return_receipt:
            lease_until = response.get('leaseUntil')
            if (isinstance(lease_until, bool) or not isinstance(lease_until, (int, float))
                    or not math.isfinite(lease_until) or lease_until <= time.time() * 1000):
                return False
            if protocol == 1:
                # Protocol 1 cannot echo a reservation on /health and cannot
                # set ingressFenced. Requiring protocol 2 here stranded every
                # seat still on that bridge: the image that speaks protocol 2
                # is the image the fence would not let them receive.
                # pendingAcceptedTurns != 0 already returned above (a retained
                # inbox is not an idle boundary).
                version = -1
            else:
                version = response.get('admissionVersion')
                if (protocol != 2 or response.get('ingressFenced') is not True
                        or isinstance(version, bool) or not isinstance(version, int) or version < 0):
                    return False
            return NativeRuntimeRollReceipt(pod_name, pod_uid, target_image, deployment_uid,
                                            source_generation, document_hash, request_id,
                                            lease_until, lease_started + 60, container_ids[0], version)
        return True
    except Exception as exc:  # noqa: BLE001 - rollout safety must fail closed
        # PLS-1247: the bare class name made every fence defer unattributable
        # ("live fence failed: AttributeError" told us nothing about which leg
        # of the fence raised). repr() keeps fail-closed while making the next
        # occurrence diagnosable from the log alone.
        logger.info(
            'Native runtime roll deferred for %s: live fence failed: %r',
            agent.agent_username,
            exc,
        )
        return False


def _replace_native_runtime(agent, core, apps, proposed, original, authority, *, require_start_lease=False,
                            placement=None) -> bool:
    """Authorize one native template CAS; no compatibility health-only fence."""
    from .native_runtime_release import (
        assert_owned_deployment, validate_applied_release, assert_still_current,
        intent_guard, stamp_release,
    )
    ns = _live_ns(agent)
    assert_owned_deployment(agent, original)
    adopted = validate_applied_release(authority, original, allow_cold=True)
    replicas_changed = proposed.spec.replicas != original.spec.replicas
    if replicas_changed:
        if original.spec.replicas != 0 or proposed.spec.replicas != 1:
            return False
        require_start_lease = True
    if not adopted and not require_start_lease:
        return False
    template_changed = proposed.spec.template.to_dict() != original.spec.template.to_dict()
    runtime_changed = template_changed or replicas_changed
    receipt = None

    def same_intent():
        assert_still_current(authority)
        current = apps.read_namespaced_deployment(agent.deployment_name, ns, _request_timeout=(3, 5))
        assert_owned_deployment(agent, current)
        if (current.metadata.uid, current.metadata.resource_version) != (
                original.metadata.uid, original.metadata.resource_version):
            raise ApiException(status=409, reason='Native Deployment changed before release mutation')
        with intent_guard(agent) as desired:
            if desired and (desired.desired_lifecycle in {'deleted', 'archived'} or (
                    desired.expected_deployment_uid and desired.expected_deployment_uid != original.metadata.uid)):
                return False
            if require_start_lease and desired and desired.desired_lifecycle != 'active':
                return False
        return True

    if not same_intent():
        return False
    # An already-running replica is not a wake. Start-lease exists to stop
    # 0→1 boots into a full pool; it must not strand a live home on an old
    # harness because the seat is an idle non-holder (operator: do not yield
    # those homes, and stall-class harness upgrades still have to land).
    rolling_in_place = (
        not replicas_changed
        and original.spec.replicas == 1
        and proposed.spec.replicas == 1
    )

    def lease_still_allows_start() -> bool:
        return rolling_in_place or _lease_start_decision(agent).allowed

    if runtime_changed:
        pods = core.list_namespaced_pod(ns, label_selector=f'shizuha.io/agent={agent.agent_username}').items
        pods = _native_runtime_process_pods(pods, apps, ns, original.metadata.uid)
        if pods:
            if original.spec.replicas != 1 or not lease_still_allows_start():
                return False
            receipt = native_agent_runtime_roll_ready(
                agent, authority.release.deployment_image,
                deployment_uid=original.metadata.uid, return_receipt=True,
                source_generation=authority.release.generation, document_hash=authority.document_hash,
                continue_wait=lambda: same_intent() and lease_still_allows_start(),
            )
            if not isinstance(receipt, NativeRuntimeRollReceipt):
                return False
        elif original.spec.replicas != 0:
            # No running bridge is not evidence of an idle boundary. A pending
            # replica can admit work while this controller is rendering.
            return False
        stamp_release(authority, proposed)

    with intent_guard(agent) as desired:
        if not same_intent():
            return False
        if placement is not None:
            from .placement_contract import desired_placement, placement_changed, PlacementUnavailable
            fresh_placement = desired_placement(agent, proposed.spec.template.spec, core)
            if (fresh_placement['binding'] != placement['binding']
                    or placement_changed(proposed.spec.template.spec, fresh_placement)):
                raise PlacementUnavailable('Workspace placement changed before runtime replacement')
        if runtime_changed:
            pods = core.list_namespaced_pod(ns, label_selector=f'shizuha.io/agent={agent.agent_username}').items
            if receipt is None:
                if require_start_lease and not _lease_start_decision(agent).allowed:
                    return False
                assert_still_current(authority)
                pods = core.list_namespaced_pod(ns, label_selector=f'shizuha.io/agent={agent.agent_username}',
                                                _request_timeout=(3, 5)).items
                pods = _native_runtime_process_pods(pods, apps, ns, original.metadata.uid)
                if pods or original.spec.replicas != 0:
                    return False
                # Owner lookups above may block; intent/CAS do not lock the
                # external release document or model-start lease.
                if require_start_lease and not _lease_start_decision(agent).allowed:
                    return False
                assert_still_current(authority)
            else:
                # Finish every source/admission read before collecting the
                # final bridge/process proof; those reads can block long enough
                # for a same-Pod process restart or a different drain arm.
                if not lease_still_allows_start():
                    return False
                assert_still_current(authority)
                pods = core.list_namespaced_pod(ns, label_selector=f'shizuha.io/agent={agent.agent_username}',
                                                _request_timeout=(3, 5)).items
                pods = _native_runtime_process_pods(pods, apps, ns, original.metadata.uid)
                if len(pods) != 1:
                    return False
                pod = pods[0]
                ids = [s.container_id for s in (pod.status.container_statuses or [])
                       if s.name == 'agent' and s.ready]
                images = [c.image for c in pod.spec.containers if c.name == 'agent']
                original_images = [c.image for c in original.spec.template.spec.containers if c.name == 'agent']
                if (pod.metadata.uid != receipt.pod_uid or pod.metadata.deletion_timestamp
                        or ids != [receipt.container_id] or images != original_images
                        or (desired and desired.desired_lifecycle != 'active')):
                    return False
                try:
                    raw = stream(core.connect_get_namespaced_pod_exec, receipt.pod_name, ns,
                                 container='agent', command=['sh', '-c',
                                 'curl -fsS --max-time 3 http://127.0.0.1:8080/health'],
                                 stderr=True, stdin=False, stdout=True, tty=False)
                    proof_body = _decode_exec_mapping(raw)
                    if receipt.admission_version < 0:
                        # Protocol 1. /health.runtimeRollDrain stays null on
                        # the bridge that needs this upgrade (Nova, gen 50).
                        drain = proof_body.get('runtimeRollDrain')
                        if isinstance(drain, dict):
                            held = (drain.get('protocol') == 1
                                    and drain.get('pendingAcceptedTurns') == 0
                                    and drain.get('state') == 'ready'
                                    and drain.get('busy') is False
                                    and drain.get('acceptingTurns') is False
                                    and drain.get('requestId') == receipt.request_id
                                    and drain.get('targetImage') == receipt.target_image
                                    and drain.get('leaseUntil') == receipt.lease_until_ms)
                        else:
                            held = (proof_body.get('busy') is False
                                    and proof_body.get('queueDepth') in (0, None))
                        if not held:
                            return False
                    else:
                        proof = proof_body.get('runtimeRollDrain') or {}
                        if not (proof.get('protocol') == 2 and proof.get('ingressFenced') is True
                                and proof.get('state') == 'ready' and proof.get('busy') is False
                                and proof.get('acceptingTurns') is False
                                and proof.get('requestId') == receipt.request_id
                                and proof.get('targetImage') == receipt.target_image
                                and proof.get('leaseUntil') == receipt.lease_until_ms
                                and proof.get('admissionVersion') == receipt.admission_version):
                            return False
                    final_pod = core.read_namespaced_pod(receipt.pod_name, ns, _request_timeout=(3, 5))
                    if (final_pod.metadata.uid != receipt.pod_uid or final_pod.metadata.deletion_timestamp
                            or [s.container_id for s in (final_pod.status.container_statuses or [])
                                if s.name == 'agent' and s.ready] != [receipt.container_id]):
                        return False
                except Exception:
                    return False
                # All possibly blocking reads precede both clock checks.
                if (time.monotonic() >= receipt.expires_monotonic
                        or time.time() * 1000 >= receipt.lease_until_ms):
                    return False
        proposed.metadata.resource_version = original.metadata.resource_version
        if receipt is not None:
            remaining = min(receipt.expires_monotonic - time.monotonic(),
                            (receipt.lease_until_ms - time.time() * 1000) / 1000)
            if remaining <= 0:
                return False
            # Bound the request by the existing reservation. An ambiguous
            # transport outcome is not confirmation that the old Pod stopped;
            # publication separately verifies actual owned Pod image/Ready.
            apps.replace_namespaced_deployment(agent.deployment_name, ns, proposed,
                                              _request_timeout=remaining)
        else:
            apps.replace_namespaced_deployment(agent.deployment_name, ns, proposed)
    return True


def native_agent_runtime_contract_drift(agent: FleetAgent, target_image: str) -> bool:
    """Return whether a Hive-native Deployment misses its managed contract.

    Persisting ``FleetAgent.image`` is not convergence evidence: an earlier
    patch can fail after the row is written, and legacy native Deployments also
    predate the runtime label consumed by the fleet PodMonitor.  Read the live
    object so the existing idle-fenced reconciler can repair both classes.
    Any read/schema failure is drift (fail closed into the retry path).
    """
    try:
        _core_v1, apps_v1, _networking_v1 = get_k8s_client()
        deploy = apps_v1.read_namespaced_deployment(
            agent.deployment_name, _live_ns(agent),
        )
        labels = dict(getattr(deploy.metadata, 'labels', None) or {})
        annotations = dict(getattr(deploy.metadata, 'annotations', None) or {})
        template_labels = dict(
            getattr(getattr(deploy.spec, 'template', None).metadata, 'labels', None) or {}
        )
        containers = list(deploy.spec.template.spec.containers or [])
        container = next(
            (item for item in containers if item.name in {'agent', agent.agent_username}),
            None,
        )
        ports = {
            (getattr(port, 'name', None), getattr(port, 'container_port', None))
            for port in (getattr(container, 'ports', None) or [])
        } if container is not None else set()
        from .placement_contract import desired_placement, placement_changed
        from .native_runtime_release import enabled as native_release_enabled
        placement_drift = native_release_enabled() and _uses_native_runtime_image(agent) and placement_changed(
            deploy.spec.template.spec,
            desired_placement(agent, deploy.spec.template.spec, _core_v1),
        )
        return bool(
            labels.get(NATIVE_RUNTIME_LABEL) != NATIVE_RUNTIME_LABEL_VALUE
            or annotations.get(NATIVE_RUNTIME_OWNER_ANNOTATION) != NATIVE_RUNTIME_OWNER
            or template_labels.get(NATIVE_RUNTIME_LABEL) != NATIVE_RUNTIME_LABEL_VALUE
            or container is None
            or str(getattr(container, 'image', '') or '') != str(target_image or '')
            or ('metrics', NATIVE_RUNTIME_METRICS_PORT) not in ports
            # Legacy shared-key credential refs are contract drift: without
            # this, a stale cortex-fleet-key Deployment stays "converged" and
            # bills its traffic onto the shared fleet principal forever.
            or _cortex_key_env_ref_drifted(container, agent)
            or _forgejo_env_drifted(container, agent, _core_v1)
            # Hive-owned lane env (MODEL, REASONING_EFFORT, and the retired
            # MODEL_FALLBACKS / FAILOVER_CHAIN_ID) must match Hive's render
            # EXACTLY, including absence. Otherwise a live agent keeps running
            # config Hive no longer declares and nothing ever notices —
            # operator 2026-08-06: "hive should sync those changes to the agent
            # asap ... permanently ensuring that hive's truth is obeyed".
            or _lane_env_drifted(container, agent)
            # A sidecar alone is not a working broker. Keep incomplete legacy
            # consumers in the existing idle-fenced reconciliation path.
            or _broker_contract_drifted(deploy.spec.template.spec, container, agent)
            or placement_drift
        )
    except Exception as exc:  # noqa: BLE001 - retry rather than assert false convergence
        logger.info(
            'Native runtime contract drift probe failed for %s: %s',
            agent.agent_username,
            type(exc).__name__,
        )
        return True


CORTEX_KEY_ENV_NAMES = ('OPENAI_API_KEY', 'VLLM_API_KEY', 'CORTEX_API_KEY')


def _per_agent_cortex_key_env(agent: FleetAgent) -> list:
    """The per-agent Cortex credential env contract (single source).

    Every env alias resolves from ``<username>-agent-creds``/CORTEX_API_KEY.
    NOT optional (operator 2026-08-05: "you could make this mandatory") — with
    optional=True a missing key resolved to an empty string and the agent ran
    indefinitely 401ing every turn (rei, ~5 weeks). And never the shared
    ``cortex-fleet-key`` (operator 2026-08-06: "whoever is using cortex must
    use their own cortex account") — ten pre-fix Deployments still carried
    that legacy ref, so kumo/kane billed 4.6M grok tokens onto
    ``agents-fleet-own-fleet`` the moment they rolled.
    """
    _creds = f'{agent.agent_username}-agent-creds'
    return [
        client.V1EnvVar(
            name=name,
            value_from=client.V1EnvVarSource(
                secret_key_ref=client.V1SecretKeySelector(
                    name=_creds, key='CORTEX_API_KEY', optional=False,
                ),
            ),
        )
        for name in CORTEX_KEY_ENV_NAMES
    ]


def _lane_env_drifted(container, agent: FleetAgent) -> bool:
    """True when live Hive-owned lane env differs from what Hive renders.

    Compares the FULL set both ways, so a key Hive stopped rendering (a
    retired MODEL_FALLBACKS chain) is drift exactly like a changed MODEL.
    """
    try:
        desired_container = _agent_container(
            agent,
            has_broker='broker' in (agent.sidecar_set or []),
            has_dind='dind' in (agent.sidecar_set or []),
        )
    except Exception:  # noqa: BLE001 — a render failure is not evidence of health
        return True
    desired = {
        e.name: (e.value or '')
        for e in (desired_container.env or [])
        if e.name in _RUNTIME_LANE_ENV_NAMES
    }
    current = {
        e.name: (getattr(e, 'value', '') or '')
        for e in (getattr(container, 'env', None) or [])
        if e.name in _RUNTIME_LANE_ENV_NAMES
    }
    return desired != current


def _cortex_key_env_ref_drifted(container, agent: FleetAgent) -> bool:
    """True when the live container's Cortex key refs miss the contract."""
    expected_secret = f'{agent.agent_username}-agent-creds'
    by_name = {getattr(e, 'name', None): e for e in (getattr(container, 'env', None) or [])}
    for name in CORTEX_KEY_ENV_NAMES:
        e = by_name.get(name)
        ref = getattr(getattr(e, 'value_from', None), 'secret_key_ref', None) if e else None
        if (
            ref is None
            or getattr(ref, 'name', None) != expected_secret
            or getattr(ref, 'key', None) != 'CORTEX_API_KEY'
            or getattr(ref, 'optional', None) is not False
        ):
            return True
    return False


def _converge_cortex_key_env(container, agent: FleetAgent) -> bool:
    """Rewrite a legacy container env to the per-agent credential contract."""
    if not _cortex_key_env_ref_drifted(container, agent):
        return False
    keep = [
        e for e in (getattr(container, 'env', None) or [])
        if getattr(e, 'name', None) not in CORTEX_KEY_ENV_NAMES
    ]
    container.env = keep + _per_agent_cortex_key_env(agent)
    return True


# PLAT-6200: identity env keys the broker sidecar must source from the per-agent
# identity Secret (the identity SoT) — never from a legacy shared creds Secret.
BROKER_IDENTITY_ENV_NAMES = frozenset({
    'AGENT_ID', 'AGENT_USERNAME', 'AGENT_PASSWORD', 'ACCESS_TOKEN',
})


def _converge_broker_identity_env(container, agent: FleetAgent) -> bool:
    """Rewrite a stale broker env to the per-agent identity Secret (PLAT-6200).

    Pre-fix native Deployments (e.g. Sara) injected AGENT_PASSWORD via an
    explicit ``secretKeyRef`` on a legacy shared creds Secret (sara-agent-creds)
    while PLAT-4884's identity SoT is the per-agent identity Secret
    (agent.identity_secret_name). After a Hive enable/model bounce the two
    drifted (33- vs 32-char password) and the broker 401'd on every ID mint.
    ``ensure_agent_image`` converged image/labels but not this env wiring, so
    the stale template never healed.

    This converges the broker container to the identity SoT: drop any explicit
    identity env entries that reference a different Secret and ensure
    ``envFrom: [identity_secret_name]`` is present. Pure env rewrite — no
    replicas, no image change, no policy change.
    """
    env = list(getattr(container, 'env', None) or [])
    env_from = list(getattr(container, 'env_from', None) or [])
    changed = False
    # Drop explicit identity env entries sourced from a NON-identity Secret.
    kept = []
    for e in env:
        name = getattr(e, 'name', None)
        if name in BROKER_IDENTITY_ENV_NAMES:
            ref = getattr(getattr(e, 'value_from', None), 'secret_key_ref', None)
            ref_name = getattr(ref, 'name', None) if ref else None
            if ref_name and ref_name != agent.identity_secret_name:
                logger.info(
                    'PLAT-6200: broker %s env %s sourced from %s (not identity '
                    'Secret %s); rewriting to identity SoT',
                    agent.agent_username, name, ref_name, agent.identity_secret_name,
                )
                changed = True
                continue
        kept.append(e)
    if len(kept) != len(env):
        container.env = kept
    # Ensure envFrom references the identity Secret.
    if not any(
        getattr(getattr(s, 'secret_ref', None), 'name', None) == agent.identity_secret_name
        for s in env_from
    ):
        env_from.append(
            client.V1EnvFromSource(
                secret_ref=client.V1SecretEnvSource(name=agent.identity_secret_name),
            )
        )
        container.env_from = env_from
        changed = True
    return changed


def _broker_contract_volumes() -> list[client.V1Volume]:
    return [
        client.V1Volume(name='broker-sock', empty_dir=client.V1EmptyDirVolumeSource()),
        client.V1Volume(
            name='mcp-projection-keyring',
            secret=client.V1SecretVolumeSource(
                secret_name=MCP_PROJECTION_PUBLIC_SECRET,
                default_mode=0o444,
                items=[client.V1KeyToPath(key='keyring.json', path='keyring.json')],
            ),
        ),
    ]


def _broker_contract_drifted(spec, container, agent: FleetAgent) -> bool:
    has_broker = any(c.name == 'broker' for c in spec.containers or [])
    if not has_broker:
        return _uses_native_runtime_image(agent) or 'broker' in (agent.sidecar_set or [])
    broker = next(c for c in spec.containers if c.name == 'broker')
    desired_broker = _broker_sidecar(agent)
    if broker.image != desired_broker.image or _converge_broker_producer_contract(deepcopy(broker), desired_broker):
        return True
    volumes = {v.name for v in (getattr(spec, 'volumes', None) or [])}
    if any(volume.name not in volumes for volume in _broker_contract_volumes()):
        return True
    desired = _agent_container(
        agent, has_broker=True,
        has_dind=any(c.name == 'dind' for c in spec.containers or []),
    )
    # Reuse the exact convergence contract without mutating the observed pod.
    # Conflicts propagate to the caller's fail-closed drift handling.
    return _converge_broker_consumer_contract(deepcopy(container), desired)


def _converge_broker_producer_contract(container, desired) -> bool:
    """Keep the signed-projection producer wired to the same native contract.

    A healthy token broker can predate /mcp-projection. Checking only the
    consumer socket leaves its multiplexer permanently fenced with no tools.
    The ordinary native rollout fences this template change at an idle turn.
    """
    owned = {
        'AGENT_ID', 'MCP_AUTH_PROXY_ID_BASE_URL', 'MCP_AUTH_PROXY_EXPECTED_AGENT_UID',
        'MCP_AUTH_PROXY_SOCKET', 'MCP_AUTH_PROXY_PROJECTION_URL',
        'MCP_AUTH_PROXY_COORDINATOR_URL', 'MCP_AUTH_PROXY_COORDINATOR_TOKEN',
        'MCP_AUTH_PROXY_COORDINATOR_OAUTH_TOKEN_URL', 'MCP_AUTH_PROXY_COORDINATOR_OAUTH_CLIENT_ID',
        'MCP_AUTH_PROXY_COORDINATOR_OAUTH_CLIENT_SECRET', 'MCP_AUTH_PROXY_COORDINATOR_OAUTH_SCOPE',
    }
    env = list(getattr(container, 'env', None) or [])
    wanted = [item for item in desired.env or [] if item.name in owned]
    observed = [item for item in env if item.name in owned]
    if ([item.to_dict() for item in sorted(observed, key=lambda item: item.name)]
            == [item.to_dict() for item in sorted(wanted, key=lambda item: item.name)]):
        return False
    container.env = [item for item in env if item.name not in owned] + wanted
    return True


def _converge_broker_consumer_contract(container, desired_container) -> bool:
    """Complete the broker consumer wiring without replacing unrelated config."""
    changed = False
    mounts = list(getattr(container, 'volume_mounts', None) or [])
    for desired in desired_container.volume_mounts or []:
        if desired.name not in {'broker-sock', 'mcp-projection-keyring'}:
            continue
        matches = [mount for mount in mounts if mount.mount_path == desired.mount_path]
        if len(matches) > 1 or any(mount.name != desired.name for mount in matches):
            raise ValueError(f'reserved broker mount path conflicts: {desired.mount_path}')
        if not matches:
            mounts.append(desired)
            changed = True
        elif matches[0].to_dict() != desired.to_dict():
            mounts[mounts.index(matches[0])] = desired
            changed = True
    container.volume_mounts = mounts
    owned_env = {
        'MCP_AUTH_PROXY_SOCKET', 'AGENT_PASSWORD',
        'CLAUDE_BRIDGE_REQUIRE_BROKER_MODEL_TOKEN', 'CODEX_BRIDGE_REQUIRE_BROKER_MODEL_TOKEN',
    }
    env = list(container.env or [])
    for desired in desired_container.env or []:
        if desired.name not in owned_env:
            continue
        matches = [item for item in env if item.name == desired.name]
        if len(matches) != 1 or matches[0].to_dict() != desired.to_dict():
            env = [item for item in env if item.name != desired.name] + [desired]
            changed = True
    container.env = env
    # Managed native readiness belongs to the renderer. Custom images render
    # no broker probe and retain theirs; liveness/startup probes are untouched.
    if desired_container.readiness_probe is not None:
        current = getattr(container, 'readiness_probe', None)
        if current is None or current.to_dict() != desired_container.readiness_probe.to_dict():
            container.readiness_probe = desired_container.readiness_probe
            changed = True
    return changed


def ensure_agent_image(agent: FleetAgent, *, _preserve_native_intent: bool = False) -> bool:
    """Converge a native agent Deployment runtime contract.

    Hive owns the image, resource profile, and per-agent repository credential
    references. This is idempotent and does not change replicas; it can
    therefore stage an immutable harness tag or repair a legacy sleeping
    template safely before wake.
    """
    # HIVE-1502: refuse the roll rather than re-render an unstartable pod. The
    # render site raises on an empty agent_id, but this loop only catches
    # ApiException, so guard explicitly here to fail cleanly with a reason on the
    # row instead of propagating out of a retry loop. This is the path that
    # recreated the shizuha-116 pod on each of 9 image rolls.
    if not (agent.agent_id or '').strip():
        logger.error(
            'ensure_agent_image: refusing to converge %s — agent_id is empty; '
            'the rendered pod could never start (HIVE-1502)',
            agent.agent_username,
        )
        agent.status_message = (
            'agent_id is empty; image convergence refused because the rendered '
            'pod could never start. Set FleetAgent.agent_id via Hive (HIVE-1502).'
        )
        agent.save(update_fields=['status_message'])
        return False

    # Resolve and bind the exact reviewed digest before the first credential or
    # Kubernetes mutation. This function is also called outside start_agent
    # (for example while staging a stopped Deployment), so it must enforce the
    # authority boundary itself rather than trusting its caller.
    desired_runtime_image = (
        _bind_reviewed_runtime_image(agent, preserve_native_intent=True)
        if _preserve_native_intent else _bind_reviewed_runtime_image(agent)
    )
    native_authority = getattr(agent, '_native_runtime_release', None)
    if native_authority is not None:
        from .native_runtime_release import assert_owned_deployment, validate_applied_release
        # Ownership must be established before credential or object writes.
        _core, apps, _networking = get_k8s_client()
        try:
            owned = apps.read_namespaced_deployment(agent.deployment_name, _live_ns(agent))
        except ApiException as exc:
            if exc.status != 404:
                raise
            # Preserve the existing start→first-time provision fallback below;
            # absence grants no right to overwrite a concurrently created object.
        else:
            assert_owned_deployment(agent, owned)
            adopted = validate_applied_release(native_authority, owned, allow_cold=True)
            if not adopted and not _preserve_native_intent:
                # Publication/release convergence does not reconstruct dormant
                # unknown templates. Only an existing authorized start may do so.
                return False
    # In-memory only — ensure_agent_image is also called from SimpleTestCase
    # wakes that forbid DB writes. Persist happens on the lifecycle path.
    heal_stale_work_agent_heartbeat_env(agent)

    # Establish the per-agent Cortex credential BEFORE converging the pod. The
    # container's CORTEX_API_KEY ref is mandatory (optional=False), so a missing
    # Secret key now stops the pod from starting rather than producing an agent
    # that runs forever and 401s on every turn. Minting here is what keeps that
    # strictness safe: agents provisioned before per-agent keys existed heal
    # themselves on the next reconcile instead of needing a human.
    cortex_key_fp = ensure_agent_cortex_key(agent)
    try:
        forgejo_token_fp = ensure_agent_forgejo_token(agent)
    except Exception as exc:  # noqa: BLE001 — Origin mint must not block Cortex boot
        logger.error('forgejo-token: image-converge mint failed for %s: %s', agent.agent_username, exc)
        forgejo_token_fp = ''

    core_v1, apps_v1, _n = get_k8s_client()
    ns = _live_ns(agent)
    # HIVE-1922: heal both namespace copies before the ordinary runtime-lane
    # converge rewrites the signer refs. This is the existing-agent recovery
    # path; a one-time reprovision is not required.
    if agent_needs_browser_mcp(agent):
        if not _ensure_browser_mcp_jwt_secrets(core_v1, agent):
            agent.status_message = (
                'browser MCP JWT source missing; runtime convergence refused '
                'because signer/verifier equality could not be established '
                '(HIVE-1922)'
            )
            agent.save(update_fields=['status_message'])
            return False
        if not provision_browser_worker(agent, jwt_secret_ready=True):
            agent.status_message = (
                'private browser worker convergence failed; runtime convergence '
                'refused before signer rollout (HIVE-1922)'
            )
            agent.save(update_fields=['status_message'])
            return False
    # HIVE-1377: heal the coordinator broker token projection for agents
    # provisioned before this fix (agent-shizuha-116). Same fail-closed rule:
    # missing shared source → refuse convergence (the broker could not start
    # anyway) and surface the reason on the row instead of re-rendering a pod
    # that crash-loops in CreateContainerConfigError.
    if not _ensure_coordinator_broker_token(core_v1, ns, agent):
        agent.status_message = (
            'coordinator broker token Secret missing; image convergence refused '
            'because the broker sidecar could not start (HIVE-1377)'
        )
        agent.save(update_fields=['status_message'])
        return False
    if not _ensure_projection_keyring(core_v1, ns, agent):
        agent.status_message = 'MCP projection public keyring unavailable; image convergence refused (PLAT-5275)'
        agent.save(update_fields=['status_message'])
        return False
    stage = 'read_deployment'
    last_exc: ApiException | None = None
    for attempt in range(_RUNTIME_CONTRACT_PATCH_ATTEMPTS):
        stage = 'read_deployment'
        try:
            stage = 'forgejo_credential_selection'
            forgejo_secret_name, _secret = _forgejo_secret(agent, core_v1, ns)
            stage = 'read_deployment'
            deploy = apps_v1.read_namespaced_deployment(agent.deployment_name, ns)
            cold_materialization = False
            if native_authority is not None:
                assert_owned_deployment(agent, deploy)
                cold_materialization = not validate_applied_release(native_authority, deploy, allow_cold=True)
            original = deepcopy(deploy) if native_authority is not None else None
            from .placement_contract import desired_placement, converge_placement
            placement = None
            changed = cold_materialization
            if cold_materialization:
                # Unknown dormant images are not adopted. Reconstruct from the
                # qualified current source only at this existing lifecycle/config
                # action; final replacement still requires zero replicas/no potentially live Pod.
                deploy.spec.template = client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels=_base_labels(agent)), spec=_build_pod_spec(agent, forgejo_secret_name=forgejo_secret_name))
                deploy.spec.strategy = client.V1DeploymentStrategy(type='Recreate')
            if native_authority is not None:
                stage = 'placement'
                placement = desired_placement(agent, deploy.spec.template.spec, core_v1)
            if placement is not None:
                changed = converge_placement(deploy.spec.template.spec, placement) or changed
            deploy.metadata = getattr(deploy, 'metadata', None) or client.V1ObjectMeta()
            deploy.metadata.labels = dict(deploy.metadata.labels or {})
            if deploy.metadata.labels.get(NATIVE_RUNTIME_LABEL) != NATIVE_RUNTIME_LABEL_VALUE:
                deploy.metadata.labels[NATIVE_RUNTIME_LABEL] = NATIVE_RUNTIME_LABEL_VALUE
                changed = True
            deploy.metadata.annotations = dict(deploy.metadata.annotations or {})
            if deploy.metadata.annotations.get(NATIVE_RUNTIME_OWNER_ANNOTATION) != NATIVE_RUNTIME_OWNER:
                deploy.metadata.annotations[NATIVE_RUNTIME_OWNER_ANNOTATION] = NATIVE_RUNTIME_OWNER
                changed = True
            deploy.spec.template.metadata = (
                getattr(deploy.spec.template, 'metadata', None) or client.V1ObjectMeta()
            )
            deploy.spec.template.metadata.labels = dict(
                deploy.spec.template.metadata.labels or {},
            )
            if wants_voice_s2s(agent):
                if deploy.metadata.labels.get('shizuha.io/voice-s2s') != 'true':
                    deploy.metadata.labels['shizuha.io/voice-s2s'] = 'true'
                    changed = True
                if deploy.spec.template.metadata.labels.get('shizuha.io/voice-s2s') != 'true':
                    deploy.spec.template.metadata.labels['shizuha.io/voice-s2s'] = 'true'
                    changed = True
                _create_service(core_v1, ns, agent)
            if (
                deploy.spec.template.metadata.labels.get(NATIVE_RUNTIME_LABEL)
                != NATIVE_RUNTIME_LABEL_VALUE
            ):
                # Pod-template ownership + metrics discovery rolls only after
                # native_agent_runtime_roll_ready has fenced a running turn.
                deploy.spec.template.metadata.labels[NATIVE_RUNTIME_LABEL] = (
                    NATIVE_RUNTIME_LABEL_VALUE
                )
                changed = True
            desired_resources = _resolve_resources(agent)
            desired_broker_image = getattr(settings, 'FLEET_BROKER_IMAGE', None)
            containers = list(deploy.spec.template.spec.containers or [])
            has_broker = any(c.name == 'broker' for c in containers)
            has_dind = any(c.name == 'dind' for c in containers)
            # Empty sidecar_set on native SCLI seats dropped the broker; Pulse
            # preflight then 401'd and the old harness fail-opened into 200k
            # prefix heartbeats (aoi/kai/zen 2026-09-02: 39k input / 76 decode).
            if _uses_native_runtime_image(agent):
                ss = list(agent.sidecar_set or [])
                if 'broker' not in ss:
                    agent.sidecar_set = ['broker'] + [s for s in ss if s]
                    try:
                        agent.save(update_fields=['sidecar_set'])
                    except Exception:
                        pass
            want_broker = 'broker' in (agent.sidecar_set or [])
            if want_broker and not has_broker:
                spec = deploy.spec.template.spec
                spec.containers = list(spec.containers or []) + [_broker_sidecar(agent)]
                containers = list(spec.containers)
                has_broker = True
                changed = True
            if has_broker:
                spec = deploy.spec.template.spec
                vols = list(getattr(spec, 'volumes', None) or [])
                vol_names = {getattr(v, 'name', '') for v in vols}
                for volume in _broker_contract_volumes():
                    if volume.name not in vol_names:
                        vols.append(volume)
                        changed = True
                spec.volumes = vols
            _SIDECAR_MODEL_ENV = {
                'MODEL', 'SHIZUHA_K8S_PRIMARY_MODEL',
                'MODEL_FALLBACKS', 'SHIZUHA_MODEL_FALLBACKS',
            }

            def _drop_sidecar_model_pins(container) -> bool:
                sidecar_env = list(getattr(container, 'env', None) or [])
                cleaned = [
                    item for item in sidecar_env
                    if item.name not in _SIDECAR_MODEL_ENV
                ]
                if len(cleaned) == len(sidecar_env):
                    return False
                container.env = cleaned
                return True

            for container in deploy.spec.template.spec.containers or []:
                if container.name == 'broker':
                    if desired_broker_image and container.image != desired_broker_image:
                        container.image = desired_broker_image
                        changed = True
                    if _converge_broker_producer_contract(container, _broker_sidecar(agent)):
                        changed = True
                    # PLAT-6200: heal stale broker env that sources identity
                    # env (AGENT_PASSWORD etc.) from a legacy shared creds
                    # Secret instead of the per-agent identity SoT.
                    if _converge_broker_identity_env(container, agent):
                        changed = True
                    if _drop_sidecar_model_pins(container):
                        changed = True
                        changed = True
                    continue
                if container.name not in {'agent', agent.agent_username}:
                    # Sidecars historically copied SHIZUHA_MODEL_FALLBACKS
                    # (retired Qwen/GLM chains). They are not a runtime lane.
                    if _drop_sidecar_model_pins(container):
                        changed = True
                    continue
                if container.image != desired_runtime_image:
                    container.image = desired_runtime_image
                    container.image_pull_policy = agent.image_pull_policy
                    changed = True
                current_resources = getattr(container, 'resources', None)
                current_contract = {
                    'requests': dict(getattr(current_resources, 'requests', None) or {}),
                    'limits': dict(getattr(current_resources, 'limits', None) or {}),
                }
                desired_contract = {
                    'requests': dict(desired_resources.requests or {}),
                    'limits': dict(desired_resources.limits or {}),
                }
                if current_contract != desired_contract:
                    container.resources = desired_resources
                    changed = True
                # Credential contract convergence: ten pre-fix Deployments kept
                # the legacy shared cortex-fleet-key ref through every image
                # roll because this converge only touched image/resources — so
                # the moment those agents woke, their usage billed onto the
                # shared agents-fleet-own-fleet principal (operator 2026-08-06).
                if _converge_cortex_key_env(container, agent):
                    changed = True
                # A model/runtime-lane edit is Hive desired state, just like the
                # image and resource contract above. Re-render only the lane
                # environment keys so a stopped native template cannot wake on
                # its previous model, while preserving unrelated live env.
                desired_container = _agent_container(
                    agent, has_broker=has_broker, has_dind=has_dind,
                    forgejo_secret_name=forgejo_secret_name,
                )
                if has_broker:
                    try:
                        if _converge_broker_consumer_contract(container, desired_container):
                            changed = True
                    except ValueError as exc:
                        agent.status_message = (
                            f'Runtime contract reconciliation failed for agent '
                            f'{agent.agent_username} at stage broker_consumer '
                            f'({exc}); wake aborted before scale.'
                        )
                        agent.save(update_fields=['status_message'])
                        _log_event(agent, 'runtime_contract_reconcile_failed', agent.status_message)
                        return False
                desired_named_ports = {
                    item.name: item
                    for item in (desired_container.ports or [])
                    if item.name in {'http', 'metrics'}
                }
                current_ports = list(getattr(container, 'ports', None) or [])
                retained_ports = [
                    item for item in current_ports
                    if getattr(item, 'name', None) not in desired_named_ports
                ]
                desired_ports = retained_ports + list(desired_named_ports.values())
                if (
                    [item.to_dict() for item in current_ports]
                    != [item.to_dict() for item in desired_ports]
                ):
                    container.ports = desired_ports
                    changed = True
                desired_lane_env = {
                    item.name: item
                    for item in (desired_container.env or [])
                    if item.name in _RUNTIME_LANE_ENV_NAMES
                }
                current_env = list(getattr(container, 'env', None) or [])
                current_lane_env = {
                    item.name: item
                    for item in current_env
                    if item.name in _RUNTIME_LANE_ENV_NAMES
                }
                current_lane_dict = {
                    name: item.to_dict() for name, item in current_lane_env.items()
                }
                desired_lane_dict = {
                    name: item.to_dict() for name, item in desired_lane_env.items()
                }
                if current_lane_dict != desired_lane_dict:
                    container.env = [
                        item for item in current_env
                        if item.name not in _RUNTIME_LANE_ENV_NAMES
                    ] + list(desired_lane_env.values())
                    changed = True
                # The bridge command is part of the same model/runtime-lane
                # contract as MODEL. Harness promotion persists an immutable
                # native runtime tag in ``agent.image``; a later lane edit must
                # therefore reconcile both fields atomically. Otherwise a GPT
                # model can retain a DeepSeek-era ``gateway`` command and fail
                # local Codex auth before the broker-backed codex bridge starts.
                # Custom images retain complete ownership of their entrypoint.
                if _uses_native_runtime_image(agent):
                    current_command = list(getattr(container, 'command', None) or [])
                    desired_command = list(desired_container.command or [])
                    if current_command != desired_command:
                        container.command = desired_container.command
                        changed = True
                # ORIG-18 / PLAT-4683: legacy sleeping native agents predate the
                # per-agent Forgejo refs. Wake must converge those refs before scale
                # so a hibernated agent cannot start without its own Origin identity.
                # Both aliases deliberately read the canonical FORGEJO_TOKEN key,
                # matching the runtime-fleet renderer. The ref stays optional so
                # agents without an Origin grant retain their existing fail-soft
                # runtime behaviour instead of failing Pod admission.
                current_env = list(getattr(container, 'env', None) or [])
                for desired_env in _forgejo_credential_env(agent, secret_name=forgejo_secret_name):
                    same_name = [item for item in current_env if item.name == desired_env.name]
                    if len(same_name) == 1 and _secret_env_matches(same_name[0], desired_env):
                        continue
                    current_env = [item for item in current_env if item.name != desired_env.name]
                    current_env.append(desired_env)
                    changed = True
                container.env = current_env
            # A Secret patch is invisible to a running Pod: env vars are resolved
            # once, at container start. Stamping the key's fingerprint on the pod
            # template turns a freshly minted key into a rollout, so the agent
            # picks it up instead of 401-ing until something else happens to
            # restart it. The value is a hash, not the key, and is stable across
            # reconciles — so this cannot churn pods on every pass.
            if cortex_key_fp or forgejo_token_fp:
                annotations = dict(deploy.spec.template.metadata.annotations or {})
                if cortex_key_fp and annotations.get(CORTEX_KEY_FINGERPRINT_ANNOTATION) != cortex_key_fp:
                    annotations[CORTEX_KEY_FINGERPRINT_ANNOTATION] = cortex_key_fp
                    changed = True
                if forgejo_token_fp and annotations.get(FORGEJO_TOKEN_FINGERPRINT_ANNOTATION) != forgejo_token_fp:
                    annotations[FORGEJO_TOKEN_FINGERPRINT_ANNOTATION] = forgejo_token_fp
                    changed = True
                deploy.spec.template.metadata.annotations = annotations
            if native_authority is not None:
                # Every init using this runtime must move with the main binary.
                from .runtime_release import is_managed_runtime_image_ref
                for init in deploy.spec.template.spec.init_containers or []:
                    if is_managed_runtime_image_ref(init.image) and init.image != desired_runtime_image:
                        init.image = desired_runtime_image
                        changed = True
            if changed:
                stage = 'patch_deployment'
                if native_authority is not None:
                    if not _replace_native_runtime(agent, core_v1, apps_v1, deploy, original, native_authority,
                                                   require_start_lease=_preserve_native_intent, placement=placement):
                        return False
                else:
                    # Full replace removes retired env entries; resourceVersion
                    # preserves the existing 409 conflict/re-read contract.
                    apps_v1.replace_namespaced_deployment(agent.deployment_name, ns, deploy)
                _log_event(
                    agent,
                    'backend_applied',
                    f'Deployment runtime contract reconciled for image '
                    f'{desired_runtime_image!r}',
                )
            _clear_runtime_contract_failure_message(agent)
            return True
        except ValueError as exc:
            agent.status_message = (
                f'Runtime contract reconciliation failed for agent {agent.agent_username} '
                f'at stage {stage}: {exc}; wake aborted before scale.'
            )
            agent.save(update_fields=['status_message'])
            _log_event(agent, 'runtime_contract_reconcile_failed', agent.status_message)
            return False
        except ApiException as exc:
            last_exc = exc
            status = getattr(exc, 'status', None)
            if (
                status in {409, 503}
                and attempt + 1 < _RUNTIME_CONTRACT_PATCH_ATTEMPTS
            ):
                logger.warning(
                    'Runtime contract reconcile hit HTTP %s for agent %s at stage %s '
                    '(attempt %s/%s); re-reading Deployment and retrying',
                    status,
                    agent.agent_username,
                    stage,
                    attempt + 1,
                    _RUNTIME_CONTRACT_PATCH_ATTEMPTS,
                )
                continue
            break

    assert last_exc is not None
    message = _runtime_contract_failure_message(agent, stage, last_exc)
    logger.error('%s', message)
    # PLAT-4683 / PLAT-1254: wake is fail-closed before scale, but the
    # failure must also be visible outside a pod log. Persist both the
    # current status message and an append-only event, then increment the
    # alert-backed owner-notification signal exactly once for this attempt.
    agent.status_message = message
    agent.save(update_fields=['status_message'])
    _log_event(agent, 'runtime_contract_reconcile_failed', message)
    _notify_runtime_contract_reconcile_failure(stage)
    return False


def _is_runtime_contract_failure_message(message: str | None) -> bool:
    return str(message or '').startswith(
        'Runtime contract reconciliation failed for agent ',
    )


def _clear_runtime_contract_failure_message(agent: FleetAgent) -> None:
    """Drop a sticky reconcile-failure banner once the contract is healthy again.

    Harness staging and intentional hibernate both call ``ensure_agent_image``
    successfully after a transient 409. Without this clear, the Agents UI keeps
    showing "wake aborted before scale" on a correctly stopped agent (sato).
    """
    if not _is_runtime_contract_failure_message(getattr(agent, 'status_message', '')):
        return
    agent.status_message = ''
    agent.save(update_fields=['status_message'])


def _runtime_contract_failure_message(
    agent: FleetAgent, stage: str, exc: ApiException,
) -> str:
    """Return the secret-safe operator message for a failed pre-wake reconcile.

    ``ApiException.body`` can echo Kubernetes object content, including env
    fields, so it must never be copied into the FleetAgent row/event/alert.
    Status + reason identify the API failure without exposing manifest data.
    """
    status = getattr(exc, 'status', None)
    reason = str(getattr(exc, 'reason', '') or 'Kubernetes API error')
    detail = f'HTTP {status} {reason}' if status else reason
    return (
        f'Runtime contract reconciliation failed for agent '
        f'{agent.agent_username} at stage {stage} ({detail}); '
        'wake aborted before scale.'
    )


def _notify_runtime_contract_reconcile_failure(stage: str) -> None:
    """Trigger the existing Alertmanager→Connect owner notification lane.

    The counter is exported on Hive's already-scraped internal delivery metrics
    endpoint. ``k8s/hive-runtime-contract-alerts.yaml`` routes any increase to
    the DevOps owner group, so this call is the targeted DM trigger rather than
    a log-only best effort.
    """
    from . import runtime_contract_metrics

    runtime_contract_metrics.record_failure(stage)


def start_failure_message(agent: FleetAgent) -> str:
    """Safe, consistent API/UI error for a failed native wake attempt."""
    message = str(getattr(agent, 'status_message', '') or '')
    if message.startswith('Runtime contract reconciliation failed for agent '):
        return message
    return f'Enable failed for agent {agent.agent_username}; check events for details.'


def get_agent_status(agent: FleetAgent) -> dict:
    """Live status of the agent pod from k8s."""
    core_v1, _, _n = get_k8s_client()
    ns = _live_ns(agent)
    result = {
        'status': agent.status, 'namespace': ns,
        'pod_status': 'unknown', 'pod_ip': None, 'pod_name': None, 'ready_containers': 0,
    }
    try:
        pods = core_v1.list_namespaced_pod(ns, label_selector=f'shizuha.io/agent={agent.agent_username}')
        if pods.items:
            pod = pods.items[0]
            result['pod_status'] = pod.status.phase
            result['pod_ip'] = pod.status.pod_ip
            result['pod_name'] = pod.metadata.name
            if pod.status.container_statuses:
                result['ready_containers'] = sum(1 for c in pod.status.container_statuses if c.ready)
    except ApiException:
        pass
    return result


# ── Internal helpers ───────────────────────────────────────────────────────


def current_replicas(agent: FleetAgent) -> int | None:
    """Live Deployment ``spec.replicas``, or None when the object is unreadable.

    Used to distinguish Hive ``status=running`` from a deploy that is actually
    scaled to 0 (ghost holder). None means "unknown" — callers fail open.
    """
    try:
        _core_v1, apps_v1, _n = get_k8s_client()
        deploy = apps_v1.read_namespaced_deployment(
            agent.deployment_name, _live_ns(agent),
        )
        return int(getattr(deploy.spec, 'replicas', 0) or 0)
    except Exception:  # noqa: BLE001 — observation must never break actuation
        return None


def deployment_runtime_ready(agent: FleetAgent) -> bool | None:
    """True when the native Deployment has at least one ready replica.

    None means unreadable OR scaled to 0 (fail open / not a cannot-turn
    signal). spec.replicas>=1 with ready_replicas=0 is a CrashLoop /
    image-pull / ENOSPC / broker-unready seat — not a working holder.
    """
    try:
        _core_v1, apps_v1, _n = get_k8s_client()
        deploy = apps_v1.read_namespaced_deployment(
            agent.deployment_name, _live_ns(agent),
        )
        spec_replicas = int(getattr(getattr(deploy, 'spec', None), 'replicas', 0) or 0)
        if spec_replicas <= 0:
            # Hibernated / scaled-to-zero is not a broken running seat.
            # Callers must not treat this as cannot-turn or they will never
            # GRANT+START a sleeping DeepSeek waiter. Ghost RUNNING with 0
            # replicas is stamped False by stamp_native_runtime_ready instead.
            return None
        ready = int(getattr(getattr(deploy, 'status', None), 'ready_replicas', 0) or 0)
        return ready >= 1
    except Exception:  # noqa: BLE001
        return None


def _scale(agent: FleetAgent, replicas: int, ok_if_missing: bool = False) -> bool:
    core, apps_v1, _n = get_k8s_client()
    try:
        from .native_runtime_release import enabled, assert_still_current, intent_guard, assert_owned_deployment, validate_applied_release
        if replicas > 0 and enabled() and _uses_native_runtime_image(agent):
            authority = getattr(agent, '_native_runtime_release', None)
            if authority is None:
                return False
            with intent_guard(agent) as desired:
                if desired and desired.desired_lifecycle != 'active':
                    return False
                assert_still_current(authority)
                dep = apps_v1.read_namespaced_deployment(agent.deployment_name, _live_ns(agent), _request_timeout=(3, 5))
                assert_owned_deployment(agent, dep)
                validate_applied_release(authority, dep)
                images = [c.image for c in dep.spec.template.spec.containers if c.name in {'agent', agent.agent_username}]
                if images != [authority.release.deployment_image]:
                    return False
                if desired and desired.expected_deployment_uid and desired.expected_deployment_uid != dep.metadata.uid:
                    return False
                if dep.spec.replicas == replicas:
                    return True
                if dep.spec.replicas != 0:
                    return False
                # Preserve the single-process fence even for terminal phases:
                # a lost-node Failed Pod is not proof that its containers ended.
                # The same complete owned-process proof governs materialization,
                # active drain reservations, and this cold scale boundary.
                live_leftovers = _native_runtime_process_pods(
                    core.list_namespaced_pod(
                        _live_ns(agent), label_selector=f'shizuha.io/agent={agent.agent_username}',
                        _request_timeout=(3, 5),
                    ).items, apps_v1, _live_ns(agent), dep.metadata.uid,
                )
                if live_leftovers:
                    logger.warning(
                        'HIVE-2893: scale 0->1 for %s deferred — %d live leftover pod(s) beside the '
                        'scaled-to-zero Deployment (phases=%s)',
                        agent.agent_username, len(live_leftovers),
                        sorted({getattr(p.status, 'phase', '?') for p in live_leftovers}),
                    )
                    agent.status_message = (
                        'start deferred: a live leftover pod sits beside the scaled-to-zero '
                        'Deployment; reconcile it before enabling'
                    )
                    try:
                        agent.save(update_fields=['status_message'])
                    except Exception:  # noqa: BLE001 — refusal must still surface
                        pass
                    return False
                if not _lease_start_decision(agent).allowed:
                    return False
                assert_still_current(authority)
                # Recompute after every blocking source/lease read; never reuse
                # a previous absence observation to authorize the scale CAS.
                live_leftovers = _native_runtime_process_pods(
                    core.list_namespaced_pod(
                        _live_ns(agent), label_selector=f'shizuha.io/agent={agent.agent_username}',
                        _request_timeout=(3, 5),
                    ).items, apps_v1, _live_ns(agent), dep.metadata.uid,
                )
                if live_leftovers:
                    return False
                # Complete terminated history cannot restart while these
                # final authority reads run. Recheck external start/release
                # authority after the potentially blocking owner proof.
                if not _lease_start_decision(agent).allowed:
                    return False
                assert_still_current(authority)
                apps_v1.patch_namespaced_deployment_scale(
                    name=agent.deployment_name, namespace=_live_ns(agent),
                    body={'metadata': {'uid': dep.metadata.uid, 'resourceVersion': dep.metadata.resource_version},
                          'spec': {'replicas': replicas}}, _request_timeout=(3, 5))
            return True
        inactive = getattr(agent, '_native_lifecycle_operation', None)
        metadata = ({'uid': inactive['deployment_uid'],
                     'resourceVersion': inactive['source_resource_version']}
                    if replicas == 0 and inactive else {})
        apps_v1.patch_namespaced_deployment_scale(
            name=agent.deployment_name,
            namespace=_live_ns(agent),
            body={'spec': {'replicas': replicas}, **({'metadata': metadata} if metadata else {})},
        )
        return True
    except ApiException as e:
        if e.status == 404 and ok_if_missing:
            return True
        logger.error(f'Scale to {replicas} failed for {agent.agent_username}: {e}')
        return False


def wants_voice_s2s(agent: FleetAgent) -> bool:
    """Grok Voice seats expose SCLI /v1/voice/realtime to the Voice proxy."""
    env = agent.env if isinstance(getattr(agent, 'env', None), dict) else {}
    blob = ' '.join([
        str(getattr(agent, 'model', '') or ''),
        str(env.get('MODEL') or ''),
        str(env.get('SHIZUHA_K8S_PRIMARY_MODEL') or ''),
    ]).lower()
    return 'grok-voice' in blob


def _base_labels(agent: FleetAgent) -> dict:
    labels = {
        'app.kubernetes.io/managed-by': MANAGED_BY,
        'shizuha.io/agent': agent.agent_username,
        NATIVE_RUNTIME_LABEL: NATIVE_RUNTIME_LABEL_VALUE,
    }
    if wants_voice_s2s(agent):
        labels['shizuha.io/voice-s2s'] = 'true'
    # P1-B/PLAT-174: isolated (DevOps) agents carry the role label so the
    # provisioner-emitted SSH-egress NetworkPolicy podSelector matches them.
    # PLAT-4729/PLAT-6041: the same label is the selector for the shared-namespace
    # policy fleet-allow-hostplane-ssh-egress-v2 (declared in
    # k8s/fleet/30-fleet-isolation-netpol.yaml, applied by
    # .forgejo/workflows/deploy-fleet-networkpolicy.yml). `isolated` cannot carry
    # that designation on its own: it relocates the namespace and is immutable
    # after provisioning, and no live seat sets it — which is exactly how the
    # devops-ssh class was lost without anyone deciding to retire it.
    if agent.isolated or agent.hostplane_ssh:
        labels['shizuha.io/agent-role'] = 'devops-ssh'
    return labels


FLEET_SSH_SOURCE_SECRETS = ('ichi-fleet-ssh', 'ni-fleet-ssh', 'san-fleet-ssh')
FLEET_SSH_INIT_SCRIPT = (
    'set -euo pipefail\n'
    'install -d -m 0700 -o 1000 -g 1000 /ssh-home\n'
    'cp -a /ssh-secret/. /ssh-home/\n'
    'chown -R 1000:1000 /ssh-home\n'
    'find /ssh-home -type f -exec chmod 0600 {} +\n'
    'for f in /ssh-home/*.pub /ssh-home/known_hosts /ssh-home/config; do\n'
    '  [ ! -e "$f" ] || chmod 0644 "$f"\n'
    'done\n'
)


def fleet_ssh_secret_name(agent: FleetAgent) -> str:
    return f'{agent.agent_username}-fleet-ssh'


def _secret_has_private_key(secret) -> bool:
    data = getattr(secret, 'data', None) or {}
    return any(name in data for name in ('id_rsa', 'id_ed25519', 'id_ecdsa'))


def ensure_fleet_ssh_secret(core_v1, ns: str, agent: FleetAgent) -> str:
    """Clone the shared DevOps fleet key into ``<user>-fleet-ssh`` without logging it.

    Source is an already-mounted devops seat secret. Last-applied annotations
    are stripped so private material is not copied into a second plaintext field.
    """
    dest = fleet_ssh_secret_name(agent)
    try:
        existing = core_v1.read_namespaced_secret(dest, ns)
        if _secret_has_private_key(existing):
            return dest
    except ApiException as exc:
        if getattr(exc, 'status', None) != 404:
            raise

    source = None
    for name in FLEET_SSH_SOURCE_SECRETS:
        if name == dest:
            continue
        try:
            candidate = core_v1.read_namespaced_secret(name, ns)
        except ApiException:
            continue
        if _secret_has_private_key(candidate):
            source = candidate
            break
    if source is None:
        raise RuntimeError(
            f'no fleet-ssh source secret in {ns} for {agent.agent_username}'
        )

    body = client.V1Secret(
        metadata=client.V1ObjectMeta(
            name=dest,
            namespace=ns,
            labels={
                'app': f'agent-{agent.agent_username}',
                'shizuha.io/credential-scope': 'fleet-ssh',
                'shizuha.io/runtime': 'k3s-native',
                'shizuha.io/agent': agent.agent_username,
            },
        ),
        data=source.data,
        type='Opaque',
    )
    try:
        core_v1.create_namespaced_secret(ns, body)
    except ApiException as exc:
        if getattr(exc, 'status', None) != 404 and getattr(exc, 'status', None) != 409:
            raise
        if getattr(exc, 'status', None) == 409:
            current = core_v1.read_namespaced_secret(dest, ns)
            current.data = source.data
            annotations = dict(current.metadata.annotations or {})
            annotations.pop('kubectl.kubernetes.io/last-applied-configuration', None)
            current.metadata.annotations = annotations
            core_v1.replace_namespaced_secret(dest, ns, current)
        else:
            raise
    return dest


def _fleet_ssh_init_container(agent: FleetAgent) -> client.V1Container:
    return client.V1Container(
        name='fleet-ssh-materialize',
        image=_resolve_image(agent),
        image_pull_policy=agent.image_pull_policy,
        command=['bash', '-lc'],
        args=[FLEET_SSH_INIT_SCRIPT],
        volume_mounts=[
            client.V1VolumeMount(name='fleet-ssh-secret', mount_path='/ssh-secret', read_only=True),
            client.V1VolumeMount(name='fleet-ssh-home', mount_path='/ssh-home'),
        ],
        security_context=client.V1SecurityContext(
            run_as_non_root=False,
            run_as_user=0,
            run_as_group=0,
            allow_privilege_escalation=False,
            capabilities=client.V1Capabilities(drop=['ALL'], add=['CHOWN', 'FOWNER', 'DAC_OVERRIDE']),
        ),
        resources=client.V1ResourceRequirements(
            requests={'cpu': '10m', 'memory': '32Mi'},
            limits={'cpu': '100m', 'memory': '64Mi'},
        ),
    )


# PLAT-7463: materialize ~/.kube/config referencing the kubelet-managed SA
# tokenFile. The kubeconfig NEVER embeds bearer bytes — client.authentication
# style tokenFile reference keeps the credential live-rotating. The workspace
# PVC is fsGroup-1000; the init runs as root to write into /home/agent/.kube
# and chowns it to the agent uid (mirrors the fleet-ssh materialize pattern).
FLEET_KUBECONFIG_INIT_SCRIPT = r'''set -euo pipefail
KUBE_DIR=/home/agent/.kube
mkdir -p "$KUBE_DIR"
cat > "$KUBE_DIR/config" <<'KCFG'
apiVersion: v1
kind: Config
clusters:
- name: in-cluster
  cluster:
    server: https://kubernetes.default.svc
    certificate-authority: /var/run/secrets/kubernetes.io/serviceaccount/ca.crt
users:
- name: service-account
  user:
    tokenFile: /var/run/secrets/kubernetes.io/serviceaccount/token
contexts:
- name: in-cluster
  context:
    cluster: in-cluster
    user: service-account
current-context: in-cluster
KCFG
chown -R 1000:1000 "$KUBE_DIR"
chmod 600 "$KUBE_DIR/config"
'''


def _fleet_kubeconfig_init_container(agent: FleetAgent) -> client.V1Container:
    return client.V1Container(
        name='fleet-kubeconfig-materialize',
        image=_resolve_image(agent),
        image_pull_policy=agent.image_pull_policy,
        command=['bash', '-lc'],
        args=[FLEET_KUBECONFIG_INIT_SCRIPT],
        volume_mounts=[
            client.V1VolumeMount(name='workspace', mount_path='/home/agent'),
        ],
        security_context=client.V1SecurityContext(
            run_as_non_root=False,
            run_as_user=0,
            run_as_group=0,
            allow_privilege_escalation=False,
            capabilities=client.V1Capabilities(drop=['ALL'], add=['CHOWN', 'FOWNER', 'DAC_OVERRIDE']),
        ),
        resources=client.V1ResourceRequirements(
            requests={'cpu': '10m', 'memory': '32Mi'},
            limits={'cpu': '100m', 'memory': '64Mi'},
        ),
    )


def apply_hostplane_ssh(agent: FleetAgent) -> None:
    """SoT follow-through: secret + devops-ssh label + ~/.ssh mount on the live seat."""
    if not agent.hostplane_ssh:
        return
    # _create_deployment renders via _resolve_image. Without a reviewed bind it
    # falls back to settings.FLEET_AGENT_IMAGE (stale tag, currently
    # harness-20260711c) and can ImagePullBackOff a live enable. Same gate as
    # start_agent / ensure_agent_image.
    _bind_reviewed_runtime_image(agent)
    core_v1, apps_v1, _n = get_k8s_client()
    ns = _live_ns(agent)
    # Configuration follows an existing runtime; it never creates or wakes one.
    # Fence the replacement against a simultaneous scale/start/hibernate.
    existing = apps_v1.read_namespaced_deployment(agent.deployment_name, ns)
    replicas = getattr(existing.spec, 'replicas', None)
    version = getattr(existing.metadata, 'resource_version', None)
    if not isinstance(replicas, int) or replicas < 0 or not isinstance(version, str) or not version:
        raise ValueError('Cannot apply hostplane SSH without an observed Deployment version/replica count')
    ensure_fleet_ssh_secret(core_v1, ns, agent)
    _create_deployment(apps_v1, ns, agent, replicas=replicas, expected_resource_version=version)


def ensure_agent_cortex_key(agent: FleetAgent) -> str:
    """Guarantee `<username>-agent-creds` carries this agent's own CORTEX_API_KEY.

    Every agent authenticates to Cortex with ITS OWN key so usage, leases and
    billing attribute to ``agent-<username>``. Nothing enforced that the key
    actually existed: the Deployment referenced it with ``optional=True``, so a
    missing key resolved to an EMPTY STRING and the agent ran indefinitely —
    Running, Alive, heartbeating on schedule — while every model call came back
    ``vLLM error 401: Authentication credentials were not provided``.

    rei was provisioned 2026-06-27, before per-agent keys landed, and had not
    completed a single turn since; the only symptom on the Agents page was a
    detail-less "Runtime unhealthy" chip. A sweep found 10 of 51 agent-creds
    Secrets with no CORTEX_API_KEY.

    Operator 2026-08-05: "hive manages its agents underneath .. and each agent
    uses their own key .. so you shouldn't have to do it". Minting belongs here,
    on the reconcile path, so an agent never waits on a human for a credential
    Hive already owns.

    Returns a short fingerprint of the live key (stable across calls, safe to put
    in an annotation), or '' when no key could be established.
    """
    import base64
    import hashlib

    core_v1, _apps_v1, _n = get_k8s_client()
    secret_name = f'{agent.agent_username}-agent-creds'

    # `_live_ns` can name an isolated namespace that was never created (or was
    # torn down), e.g. `fleet-shizuha-128`. Writing there fails with
    # `namespaces "..." not found`, which is noise, not a credential problem —
    # an agent with no namespace has no pod to credential. Prefer the namespace
    # where the agent's Secret actually lives, then the shared fleet namespace,
    # and skip quietly when neither exists.
    existing = None
    ns = None
    for candidate in (_live_ns(agent), fleet_namespace()):
        if not candidate or candidate == ns:
            continue
        try:
            existing = core_v1.read_namespaced_secret(secret_name, candidate)
            ns = candidate
            break
        except ApiException as exc:
            status = getattr(exc, 'status', None)
            if status == 404:
                # Remember the first candidate as the create target, but keep
                # looking: the Secret may live in the shared namespace.
                if ns is None:
                    ns = candidate
                continue
            logger.warning('cortex-key: cannot read %s in %s: %s', secret_name, candidate, exc)
            return ''
    if ns is None:
        return ''

    data = dict(getattr(existing, 'data', None) or {}) if existing is not None else {}
    current = (data.get('CORTEX_API_KEY') or '').strip()
    if current:
        return hashlib.sha256(current.encode()).hexdigest()[:16]

    minted = _mint_agent_cortex_key(agent)
    if not minted:
        # Fail LOUD and leave the reason on the row. The Deployment refuses to
        # start a pod without this key, which is the point: a Pod stuck on
        # CreateContainerConfigError names the missing credential, whereas the
        # old fail-soft path produced an agent that looked healthy and could not
        # answer a single prompt for five weeks.
        logger.error(
            'cortex-key: could not mint a Cortex key for %s; the agent cannot '
            'authenticate to Cortex until this is resolved',
            agent.agent_username,
        )
        return ''

    encoded = base64.b64encode(minted.encode()).decode()
    try:
        if existing is None:
            core_v1.create_namespaced_secret(ns, client.V1Secret(
                metadata=client.V1ObjectMeta(name=secret_name, namespace=ns),
                data={'CORTEX_API_KEY': encoded},
                type='Opaque',
            ))
        else:
            # PATCH with only our key: a full-object replace would drop
            # AGENT_PASSWORD / FORGEJO_TOKEN / CONTEXT_PROMPT if this raced a
            # concurrent writer of the same Secret.
            core_v1.patch_namespaced_secret(secret_name, ns, {'data': {'CORTEX_API_KEY': encoded}})
    except ApiException as exc:
        if 'namespaces' in str(getattr(exc, 'body', '') or '') and getattr(exc, 'status', None) == 404:
            # No namespace => no pod => nothing to credential yet. The agent
            # gets its key when it is actually provisioned.
            logger.info(
                'cortex-key: %s has no namespace %s yet; deferring to provisioning',
                agent.agent_username, ns,
            )
            return ''
        logger.error('cortex-key: failed to persist key for %s: %s', agent.agent_username, exc)
        return ''
    _log_event(agent, 'backend_applied', 'Minted missing per-agent Cortex API key')
    logger.info('cortex-key: minted per-agent Cortex key for %s', agent.agent_username)
    return hashlib.sha256(minted.encode()).hexdigest()[:16]


# PLAT-5372: role-scoped credential projection. Team-slug based and resolved
# from the Pulse TeamRoster the way WRITE_TEAMS is in
# reconcile_agent_github_identity — a seat gets a scope by construction from its
# team, never by a hand-maintained per-agent list.
#
# A seat whose team carries a scope has the scope's keys projected from a single
# shared fleet-namespace Secret into its `<user>-agent-creds` Secret (patch,
# never replace), so a deprovision/reprovision re-establishes it instead of
# silently dropping the material (the PLAT-5371 durability gap).
ROLE_CREDENTIAL_SCOPES: dict[str, dict] = {
    'accounting-portal': {
        'teams': frozenset({'accounting'}),
        # Single shared Secret in the fleet namespace — no per-seat duplication.
        # Maintained by the operator/reconciler from the shizuha-ns source.
        'shared_secret': 'accounting-portal-credentials',
        'keys': (
            'accounting-portal-catalog.json',
            'accounting-portal-flat.json',
            'accounting-portal-index.json',
        ),
    },
}


def _agent_teams_from_roster(agent: FleetAgent, email_to_teams: dict[str, set[str]] | None) -> set[str]:
    """Resolve an agent's team slugs from the Pulse TeamRoster.

    ``email_to_teams`` is a pre-fetched ``memberships_by_email`` map (the
    reconciler command fetches it once). When absent, fetch it lazily with a
    short TTL cache so per-provision calls do not hammer Pulse.
    """
    from .on_demand import memberships_by_email, team_slug_from_key

    if email_to_teams is None:
        email_to_teams = _role_credential_roster_cache()
    if not email_to_teams:
        # PLAT-10143: healthy-empty OR fetch-failure (None). Best-effort
        # callers treat both as no-teams; the mint path uses
        # _forgejo_grant_checked to keep the distinction.
        return set()
    uname = (agent.agent_username or '').strip().lower()
    return {
        team_slug_from_key(t)
        for t in email_to_teams.get(f'{uname}@shizuha.com', set())
    }


_ROLE_CRED_ROSTER_CACHE: dict = {'at': 0.0, 'map': None}
_ROLE_CRED_ROSTER_TTL = 300.0


def _role_credential_roster_cache() -> dict[str, set[str]] | None:
    """Fetch the Pulse TeamRoster once per TTL and cache the email→teams map.

    PLAT-10143: the three outcomes are now distinguishable — a healthy fetch
    returns the map (possibly empty); a FAILED fetch returns ``None`` (the
    seat's grant status is UNKNOWN, not absent) and is never cached, so the
    next caller retries. Callers that only need best-effort teams treat
    ``None`` like an empty map; the mint path must treat it as unknown.
    """
    now = time.monotonic()
    if _ROLE_CRED_ROSTER_CACHE['map'] is not None and now - _ROLE_CRED_ROSTER_CACHE['at'] < _ROLE_CRED_ROSTER_TTL:
        return _ROLE_CRED_ROSTER_CACHE['map']
    try:
        from .pulse_auth import pulse_fleet_rosters
        rosters = pulse_fleet_rosters()
        from .on_demand import memberships_by_email
        _ROLE_CRED_ROSTER_CACHE['map'] = memberships_by_email(rosters) if rosters else {}
        _ROLE_CRED_ROSTER_CACHE['at'] = now
    except Exception as exc:  # noqa: BLE001 — a roster outage must not break provisioning
        logger.warning('role-creds: Pulse roster fetch failed (%s); scopes UNKNOWN until the next fetch', exc)
        # Failure is not a successful empty roster. Retry the next caller,
        # without retaining expired memberships or caching a missing grant.
        _ROLE_CRED_ROSTER_CACHE.update(at=0.0, map=None)
        return None
    return _ROLE_CRED_ROSTER_CACHE['map']


def ensure_agent_role_credentials(
    agent: FleetAgent,
    email_to_teams: dict[str, set[str]] | None = None,
) -> list[str]:
    """Re-establish role-scoped credential projections on the seat's agent-creds.

    PLAT-5372: mirrors ``ensure_agent_cortex_key`` — a reconciler that patches
    ``<user>-agent-creds`` with keys the renderer does not own (3-way merge
    preserves them across re-renders), so a fresh provision heals the
    accounting-portal projection that a deprovision/reprovision would otherwise
    silently drop.

    For every scope whose team the agent belongs to, copy the scope's keys from
    the single shared fleet-namespace Secret into the seat Secret (PATCH, never
    replace — a full-object replace would drop AGENT_PASSWORD/FORGEJO_TOKEN if
    this raced a concurrent writer). Keys absent from the shared Secret are
    skipped, not fabricated.

    Returns the list of scope names projected (empty when none apply).
    """
    core_v1, _apps_v1, _n = get_k8s_client()
    teams = _agent_teams_from_roster(agent, email_to_teams)
    if not teams:
        return []

    secret_name = f'{agent.agent_username}-agent-creds'
    ns = fleet_namespace()
    projected: list[str] = []
    for scope, spec in ROLE_CREDENTIAL_SCOPES.items():
        if not (set(spec.get('teams') or set()) & teams):
            continue
        shared_name = spec.get('shared_secret')
        keys = tuple(spec.get('keys') or ())
        if not shared_name or not keys:
            continue
        try:
            shared = core_v1.read_namespaced_secret(shared_name, ns)
        except ApiException as exc:
            if getattr(exc, 'status', None) == 404:
                logger.warning(
                    'role-creds: shared Secret %s/%s missing for scope %s (agent %s); '
                    'projection deferred until it exists',
                    ns, shared_name, scope, agent.agent_username,
                )
                continue
            logger.error('role-creds: cannot read shared Secret %s/%s: %s', ns, shared_name, exc)
            continue
        shared_data = dict(getattr(shared, 'data', None) or {})
        patch_data = {
            key: shared_data[key]
            for key in keys
            if shared_data.get(key)
        }
        if not patch_data:
            logger.warning(
                'role-creds: shared Secret %s/%s has none of keys %s for scope %s (agent %s)',
                ns, shared_name, keys, scope, agent.agent_username,
            )
            continue
        try:
            core_v1.patch_namespaced_secret(secret_name, ns, {'data': patch_data})
        except ApiException as exc:
            if getattr(exc, 'status', None) == 404:
                # No seat Secret yet (not provisioned) — the provision path calls
                # this after the renderer creates it; defer quietly.
                logger.info('role-creds: %s/%s not present yet; deferring scope %s', ns, secret_name, scope)
                continue
            logger.error('role-creds: failed to project scope %s for %s: %s', scope, agent.agent_username, exc)
            continue
        projected.append(scope)
        _log_event(agent, 'role_credentials_applied', f'Projected role scope {scope} from shared Secret {shared_name}')
        logger.info('role-creds: projected scope %s for %s from %s/%s', scope, agent.agent_username, ns, shared_name)
    return projected


def _mint_agent_cortex_key(agent: FleetAgent) -> str:
    """Mint an agent-scoped key through Cortex's trusted internal endpoint.

    Same contract as ``claws.provisioner.mint_owner_cortex_key``; the key is
    attributed to the agent's own Shizuha-ID row, never to a shared fleet
    identity, so per-agent usage and leasing stay meaningful.
    """
    import requests

    # Use HIVE_CORTEX_LEASE_API_KEY, NOT CORTEX_API_KEY.
    #
    # Cortex grants the trusted-service identity that `/v1/internal/api-keys`
    # requires only to a token equal to CORTEX'S OWN CORTEX_API_KEY. Hive's
    # `CORTEX_API_KEY` is a different, customer-tier key, so a mint sent with it
    # returns 403 "Only a Cortex-trusted service credential may mint
    # elevated-tier keys" — which the caller logs as a warning and drops. That
    # is why the Claw mint path has quietly never worked, and why agents
    # provisioned before per-agent keys existed never received one.
    #
    # The identical trap is already documented one file over, on the model-lease
    # scheduler (hive/k8s/hive-backend-live.yaml): the same wrong key made
    # leasing "look enabled while being a silent no-op". `HIVE_CORTEX_LEASE_API_KEY`
    # is the credential provisioned for exactly this class of trusted call, and
    # it is verified to match Cortex's service key. Reusing it adds no privilege
    # Hive does not already hold.
    service_key = (
        (getattr(settings, 'HIVE_CORTEX_LEASE_API_KEY', '') or '').strip()
        or (getattr(settings, 'CORTEX_API_KEY', '') or '').strip()
    )
    base = (
        getattr(settings, 'CORTEX_BASE_URL', '')
        or getattr(settings, 'VLLM_BASE_URL', '')
    ).rstrip('/')
    if not service_key or not base:
        logger.warning(
            'cortex-key: mint unavailable for %s (service_key=%s base=%s)',
            agent.agent_username, bool(service_key), bool(base),
        )
        return ''
    # `identity_user_id` is only populated for agents provisioned through the
    # identity flow; most of the fleet predates it and carries None. Refusing to
    # mint there would leave 17 of 18 uncredentialed agents permanently broken,
    # so fall back to the same by-username lookup personal_agents already uses,
    # and persist what we resolve so the next pass is a direct hit.
    #
    # The refusal below still stands when NOTHING resolves: a key minted against
    # the wrong subject misattributes usage and cannot be revoked meaningfully.
    # agent-nami already exists in Cortex with user_id=0 from that mistake.
    user_id = getattr(agent, 'identity_user_id', None)
    if not user_id:
        try:
            from .personal_agents import resolve_agent_user_id
            user_id = resolve_agent_user_id(agent)
        except Exception as exc:  # never let identity lookup break the reconcile
            logger.warning(
                'cortex-key: identity lookup failed for %s: %s',
                agent.agent_username, exc,
            )
            user_id = None
        if user_id:
            prior_identity_user_id = agent.identity_user_id
            try:
                agent.identity_user_id = int(user_id)
                agent.save(update_fields=['identity_user_id'])
            except Exception as exc:
                # Minting can still proceed on the resolved id; persisting is an
                # optimisation. Restore the in-memory value on refusal: leaving
                # the rejected id on the instance made EVERY later save() on it
                # re-raise the immutability error (the enable-path 500 where
                # start_agent's status write crashed, 2026-08-07).
                agent.identity_user_id = prior_identity_user_id
                logger.info(
                    'cortex-key: could not persist resolved identity for %s: %s',
                    agent.agent_username, exc,
                )
    if not user_id:
        logger.warning(
            'cortex-key: %s has no resolvable identity_user_id; a key minted '
            'against a different identity would misattribute its usage',
            agent.agent_username,
        )
        return ''
    try:
        resp = requests.post(
            f'{base}/v1/internal/api-keys',
            headers={
                'Authorization': (
                    service_key if service_key.lower().startswith('bearer ')
                    else f'Bearer {service_key}'
                ),
                'Accept': 'application/json',
            },
            json={
                'name': f'agent-{agent.agent_username}',
                'user_id': int(user_id),
                # Human Hane payer. Agent identity stays on user_id for
                # attribution; unmapped org-fleet (no owner_id) sends 0 so
                # Cortex treats the call as platform self-use — no trade.
                'billing_user_id': int(getattr(agent, 'owner_id', None) or 0),
                # Matches every existing per-agent key (agent-nova, agent-rui,
                # agent-rin, ...): internal tier, own_fleet traffic class, no
                # org. A STANDARD-tier key would be metered as resold customer
                # traffic and would need an org_id.
                'service_tier': 'internal',
                'traffic_class': 'own_fleet',
            },
            timeout=15,
        )
    except (requests.RequestException, TypeError, ValueError) as exc:
        logger.warning('cortex-key: mint request failed for %s: %s', agent.agent_username, exc)
        return ''
    if getattr(resp, 'status_code', 0) not in (200, 201):
        logger.warning(
            'cortex-key: mint rejected for %s: HTTP %s %s',
            agent.agent_username, getattr(resp, 'status_code', '?'), (resp.text or '')[:300],
        )
        return ''
    try:
        key = ((resp.json() or {}).get('key') or '').strip()
    except ValueError:
        logger.warning('cortex-key: mint returned a non-JSON body for %s', agent.agent_username)
        return ''
    if not key:
        logger.warning('cortex-key: mint response for %s carried no plaintext key', agent.agent_username)
    return key


def _forgejo_secret(agent: FleetAgent, core_v1, namespace: str):
    """Select this seat's Origin credential in the consumer's namespace.

    A purpose-scoped Secret is externally provisioned and never rendered or
    repaired by Hive. Only confirmed absence permits the legacy credential
    path; transport/RBAC failures must not silently select a different token.
    The Hive seat username owns the Secret, not its human owner or Origin alias.
    """
    purpose_name = f'{agent.agent_username}-forgejo-token'
    try:
        return purpose_name, core_v1.read_namespaced_secret(purpose_name, namespace)
    except ApiException as exc:
        if exc.status != 404:
            raise
    legacy_name = f'{agent.agent_username}-agent-creds'
    try:
        return legacy_name, core_v1.read_namespaced_secret(legacy_name, namespace)
    except ApiException as exc:
        if exc.status != 404:
            raise
        return legacy_name, None


def _forgejo_credential_env(
    agent: FleetAgent, *, secret_name: str | None = None,
) -> list[client.V1EnvVar]:
    """Render aliases without embedding token bytes or changing Secret data.

    Pure rendering defaults to the purpose-scoped contract. Materializers pass
    the live resolver's selection so unmigrated seats retain their legacy grant.
    """
    secret_name = secret_name or f'{agent.agent_username}-forgejo-token'
    return [
        client.V1EnvVar(
            name=name,
            value_from=client.V1EnvVarSource(
                secret_key_ref=client.V1SecretKeySelector(
                    name=secret_name,
                    key='FORGEJO_TOKEN',
                    optional=True,
                ),
            ),
        )
        for name in ('FORGEJO_TOKEN', 'ORIGIN_TOKEN')
    ]


def _forgejo_env_drifted(container, agent: FleetAgent, core_v1) -> bool:
    secret_name, _secret = _forgejo_secret(agent, core_v1, _live_ns(agent))
    for desired in _forgejo_credential_env(agent, secret_name=secret_name):
        entries = [item for item in (container.env or []) if item.name == desired.name]
        if len(entries) != 1 or not _secret_env_matches(entries[0], desired):
            return True
    return False


def _forgejo_identity_username(agent: FleetAgent) -> str:
    """Forgejo login for this seat. Never the Origin admin ``shizuha`` user."""
    uname = (agent.agent_username or '').strip().lower()
    return _FORGEJO_IDENTITY_ALIASES.get(uname, uname)


def _forgejo_grant_checked(agent: FleetAgent) -> tuple[set[str], bool]:
    """PLAT-10143: resolve the seat's team grant AND whether it is known.

    Returns ``(teams, known)``. ``known=False`` means the Pulse roster fetch
    FAILED this cycle — the seat's writer status is unknown, not absent.
    Silently treating unknown as read-scoped is the defect that minted hiro a
    read token mid-writer-andidacy and cost a 10h andon (the roster outage
    window silently downgraded the mint).
    """
    from .on_demand import memberships_by_email, team_slug_from_key

    try:
        email_to_teams = _role_credential_roster_cache()
    except Exception:  # noqa: BLE001 — fail-closed into the unknown branch
        return set(), False
    if email_to_teams is None:
        # Roster fetch FAILED (never cached): the seat's grant is unknown.
        return set(), False
    uname = (agent.agent_username or '').strip().lower()
    teams = {
        team_slug_from_key(t)
        for t in email_to_teams.get(f'{uname}@shizuha.com', set())
    }
    return teams, True


def _forgejo_token_scopes(agent: FleetAgent) -> tuple[str, ...]:
    """Token scopes from Pulse team membership. Org teams still gate push."""
    teams, _known = _forgejo_grant_checked(agent)
    if teams & _FORGEJO_WRITE_TEAMS or teams & _FORGEJO_REVIEW_TEAMS:
        return _FORGEJO_WRITE_SCOPES
    return _FORGEJO_READ_SCOPES


def _forgejo_admin_basic_auth() -> tuple[str, str]:
    """Username/password for Origin token create.

    Forgejo rejects ``Authorization: token`` on ``POST /users/{u}/tokens``
    (``auth method not allowed``). Token mint requires HTTP basic auth as an
    admin. Prefer env (tests + CronJob), else the Origin admin Secret that
    hive-controller can already read.
    """
    user = (
        os.environ.get('HIVE_FORGEJO_ADMIN_USER', '').strip()
        or str(getattr(settings, 'HIVE_FORGEJO_ADMIN_USER', '') or '').strip()
    )
    password = (
        os.environ.get('HIVE_FORGEJO_ADMIN_PASSWORD', '').strip()
        or str(getattr(settings, 'HIVE_FORGEJO_ADMIN_PASSWORD', '') or '').strip()
    )
    if user and password:
        return user, password
    try:
        core_v1, _apps, _n = get_k8s_client()
        secret = core_v1.read_namespaced_secret(
            _FORGEJO_ADMIN_CREDS_SECRET, _FORGEJO_ADMIN_CREDS_NS,
        )
    except ApiException as exc:
        logger.warning('forgejo-token: cannot read %s/%s: %s', _FORGEJO_ADMIN_CREDS_NS, _FORGEJO_ADMIN_CREDS_SECRET, exc)
        return '', ''
    data = dict(getattr(secret, 'data', None) or {})
    try:
        user = base64.b64decode(data.get('FORGEJO_ADMIN_USER') or '').decode().strip()
        password = base64.b64decode(data.get('FORGEJO_ADMIN_PASSWORD') or '').decode().strip()
    except Exception as exc:  # noqa: BLE001 — corrupt secret must not crash reconcile
        logger.warning('forgejo-token: admin creds secret was not decodable: %s', exc)
        return '', ''
    return user, password


def _hive_origin_api_url() -> str:
    return os.environ.get(
        'HIVE_ORIGIN_API_URL',
        str(getattr(settings, 'HIVE_ORIGIN_API_URL', '') or '')
        or 'http://forgejo-http.origin.svc.cluster.local',
    ).rstrip('/')


def _mint_agent_forgejo_token(agent: FleetAgent, scopes: tuple[str, ...]) -> str:
    """Create a per-agent Origin access token. Returns plaintext once; never logs it."""
    from .forgejo_identity import OriginIdentityError, ensure_origin_identity

    ident = _forgejo_identity_username(agent)
    if not ident or ident == 'shizuha':
        logger.error(
            'forgejo-token: refusing to mint the Origin admin identity into %s',
            agent.agent_username,
        )
        return ''
    user, password = _forgejo_admin_basic_auth()
    if not user or not password:
        logger.warning('forgejo-token: no Origin admin basic-auth creds; cannot mint for %s', ident)
        return ''
    try:
        ensure_origin_identity(agent, ident, _hive_origin_api_url(), (user, password))
    except (OriginIdentityError, requests.RequestException, TypeError, ValueError) as exc:
        logger.warning('forgejo-token: Origin identity verification failed for %s: %s', ident, exc)
        return ''
    url = f'{_hive_origin_api_url()}/api/v1/users/{ident}/tokens'
    name = f"fleet-{ident}-{int(time.time())}"
    try:
        resp = requests.post(
            url,
            json={'name': name, 'scopes': list(scopes)},
            auth=(user, password),
            timeout=20,
        )
    except (requests.RequestException, TypeError, ValueError) as exc:
        logger.warning('forgejo-token: mint request failed for %s: %s', ident, exc)
        return ''
    if getattr(resp, 'status_code', 0) not in (200, 201):
        logger.warning(
            'forgejo-token: mint rejected for %s: HTTP %s %s',
            ident, getattr(resp, 'status_code', '?'), (resp.text or '')[:200],
        )
        return ''
    try:
        payload = resp.json() or {}
    except ValueError:
        logger.warning('forgejo-token: mint returned a non-JSON body for %s', ident)
        return ''
    token = str(payload.get('sha1') or payload.get('token') or '').strip()
    if not token:
        logger.warning('forgejo-token: mint response for %s carried no token', ident)
        return ''
    return token


def ensure_agent_forgejo_token(agent: FleetAgent, *, rescope_to: tuple[str, ...] | None = None) -> str:
    """Observe a purpose-scoped grant, or repair the unmigrated legacy grant.

    PLAT-7406: ``ensure_agent_cortex_key`` create-if-missing persists only
    ``CORTEX_API_KEY``. The Deployment references FORGEJO_TOKEN with
    ``optional=True``, so a missing key starts a Ready pod with no Origin
    identity. 2026-09-01 secret recreation left most seats Cortex-only and
    froze every merge/write path.

    Patch, never replace. Existing tokens are left alone (repair, not rotation)
    — EXCEPT under ``rescope_to`` (PLAT-10143): when the seat's CURRENT Pulse
    grant diverges from the stamped mint scopes, re-minting is the FIX, not a
    rotation-for-rotation's-sake; the drifted token cannot exercise the seat's
    advertised verbs (hiro's 10h andon class). Only the scope-parity sweep
    passes ``rescope_to``, after verifying the drift against the stamped
    annotation — an unknown grant never rotates (reported loud instead).
    Returns a short fingerprint of the live token (safe for an annotation), or
    '' when no token could be established.
    """
    ident = _forgejo_identity_username(agent)
    if not ident:
        return ''

    core_v1, _apps_v1, _n = get_k8s_client()
    ns = _live_ns(agent)
    try:
        secret_name, existing = _forgejo_secret(agent, core_v1, ns)
    except ApiException as exc:
        logger.warning('forgejo-token: credential selection failed for %s in %s (HTTP %s)',
                       agent.agent_username, ns, exc.status)
        return ''
    if existing is None:
        # Cortex-key create owns Secret creation. A missing Secret here means
        # the seat is not provisioned yet; minting into a new Cortex-less
        # Secret would invert that ownership.
        return ''

    raw = getattr(existing, 'data', None) or {}
    data = dict(raw) if isinstance(raw, dict) else {}
    value = data.get('FORGEJO_TOKEN')
    current = value.strip() if isinstance(value, str) else ''
    if current and rescope_to is None:
        return hashlib.sha256(current.encode()).hexdigest()[:16]

    if (secret_name == f'{agent.agent_username}-forgejo-token'
            and not current and rescope_to is None):
        # An externally provisioned grant is never overwritten or replaced by
        # minting another identity/token. Keep an incomplete grant visible.
        # (PLAT-10143: a rescope passes THROUGH here only when the seat holds
        # a drifted token — that overwrite is the fix, verified by the sweep.)
        logger.error('forgejo-token: purpose Secret %s in %s has no FORGEJO_TOKEN',
                     secret_name, ns)
        return ''

    if rescope_to is not None:
        # PLAT-10143: the sweep verified the CURRENT grant against the stamped
        # scopes immediately before calling — trust that verification; a fresh
        # roster read here could flap mid-run and stamp a different csv than
        # the sweep acted on.
        scopes = rescope_to
        scope_provisional = False
    else:
        scopes = _forgejo_token_scopes(agent)
        # PLAT-10143: unknown-grant mints must be LOUD and MARKED. A roster-fetch
        # failure used to silently downgrade the mint to read scopes (hiro's 10h
        # andon: writer seat, read token, discovered only on a mid-push 403).
        # Repair-not-brick still applies — the seat gets a working token — but the
        # Secret carries the provisional marker so the scope-parity sweep re-mints
        # the moment the roster resolves, and the mint event names the downgrade.
        teams, grant_known = _forgejo_grant_checked(agent)
        scope_provisional = not grant_known
        if scope_provisional:
            logger.error(
                'forgejo-token: roster UNAVAILABLE at mint for %s (identity %s) — '
                'minting READ-scoped token provisionally; scope-parity sweep will '
                're-mint at the seat\'s current grant once the roster resolves',
                agent.agent_username, ident,
            )
            _log_event(
                agent, 'forgejo_token_scope_provisional',
                'Origin token minted READ-scoped: Pulse roster unavailable at '
                'mint, seat grant unknown. Scope-parity sweep re-mints on '
                'roster recovery.',
            )
    # HIVE-2695: token-scope parity at grant time — the seat's capability
    # listing must not silently advertise verbs this token cannot exercise
    # (the PLAT-9426 class). Flag path per the PLAT-9430 design: mint
    # proceeds (repair-not-brick — this path patches, never rotates), the gap
    # is logged loud here and stamped on the listing by the mint point in
    # fleet.views._effective_capability_set (signed payload).
    try:
        from fleet.effective_capabilities import compute_effective_capability_grant
        from fleet.scope_parity import check_scope_parity

        grant = compute_effective_capability_grant(
            _agent_teams_from_roster(agent, None),
            # FleetAgent rows carry no email column (HIVE-620/644: the email is
            # the Shizuha-ID owner SUBJECT, not mutable row state); the
            # canonical platform identity is username@shizuha.com, matching the
            # _agent_email derivation and line-870 house pattern. Passing
            # agent.email here raised AttributeError and failed the HIVE-417
            # delegation payload-shape test (premerge run 3120).
            (agent.agent_id, agent.agent_username,
             f'{agent.agent_username}@shizuha.com'),
        )
        parity = check_scope_parity(
            agent=agent, grant=grant, origin_token_scopes=scopes,
        )
        if parity.scope_gap:
            logger.error(
                'HIVE-2695 scope parity: token for %s (identity %s) cannot '
                'exercise advertised verbs — %s',
                agent.agent_username, ident,
                '; '.join(
                    f'{g.tag} missing {sorted(g.missing_scopes)}'
                    for g in parity.gaps
                    if g.gap_class == 'advertised-but-unexercisable'
                ),
            )
    except Exception:  # noqa: BLE001 — parity must never block provisioning repair
        logger.exception('HIVE-2695: scope parity check failed for %s', agent.agent_username)
    minted = _mint_agent_forgejo_token(agent, scopes)
    if not minted:
        logger.error(
            'forgejo-token: could not mint an Origin token for %s (identity %s); '
            'the agent will start without FORGEJO_TOKEN until this is resolved',
            agent.agent_username, ident,
        )
        return ''

    encoded = base64.b64encode(minted.encode()).decode()
    annotations = {
        # PLAT-10143: stamp the granted scopes so the scope-parity sweep can
        # detect drift between the mint-time grant and the seat's CURRENT
        # grant without guessing from token bytes.
        'shizuha.io/token-scopes': ','.join(scopes),
    }
    if scope_provisional:
        annotations['shizuha.io/scope-provisional'] = 'roster-unavailable'
    try:
        core_v1.patch_namespaced_secret(
            secret_name, ns,
            {
                'data': {'FORGEJO_TOKEN': encoded, 'ORIGIN_TOKEN': encoded},
                'metadata': {'annotations': annotations},
            },
        )
    except ApiException as exc:
        logger.error('forgejo-token: failed to persist token for %s: %s', agent.agent_username, exc)
        return ''
    _log_event(agent, 'backend_applied', 'Minted missing per-agent Origin/Forgejo token')
    logger.info('forgejo-token: minted Origin token for %s as %s', agent.agent_username, ident)
    return hashlib.sha256(encoded.encode()).hexdigest()[:16]


def _secret_env_matches(current: client.V1EnvVar | None, desired: client.V1EnvVar) -> bool:
    """Compare only the secret-backed contract fields used by this renderer."""
    if current is None or getattr(current, 'value', None) is not None:
        return False
    current_source = getattr(current, 'value_from', None)
    desired_source = getattr(desired, 'value_from', None)
    current_ref = getattr(current_source, 'secret_key_ref', None)
    desired_ref = getattr(desired_source, 'secret_key_ref', None)
    if current_ref is None or desired_ref is None:
        return False
    return (
        current_ref.name == desired_ref.name
        and current_ref.key == desired_ref.key
        and bool(current_ref.optional) == bool(desired_ref.optional)
    )


def _ensure_namespace(core_v1, ns: str, agent: FleetAgent):
    try:
        core_v1.read_namespace(name=ns)
    except ApiException as e:
        if e.status != 404:
            raise
        core_v1.create_namespace(
            body=client.V1Namespace(
                metadata=client.V1ObjectMeta(
                    name=ns,
                    labels={
                        'app.kubernetes.io/managed-by': MANAGED_BY,
                        'shizuha.io/fleet': 'true',
                        'shizuha.io/isolated': 'true' if agent.isolated else 'false',
                    },
                )
            )
        )
        logger.info(f'Created fleet namespace {ns}')


def _ensure_coordinator_oauth_secret(core_v1, ns: str, agent: FleetAgent) -> bool:
    """Project the exact control-plane managed client, never rotate/adopt drift.

    Unlike the historical bearer, OAuth credentials have one canonical source
    in Hive's namespace. Shared-fleet pods also need an explicit projection.
    """
    import base64
    from hive_platform.coordinator import COORDINATOR_SERVICE_CLIENT_ID
    name = settings.FLEET_COORDINATOR_OAUTH_SECRET_NAME
    source_ns = getattr(settings, 'HIVE_NAMESPACE', 'shizuha-hive')
    try:
        source = core_v1.read_namespaced_secret(name, source_ns)
    except ApiException as exc:
        if exc.status != 404:
            raise
        logger.error('Coordinator OAuth source Secret is absent in %s', source_ns)
        return False
    data = dict(source.data or {})
    try:
        values = {key: base64.b64decode(data[key], validate=True).decode()
                  for key in ('client-id', 'client-secret', 'token-url')}
        valid = (values['client-id'] == COORDINATOR_SERVICE_CLIENT_ID
                 and values['token-url'] == 'http://shizuha-id.shizuha.svc.cluster.local:8001/api/oauth/token'
                 and len(values['client-secret']) >= 32
                 and not any(c.isspace() for c in values['client-secret']))
    except (KeyError, ValueError, UnicodeError, TypeError):
        valid = False
    if not valid:
        raise RuntimeError('Coordinator OAuth source Secret contract is invalid')
    if ns == source_ns:
        return True
    try:
        existing = core_v1.read_namespaced_secret(name, ns)
    except ApiException as exc:
        if exc.status != 404:
            raise
    else:
        if dict(existing.data or {}) != data:
            raise RuntimeError('Coordinator OAuth projected Secret differs from canonical source')
        return True
    body = client.V1Secret(
        metadata=client.V1ObjectMeta(name=name, namespace=ns, labels=_base_labels(agent)),
        type=source.type or 'Opaque', data=data,
    )
    try:
        core_v1.create_namespaced_secret(ns, body)
    except ApiException as exc:
        if exc.status != 409:
            raise
        winner = core_v1.read_namespaced_secret(name, ns)
        if dict(winner.data or {}) != data:
            raise RuntimeError('Coordinator OAuth concurrent projection differs from canonical source')
    return True


def _ensure_coordinator_broker_token(core_v1, ns: str, agent: FleetAgent) -> bool:
    """HIVE-1377: project the shared coordinator-broker-token Secret into a
    per-agent namespace BEFORE its broker Deployment renders.

    The broker sidecar's `MCP_AUTH_PROXY_COORDINATOR_TOKEN` env is a
    namespace-scoped ``secretKeyRef`` with ``optional=False``. When coordinator
    model tokens are enabled, a per-agent namespace that renders a broker sidecar
    MUST already contain the ``FLEET_COORDINATOR_TOKEN_SECRET_NAME`` Secret.
    Provisioning created the namespace, the identity Secret and the PVC but never
    projected this shared Secret, so agent-shizuha-116 sat in
    ``CreateContainerConfigError`` / ``CrashLoopBackOff`` — kubelet could not
    resolve the reference the Deployment pointed at.

    We COPY (never move) the shared Secret into the agent namespace, Hive
    labelled, so the broker can start and Hive stays the single source of truth
    (no side-channel ``kubectl apply`` that would desync the reconcile).

    Idempotent: an existing Secret in the target namespace short-circuits with
    its bytes preserved. Returns ``False`` (recording why) when the REQUIRED
    source Secret exists nowhere — the broker could not start either way, so
    fail closed at the render boundary rather than crash-looping for hours.
    """
    if getattr(settings, 'FLEET_COORDINATOR_OAUTH_SECRET_NAME', ''):
        return _ensure_coordinator_oauth_secret(core_v1, ns, agent)
    if not _coordinator_model_token_enabled():
        return True
    secret_name = str(getattr(settings, 'FLEET_COORDINATOR_OAUTH_SECRET_NAME', '')
                      or getattr(settings, 'FLEET_COORDINATOR_TOKEN_SECRET_NAME', '') or '').strip()
    if not secret_name:
        return True
    if ns == fleet_namespace():
        return True  # shared-ns agents live beside the shared Secret already
    try:
        core_v1.read_namespaced_secret(secret_name, ns)
        return True  # already projected by a prior provision/reconcile
    except ApiException as e:
        if e.status != 404:
            raise
    source = None
    for probe_ns in (fleet_namespace(), getattr(settings, 'HIVE_NAMESPACE', 'shizuha-hive')):
        try:
            source = core_v1.read_namespaced_secret(secret_name, probe_ns)
            break
        except ApiException as e:
            if e.status != 404:
                raise
    if source is None:
        reason = (
            f'{secret_name} is referenced by the broker sidecar in {ns} but '
            f'exists in neither {fleet_namespace()} nor shizuha-hive; provision '
            'the coordinator broker token Secret first (HIVE-1377)'
        )
        logger.error('HIVE-1377: %s', reason)
        return False
    body = client.V1Secret(
        metadata=client.V1ObjectMeta(
            name=secret_name, namespace=ns, labels=_base_labels(agent),
        ),
        type=source.type or 'Opaque',
        data=dict(source.data or {}),  # faithful byte copy (base64)
    )
    try:
        core_v1.create_namespaced_secret(ns, body)
    except ApiException as e:
        if e.status != 409:
            raise
    logger.info('HIVE-1377: projected %s into %s (managed-by: shizuha-hive)', secret_name, ns)
    return True


def _ensure_projection_keyring(core_v1, ns: str, agent: FleetAgent) -> bool:
    """Copy only the signer public keyring into an isolated agent namespace.

    The private signing key never leaves shizuha-hive. The shared fleet
    namespace receives the public-only Secret too, so pod specs never reference
    the signer Secret directly and Kubernetes cannot expose its other keys.
    """
    secret_name = MCP_PROJECTION_SIGNER_SECRET
    target_name = MCP_PROJECTION_PUBLIC_SECRET
    try:
        source = core_v1.read_namespaced_secret(
            secret_name, getattr(settings, 'HIVE_NAMESPACE', 'shizuha-hive'),
        )
    except ApiException as exc:
        if exc.status == 404:
            logger.error('PLAT-5275: %s/keyring.json is unavailable', secret_name)
            return False
        raise
    keyring = (source.data or {}).get('keyring.json')
    if not keyring:
        logger.error('PLAT-5275: %s is missing keyring.json', secret_name)
        return False
    desired_data = {'keyring.json': keyring}
    body = client.V1Secret(
        metadata=client.V1ObjectMeta(name=target_name, namespace=ns, labels=_base_labels(agent)),
        type='Opaque', data=desired_data,
    )
    try:
        current = core_v1.read_namespaced_secret(target_name, ns)
        if dict(current.data or {}) == desired_data:
            return True
        current.data = desired_data
        current.type = 'Opaque'
        current.metadata.labels = _base_labels(agent)
        core_v1.replace_namespaced_secret(target_name, ns, current)
    except ApiException as exc:
        if exc.status != 404:
            raise
        try:
            core_v1.create_namespaced_secret(ns, body)
        except ApiException as create_exc:
            if create_exc.status != 409:
                raise
    return True


def _create_identity_secret(core_v1, ns: str, agent: FleetAgent, agent_password: str, user_token: str):
    """
    Per-agent identity Secret (HLD §5/§6). Holds the crown-jewel AGENT_PASSWORD,
    mounted ONLY into the broker sidecar. Never referenced by the agent container.
    """
    # HIVE-1750: AGENT_ID is REQUIRED, not optional. The render site refuses an
    # empty agent_id (HIVE-1502); this Secret site must refuse too rather than
    # coerce to '' — a call-graph contingency ("only one caller today") is not
    # an invariant, and it is exactly the protection that failed for the
    # container path before a second caller appeared.
    agent_id = (agent.agent_id or '').strip()
    if not agent_id:
        raise ValueError(
            f"refusing to create identity Secret for {agent}: agent_id is empty "
            "(HIVE-1502/HIVE-1750)"
        )
    string_data = {
        'AGENT_ID': agent_id,
        'AGENT_USERNAME': agent.agent_username,
    }
    if agent_password:
        string_data['AGENT_PASSWORD'] = agent_password
    if user_token:
        string_data['ACCESS_TOKEN'] = user_token

    try:
        core_v1.create_namespaced_secret(
            ns,
            client.V1Secret(
                metadata=client.V1ObjectMeta(
                    name=agent.identity_secret_name, namespace=ns, labels=_base_labels(agent),
                ),
                string_data=string_data,
            ),
        )
    except ApiException as e:
        if e.status != 409:
            raise
        # Reprovision/retry: preserve crown-jewel keys we are NOT explicitly
        # setting this call (review #2). A spec-only reprovision omits
        # agent_password/user_token — overwriting would wipe the stored
        # AGENT_PASSWORD/ACCESS_TOKEN the broker sidecar depends on. Read the live
        # Secret, keep its existing keys (as base64 .data), and overlay only the
        # keys we're changing (string_data wins over data on conflict).
        current = core_v1.read_namespaced_secret(agent.identity_secret_name, ns)
        preserved = dict(current.data or {})  # base64-encoded values
        merged = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name=agent.identity_secret_name, namespace=ns, labels=_base_labels(agent),
                resource_version=current.metadata.resource_version,
            ),
            data=preserved,
            string_data=string_data,
        )
        core_v1.replace_namespaced_secret(agent.identity_secret_name, ns, merged)


def _read_identity_secret_snapshot(core_v1, ns: str, agent: FleetAgent):
    """Return the current identity Secret, or None if this provision creates it.

    PLAT-673 uses this as the rollback point when an auto-generated password has
    been written to k8s but the matching shizuha-id update fails.
    """
    try:
        return core_v1.read_namespaced_secret(agent.identity_secret_name, ns)
    except ApiException as e:
        if e.status == 404:
            return None
        raise


def _secret_data_text(secret, key: str) -> str:
    """Decode a base64 Secret .data value without exposing it in logs."""
    data = getattr(secret, 'data', None) or {}
    value = data.get(key)
    if not value:
        return ''
    try:
        return base64.b64decode(value).decode('utf-8')
    except Exception:  # noqa: BLE001
        logger.warning('PLAT-673: could not decode %s from existing identity Secret', key)
        return ''


def _restore_identity_secret(core_v1, ns: str, agent: FleetAgent, snapshot) -> None:
    """Restore the identity Secret to its pre-PLAT-673 state.

    If there was no previous Secret, remove the newly-created Secret. If there
    was one, replace only with its prior base64 data and labels so the k8s store
    continues to match the password that shizuha-id still accepts.
    """
    if snapshot is None:
        _delete_if_exists(core_v1.delete_namespaced_secret, agent.identity_secret_name, ns)
        return

    current = core_v1.read_namespaced_secret(agent.identity_secret_name, ns)
    metadata = client.V1ObjectMeta(
        name=agent.identity_secret_name,
        namespace=ns,
        labels=getattr(getattr(snapshot, 'metadata', None), 'labels', None) or _base_labels(agent),
        resource_version=current.metadata.resource_version,
    )
    restored = client.V1Secret(
        metadata=metadata,
        data=dict(getattr(snapshot, 'data', None) or {}),
        type=getattr(snapshot, 'type', None) or 'Opaque',
    )
    core_v1.replace_namespaced_secret(agent.identity_secret_name, ns, restored)


def _workspace_storage_class(agent: FleetAgent) -> str:
    return agent.storage_class or getattr(settings, 'RUNTIME_STORAGE_CLASS', 'longhorn')


# Daemon-era k3s agents mounted `{username}-workspace`. Hive native names the
# claim `agent-{username}-workspace`. A live Deployment may still pin the
# legacy name (Sara 2026-08-22: claim=sara-workspace, Hive SoT
# agent-sara-workspace). Always act on the bound volume.
WORKSPACE_ENOSPC_MIN_SIZE = '50Gi'
_ENOSPC_LOG_HINT = re.compile(
    r'ENOSPC|SQLITE_IOERR_SHMSIZE|no space left on device',
    re.IGNORECASE,
)


def workspace_claim_aliases(agent: FleetAgent) -> list[str]:
    username = str(agent.agent_username or '').strip()
    names: list[str] = []
    for name in (
        agent.workspace_pvc_name,
        f'{username}-workspace' if username else '',
        f'agent-{username}-workspace' if username else '',
    ):
        if name and name not in names:
            names.append(name)
    return names


def bound_workspace_claim(core_v1, apps_v1, ns: str, agent: FleetAgent) -> str:
    """PVC name the live Deployment actually mounts, else the first existing alias."""
    if apps_v1 is not None:
        try:
            deploy = apps_v1.read_namespaced_deployment(agent.deployment_name, ns)
            volumes = (
                getattr(
                    getattr(getattr(getattr(deploy, 'spec', None), 'template', None), 'spec', None),
                    'volumes',
                    None,
                )
                or []
            )
            for vol in volumes:
                pvc = getattr(vol, 'persistent_volume_claim', None)
                claim = str(getattr(pvc, 'claim_name', '') or '').strip()
                if claim:
                    return claim
        except Exception:  # noqa: BLE001
            pass
    for name in workspace_claim_aliases(agent):
        try:
            core_v1.read_namespaced_persistent_volume_claim(name, ns)
            return name
        except ApiException as e:
            if e.status != 404:
                raise
        except Exception:  # noqa: BLE001
            continue
    return agent.workspace_pvc_name


def _pod_logs_look_enospc(core_v1, ns: str, agent: FleetAgent) -> bool:
    try:
        pods = core_v1.list_namespaced_pod(
            ns, label_selector=f'app={agent.app_label}',
        )
    except Exception:  # noqa: BLE001
        return False
    for pod in getattr(pods, 'items', None) or []:
        name = str(getattr(getattr(pod, 'metadata', None), 'name', '') or '')
        if not name:
            continue
        for previous in (False, True):
            try:
                log = core_v1.read_namespaced_pod_log(
                    name, ns, container='agent', tail_lines=80, previous=previous,
                )
            except Exception:  # noqa: BLE001
                continue
            if _ENOSPC_LOG_HINT.search(str(log or '')):
                return True
    return False


def heal_workspace_disk(agent: FleetAgent) -> bool:
    """Grow a full agent workspace onto cluster storage that still has space.

    Operator 2026-08-22: Sara CrashLoopBackOff ENOSPC/SQLITE_IOERR_SHMSIZE on a
    10Gi volume while gx10-14 had ~640Gi free. Relocate/grow, don't pin the
    seat to a node-local 10Gi that cannot move.
    """
    ns = _live_ns(agent)
    try:
        core_v1, apps_v1, _n = get_k8s_client()
    except Exception as exc:  # noqa: BLE001
        logger.warning('heal_workspace_disk %s: no k8s client: %s', agent.agent_username, exc)
        return False
    if not _pod_logs_look_enospc(core_v1, ns, agent):
        return False
    claim = bound_workspace_claim(core_v1, apps_v1, ns, agent)
    try:
        pvc = core_v1.read_namespaced_persistent_volume_claim(claim, ns)
    except ApiException as e:
        logger.warning('heal_workspace_disk %s: PVC %s unreadable: %s', agent.agent_username, claim, e)
        return False
    cur_size = str((getattr(getattr(pvc, 'spec', None), 'resources', None) or None)
                   and (pvc.spec.resources.requests or {}).get('storage') or '') or '10Gi'
    target = WORKSPACE_ENOSPC_MIN_SIZE
    if _quantity_bytes(target) <= _quantity_bytes(cur_size):
        target = cur_size
        # Already at/above the floor — still record SoT so the next render
        # does not recreate a 10Gi claim.
    else:
        try:
            core_v1.patch_namespaced_persistent_volume_claim(
                claim, ns,
                {'spec': {'resources': {'requests': {'storage': target}}}},
            )
            logger.info(
                'heal_workspace_disk %s: expanded %s %s -> %s',
                agent.agent_username, claim, cur_size, target,
            )
        except ApiException as pe:
            logger.warning(
                'heal_workspace_disk %s: expand %s %s -> %s failed: %s',
                agent.agent_username, claim, cur_size, target, pe.reason,
            )
            return False
    desired_sc = _workspace_storage_class(agent)
    updates = []
    if (agent.pvc_size or '') != target:
        agent.pvc_size = target
        updates.append('pvc_size')
    live_sc = str(getattr(getattr(pvc, 'spec', None), 'storage_class_name', '') or '')
    if live_sc and live_sc != 'local-path' and not (agent.storage_class or ''):
        agent.storage_class = live_sc
        updates.append('storage_class')
    elif desired_sc and desired_sc != 'local-path' and not (agent.storage_class or ''):
        agent.storage_class = desired_sc
        updates.append('storage_class')
    # UUID PKs are assigned at construction, so `pk` is set on unsaved
    # instances. Only persist when this row already exists in the DB.
    if updates and not getattr(getattr(agent, '_state', None), 'adding', True):
        updates.append('updated_at')
        agent.save(update_fields=updates)
    return True


def _create_workspace_pvc(core_v1, ns: str, agent: FleetAgent):
    storage_class = _workspace_storage_class(agent)
    size = agent.pvc_size or getattr(settings, 'RUNTIME_PVC_SIZE', '10Gi')
    claim = bound_workspace_claim(core_v1, None, ns, agent)
    pvc = client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(
            name=claim, namespace=ns, labels=_base_labels(agent),
        ),
        spec=client.V1PersistentVolumeClaimSpec(
            access_modes=['ReadWriteOnce'],
            storage_class_name=storage_class,
            resources=client.V1VolumeResourceRequirements(requests={'storage': size}),
        ),
    )
    try:
        core_v1.create_namespaced_persistent_volume_claim(ns, pvc)
    except ApiException as e:
        if e.status != 409:
            raise
        # P2 (aoi): PVC already exists. A PVC's size is NOT recreatable in place —
        # shrinking is impossible and growing needs a volume-expansion patch (only
        # if the StorageClass allows it). Handle the size delta explicitly instead
        # of silently ignoring the requested change.
        try:
            current = core_v1.read_namespaced_persistent_volume_claim(claim, ns)
            cur_size = (current.spec.resources.requests or {}).get('storage')
        except ApiException:
            cur_size = None
        if cur_size and cur_size != size:
            if _quantity_bytes(size) > _quantity_bytes(cur_size):
                try:
                    core_v1.patch_namespaced_persistent_volume_claim(
                        claim, ns,
                        {'spec': {'resources': {'requests': {'storage': size}}}},
                    )
                    logger.info(f'PVC {claim}: expanded {cur_size} -> {size}')
                except ApiException as pe:
                    logger.warning(
                        f'PVC {claim}: expand {cur_size} -> {size} failed '
                        f'(StorageClass may not allow expansion): {pe.reason}'
                    )
            else:
                logger.warning(
                    f'PVC {claim}: requested {size} < existing {cur_size}; '
                    f'PVCs cannot shrink — keeping {cur_size}.'
                )


def _quantity_bytes(q: str) -> int:
    """Parse a k8s storage quantity (e.g. '10Gi', '500Mi') to bytes for compare."""
    units = {'Ki': 1024, 'Mi': 1024**2, 'Gi': 1024**3, 'Ti': 1024**4,
             'K': 1000, 'M': 1000**2, 'G': 1000**3, 'T': 1000**4}
    s = (q or '').strip()
    for u, mult in units.items():
        if s.endswith(u):
            try:
                return int(float(s[:-len(u)]) * mult)
            except ValueError:
                return 0
    try:
        return int(s)
    except ValueError:
        return 0


def _live_fleet_image() -> str:
    """Return only the fresh, validated reviewed-release projection.

    There is deliberately no cache, daemon-image, or static-setting fallback.
    Provisioning a native runtime without current reviewed intent would create
    an unreviewed release decision inside Hive, so callers fail closed instead.
    """
    from .native_runtime_release import enabled, get_native_runtime_release
    if enabled():
        return get_native_runtime_release().release.deployment_image
    from .runtime_release import get_authoritative_runtime_release

    return get_authoritative_runtime_release().release.deployment_image


def _bind_reviewed_runtime_image(agent: FleetAgent, *, preserve_native_intent: bool = False) -> str:
    """Attach reviewed native image intent to one in-memory actuation request."""
    if not _uses_native_runtime_image(agent):
        return str(agent.image or '').strip()
    from .native_runtime_release import enabled, get_native_runtime_release, row_candidate
    if enabled():
        from .runtime_release import RuntimeReleaseProjectionUnavailable
        if not row_candidate(agent):
            raise RuntimeReleaseProjectionUnavailable('Native release refused a deleted or unbound desired row')
        authority = get_native_runtime_release()
        from .native_runtime_release import bind_intent, assert_owned_deployment, validate_applied_release
        if preserve_native_intent:
            from .native_runtime_release import assert_still_current, intent_guard
            original = getattr(agent, '_native_runtime_release', None)
            if original is None:
                raise RuntimeReleaseProjectionUnavailable('Native nested action lost its original authority')
            if (authority.uid, authority.document_hash) != (original.uid, original.document_hash):
                raise RuntimeReleaseProjectionUnavailable('Native nested action changed release intent')
            assert_still_current(original)
            with intent_guard(agent):
                pass
        else:
            bind_intent(agent)
        _, apps, _ = get_k8s_client()
        try:
            existing = apps.read_namespaced_deployment(agent.deployment_name, _live_ns(agent))
        except ApiException as exc:
            if exc.status != 404:
                raise
        else:
            assert_owned_deployment(agent, existing)
            validate_applied_release(authority, existing, allow_cold=True)
        agent._native_runtime_release = authority
        agent._reviewed_runtime_image = authority.release.deployment_image
        return agent._reviewed_runtime_image
    already_bound = str(
        getattr(agent, '_reviewed_runtime_image', '') or ''
    ).strip()
    if already_bound:
        return already_bound
    image = _live_fleet_image()
    agent._reviewed_runtime_image = image
    return image


def _resolve_image(agent: FleetAgent) -> str:
    if not _uses_native_runtime_image(agent):
        return agent.image
    reviewed = str(getattr(agent, '_reviewed_runtime_image', '') or '').strip()
    if reviewed:
        return reviewed
    # Rendering is intentionally pure. Public create/wake entry points bind a
    # fresh reviewed projection before reaching here; this fallback exists for
    # non-actuating object construction and preserves explicit managed rows.
    return str(agent.image or '').strip() or getattr(settings, 'FLEET_AGENT_IMAGE', '')


# Execution METHOD -> runtime bridge command, mirroring the fleet daemon's
# k8s-native inline launcher. The DEFAULT is `gateway` (the native self-hosted
# loop that talks to Cortex over chat/completions) — NOT codex-bridge. SCLI-331:
# the image entrypoint historically hardcoded codex-bridge, so a method=shizuha
# cortex/DeepSeek agent ran codex-bridge, spoke the OpenAI `responses` API Cortex
# doesn't serve, fell back to api.openai.com → 401 → empty turns. Resolving the
# bridge HERE (at provision time) makes the pod launch the right runtime
# regardless of the image's entrypoint version — no image rebuild required.
_METHOD_TO_BRIDGE = {
    'claude_code_server': 'claude-bridge', 'claude': 'claude-bridge',
    'codex_app_server': 'codex-bridge', 'codex': 'codex-bridge',
    'openclaw_bridge': 'openclaw-bridge',
    'antigravity_server': 'antigravity-bridge', 'antigravity': 'antigravity-bridge',
    # Gemini CLI permanently removed — legacy method names → Antigravity CLI.
    'gemini_cli_server': 'antigravity-bridge', 'gemini': 'antigravity-bridge',
    'grok_build': 'gateway', 'shizuha': 'gateway',
}


def _resolve_execution_method(agent: FleetAgent) -> str:
    """Authoritative execution method: mcp_config first (runtime SoT), then fallbacks."""
    mcp = agent.mcp_config or {}
    method = (mcp.get('execution_method') or '').strip()
    if method:
        # Permanent rename: never launch Gemini CLI.
        if method in ('gemini_cli_server', 'gemini'):
            return 'antigravity_server'
        return method
    mf = agent.model_fallbacks or []
    if isinstance(mf, list) and mf and isinstance(mf[0], dict):
        method = (mf[0].get('method') or '').strip()
        if method in ('gemini_cli_server', 'gemini'):
            return 'antigravity_server'
        if method:
            return method
    return ''


def _resolve_bridge(agent: FleetAgent) -> str:
    """The runtime bridge command for this agent's primary execution method."""
    method = _resolve_execution_method(agent)
    cmd = _METHOD_TO_BRIDGE.get(method)
    if cmd:
        return cmd
    # No explicit method: only OpenAI codex/gpt models need codex-bridge (Cortex
    # does not serve them); gemini/antigravity models use Antigravity CLI;
    # everything else — self-hosted cortex/* included — takes the native gateway.
    model = agent.model or ''
    if re.match(r'^(codex/|gpt)', model, re.I):
        return 'codex-bridge'
    if re.match(r'^(gemini|antigravity|agy)', model, re.I):
        return 'antigravity-bridge'
    return 'gateway'


def _resolve_model(agent: FleetAgent) -> str:
    from .runtime_lane import remap_retired_fleet_model
    return remap_retired_fleet_model(
        agent.model or getattr(settings, 'FLEET_AGENT_MODEL', '')
    )


def _memory_mb(value: str | int | None) -> int:
    """Parse a k8s memory quantity into MiB for comparison.

    Handles the plain-byte form ("536870912"), the binary suffixes the fleet
    profiles use ("256Mi", "6Gi", "8Gi"), and the decimal suffix ("1G"). Unknown
    or malformed values return 0 so callers treat them as "no floor to keep".
    """
    if value is None:
        return 0
    text = str(value).strip()
    if not text:
        return 0
    m = re.fullmatch(r'(\d+(?:\.\d+)?)\s*([KMGTPE]i?|k|m|g|t|p|e)?', text, re.I)
    if not m:
        return 0
    number = float(m.group(1))
    suffix = (m.group(2) or '').lower()
    binary = suffix.endswith('i')  # Ki/Mi/Gi = powers of 1024
    if not suffix:
        return int(number // (1024 * 1024))
    if binary:
        # Ki/Mi/Gi/Ti are powers of 1024; express the result in MiB.
        units = {'ki': -1, 'mi': 0, 'gi': 1, 'ti': 2, 'pi': 3, 'ei': 4}
        return int(number * (1024 ** units[suffix]))
    units = {'k': 1, 'm': 2, 'g': 3, 't': 4, 'p': 5, 'e': 6}
    return int(number * (1000 ** units[suffix]) / (1024 * 1024))


def _resolve_resources(agent: FleetAgent) -> client.V1ResourceRequirements:
    if agent.resource_overrides:
        spec = agent.resource_overrides
    else:
        profiles = getattr(settings, 'FLEET_RESOURCE_PROFILES', DEFAULT_RESOURCE_PROFILES)
        spec = profiles.get(agent.resource_profile, DEFAULT_RESOURCE_PROFILES['standard'])
        # Native shizuha-agent-runtime pods execute the same stateful bridge or
        # gateway process as daemon-managed agents. Legacy Hive defaults left
        # bridge pods at 2Gi (Sato OOM-killed every ~7m). Enforce the runtime
        # floor even when a stale settings profile still carries the old value;
        # explicit per-agent overrides remain authoritative.
        #
        # PLAT-6397: the floor is a MINIMUM, not a replacement. The previous
        # code overwrote the profile's memory with the floor, so a heavy
        # gateway agent (8Gi profile) was silently reduced to 6Gi and
        # OOM-killed (agent-nagi, 14x CrashLoopBackOff). A heavy 300K-token
        # session spikes to 4-6GB; the floor must never LOWER a profile that is
        # already at/above it.
        if not agent.image or 'shizuha-agent-runtime' in agent.image:
            limits = dict(spec.get('limits') or {})
            floor = '8Gi' if _resolve_bridge(agent) != 'gateway' else '6Gi'
            if _memory_mb(limits.get('memory')) < _memory_mb(floor):
                limits['memory'] = floor
            spec = {**spec, 'limits': limits}
    return client.V1ResourceRequirements(
        requests=spec.get('requests'), limits=spec.get('limits'),
    )


def _node_selector(agent: FleetAgent) -> dict:
    sel = {'kubernetes.io/arch': agent.arch}
    if agent.requires_longhorn:
        sel['storage.longhorn.io/ready'] = 'true'
    if agent.node_pool:
        sel['shizuha.io/node-pool'] = agent.node_pool
    if agent.node_name:
        sel['kubernetes.io/hostname'] = agent.node_name
    sel.update(agent.node_selector_overrides or {})
    return sel


def _workspace_volume_hostname(agent: FleetAgent) -> str:
    """Hostname a bound workspace PV is pinned to, if any.

    local-path (and other WaitForFirstConsumer) volumes carry nodeAffinity on
    the PV. That pin is a legitimate scheduling constraint (cluster-scheduling
    storage-locality). ``agent.node_name`` is the Hive-side pin when set.
    Fail-closed to '' when the API is unreadable so tests and dry renders stay
    cluster-free.
    """
    pinned = (getattr(agent, 'node_name', None) or '').strip()
    if pinned:
        return pinned
    try:
        core_v1, _, _ = get_k8s_client()
        ns = _live_ns(agent)
        claim = agent.workspace_pvc_name
        pvc = core_v1.read_namespaced_persistent_volume_claim(claim, ns)
        vol = ((pvc.spec.volume_name or '') if pvc.spec else '')
        if not vol:
            return ''
        pv = core_v1.read_persistent_volume(vol)
    except Exception:
        return ''
    return _bound_volume_hostname(pv)


def _bound_volume_hostname(volume) -> str:
    affinity = getattr(getattr(volume, 'spec', None), 'node_affinity', None)
    required = (
        getattr(affinity, 'required', None)
        or getattr(affinity, 'required_during_scheduling_ignored_during_execution', None)
    )
    for term in getattr(required, 'node_selector_terms', None) or []:
        for expr in getattr(term, 'match_expressions', None) or []:
            if getattr(expr, 'key', '') == 'kubernetes.io/hostname' and expr.values:
                return str(expr.values[0])
    return ''


def _hostname_is_control_plane(hostname: str) -> bool:
    """True when the named node currently carries a control-plane/etcd role label."""
    if not hostname:
        return False
    try:
        core_v1, _, _ = get_k8s_client()
        node = core_v1.read_node(hostname)
    except Exception:
        return False
    labels = (node.metadata.labels or {}) if node.metadata else {}
    return (
        'node-role.kubernetes.io/control-plane' in labels
        or 'node-role.kubernetes.io/etcd' in labels
    )


def _affinity(agent: FleetAgent, *, bound_control_plane: bool | None = None) -> client.V1Affinity:
    # (1) HARD — never schedule a fleet agent onto a control-plane/etcd node.
    #     Current control-plane/etcd nodes are protected by the standard role
    #     label, not by hard-coded hostnames. An OOM/runaway agent pod there can
    #     threaten etcd quorum, and taints drift over time; role-label exclusion
    #     remains robust as the control-plane set changes. Operator
    #     hard-constraint #1 — HIVE-45 / PLAT-146.
    #     EXCEPTION — storage-locality: when the workspace PV (or Hive
    #     node_name pin) is already bound to a control-plane hostname, the
    #     exclusion makes the pod unschedulable (0 nodes). CoS 2026-09-05:
    #     local-path PVC on gx10-15 + CP DoesNotExist → Pending 134m.
    #     Skip the exclusion in that case; keep etcd safe via memory limits
    #     + earlyoom (cluster-scheduling doctrine), not a second hard pin.
    # (2) SOFT — spread the ~25 agent pods across the remaining nodes (HLD §3),
    #     soft so provisioning never blocks if a node is momentarily unschedulable.
    # Review #4: select on a FLEET-WIDE label (every fleet pod carries
    # shizuha.io/agent), with the Exists operator — NOT shizuha.io/agent=<this>,
    # which never matches another pod (each agent is replicas=1) and is a no-op.
    # This makes the scheduler actually spread DIFFERENT agents across nodes.
    skip_cp_exclusion = bool(bound_control_plane)
    if bound_control_plane is None:
        bound = _workspace_volume_hostname(agent)
        if bound and _hostname_is_control_plane(bound):
            skip_cp_exclusion = True
    node_affinity = None
    if not skip_cp_exclusion:
        node_affinity = client.V1NodeAffinity(
            required_during_scheduling_ignored_during_execution=client.V1NodeSelector(
                node_selector_terms=[
                    client.V1NodeSelectorTerm(
                        match_expressions=[
                            client.V1NodeSelectorRequirement(
                                key='node-role.kubernetes.io/control-plane',
                                operator='DoesNotExist',
                            ),
                        ],
                    ),
                ],
            ),
        )
    return client.V1Affinity(
        node_affinity=node_affinity,
        pod_anti_affinity=client.V1PodAntiAffinity(
            preferred_during_scheduling_ignored_during_execution=[
                client.V1WeightedPodAffinityTerm(
                    weight=100,
                    pod_affinity_term=client.V1PodAffinityTerm(
                        topology_key='kubernetes.io/hostname',
                        label_selector=client.V1LabelSelector(
                            match_expressions=[
                                client.V1LabelSelectorRequirement(
                                    key='shizuha.io/agent', operator='Exists',
                                ),
                            ],
                        ),
                        # P2 (aoi): scope the term to fleet namespaces (shared +
                        # isolated, all carry shizuha.io/fleet=true on the ns) so
                        # spread is computed across the WHOLE fleet, not just the
                        # scheduling pod's own namespace (the k8s default).
                        namespace_selector=client.V1LabelSelector(
                            match_labels={'shizuha.io/fleet': 'true'},
                        ),
                    ),
                )
            ]
        )
    )


def _agent_container(agent: FleetAgent, has_broker: bool = False, has_dind: bool = False,
                     *, forgejo_secret_name: str | None = None) -> client.V1Container:
    # NON-secret env only. No AGENT_PASSWORD here (HLD §5) — the broker sidecar
    # mints the JWT and hands it over the pod-local socket (PLAT-149).
    # HIVE-73: always resolve platform_url from current Django settings at
    # provision time — not from agent.platform_url (stored at creation, which
    # may be stale if the deployment env was misconfigured when the agent was
    # first created).
    current_platform_url = getattr(settings, 'SHIZUHA_PLATFORM_URL', agent.platform_url)
    # HIVE-1502: hard invariant, not a courtesy check. The runtime entrypoint
    # guards `${AGENT_ID:?AGENT_ID required}`, which fires on empty as well as
    # unset, so rendering an empty value produces a pod that can never start.
    #
    # This lives HERE, in the single render site for the agent container, because
    # `provision_agent` is not the only caller: `ensure_agent_image` re-renders on
    # every image roll, which is the path that recreated the shizuha-116 pod 9
    # times over 4 days without once producing a Ready container. Guarding only
    # the provisioning entry point would have left that loop intact.
    if not (agent.agent_id or '').strip():
        raise ValueError(
            f'agent_id is empty for {agent.agent_username!r}; refusing to render '
            'an agent container whose entrypoint requires a non-empty AGENT_ID '
            '(HIVE-1502)'
        )
    env = [
        client.V1EnvVar(name='AGENT_ID', value=agent.agent_id),
        client.V1EnvVar(name='AGENT_USERNAME', value=agent.agent_username),
        client.V1EnvVar(name='AGENT_NAME', value=agent.display_name or agent.agent_username),
        client.V1EnvVar(name='SHIZUHA_PLATFORM_URL', value=current_platform_url),
        client.V1EnvVar(name='BACKEND_URL', value=current_platform_url),
        # Stream GLM tool turns so Cortex Usage records first-token TTFT instead
        # of non-stream e2e. GLM-5.1 glm47 used to leak XML into streamed names
        # (`pulse_get_task</arg_value>`) at large context; live GLM-5.3-Flash EXL3
        # (2026-09-06) streamed clean `pulse_get_task` + parseable args, and
        # bench STREAM=1 cells already pass with write/bash tools. Keep the key
        # on the denylist so agent.env cannot override it. vllm.ts still honors
        # VLLM_STREAM_WITH_TOOLS=0 if we must roll back.
        client.V1EnvVar(name='VLLM_STREAM_WITH_TOOLS', value='1'),
        # Cortex KV residency: fleet SCLI must send cache_control ttl=30m.
        # AGENT_ID/USERNAME exist but resolveCortexCacheControl looks for
        # SHIZUHA_AGENT_* (absent on live pods — registry.ts). Stamp both
        # the identity and an explicit TTL so even a pre-TTL image that
        # later picks up SCLI still homes Hive sessions for 30 minutes.
        client.V1EnvVar(name='SHIZUHA_AGENT_ID', value=agent.agent_id),
        client.V1EnvVar(name='SHIZUHA_AGENT_USERNAME', value=agent.agent_username),
        client.V1EnvVar(name='SHIZUHA_CACHE_TTL', value='30m'),
    ]
    if agent.role:
        env.append(client.V1EnvVar(name='AGENT_ROLE', value=agent.role))
    # PLAT-7463: cluster-plane seats point kubectl at the init-materialized
    # config (which references the kubelet-rotated tokenFile — no embedded
    # bearer bytes). The init container writes it before the agent starts.
    if agent.clusterplane_kubeconfig:
        env.append(client.V1EnvVar(
            name='KUBECONFIG', value='/home/agent/.kube/config',
        ))
    model = _resolve_model(agent)
    if model:
        env.append(client.V1EnvVar(name='MODEL', value=model))
    # HIVE-133: behavioral fields — inject only when set so runtime defaults apply
    # when blank (avoids overriding hardcoded defaults with empty strings).
    if agent.reasoning_effort:
        env.append(client.V1EnvVar(name='REASONING_EFFORT', value=agent.reasoning_effort))
    if agent.context_prompt:
        env.append(client.V1EnvVar(name='CONTEXT_PROMPT', value=agent.context_prompt))
    # NO MODEL FALLBACKS — one agent, one model (operator 2026-08-06: "let's
    # completely remove the concept of model fallbacks to avoid future
    # confusions"). MODEL_FALLBACKS / FAILOVER_CHAIN_ID are deliberately never
    # rendered; they remain in _RUNTIME_LANE_ENV_NAMES so the lane converge
    # DELETES any surviving value from a legacy template.
    if agent.model_overrides:
        import json as _json
        env.append(client.V1EnvVar(name='MODEL_OVERRIDES', value=_json.dumps(agent.model_overrides)))
    if agent.personality_traits:
        import json as _json
        env.append(client.V1EnvVar(name='PERSONALITY_TRAITS', value=_json.dumps(agent.personality_traits)))
    # Per-agent env overrides (injected last; denylist enforced — HIVE-133 security fix)
    from .installation_config import effective_agent_env
    for key, val in effective_agent_env(agent.env).items():
        if not (key and isinstance(val, str)):
            continue
        if key in _ENV_DENYLIST:
            logger.warning(
                'FleetAgent %s: env key %r is reserved and was not injected; '
                'use the dedicated FleetAgent field instead.',
                agent.agent_username, key,
            )
            continue
        if key == 'SHIZUHA_BROWSER_MCP_JWT_SECRET_FROM':
            continue
        env.append(client.V1EnvVar(name=key, value=val))
    # After agent.env so stale first-heartbeat / missing talk knobs win the
    # last-key-wins k8s merge. Must not run earlier — env overrides used to
    # re-stamp SHIZUHA_FIRST_HEARTBEAT_MS=1800000 and cancel the prefix warm.
    _apply_lean_conversational_runtime_env(agent, env)
    _apply_work_agent_runtime_env(agent, env)
    _apply_mcp_allow_list_env(agent, env)

    # Private browser MCP — one worker Service per agent (never shared). Bridges
    # attach when SHIZUHA_BROWSER_MCP_URL is set (cli/src/browser-mcp.ts).
    if agent_needs_browser_mcp(agent):
        env_names = {e.name for e in env}
        for key, val in browser_mcp_env_for_agent(agent).items():
            if key == 'SHIZUHA_BROWSER_MCP_JWT_SECRET_FROM':
                continue
            if key not in env_names:
                env.append(client.V1EnvVar(name=key, value=val))
                env_names.add(key)
        if 'SHIZUHA_BROWSER_MCP_JWT_SECRET' not in env_names:
            env.append(client.V1EnvVar(
                name='SHIZUHA_BROWSER_MCP_JWT_SECRET',
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(
                        name=BROWSER_MCP_JWT_SECRET_NAME,
                        key=BROWSER_MCP_JWT_SECRET_KEY,
                        optional=False,
                    ),
                ),
            ))
        if 'JWT_SECRET_KEY' not in env_names:
            env.append(client.V1EnvVar(
                name='JWT_SECRET_KEY',
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(
                        name=BROWSER_MCP_JWT_SECRET_NAME,
                        key=BROWSER_MCP_JWT_SECRET_KEY,
                        optional=False,
                    ),
                ),
            ))

    # Per-agent Cortex key (operator 2026-07-10 / 2026-07-28): each agent uses
    # ITS OWN key from ``<username>-agent-creds`` CORTEX_API_KEY so usage,
    # leases, and billing attribute to ``agent-<username>`` — never the shared
    # ``agents-fleet-own-fleet`` blob. The shared cortex-fleet-key is NOT
    # mounted as the primary key (that collapsed nova/rui traffic onto one
    # principal and broke per-agent leases). Google/Gemini keys may still ride
    # the shared secret when present; Cortex path is per-agent only.
    env.extend(_per_agent_cortex_key_env(agent))
    for _cortex_key_env in ('GOOGLE_API_KEY', 'GEMINI_API_KEY'):
        env.append(client.V1EnvVar(
            name=_cortex_key_env,
            value_from=client.V1EnvVarSource(
                secret_key_ref=client.V1SecretKeySelector(
                    name='cortex-fleet-key',
                    key='GOOGLE_API_KEY',
                    optional=True,
                ),
            ),
        ))
    # ORIG-18 / PLAT-4683: native Hive agents use the same per-agent Forgejo
    # Secret contract as daemon-rendered k3s agents. Keep token bytes out of
    # Hive; Kubernetes resolves both aliases from the canonical key at Pod
    # start. ``ensure_agent_image`` also repairs this contract before wake for
    # Deployments created before these refs existed.
    env.extend(_forgejo_credential_env(agent, secret_name=forgejo_secret_name))

    volume_mounts = [client.V1VolumeMount(name='workspace', mount_path=agent.workspace_mount_path)]
    if agent.shared_workspace:
        # PLAT-1056: fleet-wide RWX PVC for cross-agent file handoff
        # (Scout→Nori news catalog). The PVC (`agent-shared-workspace`,
        # Longhorn RWX) is a shared fleet resource — created once, never
        # per-agent, and never deleted by deprovision.
        volume_mounts.append(client.V1VolumeMount(name='shared-workspace', mount_path='/shared'))
    if has_dind:
        env.append(client.V1EnvVar(name='DOCKER_HOST', value=DOCKER_HOST))
        volume_mounts.extend([
            client.V1VolumeMount(name='docker-bin', mount_path='/usr/local/bin/docker', sub_path='docker'),
            client.V1VolumeMount(name='docker-run', mount_path='/var/run'),
        ])
    if agent.hostplane_ssh:
        volume_mounts.append(client.V1VolumeMount(name='fleet-ssh-home', mount_path='/home/agent/.ssh'))
    readiness = None
    command = None
    if has_broker:
        # Generic broker wiring — correct for ANY broker-consuming image: mount
        # the shared UDS dir + point the agent at the socket (PLAT-169 reads
        # MCP_AUTH_PROXY_SOCKET to fetch GET /token).
        volume_mounts.append(client.V1VolumeMount(name='broker-sock', mount_path=BROKER_SOCK_DIR))
        volume_mounts.append(client.V1VolumeMount(
            name='mcp-projection-keyring', mount_path=MCP_PROJECTION_TRUST_DIR,
            read_only=True,
        ))
        env.append(client.V1EnvVar(name='MCP_AUTH_PROXY_SOCKET', value=BROKER_SOCK_PATH))
        # HIVE-2058 (CON-363): broker-sidecar QA seats need the browser-login
        # credential ON the agent container, not just in the broker. The broker
        # mints MCP tokens from the identity Secret, but browser-e2e QA logs in
        # via /id/api/auth/login/ with AGENT_PASSWORD directly from the agent
        # container env — without it the seat cannot authenticate (CON-237).
        # Source it from the SAME identity Secret the broker envFrom uses
        # (agent-<username>-identity), which holds the live password, so a
        # daemon re-render keeps it (no manual patch, no stale <name>-agent-creds).
        env.append(client.V1EnvVar(
            name='AGENT_PASSWORD',
            value_from=client.V1EnvVarSource(
                secret_key_ref=client.V1SecretKeySelector(
                    name=agent.identity_secret_name,
                    key='AGENT_PASSWORD',
                    optional=False,
                ),
            ),
        ))
        if _coordinator_model_token_enabled():
            # HIVE-146/HIVE-125: bridge must fail closed once the broker can serve
            # model tokens over the UDS. Do not set this unless the broker is also
            # wired to Coordinator; otherwise agents would start without a token.
            env.append(client.V1EnvVar(name='CLAUDE_BRIDGE_REQUIRE_BROKER_MODEL_TOKEN', value='1'))
            env.append(client.V1EnvVar(name='CODEX_BRIDGE_REQUIRE_BROKER_MODEL_TOKEN', value='1'))

        # Default-image-only start-gate + readiness probe (review P2): both assume
        # the default runtime image's tooling (curl) and entrypoint, so only apply
        # them when this FleetAgent uses the default image. A custom `image` keeps
        # its own entrypoint and must handle broker readiness itself (the PLAT-175
        # runtime self-retry of /token covers the boot race generically).
        if _uses_native_runtime_image(agent):
            readyz_check = f'curl -fsS --unix-socket {BROKER_SOCK_PATH} http://localhost/readyz'
            readiness = client.V1Probe(
                _exec=client.V1ExecAction(command=['sh', '-c', readyz_check]),
                initial_delay_seconds=5, period_seconds=10, timeout_seconds=5,
                failure_threshold=6, success_threshold=1,
            )
            # Startup-ordering gate (proven needed by the PLAT-146 e2e): the broker
            # mints the JWT ~2s after start; the agent's Connect client fetches
            # /token ONCE at boot and disables Connect if not ready. Wait for the
            # broker /readyz, then exec the default entrypoint WITH the explicit,
            # method-resolved bridge + args (SCLI-331).
            command = _agent_launch_command(agent, wait_for_broker=True)
    elif _uses_native_runtime_image(agent):
        # No broker sidecar (scout 2026-09-01): still pin --cwd to the PVC so
        # hibernate/wakeup resumes the eternal session instead of creating a
        # new 12K document on the overlay.
        command = _agent_launch_command(agent, wait_for_broker=False)

    return client.V1Container(
        name='agent',
        image=_resolve_image(agent),
        image_pull_policy=agent.image_pull_policy,
        ports=[
            client.V1ContainerPort(name='http', container_port=agent.http_port, protocol='TCP'),
            client.V1ContainerPort(
                name='metrics', container_port=NATIVE_RUNTIME_METRICS_PORT, protocol='TCP',
            ),
        ],
        command=command,
        env=env,
        volume_mounts=volume_mounts,
        readiness_probe=readiness,
        resources=_resolve_resources(agent),
        # BRW-12 / PLAT-422: container-level security hardening — least-privilege
        # for all fleet agent containers. runAsUser=1000 matches the 'agent' user
        # in the runtime image; capabilities.drop=ALL removes every Linux capability
        # the kernel grants by default (net_bind_service, chown, etc.).
        security_context=client.V1SecurityContext(
            run_as_non_root=True,
            run_as_user=1000,
            allow_privilege_escalation=False,
            capabilities=client.V1Capabilities(drop=['ALL']),
        ),
    )


def _docker_cli_init_container() -> client.V1Container:
    return client.V1Container(
        name='docker-cli',
        image='docker:27-cli',
        image_pull_policy='IfNotPresent',
        command=['sh', '-c', 'cp /usr/local/bin/docker /docker-bin/docker && chmod 0755 /docker-bin/docker'],
        volume_mounts=[
            client.V1VolumeMount(name='docker-bin', mount_path='/docker-bin'),
        ],
        resources=client.V1ResourceRequirements(
            requests={'cpu': '25m', 'memory': '64Mi'}, limits={'cpu': '250m', 'memory': '256Mi'},
        ),
    )


def _dind_sidecar() -> client.V1Container:
    # WAN cache coverage (operator 2026-07-12): dind previously pulled Docker Hub
    # DIRECT — every agent's in-sandbox `docker run/pull` (test containers,
    # postgres/redis sidecars, playwright…) left the WAN, bypassing the
    # in-cluster pull-through mirror that node containerd already uses. Route
    # dockerd through mirror-dockerhub (HTTP → also mark insecure). dockerd
    # only supports mirroring Docker Hub; ghcr/quay pulls inside dind remain
    # direct (rare — the heavy repeat-pull traffic is Hub images).
    mirror = getattr(settings, 'FLEET_DIND_REGISTRY_MIRROR',
                     'http://mirror-dockerhub.registry.svc.cluster.local:5000')
    args = ['--host=unix:///var/run/docker.sock', '--group=1000']
    if mirror:
        args += [f'--registry-mirror={mirror}',
                 f'--insecure-registry={mirror.replace("http://", "").replace("https://", "")}']
    return client.V1Container(
        name='dind',
        image='docker:27-dind',
        image_pull_policy='IfNotPresent',
        command=['dockerd'],
        args=args,
        env=[client.V1EnvVar(name='DOCKER_TLS_CERTDIR', value='')],
        volume_mounts=[
            client.V1VolumeMount(name='docker-graph', mount_path='/var/lib/docker'),
            client.V1VolumeMount(name='docker-run', mount_path='/var/run'),
        ],
        resources=client.V1ResourceRequirements(
            requests={'cpu': '250m', 'memory': '512Mi'}, limits={'cpu': '2', 'memory': '4Gi'},
        ),
        security_context=client.V1SecurityContext(privileged=True),
    )


def _broker_sidecar(agent: FleetAgent) -> client.V1Container:
    # ADR-PLAT-002 / PLAT-149 broker = mcp-auth-proxy. The identity Secret — incl.
    # AGENT_PASSWORD — is mounted ONLY here via envFrom; it mints the shizuha-id
    # JWT and serves it to the agent over the pod-local UDS GET /token.
    #   - PasswordMint enable-gate = AGENT_USERNAME + AGENT_PASSWORD (from the
    #     Secret) + an ID login URL (from MCP_AUTH_PROXY_ID_BASE_URL).
    #   - MCP_AUTH_PROXY_EXPECTED_AGENT_UID is MANDATORY (the broker fail-closed
    #     log.Fatals without it — the SO_PEERCRED gate for /token). Agent runs
    #     as uid 1000 (the runtime image's `agent` user).
    broker_image = getattr(settings, 'FLEET_BROKER_IMAGE', None) or 'mcp-auth-proxy:latest'
    env = [
        # HIVE-73: resolve from current settings at provision time (not agent.platform_url
        # which may have been persisted with a stale misconfigured value).
        client.V1EnvVar(name='MCP_AUTH_PROXY_ID_BASE_URL',
                        value=getattr(settings, 'SHIZUHA_PLATFORM_URL', agent.platform_url)),
        client.V1EnvVar(name='MCP_AUTH_PROXY_EXPECTED_AGENT_UID', value='1000'),
        client.V1EnvVar(name='MCP_AUTH_PROXY_SOCKET', value=BROKER_SOCK_PATH),
    ]
    env.extend([
        client.V1EnvVar(name='AGENT_ID', value=agent.agent_id),
        client.V1EnvVar(name='MCP_AUTH_PROXY_PROJECTION_URL', value=MCP_PROJECTION_URL),
    ])
    if _coordinator_model_token_enabled():
        env.append(client.V1EnvVar(
            name='MCP_AUTH_PROXY_COORDINATOR_URL',
            value=getattr(settings, 'FLEET_COORDINATOR_MODEL_TOKEN_URL', ''),
        ))
        oauth_secret = getattr(settings, 'FLEET_COORDINATOR_OAUTH_SECRET_NAME', '')
        if oauth_secret:
            from hive_platform.coordinator import COORDINATOR_SERVICE_SCOPE
            for suffix, key in [('TOKEN_URL', 'token-url'), ('CLIENT_ID', 'client-id'),
                                ('CLIENT_SECRET', 'client-secret')]:
                env.append(client.V1EnvVar(
                    name='MCP_AUTH_PROXY_COORDINATOR_OAUTH_' + suffix,
                    value_from=client.V1EnvVarSource(secret_key_ref=client.V1SecretKeySelector(
                        name=oauth_secret, key=key, optional=False,
                    )),
                ))
            env.append(client.V1EnvVar(name='MCP_AUTH_PROXY_COORDINATOR_OAUTH_SCOPE',
                                       value=COORDINATOR_SERVICE_SCOPE))
        else:
            # Explicit compatibility for standalone historical deployments.
            # OAuth mode above never renders the broken static credential.
            env.append(client.V1EnvVar(
                name='MCP_AUTH_PROXY_COORDINATOR_TOKEN',
                value_from=client.V1EnvVarSource(secret_key_ref=client.V1SecretKeySelector(
                    name=getattr(settings, 'FLEET_COORDINATOR_TOKEN_SECRET_NAME', ''),
                    key=getattr(settings, 'FLEET_COORDINATOR_TOKEN_SECRET_KEY', 'token'),
                )),
            ))
    return client.V1Container(
        name='broker',
        image=broker_image,
        image_pull_policy=agent.image_pull_policy,
        env_from=[
            client.V1EnvFromSource(
                secret_ref=client.V1SecretEnvSource(name=agent.identity_secret_name),
            )
        ],
        env=env,
        volume_mounts=[
            client.V1VolumeMount(name='broker-sock', mount_path=BROKER_SOCK_DIR),
        ],
        resources=client.V1ResourceRequirements(
            requests={'cpu': '50m', 'memory': '64Mi'}, limits={'cpu': '500m', 'memory': '256Mi'},
        ),
        # BRW-12 / PLAT-422: same hardening as the agent container. The broker
        # runs the mcp-auth-proxy binary as uid 1000 ('agent' user in the image).
        security_context=client.V1SecurityContext(
            run_as_non_root=True,
            run_as_user=1000,
            allow_privilege_escalation=False,
            capabilities=client.V1Capabilities(drop=['ALL']),
        ),
    )

def _toleration_from_dict(raw: dict) -> client.V1Toleration:
    return client.V1Toleration(
        key=raw.get('key'),
        operator=raw.get('operator', 'Equal' if raw.get('value') is not None else 'Exists'),
        value=raw.get('value'),
        effect=raw.get('effect'),
        toleration_seconds=raw.get('tolerationSeconds', raw.get('toleration_seconds')),
    )


def _pod_tolerations(agent: FleetAgent):
    tolerations = []
    if agent.arch == 'arm64':
        tolerations.append(client.V1Toleration(
            key='shizuha.com/inference-paused', operator='Exists', effect='NoSchedule',
        ))
    for raw in getattr(settings, 'FLEET_TOLERATIONS', []) or []:
        tolerations.append(_toleration_from_dict(raw))
    for raw in agent.tolerations or []:
        tolerations.append(_toleration_from_dict(raw))
    return tolerations or None


def _build_pod_spec(agent: FleetAgent, *, forgejo_secret_name: str | None = None) -> client.V1PodSpec:
    sidecars = agent.sidecar_set or []
    has_broker = 'broker' in sidecars
    has_dind = 'dind' in sidecars
    containers = [_agent_container(agent, has_broker=has_broker, has_dind=has_dind,
                                   forgejo_secret_name=forgejo_secret_name)]
    if has_broker:
        containers.append(_broker_sidecar(agent))
    if has_dind:
        containers.append(_dind_sidecar())

    # TODO(PLAT-153, HLD §11 R1): native code-exec sandbox substitute. When the
    # gVisor/Kata runtimeClass lands, sandbox_required agents run under it instead
    # of being gated (see provision_agent). We set runtimeClassName here from
    # FLEET_SANDBOX_RUNTIME_CLASS so the moment R1 configures it, gated agents
    # schedule onto the sandboxed runtime — no further provisioner change needed.
    runtime_class = None
    if agent.sandbox_required:
        runtime_class = getattr(settings, 'FLEET_SANDBOX_RUNTIME_CLASS', '') or None

    # HIVE-69: ARM64 nodes (gx10-1 through gx10-16) carry a NoSchedule taint
    # shizuha.com/inference-paused to gate inference workloads. Fleet agents must
    # tolerate it to schedule; without this, ARM64 agent pods remain Pending
    # indefinitely with "16 had untolerated taint".
    return client.V1PodSpec(
        # Q1a — no kube API path from a fleet pod. PLAT-7463: the flag-gated
        # cluster-plane exception projects the kubelet-managed SA token (live
        # rotation; the kubeconfig references the tokenFile, never embedded
        # bytes). Authorization stays fail-closed — the scoped RBAC is a
        # separate declarative grant.
        automount_service_account_token=bool(agent.clusterplane_kubeconfig),
        node_selector=_node_selector(agent),
        affinity=_affinity(agent),
        tolerations=_pod_tolerations(agent),
        termination_grace_period_seconds=30,
        runtime_class_name=runtime_class,
        # fsGroup so the agent (uid 1000) can WRITE the Longhorn workspace PVC at
        # /home/agent/.shizuha — Longhorn mounts root-owned, and without this the
        # agent gets EACCES on mkdir (e.g. .shizuha/auth) and can't persist its
        # session/auth state. (Found by the PLAT-146 capability proof.)
        # BRW-12 / PLAT-422: runAsNonRoot + explicit uid/gid 1000 at the pod
        # level; container-level SecurityContext (above) inherits and cannot
        # override these toward root.
        # fsGroupChangePolicy=OnRootMismatch (PLAT-1173 / ShizuhaMcpAuthProxyUnready
        # 09-20): the default Always policy recursively chowns the whole workspace
        # PVC on EVERY mount. After a Longhorn attach migration (wake onto a new
        # node) that walk raced a settling remount and failed with lstat ENOENT
        # deep inside .venv — applyFSGroup failed, the pod's volume-ownership
        # phase aborted, and the broker-sock emptyDir stayed root:root 755, so
        # the mcp-auth-proxy broker could not chmod its sockdir (not_owner) nor
        # bind proxy.sock → CrashLoop → seat unready >5m (shion q4n48, nagi
        # d4jjc: broker restarts 1→5 in 3m). OnRootMismatch stats the volume
        # root and skips the recursion when ownership already matches — the
        # steady-state wake path (root chowned by the first-ever mount) does no
        # walk, and a mismatched fresh root chowns instantly. Same recommendation
        # the kubelet event itself emits (VolumePermissionChangeInProgress).
        security_context=client.V1PodSecurityContext(
            fs_group=1000,
            fs_group_change_policy='OnRootMismatch',
            **({} if (has_dind or agent.hostplane_ssh) else {
                'run_as_non_root': True,
                'run_as_user': 1000,
                'run_as_group': 1000,
            }),
        ),
        init_containers=(
            ([_docker_cli_init_container()] if has_dind else [])
            + ([_fleet_ssh_init_container(agent)] if agent.hostplane_ssh else [])
            + ([_fleet_kubeconfig_init_container(agent)] if agent.clusterplane_kubeconfig else [])
        ) or None,
        containers=containers,
        volumes=[
            client.V1Volume(
                name='workspace',
                persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                    claim_name=agent.workspace_pvc_name,
                ),
            ),
            # PLAT-1056: shared RWX PVC for cross-agent handoff (flag-gated).
            *([client.V1Volume(
                name='shared-workspace',
                persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                    claim_name=SHARED_WORKSPACE_PVC_NAME,
                ),
            )] if agent.shared_workspace else []),
            # Pod-local emptyDir for the broker UDS, shared agent<->broker
            # (only meaningful when the broker sidecar is attached).
            *(_broker_contract_volumes() if has_broker else []),
            *([
                client.V1Volume(name='docker-graph', empty_dir=client.V1EmptyDirVolumeSource()),
                client.V1Volume(name='docker-run', empty_dir=client.V1EmptyDirVolumeSource()),
                client.V1Volume(name='docker-bin', empty_dir=client.V1EmptyDirVolumeSource()),
            ] if has_dind else []),
            *([
                client.V1Volume(
                    name='fleet-ssh-secret',
                    secret=client.V1SecretVolumeSource(
                        secret_name=fleet_ssh_secret_name(agent),
                        default_mode=0o400,
                    ),
                ),
                client.V1Volume(name='fleet-ssh-home', empty_dir=client.V1EmptyDirVolumeSource()),
            ] if agent.hostplane_ssh else []),
        ],
    )


def _managed_hostplane_ssh_cidrs(name):
    raw = os.environ.get(name, '')
    if not raw:
        return []
    try:
        values = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(name + ' must be a JSON list of canonical CIDRs') from exc
    if not isinstance(values, list):
        raise ValueError(name + ' must be a JSON list of canonical CIDRs')
    result = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError(name + ' requires CIDR strings')
        try:
            network = ipaddress.ip_network(value, strict=True)
        except ValueError as exc:
            raise ValueError(name + ' contains an invalid canonical CIDR') from exc
        if network.prefixlen == 0 or network.with_prefixlen != value or value in result:
            raise ValueError(name + ' requires unique, explicitly scoped canonical CIDRs')
        result.append(value)
    return result


def _create_ssh_egress_network_policies(networking_v1, ns: str, agent: FleetAgent):
    """PLAT-174: emit the full isolation NetworkPolicy set into the isolated agent's
    namespace (fleet-<username>).  The static YAML in k8s/fleet/20-devops-isolation.yaml
    is documentation/template only — the live objects live here.

    Emits (in order):
    1. default-deny-all — default deny all ingress+egress (any later egress policy
       selecting this pod implicitly drops everything not listed here).
    2. allow-dns-egress — CoreDNS UDP/TCP 53 (required for all cluster connectivity).
    3. allow-platform-egress — Shizuha platform services in ns shizuha.
    4. allow-llm-https-egress — external LLM HTTPS (443), public internet only.
    5. allow-cortex-llm-egress — in-cluster Cortex/vLLM (shizuha-cortex ns, port 8040).
    6. allow-hostplane-ssh-egress — tailnet-scoped /32s (identity boundary preserved).
    7. allow-hostplane-ssh-bootstrap-lan — LAN bootstrap; RETIRED by PLAT-178.

    Codex P1-A: emitting only the SSH policies (6+7) without the baseline (1-5) leaves
    the pod egress-isolated (any Egress policy selects the pod → default deny everything
    else), so DNS/platform/LLM traffic is silently dropped.
    """
    # Validate both lists before the first Kubernetes policy operation.
    tailnet_cidrs = _managed_hostplane_ssh_cidrs('HIVE_HOSTPLANE_SSH_TAILNET_CIDRS')
    bootstrap_cidrs = _managed_hostplane_ssh_cidrs('HIVE_HOSTPLANE_SSH_BOOTSTRAP_CIDRS')
    base_meta = {'app.kubernetes.io/managed-by': MANAGED_BY, 'shizuha.io/agent': agent.agent_username}
    pod_selector = client.V1LabelSelector(match_labels={'shizuha.io/agent-role': 'devops-ssh'})
    all_pods = client.V1LabelSelector()  # {} — selects all pods in namespace

    # ── Policy 1: default-deny-all (selects all pods; must come first) ───────────
    default_deny = client.V1NetworkPolicy(
        metadata=client.V1ObjectMeta(name='default-deny-all', namespace=ns, labels=base_meta),
        spec=client.V1NetworkPolicySpec(
            pod_selector=all_pods,
            policy_types=['Ingress', 'Egress'],
        ),
    )

    # ── Policy 2: DNS egress (CoreDNS) ──────────────────────────────────────────
    dns_egress = client.V1NetworkPolicy(
        metadata=client.V1ObjectMeta(name='allow-dns-egress', namespace=ns, labels=base_meta),
        spec=client.V1NetworkPolicySpec(
            pod_selector=all_pods,
            policy_types=['Egress'],
            egress=[
                client.V1NetworkPolicyEgressRule(
                    to=[client.V1NetworkPolicyPeer(
                        namespace_selector=client.V1LabelSelector(
                            match_labels={'kubernetes.io/metadata.name': 'kube-system'},
                        ),
                        pod_selector=client.V1LabelSelector(
                            match_labels={'k8s-app': 'kube-dns'},
                        ),
                    )],
                    ports=[
                        client.V1NetworkPolicyPort(protocol='UDP', port=53),
                        client.V1NetworkPolicyPort(protocol='TCP', port=53),
                    ],
                ),
            ],
        ),
    )

    # ── Policy 3: platform/broker egress (shizuha + hive) ──────────────────────
    # Shared fleet (`fleet-agent-allow-egress`) already allows nginx :80, ID
    # :8001, and Hive coordinator :8030. Isolated personal seats used a stale
    # 4-port list, so broker login and Codex token mint were connection-refused
    # and the owner's Shizuha never took a turn.
    platform_egress = client.V1NetworkPolicy(
        metadata=client.V1ObjectMeta(name='allow-platform-egress', namespace=ns, labels=base_meta),
        spec=client.V1NetworkPolicySpec(
            pod_selector=all_pods,
            policy_types=['Egress'],
            egress=[
                client.V1NetworkPolicyEgressRule(
                    to=[client.V1NetworkPolicyPeer(
                        namespace_selector=client.V1LabelSelector(
                            match_labels={'kubernetes.io/metadata.name': 'shizuha'},
                        ),
                    )],
                    ports=[
                        client.V1NetworkPolicyPort(protocol='TCP', port=80),
                        client.V1NetworkPolicyPort(protocol='TCP', port=8000),
                        client.V1NetworkPolicyPort(protocol='TCP', port=8001),
                        client.V1NetworkPolicyPort(protocol='TCP', port=8002),
                        client.V1NetworkPolicyPort(protocol='TCP', port=8013),
                        client.V1NetworkPolicyPort(protocol='TCP', port=8019),
                    ],
                ),
                client.V1NetworkPolicyEgressRule(
                    to=[client.V1NetworkPolicyPeer(
                        namespace_selector=client.V1LabelSelector(
                            match_labels={'kubernetes.io/metadata.name': 'shizuha-hive'},
                        ),
                    )],
                    ports=[
                        client.V1NetworkPolicyPort(protocol='TCP', port=8030),
                        client.V1NetworkPolicyPort(protocol='TCP', port=8031),
                    ],
                ),
            ],
        ),
    )

    # ── Policy 4: external LLM HTTPS egress (public internet, not private CIDRs) ─
    llm_https_egress = client.V1NetworkPolicy(
        metadata=client.V1ObjectMeta(name='allow-llm-https-egress', namespace=ns, labels=base_meta),
        spec=client.V1NetworkPolicySpec(
            pod_selector=all_pods,
            policy_types=['Egress'],
            egress=[
                client.V1NetworkPolicyEgressRule(
                    to=[client.V1NetworkPolicyPeer(
                        ip_block=client.V1IPBlock(
                            cidr='0.0.0.0/0',
                            _except=[
                                '10.0.0.0/8',
                                '172.16.0.0/12',
                                '192.168.0.0/16',
                                '169.254.0.0/16',
                                '100.64.0.0/10',
                            ],
                        ),
                    )],
                    ports=[client.V1NetworkPolicyPort(protocol='TCP', port=443)],
                ),
            ],
        ),
    )

    # ── Policy 5: in-cluster Cortex/vLLM egress ──────────────────────────────────
    cortex_egress = client.V1NetworkPolicy(
        metadata=client.V1ObjectMeta(name='allow-cortex-llm-egress', namespace=ns, labels=base_meta),
        spec=client.V1NetworkPolicySpec(
            pod_selector=all_pods,
            policy_types=['Egress'],
            egress=[
                client.V1NetworkPolicyEgressRule(
                    to=[client.V1NetworkPolicyPeer(
                        namespace_selector=client.V1LabelSelector(
                            match_labels={'kubernetes.io/metadata.name': 'shizuha-cortex'},
                        ),
                    )],
                    ports=[client.V1NetworkPolicyPort(protocol='TCP', port=8040)],
                ),
            ],
        ),
    )

    # ── Policy 6: tailnet-scoped SSH egress ──────────────────────────────────────
    # Only tailnet /32s — both NetworkPolicy and Tailscale ACL must allow a
    # connection, preserving the per-identity boundary (PLAT-156 axis-ii).
    # Installation targets are explicit managed configuration, never inferred host addresses.
    # Review exact target lists when hosts join or leave the installation tailnet.
    tailnet_policy = client.V1NetworkPolicy(
        metadata=client.V1ObjectMeta(name='allow-hostplane-ssh-egress', namespace=ns, labels=base_meta),
        spec=client.V1NetworkPolicySpec(
            pod_selector=pod_selector,
            policy_types=['Egress'],
            egress=[
                client.V1NetworkPolicyEgressRule(
                    to=[client.V1NetworkPolicyPeer(ip_block=client.V1IPBlock(cidr=cidr)) for cidr in tailnet_cidrs],
                    ports=[client.V1NetworkPolicyPort(protocol='TCP', port=22)],
                ),
            ] if tailnet_cidrs else [],
        ),
    )

    # ── Policy 7: installation-owned temporary LAN bootstrap ─────────────────
    # Retire this separate policy when all approved hosts use tailnet SSH.
    # Kept separate so PLAT-178 can delete this policy alone without touching
    # the tailnet-scoped policy above.
    bootstrap_policy = client.V1NetworkPolicy(
        metadata=client.V1ObjectMeta(
            name='allow-hostplane-ssh-bootstrap-lan',
            namespace=ns,
            labels={**base_meta, 'shizuha.io/plat178-retire': 'true'},
            annotations={'shizuha.io/retire-with': 'PLAT-178'},
        ),
        spec=client.V1NetworkPolicySpec(
            pod_selector=pod_selector,
            policy_types=['Egress'],
            egress=[
                client.V1NetworkPolicyEgressRule(
                    to=[client.V1NetworkPolicyPeer(ip_block=client.V1IPBlock(cidr=cidr)) for cidr in bootstrap_cidrs],
                    ports=[client.V1NetworkPolicyPort(protocol='TCP', port=22)],
                ),
            ] if bootstrap_cidrs else [],
        ),
    )

    # ── Policy 8: ingress allow for expose_http agents ──────────────────────────
    # default-deny-all selects ALL pods for Ingress; without a companion allow
    # the Service's traffic is permanently dropped for isolated agents with HTTP
    # exposure. Allow ingress only from the ingress controller namespace so the
    # pod remains reachable through the nginx proxy (PLAT-174 P2).
    expose_ingress_name = 'allow-http-ingress'
    if agent.expose_http:
        expose_ingress = client.V1NetworkPolicy(
            metadata=client.V1ObjectMeta(
                name=expose_ingress_name, namespace=ns, labels=base_meta,
            ),
            spec=client.V1NetworkPolicySpec(
                pod_selector=client.V1LabelSelector(
                    match_labels={'shizuha.io/agent': agent.agent_username},
                ),
                policy_types=['Ingress'],
                ingress=[
                    client.V1NetworkPolicyIngressRule(
                        _from=[client.V1NetworkPolicyPeer(
                            namespace_selector=client.V1LabelSelector(
                                match_labels={
                                    'kubernetes.io/metadata.name': 'ingress-nginx',
                                },
                            ),
                        )],
                        ports=[client.V1NetworkPolicyPort(protocol='TCP', port=agent.http_port)],
                    ),
                ],
            ),
        )
    else:
        expose_ingress = None

    base_policies = (default_deny, dns_egress, platform_egress, llm_https_egress,
                     cortex_egress, tailnet_policy, bootstrap_policy)
    for policy in base_policies:
        try:
            networking_v1.create_namespaced_network_policy(ns, policy)
        except ApiException as e:
            if e.status == 409:
                networking_v1.replace_namespaced_network_policy(policy.metadata.name, ns, policy)
            else:
                raise

    if expose_ingress is not None:
        try:
            networking_v1.create_namespaced_network_policy(ns, expose_ingress)
        except ApiException as e:
            if e.status == 409:
                networking_v1.replace_namespaced_network_policy(expose_ingress_name, ns, expose_ingress)
            else:
                raise
    else:
        # expose_http turned off on reprovision — remove any stale ingress allow.
        try:
            networking_v1.delete_namespaced_network_policy(expose_ingress_name, ns)
        except ApiException as e:
            if e.status != 404:
                raise


def _write_deployment(
    apps_v1, ns: str, deployment, cold_state: _ColdDeploymentState | None = None,
    *, expected_resource_version: str | None = None,
):
    if expected_resource_version is not None:
        deployment.metadata.resource_version = expected_resource_version
        apps_v1.replace_namespaced_deployment(deployment.metadata.name, ns, deployment)
        return
    if cold_state is not None:
        if not cold_state.can_stage or deployment.spec.replicas != 0:
            raise ValueError('Cold provisioning requires a confirmed zero-replica target')
        if cold_state.resource_version is None:
            # A concurrent create is a conflict, never permission to replace.
            apps_v1.create_namespaced_deployment(ns, deployment)
        else:
            # A concurrent scale changes resourceVersion and makes this replace
            # fail; it cannot silently hibernate the newly started runtime.
            deployment.metadata.resource_version = cold_state.resource_version
            apps_v1.replace_namespaced_deployment(deployment.metadata.name, ns, deployment)
        return
    try:
        apps_v1.create_namespaced_deployment(ns, deployment)
    except ApiException as exc:
        if exc.status != 409:
            raise
        apps_v1.replace_namespaced_deployment(deployment.metadata.name, ns, deployment)


def _create_deployment(
    apps_v1, ns: str, agent: FleetAgent, *, cold_state: _ColdDeploymentState | None = None,
    replicas: int = 1, expected_resource_version: str | None = None,
):
    from .native_runtime_release import enabled
    if enabled() and _uses_native_runtime_image(agent) and getattr(agent, '_native_runtime_release', None) is None:
        from .runtime_release import RuntimeReleaseProjectionUnavailable
        raise RuntimeReleaseProjectionUnavailable('Native materializer lacks bound source authority')
    core_v1, _apps, _networking = get_k8s_client()
    forgejo_secret_name, _secret = _forgejo_secret(agent, core_v1, ns)
    labels = _base_labels(agent)
    selector_labels = {'shizuha.io/agent': agent.agent_username}
    deployment = client.V1Deployment(
        metadata=client.V1ObjectMeta(
            name=agent.deployment_name,
            namespace=ns,
            labels=labels,
            annotations={NATIVE_RUNTIME_OWNER_ANNOTATION: NATIVE_RUNTIME_OWNER},
        ),
        spec=client.V1DeploymentSpec(
            replicas=0 if cold_state is not None else replicas,
            # Single-active-instance invariant (HLD §8): never two pods at once.
            strategy=client.V1DeploymentStrategy(type='Recreate'),
            selector=client.V1LabelSelector(match_labels=selector_labels),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels=labels),
                spec=_build_pod_spec(agent, forgejo_secret_name=forgejo_secret_name),
            ),
        ),
    )
    native_authority = getattr(agent, '_native_runtime_release', None)
    if native_authority is not None:
        from .native_runtime_release import assert_still_current, stamp_release, intent_guard
        from .runtime_release import RuntimeReleaseProjectionUnavailable
        # Retain the caller's original operation/generation. Rebinding here
        # would adopt a concurrent stop or executor handoff as fresh authority.
        core, _, _ = get_k8s_client()
        try:
            existing = apps_v1.read_namespaced_deployment(agent.deployment_name, ns)
        except ApiException as exc:
            if exc.status != 404:
                raise
            with intent_guard(agent) as desired:
                assert_still_current(native_authority)
                if core.list_namespaced_pod(ns, label_selector=f'shizuha.io/agent={agent.agent_username}').items:
                    raise RuntimeReleaseProjectionUnavailable('Native create found an existing Pod identity')
                if desired and desired.desired_lifecycle in {'deleted', 'archived'}:
                    raise RuntimeReleaseProjectionUnavailable('Native creation is no longer desired')
                if deployment.spec.replicas > 0 and (
                        (desired and desired.desired_lifecycle != 'active')
                        or not _lease_start_decision(agent).allowed):
                    raise RuntimeReleaseProjectionUnavailable('Native creation awaits active lifecycle and lease authority')
                assert_still_current(native_authority)
                if core.list_namespaced_pod(ns, label_selector=f'shizuha.io/agent={agent.agent_username}').items:
                    raise RuntimeReleaseProjectionUnavailable('Native create found a newly arrived Pod')
                stamp_release(native_authority, deployment)
                # A concurrent create stays a conflict; never replace an object
                # whose ownership and turn boundary have not been observed.
                apps_v1.create_namespaced_deployment(ns, deployment)
        else:
            if expected_resource_version is not None and existing.metadata.resource_version != expected_resource_version:
                raise ApiException(status=409, reason='Native existing Deployment changed')
            if cold_state is not None and (not cold_state.can_stage or existing.spec.replicas != 0):
                raise RuntimeReleaseProjectionUnavailable('Native cold target is no longer cold')
            if not _replace_native_runtime(agent, core, apps_v1, deployment, existing, native_authority):
                raise RuntimeReleaseProjectionUnavailable('Native runtime replacement awaits a safe turn boundary')
        return
    _write_deployment(apps_v1, ns, deployment, cold_state, expected_resource_version=expected_resource_version)



def _create_service(core_v1, ns: str, agent: FleetAgent):
    service = client.V1Service(
        metadata=client.V1ObjectMeta(name=agent.deployment_name, namespace=ns, labels=_base_labels(agent)),
        spec=client.V1ServiceSpec(
            selector={'shizuha.io/agent': agent.agent_username},
            ports=[client.V1ServicePort(name='http', port=agent.http_port, target_port=agent.http_port)],
        ),
    )
    try:
        core_v1.create_namespaced_service(ns, service)
    except ApiException as e:
        if e.status == 409:
            current = core_v1.read_namespaced_service(name=agent.deployment_name, namespace=ns)
            current.spec.selector = {'shizuha.io/agent': agent.agent_username}
            current.spec.ports = [client.V1ServicePort(name='http', port=agent.http_port, target_port=agent.http_port)]
            core_v1.replace_namespaced_service(agent.deployment_name, ns, current)
        else:
            raise


def _delete_namespace(core_v1, ns: str):
    # P1 defense-in-depth (aoi round-2): NEVER delete the shared fleet namespace —
    # doing so would tear down every fleet agent. Only per-agent isolated ns may
    # be deleted; callers must pass an isolated ns. Hard refusal here regardless.
    if ns == fleet_namespace():
        raise ValueError(
            f"refusing to delete the shared fleet namespace '{ns}' "
            f"(blast-radius guard — would take down ALL fleet agents)"
        )
    try:
        core_v1.delete_namespace(name=ns)
    except ApiException as e:
        if e.status != 404:
            raise


def _delete_if_exists(delete_fn, name: str, ns: str):
    try:
        delete_fn(name=name, namespace=ns)
    except ApiException as e:
        if e.status != 404:
            raise


def _log_event(agent: FleetAgent, event_type: str, message: str):
    FleetAgentEvent.objects.create(agent=agent, event_type=event_type, message=message)
    logger.info(f'[hive-fleet] agent {agent.agent_username}: {event_type} — {message}')


# ── Browser-worker provisioner (PLAT-536 / BRW-12) ───────────────────────────
#
# Per-agent browser-worker Deployments in the ``shizuha-browser`` namespace.
# Each fleet agent that needs browser automation gets its own isolated worker
# pod running the shizuha-browser image (Playwright + Chromium, pwuser uid 1000,
# port 18116, /health liveness+readiness endpoint, /data PVC for browser profile).
#
# This is a SEPARATE pod type from the fleet agent — it does NOT use the
# broker sidecar, does NOT hold AGENT_PASSWORD, and does NOT use FleetAgent's
# workspace PVC. It is purely a browser automation worker that the agent pod
# reaches over the cluster network (BRW-13 NetworkPolicy is a sibling task).
#
# Security posture (BRW-12 C7): fully Restricted PSS —
#   Pod:       runAsNonRoot, runAsUser/Group=1000, fsGroup=1000, seccompProfile=RuntimeDefault
#   Container: runAsNonRoot, runAsUser=1000, allowPrivilegeEscalation=false, drop=ALL
#   No privileged, no hostPath, no hostPID, no hostNetwork, no hostIPC.
#   automountServiceAccountToken: false (no kube-API path from the browser pod).

BROWSER_WORKER_PORT = 18116
BROWSER_WORKER_PROFILE_PVC_SIZE = '5Gi'


def browser_worker_namespace() -> str:
    return getattr(settings, 'SHIZUHA_BROWSER_NAMESPACE', 'shizuha-browser')


def agent_needs_browser_mcp(agent: FleetAgent) -> bool:
    """True when this agent should own a private browser MCP worker.

    Isolation contract (operator 2026-07-29): browser automation is NEVER a
    shared multi-tenant service. Each agent that needs it gets its own worker
    Deployment+PVC+Service; the agent pod reaches only its own Service URL via
    ``SHIZUHA_BROWSER_MCP_URL`` (bridges wire Codex/Claude to that HTTP MCP).

    Triggers (any):
      - skill_config lists human-browser-stealth (team capability SoT)
      - sidecar_set contains ``browser``
      - env SHIZUHA_BROWSER_MCP is truthy
      - runtime role/toolset social-media (defensive default for SM seats)
    """
    sc = agent.skill_config if isinstance(agent.skill_config, dict) else {}
    skills = set(sc.get('skills') or []) | set(sc.get('eager_skills') or [])
    if 'human-browser-stealth' in skills or 'x-twitter' in skills:
        return True
    sidecars = {str(s).strip().lower() for s in (agent.sidecar_set or [])}
    if 'browser' in sidecars:
        return True
    env = agent.env if isinstance(agent.env, dict) else {}
    flag = str(env.get('SHIZUHA_BROWSER_MCP') or '').strip().lower()
    if flag in {'1', 'true', 'on', 'yes'}:
        return True
    toolset = str(env.get('SHIZUHA_TOOLSET') or '').strip().lower()
    if toolset in {'social-media', 'browser'}:
        return True
    role = str(agent.role or '').strip().lower()
    if 'social media' in role or role == 'social-media':
        return True
    return False


def browser_worker_service_url(agent: FleetAgent) -> str:
    """Cluster-DNS URL for this agent's private browser MCP (/mcp)."""
    ns = browser_worker_namespace()
    name = _browser_worker_name(agent)
    return f'http://{name}.{ns}.svc.cluster.local:{BROWSER_WORKER_PORT}/mcp'


def browser_mcp_env_for_agent(agent: FleetAgent) -> dict[str, str]:
    """Env the agent bridge needs to attach the private browser MCP (not shared)."""
    return {
        'SHIZUHA_BROWSER_MCP': '1',
        'SHIZUHA_BROWSER_MCP_URL': browser_worker_service_url(agent),
        # Observability-only provenance: both consumers read the same dedicated
        # Hive-projected Secret name/key in their own namespace.
        'SHIZUHA_BROWSER_MCP_JWT_SECRET_FROM': (
            f'{BROWSER_MCP_JWT_SECRET_NAME}/{BROWSER_MCP_JWT_SECRET_KEY}'
        ),
    }


def _start_browser_worker(agent: FleetAgent) -> bool:
    """Activate a cold browser only after the agent's model lease is confirmed."""
    try:
        _, apps_v1, _ = get_k8s_client()
        ns = browser_worker_namespace()
        name = _browser_worker_name(agent)
        try:
            current = apps_v1.read_namespaced_deployment(name, ns)
        except ApiException as exc:
            if exc.status == 404:
                return provision_browser_worker(agent)
            raise
        replicas = getattr(current.spec, 'replicas', None)
        if not isinstance(replicas, int) or replicas < 0:
            raise ValueError('Browser Deployment replica count is unreadable')
        if replicas == 0:
            apps_v1.patch_namespaced_deployment_scale(name, ns, {'spec': {'replicas': 1}})
        return True
    except Exception:
        logger.exception('Browser runtime start failed for %s', agent.agent_username)
        agent.status_message = 'Browser runtime start failed; agent start aborted before scale'
        agent.save(update_fields=['status_message'])
        _log_event(agent, 'error', agent.status_message)
        return False


def provision_browser_worker(
    agent: FleetAgent, image: str = '', *, jwt_secret_ready: bool = False,
    cold_state: _ColdDeploymentState | None = None,
) -> bool:
    """Provision a per-agent browser-worker Deployment in the shizuha-browser namespace.

    Creates (idempotent — safe to call on reprovision):
      - shizuha-browser namespace (if absent; normally pre-created by k8s/fleet/00-namespaces.yaml)
      - Per-agent /data PVC for the Chromium browser profile (never shared)
      - Single-replica Recreate Deployment with BRW-12 C7 security context
      - Per-agent Service so only this agent targets this worker by DNS

    ``image`` is the browser-worker image to run. Falls back to
    ``settings.FLEET_BROWSER_IMAGE`` then the pinned internal-registry tag.
    """
    resolved_image = (
        image
        or getattr(settings, 'FLEET_BROWSER_IMAGE', None)
        or 'localhost:30500/shizuha-browser:s260606x-b9669f4'
    )
    core_v1, apps_v1, _ = get_k8s_client()
    ns = browser_worker_namespace()
    try:
        _ensure_browser_namespace(core_v1, ns)
        if not jwt_secret_ready and not _ensure_browser_mcp_jwt_secrets(core_v1, agent):
            return False
        _create_browser_worker_pvc(core_v1, ns, agent)
        cold_options = {'cold_state': cold_state} if cold_state is not None else {}
        _create_browser_worker_deployment(apps_v1, ns, agent, resolved_image, **cold_options)
        _create_browser_worker_service(core_v1, ns, agent)
        logger.info(f'[hive-fleet] browser-worker provisioned for {agent.agent_username} in {ns}')
        return True
    except Exception as exc:
        logger.error(f'[hive-fleet] browser-worker provisioning failed for {agent.agent_username}: {exc}')
        return False


def deprovision_browser_worker(agent: FleetAgent) -> bool:
    """Remove the per-agent browser-worker Deployment, Service, and PVC from shizuha-browser.

    Best-effort: logs errors but does not raise so that fleet-agent deprovision
    is never blocked by a browser-worker cleanup failure.
    """
    core_v1, apps_v1, _ = get_k8s_client()
    ns = browser_worker_namespace()
    try:
        _delete_if_exists(apps_v1.delete_namespaced_deployment, _browser_worker_name(agent), ns)
        _delete_if_exists(core_v1.delete_namespaced_service, _browser_worker_name(agent), ns)
        _delete_if_exists(core_v1.delete_namespaced_persistent_volume_claim, _browser_worker_pvc_name(agent), ns)
        logger.info(f'[hive-fleet] browser-worker deprovisioned for {agent.agent_username}')
        return True
    except Exception as exc:
        logger.error(f'[hive-fleet] browser-worker deprovision failed for {agent.agent_username}: {exc}')
        return False


def _browser_worker_name(agent: FleetAgent) -> str:
    return f'browser-{agent.agent_username}'


def _browser_worker_pvc_name(agent: FleetAgent) -> str:
    return f'browser-profile-{agent.agent_username}'


def _browser_worker_labels(agent: FleetAgent) -> dict:
    return {
        'app.kubernetes.io/managed-by': MANAGED_BY,
        'app.kubernetes.io/name': 'shizuha-browser-worker',
        'shizuha.io/agent': agent.agent_username,
        'shizuha.io/component': 'browser-worker',
    }


def _ensure_browser_namespace(core_v1, ns: str):
    """Ensure the shizuha-browser namespace exists.

    In practice the namespace is pre-created by k8s/fleet/00-namespaces.yaml
    (applied at cluster bootstrap). This create-if-absent guard covers the case
    where the manifest hasn't been applied yet (e.g. dev/test environments).
    The static manifest is the canonical source of the namespace labels (incl.
    PSS enforce=restricted) — the imperatively-created fallback uses the same labels.
    """
    try:
        core_v1.read_namespace(name=ns)
    except ApiException as exc:
        if exc.status != 404:
            raise
        core_v1.create_namespace(
            body=client.V1Namespace(
                metadata=client.V1ObjectMeta(
                    name=ns,
                    labels={
                        'app.kubernetes.io/managed-by': MANAGED_BY,
                        'shizuha.io/tier': 'browser',
                        'pod-security.kubernetes.io/enforce': 'restricted',
                        'pod-security.kubernetes.io/warn': 'restricted',
                        'pod-security.kubernetes.io/audit': 'restricted',
                    },
                )
            )
        )
        logger.info(f'[hive-fleet] created browser-worker namespace {ns}')


def _browser_mcp_jwt_target_namespaces(agent: FleetAgent) -> tuple[str, str]:
    """Namespaces that host the signer and verifier copies, respectively."""
    return _live_ns(agent), browser_worker_namespace()


def _ensure_browser_mcp_jwt_secrets(core_v1, agent: FleetAgent) -> bool:
    """Converge one browser-MCP JWT byte contract into both consumer namespaces.

    The platform JWT key in ``shizuha/shizuha-secrets`` is the existing
    shared credential source. Secret refs are namespace-scoped, so Hive materializes a dedicated
    ``shizuha-browser-jwt`` copy in the fleet agent's namespace (signer) and the
    browser-worker namespace (verifier). Existing targets are PATCHED to the
    source bytes instead of short-circuiting: equality is the invariant, and
    preserving a stale target is exactly the HIVE-1922 reconnect wedge.

    No secret value is decoded or logged. A missing/empty source fails closed
    before either Deployment is rendered with an unresolvable or divergent key.
    """
    source_ns = str(getattr(settings, 'SHIZUHA_PLATFORM_NAMESPACE', 'shizuha') or 'shizuha')
    try:
        source = core_v1.read_namespaced_secret(
            BROWSER_MCP_JWT_SOURCE_SECRET_NAME, source_ns,
        )
    except ApiException as exc:
        if exc.status == 404:
            logger.error(
                'HIVE-1922: browser MCP JWT source %s/%s is missing',
                source_ns, BROWSER_MCP_JWT_SOURCE_SECRET_NAME,
            )
            return False
        raise
    encoded = str((source.data or {}).get(BROWSER_MCP_JWT_SECRET_KEY) or '').strip()
    if not encoded:
        logger.error(
            'HIVE-1922: browser MCP JWT source %s/%s has no %s key',
            source_ns, BROWSER_MCP_JWT_SOURCE_SECRET_NAME,
            BROWSER_MCP_JWT_SECRET_KEY,
        )
        return False

    labels = {
        'app.kubernetes.io/managed-by': MANAGED_BY,
        'shizuha.io/component': 'browser-mcp-auth',
    }
    for ns in dict.fromkeys(_browser_mcp_jwt_target_namespaces(agent)):
        try:
            current = core_v1.read_namespaced_secret(BROWSER_MCP_JWT_SECRET_NAME, ns)
        except ApiException as exc:
            if exc.status != 404:
                raise
            current = None
        if current is None:
            body = client.V1Secret(
                metadata=client.V1ObjectMeta(
                    name=BROWSER_MCP_JWT_SECRET_NAME,
                    namespace=ns,
                    labels=labels,
                ),
                data={BROWSER_MCP_JWT_SECRET_KEY: encoded},
                type='Opaque',
            )
            try:
                core_v1.create_namespaced_secret(ns, body)
            except ApiException as exc:
                if exc.status != 409:
                    raise
                # A concurrent reconcile created it. Patch the authoritative key
                # below so the race cannot preserve different bytes.
                core_v1.patch_namespaced_secret(
                    BROWSER_MCP_JWT_SECRET_NAME, ns,
                    {'metadata': {'labels': labels},
                     'data': {BROWSER_MCP_JWT_SECRET_KEY: encoded}},
                )
            continue
        current_data = dict(current.data or {})
        current_labels = dict(getattr(current.metadata, 'labels', None) or {})
        if (
            current_data.get(BROWSER_MCP_JWT_SECRET_KEY) != encoded
            or any(current_labels.get(key) != value for key, value in labels.items())
        ):
            core_v1.patch_namespaced_secret(
                BROWSER_MCP_JWT_SECRET_NAME, ns,
                {'metadata': {'labels': labels},
                 'data': {BROWSER_MCP_JWT_SECRET_KEY: encoded}},
            )
    return True


def _create_browser_worker_pvc(core_v1, ns: str, agent: FleetAgent):
    # Prefer local-path on the amd64 workstation (s1) — Longhorn CSI is not
    # available there and RWO multi-attach across arch nodes fails hard.
    storage_class = (
        getattr(settings, 'FLEET_BROWSER_STORAGE_CLASS', None)
        or 'local-path'
    )
    pvc = client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(
            name=_browser_worker_pvc_name(agent), namespace=ns,
            labels=_browser_worker_labels(agent),
        ),
        spec=client.V1PersistentVolumeClaimSpec(
            access_modes=['ReadWriteOnce'],
            storage_class_name=storage_class,
            resources=client.V1VolumeResourceRequirements(
                requests={'storage': BROWSER_WORKER_PROFILE_PVC_SIZE},
            ),
        ),
    )
    try:
        core_v1.create_namespaced_persistent_volume_claim(ns, pvc)
    except ApiException as exc:
        if exc.status != 409:
            raise


def _browser_worker_pod_spec(agent: FleetAgent, image: str) -> client.V1PodSpec:
    """Build the BRW-12 C7 hardened pod spec for a browser-worker pod.

    Security invariants (Restricted PSS):
      Pod:       runAsNonRoot, uid/gid=1000, fsGroup=1000, seccompProfile=RuntimeDefault
      Container: runAsNonRoot, uid=1000, allowPrivilegeEscalation=false, drop=ALL
      No privileged, hostPath, hostPID, hostNetwork, or hostIPC.
    """
    health_probe = client.V1Probe(
        http_get=client.V1HTTPGetAction(path='/health', port=BROWSER_WORKER_PORT),
        initial_delay_seconds=10,
        period_seconds=10,
        timeout_seconds=5,
        failure_threshold=3,
    )
    container = client.V1Container(
        name='browser-worker',
        image=image,
        image_pull_policy='IfNotPresent',
        ports=[
            client.V1ContainerPort(
                name='mcp', container_port=BROWSER_WORKER_PORT, protocol='TCP',
            ),
        ],
        env=[
            client.V1EnvVar(name='AGENT_USERNAME', value=agent.agent_username),
            client.V1EnvVar(name='PORT', value=str(BROWSER_WORKER_PORT)),
            client.V1EnvVar(name='PROFILE_DIR', value='/data/browser-profile'),
            client.V1EnvVar(name='MAX_SESSIONS', value='2'),
            # Default human mode for social/portal work; agent can still request mode.
            client.V1EnvVar(name='DEFAULT_BROWSER_MODE', value='human'),
            client.V1EnvVar(
                name='JWT_SECRET_KEY',
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(
                        name=BROWSER_MCP_JWT_SECRET_NAME,
                        key=BROWSER_MCP_JWT_SECRET_KEY,
                        optional=False,
                    ),
                ),
            ),
        ],
        volume_mounts=[
            client.V1VolumeMount(name='browser-profile', mount_path='/data'),
        ],
        liveness_probe=health_probe,
        readiness_probe=health_probe,
        resources=client.V1ResourceRequirements(
            requests={'cpu': '250m', 'memory': '512Mi'},
            limits={'cpu': '2', 'memory': '2Gi'},
        ),
        security_context=client.V1SecurityContext(
            run_as_non_root=True,
            run_as_user=1000,
            allow_privilege_escalation=False,
            privileged=False,
            capabilities=client.V1Capabilities(drop=['ALL']),
        ),
    )
    # Browser image is currently amd64-only (exec format error on arm64). Always
    # schedule private browser workers on amd64 + tolerate the s1 workstation
    # taint (PLAT-536). Agent arch may be arm64 — browser worker stays private
    # on amd64 and is reached over cluster DNS.
    #
    # s1 also carries the operator=unschedulable maintenance taint (gpu-operator
    # values-s1-maintenance-taints.yaml, PLAT-5936): it keeps nvidia-device-plugin
    # landing on s1 after a kubelet-data move and prevents the ComfyUI admission
    # storm. Browser-worker PVCs are local-path (node-pinned to s1), so without
    # tolerating operator=unschedulable the pod is Unschedulable and the agent's
    # private browser MCP never comes up (ShizuhaAgentMcpReconnectWedge). Match
    # the shared shizuha-browser Deployment, which tolerates both taints.
    tolerations = [
        client.V1Toleration(
            key='node.shizuha/workstation',
            operator='Equal',
            value='true',
            effect='NoSchedule',
        ),
        client.V1Toleration(
            key='operator',
            operator='Equal',
            value='unschedulable',
            effect='NoSchedule',
        ),
    ]

    return client.V1PodSpec(
        automount_service_account_token=False,
        node_selector={'kubernetes.io/arch': 'amd64'},
        tolerations=tolerations,
        termination_grace_period_seconds=30,
        security_context=client.V1PodSecurityContext(
            run_as_non_root=True,
            run_as_user=1000,
            run_as_group=1000,
            fs_group=1000,
            seccomp_profile=client.V1SeccompProfile(type='RuntimeDefault'),
        ),
        containers=[container],
        volumes=[
            client.V1Volume(
                name='browser-profile',
                persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                    claim_name=_browser_worker_pvc_name(agent),
                ),
            ),
        ],
    )


def _create_browser_worker_deployment(
    apps_v1, ns: str, agent: FleetAgent, image: str,
    *, cold_state: _ColdDeploymentState | None = None,
):
    labels = _browser_worker_labels(agent)
    selector_labels = {
        'shizuha.io/agent': agent.agent_username,
        'shizuha.io/component': 'browser-worker',
    }
    deployment = client.V1Deployment(
        metadata=client.V1ObjectMeta(
            name=_browser_worker_name(agent), namespace=ns, labels=labels,
        ),
        spec=client.V1DeploymentSpec(
            replicas=0 if cold_state is not None else 1,
            strategy=client.V1DeploymentStrategy(type='Recreate'),
            selector=client.V1LabelSelector(match_labels=selector_labels),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels=labels),
                spec=_browser_worker_pod_spec(agent, image),
            ),
        ),
    )
    _write_deployment(apps_v1, ns, deployment, cold_state)


def _create_browser_worker_service(core_v1, ns: str, agent: FleetAgent):
    """Per-agent ClusterIP Service — agents must only talk to their own worker."""
    name = _browser_worker_name(agent)
    labels = _browser_worker_labels(agent)
    svc = client.V1Service(
        metadata=client.V1ObjectMeta(name=name, namespace=ns, labels=labels),
        spec=client.V1ServiceSpec(
            type='ClusterIP',
            selector={
                'shizuha.io/agent': agent.agent_username,
                'shizuha.io/component': 'browser-worker',
            },
            ports=[
                client.V1ServicePort(
                    name='mcp',
                    port=BROWSER_WORKER_PORT,
                    target_port=BROWSER_WORKER_PORT,
                    protocol='TCP',
                ),
            ],
        ),
    )
    try:
        core_v1.create_namespaced_service(ns, svc)
    except ApiException as exc:
        if exc.status != 409:
            raise
