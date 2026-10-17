"""PLAT-8834: seat-lifecycle transition telemetry for the Hive fleet.

PLAT-8788 verdict context: the mio→saki→kai seat-loss sequence was invisible
until a lane-health stall classification landed (≥24h lag by design). The
lease-lease map (``GET /internal/v1/fleet/lifecycle-availability``) is
stateless — no per-seat timestamps, no transition events — so seat-loss
alerting depended on an external snapshot-diff (``fleet_seat_watch.sh``,
30m cadence). This module makes transitions first-class telemetry so the
second-loss threshold pages directly.

- ``hive_seat_lifecycle_transitions_total{seat, from_status, to_status}`` —
  monotonic counter, incremented on every lifecycle transition (serve,
  hibernate, stop, provisioning).
- ``hive_seat_last_transition_unixtime{seat}`` — gauge for staleness checks.

Exposed at ``GET /hive/api/v1/internal/fleet/seat-lifecycle-metrics``
(Prometheus text). The Team-Outage alert wires the rate:
``increase(hive_seat_lifecycle_transitions_total{to_status="hibernated"}[24h]) >= 2``
(see ``k8s/hive-seat-lifecycle-alerts.yaml``). The external snapshot-diff
retires once this is live.

Cache-backed (same mechanism as HIVE-2011 ``idle_recycle_metrics`` and
HIVE-408 ``usage_metering_metrics``) so it works across the gunicorn worker
set without a shared in-process registry. One counter key per
``(seat, from_status, to_status)`` tuple plus a registry key enumerating the
live tuples so the metrics endpoint never scans the whole cache. Seat
cardinality is fleet-sized (~tens), so the registry stays small.

Every write is best-effort and never raises: telemetry must not be able to
break a lifecycle transition (a failed hibernate is worse than a missed
counter increment).
"""
import time

from django.core.cache import cache

_TTL = 7 * 24 * 60 * 60  # counters are cache-backed; refreshed on every write
_KEY = 'hive:plat8834:seat_lifecycle'
_REGISTRY_KEY = f'{_KEY}:tuples'


def _safe(fn, *args):
    """Run a cache op, degrading to no-op on any backend error."""
    try:
        fn(*args)
    except Exception:
        pass


def record_transition(seat: str, from_status: str, to_status: str) -> None:
    """Record ONE seat lifecycle transition (best-effort, never raises).

    Called from the authoritative transition points in
    ``fleet.provisioner`` (native start / scale-to-zero) and
    ``fleet.lifecycle_fleet`` (rt-fleet roster writes) after the status
    change has been persisted — the counter reflects what actually happened,
    not what was attempted.
    """
    seat = str(seat or 'unknown')
    from_status = str(from_status or 'unknown')
    to_status = str(to_status or 'unknown')
    if from_status == to_status:
        return
    tuple_key = f'{_KEY}:t:{seat}:{from_status}:{to_status}'
    now = int(time.time())

    def _write():
        cache.add(tuple_key, 0, _TTL)
        cache.incr(tuple_key)
        cache.add(f'{_KEY}:last:{seat}', 0, _TTL)
        cache.set(f'{_KEY}:last:{seat}', now, _TTL)
        known = cache.get(_REGISTRY_KEY)
        entry = f'{seat}|{from_status}|{to_status}'
        if not known:
            cache.set(_REGISTRY_KEY, entry, _TTL)
        elif entry not in known.split(','):
            cache.set(_REGISTRY_KEY, f'{known},{entry}', _TTL)

    _safe(_write)


def to_prometheus() -> str:
    """Render the Prometheus text exposition (never raises)."""
    lines = ['# HELP hive_seat_lifecycle_transitions_total Seat lifecycle transitions.',
             '# TYPE hive_seat_lifecycle_transitions_total counter',
             '# HELP hive_seat_last_transition_unixtime Unix time of the last seat lifecycle transition.',
             '# TYPE hive_seat_last_transition_unixtime gauge']
    try:
        registry = cache.get(_REGISTRY_KEY) or ''
        for entry in registry.split(','):
            if not entry:
                continue
            seat, from_status, to_status = (entry.split('|') + ['?', '?', '?'])[:3]
            value = cache.get(f'{_KEY}:t:{seat}:{from_status}:{to_status}')
            if value is None:
                continue
            lines.append(
                f'hive_seat_lifecycle_transitions_total{{seat="{seat}",'
                f'from_status="{from_status}",to_status="{to_status}"}} {value}'
            )
        # Last-transition gauges: enumerate from the registry tuples (a seat
        # that never transitioned since deploy has no gauge — absent() on the
        # gauge is acceptable; the counter is the alerting substrate).
        seen = set()
        for entry in registry.split(','):
            if not entry:
                continue
            seat = entry.split('|')[0]
            if seat in seen:
                continue
            seen.add(seat)
            last = cache.get(f'{_KEY}:last:{seat}')
            if last:
                lines.append(f'hive_seat_last_transition_unixtime{{seat="{seat}"}} {last}')
    except Exception:
        pass
    return '\n'.join(lines) + '\n'
