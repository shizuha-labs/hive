"""HIVE-408: fail-loud observability for the HIVE-345/HIVE-319 usage-metering
producer paths (PLAT-1254 Fail-Loud Mechanisms standard).

Billing/payments metering must not fail silently. Every usage-event record that
fails to persist is a **missed (or at-risk) billing event** — previously that was
only ``logger.exception``'d and surfaced to the end user as ``upstream_unavailable``,
with no metric or alert to a person, so it would go undetected until reconciliation.

- **Invariant (stated):** recording a billable usage event is atomic — it either
  persists or the producing request fails closed (the HIVE-345 rollback tests cover
  the atomicity half). This module adds the observability half: **a metering-record
  failure is observable.**
- **Failure path → whose DM (not log-only):** every failure in
  ``usage_metering.record_usage_event`` (the single chokepoint for all producer
  paths — runtime turns, Cortex token attribution, storage/uptime) increments
  ``hive_usage_metering_failure_total{source=…}``. The
  ``k8s/hive-usage-metering-alerts.yaml`` PrometheusRule fires on any increase and
  routes to the owning team via Alertmanager→Connect DM, carrying the failing
  producer source. Metric is exposed at
  ``GET /hive/api/v1/internal/usage-metering/metrics`` (Prometheus text).
- **Probe:** the counter is always emitted (at 0 when healthy) for each known
  producer source, so ``increase()`` works from zero and ``absent()`` catches a
  dead exporter.

Cache-backed (same mechanism as HIVE-360 ``connect_delivery_metrics``) so it works
across the gunicorn worker set without a shared in-process registry.
"""
import time

from django.core.cache import cache

_TTL = 7 * 24 * 60 * 60  # counters are cache-backed; refreshed on every write
_KEY = 'hive:hive408:usage_metering'

# Producer sources (mirror claws.models.UsageEvent.Source values) that can fail to
# record a billable usage event. Bounded label cardinality; anything else -> unknown.
SOURCES = ('runtime', 'cortex', 'storage', 'unknown')
OUTBOX_OUTCOMES = ('delivered', 'retry', 'conflict')


def _incr(key):
    try:
        cache.add(key, 0, _TTL)
        cache.incr(key)
    except Exception:
        try:
            cache.set(key, (cache.get(key) or 0) + 1, _TTL)
        except Exception:
            pass


def _normalise_source(source) -> str:
    src = str(source or '').strip().lower()
    if src in SOURCES:
        return src
    # CloudEvents sources are URI-references (for example
    # ``urn:shizuha:cortex:worker``). Preserve a bounded, useful producer label.
    for candidate in SOURCES:
        if candidate == 'unknown':
            continue
        if f':{candidate}:' in src or src.endswith(f':{candidate}'):
            return candidate
    return 'unknown'


def record_failure(source=''):
    """Record ONE usage-metering-record failure (a missed/at-risk billing event).

    Best-effort: never raises — the caller re-raises the concrete metering error and
    must fail closed regardless of whether this observability write succeeds.
    """
    _incr(f'{_KEY}:failure:{_normalise_source(source)}')


def snapshot() -> dict:
    return {s: int(cache.get(f'{_KEY}:failure:{s}') or 0) for s in SOURCES}


def record_outbox_delivery(outcome: str, source=''):
    """Record one terminal delivery attempt outcome using bounded labels."""
    normalized_outcome = outcome if outcome in OUTBOX_OUTCOMES else 'retry'
    _incr(f'{_KEY}:outbox:{normalized_outcome}:{_normalise_source(source)}')


def record_outbox_dispatch_heartbeat():
    """Prove that the scheduled dispatcher completed a sweep, even if empty."""
    try:
        cache.set(f'{_KEY}:outbox:last_dispatch', int(time.time()), _TTL)
    except Exception:
        pass


def outbox_snapshot() -> dict:
    return {
        'outcomes': {
            outcome: {
                source: int(cache.get(f'{_KEY}:outbox:{outcome}:{source}') or 0)
                for source in SOURCES
            }
            for outcome in OUTBOX_OUTCOMES
        },
        'last_dispatch': int(cache.get(f'{_KEY}:outbox:last_dispatch') or 0),
    }


def to_prometheus() -> str:
    snap = snapshot()
    outbox = outbox_snapshot()
    lines = [
        '# HELP hive_usage_metering_failure_total Usage-metering record failures — '
        'missed/at-risk billing events by producer source (HIVE-408/PLAT-1254; '
        'fail-loud — alerted).',
        '# TYPE hive_usage_metering_failure_total counter',
    ]
    # Always emit every source (0 when healthy) so increase()/absent() behave.
    for s in SOURCES:
        lines.append(f'hive_usage_metering_failure_total{{source="{s}"}} {snap[s]}')
    lines.extend([
        '# HELP hive_usage_outbox_delivery_total Metering Core producer-outbox delivery attempts by outcome and producer source.',
        '# TYPE hive_usage_outbox_delivery_total counter',
    ])
    for outcome in OUTBOX_OUTCOMES:
        for source in SOURCES:
            lines.append(
                'hive_usage_outbox_delivery_total'
                f'{{outcome="{outcome}",source="{source}"}} '
                f'{outbox["outcomes"][outcome][source]}'
            )
    lines.extend([
        '# HELP hive_usage_outbox_last_dispatch_timestamp_seconds Unix timestamp of the last completed usage-outbox sweep.',
        '# TYPE hive_usage_outbox_last_dispatch_timestamp_seconds gauge',
        f'hive_usage_outbox_last_dispatch_timestamp_seconds {outbox["last_dispatch"]}',
    ])
    return '\n'.join(lines) + '\n'
