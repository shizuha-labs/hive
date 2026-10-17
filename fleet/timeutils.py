"""The one place that states Hive's stored-clock convention.

Hive runs ``USE_TZ=False`` with ``TIME_ZONE='America/Chicago'``, so Django's own
helpers are a trap for lifecycle clocks:

* ``django.utils.timezone.now()`` returns a naive **TIME_ZONE wall clock**
  (Chicago), *not* UTC.
* ``datetime.fromisoformat(naive).timestamp()`` likewise interprets a naive value
  as **local**.

But ``FleetAgent.last_active_at`` (and the daemon wire contract that feeds it) is
**naive UTC**: ``fleet/on_demand.py:_within_grace_window``,
``fleet/views.py`` snapshot-age, and ``fleet/external_agents.py`` all read a naive
value by stamping ``tzinfo=utc`` onto it.

Mixing the two put values 5-6h apart into a single column. Readers then disagreed
about which agents were stale — an agent written with the local-wall-clock helper
looked six hours *older* than reality to the hibernation reconciler, which is the
"can stop an active agent" hazard already called out in ``consumers.py``. It also
made fleet-wide staleness alerting untrustworthy while the whole fleet sat dead
for 14h+ on 2026-07-25.

Use ``naive_utc_now()`` instead of ``timezone.now()`` for any clock stored in one
of these columns, and ``naive_utc(value)`` to normalize an inbound datetime.
"""
from __future__ import annotations

from datetime import datetime, timezone


def naive_utc_now() -> datetime:
    """Current UTC time as a naive datetime (the stored-clock convention)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def naive_utc(value: datetime) -> datetime:
    """Normalize ``value`` to naive UTC.

    An aware value is converted to UTC and stripped; a naive value is already
    assumed to be UTC (the convention) and returned unchanged.
    """
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value
