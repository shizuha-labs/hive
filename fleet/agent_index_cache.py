"""PLAT-1063: bounded-TTL read-model cache for the /v1/fleet/agents hot path.

``FleetAgentListView.get()`` fans out *serially* to >=5 remote/expensive sources
on every load — the live rt-fleet runtime list, external/BYO agents, per-agent
bridge telemetry, HIVE-175 output-rates, and the Pulse team-roster round-trip.
Summed serially that is the user-reported 5-10s. The page is operational and is
typically refreshed/polled repeatedly, so the same expensive payload is rebuilt
over and over.

This module caches the fully-assembled agents-index payload so common *warm*
loads serve from cache (target p95 < 1s) instead of re-aggregating:

  * FRESH window (``FRESH_TTL``, default 8s): serve the cached payload directly.
  * miss / expiry: the caller rebuilds synchronously, we store, and serve.
  * serve-stale-on-error: if a rebuild raises (an upstream is slow/down) we serve
    the last good payload when we still have one, so a single flaky dependency
    never blanks the whole page.
  * event-driven invalidation: :func:`invalidate` bumps a version counter so the
    next load rebuilds. Lifecycle/admin mutations (enable/disable/restart/
    provision/switch-backend/claim/delete) call it, so a command's result is
    never hidden behind a stale cache.

The cache is strictly an optimization, never a hard dependency: every cache
operation is guarded, so if Redis is unreachable the path degrades to today's
live-aggregate behaviour (just not cached).
"""
from __future__ import annotations

import logging
import threading
import time

from django.core.cache import cache

logger = logging.getLogger(__name__)

# Freshness window for a warm hit. Short enough that status/telemetry stays
# near-live; long enough to collapse a burst of refreshes/polls into one rebuild.
FRESH_TTL = 8
# Last-good payload retention. Served instantly on any miss (stale-while-
# revalidate) so the page NEVER blocks on the multi-second fan-out once a
# payload has been built; kept long so an idle period can't cold-start the page.
LAST_GOOD_TTL = 24 * 3600
# Cold start: wait this long for the background rebuild, then return empty
# with state ``building`` rather than occupying Daphne's thread-sensitive
# pool for the full fan-out (that path timed out kube probes).
COLD_WAIT_S = 1.5

_VER_KEY = 'fleet:agents_index:ver'
_OBSERVATION_INVALIDATE_LOCK_KEY = 'fleet:agents_index:observation-invalidate-lock'
_FRESH_PREFIX = 'fleet:agents_index:fresh'
_LASTGOOD_PREFIX = 'fleet:agents_index:lastgood'

# Process-local metrics (best-effort; for a single-pod backend this is the whole
# picture, for HA it is per-pod). Surfaced via :func:`stats`.
_metrics_lock = threading.Lock()
_metrics = {
    'hits': 0,
    'misses': 0,
    'stale_served': 0,
    'bg_rebuilds': 0,
    'bypass': 0,
    'building': 0,
    'invalidations': 0,
    'coalesced_invalidations': 0,
    'build_ms_last': 0.0,
}


def _bump(metric, value=1):
    with _metrics_lock:
        _metrics[metric] = _metrics.get(metric, 0) + value


def _set(metric, value):
    with _metrics_lock:
        _metrics[metric] = value


def _current_version():
    try:
        ver = cache.get(_VER_KEY)
        if ver is None:
            # Seed lazily so every pod agrees on a starting version.
            cache.add(_VER_KEY, 0, None)
            ver = cache.get(_VER_KEY) or 0
        return int(ver)
    except Exception:
        # Redis down / cache error -> a stable sentinel; get_or_build will simply
        # miss and rebuild live every time (degraded, but correct).
        return -1


def invalidate(reason=''):
    """Bump the version so the next load rebuilds. Safe to call from any mutation;
    never raises."""
    try:
        try:
            cache.incr(_VER_KEY)
        except ValueError:
            # Key missing/expired -> (re)seed past 0 so a concurrently-cached
            # v0 payload is no longer matched.
            cache.set(_VER_KEY, 1, None)
        _bump('invalidations')
        logger.info('agents-index cache invalidated (%s)', reason or 'unspecified')
    except Exception:
        logger.exception('agents-index cache invalidate failed (non-fatal)')


def invalidate_observation(reason=''):
    """Coalesce high-volume runtime observations into one version bump.

    A daemon reconnect sends one snapshot frame per child, followed by a burst
    of deltas.  Invalidating the shared generation for every frame makes a
    single reconnect create dozens of obsolete cache generations and can keep
    the expensive fleet-index revalidator permanently behind.  Redis ``add``
    gives all Hive replicas one short, atomic event window; operator mutations
    continue to call :func:`invalidate` directly and remain immediate.

    Cache failure must never hide an observation, so failure to acquire/read the
    coalescing key falls back to an ordinary invalidation.
    """
    try:
        acquired = cache.add(_OBSERVATION_INVALIDATE_LOCK_KEY, 1, timeout=1)
    except Exception:
        invalidate(reason)
        return
    if not acquired:
        _bump('coalesced_invalidations')
        return
    invalidate(reason)


