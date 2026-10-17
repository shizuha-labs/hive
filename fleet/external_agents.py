"""HIVE-177 / ADR-0002: surface external / BYO agents in the Hive fleet.

An "external" agent is a shizuha-id account with account_type=agent that is
heartbeat-alive (PLAT-694 `pulse:agent_hb:<email>` in the Pulse broker redis) but
is NOT represented by a FleetAgent row or the rt-fleet host-docker runtime — i.e.
a bring-your-own-runtime agent (e.g. a k3s fleet on the v4 MacBook) connecting to
Hive over public shizuha.com. Each carries `owner` (= shizuha-id `created_by`) so
the UI can attribute it to the human who owns it.

Read-only: Hive does not host or control these runtimes; we only surface identity,
ownership, and heartbeat-derived liveness.
"""
import functools
import logging
import os
import threading
import time
from datetime import datetime, timezone

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


def _ttl_cached(ttl_env: str, default_ttl: float):
    """PLAT-4210: bound the external-agents network fan-out to once per TTL.

    These zero-arg readers (heartbeat redis, id bulk list, connect telemetry —
    up to ~10s of blocking HTTP combined) used to run on EVERY
    /fleet/agents/ request. Under channels' ASGI adapter all sync views share
    one thread, so a single slow shizuha-id call starved every other request
    INCLUDING /api/health/ — the coordinator "hang" that dropped the hive
    Service to zero endpoints and token-starved the fleet (2026-07-12, twice).
    Caching the result for a short TTL (single-flight under a lock) bounds the
    blocking work to at most one fan-out per TTL instead of per request; a
    30s-stale external-agents list is invisible in the UI, an unresponsive
    coordinator is not.
    """
    def deco(fn):
        state: dict = {'ts': 0.0, 'val': None}
        lock = threading.Lock()

        @functools.wraps(fn)
        def wrapper():
            ttl = float(_cfg(ttl_env, str(default_ttl)))
            now = time.time()
            with lock:
                if state['val'] is not None and now - state['ts'] < ttl:
                    return state['val']
                # Single-flight: refresh inside the lock. A concurrent caller
                # waits here (≤ the fan-out timeout) instead of issuing its own
                # duplicate blocking fan-out — strictly better than the
                # per-request fan-outs this replaces.
                val = fn()
                state['val'] = val
                state['ts'] = time.time()
                return val

        wrapper.cache_clear = lambda: state.update(ts=0.0, val=None)  # for tests
        return wrapper
    return deco


def _cfg(name: str, default: str) -> str:
    """Resolve a config value: explicit Django setting → process env → default.

    Hive's k8s deployment supplies infra endpoints via env (e.g.
    ``AGENT_HEARTBEAT_REDIS_URL``), but ``hive_project.settings`` does not
    re-export every such var as a Django setting. Reading from settings ALONE
    therefore silently fell back to the hard-coded default — and when the
    infra-redis was consolidated into the single HA redis, that default
    (``shizuha-infra-redis-master``) became an orphan Service with no backing
    pod → ``_alive_heartbeats()`` got connection-refused → EVERY agent rendered
    "Unavailable" in Hive even while telemetry was fresh. Honour the env var too
    so the deployment's config actually takes effect.
    """
    return (getattr(settings, name, '') or os.environ.get(name, '') or default)


def _id_internal_base() -> str:
    # Internal endpoints are network-secured and NOT routed through the public
    # shizuha-nginx gateway — call the shizuha-id service directly in-cluster.
    return _cfg(
        'SHIZUHA_ID_INTERNAL_URL',
        'http://shizuha-id.shizuha.svc.cluster.local:8001',
    ).rstrip('/')


def _hb_redis_url() -> str:
    # Default points at the live HA redis (the infra-redis was decommissioned in
    # the single-HA-redis consolidation); env/settings override as usual.
    return _cfg(
        'AGENT_HEARTBEAT_REDIS_URL',
        'redis://shizuha-redis-ha-master.shizuha.svc.cluster.local:6379/1',
    )


def _hb_stale_sec() -> int:
    return int(_cfg('AGENT_HEARTBEAT_STALE_SEC', '5400'))


