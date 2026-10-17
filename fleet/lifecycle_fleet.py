"""Authoritative agent inventory and actuation for automatic lifecycle policy.

Hive operates two managed runtime planes during the k3s migration:

* native :class:`~fleet.models.FleetAgent` deployments, scaled by Hive; and
* rt-fleet agents, controlled through the daemon proxy.

Automatic hibernation and wake policy must see and actuate both planes.  The
operator UI already merges them, so using only ``list_runtime_agents`` here
would make native agents visible as Running while silently excluding them from
the lifecycle controller.
"""
from __future__ import annotations

import logging
from typing import Any

from django.core.exceptions import ValidationError

logger = logging.getLogger(__name__)

from . import agent_index_cache, provisioner
from .runtime_fleet import (
    _apply_cached_runtime_config,
    configure_runtime_agent,
    list_runtime_agents,
    normalize_runtime_config_updates,
    set_runtime_agent_enabled,
)
from .timeutils import naive_utc_now


def _native_lifecycle_agent(agent) -> dict[str, Any]:
    """Project a native row into the small policy-facing agent contract."""
    username = str(agent.agent_username or '').strip().lower()
    skill_config = dict(agent.skill_config or {})
    mcp_config = dict(agent.mcp_config or {})
    # The planner and start guard must see the same model the native launcher
    # will use, including the configured default and retired-model remapping.
    runtime_model = provisioner._resolve_model(agent)
    # Personal/owned seats use the dedicated @agents.shizuha.io identity that
    # Connect stores on the DM participant. Org fleet seats stay @shizuha.com.
    email_domain = 'agents.shizuha.io' if getattr(agent, 'owner_id', None) else 'shizuha.com'
    return {
        # Use the Hive row UUID for native actuation.  ``agent_id`` is the
        # platform identity UUID and is not the FleetAgent primary key.
        'id': str(agent.id),
        'agent_username': username,
        'username': username,
        'email': f'{username}@{email_domain}' if username else '',
        'enabled': agent.status == agent.Status.RUNNING,
        'status': agent.status,
        'hibernation_reason': agent.hibernation_reason,
        'hibernated_at': agent.hibernated_at.isoformat() if agent.hibernated_at else None,
        'last_active_at': agent.last_active_at.isoformat() if agent.last_active_at else None,
        'lifecycle_plane': 'native',
        # PLAT-5265: the health enrichment resolves an agent's provider from
        # these; without them a native row can never match a poisoned provider
        # and provider-unavailable enrichment is a silent no-op on the very
        # plane the incident happened on.
        'model': runtime_model,
        'effective_model': runtime_model,
        # Capability reconciliation operates on the same normalized contract
        # for native and daemon agents. Include both documents and their flat
        # convenience fields so a second pass observes the config it persisted.
        'skill_config': skill_config,
        'mcp_config': mcp_config,
        'skills': skill_config.get('skills') or [],
        'eager_skills': skill_config.get('eager_skills') or [],
        'enabled_mcp_server_ids': mcp_config.get('enabled_mcp_server_ids') or [],
        'warm_pool_size': mcp_config.get('warm_pool_size') or 0,
    }


def list_lifecycle_agents(*, include_disabled: bool = True) -> list[dict[str, Any]]:
    """Return every Hive-managed agent eligible for lifecycle reconciliation.

    Runtime wins by stable username, matching the agents page: a daemon agent
    that replaced an old native row must not be represented twice or actuated
    through the retired k3s deployment.
    """
    from .models import FleetAgent

    runtime = [dict(agent, lifecycle_plane='runtime') for agent in list_runtime_agents(
        include_disabled=include_disabled,
    )]
    runtime_usernames = {
        str(agent.get('agent_username') or agent.get('username') or '').strip().lower()
        for agent in runtime
    }
    native_rows = (
        FleetAgent.objects
        .filter(managing_daemon__isnull=True)
        .exclude(status=FleetAgent.Status.DELETED)
        .order_by('agent_username')
    )
    native = []
    for row in native_rows:
        projected = _native_lifecycle_agent(row)
        if projected['agent_username'] in runtime_usernames:
            continue
        if not include_disabled and not projected['enabled']:
            continue
        native.append(projected)
    return runtime + native


