"""HIVE-615: renewable fencing lease + durable promotion run state.

Architecture decision (CoS, HLD v7): the upgrade lifecycle stays in ONE
orchestration command; canary observation may be factored out but reports under
the same run ID and the same renewable fencing lease, and only the owning
command advances or rolls back. Lease loss or an unverified rollback fails
closed.

These tests pin the failure directions, because every one of them is a way the
fleet could be silently corrupted:

  * a lease that expired mid-run must STOP the writer, not let it keep mutating
    (the gap in the pre-existing 300s lock, which had no renewal and no fence);
  * a lease must never be stealable or releasable by a non-owner;
  * a run interrupted after the first switch must be resumable-or-rollbackable,
    never silently abandoned;
  * a rollback with no PROVEN last-good must refuse rather than write an empty
    image and record success — the exact defect revi found in the abandoned
    Home implementation.
"""

from __future__ import annotations

from django.core.cache import cache
from django.test import SimpleTestCase

from fleet import harness_versions as hv


class LeaseTests(SimpleTestCase):
    def setUp(self):
        cache.delete(hv._AUTOMATED_PROMOTION_LOCK_KEY)
        cache.delete(hv.HARNESS_UPGRADE_RUN_KEY)

    tearDown = setUp

    def test_acquire_is_exclusive(self):
        a = hv.PromotionLease('run-a')
        b = hv.PromotionLease('run-b')
        self.assertTrue(a.acquire())
        self.assertFalse(b.acquire(), 'a second writer must not acquire')
        self.assertTrue(a.held())
        self.assertFalse(b.held())

    def test_assert_held_raises_once_the_lease_is_gone(self):
        """The core fence: an expired lease must stop the writer."""
        a = hv.PromotionLease('run-a')
        self.assertTrue(a.acquire())
        a.assert_held()  # fine while held
        cache.delete(hv._AUTOMATED_PROMOTION_LOCK_KEY)  # simulate TTL expiry
        with self.assertRaises(hv.LeaseLost):
            a.assert_held()

    def test_assert_held_raises_when_another_writer_took_it(self):
        a = hv.PromotionLease('run-a')
        self.assertTrue(a.acquire())
        cache.set(hv._AUTOMATED_PROMOTION_LOCK_KEY, 'someone-else', 300)
        with self.assertRaises(hv.LeaseLost):
            a.assert_held()

    def test_renew_extends_only_for_the_owner(self):
        a = hv.PromotionLease('run-a')
        self.assertTrue(a.acquire())
        self.assertTrue(a.renew())
        cache.set(hv._AUTOMATED_PROMOTION_LOCK_KEY, 'someone-else', 300)
        self.assertFalse(a.renew(), 'renew must not steal a lease it lost')
        self.assertEqual(cache.get(hv._AUTOMATED_PROMOTION_LOCK_KEY), 'someone-else')

    def test_release_never_drops_another_writers_lease(self):
        a = hv.PromotionLease('run-a')
        self.assertTrue(a.acquire())
        cache.set(hv._AUTOMATED_PROMOTION_LOCK_KEY, 'someone-else', 300)
        a.release()
        self.assertEqual(
            cache.get(hv._AUTOMATED_PROMOTION_LOCK_KEY), 'someone-else',
            'release must be a no-op when we no longer own the lease',
        )

    def test_adopt_reattaches_after_restart_but_only_with_the_right_token(self):
        a = hv.PromotionLease('run-a')
        self.assertTrue(a.acquire())
        token = a.token
        resumed = hv.PromotionLease('run-a')          # fresh process
        self.assertFalse(resumed.adopt('wrong-token'))
        self.assertTrue(resumed.adopt(token))
        resumed.assert_held()

    def test_uses_the_same_key_as_the_fast_promote_path(self):
        """One writer, one key — a separate key would recreate the race."""
        self.assertEqual(hv.PromotionLease('x').key, hv._AUTOMATED_PROMOTION_LOCK_KEY)