@_ttl_cached('HIVE_EXTERNAL_HB_TTL', 20.0)
def _alive_heartbeats() -> dict:
    """email(lower) -> last-seen epoch, from the Pulse heartbeat redis."""
    try:
        import redis as _redis
        r = _redis.Redis.from_url(
            _hb_redis_url(), decode_responses=True,
            socket_connect_timeout=1, socket_timeout=1,
        )
        out = {}
        for key in r.scan_iter('pulse:agent_hb:*', count=500):
            email = key.split('pulse:agent_hb:', 1)[-1].strip().lower()
            try:
                ts = int(float(r.get(key) or 0))
            except Exception:
                ts = 0
            out[email] = ts
        return out
    except Exception as exc:
        logger.warning('external-agents: heartbeat redis read failed: %s', exc)
        return {}


def _id_get(path: str, params=None):
    base = _id_internal_base()
    if not base:
        return None
    try:
        r = requests.get(f'{base}{path}', params=params or {}, timeout=6)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        logger.warning('external-agents: id GET %s failed: %s', path, exc)
        return None


def _connect_internal_base() -> str:
    return _cfg(
        'SHIZUHA_CONNECT_INTERNAL_URL',
        'http://shizuha-connect.shizuha.svc.cluster.local:8019',
    ).rstrip('/')


def _resolve_agent_user_id(username):
    """HIVE-212: resolve a username -> numeric platform user_id via the id internal
    endpoint. BYO/daemon-registered agents cannot be resolved by username on
    connect's side (the config POST returns 400); the numeric agent_user_id is the
    portable key that works for both BYO and container-fleet agents. Returns the int
    id or None if it can't be resolved (caller falls back to username)."""
    uname = (username or '').strip().lower()
    if not uname:
        return None
    d = _id_get('/api/internal/agents/', {'username': uname}) or {}
    uid = d.get('user_id')
    try:
        return int(uid) if uid is not None else None
    except (TypeError, ValueError):
        return None


def configure_agent_via_connect(username, updates) -> tuple:
    """HIVE-195 Phase 3: push config to a BYO/containerized agent (scout/miya/taki)
    that has NO daemon control-proxy, via Connect's WS relay. POSTs to connect's
    internal config endpoint; the agent's ConnectClient applies it live + persists.
    Returns (ok, error)."""
    if not updates:
        return True, ''
    uname = (username or '').strip().lower()
    # HIVE-212: prefer the numeric agent_user_id. Connect cannot resolve BYO/daemon-
    # registered agents by username (returns 400), so resolve the platform user_id
    # and send agent_user_id; fall back to username only when it can't be resolved
    # (keeps the container-fleet path working on id images that lack the lookup).
    uid = _resolve_agent_user_id(uname)
    if uid is not None:
        payload = {'agent_user_id': uid, 'config': updates}
    else:
        payload = {'username': uname, 'config': updates}
    try:
        r = requests.post(
            f'{_connect_internal_base()}/api/internal/agents/config/',
            json=payload,
            timeout=5,
        )
        if r.status_code >= 400:
            return False, f'connect returned {r.status_code}: {r.text[:200]}'
        return True, ''
    except Exception as exc:
        logger.warning('configure_agent_via_connect(%s) failed: %s', username, exc)
        return False, str(exc)


def telemetry_username_for_id(agent_id) -> str:
    """Resolve an agent UUID -> username via the live telemetry snapshots (used to
    route a config push for a BYO agent that has no daemon/native row)."""
    aid = str(agent_id)
    for uname, snap in agent_telemetry().items():
        if str((snap or {}).get('agent_id')) == aid:
            return uname
    return ''


def _fetch_agent_telemetry() -> dict:
    """Read one uncached Connect telemetry snapshot.

    Lifecycle safety callers use this directly at the actuation boundary.  The
    cached public reader remains appropriate for UI/list enrichment, but a
    cache warmed earlier in a reconcile command is not proof that a runtime is
    still idle when Hive is about to stop it.
    """
    try:
        r = requests.get(
            f'{_connect_internal_base()}/api/internal/agents/telemetry/', timeout=4,
        )
        r.raise_for_status()
        agents = (r.json() or {}).get('agents') or {}
        return {(k or '').strip().lower(): v for k, v in agents.items()}
    except Exception as exc:
        logger.warning('external-agents: connect telemetry read failed: %s', exc)
        return {}