def stamp_native_runtime_ready(agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project Deployment readiness onto lifecycle dicts (fail open).

    Hive ``status=running`` is not a Ready pod. Native CrashLoop seats (Sara
    ENOSPC) and rt-fleet pods whose broker never becomes Ready (mio/ryo/sara
    identity 401, 2026-08-26) both look RUNNING while they cannot take a
    model turn. Stamping only ``lifecycle_plane=native`` left those rt-fleet
    holders on the DeepSeek board: 8/8 leases, 5 KV homes, waiters hibernated.

    Scaled-to-zero hibernated seats: probe returns None (not False) so they
    stay GRANT+START-able. Ghost ``status=running`` with 0 replicas is False.
    """
    from .models import FleetAgent

    by_name: dict[str, dict[str, Any]] = {}
    for agent in agents or []:
        uname = str(agent.get('username') or agent.get('agent_username') or '').strip()
        if uname:
            by_name[uname] = agent
    if not by_name:
        return agents
    try:
        rows = list(FleetAgent.objects.filter(agent_username__in=by_name.keys()))
    except Exception:  # noqa: BLE001
        return agents
    for row in rows:
        uname = str(row.agent_username or '').strip()
        agent = by_name.get(uname)
        if agent is None:
            continue
        try:
            ready = provisioner.deployment_runtime_ready(row)
        except Exception:  # noqa: BLE001
            ready = None
        status = str(agent.get('status') or '').strip().lower()
        if ready is None and status in {'running', 'starting', 'provisioning', 'pending'}:
            # Deployment scaled to 0 (or unreadable spec) while Hive still
            # says running: ghost holder, not a sleeping waiter.
            ready = False
        if ready is not None:
            agent['runtime_ready'] = ready
        if ready is False:
            try:
                provisioner.heal_workspace_disk(row)
            except Exception:  # noqa: BLE001
                pass
    return agents


def _find_native_lifecycle_agent(agent_id: str):
    """Resolve a native FleetAgent UUID without swallowing runtime identifiers."""
    from .models import FleetAgent

    try:
        return (
            FleetAgent.objects
            .filter(managing_daemon__isnull=True)
            .exclude(status=FleetAgent.Status.DELETED)
            .filter(id=agent_id)
            .first()
        )
    except (TypeError, ValueError, ValidationError):
        return None


def _k8s_lifecycle_agent(agent_id: str):
    """FleetAgent whose living process is the Hive-rendered k3s Deployment.

    Native rows (no daemon) always. Daemon-linked rows only when a Deployment
    is actually readable — leftover rt-fleet identifiers without a deploy
    still go through the daemon toggle.

    ryo 2026-08-28: Hive ``status=hibernated`` while ``agent-ryo`` stayed
    Ready because hibernate resolved only ``managing_daemon IS NULL`` and
    toggled rt-fleet-control (which was down). The pod kept hitting Cortex.
    """
    native = _find_native_lifecycle_agent(agent_id)
    if native is not None:
        return native
    row = _find_k3s_deploy_agent(agent_id)
    if row is None or getattr(row, 'managing_daemon_id', None) is None:
        return row
    try:
        if provisioner.current_replicas(row) is None:
            return None
    except Exception:  # noqa: BLE001 — unreadable deploy is not a k8s plane
        return None
    return row


def _runtime_agent_missing(detail: Any) -> bool:
    """True when rt-fleet control does not have this Hive agent at all."""
    text = str(detail or '').lower()
    return 'agent not found' in text


def _find_any_fleet_agent(agent_id: str):
    """Resolve a FleetAgent by UUID, runtime agent_id, or username."""
    from .models import FleetAgent

    key = str(agent_id or '').strip()
    if not key:
        return None
    qs = FleetAgent.objects.exclude(status=FleetAgent.Status.DELETED)
    try:
        row = qs.filter(id=key).first()
    except (TypeError, ValueError, ValidationError):
        row = None
    if row is None:
        row = qs.filter(agent_id=key).first()
    if row is None:
        row = qs.filter(agent_username=key).first()
    return row


def _best_effort_runtime_toggle(agent, *, enabled: bool) -> None:
    """Keep the leftover rt-fleet daemon flag aligned with k8s scale.

    Daemon-linked k3s seats still have a runtime-fleet row. If Hive scales
    the Deployment to 0 but leaves the daemon enabled, the daemon's next
    reconcile writes ``replicas: 1`` (ryo connect-wake 2026-08-28 16:02Z).
    A down control proxy must not fail the native scale.

    Operator 2026-09-01: automatic disable is hibernation, never operator
    stop. Only an explicit Hive Stop (set_lifecycle_agent_enabled False)
    may send lifecycle_state=operator_stopped.
    """
    if getattr(agent, 'managing_daemon_id', None) is None:
        return
    for key in (
        str(getattr(agent, 'id', '') or ''),
        str(getattr(agent, 'agent_id', '') or ''),
    ):
        if not key:
            continue
        try:
            kwargs = {'lifecycle_state': 'hibernated'} if not enabled else {}
            ok, _detail = set_runtime_agent_enabled(key, enabled, **kwargs)
            if ok:
                return
        except Exception:  # noqa: BLE001
            logger.warning(
                'rt-fleet toggle enabled=%s after native lifecycle %s failed',
                enabled, getattr(agent, 'agent_username', ''),
                exc_info=True,
            )


def _find_k3s_deploy_agent(agent_id: str):
    """Resolve a Hive-rendered k3s Deployment row, even if daemon-linked.

    Several DeepSeek holders (hiro/kai/nagi/saki/san/sara) still have
    ``managing_daemon`` set from the rt-fleet era but run as k3s Deployments
    that ``ensure_agent_image`` owns. Native-only lookup skipped them, so
    heartbeat-env heals never reached the live pod.
    """
    from .models import FleetAgent

    key = str(agent_id or '').strip()
    if not key:
        return None
    qs = FleetAgent.objects.exclude(status=FleetAgent.Status.DELETED)
    row = None
    try:
        row = qs.filter(id=key).first()
    except (TypeError, ValueError, ValidationError):
        row = None
    if row is None:
        row = qs.filter(agent_id=key).first()
    if row is None:
        row = qs.filter(agent_username=key).first()
    if row is None:
        return None
    if not (getattr(row, 'deployment_name', None) or '').strip():
        return None
    return row


def _find_runtime_lifecycle_agent(agent_id: str):
    """Resolve the Hive desired-state row behind an rt-fleet identifier."""
    from .models import FleetAgent

    key = str(agent_id or '').strip()
    if not key:
        return None
    row = (
        FleetAgent.objects
        .filter(managing_daemon__isnull=False, agent_id=key)
        .exclude(status=FleetAgent.Status.DELETED)
        .first()
    )
    if row is not None:
        return row
    try:
        return (
            FleetAgent.objects
            .filter(managing_daemon__isnull=False, id=key)
            .exclude(status=FleetAgent.Status.DELETED)
            .first()
        )
    except (TypeError, ValueError, ValidationError):
        return None


def _release_runtime_lease(
    agent_id: str,
    detail: Any,
    *,
    reason: str,
    username: str = '',
) -> None:
    try:
        from .model_lease import release_leases_for_agent
        row = _find_runtime_lifecycle_agent(agent_id)
        uname = str(username or (row.agent_username if row is not None else ''))
        if not uname and isinstance(detail, dict):
            uname = str(detail.get('username') or detail.get('agent_username') or '')
        release_leases_for_agent(uname, reason=reason)
        # PLAT-9052: a slot just freed — honor the DEFER promise by granting
        # the oldest outstanding deferred connect-wake, if any.
        from .wake_defers import reconcile_deferred_wakes
        reconcile_deferred_wakes()
    except Exception:  # noqa: BLE001 — lifecycle actuation must fail open on observation cleanup
        pass


def _runtime_confirmed_lifecycle_state(detail: Any, expected: str) -> bool:
    """True only when the runtime echoed the typed state it committed."""
    return bool(
        isinstance(detail, dict)
        and str(detail.get('lifecycle_state') or '').strip().lower() == expected
    )


def configure_lifecycle_agent(agent_id: str, updates: dict) -> tuple[bool, str]:
    """Persist desired config through the authoritative lifecycle plane.

    Daemon agents retain the existing top-down config push. Native agents have
    no daemon actuator, so Hive persists the same normalized desired documents
    directly; stopped agents then remain converged without a manual wake.
    """
    native = _find_native_lifecycle_agent(agent_id)
    if native is None:
        return configure_runtime_agent(agent_id, updates)
    if not updates:
        return True, ''

    desired = normalize_runtime_config_updates(updates)
    _apply_cached_runtime_config(native, desired)
    agent_index_cache.invalidate(
        f'native lifecycle configure {native.agent_username}',
    )
    return True, 'saved native desired config'


def set_lifecycle_agent_enabled(agent_id: str, enabled: bool) -> tuple[bool, Any]:
    """Start/stop a native or rt-fleet agent through its authoritative plane."""
    from .models import FleetAgent

    native = _k8s_lifecycle_agent(agent_id)

    if native is None:
        ok, detail = set_runtime_agent_enabled(agent_id, enabled)
        if enabled and not ok and _runtime_agent_missing(detail):
            # rt-fleet lost the roster row (2026-09-01: 13 GLM lease
            # holders START → 500 Agent not found, on-demand CronJob Failed
            # for an hour). Hive still owns the FleetAgent; provision the
            # k3s Deployment instead of leaving the lease seat empty.
            row = _find_runtime_lifecycle_agent(agent_id) or _find_any_fleet_agent(agent_id)
            if row is not None and provisioner.start_agent(row):
                agent_index_cache.invalidate(
                    f'native-fallback start {row.agent_username}',
                )
                return True, {
                    'plane': 'native-fallback',
                    'status': row.status,
                    'reason': 'rt-fleet agent missing',
                }
            pending_reason = provisioner.lease_start_pending_reason(row) if row is not None else None
            if pending_reason is not None:
                return False, {'plane': 'native-fallback', 'lease_waiting': True, 'reason': pending_reason}
        # rt-fleet stop must also drop Cortex leases (same pin hazard as native).
        if ok and not enabled:
            row = _find_runtime_lifecycle_agent(agent_id)
            if row is not None:
                previous_status = row.status
                row.status = FleetAgent.Status.STOPPED
                row.hibernation_reason = ''
                row.hibernated_at = None
                update_fields = [
                    'status', 'hibernation_reason', 'hibernated_at', 'updated_at',
                ]
                if _runtime_confirmed_lifecycle_state(
                    detail, 'operator_stopped',
                ):
                    row.runtime_lifecycle_state = 'operator_stopped'
                    update_fields.append('runtime_lifecycle_state')
                row.save(update_fields=update_fields)
                # PLAT-8834: rt-fleet seat-loss telemetry (best-effort).
                from . import seat_lifecycle_metrics
                seat_lifecycle_metrics.record_transition(
                    row.agent_username, previous_status, FleetAgent.Status.STOPPED,
                )
            _release_runtime_lease(
                agent_id, detail, reason=f'rt-fleet explicit stop {agent_id}',
            )
        elif ok and enabled:
            row = _find_runtime_lifecycle_agent(agent_id)
            if row is not None:
                previous_status = row.status
                row.status = FleetAgent.Status.PROVISIONING
                row.hibernation_reason = ''
                row.hibernated_at = None
                update_fields = [
                    'status', 'hibernation_reason', 'hibernated_at', 'updated_at',
                ]
                if _runtime_confirmed_lifecycle_state(detail, 'enabled'):
                    row.runtime_lifecycle_state = 'enabled'
                    update_fields.append('runtime_lifecycle_state')
                row.save(update_fields=update_fields)
                # PLAT-8834: rt-fleet serve transition telemetry (best-effort).
                from . import seat_lifecycle_metrics
                seat_lifecycle_metrics.record_transition(
                    row.agent_username, previous_status, FleetAgent.Status.PROVISIONING,
                )
        if ok:
            agent_index_cache.invalidate(
                f'rt-fleet lifecycle {"start" if enabled else "stop"} {agent_id}',
            )
        return ok, detail

    if enabled and native.status == FleetAgent.Status.RUNNING:
        replicas = provisioner.current_replicas(native)
        if replicas is not None and replicas > 0:
            return converge_running_lifecycle_agent(native)
        # Ghost RUNNING: FleetAgent says running but the Deployment is 0 (or
        # unreadable). Fall through to start_agent so a confirmed lease holder
        # actually gets a pod. Observed 2026-08-12: nami held DeepSeek 1/5
        # with status=running and replicas=0; enable() was a no-op.
    if not enabled and native.status == FleetAgent.Status.STOPPED:
        # Idempotent stop: still try lease release — agent may already be stopped
        # while a stale Cortex board still names them as holder (Nami incident).
        if native.hibernation_reason or native.hibernated_at is not None:
            native.hibernation_reason = ''
            native.hibernated_at = None
            native.save(update_fields=[
                'hibernation_reason', 'hibernated_at', 'updated_at',
            ])
        try:
            from .model_lease import release_leases_for_agent
            release_leases_for_agent(
                native.agent_username,
                reason=f'idempotent stop {native.agent_username}',
            )
            # PLAT-9052: slot freed — reconcile deferred connect-wakes.
            from .wake_defers import reconcile_deferred_wakes
            reconcile_deferred_wakes()
        except Exception:  # noqa: BLE001
            pass
        return True, {'plane': 'native', 'status': native.status, 'unchanged': True}

    ok = provisioner.start_agent(native) if enabled else provisioner.stop_agent(native)
    if ok:
        if not enabled:
            _release_runtime_lease(
                str(native.id), {},
                reason=f'native explicit stop {native.agent_username}',
                username=native.agent_username,
            )
        _best_effort_runtime_toggle(native, enabled=enabled)
        agent_index_cache.invalidate(
            f'native lifecycle {"start" if enabled else "stop"} {native.agent_username}',
        )
        return True, {'plane': 'native', 'status': native.status}
    pending_reason = provisioner.lease_start_pending_reason(native) if enabled else None
    if pending_reason is not None:
        return False, {'plane': 'native', 'lease_waiting': True, 'reason': pending_reason}
    return False, {
        'plane': 'native',
        'error': f'failed to {"start" if enabled else "stop"} {native.agent_username}',
    }


def converge_running_lifecycle_agent(agent) -> tuple[bool, Any]:
    """Re-render Hive-owned lane env on a live native seat without scaling.

    On-demand skips already-running lease holders (claimed_running ==
    should_run). Without this, a provisioner heartbeat/model change never
    reaches a live DeepSeek holder until the next hibernate/wake.
    """
    native = agent
    if isinstance(agent, str):
        native = _find_native_lifecycle_agent(agent) or _find_k3s_deploy_agent(agent)
    if native is None:
        return False, {'plane': 'unknown', 'error': 'not a native lifecycle agent'}
    if provisioner.heal_stale_work_agent_heartbeat_env(native) and getattr(native, 'pk', None):
        native.save(update_fields=['env', 'updated_at'])
    try:
        provisioner.heal_workspace_disk(native)
    except Exception:  # noqa: BLE001 — disk heal must not block lane converge
        pass
    ok = provisioner.ensure_agent_image(native)
    return bool(ok), {
        'plane': 'native',
        'status': native.status,
        'unchanged': True,
        'converged': bool(ok),
    }


# Hive PATCH fields that live in the k3s Deployment env contract
# (``provisioner._RUNTIME_LANE_ENV_NAMES``). A daemon config_update frame
# never reaches these pods.
from .placement_contract import PLACEMENT_FIELDS


K3S_PATCH_LANE_FIELDS = PLACEMENT_FIELDS | frozenset({
    'model', 'execution_method', 'reasoning_effort',
    'model_fallbacks', 'model_overrides',
})


def _model_yield_aliases(model: str) -> list[str]:
    raw = str(model or '').strip()
    if not raw:
        return []
    names = {raw}
    if raw.lower().startswith('cortex/'):
        names.add(raw.split('/', 1)[1])
    else:
        names.add('cortex/' + raw)
    return sorted(names)


def drop_previous_model_occupancy(agent, old_model: str, *, reset_session: bool = True) -> dict:
    """Yield Cortex homes on the previous model and wipe the PVC session.

    Nova 2026-09-18: Hive SoT was already GLM-5.3-Flash while SCLI kept POSTing
    Vision-Metal every 120s. PATCH/converge updated ``MODEL`` env (eventually)
    but never deleted the Metal Redis book or ``agent-session-*`` sqlite, and
    leftover yield skips *running* agents on the same pool. Conversion must
    drop the old occupancy even while the pod stays Running on the new model.
    """
    result = {'yielded': [], 'session_reset': False, 'errors': []}
    old = str(old_model or '').strip()
    new = str(getattr(agent, 'model', '') or '').strip()
    if not old or old == new:
        return result
    username = str(getattr(agent, 'agent_username', '') or '').strip()
    try:
        from .model_lease import CortexLeaseClient, principal_for
        client = CortexLeaseClient()
        principal = principal_for(username) if username else ''
        if principal:
            for model_id in _model_yield_aliases(old):
                if client.yield_homes(
                    model_id, principal, reason=f'model changed {old} -> {new}',
                ):
                    result['yielded'].append(model_id)
    except Exception as exc:  # noqa: BLE001 — occupancy drop must not fail PATCH
        logger.exception(
            'drop_previous_model_occupancy: yield failed for %s %s: %s',
            username, old, exc,
        )
        result['errors'].append(f'yield:{exc}')
    if reset_session:
        try:
            payload = provisioner.reset_k8s_native_runtime_session(agent)
            result['session_reset'] = True
            result['session'] = payload
        except Exception as exc:  # noqa: BLE001 — scaled-to-zero / no pod
            logger.info(
                'drop_previous_model_occupancy: session reset skipped for %s: %s',
                username, exc,
            )
            result['errors'].append(f'session:{exc}')
    return result


def apply_k3s_lane_after_patch(agent, *, previous_model: str = '') -> dict:
    """Event-driven k3s actuator for a Hive lane PATCH.

    kai/nagi 2026-09-05: Hive SoT was already ``cortex/grok-4.6`` while the
    live Deployment stayed ``MODEL=cortex/GLM-5.3-Flash`` until a human
    disable+enable. ``admit_runtime_lane_update`` only writes the daemon
    outbox (ACTUATOR_PENDING); Hive-rendered Deployments never see that
    frame. Enable-of-running already calls ``converge_running_lifecycle_agent``;
    PATCH must too.

    ``current_replicas is None`` means there is no readable Deployment
    (pure rt-fleet docker, or k8s unreadable) — skip, daemon path remains.
    """
    replicas = provisioner.current_replicas(agent)
    if replicas is None:
        result = {'applied': False, 'reason': 'no_k3s_deployment'}
    else:
        ok, detail = converge_running_lifecycle_agent(agent)
        result = {
            'applied': bool(ok),
            'replicas': replicas,
            'reason': 'converged' if ok else 'ensure_agent_image_failed',
        }
        if isinstance(detail, dict):
            result.update(detail)
    old = str(previous_model or '').strip()
    if old and old != str(getattr(agent, 'model', '') or '').strip():
        result['occupancy'] = drop_previous_model_occupancy(agent, old)
    return result


def notify_pulse_agent_lifecycle(
    *,
    username: str,
    email: str = '',
    status: str,
    reason: str = '',
) -> None:
    """Best-effort event so Pulse can drain ready work off a hibernated seat.

    Must never raise: a Pulse outage must not block Hive lifecycle actuation.
    """
    import os

    import requests
    from django.conf import settings

    base = (
        getattr(settings, 'HIVE_PULSE_API_URL', '')
        or os.environ.get('HIVE_PULSE_API_URL', '')
    ).rstrip('/')
    uname = (username or '').strip().lower()
    em = (email or '').strip().lower() or (f'{uname}@shizuha.com' if uname else '')
    if not base or not em:
        return
    token = (
        getattr(settings, 'HIVE_PULSE_API_TOKEN', '')
        or os.environ.get('HIVE_PULSE_API_TOKEN', '')
    ).strip()
    if not token:
        return
    headers = {
        'Content-Type': 'application/json',
        'Authorization': (
            token if token.lower().startswith('bearer ') else f'Bearer {token}'
        ),
    }
    try:
        requests.post(
            f'{base}/internal/agent-lifecycle/',
            json={
                'username': uname,
                'email': em,
                'status': status,
                'reason': reason or '',
            },
            headers=headers,
            timeout=5,
        )
    except Exception:
        logger.warning(
            'pulse lifecycle notify failed for %s status=%s',
            em, status, exc_info=True,
        )


def hibernate_lifecycle_agent(agent_id: str, *, reason: str) -> tuple[bool, Any]:
    """Automatically scale an agent to zero while retaining auto-wake intent."""
    from .models import FleetAgent

    native = _k8s_lifecycle_agent(agent_id)
    if native is not None:
        if native.status == FleetAgent.Status.HIBERNATED:
            replicas = None
            try:
                replicas = provisioner.current_replicas(native)
            except Exception:  # noqa: BLE001
                replicas = None
            if replicas is None or replicas <= 0:
                _release_runtime_lease(
                    agent_id, {}, reason=f'idempotent native hibernate {native.agent_username}',
                    username=native.agent_username,
                )
                return True, {'plane': 'native', 'status': native.status, 'unchanged': True}
            # Ghost living: FleetAgent already hibernated but the Deployment
            # still has replicas (ryo 2026-08-28). Scale to zero.
        ok = provisioner.hibernate_agent(native, reason=reason)
        if ok:
            _release_runtime_lease(
                str(native.id), {},
                reason=f'native hibernate {native.agent_username}: {reason}',
                username=native.agent_username,
            )
            _best_effort_runtime_toggle(native, enabled=False)
            agent_index_cache.invalidate(
                f'native lifecycle hibernate {native.agent_username}',
            )
            projected = _native_lifecycle_agent(native)
            notify_pulse_agent_lifecycle(
                username=projected.get('username') or native.agent_username,
                email=projected.get('email') or '',
                status=FleetAgent.Status.HIBERNATED,
                reason=reason,
            )
            return True, {'plane': 'native', 'status': native.status}
        return False, {
            'plane': 'native',
            'error': f'failed to hibernate {native.agent_username}',
        }

    ok, detail = set_runtime_agent_enabled(
        agent_id,
        False,
        lifecycle_state='hibernated',
    )
    if not ok and _runtime_agent_missing(detail):
        # rt-fleet lost the roster row (jun 2026-09-01: SYNC_HIBERNATION
        # 500 Agent not found failed the on-demand CronJob every 10 min
        # after leftover GLM hibernates). No process exists to scale down;
        # persist Hive hibernated + drop Cortex leases/homes.
        row = _find_runtime_lifecycle_agent(agent_id) or _find_any_fleet_agent(agent_id)
        if row is not None:
            from .models import FleetAgent

            row.status = FleetAgent.Status.HIBERNATED
            row.hibernation_reason = str(reason or 'Hive automatic hibernation policy')[:255]
            row.hibernated_at = naive_utc_now()
            update_fields = [
                'status', 'hibernation_reason', 'hibernated_at', 'updated_at',
            ]
            row.runtime_lifecycle_state = 'hibernated'
            update_fields.append('runtime_lifecycle_state')
            row.save(update_fields=update_fields)
            _release_runtime_lease(
                str(row.id), {},
                reason=f'rt-fleet missing hibernate {row.agent_username}: {reason}',
                username=row.agent_username,
            )
            agent_index_cache.invalidate(
                f'rt-fleet-missing hibernate {row.agent_username}',
            )
            notify_pulse_agent_lifecycle(
                username=row.agent_username,
                email=str(getattr(row, 'email', '') or ''),
                status=FleetAgent.Status.HIBERNATED,
                reason=reason,
            )
            return True, {
                'plane': 'runtime-missing',
                'status': row.status,
                'reason': 'rt-fleet agent missing',
            }
    if not ok:
        return ok, detail
    row = _find_runtime_lifecycle_agent(agent_id)
    email = ''
    username = ''
    if row is not None:
        row.status = FleetAgent.Status.HIBERNATED
        row.hibernation_reason = str(reason or 'Hive automatic hibernation policy')[:255]
        row.hibernated_at = naive_utc_now()
        update_fields = [
            'status', 'hibernation_reason', 'hibernated_at', 'updated_at',
        ]
        if _runtime_confirmed_lifecycle_state(detail, 'hibernated'):
            row.runtime_lifecycle_state = 'hibernated'
            update_fields.append('runtime_lifecycle_state')
        row.save(update_fields=update_fields)
        username = str(getattr(row, 'agent_username', '') or '').strip().lower()
        email = str(getattr(row, 'email', '') or '').strip().lower()
    _release_runtime_lease(
        agent_id, detail, reason=f'rt-fleet hibernate {agent_id}: {reason}',
    )
    agent_index_cache.invalidate(f'rt-fleet lifecycle hibernate {agent_id}')
    notify_pulse_agent_lifecycle(
        username=username,
        email=email,
        status=FleetAgent.Status.HIBERNATED,
        reason=reason,
    )
    return True, {
        'plane': 'runtime',
        'status': FleetAgent.Status.HIBERNATED,
        'detail': detail,
    }
