"""Cache access contracts for controllers that keep durable fleet state.

Hive's default Redis cache deliberately uses a 250ms connect/read timeout and
does not enable redis-py's global ``retry_on_timeout``.  That keeps request
paths bounded, but it also means a single transient socket timeout can abort a
scheduled controller unless the controller states its own cache contract.

Use the ``required_*`` helpers for authoritative controller state: retry the
individual idempotent cache operation a small, bounded number of times and
then fail loudly.  Use the ``optional_*`` helpers for acceleration-only cache
entries: a cache backend failure is a miss and the authoritative source is
consulted immediately.
"""
from __future__ import annotations

import logging
import time

from django.core.cache import cache
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

REQUIRED_CACHE_ATTEMPTS = 3
REQUIRED_CACHE_RETRY_DELAY = 0.1


def _required_call(method: str, *args, **kwargs):
    """Run one idempotent authoritative cache operation with bounded retry."""
    operation = getattr(cache, method)
    for attempt in range(1, REQUIRED_CACHE_ATTEMPTS + 1):
        try:
            return operation(*args, **kwargs)
        except RedisError as exc:
            logger.warning(
                'required cache %s failed (attempt %s/%s): %s',
                method,
                attempt,
                REQUIRED_CACHE_ATTEMPTS,
                exc,
            )
            if attempt == REQUIRED_CACHE_ATTEMPTS:
                raise
            time.sleep(REQUIRED_CACHE_RETRY_DELAY * attempt)


def required_get(key, default=None):
    return _required_call('get', key, default)


def required_set(key, value, timeout=None):
    return _required_call('set', key, value, timeout)


def required_add(key, value, timeout=None):
    return _required_call('add', key, value, timeout)


def required_delete(key):
    return _required_call('delete', key)


def optional_get(key, default=None):
    """Return a cache miss immediately when an acceleration cache is down."""
    try:
        return cache.get(key, default)
    except RedisError as exc:
        logger.info('optional cache get bypassed for %s: %s', key, exc)
        return default


def optional_set(key, value, timeout=None):
    """Best-effort store for acceleration-only data; never fail its caller."""
    try:
        return cache.set(key, value, timeout)
    except RedisError as exc:
        logger.info('optional cache set bypassed for %s: %s', key, exc)
        return False
