"""Exercise the cache tests' real worker/reset lifecycle across test cases."""
import threading
import unittest
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from fleet import agent_index_cache as aic
from fleet import test_plat1063_agent_index_cache as cache_tests


@override_settings(CACHES=cache_tests._LOCMEM)
class AgentIndexCacheTestLifecycleTests(SimpleTestCase):
    databases = {'default'}

    def test_failed_rebuild_finishes_before_next_test_resets_cache(self):
        # The real stale-error test returns while its failing worker is still
        # logging. Its cleanup must join that worker before the real stats
        # test resets cache/counters; otherwise stats sees building, miss, hit.
        entered = threading.Event()
        release = threading.Event()
        workers = []
        states = []
        real_join = threading.Thread.join
        real_get = aic.get_or_build

        def paused_error(*args, **kwargs):
            workers.append(threading.current_thread())
            entered.set()
            release.wait(10)

        def join_worker(worker, *args, **kwargs):
            if worker.name == 'agents-index-revalidate-ext1':
                self.assertTrue(entered.wait(5), 'error worker did not start')
                release.set()
            return real_join(worker, *args, **kwargs)

        def record_state(*args, **kwargs):
            result = real_get(*args, **kwargs)
            states.append(result[1])
            # Negative control also leaves no worker behind: without fixture
            # cleanup, the first stats call returns building and fails later.
            release.set()
            for worker in workers:
                real_join(worker, 5)
            return result

        predecessor = cache_tests.AgentIndexCacheTests('test_serve_stale_on_rebuild_error')
        successor = cache_tests.AgentIndexCacheTests('test_stats_counts_and_hit_rate')
        previous_result = unittest.TestResult()
        next_result = unittest.TestResult()
        try:
            with patch.object(aic.logger, 'exception', side_effect=paused_error), \
                    patch.object(threading.Thread, 'join', join_worker):
                predecessor.run(previous_result)
                self.assertTrue(entered.wait(5), 'predecessor did not exercise its worker')
                finished_before_next_test = all(not worker.is_alive() for worker in workers)
                with patch.object(aic, 'get_or_build', side_effect=record_state):
                    successor.run(next_result)
            self.assertTrue(previous_result.wasSuccessful(),
                            str(previous_result.errors + previous_result.failures))
            self.assertTrue(next_result.wasSuccessful(),
                            str(next_result.errors + next_result.failures))
            self.assertTrue(finished_before_next_test, 'predecessor cleanup left a live worker')
            self.assertEqual(states, ['miss', 'hit', 'hit'])
            self.assertTrue(all(not worker.is_alive() for worker in workers))
        finally:
            release.set()
            for worker in workers:
                real_join(worker, 5)

    def test_slow_builder_released_before_cleanup_after_assertion_failure(self):
        case = cache_tests.AgentIndexCacheTests('test_cold_miss_slow_builder_does_not_block')
        real_add_cleanup = case.addCleanup
        real_join_builders = case._join_revalidators
        release_events = []
        cleanup_observed = []

        def remember_cleanup(callback, *args, **kwargs):
            event = getattr(callback, '__self__', None)
            if isinstance(event, threading.Event):
                release_events.append(event)
            real_add_cleanup(callback, *args, **kwargs)

        def join_builders():
            if release_events:
                cleanup_observed.append(True)
                self.assertTrue(release_events[0].is_set(),
                                'failed assertion must release builder before joining it')
            real_join_builders()

        result = unittest.TestResult()
        with patch.object(case, 'addCleanup', side_effect=remember_cleanup), \
                patch.object(case, '_join_revalidators', side_effect=join_builders), \
                patch.object(case, 'assertLess', side_effect=AssertionError('intentional early failure')):
            case.run(result)
        self.assertEqual(len(result.errors), 0, str(result.errors))
        self.assertEqual(len(result.failures), 1, str(result.failures))
        self.assertIn('intentional early failure', result.failures[0][1])
        self.assertEqual(len(release_events), 1)
        self.assertEqual(cleanup_observed, [True])
        with aic._inflight_lock:
            self.assertNotIn('slow', aic._inflight)
