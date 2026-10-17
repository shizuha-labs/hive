"""HIVE-2862 follow-up: floor candidate selection prefers READY waiters.

Live shape this cures (CoS sweep, 2026-10-08 ~17:00 IST): the devops floor DID
bite — taku held the seat at `critical-team-floor team=devops` — but taku's
only membership was a different org's devops seat with 0 active tasks. The
reserved seat was spent on an idle cross-org waiter while the urgent Shizuha
devops root (PLAT-11501) had a ready waiter stuck `waiting` behind it.

Seam: `critical_team_floor_state(ready=...)`. Within a target, a ready waiter
(holds UNSTARTED assigned Pulse work — ready/open/todo) seats before any idle
one regardless of lease age; across targets, a target fielded by a ready
waiter rotates in ahead of one fielded only by idle waiters. None (default)
keeps pure LRH selection.
"""
from __future__ import annotations

from django.test import SimpleTestCase

from fleet.model_lease import PoolMember, critical_team_floor_state


def _waiter(uname, teams, last_held_at=0.0):
    return PoolMember(
        username=uname, email=f'{uname}@shizuha.com',
        teams=tuple(teams), last_held_at=last_held_at,
    )


class ReadyWaiterSelectionTests(SimpleTestCase):
    def test_ready_waiter_seats_before_newer_idle_waiter(self):
        # Regression case from the CoS check-in: devops floor, two devops
        # waiters — `ichi` holds the board's urgent assigned item, `takusei`
        # is an idle cross-org devops seat with a newer lease age.
        policy = {'devops': 1}
        members = [
            _waiter('ichi', ('shizuha::devops',), last_held_at=0.0),
            _waiter('takusei', ('shizuha-trading::devops',), last_held_at=600.0),
        ]
        state = critical_team_floor_state(
            members=members,
            wants={'ichi', 'takusei'},
            holder_usernames={'san'},
            policy=policy,
            ready={'ichi'},
        )
        self.assertEqual(state['targets'], [('devops', ['ichi'])])

    def test_ready_fielded_target_rotates_before_lrh_earlier_idle_target(self):
        # Team `alpha` was held longer ago (LRH-earlier) but can only field an
        # idle waiter; team `beta` was held more recently but fields a ready
        # one. The board's urgent item is on `beta` — `beta` seats first.
        policy = {'alpha': 1, 'beta': 1}
        members = [
            _waiter('alpha_idle', ('x::alpha',), last_held_at=900.0),
            _waiter('beta_ready', ('y::beta',), last_held_at=300.0),
        ]
        state = critical_team_floor_state(
            members=members,
            wants={'alpha_idle', 'beta_ready'},
            holder_usernames=set(),
            policy=policy,
            ready={'beta_ready'},
        )
        self.assertEqual(state['targets'],
                         [('beta', ['beta_ready']), ('alpha', ['alpha_idle'])])

    def test_no_ready_map_is_unchanged_pure_lrh(self):
        policy = {'devops': 1}
        members = [
            _waiter('ichi', ('shizuha::devops',), last_held_at=100.0),
            _waiter('takusei', ('shizuha-trading::devops',), last_held_at=60.0),
        ]
        state = critical_team_floor_state(
            members=members,
            wants={'ichi', 'takusei'},
            holder_usernames=set(),
            policy=policy,
        )
        # No ready map → lease age alone decides (takusei held least recently).
        self.assertEqual(state['targets'], [('devops', ['takusei'])])

    def test_empty_ready_map_is_unchanged_pure_lrh(self):
        policy = {'devops': 1}
        members = [
            _waiter('ichi', ('shizuha::devops',), last_held_at=100.0),
            _waiter('takusei', ('shizuha-trading::devops',), last_held_at=60.0),
        ]
        state = critical_team_floor_state(
            members=members,
            wants={'ichi', 'takusei'},
            holder_usernames=set(),
            policy=policy,
            ready=set(),
        )
        self.assertEqual(state['targets'], [('devops', ['takusei'])])
