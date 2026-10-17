"""Generic stale-while-revalidate read-model cache for fleet hot paths.

Same serving contract as ``agent_index_cache`` (PLAT-1063) but reusable for any
JSON-serializable payload keyed by a caller-chosen cache key:

  * FRESH window: serve the cached payload directly (Redis, shared by all pods).
  * expiry: serve the LAST-GOOD payload instantly and rebuild in a background
    single-flight thread — a user request never runs the expensive upstream
    fan-out inline once any payload has ever been built.
  * true cold start: build inline (first request after a very long idle).

Every cache operation is guarded; if Redis is unreachable the caller degrades to
live building — the cache is an optimization, never a hard dependency.
"""
from __future__ import annotations

import logging
import threading
import time

from django.core.cache import cache

logger = logging.getLogger(__name__)

DEFAULT_FRESH_TTL = 10
DEFAULT_LAST_GOOD_TTL = 24 * 3600
# If the last-good payload is older than this, background rebuilds are failing;
# callers may want to annotate the response as degraded.
DEGRADED_AGE_S = 300

_inflight_lock = threading.Lock()
_inflight: dict = {}
_REVALIDATE_STUCK_S = 120
# PLAT-4169: a background revalidate that raised leaves this marker until the
# next successful revalidate clears it. `revalidate_failing(key)` reads it so a
# caller can tell "stale because the fetcher is BROKEN" (genuinely degraded)
# apart from "stale because nobody visited in a while" (healthy — do not alarm).
# TTL bounds a marker whose pod died mid-failure so it cannot wedge "degraded"
# forever with no fetcher left to clear it.
_REVALIDATE_FAIL_TTL = 24 * 3600


def _revalidate_fail_key(key):
    return f'rmc:revalfail:{key}'


def _mark_revalidate_failure(key):
    try:
        cache.set(_revalidate_fail_key(key), {'failed_at': time.time()}, _REVALIDATE_FAIL_TTL)
    except Exception:
        logger.debug('read-model %s failure-marker set failed (non-fatal)', key)


def _clear_revalidate_failure(key):
    try:
        cache.delete(_revalidate_fail_key(key))
    except Exception:
        logger.debug('read-model %s failure-marker clear failed (non-fatal)', key)


def revalidate_failing(key):
    """True iff the most recent background revalidate for ``key`` raised and no
    later revalidate has since succeeded to clear it. This is the signal for
    "genuinely degraded" — an old last-good payload combined with a fetcher that
    cannot refresh it — as opposed to a simply idle-stale-but-healthy cache.
    Fails safe to ``False`` (not degraded) if the marker store is unreadable."""
    try:
        return cache.get(_revalidate_fail_key(key)) is not None
    except Exception:
        return False


def _store(key, payload, fresh_ttl, last_good_ttl):
    record = {'payload': payload, 'built_at': time.time()}
    try:
        cache.set(f'rmc:fresh:{key}', record, fresh_ttl)
        cache.set(f'rmc:lastgood:{key}', record, last_good_ttl)
    except Exception:
        logger.exception('read-model cache store failed for %s (non-fatal)', key)


def _spawn_revalidate(key, builder, fresh_ttl, last_good_ttl):
    now = time.monotonic()
    with _inflight_lock:
        started = _inflight.get(key)
        if started is not None and (now - started) < _REVALIDATE_STUCK_S:
            return
        _inflight[key] = now

    def _run():
        from django.db import close_old_connections
        t0 = time.monotonic()
        try:
            payload = builder()
            _store(key, payload, fresh_ttl, last_good_ttl)
            _clear_revalidate_failure(key)  # PLAT-4169: fetcher is healthy again
            logger.info('read-model %s revalidated in %.0fms (background)',
                        key, (time.monotonic() - t0) * 1000.0)
        except Exception:
            _mark_revalidate_failure(key)  # PLAT-4169: fetcher broken -> degraded
            logger.exception('read-model %s background revalidate failed (last-good keeps serving)', key)
        finally:
            with _inflight_lock:
                _inflight.pop(key, None)
            try:
                close_old_connections()
            except Exception:
                pass

    threading.Thread(target=_run, name=f'rmc-revalidate-{key}', daemon=True).start()


def get_or_revalidate(key, builder, fresh_ttl=DEFAULT_FRESH_TTL,
                      last_good_ttl=DEFAULT_LAST_GOOD_TTL):
    """Return ``(payload, state, age_seconds)``.

    ``state`` is ``hit`` | ``stale-revalidating`` | ``miss``. On a true cold
    start with a failing builder, the builder's exception propagates so the
    caller can keep its existing live/error semantics.
    """
    try:
        record = cache.get(f'rmc:fresh:{key}')
    except Exception:
        record = None
    if record is not None:
        return record['payload'], 'hit', time.time() - record.get('built_at', 0)

    try:
        stale = cache.get(f'rmc:lastgood:{key}')
    except Exception:
        stale = None
    if stale is not None:
        _spawn_revalidate(key, builder, fresh_ttl, last_good_ttl)
        return stale['payload'], 'stale-revalidating', time.time() - stale.get('built_at', 0)

    payload = builder()
    _store(key, payload, fresh_ttl, last_good_ttl)
    return payload, 'miss', 0.0
