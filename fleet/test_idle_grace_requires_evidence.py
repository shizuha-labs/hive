"""Absent telemetry is not evidence of work.

Operator 2026-08-05, looking at Accounting on /hive/agents (0 active · 2 blocked
· 2 scheduled · 0 backlog):

    i don't understand why banto is even running here .. ideally hive should
    have hibernated such agents automatically since the team doesn't seem to
    have any active tasks?

The bucket policy was already right — ACTIONABLE_BUCKETS deliberately excludes
blocked/scheduled/backlog (HIVE-651), so those four items were not holding the
seat. What held it was `_activity_grace_protects`.

banto reported `heartbeat_outcome=None` AND `activity_state=None`, with a
`last_active_at` two minutes old. The missing-outcome branch withheld grace only
when activity_state was EXPLICITLY 'idle' or 'sleeping', so an agent reporting
neither field fell through to the unconditional keep and was protected forever —
while its own heartbeat kept refreshing last_active_at. Measured on the live row
before the fix:

    activity_state=None    outcome=None -> grace_protects=True
    activity_state='idle'  outcome=None -> grace_protects=False

The docstring already stated the intended rule ("paid turns that never write
telemetry still bump last_active_at, so treating missing outcome as fail-closed
grace keeps high-token idle seats warm"); the code contradicted it. This is the
same family as the PLS-204 flapping-agent drain: a signal that only proves the
process REACHED the system was being read as proof that it did work.
"""
from datetime import datetime, timedelta, timezone

from django.test import SimpleTestCase

from .on_demand import _activity_grace_protects


def _fresh(minutes_ago: float = 0.5) -> str:
    """A last_active_at inside the grace window, relative to NOW.

    A hardcoded timestamp silently ages out of the window and turns every
    "grace is denied" assertion vacuous (it would pass because the activity was
    stale, not because the telemetry was empty) while flipping every "grace is
    kept" assertion red. Generate it.
    """
    return (
        datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    ).replace(tzinfo=None).isoformat()


def _stale(minutes_ago: float = 90) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    ).replace(tzinfo=None).isoformat()


def _agent(**kw):
    base = {'last_active_at': _fresh()}
    base.update(kw)
    return base


class EmptyTelemetryDoesNotHoldASeatTests(SimpleTestCase):
    def test_no_outcome_and_no_activity_state_does_not_grant_grace(self):
        """banto's exact shape."""
        self.assertFalse(
            _activity_grace_protects(_agent(), 5),
            'a fresh last_active_at with no telemetry at all is a heartbeat, '
            'not work — granting grace here keeps the seat resident forever',
        )

    def test_blank_and_null_activity_state_behave_the_same(self):
        for act in (None, '', '   '):
            self.assertFalse(
                _activity_grace_protects(_agent(activity_state=act), 5),
                f'activity_state={act!r} carries no evidence of work',
            )

    def test_explicit_idle_still_denies_grace(self):
        for act in ('idle', 'sleeping', 'IDLE'):
            self.assertFalse(_activity_grace_protects(_agent(activity_state=act), 5))


class RealWorkIsStillProtectedTests(SimpleTestCase):
    """The regression guard: this must not start culling busy agents."""

    def test_a_working_agent_keeps_its_grace(self):
        self.assertTrue(
            _activity_grace_protects(_agent(activity_state='working'), 5),
            'a positive non-idle signal is the fail-closed keep this branch '
            'exists for — mid-turn agents must not be stopped',
        )

    def test_a_productive_outcome_keeps_its_grace_without_activity_state(self):
        self.assertTrue(
            _activity_grace_protects(_agent(heartbeat_outcome='worked_task'), 5),
            'an explicit productive outcome is evidence even with no '
            'activity_state',
        )

    def test_needs_help_with_ready_work_keeps_its_grace(self):
        """Stuck on real work — keep the seat so it can be helped."""
        self.assertTrue(_activity_grace_protects(
            _agent(heartbeat_outcome='needs_help', heartbeat_ready_task_count=3), 5,
        ))

    def test_needs_help_with_an_empty_queue_does_not(self):
        # The non-productive branch needs `heartbeat_observed_at` to prove the
        # heartbeat is what moved last_active_at; supply it, as a real envelope
        # does.
        self.assertFalse(_activity_grace_protects(_agent(
            heartbeat_outcome='needs_help',
            heartbeat_ready_task_count=0,
            heartbeat_observed_at=_fresh(0.4),
        ), 5))

    def test_a_non_productive_outcome_without_an_observed_at_keeps_grace(self):
        """Documented as-is, deliberately NOT changed here.

        With no `heartbeat_observed_at` we cannot show the heartbeat is what
        moved `last_active_at`, so the code keeps the seat rather than risk
        culling a mid-turn agent. That is a defensible fail-safe, but it is the
        same shape as the bug this file fixes: an agent that reports a
        non-productive outcome and omits the timestamp stays resident forever.
        Pinning it here so the behaviour is a decision rather than an accident —
        tightening it is a separate change with real cull risk.

        Note this is NOT banto's path: banto reported no outcome at all.
        """
        self.assertTrue(_activity_grace_protects(
            _agent(heartbeat_outcome='queue_empty'), 5,
        ))


class StaleActivityIsUnaffectedTests(SimpleTestCase):
    def test_activity_older_than_the_window_never_protects(self):
        for extra in ({}, {'activity_state': 'working'}, {'heartbeat_outcome': 'worked_task'}):
            agent = _agent(last_active_at=_stale(), **extra)
            self.assertFalse(
                _activity_grace_protects(agent, 5),
                'the grace window is the first gate; nothing reopens it',
            )

    def test_missing_activity_timestamp_never_protects(self):
        self.assertFalse(_activity_grace_protects({'activity_state': 'working'}, 5))
