"""HIVE-2624 fail-loud signal for active-desired-but-0-ready-replicas.

``fleet.provisioner.reconcile_active_agent_readiness`` stamps one gauge per
firing agent; the existing Hive internal metrics scrape exports it and
``k8s/hive-active-readiness-alerts.yaml`` routes ``== 1`` to the DevOps owner
group via the standard Alertmanager→Connect DM lane, with the workload
identity on the series (bounded fleet set — identity-on-series per the
HIVE-615 v9 §5.3 house rules; free text never a label beyond the agent slug).

Gauge semantics (not a counter): 1 while the state persists for that agent,
0 once the sweep observes recovery — resolution drives the same series to 0
in place, so the alert auto-resolves and no stale entry survives a heal.
"""
from django.core.cache import cache

_TTL = 7 * 24 * 60 * 60
_KEY = 'hive:hive2624:active-desired-zero-ready'


def record_active_zero_ready(firing: dict[str, int]) -> None:
    """Replace the firing set with the sweep's current observation.

    ``firing`` maps agent_username -> 1 for agents currently in the
    active-desired-zero-ready state past the bounded window. Agents seen
    firing previously but absent now are driven back to 0 (recovery).
    """
    try:
        previous = cache.get(_KEY) or {}
        if not isinstance(previous, dict):
            previous = {}
        merged = {str(k): 1 for k in firing}
        for agent, value in previous.items():
            if str(agent) not in merged and int(value or 0) == 1:
                merged[str(agent)] = 0  # recovery: same series to 0 in place
        cache.set(_KEY, merged, _TTL)
    except Exception:  # noqa: BLE001 — metrics must never break the sweep
        pass


def snapshot() -> dict[str, int]:
    try:
        values = cache.get(_KEY) or {}
        if not isinstance(values, dict):
            return {}
        return {str(k): int(v or 0) for k, v in values.items()}
    except Exception:  # noqa: BLE001
        return {}


def to_prometheus() -> str:
    values = snapshot()
    lines = [
        '# HELP hive_fleet_active_desired_zero_ready Agent Deployment is '
        'desired-active (spec.replicas>=1) with 0 ready replicas beyond the '
        'bounded window (HIVE-2624).',
        '# TYPE hive_fleet_active_desired_zero_ready gauge',
    ]
    for agent in sorted(values):
        lines.append(
            'hive_fleet_active_desired_zero_ready'
            f'{{agent="{agent}"}} {values[agent]}'
        )
    if not values:
        # Always emit the series family so absence-of-state is distinguishable
        # from absence-of-scrape (the PLAT-9118 absent() lesson).
        lines.append('hive_fleet_active_desired_zero_ready 0')
    return '\n'.join(lines) + '\n'