class RunStateTests(SimpleTestCase):
    def setUp(self):
        cache.delete(hv._AUTOMATED_PROMOTION_LOCK_KEY)
        cache.delete(hv.HARNESS_UPGRADE_RUN_KEY)

    tearDown = setUp

    def _run(self, **over):
        lease = hv.PromotionLease('run-1')
        lease.acquire()
        run = hv.new_run('img:new', run_id='run-1', lease_token=lease.token,
                         last_good='img:old')
        run.update(over)
        return run, lease

    def test_new_run_persists_every_field_the_decision_requires(self):
        run, _ = self._run()
        for field in ('run_id', 'phase', 'candidate', 'last_good',
                      'pre_switch_turns', 'canary', 'deadlines', 'lease_token'):
            self.assertIn(field, run, field)
        self.assertEqual(run['phase'], hv.PHASE_DETECTED)

    def test_save_and_load_round_trip(self):
        run, lease = self._run()
        hv.save_run(run, lease)
        loaded = hv.load_run()
        self.assertEqual(loaded['run_id'], 'run-1')
        self.assertEqual(loaded['candidate'], 'img:new')
        self.assertEqual(loaded['last_good'], 'img:old')

    def test_save_is_fenced_and_refuses_without_the_lease(self):
        """State must not be written by a writer that lost the lease."""
        run, lease = self._run()
        cache.delete(hv._AUTOMATED_PROMOTION_LOCK_KEY)
        with self.assertRaises(hv.LeaseLost):
            hv.save_run(run, lease)

    def test_clear_is_fenced_too(self):
        run, lease = self._run()
        hv.save_run(run, lease)
        cache.set(hv._AUTOMATED_PROMOTION_LOCK_KEY, 'someone-else', 300)
        with self.assertRaises(hv.LeaseLost):
            hv.clear_run(lease)
        self.assertIsNotNone(hv.load_run(), 'run state must survive a fenced-out clear')

    def test_load_ignores_garbage(self):
        cache.set(hv.HARNESS_UPGRADE_RUN_KEY, 'not-a-dict', 60)
        self.assertIsNone(hv.load_run())
        cache.set(hv.HARNESS_UPGRADE_RUN_KEY, {'no': 'run_id'}, 60)
        self.assertIsNone(hv.load_run())


class RestartContractTests(SimpleTestCase):
    """A run interrupted after mutating must resume or roll back — never vanish."""

    def test_phases_that_mutated_require_rollback_on_resume(self):
        for phase in (hv.PHASE_CANARY_SWITCHED, hv.PHASE_CANARY_VERIFIED,
                      hv.PHASE_FLEET_SWITCHED, hv.PHASE_VERIFIED,
                      hv.PHASE_ROLLING_BACK):
            self.assertTrue(hv.run_needs_rollback({'phase': phase}), phase)

    def test_pre_mutation_phases_need_no_rollback(self):
        for phase in (hv.PHASE_DETECTED, hv.PHASE_DISPATCHED):
            self.assertFalse(hv.run_needs_rollback({'phase': phase}), phase)

    def test_terminal_phases_need_no_rollback(self):
        for phase in (hv.PHASE_ROLLED_BACK, hv.PHASE_ROLLBACK_FAILED, hv.PHASE_FAILED):
            self.assertFalse(hv.run_needs_rollback({'phase': phase}), phase)

    def test_absent_run_needs_nothing(self):
        self.assertFalse(hv.run_needs_rollback(None))


class RollbackTargetTests(SimpleTestCase):
    """No proven last-good => refuse. This is revi's P1 #5, fixed at the source."""

    def test_returns_the_proven_last_good(self):
        self.assertEqual(hv.rollback_target({'last_good': 'img:old'}), 'img:old')

    def test_refuses_without_a_proven_last_good(self):
        for run in ({'last_good': ''}, {'last_good': None}, {'last_good': '   '},
                    {}, None):
            self.assertIsNone(hv.rollback_target(run), run)
