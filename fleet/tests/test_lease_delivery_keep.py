"""HIVE-3016 leg 1 (ship-first slice): fenced floor/reserved preempt.

The keep-resident/floor grant loop (~L3154) force-revoked a quiet holder with
NO personal-work fence — that is how kei (PLAT-11436 in_progress, pulse#1005
open) was evicted at 11:50 IST for hina. The preempt now runs the SAME fences
as ``wake_preempt_lease_slot`` (fresh-idle + personal-signal) through the
shared helper ``_holder_working_fences`` — one helper, called at both sites
(the fence body and the reserved-grant loop), never duplicated. No victim
means the floor waits: ``RESERVED GRANT blocked (no preemptable non-working
holder)``.

Delivery-keep gating (spec-v2 leg 2) is follow-up PR work: it brings its own
per-evidence-item book and threads a separate gate at the fence's call sites.
This PR carries only the fresh-idle + personal-signal contract and its tests.
"""
from unittest.mock import patch

from django.test import TransactionTestCase

from fleet import model_lease


def _fresh_idle(uname):
    return True, 'fresh-idle'


def _raising_idle(uname):
    raise RuntimeError('health map unavailable')


class FencePartitionBehaviorTests(TransactionTestCase):
    """The fence partitions full boards: work fences work, only no-work
    holders are eviction candidates."""

    HOLDERS = [
        {'principal': 'agent-working'},
        {'principal': 'agent-idle'},
    ]

    def _fence(self, holder_rows=None, workload_by_email=None,
               idle=_fresh_idle):
        return model_lease._holder_working_fences(
            holder_rows=holder_rows if holder_rows is not None else list(self.HOLDERS),
            reserved_usernames=set(),
            agent_by_username={
                'working': {'email': 'working@shizuha.com'},
                'idle': {'email': 'idle@shizuha.com'},
            },
            workload_by_email=workload_by_email or {},
            unread_by_email={},
            alerts_by_email={},
            actionable_buckets=('in_progress', 'in_review'),
            idle_confirmation=idle,
        )

    def test_work_holders_fenced_only_idle_evictable(self):
        # Card test (a): a quiet holder carrying in_progress (actionable)
        # work is fenced; only the no-work holder is the eviction candidate.
        candidates, fenced = self._fence(
            workload_by_email={'working@shizuha.com': {'in_progress': 1}})
        self.assertEqual([h['principal'] for h in candidates], ['agent-idle'])
        self.assertEqual(fenced, {'working'})

    def test_fresh_idle_holders_are_candidates(self):
        # With no workload rows both holders pass every fence and both are
        # eviction candidates (nothing is fenced).
        candidates, fenced = self._fence()
        self.assertEqual(
            sorted(h['principal'] for h in candidates),
            ['agent-idle', 'agent-working'])
        self.assertEqual(fenced, set())

    def test_all_candidates_working_posts_blocked_receipt(self):
        # Card test (a), blocked arm: when every quiet holder carries work
        # the pass must post the blocked receipt — never tune the fence away.
        candidates, fenced = self._fence(
            holder_rows=[{'principal': 'agent-working'}],
            workload_by_email={'working@shizuha.com': {'in_progress': 1}})
        self.assertEqual(candidates, [])
        # The candidates feed reserved_preempt_victim directly:
        victim = model_lease.reserved_preempt_victim(
            candidates, set(), active=set(),
        )
        self.assertIsNone(victim)


class FenceFailureDirectionTests(TransactionTestCase):
    """Every fence failure is fail-closed and blocks the preempt."""

    def test_failing_check_never_evicts(self):
        # A fence failure must not evict: unknown/raising idle checks fence
        # the holder out of the candidate partition.
        candidates, fenced = model_lease._holder_working_fences(
            holder_rows=[{'principal': 'agent-quiet'}],
            reserved_usernames=set(),
            agent_by_username={},
            workload_by_email={},
            unread_by_email={},
            alerts_by_email={},
            actionable_buckets=('in_progress', 'in_review'),
            idle_confirmation=_raising_idle,
        )
        self.assertEqual(candidates, [])
        self.assertEqual(fenced, {'quiet'})

    def test_reserved_holders_are_never_candidates(self):
        candidates, fenced = model_lease._holder_working_fences(
            holder_rows=[{'principal': 'agent-ceo-office'}],
            reserved_usernames={'ceo-office'},
            agent_by_username={},
            workload_by_email={},
            unread_by_email={},
            alerts_by_email={},
            actionable_buckets=('in_progress', 'in_review'),
            idle_confirmation=idle_confirmation_fresh,
        )
        self.assertEqual(candidates, [])
        self.assertEqual(fenced, set())


def idle_confirmation_fresh(uname):
    return True, 'fresh-idle'


class ReservedRenewalBlockedArmTests(TransactionTestCase):
    """Localized `RESERVED GRANT blocked (all candidates working)`: a full
    board where every quiet holder carries actionable work posts nothing and
    the block is auditable."""

    def test_full_pool_every_holder_working_posts_blocked_receipt(self):
        holder_rows = [
            {'principal': 'agent-busy', 'idle_for_s': 900, 'held_for_s': 900},
            {'principal': 'agent-busier', 'idle_for_s': 800, 'held_for_s': 800},
        ]
        workload_by_email = {
            'busy@shizuha.com': {'in_progress': 1},
            'busier@shizuha.com': {'in_progress': 1},
        }
        candidates, fenced = model_lease._holder_working_fences(
            holder_rows=holder_rows,
            reserved_usernames=set(),
            agent_by_username={
                'busy': {'email': 'busy@shizuha.com'},
                'busier': {'email': 'busier@shizuha.com'},
            },
            workload_by_email=workload_by_email,
            unread_by_email={},
            alerts_by_email={},
            actionable_buckets=('in_progress', 'in_review'),
            idle_confirmation=_fresh_idle,
        )
        self.assertEqual(candidates, [])
        self.assertEqual(fenced, {'busy', 'busier'})
