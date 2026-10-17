"""HIVE-615 v9 §5.3: the failure fact must be scrapeable AND clear promptly.

Two properties, and the second is why the shape changed after review:

  * emitted ALWAYS, 0 when healthy -- an absent-when-healthy metric makes
    "nothing is wrong" and "the exporter is broken" identical;
  * the ALERTING series carry NO labels. With run_id/phase on the gauge, a
    resolved failure does not drive its series to 0 -- it stops being emitted,
    and Prometheus serves the last value until staleness (~5m), while the
    healthy sample is a DIFFERENT series. The page would then outlive the
    audited clear by minutes. Identity lives on a companion info series.
"""

from __future__ import annotations

from django.core.cache import cache
from django.test import SimpleTestCase

from fleet import harness_versions as hv


class FailureMetricTests(SimpleTestCase):
    def setUp(self):
        cache.delete(hv.HARNESS_UPGRADE_FAILURE_KEY)
        cache.delete(hv.HARNESS_UPGRADE_RUN_KEY)

    tearDown = setUp

    def _fail(self):
        run = hv.new_run('img:new', run_id='r-42', lease_token='t', last_good='img:old')
        hv.execute_rollback(run, apply=lambda _i: (_ for _ in ()).throw(RuntimeError('boom')))

    def _series(self, out):
        return [l for l in out.splitlines() if l and not l.startswith('#')]

    def test_healthy_emits_explicit_unlabeled_zeros(self):
        out = hv.harness_upgrade_failure_to_prometheus()
        self.assertIn('hive_harness_upgrade_failure 0', out)
        self.assertIn('hive_harness_upgrade_failed_at 0', out)

    def test_the_alerting_series_carry_NO_labels(self):
        """The whole point: same series transitions 1 -> 0 in place."""
        for phase in ('healthy', 'failing'):
            with self.subTest(phase=phase):
                if phase == 'failing':
                    self._fail()
                for line in self._series(hv.harness_upgrade_failure_to_prometheus()):
                    if line.startswith('hive_harness_upgrade_failure_info'):
                        continue
                    self.assertNotIn('{', line,
                                     'alerting series must be unlabeled: %s' % line)

    def test_clearing_drives_the_SAME_series_to_zero(self):
        """Not a different label set — otherwise the page waits for staleness."""
        self._fail()
        self.assertIn('hive_harness_upgrade_failure 1',
                      hv.harness_upgrade_failure_to_prometheus())
        hv.clear_harness_upgrade_failure(
            resolved_by='sara', note='restored and verified',
            converged_image='img:old', expected_image='img:old')
        out = hv.harness_upgrade_failure_to_prometheus()
        self.assertIn('hive_harness_upgrade_failure 0', out)
        self.assertNotIn('hive_harness_upgrade_failure 1', out)

    def test_identity_moves_to_a_companion_info_series(self):
        self._fail()
        out = hv.harness_upgrade_failure_to_prometheus()
        self.assertIn('hive_harness_upgrade_failure_info{run_id="r-42",phase="%s"} 1'
                      % hv.PHASE_ROLLBACK_FAILED, out)

    def test_the_info_series_is_ABSENT_when_healthy(self):
        self.assertNotIn('hive_harness_upgrade_failure_info',
                         '\n'.join(self._series(hv.harness_upgrade_failure_to_prometheus())))

    def test_failed_at_is_a_real_timestamp_while_failing_and_zero_when_not(self):
        self.assertIn('hive_harness_upgrade_failed_at 0',
                      hv.harness_upgrade_failure_to_prometheus())
        self._fail()
        self.assertRegex(hv.harness_upgrade_failure_to_prometheus(),
                         r'hive_harness_upgrade_failed_at \d{9,}')

    def test_an_UNAUDITED_clear_attempt_leaves_the_gauge_paging(self):
        self._fail()
        hv.clear_harness_upgrade_failure(
            resolved_by='sara', note='', converged_image='img:old',
            expected_image='img:old')
        self.assertIn('hive_harness_upgrade_failure 1',
                      hv.harness_upgrade_failure_to_prometheus())

    def test_reason_is_never_a_label(self):
        self._fail()
        out = hv.harness_upgrade_failure_to_prometheus()
        self.assertNotIn('reason=', out)
        self.assertNotIn('boom', out)

    def test_info_labels_cannot_escape_their_quotes(self):
        """Harmless text inside a quoted value is fine; ESCAPING it is not."""
        cache.set(hv.HARNESS_UPGRADE_FAILURE_KEY, {
            'run_id': 'r"1\nhive_evil{x="y"} 99', 'phase': 'a"b',
            'failed_at': 123, 'resolution': None,
        }, None)
        info = [l for l in self._series(hv.harness_upgrade_failure_to_prometheus())
                if l.startswith('hive_harness_upgrade_failure_info')]
        self.assertEqual(len(info), 1)
        self.assertEqual(info[0].count('{'), 1)
        self.assertEqual(info[0].count('}'), 1)
        self.assertEqual(info[0].count('"'), 4)

    def test_emitter_is_read_only(self):
        self._fail()
        before = cache.get(hv.HARNESS_UPGRADE_FAILURE_KEY)
        hv.harness_upgrade_failure_to_prometheus()
        self.assertEqual(cache.get(hv.HARNESS_UPGRADE_FAILURE_KEY), before)
