"""Canonical live activity phases for fleet agents.

SCLI / bridges report a coarse ``activity.state`` (idle|working) for existing
lease and grace consumers, plus a fine ``activity.phase`` the Agents card
animates: thinking / responding / tool / idle.

This module is the Hive-side normalizer. It never invents work — missing
telemetry stays missing.
"""
from __future__ import annotations

from typing import Any

CANONICAL_PHASES = ('idle', 'thinking', 'responding', 'tool')
PRODUCTIVE_PHASES = frozenset({
    'thinking',
    'responding',
    'tool',
    'working',
    'busy',
    'running_turn',
    'running-tool',
    'running_tool',
})
IDLE_PHASES = frozenset({'idle', 'sleeping'})
PHASE_ALIASES = {
    'waiting_first_model_event': 'thinking',
    'running': 'working',
    'running-tool': 'tool',
    'running_tool': 'tool',
    'busy': 'working',
    'running_turn': 'working',
}
LEASE_IDLE_WASTE_MIN_MS = 30_000


def _norm(value: Any) -> str:
    return str(value or '').strip().lower().replace(' ', '_')


def normalize_activity_phase(value: Any) -> str | None:
    raw = _norm(value)
    if not raw:
        return None
    raw = PHASE_ALIASES.get(raw, raw)
    if raw in CANONICAL_PHASES or raw == 'working':
        return raw
    if raw in PRODUCTIVE_PHASES:
        return 'working'
    if raw in IDLE_PHASES:
        return 'idle'
    return None


def resolve_activity_phase(agent: dict[str, Any] | None) -> str | None:
    """Prefer the fine phase; fall back to the coarse state."""
    if not agent:
        return None
    phase = normalize_activity_phase(agent.get('activity_phase'))
    if phase in CANONICAL_PHASES:
        return phase
    state = normalize_activity_phase(agent.get('activity_state') or agent.get('activityState'))
    if state in CANONICAL_PHASES:
        return state
    if state == 'working' or state in PRODUCTIVE_PHASES:
        tool = agent.get('activity_tool_name')
        if tool:
            return 'tool'
        return 'working'
    if state in IDLE_PHASES:
        return 'idle'
    return state


def is_productive_activity(agent: dict[str, Any] | None) -> bool:
    phase = resolve_activity_phase(agent)
    return phase in PRODUCTIVE_PHASES or phase in {'thinking', 'responding', 'tool', 'working'}


def _phase_idle_ms(agent: dict[str, Any]) -> float | None:
    raw = agent.get('activity_phase_for_ms')
    try:
        if raw is not None:
            return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    raw_age = agent.get('last_activity_ms_ago')
    try:
        if raw_age is not None:
            return max(0.0, float(raw_age))
    except (TypeError, ValueError):
        pass
    return None


def is_lease_idle_waste(
    agent: dict[str, Any] | None,
    *,
    min_idle_ms: int = LEASE_IDLE_WASTE_MIN_MS,
) -> bool:
    """A scarce model lease held by a running agent that is sitting idle."""
    if not agent:
        return False
    lease = agent.get('model_lease') or {}
    if lease.get('state') != 'holder':
        return False
    if str(agent.get('status') or '').strip().lower() != 'running':
        return False
    if resolve_activity_phase(agent) != 'idle':
        return False
    idle_ms = _phase_idle_ms(agent)
    if idle_ms is None:
        return False
    return idle_ms >= min_idle_ms


def annotate_activity(agent: dict[str, Any]) -> dict[str, Any]:
    """Stamp normalized phase fields onto a fleet-card dict (in place)."""
    phase = resolve_activity_phase(agent)
    if phase:
        agent['activity_phase'] = phase
    idle_ms = _phase_idle_ms(agent)
    if idle_ms is not None and agent.get('activity_phase_for_ms') is None:
        agent['activity_phase_for_ms'] = int(idle_ms)
    agent['lease_idle_waste'] = is_lease_idle_waste(agent)
    return agent
