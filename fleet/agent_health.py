"""Shared agent-health enrichment (PLAT-5265).

These helpers used to live in ``fleet/views.py``, which meant the
provider-unavailable signals they produce reached the API/UI read paths and
nowhere else. The on-demand reconciler decides capacity from
``list_lifecycle_agents()``, never goes through a view, and therefore saw agent
dicts carrying none of the health keys — so a capacity predicate reading them
degenerated to "is the process running", and a provider-dead seat still counted
as usable capacity (PLAT-5265; found in review by reika).

They live here so the read path and the decision path apply the SAME enrichment
and cannot drift apart again.
"""

from __future__ import annotations

import time


def _provider_key(*values):
    text = ' '.join(str(v or '').lower() for v in values)
    if 'anthropic' in text or 'claude' in text or 'sonnet' in text or 'opus' in text:
        return 'anthropic'
    if 'openai' in text or 'codex' in text or 'gpt' in text or text.startswith('o1') or text.startswith('o3') or text.startswith('o4'):
        return 'openai'
    return ''


def agent_provider_key(agent):
    return _provider_key(
        agent.get('model_provider'),
        agent.get('runtime_provider'),
        agent.get('runtime_harness'),
        agent.get('execution_method'),
        agent.get('effective_model'),
        agent.get('model'),
    )


def _snap_provider_key(snap):
    runtime = (snap or {}).get('runtime') or {}
    return _provider_key(runtime.get('provider'), runtime.get('harness'), runtime.get('model'))


def _telemetry_ts_ms(value):
    if value is None:
        return None
    try:
        num = float(value)
        return int(num if num > 1_000_000_000_000 else num * 1000)
    except (TypeError, ValueError):
        return None


def provider_unavailable_from_telemetry(telemetry, now_ms=None):
    """Providers with a RECENT pool/provider-unavailable report.

    2026-07-02: age-gate the poison. A bridge that hits a token limit reports
    token_pool_unavailable once and then typically goes idle (its turn just
    errored), so its poisoned snapshot lingers in Connect's telemetry store
    indefinitely — painting every idle same-provider agent "Unavailable" for
    hours after the pool recovered (operator-visible ghost badges). Honor the
    poison only while the snapshot is fresh, symmetric with the 10-minute
    window _has_recent_explicit_healthy_telemetry uses for the healthy side.
    """
    providers = set()
    now_ms = now_ms or int(time.time() * 1000)
    max_age_ms = 10 * 60 * 1000
    for snap in (telemetry or {}).values():
        health = (snap or {}).get('health') or {}
        if health.get('token_pool_unavailable') is True or health.get('provider_unavailable') is True:
            ts = _telemetry_ts_ms((snap or {}).get('ts') or (snap or {}).get('received_at'))
            if ts is None or not (0 <= (now_ms - ts) <= max_age_ms):
                continue
            provider = _snap_provider_key(snap)
            if provider:
                providers.add(provider)
    return providers


def has_recent_explicit_healthy_telemetry(agent, now_ms=None):
    if agent.get('health_ok') is not True or agent.get('health_token_pool_unavailable') is True:
        return False
    ts = _telemetry_ts_ms(agent.get('telemetry_ts'))
    if ts is None:
        return False
    now_ms = now_ms or int(time.time() * 1000)
    # Operator 2026-09-23 (Hina false "Unavailable"): the poison window must
    # exceed the agent's own idle heartbeat cadence. Voice/CEO-Office seats
    # tick every 30m, so a 10-minute window chronically paints healthy idle
    # agents with another bridge's provider outage. Explicit-healthy telemetry
    # is trusted up to 35 minutes (30m cadence + jitter); agents WITHOUT an
    # explicit-healthy stamp still fall through to the poison paint.
    return 0 <= (now_ms - ts) <= 35 * 60 * 1000


def apply_provider_unavailable_health(agents, telemetry):
    """Propagate shared provider outage signals to same-provider agents.

    Some agents can be running but unable to get a model token, and some may miss
    fresh per-agent telemetry while blocked before the bridge fully starts. When
    any bridge reports a shared provider token pool unavailable, same-provider
    agents without fresh explicit healthy telemetry must render unavailable
    instead of green/alive.
    """
    unavailable = provider_unavailable_from_telemetry(telemetry)
    if not unavailable:
        return agents
    now_ms = int(time.time() * 1000)
    for agent in agents:
        if agent_provider_key(agent) not in unavailable:
            continue
        if has_recent_explicit_healthy_telemetry(agent, now_ms=now_ms):
            continue
        agent['health_ok'] = False
        agent['health_token_pool_unavailable'] = True
        agent['provider_unavailable'] = True
        agent['availability_status'] = 'unavailable'
    return agents
