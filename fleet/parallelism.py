"""Runtime parallelism contract for Hive fleet diagnostics.

PLAT-1109: a configured "max concurrent tasks" number is not proof that an
agent can safely run multiple Pulse tasks. Hive only reports parallel execution
as ready when the runtime provides explicit evidence for the isolation contract;
everything else fails closed to single-task.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


CONTRACT_VERSION = 'isolated-task-v1'
REQUIRED_ISOLATION_FLAGS = (
    'session',
    'workspace',
    'mcp_client',
    'credential_broker',
    'activity_log',
)


@dataclass(frozen=True)
class ParallelismReadiness:
    supported: bool
    effective_max_tasks: int
    status: str
    contract_version: str
    reason: str
    requested_max_tasks: int = 1

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _positive_int(value: Any, default: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _env_int(env: dict[str, Any], key: str, default: int = 1) -> int:
    return _positive_int(env.get(key), default)


def _runtime_evidence(agent: dict[str, Any]) -> dict[str, Any]:
    evidence = agent.get('runtimeParallelism') or agent.get('runtime_parallelism') or {}
    return evidence if isinstance(evidence, dict) else {}


def runtime_agent_parallelism(agent: dict[str, Any]) -> ParallelismReadiness:
    """Return fail-closed parallelism diagnostics for an external rt-fleet agent.

    A daemon config field (`allowParallelExecution`/`maxConcurrentTasks`) is only
    intent. The runtime must also report `runtimeParallelism` evidence with the
    PLAT-1109 isolation flags before Hive marks it parallel-ready.
    """
    requested = _positive_int(agent.get('maxConcurrentTasks') or agent.get('max_concurrent_tasks'), 1)
    if not agent.get('allowParallelExecution') and not agent.get('allow_parallel_execution'):
        return ParallelismReadiness(False, 1, 'single_task', '', 'parallel execution not enabled by runtime config', requested)

    evidence = _runtime_evidence(agent)
    if not evidence:
        return ParallelismReadiness(False, 1, 'single_task', '', 'runtime has not reported isolation evidence', requested)

    return evidence_parallelism(evidence, requested)


def native_agent_parallelism(agent: Any) -> ParallelismReadiness:
    """Return fail-closed parallelism diagnostics for a k3s-native FleetAgent.

    The current native runtime is single-task unless the agent env explicitly
    carries the PLAT-1109 contract marker. This keeps Hive honest while allowing
    a future isolated-session runner to advertise readiness without a schema
    migration.
    """
    env = getattr(agent, 'env', {}) or {}
    requested = _env_int(env, 'SHIZUHA_MAX_PARALLEL_TASKS', 1)
    if env.get('SHIZUHA_PARALLEL_EXECUTION_CONTRACT') != CONTRACT_VERSION:
        return ParallelismReadiness(False, 1, 'single_task', '', 'no PLAT-1109 isolated-task runtime contract marker', requested)
    evidence = {
        'supported': True,
        'contractVersion': CONTRACT_VERSION,
        'maxTasks': requested,
        'isolation': {key: True for key in REQUIRED_ISOLATION_FLAGS},
    }
    return evidence_parallelism(evidence, requested)


def evidence_parallelism(evidence: dict[str, Any], requested_max_tasks: int) -> ParallelismReadiness:
    contract = evidence.get('contractVersion') or evidence.get('contract_version') or ''
    if contract != CONTRACT_VERSION:
        return ParallelismReadiness(False, 1, 'single_task', str(contract), 'unsupported or missing runtime parallelism contract', requested_max_tasks)
    if not evidence.get('supported'):
        return ParallelismReadiness(False, 1, 'single_task', contract, 'runtime reports parallel execution unsupported', requested_max_tasks)

    isolation = evidence.get('isolation') or {}
    missing = [flag for flag in REQUIRED_ISOLATION_FLAGS if not isolation.get(flag)]
    if missing:
        return ParallelismReadiness(False, 1, 'single_task', contract, f'missing isolation evidence: {", ".join(missing)}', requested_max_tasks)

    runtime_max = _positive_int(evidence.get('maxTasks') or evidence.get('max_tasks'), 1)
    effective = max(1, min(requested_max_tasks, runtime_max))
    if effective <= 1:
        return ParallelismReadiness(False, 1, 'single_task', contract, 'effective runtime limit is one task', requested_max_tasks)
    return ParallelismReadiness(True, effective, 'parallel_ready', contract, 'runtime reports isolated sessions/workspaces/MCP/credentials/activity logs', requested_max_tasks)
