"""HIVE-615: rollback execution — fenced writes, fail-closed target.

The ruling says only the owning command rolls back and an unverified rollback
fails closed. Each test below pins one way that can be violated, and each maps
to a defect that has already happened once:

  * an unproven target must REFUSE, not write. The abandoned Home
    implementation POSTed an empty image and recorded the resulting 400 as a
    successful rollback — "rolled back" written over a fleet that never moved;
  * every write must be fenced. The pre-existing 300s promotion lock had no
    renewal, so a canary waiting on a real agent turn could outlive it while
    the code kept mutating;
  * the phase must advance to ROLLING_BACK BEFORE the write and to ROLLED_BACK
    only after it returns. Recording success first makes a crash mid-write look
    completed, and run_needs_rollback() then stops reporting it as mutated.
"""

from __future__ import annotations

from django.core.cache import cache
from django.test import SimpleTestCase

from fleet import harness_versions as hv


class ExecuteRollbackTests(SimpleTestCase):
    def setUp(self):
        cache.delete(hv.HARNESS_UPGRADE_RUN_KEY)
        cache.delete(hv._AUTOMATED_PROMOTION_LOCK_KEY)

    tearDown = setUp

    def _run(self, last_good):
        return hv.new_run('img:new', run_id='r1', lease_token='t1',
                          last_good=last_good)

    def test_proven_last_good_is_written_and_recorded(self):
        applied = []
        run = self._run('img:old')
        r = hv.execute_rollback(run, apply=lambda i: applied.append(i) or {'ok': i})
        self.assertEqual(r['status'], hv.PHASE_ROLLED_BACK)
        self.assertEqual(r['target'], 'img:old')
        self.assertEqual(applied, ['img:old'])
        self.assertEqual(cache.get(hv.HARNESS_UPGRADE_RUN_KEY)['phase'],
                         hv.PHASE_ROLLED_BACK)

    def test_unproven_target_refuses_and_writes_nothing(self):
        """The Home defect: an empty target must never reach the fleet API."""
        for last_good in (None, '', '   '):
            with self.subTest(last_good=last_good):
                cache.delete(hv.HARNESS_UPGRADE_RUN_KEY)
                applied = []
                run = self._run(last_good)
                r = hv.execute_rollback(run, apply=lambda i: applied.append(i))
                self.assertEqual(r['status'], hv.PHASE_ROLLBACK_FAILED)
                self.assertEqual(applied, [], 'must not write an unproven target')
                self.assertIn('no proven last-good', r['reason'])

    def test_refusal_is_recorded_on_the_run_not_silently_dropped(self):
        run = self._run(None)
        hv.execute_rollback(run)
        stored = cache.get(hv.HARNESS_UPGRADE_RUN_KEY)
        self.assertEqual(stored['phase'], hv.PHASE_ROLLBACK_FAILED)
        self.assertIn('no proven last-good', stored['rollback']['reason'])

    def test_a_failed_write_is_not_recorded_as_rolled_back(self):
        def boom(_image):
            raise RuntimeError('fleet API 400')

        run = self._run('img:old')
        r = hv.execute_rollback(run, apply=boom)
        self.assertEqual(r['status'], hv.PHASE_ROLLBACK_FAILED)
        self.assertIn('rollback write failed', r['reason'])
        self.assertEqual(cache.get(hv.HARNESS_UPGRADE_RUN_KEY)['phase'],
                         hv.PHASE_ROLLBACK_FAILED)

    def test_a_failed_rollback_is_TERMINAL_not_auto_retried(self):
        """Pins PR #72's deliberate choice, which I initially assumed wrong.

        ROLLBACK_FAILED is in TERMINAL_PHASES and NOT in MUTATED_PHASES, so a
        resumed run does not retry it. That is the right call: an automatic
        retry against a permanently failing write is an infinite loop against
        the fleet API, and "we already tried and could not" is a different
        state from "we have not tried yet".

        The safety property that must hold instead is that the failure is
        RECORDED with its reason, so it is escalatable rather than silent —
        asserted below.
        """
        run = self._run('img:old')
        hv.execute_rollback(run, apply=lambda _i: (_ for _ in ()).throw(RuntimeError('x')))
        stored = cache.get(hv.HARNESS_UPGRADE_RUN_KEY)
        self.assertEqual(stored['phase'], hv.PHASE_ROLLBACK_FAILED)
        self.assertIn(hv.PHASE_ROLLBACK_FAILED, hv.TERMINAL_PHASES)
        self.assertNotIn(hv.PHASE_ROLLBACK_FAILED, hv.MUTATED_PHASES)
        self.assertFalse(hv.run_needs_rollback(stored),
                         'terminal: not silently re-queued for another attempt')
        self.assertIn('rollback write failed', stored['rollback']['reason'],
                      'a terminal failure MUST carry its reason or it is silent')

    def test_an_interrupted_rollback_IS_still_reported_as_mutated(self):
        """The distinction that makes the above safe: ROLLING_BACK (we were
        mid-write when we died) must still count as mutated, so a resumed run
        finishes it. Only a *completed attempt* is terminal."""
        run = self._run('img:old')
        run['phase'] = hv.PHASE_ROLLING_BACK
        self.assertTrue(hv.run_needs_rollback(run))

    def test_lost_lease_propagates_and_does_not_write(self):
        """A lost fence must stop the writer, not be swallowed into a status."""
        lease = hv.PromotionLease('r1')
        self.assertTrue(lease.acquire())
        cache.delete(hv._AUTOMATED_PROMOTION_LOCK_KEY)  # simulate expiry
        applied = []
        with self.assertRaises(hv.LeaseLost):
            hv.execute_rollback(self._run('img:old'), lease,
                                apply=lambda i: applied.append(i))
        self.assertEqual(applied, [])

    def test_no_run_is_a_refusal_not_a_crash(self):
        r = hv.execute_rollback(None)
        self.assertEqual(r['status'], hv.PHASE_ROLLBACK_FAILED)
        self.assertIn('nothing to roll back', r['reason'])


