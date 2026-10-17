"""HIVE-2862 - over-floor lease_idle_waste holders yield to urgent waiters.

Live shape this cures: merge holds 3 of 6 leases while devops (urgent root
PLAT-11501) has zero holders and a directly-assigned agent stuck on
lease_pool_full. The preempt must pick the idle holder from the over-floor
team FIRST - releasing an idle lease is pure waste release - before any
productive candidate.
"""
from __future__ import annotations

from django.test import SimpleTestCase

from ..model_lease import reserved_preempt_victim


class OverFloorWasteYieldTests(SimpleTestCase):

    def test_over_floor_waste_holder_yields_first(self):
        # Full board: kei (merge, productive - floor 1 of 1), aoi + ryo
        # (merge, idle - merge is 3 over its floor of 1), zen (review, idle
        # AT its floor), and a devops waiter (mio) waiting with urgent work.
        # The yielded lease must be one of the merge over-floor idles.
        holders = [
            {'principal': 'agent-kei', 'idle_for_s': 400, 'held_for_s': 400},
            {'principal': 'agent-aoi', 'idle_for_s': 1200, 'held_for_s': 1800},
            {'principal': 'agent-ryo', 'idle_for_s': 900, 'held_for_s': 2400},
            {'principal': 'agent-zen', 'idle_for_s': 200, 'held_for_s': 600},
            {'principal': 'agent-mio', 'idle_for_s': 60, 'held_for_s': 900},
        ]
        priorities = {
            'agent-aoi': 0,   # merge idle, merge 3/1 over floor
            'agent-ryo': 0,   # merge idle, same over-floor signature
            'agent-zen': 1,   # review idle, review AT floor
            'agent-kei': 1,   # merge productive
        }
        victim = reserved_preempt_victim(holders, set(), yield_priority=priorities)
        self.assertIn(victim, {'agent-aoi', 'agent-ryo'})

    def test_waste_inside_floor_does_not_outrank_floor_holder(self):
        # A waste holder whose team is AT its floor must NOT outrank a
        # productive floor-holder claim when no waste is over-floor.
        holders = [
            {'principal': 'agent-ni', 'idle_for_s': 1500, 'held_for_s': 3000},
            {'principal': 'agent-sara', 'idle_for_s': 300, 'held_for_s': 300},
        ]
        priorities = {'agent-ni': 1, 'agent-sara': 1}
        victim = reserved_preempt_victim(holders, set(), yield_priority=priorities)
        self.assertEqual(victim, 'agent-sara')

    def test_no_priority_map_is_unchanged_behavior(self):
        holders = [
            {'principal': 'agent-kei', 'idle_for_s': 600, 'held_for_s': 600},
            {'principal': 'agent-ryo', 'idle_for_s': 100, 'held_for_s': 300},
        ]
        victim = reserved_preempt_victim(holders, set())
        self.assertEqual(victim, 'agent-kei')
