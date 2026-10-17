"""PLAT-4683 fail-loud signal for native pre-wake runtime reconciliation.

Every failed read/patch aborts before scale in ``fleet.provisioner`` and records
exactly one counter increment here. The existing Hive internal metrics scrape
exports this counter; ``k8s/hive-runtime-contract-alerts.yaml`` turns any
increase into the standard Alertmanager→Connect DM for the DevOps owner group.
"""
from django.core.cache import cache

_TTL = 7 * 24 * 60 * 60
_KEY = 'hive:plat4683:runtime-contract-reconcile-failed'
FAILURE_STAGES = ('read_deployment', 'patch_deployment', 'unknown')


def _stage(value: str) -> str:
    return value if value in FAILURE_STAGES else 'unknown'


def record_failure(stage: str) -> None:
    """Record one failed reconcile attempt without raising into lifecycle code."""
    key = f'{_KEY}:{_stage(stage)}'
    try:
        cache.add(key, 0, _TTL)
        cache.incr(key)
    except Exception:
        try:
            cache.set(key, int(cache.get(key) or 0) + 1, _TTL)
        except Exception:
            pass


def snapshot() -> dict[str, int]:
    return {
        stage: int(cache.get(f'{_KEY}:{stage}') or 0)
        for stage in FAILURE_STAGES
    }


def to_prometheus() -> str:
    values = snapshot()
    lines = [
        '# HELP hive_fleet_runtime_contract_reconcile_failed_total Native agent '
        'pre-wake runtime contract reconciliation failures (PLAT-4683).',
        '# TYPE hive_fleet_runtime_contract_reconcile_failed_total counter',
    ]
    for stage in FAILURE_STAGES:
        lines.append(
            'hive_fleet_runtime_contract_reconcile_failed_total'
            f'{{stage="{stage}"}} {values[stage]}'
        )
    return '\n'.join(lines) + '\n'