class PersistentFailureFactTests(SimpleTestCase):
    """HIVE-615 architecture ruling v8 §5.2: ROLLBACK_FAILED must be
    CRITICAL-AND-LOUD, and clearing requires proven fleet convergence plus an
    audited resolution — explicitly NOT Job success, expiry, or acknowledgement.

    Each test is one of those three rejected clearing conditions, plus the one
    that made a separate fact necessary at all: the run record carries a 24h
    TTL, so a fact stored only there would clear itself by expiry and silently
    stop paging while the fleet is still on the bad image.
    """

    def setUp(self):
        cache.delete(hv.HARNESS_UPGRADE_FAILURE_KEY)
        cache.delete(hv.HARNESS_UPGRADE_RUN_KEY)

    tearDown = setUp

    def _fail(self, last_good='img:old'):
        run = hv.new_run('img:new', run_id='r1', lease_token='t1', last_good=last_good)
        return hv.execute_rollback(
            run, apply=lambda _i: (_ for _ in ()).throw(RuntimeError('fleet API 400')))

    def test_a_failed_rollback_records_an_unresolved_fact(self):
        self._fail()
        fact = hv.harness_upgrade_failure()
        self.assertIsNotNone(fact)
        self.assertEqual(fact['phase'], hv.PHASE_ROLLBACK_FAILED)
        self.assertIn('rollback write failed', fact['reason'])
        self.assertIsNone(fact['resolution'])

    def test_a_refused_rollback_also_records_a_fact(self):
        """Unproven target refuses — but the fleet is still on the bad image."""
        hv.execute_rollback(
            hv.new_run('img:new', run_id='r2', lease_token='t', last_good=None))
        self.assertIsNotNone(hv.harness_upgrade_failure())

    def test_the_fact_is_stored_WITHOUT_expiry(self):
        """The whole reason it is not kept in the run record, which has a 24h TTL.
        An expiring fact stops paging while the fleet is still broken — that is
        clear-by-expiry, which the ruling rejects."""
        self._fail()
        self.assertIsNotNone(cache.get(hv.HARNESS_UPGRADE_FAILURE_KEY))
        # the run record is TTL'd; the fact must not share that fate
        cache.delete(hv.HARNESS_UPGRADE_RUN_KEY)
        self.assertIsNotNone(hv.harness_upgrade_failure(),
                             'fact must outlive the run record it came from')

    def test_acknowledgement_alone_does_not_clear(self):
        self._fail()
        for kw in ({'resolved_by': '', 'note': 'ack'},
                   {'resolved_by': 'sara', 'note': ''}):
            with self.subTest(**kw):
                r = hv.clear_harness_upgrade_failure(
                    converged_image='img:old', expected_image='img:old', **kw)
                self.assertFalse(r['cleared'])
                self.assertIsNotNone(hv.harness_upgrade_failure())

    def test_unproven_convergence_does_not_clear(self):
        """'The Job finished' is not 'the fleet converged'."""
        self._fail()
        for conv, exp in ((None, 'img:old'), ('', 'img:old'), ('img:old', None)):
            with self.subTest(conv=conv, exp=exp):
                r = hv.clear_harness_upgrade_failure(
                    resolved_by='sara', note='looked fine',
                    converged_image=conv, expected_image=exp)
                self.assertFalse(r['cleared'])
                self.assertIn('unproven', r['reason'])

    def test_convergence_to_the_WRONG_image_does_not_clear(self):
        self._fail()
        r = hv.clear_harness_upgrade_failure(
            resolved_by='sara', note='rolled forward instead',
            converged_image='img:something-else', expected_image='img:old')
        self.assertFalse(r['cleared'])
        self.assertIn('expected', r['reason'])
        self.assertIsNotNone(hv.harness_upgrade_failure())

    def test_proven_convergence_plus_audited_resolution_clears(self):
        self._fail()
        r = hv.clear_harness_upgrade_failure(
            resolved_by='sara', note='restored last-good and verified endpoints',
            converged_image='img:old', expected_image='img:old')
        self.assertTrue(r['cleared'])
        self.assertIsNone(hv.harness_upgrade_failure(),
                          'resolved fact must stop paging')
        stored = cache.get(hv.HARNESS_UPGRADE_FAILURE_KEY)
        self.assertEqual(stored['resolution']['resolved_by'], 'sara')
        self.assertIn('verified endpoints', stored['resolution']['note'])

    def test_the_audit_trail_survives_the_clear(self):
        """Cleared != deleted: the resolution must remain auditable."""
        self._fail()
        hv.clear_harness_upgrade_failure(
            resolved_by='sara', note='done', converged_image='img:old',
            expected_image='img:old')
        self.assertIsNotNone(cache.get(hv.HARNESS_UPGRADE_FAILURE_KEY))