# Single-flight registry for background revalidations: cache_suffix → started
# monotonic time. Guarded by _metrics_lock's sibling below; entries older than
# _REVALIDATE_STUCK_S are considered dead (thread crashed) and may be replaced.
_inflight_lock = threading.Lock()
_inflight: dict = {}
_REVALIDATE_STUCK_S = 120


def _store_payload(fresh_key, lastgood_key, payload):
    try:
        cache.set(fresh_key, payload, FRESH_TTL)
        cache.set(lastgood_key, payload, LAST_GOOD_TTL)
    except Exception:
        logger.exception('agents-index cache store failed (non-fatal)')


def _spawn_revalidate(cache_suffix, fresh_key, lastgood_key, builder):
    """Rebuild the index in a background thread (single-flight per suffix) so
    the serving request returns the last-good payload instantly. The game-server
    model: requests read precomputed state; aggregation happens off-path."""
    now = time.monotonic()
    with _inflight_lock:
        started = _inflight.get(cache_suffix)
        if started is not None and (now - started) < _REVALIDATE_STUCK_S:
            return  # a rebuild is already running for this variant
        _inflight[cache_suffix] = now

    def _run():
        from django.db import close_old_connections
        t0 = time.monotonic()
        try:
            payload = builder()
            _store_payload(fresh_key, lastgood_key, payload)
            dt_ms = (time.monotonic() - t0) * 1000.0
            _set('build_ms_last', round(dt_ms, 1))
            _bump('bg_rebuilds')
            logger.info('agents-index revalidated in %.0fms (background, %d agents)',
                        dt_ms, len(payload or []))
        except Exception:
            logger.exception('agents-index background revalidate failed (last-good keeps serving)')
        finally:
            with _inflight_lock:
                _inflight.pop(cache_suffix, None)
            try:
                close_old_connections()
            except Exception:
                pass

    threading.Thread(target=_run, name=f'agents-index-revalidate-{cache_suffix}', daemon=True).start()


def get_or_build(cache_suffix, builder):
    """Return ``(payload, state)`` for the agents-index identified by
    ``cache_suffix`` (the request variant key, e.g. ``ext1``).

    ``builder`` is a zero-arg callable that performs the expensive live
    aggregation and returns the payload (a list of agent dicts). ``state`` is one
    of ``hit`` | ``miss`` | ``stale-revalidating`` | ``stale`` for observability.

    Serving contract (stale-while-revalidate): once ANY payload has ever been
    built for this variant, requests are answered from cache immediately —
    a version bump/expiry only schedules a background rebuild, it never makes a
    user request run the multi-second fan-out inline.
    """
    ver = _current_version()
    fresh_key = f'{_FRESH_PREFIX}:v{ver}:{cache_suffix}'
    lastgood_key = f'{_LASTGOOD_PREFIX}:{cache_suffix}'

    if ver >= 0:
        try:
            cached = cache.get(fresh_key)
        except Exception:
            cached = None
        if cached is not None:
            _bump('hits')
            return cached, 'hit'

    # Miss: serve last-good instantly and revalidate off-path.
    try:
        stale = cache.get(lastgood_key)
    except Exception:
        stale = None
    if stale is not None:
        _bump('stale_served')
        if ver >= 0:
            _spawn_revalidate(cache_suffix, fresh_key, lastgood_key, builder)
        return stale, 'stale-revalidating'

    # True cold start: never run the multi-second fan-out on the request
    # thread. Daphne's thread-sensitive pool is shared with /api/health/;
    # an inline rebuild is how GET /fleet/agents/ made Hive intermittently
    # unreachable (operator 2026-09-04).
    _bump('misses')
    if ver >= 0:
        _spawn_revalidate(cache_suffix, fresh_key, lastgood_key, builder)
        deadline = time.monotonic() + COLD_WAIT_S
        while time.monotonic() < deadline:
            try:
                payload = cache.get(fresh_key) or cache.get(lastgood_key)
            except Exception:
                payload = None
            if payload is not None:
                return payload, 'miss'
            time.sleep(0.05)
        _bump('building')
        return [], 'building'
    t0 = time.monotonic()
    payload = builder()
    dt_ms = (time.monotonic() - t0) * 1000.0
    _set('build_ms_last', round(dt_ms, 1))
    logger.info('agents-index rebuilt in %.0fms (cache unavailable, %d agents)', dt_ms, len(payload or []))
    return payload, 'miss'


def note_bypass():
    _bump('bypass')


def stats():
    """Snapshot of cache counters + a derived hit-rate, for metrics/logging."""
    with _metrics_lock:
        snap = dict(_metrics)
    served = snap['hits'] + snap['misses']
    snap['hit_rate'] = round(snap['hits'] / served, 3) if served else 0.0
    snap['fresh_ttl_s'] = FRESH_TTL
    snap['last_good_ttl_s'] = LAST_GOOD_TTL
    return snap
