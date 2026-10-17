"""Context-budget estimation for fleet agents.

This module deliberately emits counts and metadata only.  It never returns raw
prompt, skill, env, or memory content; HIVE-159 requires a security-sensitive
visibility surface, not a prompt exfiltration endpoint.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from django.conf import settings


# Baseline constants come from the HIVE-159 feature spec and the companion
# context-injection taxonomy page.  They are estimates until the agent runtime
# can report turn-start tokenizer measurements directly.
AGENTS_MD_ESTIMATE = 2130
RUNTIME_WRAPPER_ESTIMATE = 2600
EAGER_MCP_SCHEMA_ESTIMATE = 2500
SKILL_BODY_ESTIMATE = 1250
SKILL_MENU_ITEM_ESTIMATE = 18
DEFAULT_CONTEXT_WINDOW = 128_000
WARNING_UTILIZATION = 0.50
CRITICAL_UTILIZATION = 0.80
WARNING_ABSOLUTE_TOKENS = 100_000


@dataclass(frozen=True)
class TokenEstimate:
    tokens: int
    status: str


def _safe_json(value: Any) -> str:
    if value in (None, '', [], {}):
        return ''
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), default=str)
    except TypeError:
        return str(value)


def estimate_text_tokens(value: Any, model: str = '') -> TokenEstimate:
    """Return a token count using a model tokenizer when available.

    ``tiktoken`` is optional in Hive.  If it is absent or does not know the
    model, fall back to the standard chars/4 estimator and mark the row as
    estimated so the UI/API remains honest about confidence.
    """
    if value in (None, ''):
        return TokenEstimate(0, 'exact')
    text = value if isinstance(value, str) else _safe_json(value)
    if not text:
        return TokenEstimate(0, 'exact')

    try:
        import tiktoken  # type: ignore

        try:
            enc = tiktoken.encoding_for_model(model) if model else tiktoken.get_encoding('cl100k_base')
        except Exception:
            enc = tiktoken.get_encoding('cl100k_base')
        return TokenEstimate(len(enc.encode(text)), 'exact')
    except Exception:
        return TokenEstimate(max(1, math.ceil(len(text) / 4)), 'estimated')


def _agent_get(agent: Any, key: str, default: Any = '') -> Any:
    if isinstance(agent, dict):
        return agent.get(key, default)
    return getattr(agent, key, default)


def _model(agent: Any) -> str:
    return _agent_get(agent, 'effective_model') or _agent_get(agent, 'model') or ''


def _agent_name(agent: Any) -> str:
    return _agent_get(agent, 'agent_username') or _agent_get(agent, 'display_name') or str(_agent_get(agent, 'id', 'unknown'))


def _context_window_for_model(model: str) -> int:
    configured = getattr(settings, 'HIVE_CONTEXT_WINDOW_OVERRIDES', {}) or {}
    if model in configured:
        return int(configured[model])

    name = (model or '').lower()
    if 'gpt-4.1' in name:
        return 1_047_576
    # Live DeepSeek-V4-Flash (deepseek-v4-flash-i6) serves max_model_len=524288.
    # Do not fall through to the 128K generic default — that misled the Agents UI
    # and made operators think compaction was gated by Cortex at 128K.
    if 'deepseek-v4-flash' in name or name.endswith('deepseek-v4-flash'):
        return 524_288
    # Claude Opus 4.6+ and Sonnet 4.x unlock 1M context (long-context beta / Claude Code
    # 1M variant); Opus 5 / Sonnet 5 / Fable 5 are 1M by default. Matches the shizuha
    # runtime model-profile — haiku and older/unversioned claude stay 200K.
    if any(m in name for m in (
        'opus-4-6', 'opus-4-7', 'opus-4-8', 'opus-4-9',
        'opus-4.6', 'opus-4.7', 'opus-4.8', 'opus-4.9',
        'opus-5', 'sonnet-5', 'sonnet-4', 'fable-5',
    )):
        return 1_000_000
    if name.startswith(('claude', 'opus', 'sonnet', 'haiku')):
        return 200_000
    if name.startswith(('o3', 'o4', 'o1')) or 'gpt-4o' in name:
        return 200_000
    if 'grok-4.7' in name or 'grok-4.6' in name or 'grok-4.5' in name:
        return 500_000
    if 'grok-4' in name:
        return 256_000
    # Codex 0.157.1 catalog max for gpt-6-astra/sol/luna. Measured telemetry
    # still wins over this name, so a runtime still reporting 272k stays 272k.
    if 'gpt-6-astra' in name or 'gpt-6-sol' in name or 'gpt-6-luna' in name:
        return 872_000
    # Live GLM-5.3-Flash EXL3 TP4 serves max_model_len=500000. The 128k
    # generic default made the Agents drawer show ~7k / 5% while telemetry
    # already had 350k / 70% (Kei 2026-09-13).
    if 'glm-5.3' in name or 'glm53' in name:
        return 500_000
    return int(getattr(settings, 'HIVE_DEFAULT_CONTEXT_WINDOW_TOKENS', DEFAULT_CONTEXT_WINDOW))


def _skills(agent: Any) -> list[Any]:
    skill_config = _agent_get(agent, 'skill_config', {}) or {}
    if isinstance(skill_config, dict):
        raw = skill_config.get('skills') or skill_config.get('starred') or skill_config.get('critical') or []
    else:
        raw = skill_config
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, Iterable):
        return list(raw)
    return []


def _skill_tokens(agent: Any) -> int:
    skills = _skills(agent)
    if not skills:
        return 0
    # The runtime inlines starred/critical skill bodies.  We usually only have
    # skill names in Hive, so include both a small menu/name budget and a body
    # budget per loaded skill.  This is intentionally labelled estimated.
    return len(skills) * (SKILL_BODY_ESTIMATE + SKILL_MENU_ITEM_ESTIMATE)


def _component(name: str, tokens: int, source: str, status: str, description: str = '') -> dict[str, Any]:
    return {
        'component': name,
        'tokens': max(0, int(tokens)),
        'source': source,
        'status': status,
        'description': description,
    }


def _optional_nonneg_int(value: Any) -> int | None:
    if value in (None, ''):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def context_budget_for_agent(agent: Any) -> dict[str, Any]:
    """Build the HIVE-159 context-budget payload for a model or dict agent."""
    model = _model(agent)
    context_window = _context_window_for_model(model)
    measured_max = _optional_nonneg_int(_agent_get(agent, 'context_max_tokens', None))
    if measured_max:
        # Live serving window (telemetry) wins over the model-name map so the
        # Agents UI cannot show 128k headroom on a 500k GLM resident.
        context_window = measured_max
    measured_at = datetime.now(timezone.utc).isoformat()

    config_payload = {
        'display_name': _agent_get(agent, 'display_name'),
        'role': _agent_get(agent, 'role'),
        'model': _agent_get(agent, 'model'),
        'execution_method': _agent_get(agent, 'execution_method'),
        'reasoning_effort': _agent_get(agent, 'reasoning_effort'),
        'context_prompt': _agent_get(agent, 'context_prompt'),
        'personality_traits': _agent_get(agent, 'personality_traits', {}),
        'model_fallbacks': _agent_get(agent, 'model_fallbacks', []),
        'model_overrides': _agent_get(agent, 'model_overrides', {}),
    }
    config_estimate = estimate_text_tokens(config_payload, model)

    mcp_config = _agent_get(agent, 'mcp_config', {}) or {}
    mcp_estimate = estimate_text_tokens(mcp_config, model)
    # Claude Code (the rt-fleet bridge) DEFERS MCP tool schemas by default (tst mode):
    # full schemas are NOT in turn-1 context — only deferred tool names/capability phrases
    # for ToolSearch are. So this is a deferred-NAME budget, not eager full schemas.
    eager_mcp_tokens = EAGER_MCP_SCHEMA_ESTIMATE if _agent_get(agent, 'runtime_source') == 'rt-fleet' else 0

    static_components = [
        _component('agents_md', AGENTS_MD_ESTIMATE, 'AGENTS.md/CLAUDE.md', 'estimated', 'CLI-loaded base instructions'),
        _component('agent_config', config_estimate.tokens, 'FleetAgent fields', config_estimate.status, 'Model, role, prompt, personality, fallbacks'),
        _component('skills', _skill_tokens(agent), 'skill_config', 'estimated', 'Loaded/starred skill menu and inlined bodies'),
        _component('mcp_tools', mcp_estimate.tokens + eager_mcp_tokens, 'mcp_config/runtime schemas', 'estimated' if eager_mcp_tokens else mcp_estimate.status, 'Eager MCP tool schema budget'),
        _component('runtime_wrapper', RUNTIME_WRAPPER_ESTIMATE, 'bridge identity/runtime wrapper', 'estimated', 'Bridge identity, policy, and wrapper scaffolding'),
    ]
    static_total = sum(item['tokens'] for item in static_components)
    measured_used = _optional_nonneg_int(_agent_get(agent, 'context_used_tokens', None))
    if measured_used is None:
        history = _component(
            'conversation_history', 0, 'runtime conversation state', 'deferred',
            'Measured at turn-start by runtime when available',
        )
    else:
        history = _component(
            'conversation_history',
            max(0, measured_used - static_total),
            'runtime telemetry context.used_tokens',
            'measured',
            'Live prompt tokens minus estimated static prefix',
        )
    components = static_components + [
        _component('memory', 0, 'runtime memory', 'deferred', 'Not exposed by Hive list API'),
        history,
    ]

    total = sum(item['tokens'] for item in components)
    headroom = max(context_window - total, 0)
    utilization = (total / context_window * 100) if context_window else 0

    warning_threshold = float(getattr(settings, 'HIVE_CONTEXT_BUDGET_WARNING_UTILIZATION', WARNING_UTILIZATION))
    critical_threshold = float(getattr(settings, 'HIVE_CONTEXT_BUDGET_CRITICAL_UTILIZATION', CRITICAL_UTILIZATION))
    absolute_threshold = int(getattr(settings, 'HIVE_CONTEXT_BUDGET_WARNING_TOKENS', WARNING_ABSOLUTE_TOKENS))
    if utilization >= critical_threshold * 100:
        severity = 'critical'
    elif utilization >= warning_threshold * 100 or total >= absolute_threshold:
        severity = 'warning'
    else:
        severity = 'ok'

    synthetic = components + [
        _component('total', total, 'computed', 'derived', 'Sum of non-derived components'),
        _component('headroom', headroom, 'computed', 'derived', 'Context window minus total tokens'),
    ]

    return {
        'agent': _agent_name(agent),
        'agent_id': str(_agent_get(agent, 'id', '')),
        'model': model or 'runtime default',
        'context_window_tokens': context_window,
        'total_tokens': total,
        'headroom_tokens': headroom,
        'utilization_percent': round(utilization, 2),
        'outlier': severity != 'ok',
        'severity': severity,
        'thresholds': {
            'warning_utilization_percent': warning_threshold * 100,
            'critical_utilization_percent': critical_threshold * 100,
            'warning_tokens': absolute_threshold,
        },
        'measured_at': measured_at,
        'components': synthetic,
    }


def attach_context_budget(agent: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(agent)
    enriched['context_budget'] = context_budget_for_agent(enriched)
    return enriched


def _prom_escape(value: Any) -> str:
    return str(value or '').replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')


def budgets_to_prometheus(budgets: Iterable[dict[str, Any]]) -> str:
    """Render Prometheus exposition for shizuha_agent_context_tokens."""
    lines = [
        '# HELP shizuha_agent_context_tokens Estimated context tokens loaded for each fleet agent component.',
        '# TYPE shizuha_agent_context_tokens gauge',
    ]
    for budget in budgets:
        agent = _prom_escape(budget.get('agent'))
        model = _prom_escape(budget.get('model'))
        for comp in budget.get('components') or []:
            labels = {
                'agent': agent,
                'component': _prom_escape(comp.get('component')),
                'source': _prom_escape(comp.get('source')),
                'status': _prom_escape(comp.get('status')),
                'model': model,
            }
            label_text = ','.join(f'{key}="{value}"' for key, value in labels.items())
            lines.append(f'shizuha_agent_context_tokens{{{label_text}}} {int(comp.get("tokens") or 0)}')
    return '\n'.join(lines) + '\n'
