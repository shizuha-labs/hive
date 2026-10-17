"""Per-enabled-agent Grok provider/capacity Prometheus exposition."""
from __future__ import annotations

from django.conf import settings
from django.utils import timezone

from hive_platform.models import TokenEntry, TokenPool
from hive_platform.token_coordinator import quota_routing_eligible

from .models import FleetAgent


def _scopes_for(agent: FleetAgent) -> list[tuple[str, str]]:
    if agent.owner_id is not None:
        return [(TokenPool.OwnerKind.USER, str(agent.owner_id))]
    scopes = [(TokenPool.OwnerKind.ORG, TokenPool.ORG_OWNER_ID)]
    fleet_owner = str(getattr(settings, 'HIVE_FLEET_OWNER_USER_ID', '') or '').strip()
    if fleet_owner:
        scopes.insert(0, (TokenPool.OwnerKind.USER, fleet_owner))
    return scopes


def _enabled_grok_agents():
    enabled_statuses = (FleetAgent.Status.RUNNING, FleetAgent.Status.HIBERNATED)
    for agent in FleetAgent.objects.filter(
        status__in=enabled_statuses,
    ).order_by('agent_username'):
        method = str((agent.mcp_config or {}).get('execution_method') or '').strip()
        if method == 'grok_build':
            yield agent


def _has_routable_xai_capacity(agent: FleetAgent, *, now) -> bool:
    entries = (
        TokenEntry.objects
        .active_for_scopes(_scopes_for(agent), TokenPool.Provider.XAI, now=now)
        .select_related('pool')
    )
    return any(quota_routing_eligible(entry, now=now) for entry in entries)


def to_prometheus(*, now=None) -> str:
    now = now or timezone.now()
    lines = [
        '# HELP shizuha_agent_provider_healthy Model provider reachable (1) or unavailable (0).',
        '# TYPE shizuha_agent_provider_healthy gauge',
        '# HELP shizuha_agent_capacity_unavailable Provider credential/quota capacity unavailable (1).',
        '# TYPE shizuha_agent_capacity_unavailable gauge',
    ]
    for agent in _enabled_grok_agents():
        healthy = 1 if _has_routable_xai_capacity(agent, now=now) else 0
        labels = f'agent="{agent.agent_username}",backend="grok_build"'
        lines.append(f'shizuha_agent_provider_healthy{{{labels}}} {healthy}')
        lines.append(f'shizuha_agent_capacity_unavailable{{{labels}}} {1 - healthy}')
    return '\n'.join(lines) + '\n'
