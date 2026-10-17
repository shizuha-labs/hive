"""PLAT-9052: deferred connect-wakes must be scrapeable and clear promptly.

Same two properties HIVE-615 v9 §5.3 pinned for the harness failure fact,
because the failure mode is the same:

  * emitted ALWAYS, 0 when nothing is deferred -- an absent-when-healthy
    metric makes "nothing is wrong" and "the exporter is broken" identical;
  * the ALERTING series carry NO labels. With email on the gauge, a resolved
    deferral does not drive its series to 0 -- it stops being emitted, and
    Prometheus serves the last value until staleness (~5m) while the healthy
    sample is a DIFFERENT series. The page would outlive the wake.

Identity lives on the companion info series. The starvation clock starts at
the FIRST defer: a retry must not reset the age a paging rule reads.
"""

from __future__ import annotations

from django.core.cache import cache
from django.test import SimpleTestCase

from fleet import wake_defers as wd


class WakeDeferMetricTests(SimpleTestCase):
    def setUp(self):
        cache.delete(wd.WAKE_DEFERRED_KEY)

    tearDown = setUp

    def _series(self, out):
        return [l for l in out.splitlines() if l and not l.startswith('#')]

    def test_healthy_emits_explicit_unlabeled_zeros(self):
        out = wd.wake_defers_to_prometheus()
        self.assertIn('hive_wake_deferred_total 0', out)
        self.assertIn('hive_wake_deferred_outstanding 0', out)
        self.assertIn('hive_wake_deferred_age_seconds 0', out)

    def test_the_alerting_series_carry_NO_labels(self):
        """The whole point: same series transitions 1 -> 0 in place."""
        for phase in ('healthy', 'starving'):
            with self.subTest(phase=phase):
                if phase == 'starving':
                    wd.record_wake_defer('agent-kai@shizuha.com', now=1000.0)
                for line in self._series(wd.wake_defers_to_prometheus(now=1000.0)):
                    if line.startswith('hive_wake_deferred_info'):
                        continue
                    self.assertNotIn('{', line,
                                     'alerting series must be unlabeled: %s' % line)

    def test_record_exposes_identity_and_age(self):
        wd.record_wake_defer('agent-kai@shizuha.com', now=1000.0)
        out = wd.wake_defers_to_prometheus(now=1300.0)
        self.assertIn('hive_wake_deferred_outstanding 1', out)
        self.assertIn('hive_wake_deferred_age_seconds 300', out)
        self.assertIn('hive_wake_deferred_info{email="agent-kai@shizuha.com"} 1', out)

    def test_redefer_does_not_reset_the_starvation_clock(self):
        wd.record_wake_defer('agent-kai@shizuha.com', now=1000.0)
        wd.record_wake_defer('agent-kai@shizuha.com', now=1900.0)
        out = wd.wake_defers_to_prometheus(now=2000.0)
        self.assertIn('hive_wake_deferred_age_seconds 1000', out)
        self.assertIn('hive_wake_deferred_outstanding 1', out)

    def test_age_reads_the_oldest_outstanding_seat(self):
        wd.record_wake_defer('agent-kai@shizuha.com', now=1000.0)
        wd.record_wake_defer('agent-mio@shizuha.com', now=1800.0)
        out = wd.wake_defers_to_prometheus(now=2000.0)
        self.assertIn('hive_wake_deferred_outstanding 2', out)
        self.assertIn('hive_wake_deferred_age_seconds 1000', out)
        self.assertIn('hive_wake_deferred_info{email="agent-kai@shizuha.com"} 1', out)
        self.assertIn('hive_wake_deferred_info{email="agent-mio@shizuha.com"} 1', out)

    def test_clearing_drives_the_SAME_series_to_zero(self):
        """Not a different label set -- otherwise the page waits for staleness."""
        wd.record_wake_defer('agent-kai@shizuha.com', now=1000.0)
        self.assertIn('hive_wake_deferred_outstanding 1',
                      wd.wake_defers_to_prometheus(now=1000.0))
        wd.clear_wake_defer('agent-kai@shizuha.com')
        out = wd.wake_defers_to_prometheus(now=1000.0)
        self.assertIn('hive_wake_deferred_outstanding 0', out)
        self.assertIn('hive_wake_deferred_age_seconds 0', out)
        self.assertNotIn('hive_wake_deferred_info{', out)

    def test_total_survives_clears(self):
        """The counter is cumulative: clears resolve deferrals, not history."""
        wd.record_wake_defer('agent-kai@shizuha.com', now=1000.0)
        wd.record_wake_defer('agent-mio@shizuha.com', now=1100.0)
        wd.clear_wake_defer('agent-kai@shizuha.com')
        wd.clear_wake_defer('agent-mio@shizuha.com')
        self.assertIn('hive_wake_deferred_total 2',
                      wd.wake_defers_to_prometheus(now=1200.0))

    def test_rotten_entries_are_pruned_not_aged_forever(self):
        """A deferral past the 24h bound is registry rot (seat woke outside
        the wake path); it must stop aging, not pin a permanent page."""
        wd.record_wake_defer('agent-kai@shizuha.com', now=0.0)
        out = wd.wake_defers_to_prometheus(now=wd.MAX_DEFER_AGE_SECONDS + 60)
        self.assertIn('hive_wake_deferred_outstanding 0', out)
        self.assertNotIn('hive_wake_deferred_info{', out)

    def test_empty_email_is_ignored(self):
        wd.record_wake_defer('', now=1000.0)
        wd.clear_wake_defer('')
        self.assertIn('hive_wake_deferred_total 0',
                      wd.wake_defers_to_prometheus(now=1000.0))