@_ttl_cached('HIVE_EXTERNAL_TELEMETRY_TTL', 15.0)
def agent_telemetry() -> dict:
    """username(lower) -> latest runtime telemetry snapshot (model, context%,
    tok/s, health), reported by agent bridges over the Connect WS and exposed by
    shizuha-connect's internal endpoint. {} on any failure (telemetry is additive;
    the fleet list must still render without it)."""
    return _fetch_agent_telemetry()


def fresh_agent_telemetry() -> dict:
    """Uncached telemetry for a last-instant automatic lifecycle fence."""
    return _fetch_agent_telemetry()


def telemetry_fields(snap: dict, *, configured: dict | None = None) -> dict:
    """Flatten a telemetry snapshot into the agent-card fields the fleet UI reads.

    Health/context/usage always come from the live process. Harness/model are
    more subtle: after a Hive Agents UI convert, agents.json (desired) can
    already be shizuha/grok while the k8s pod is still on the previous bridge
    until redeploy. Prefer the Hive-configured method/model for card identity
    (source of truth = /hive/agents), and expose live process as running_*.
    """
    if not snap:
        return {}
    rt = snap.get('runtime') or {}
    ctx = snap.get('context') or {}
    usage = snap.get('usage') or {}
    act = snap.get('activity') or {}
    health = snap.get('health') or {}
    heartbeat = snap.get('heartbeat') or {}
    configured = configured or {}
    live_model = rt.get('model') or ''
    live_harness = rt.get('harness') or 'Remote'
    live_provider = rt.get('provider') or ''
    # Connect stamps ``received_at`` itself when it accepts the authenticated
    # bridge frame, so it is the authoritative freshness clock.  ``ts`` is a
    # bridge-provided millisecond epoch and is only a compatibility fallback.
    telemetry_epoch = None
    raw_received_at = snap.get('received_at')
    raw_bridge_ts = snap.get('ts')
    try:
        telemetry_epoch = float(raw_received_at)
    except (TypeError, ValueError):
        try:
            telemetry_epoch = float(raw_bridge_ts)
            if telemetry_epoch > 100_000_000_000:
                telemetry_epoch /= 1000
        except (TypeError, ValueError):
            telemetry_epoch = None

    telemetry_at = None
    if telemetry_epoch is not None and telemetry_epoch >= 0:
        try:
            telemetry_at = datetime.fromtimestamp(telemetry_epoch, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            telemetry_at = None

    fields = {
        'runtime_version': rt.get('version') or '',
        # Connect's authenticated receive clock is authoritative.  Prefer it
        # here as well as in ``telemetry_at``; using the bridge-supplied clock
        # first made a fresh frame appear stale under host clock skew.
        'telemetry_ts': snap.get('received_at') or snap.get('ts'),
        'context_used_tokens': ctx.get('used_tokens'),
        'context_max_tokens': ctx.get('max_tokens'),
        'context_pct': ctx.get('pct'),
        'tokens_per_sec': usage.get('tokens_per_sec'),
        'total_input_tokens': usage.get('total_input_tokens'),
        'total_output_tokens': usage.get('total_output_tokens'),
        'turns': usage.get('turns'),
        'activity_state': act.get('state'),
        'activity_phase': act.get('phase'),
        'activity_phase_for_ms': act.get('phase_for_ms'),
        'activity_tool_name': act.get('tool_name'),
        'activity_tool_count': act.get('tool_count'),
        'activity_seconds_24h': act.get('seconds_24h') or {},
        'last_activity_ms_ago': act.get('last_activity_ms_ago'),
        'health_ok': health.get('ok'),
        'health_token_pool_unavailable': health.get('token_pool_unavailable'),
        'health_token_pool_unavailable_since': health.get('token_pool_unavailable_since'),
        'provider_unavailable': health.get('provider_unavailable'),
        'health_consecutive_errors': health.get('consecutive_error_turns'),
        'health_error_turns_total': health.get('error_turns_total'),
        'recent_errors': health.get('recent_errors') or [],
        'has_telemetry': True,
        'running_runtime_harness': live_harness,
        'running_model': live_model,
        'running_model_provider': live_provider,
    }

    # PLAT-4172 originally reached Hive only through daemon-link state frames.
    # Native k3s agents have no managing daemon, so project the same outcome from
    # their authenticated Connect telemetry.  Never let an older telemetry
    # snapshot clear a newer daemon-link needs-help state during the 30s bridge
    # reporting interval.
    heartbeat_outcome = str(heartbeat.get('outcome') or '').strip()
    heartbeat_observed_at = heartbeat.get('observed_at')

    def _heartbeat_datetime(value):
        if isinstance(value, datetime):
            parsed = value
        elif value:
            try:
                parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            except ValueError:
                return None
        else:
            return None
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed

    apply_heartbeat = bool(heartbeat_outcome)
    configured_heartbeat_at = configured.get('heartbeat_observed_at')
    if apply_heartbeat and configured_heartbeat_at:
        live_heartbeat_at = _heartbeat_datetime(heartbeat_observed_at)
        configured_heartbeat_dt = _heartbeat_datetime(configured_heartbeat_at)
        if live_heartbeat_at is None or (
            configured_heartbeat_dt is not None and live_heartbeat_at < configured_heartbeat_dt
        ):
            apply_heartbeat = False
    if apply_heartbeat:
        needs_help = heartbeat_outcome == 'needs_help'
        fields.update({
            'heartbeat_outcome': heartbeat_outcome,
            'heartbeat_observed_at': heartbeat_observed_at or '',
            'heartbeat_ready_task_count': heartbeat.get('ready_task_count'),
            'heartbeat_blocked_task_count': heartbeat.get('blocked_task_count'),
            'heartbeat_future_due_count': heartbeat.get('future_due_count'),
            'heartbeat_progress_event_count': heartbeat.get('progress_event_count'),
            'heartbeat_consecutive_ready_no_progress': heartbeat.get(
                'consecutive_ready_no_progress_heartbeats'
            ),
            'needs_help': needs_help,
            'needs_help_reason': str(heartbeat.get('reason') or '') if needs_help else '',
        })

    if telemetry_at is not None:
        fields['last_telemetry_at'] = telemetry_at.isoformat().replace('+00:00', 'Z')

        # Runtime activity is distinct from telemetry delivery.  Derive the
        # actual last-work timestamp from the bridge's monotonic activity age,
        # anchored to Connect's server receipt time.  Never regress a newer
        # daemon/native timestamp already held by Hive.
        raw_activity_age_ms = act.get('last_activity_ms_ago')
        try:
            activity_age_ms = max(0.0, float(raw_activity_age_ms))
        except (TypeError, ValueError):
            activity_age_ms = None
        if activity_age_ms is not None:
            activity_at = datetime.fromtimestamp(
                max(0.0, telemetry_epoch - (activity_age_ms / 1000)),
                tz=timezone.utc,
            )
            configured_activity_at = None
            raw_configured_activity_at = configured.get('last_active_at')
            if isinstance(raw_configured_activity_at, datetime):
                configured_activity_at = raw_configured_activity_at
                if configured_activity_at.tzinfo is None:
                    configured_activity_at = configured_activity_at.replace(tzinfo=timezone.utc)
            elif raw_configured_activity_at:
                try:
                    configured_activity_at = datetime.fromisoformat(
                        str(raw_configured_activity_at).replace('Z', '+00:00')
                    )
                    if configured_activity_at.tzinfo is None:
                        configured_activity_at = configured_activity_at.replace(tzinfo=timezone.utc)
                except ValueError:
                    configured_activity_at = None
            if configured_activity_at is None or activity_at > configured_activity_at:
                fields['last_active_at'] = activity_at.isoformat().replace('+00:00', 'Z')

    configured_method = (configured.get('execution_method') or '').strip()
    configured_model = (
        configured.get('effective_model')
        or configured.get('model')
        or ''
    ).strip()
    configured_harness = (configured.get('runtime_harness') or '').strip()
    configured_provider = (
        configured.get('model_provider')
        or configured.get('runtime_provider')
        or ''
    ).strip()
    # Prefer Hive-configured identity when present; fall back to live process.
    if configured_harness:
        fields['runtime_harness'] = configured_harness
    else:
        fields['runtime_harness'] = live_harness
    if configured_provider:
        fields['model_provider'] = configured_provider
        fields['runtime_provider'] = configured_provider
    elif live_provider:
        fields['model_provider'] = live_provider
        fields['runtime_provider'] = live_provider
    if configured_model:
        fields['model'] = configured_model
        fields['effective_model'] = configured_model
        fields['model_source'] = configured.get('model_source') or 'pinned'
    elif live_model:
        fields['model'] = live_model
        fields['effective_model'] = live_model
    # Surface drift when the live process has not yet restarted onto the convert.
    if configured_method or configured_model:
        def _norm(value):
            return (value or '').replace('cortex/', '').strip().lower()

        live_norm = _norm(live_model)
        conf_norm = _norm(configured_model)
        # Hive is the source of truth — and for some lanes Hive's truth is a
        # CHAIN, not a single model. `managed_model_fallbacks` gives Grok Build
        # a sanctioned "grok primary, then cortex/DeepSeek-V4-Flash" chain
        # (operator exception 2026-08-01), and that chain is handed to the pod
        # in MODEL_FALLBACKS. An agent that fails over to step 2 is therefore
        # OBEYING Hive, not drifting from it.
        #
        # Comparing only against the primary called that compliance a drift:
        # nagi ran its Hive-issued DeepSeek fallback while grok was out of quota
        # (157 "no healthy token" + 34 upstream 403s in 24h) and was reported as
        # `runtime_config_drift` / `degraded` / health_ok=False the whole time
        # (operator 2026-08-04). Drift now means "running a model Hive never
        # authorised", which is the condition the check was actually for — a
        # bridge serving an UNSANCTIONED fallback is still caught.
        sanctioned = {conf_norm} if conf_norm else set()
        for step in (configured.get('model_fallbacks') or []):
            if isinstance(step, dict):
                sanctioned.add(_norm(step.get('model')))
        sanctioned.discard('')
        runtime_config_drift = bool(
            conf_norm and live_norm and live_norm not in sanctioned
        )
        fields['runtime_config_drift'] = runtime_config_drift
        if runtime_config_drift:
            # Fresh output on a model Hive never authorised is proof the drift
            # is still active, not evidence that it cleared.
            fields['health_ok'] = False
            fields['health_reason'] = 'runtime_config_drift'
            if not (
                fields.get('provider_unavailable') is True
                or fields.get('health_token_pool_unavailable') is True
            ):
                fields['availability_status'] = 'degraded'
    return fields


@_ttl_cached('HIVE_EXTERNAL_ID_TTL', 30.0)
def _bulk_agents_by_email() -> dict:
    """Preferred path: the HIVE-177 bulk list endpoint. email(lower) -> record.
    Returns {} if the id image predates it (then we fall back to per-agent)."""
    d = _id_get('/api/internal/agents/list/', {'active': 'true'})
    if not d:
        return {}
    return {(a.get('email') or '').strip().lower(): a for a in d.get('agents', [])}


def _resolve_agent_per_username(localpart: str):
    """Fallback using the pre-existing per-agent endpoint (present in ANY id
    image), so external surfacing survives id rollouts that lack the bulk list."""
    d = _id_get('/api/internal/agents/', {'username': localpart})
    if not d or not d.get('exists'):
        return None
    if (d.get('account_type') or 'human') != 'agent':
        return None
    return {
        'username': d.get('username') or localpart,
        'email': d.get('email'),
        'user_id': d.get('user_id'),
        'created_by_id': d.get('created_by_id'),
        'agent_runtime_id': d.get('agent_runtime_id'),
        'first_name': d.get('first_name'),
        'display_name': d.get('first_name') or (d.get('username') or localpart).capitalize(),
    }


_owner_username_cache: dict = {}


def _owner_username(owner_id):
    if not owner_id:
        return None
    if owner_id in _owner_username_cache:
        return _owner_username_cache[owner_id]
    d = _id_get(f'/api/internal/users/{owner_id}/') or {}
    name = d.get('username') or d.get('email')
    _owner_username_cache[owner_id] = name
    return name


def _k3s_runtime_ref(runtime_id: str):
    value = (runtime_id or '').strip()
    if not value.startswith('k3s:'):
        return None
    ref = value.split(':', 1)[1].strip()
    if '/' not in ref:
        return None
    namespace, deployment = [part.strip() for part in ref.split('/', 1)]
    if not namespace or not deployment:
        return None
    return namespace, deployment


def _flag_value(values, flag: str) -> str:
    try:
        idx = values.index(flag)
        return str(values[idx + 1] or '') if idx + 1 < len(values) else ''
    except ValueError:
        return ''


def _k3s_runtime_snapshot(runtime_id: str) -> dict:
    """Return a Hive-card shape for an owned k3s runtime reference.

    BYO/tenant-native agents do not always publish Pulse heartbeat keys or
    Connect telemetry. If Shizuha ID says an agent is hosted by a concrete k3s
    Deployment, Kubernetes is the authoritative liveness source.
    """
    ref = _k3s_runtime_ref(runtime_id)
    if not ref:
        return {}
    namespace, deployment_name = ref
    try:
        from . import provisioner
        _, apps_v1, _ = provisioner.get_k8s_client()
        deployment = apps_v1.read_namespaced_deployment(deployment_name, namespace)
    except Exception as exc:
        logger.info('external-agents: k3s runtime %s unavailable: %s', runtime_id, exc)
        return {
            'runtime_source': 'k3s-native',
            'runtime_harness': 'Shizuha CLI',
            'runtime_summary': f'k3s native runtime ({namespace}/{deployment_name})',
            'external_runtime': True,
            'lifecycle_managed': False,
            'status': 'stopped',
            'enabled': False,
            'availability_status': 'unavailable',
            'k3s_namespace': namespace,
            'k3s_deployment': deployment_name,
        }

    spec = deployment.spec
    status = deployment.status
    container = (spec.template.spec.containers or [None])[0]
    command = list((getattr(container, 'command', None) or []) if container else [])
    image = getattr(container, 'image', '') if container else ''
    desired = int(spec.replicas or 0)
    ready = int(status.ready_replicas or 0)
    running = desired > 0 and ready > 0
    model = _flag_value(command, '--model')
    username = _flag_value(command, '--agent-username')
    display_name = _flag_value(command, '--agent-name')
    harness = 'Shizuha CLI' if 'shizuha-agent-runtime' in (image or '') else 'k3s'
    return {
        'agent_username': username or '',
        'display_name': display_name or '',
        'model': model,
        'effective_model': model,
        'model_source': 'k3s',
        'model_provider': 'Cortex' if model.startswith('cortex/') else '',
        'runtime_source': 'k3s-native',
        'runtime_harness': harness,
        'runtime_summary': f'{harness} ({namespace}/{deployment_name})',
        'runtime_image_tag': image.rsplit(':', 1)[-1] if ':' in (image or '').rsplit('/', 1)[-1] else '',
        'effective_image': image,
        'external_runtime': True,
        'lifecycle_managed': False,
        'status': 'running' if running else 'stopped',
        'enabled': running,
        'availability_status': 'available' if running else 'unavailable',
        'k3s_namespace': namespace,
        'k3s_deployment': deployment_name,
        'k3s_ready_replicas': ready,
        'k3s_desired_replicas': desired,
        'has_telemetry': False,
    }


def list_external_agents(exclude_usernames=None, owner_id=None) -> list:
    """Heartbeat-alive account_type=agent accounts not already shown as
    FleetAgent / rt-fleet. Shaped like normalize_runtime_agent so the same UI
    renders them; tagged runtime_source='external' + is_external_byo=True and
    carrying owner/owner_id from shizuha-id created_by.

    Resolution is resilient to the id image: prefer the bulk list endpoint, fall
    back to the pre-existing per-agent + user endpoints when it's absent.
    """
    hb = _alive_heartbeats()
    bulk = _bulk_agents_by_email()
    owner_id_str = str(owner_id) if owner_id is not None else ''
    candidate_emails = set(hb.keys())
    for email, rec in bulk.items():
        if owner_id_str and str(rec.get('created_by_id') or '') != owner_id_str:
            continue
        if _k3s_runtime_ref(rec.get('agent_runtime_id') or ''):
            candidate_emails.add(email)
    if not candidate_emails:
        return []
    exclude = {(u or '').lower() for u in (exclude_usernames or set()) if u}
    # An account that already has a live FleetAgent row is INTERNAL (Hive ->
    # fleet-daemon managed), never external BYO — even if it also has a heartbeat
    # or a k3s runtime ref. Surfacing it as external mislabels it and hides its
    # daemon lifecycle controls behind the read-only "owning runtime" view.
    # (operator 2026-07-12: all agents are internal, Hive -> fleet-daemon managed
    # only; no external BYO.) Exclude internally so no caller can leak it through.
    from .models import FleetAgent
    internal_usernames = {
        (u or '').strip().lower()
        for u in FleetAgent.objects.exclude(status=FleetAgent.Status.DELETED)
        .values_list('agent_username', flat=True)
        if u
    }
    exclude |= internal_usernames
    tele = agent_telemetry()
    now = int(time.time())
    stale = _hb_stale_sec()
    identities = {}
    for email in sorted(candidate_emails):
        email = (email or '').strip().lower()
        last = hb.get(email)
        localpart = email.split('@')[0]
        if not localpart or localpart in exclude:
            continue  # already a live FleetAgent / rt-fleet entry, or junk
        rec = bulk.get(email)
        if rec is None:
            rec = _resolve_agent_per_username(localpart)
        if not rec:
            continue  # not an account_type=agent account we can resolve
        canonical = bulk.get((rec.get('email') or '').strip().lower())
        if canonical and str(canonical.get('user_id')) == str(rec.get('user_id')):
            rec = canonical
        if owner_id_str and str(rec.get('created_by_id') or '') != owner_id_str:
            continue
        uname = (rec.get('username') or localpart).strip().lower()
        identity = str(rec.get('user_id') or '')
        if not identity or uname in exclude:
            continue
        previous = identities.get(identity)
        identities[identity] = (rec, max(last or 0, previous[1] if previous else 0))
    out = []
    for rec, last in identities.values():
        owner = rec.get('created_by_username') or _owner_username(rec.get('created_by_id'))
        age = max(0, now - int(last or 0)) if last else None
        k3s = _k3s_runtime_snapshot(rec.get('agent_runtime_id') or '')
        running = (age is not None and age <= stale) or k3s.get('status') == 'running'
        uname = rec.get('username') or (rec.get('email') or '').split('@')[0]
        record = {
            'id': f'ext:{rec.get("user_id")}',
            'agent_username': uname,
            'agent_id': str(rec.get('user_id') or ''),
            'display_name': rec.get('display_name') or rec.get('first_name') or uname.capitalize(),
            'role': 'Agent',
            'email': rec.get('email') or '',
            'model': '',
            'effective_model': '',
            'model_source': 'external',
            'model_provider': '',
            'status': 'running' if running else 'stopped',
            'runtime_source': 'external',
            'runtime_harness': 'Remote',
            'runtime_summary': 'Remote self-hosted runtime (heartbeat)',
            'external_runtime': True,
            'lifecycle_managed': False,
            'enabled': running,
            'owner': owner,
            'owner_id': rec.get('created_by_id'),
            'agent_runtime_id': rec.get('agent_runtime_id'),
            'is_external_byo': True,
            'heartbeat_age_sec': age,
            'last_seen_epoch': int(last or 0),
            'has_telemetry': False,
        }
        if k3s:
            record.update({k: v for k, v in k3s.items() if v not in ('', None)})
            if k3s.get('agent_username'):
                record['agent_username'] = k3s['agent_username']
            if k3s.get('display_name'):
                record['display_name'] = k3s['display_name']
        # Enrich with live bridge telemetry (model / context% / tok-per-sec /
        # health) when the agent has reported it over Connect — overrides the
        # empty model + generic 'Remote' harness with the real, live values.
        tsnap = tele.get(uname.lower())
        if tsnap:
            record['model_source'] = 'telemetry'
            record['runtime_summary'] = 'Remote self-hosted runtime (live telemetry)'
            record.update(telemetry_fields(tsnap, configured=dict(record)))
        out.append(record)
    return out

def claim_external_agent(agent_user_id, *, actor_user_id, actor_email='', is_staff=False, action='claim') -> tuple[bool, dict | str]:
    """Ask shizuha-id to claim/reassign/unclaim an external/BYO agent account.

    Hive never accepts an owner id from the agent/runtime side; the caller passes
    the authenticated human actor derived from the Hive request. shizuha-id owns
    the atomic created_by update and audit signal.
    """
    try:
        r = requests.patch(
            f'{_id_internal_base()}/api/internal/agents/',
            json={
                'agent_user_id': agent_user_id,
                'actor_user_id': actor_user_id,
                'actor_email': actor_email,
                'is_staff': bool(is_staff),
                'action': action,
            },
            timeout=6,
        )
        if r.status_code >= 400:
            try:
                detail = r.json()
            except Exception:
                detail = r.text[:200]
            return False, detail
        return True, r.json()
    except Exception as exc:
        logger.warning('claim_external_agent(%s) failed: %s', agent_user_id, exc)
        return False, str(exc)
