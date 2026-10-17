"""HIVE-2011: fail-loud observability for the Claw idle-recycle worker.

ADR-0080 root cause: 15 idle Running OpenClaw tenants sat for months because
scale-to-zero silently failed — ``idle_recycle_after`` was stamped at
provision/start but no worker consumed it, and when a stop did fail there was
no metric or alert. HIVE-2007 wired the worker into
``finalize_claw_deletions``; this module makes the outcome observable so a
silent failure cannot recur.

- **Invariant (stated):** an idle Running Claw is scaled to zero by the
  scheduled reconcile, or the failure is observable. ``hive_claw_idle_running``
  is a per-claw gauge (1 = Running past its idle TTL and still not stopped
  after the reconcile attempt); ``hive_claw_idle_stop_failure_total`` counts
  scale-to-zero failures; ``hive_claw_idle_stopped_total`` counts successes.
- **Failure path → who sees it:** the gauge drives the ``HiveClawIdleRunning``
  PrometheusRule (stale Running = the exact ADR-0080 failure mode), and the
  failure counter drives ``HiveClawIdleStopFailed``. Both route to the owning
  team via Alertmanager→Connect DM. Metric is exposed at
  ``GET /hive/api/v1/internal/claw-idle/metrics`` (Prometheus text).
- **Probe:** the gauge is always emitted (0 when healthy) so ``absent()``
  catches a dead exporter/reconcile.

Cache-backed (same mechanism as HIVE-408 ``usage_metering_metrics``) so it
works across the gunicorn worker set without a shared in-process registry. The
gauge set is stored as ONE cache key (a dict) so the metrics endpoint never has
to enumerate keys — the reconcile worker rewrites the whole set each run.
"""
import time

from django.core.cache import cache

_TTL = 7 * 24 * 60 * 60  # counters are cache-backed; refreshed on every write
_KEY = 'hive:hive2011:claw_idle'
_GAUGE_KEY = f'{_KEY}:running_set'


def _incr(key):
    try:
        cache.add(key, 0, _TTL)
        cache.incr(key)
    except Exception:
        try:
            cache.set(key, (cache.get(key) or 0) + 1, _TTL)
        except Exception:
            pass


def record_stopped():
    """Record ONE successful idle scale-to-zero (best-effort, never raises)."""
    _incr(f'{_KEY}:stopped')


def record_stop_failure():
    """Record ONE idle scale-to-zero failure (best-effort, never raises)."""
    _incr(f'{_KEY}:stop_failure')


def set_idle_running_set(entries):
    """Replace the stale-Running gauge set.

    ``entries`` is an iterable of ``(claw_id, namespace, value)`` — one row per
    Running claw past its idle TTL that remains Running after the reconcile
    attempt (value 1), plus a ``("", "", 0)`` sentinel when the set is empty so
    the zero series stays present. Best-effort: never raises.
    """
    try:
        cache.set(_GAUGE_KEY, list(entries), _TTL)
    except Exception:
        pass


def to_prometheus() -> str:
    try:
        entries = cache.get(_GAUGE_KEY) or []
    except Exception:
        entries = []
    stopped = int(cache.get(f'{_KEY}:stopped') or 0)
    failures = int(cache.get(f'{_KEY}:stop_failure') or 0)
    lines = [
        '# HELP hive_claw_idle_running Running Claw past its idle TTL not stopped '
        'after the reconcile attempt (ADR-0080 stale-Running guard).',
        '# TYPE hive_claw_idle_running gauge',
    ]
    if not entries:
        entries = [('', '', 0)]
    for claw_id, namespace, value in entries:
        lines.append(
            'hive_claw_idle_running'
            f'{{claw_id="{claw_id}",namespace="{namespace}"}} {int(bool(value))}'
        )
    lines.extend([
        '# HELP hive_claw_idle_stopped_total Idle Claws scaled to zero by the '
        'scheduled reconcile (HIVE-2011).',
        '# TYPE hive_claw_idle_stopped_total counter',
        f'hive_claw_idle_stopped_total {stopped}',
        '# HELP hive_claw_idle_stop_failure_total Idle Claw scale-to-zero failures '
        '(HIVE-2011 — must not fail silently).',
        '# TYPE hive_claw_idle_stop_failure_total counter',
        f'hive_claw_idle_stop_failure_total {failures}',
    ])
    return '\n'.join(lines) + '\n'
