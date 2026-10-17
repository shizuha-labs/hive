"""PLAT-1063 — bounded-TTL agents-index read-model cache.

Covers the cache contract that backs the /v1/fleet/agents hot-path optimization:
  - warm hit serves the cached payload without rebuilding (the perf win);
  - invalidate() forces the next load to rebuild (command results never hidden);
  - serve-stale-on-error returns the last-good payload when a rebuild raises, so
    one flaky upstream never blanks the page;
  - the cached payload is returned intact (no duplicated/dropped rows);
  - stats() reports hit/miss/stale/invalidation counters + hit-rate.

Run with a deterministic local-memory cache so the suite needs no live Redis;
the production backend is Redis (settings.CACHES) but the cache *contract* is
backend-agnostic. View-level behaviour is validated live on the running stack
per the team live-verification policy.
"""
import threading
import time
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.core.cache import cache

from fleet import agent_index_cache as aic


_LOCMEM = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'plat1063-test',
    }
}


@override_settings(CACHES=_LOCMEM)
class AgentIndexCacheTests(TestCase):
    def setUp(self):
        # A worker from the preceding test can still own the single-flight
        # entry after its response returned. Finish it before clearing shared
        # state, and finish this test's workers before the next test starts.
        self._join_revalidators()
        self.addCleanup(self._join_revalidators)
        cache.clear()
        # Reset process-local metrics so assertions are independent of order.
        with aic._metrics_lock:
            for k in aic._metrics:
                aic._metrics[k] = 0 if k != 'build_ms_last' else 0.0

    def _join_revalidators(self):
        deadline = time.monotonic() + 10
        for worker in threading.enumerate():
            if worker.name.startswith('agents-index-revalidate-'):
                worker.join(max(0, deadline - time.monotonic()))
                self.assertFalse(worker.is_alive(),
                                 f'cache revalidator did not finish: {worker.name}')

    def _counting_builder(self, payload):
        """Return (builder, calls) where calls[0] increments on each build."""
        calls = [0]

        def build():
            calls[0] += 1
            return payload
        return build, calls

    def test_warm_hit_does_not_rebuild(self):
        payload = [{'agent_username': 'a'}, {'agent_username': 'b'}]
        build, calls = self._counting_builder(payload)

        out1, st1 = aic.get_or_build('ext1', build)
        # HIVE-2515: the cold-start rebuild runs on a background thread; under
        # CI load it can exceed COLD_WAIT_S, in which case the first call
        # returns ('building', []) and the payload assertion below would
        # flake. The contract here is about the cold/warm split, not thread
        # scheduling: accept either cold-start outcome, then pin the warm
        # read after the revalidator has finished.
        self.assertIn(st1, ('miss', 'building'), 'first load must be a cold start')
        if st1 == 'miss':
            self.assertEqual(out1, payload)
        self._join_revalidators()

        out2, st2 = aic.get_or_build('ext1', build)

        self.assertEqual(out2, payload)
        self.assertEqual(st2, 'hit')
        self.assertEqual(calls[0], 1, 'second load must be served from cache, not rebuilt')

    def test_distinct_suffix_is_a_separate_entry(self):
        build1, c1 = self._counting_builder([{'agent_username': 'a'}])
        build0, c0 = self._counting_builder([])
        aic.get_or_build('ext1', build1)
        # different variant key (include_external=false) must not hit ext1's entry
        _, st = aic.get_or_build('ext0', build0)
        self.assertEqual(st, 'miss')
        self.assertEqual(c0[0], 1)

    def _wait_for(self, predicate, timeout=5.0):
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_invalidate_serves_stale_and_rebuilds_in_background(self):
        # Stale-while-revalidate contract (operator 2026-07-10 instant-page
        # directive): after invalidate() the NEXT load is answered instantly
        # from last-good while a single-flight background thread rebuilds; the
        # request never runs the fan-out inline once a payload exists.
        payload = [{'agent_username': 'a'}]
        build, calls = self._counting_builder(payload)
        aic.get_or_build('ext1', build)          # cold miss -> build #1 (inline)
        aic.get_or_build('ext1', build)          # hit
        aic.invalidate('status change')
        out, st = aic.get_or_build('ext1', build)
        self.assertEqual(st, 'stale-revalidating')
        self.assertEqual(out, payload, 'must serve last-good instantly on invalidation')
        self.assertTrue(self._wait_for(lambda: calls[0] == 2),
                        'invalidate() must trigger exactly one background rebuild')
        # After the background rebuild lands, the same version serves fresh hits.
        self.assertTrue(self._wait_for(
            lambda: aic.get_or_build('ext1', build)[1] == 'hit'))
        self.assertEqual(calls[0], 2)

    def test_runtime_observation_burst_bumps_generation_once(self):
        aic.invalidate_observation('daemon-link snapshot')
        version = aic._current_version()

        for _ in range(50):
            aic.invalidate_observation('daemon-link snapshot')

        self.assertEqual(aic._current_version(), version)
        self.assertEqual(aic.stats()['invalidations'], 1)
        self.assertEqual(aic.stats()['coalesced_invalidations'], 50)

        # A later observation window must remain event-driven.  Removing the
        # lock models its one-second expiry without making the test sleep.
        cache.delete(aic._OBSERVATION_INVALIDATE_LOCK_KEY)
        aic.invalidate_observation('daemon-link delta')
        self.assertEqual(aic._current_version(), version + 1)

    def test_runtime_observation_cache_failure_falls_back_to_invalidate(self):
        with patch.object(aic.cache, 'add', side_effect=RuntimeError('redis down')), \
                patch.object(aic, 'invalidate') as invalidate:
            aic.invalidate_observation('daemon-link delta')

        invalidate.assert_called_once_with('daemon-link delta')

    def test_serve_stale_on_rebuild_error(self):
        good = [{'agent_username': 'a'}]
        build_ok, _ = self._counting_builder(good)
        aic.get_or_build('ext1', build_ok)       # primes last-good
        aic.invalidate('force next rebuild')

        def boom():
            raise RuntimeError('upstream telemetry timed out')

        out, st = aic.get_or_build('ext1', boom)
        self.assertEqual(st, 'stale-revalidating')
        self.assertEqual(out, good, 'must serve the last-good payload, not blank the page')

    def test_error_without_last_good_does_not_block_the_request(self):
        # Cold miss + failing builder must not raise on the request thread
        # (that occupied Daphne and timed out kube probes). The caller gets
        # an empty ``building`` payload; the error stays in the background.
        def boom():
            raise RuntimeError('cold cache + upstream down')
        out, st = aic.get_or_build('ext1', boom)
        self.assertEqual(st, 'building')
        self.assertEqual(out, [])

    def test_cold_miss_slow_builder_does_not_block(self):
        release = threading.Event()
        # Cleanups run in reverse order: release first even if an assertion
        # fails, then the setUp cleanup joins the actual background worker.
        self.addCleanup(release.set)

        def build():
            release.wait(timeout=8)
            return [{'agent_username': 'a'}]

        t0 = time.monotonic()
        out, st = aic.get_or_build('slow', build)
        self.assertLess(time.monotonic() - t0, 2.5)
        self.assertEqual(st, 'building')
        self.assertEqual(out, [])
        release.set()
        self.assertTrue(self._wait_for(
            lambda: aic.get_or_build('slow', lambda: [{'agent_username': 'a'}])[1] in ('hit', 'stale-revalidating', 'miss'),
            timeout=5.0,
        ))

    def test_payload_returned_intact_no_dup_or_drop(self):
        payload = [{'agent_username': 'a'}, {'agent_username': 'b'}, {'agent_username': 'c'}]
        build, _ = self._counting_builder(payload)
        out, _ = aic.get_or_build('ext1', build)
        hit, _ = aic.get_or_build('ext1', build)
        self.assertEqual([a['agent_username'] for a in out], ['a', 'b', 'c'])
        self.assertEqual(hit, payload, 'cached read must equal the built list exactly')

    def test_stats_counts_and_hit_rate(self):
        build, _ = self._counting_builder([{'agent_username': 'a'}])
        aic.get_or_build('ext1', build)   # miss (cold start; rebuilt off-path)
        # HIVE-2515: under CI load the background revalidator can exceed
        # COLD_WAIT_S, making a warm call re-enter the cold-start path and
        # count a second miss (observed live: hits=1 != 2, run 2662 deploy
        # gate + fleet auto-rollback). Join the revalidator before the warm
        # reads so the counters measure the cache contract, not thread
        # scheduling.
        self._join_revalidators()
        aic.get_or_build('ext1', build)   # hit
        aic.get_or_build('ext1', build)   # hit
        aic.invalidate('x')
        s = aic.stats()
        self.assertEqual(s['hits'], 2)
        self.assertEqual(s['misses'], 1)
        self.assertEqual(s['invalidations'], 1)
        self.assertAlmostEqual(s['hit_rate'], round(2 / 3, 3))
        self.assertEqual(s['fresh_ttl_s'], aic.FRESH_TTL)
