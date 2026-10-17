"""PLAT-8834: seat-lifecycle metrics — no-DB unit tests.

Covers the counter/gauge substrate (cache-backed, best-effort) and the
Prometheus text rendering, including the label-tuple registry that lets the
metrics endpoint enumerate series without scanning the cache.
"""
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase

from fleet import seat_lifecycle_metrics as slm


class SeatLifecycleMetricsTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_record_transition_increments_counter(self):
        slm.record_transition('mio', 'running', 'hibernated')
        slm.record_transition('mio', 'running', 'hibernated')
        slm.record_transition('mio', 'hibernated', 'running')
        text = slm.to_prometheus()
        self.assertIn(
            'hive_seat_lifecycle_transitions_total{seat="mio",'
            'from_status="running",to_status="hibernated"} 2',
            text,
        )
        self.assertIn(
            'hive_seat_lifecycle_transitions_total{seat="mio",'
            'from_status="hibernated",to_status="running"} 1',
            text,
        )

    def test_noop_transition_not_recorded(self):
        slm.record_transition('mio', 'running', 'running')
        self.assertEqual(slm.to_prometheus().count('hive_seat_lifecycle_transitions_total{'), 0)

    def test_last_transition_gauge_rendered(self):
        slm.record_transition('kai', 'running', 'hibernated')
        text = slm.to_prometheus()
        self.assertRegex(text, r'hive_seat_last_transition_unixtime\{seat="kai"\} \d+')

    def test_never_raises_on_cache_failure(self):
        with mock.patch.object(cache, 'add', side_effect=RuntimeError('backend down')), \
                mock.patch.object(cache, 'incr', side_effect=RuntimeError('backend down')), \
                mock.patch.object(cache, 'get', side_effect=RuntimeError('backend down')), \
                mock.patch.object(cache, 'set', side_effect=RuntimeError('backend down')):
            slm.record_transition('mio', 'running', 'hibernated')  # must not raise
            self.assertTrue(slm.to_prometheus().endswith('\n'))  # must not raise

    def test_render_never_raises_on_corrupt_registry(self):
        cache.set(slm._REGISTRY_KEY, 'bad-entry-without-pipes', 60)
        text = slm.to_prometheus()
        self.assertIn('# TYPE hive_seat_lifecycle_transitions_total counter', text)
