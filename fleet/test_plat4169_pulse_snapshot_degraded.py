"""PLAT-4169 — "roster degraded" must mean the revalidate FAILED, not idle-stale.

The pulse-snapshot read-model is demand-driven stale-while-revalidate. After the
page sits unvisited > DEGRADED_AGE_S, the first visitor is served an old last-good
while a healthy revalidate runs off-path. The old code flagged that first hit as
`pulse_degraded` purely on age, so a healthy-but-idle cache rendered an amber
"roster degraded" banner that read like an outage. The fix: track whether the
background revalidate is actually failing and only treat the cache as degraded
when it is.

Run with a deterministic local-memory cache so no live Redis is needed; the
production backend is Redis but the cache *contract* is backend-agnostic.
View-level behaviour is validated live on the running stack per the team policy.
"""
import time

from django.test import TestCase, override_settings
from django.core.cache import cache

from fleet import read_model_cache as rmc


_LOCMEM = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'plat4169-test',
    }
}


@override_settings(CACHES=_LOCMEM)
class RevalidateFailingSignalTests(TestCase):
    def setUp(self):
        cache.clear()

    def _wait_revalidate_done(self, key, timeout=3.0):
        """Block until the background revalidate thread for `key` has finished."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with rmc._inflight_lock:
                still_running = key in rmc._inflight
            if not still_running:
                return
            time.sleep(0.02)
        self.fail(f'revalidate for {key} did not finish within {timeout}s')

    def test_marker_helpers_roundtrip(self):
        self.assertFalse(rmc.revalidate_failing('k'))
        rmc._mark_revalidate_failure('k')
        self.assertTrue(rmc.revalidate_failing('k'))
        rmc._clear_revalidate_failure('k')
        self.assertFalse(rmc.revalidate_failing('k'))

    def test_idle_stale_first_hit_is_not_degraded(self):
        """The core bug: an OLD last-good with no failed revalidate is NOT
        degraded. Serving stale + spawning a healthy revalidate must not flag it."""
        key = 'pulse-snapshot:global'
        # Seed a last-good far older than DEGRADED_AGE_S, no fresh entry.
        old = {'payload': {'nodes': [1, 2]}, 'built_at': time.time() - 4 * rmc.DEGRADED_AGE_S}
        cache.set(f'rmc:lastgood:{key}', old, rmc.DEFAULT_LAST_GOOD_TTL)

        payload, state, age = rmc.get_or_revalidate(key, lambda: {'nodes': [3]}, fresh_ttl=10)
        self.assertEqual(state, 'stale-revalidating')
        self.assertGreater(age, rmc.DEGRADED_AGE_S)
        # At the instant of the first hit the healthy revalidate has been spawned;
        # nothing has failed, so the cache is NOT degraded.
        self.assertFalse(rmc.revalidate_failing(key))
        self._wait_revalidate_done(key)
        # The healthy revalidate succeeded -> still not degraded, now fresh.
        self.assertFalse(rmc.revalidate_failing(key))
        fresh, st2, _ = rmc.get_or_revalidate(key, lambda: {'nodes': [3]}, fresh_ttl=10)
        self.assertEqual(st2, 'hit')
        self.assertEqual(fresh, {'nodes': [3]})

    def test_failing_revalidate_marks_degraded_then_recovers(self):
        key = 'pulse-snapshot:global'
        old = {'payload': {'nodes': [1]}, 'built_at': time.time() - 4 * rmc.DEGRADED_AGE_S}
        cache.set(f'rmc:lastgood:{key}', old, rmc.DEFAULT_LAST_GOOD_TTL)

        def boom():
            raise RuntimeError('pulse down')

        payload, state, age = rmc.get_or_revalidate(key, boom, fresh_ttl=10)
        self.assertEqual(state, 'stale-revalidating')  # still serves last-good
        self.assertEqual(payload, {'nodes': [1]})
        self._wait_revalidate_done(key)
        # The revalidate raised -> now genuinely degraded.
        self.assertTrue(rmc.revalidate_failing(key))

        # A later successful revalidate clears the degraded marker.
        # last-good is stale again so this re-spawns the background revalidate.
        cache.delete(f'rmc:fresh:{key}')
        rmc.get_or_revalidate(key, lambda: {'nodes': [9]}, fresh_ttl=10)
        self._wait_revalidate_done(key)
        self.assertFalse(rmc.revalidate_failing(key))

    def test_failing_marker_survives_but_read_failure_is_safe(self):
        # revalidate_failing must fail safe to False if the store is unreadable.
        key = 'pulse-snapshot:global'
        rmc._mark_revalidate_failure(key)
        self.assertTrue(rmc.revalidate_failing(key))
        cache.clear()
        self.assertFalse(rmc.revalidate_failing(key))
