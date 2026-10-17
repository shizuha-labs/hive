"""Small, safe read projection for the Agents grid.

The full fleet agent serializer is also the edit-drawer contract.  Returning
that shape for the polling grid ships prompts, runtime config, environment
overrides, and other drawer-only fields every 15 seconds.  Keep the full detail
contract intact and derive a deliberately smaller card projection instead.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_DETAIL_ONLY_FIELDS = frozenset({
    'context_prompt',
    'credential_grant_scopes',
    'eager_skills',
    'enabled_mcp_server_ids',
    'env',
    'identity_health',
    'mcp_config',
    'mcp_servers',
    'model_fallbacks',
    'model_overrides',
    'node_selector_overrides',
    'personality_traits',
    'recent_errors',
    'resource_overrides',
    'resource_profile_spec',
    'routing_health',
    'sidecar_set',
    'skill_config',
    'skills',
    'tolerations',
})

_CARD_CONTEXT_BUDGET_FIELDS = (
    'context_window_tokens',
    'total_tokens',
    'headroom_tokens',
    'utilization_percent',
    'outlier',
    'severity',
    'measured_at',
)


def _card_context_budget(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return value
    compact = {key: value.get(key) for key in _CARD_CONTEXT_BUDGET_FIELDS}
    compact['components'] = [
        {
            'component': component.get('component'),
            'tokens': component.get('tokens'),
        }
        for component in (value.get('components') or [])
        if isinstance(component, Mapping)
    ]
    return compact


def project_agent_card(agent: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy containing grid/card state but no edit-only detail."""
    projected = {
        key: value
        for key, value in agent.items()
        if key not in _DETAIL_ONLY_FIELDS
    }
    if 'context_budget' in projected:
        projected['context_budget'] = _card_context_budget(projected['context_budget'])
    return projected


def project_agent_cards(agents) -> list[dict[str, Any]]:
    return [project_agent_card(agent) for agent in agents]
