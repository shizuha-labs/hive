"""PLAT-9052 direction-2: fenced wake-time preemption of a quiet holder.

The connect-wake path defers on ``lease_pool_full`` regardless of the wakee's
worthiness, and the reconcile's voluntary revokes can be blocked indefinitely
by the apply-time idle fence — a fence that defers forever is functionally a
leak (19 DEFERs / 25h / zero releases, 2026-09-17). A wake for a seat holding
non-blocked assigned/due work must be able to take a slot at wake time
(PLAT-7032 doctrine), with every fence failing closed to the ordinary defer.
"""

from __future__ import annotations

import os
from unittest import mock

from django.test import SimpleTestCase
from rest_framework.test import APIClient

from fleet.model_lease import wake_preempt_lease_slot

URL = '/hive/api/v1/fleet/internal/connect-wake'
ENV = {'HIVE_CONNECT_WAKE_TOKEN': 'sekrit'}
MODEL_ID = 'DeepSeek-V4-Flash'


def _configs(max_holders=1):
    return [{
        'model_id': MODEL_ID,
        'members': ['kai', 'holder1', 'holder2'],
        'max_holders': max_holders,
        'enabled': True,
        'min_hold_seconds': 60,
        'max_hold_seconds': 3600,
    }]


def _wakee():
    return {
        'id': 'kai', 'agent_username': 'kai', 'username': 'kai',
        'email': 'kai@shizuha.com', 'model': MODEL_ID, 'enabled': False,
    }


def _holder_row(username, idle_for_s=600, held_for_s=3600):
    return {
        'principal': f'agent-{username}',
        'idle_for_s': idle_for_s,
        'held_for_s': held_for_s,
    }


class _FakeClient:
    """Records revoke/grant calls; board shape mirrors Cortex's payload."""

    def __init__(self, holders, max_holders=1, *, revoke_ok=True, grant_ok=True,
                 residency=None):
        self._holders = holders
        self._max_holders = max_holders
        self._revoke_ok = revoke_ok
        self._grant_ok = grant_ok
        self._residency = residency or {}
        self.revoked = []
        self.granted = []

    def board(self, model):
        return {
            'leased': True,
            'model': model,
            'max_holders': self._max_holders,
            'holders': list(self._holders),
            'residency': self._residency,
        }

    def revoke(self, model, *, holder, force, reason):
        self.revoked.append((holder, force, reason))
        return self._revoke_ok

    def grant(self, model, principal, **kwargs):
        self.granted.append((principal, kwargs))
        return self._grant_ok


def _idle_ok(username):
    return True, 'runtime reports fresh idle at apply time'


def _signals(workload=None, unread=None, alerts=None, rosters=None):
    return (workload or {}, unread or {}, alerts or {}, rosters or [])


class WakePreemptFenceTests(SimpleTestCase):
    """Every fence fails closed to "defer" — nobody is evicted on doubt."""

    def _run(self, client, *, agent=None, reason='connect_dm:c1', **kwargs):
        return wake_preempt_lease_slot(
            agent=agent or _wakee(),
            reason=reason,
            client=client,
            configs=_configs(),
            now=1_000_000.0,
            fetch_signals=lambda: _signals(),
            idle_confirmation=_idle_ok,
            **kwargs,
        )

    def test_reserved_wakee_never_bypasses_the_board(self):
        wakee = _wakee()
        wakee['warm_pool_size'] = 1
        client = _FakeClient([_holder_row('holder1')])
        ok, detail = self._run(client, agent=wakee)
        self.assertEqual((ok, detail), (False, 'wakee_reserved'))
        self.assertEqual(client.revoked, [])

    def test_signals_unavailable_defers_fail_closed(self):
        client = _FakeClient([_holder_row('holder1')])
        ok, detail = wake_preempt_lease_slot(
            agent=_wakee(), reason='connect_dm:c1', client=client,
            configs=_configs(), now=1.0,
            fetch_signals=lambda: None, idle_confirmation=_idle_ok,
        )
        self.assertEqual((ok, detail), (False, 'worthiness_signals_unavailable'))
        self.assertEqual(client.revoked, [])

    def test_live_signal_fetch_failure_defers_fail_closed(self):
        # The live fetchers (reconcile command) are the production path: any
        # unavailability must defer, never preempt on an unrankable board.
        from fleet.management.commands import reconcile_on_demand_agents as rod

        client = _FakeClient([_holder_row('holder1')])
        with mock.patch.object(rod.Command, '_fetch_pulse_snapshot',
                               return_value={}), \
             mock.patch.object(rod.Command, '_fetch_pulse_rosters',
                               return_value=[]), \
             mock.patch.object(rod.Command, '_fetch_connect_state'):
            ok, detail = wake_preempt_lease_slot(
                agent=_wakee(), reason='connect_dm:c1', client=client,
                configs=_configs(), now=1.0, idle_confirmation=_idle_ok,
            )
        self.assertEqual((ok, detail), (False, 'worthiness_signals_unavailable'))
        self.assertEqual(client.revoked, [])

    def test_live_connect_snapshot_unavailable_defers_fail_closed(self):
        from fleet.management.commands import reconcile_on_demand_agents as rod
        from fleet.management.commands.reconcile_on_demand_agents import (
            ConnectLifecycleSnapshot,
        )

        client = _FakeClient([_holder_row('holder1')])
        with mock.patch.object(rod.Command, '_fetch_pulse_snapshot',
                               return_value={'workload': {}}), \
             mock.patch.object(rod.Command, '_fetch_pulse_rosters',
                               return_value=[]), \
             mock.patch.object(rod.Command, '_fetch_connect_state',
                               return_value=ConnectLifecycleSnapshot(
                                   available=False, unread_by_email={},
                                   last_dm_at_by_email={}, error='down')):
            ok, detail = wake_preempt_lease_slot(
                agent=_wakee(), reason='connect_dm:c1', client=client,
                configs=_configs(), now=1.0, idle_confirmation=_idle_ok,
            )
        self.assertEqual((ok, detail), (False, 'worthiness_signals_unavailable'))
        self.assertEqual(client.revoked, [])

    def test_team_queue_only_wakee_keeps_deferring(self):
        # Team-queue is the anti-starvation wake path, not deadline pressure:
        # it must never evict a holder.
        client = _FakeClient([_holder_row('holder1')])
        ok, detail = wake_preempt_lease_slot(
            agent=_wakee(), reason='team_queue', client=client,
            configs=_configs(), now=1.0,
            fetch_signals=lambda: _signals(), idle_confirmation=_idle_ok,
        )
        self.assertFalse(ok)
        self.assertTrue(detail.startswith('wakee_not_personally_worthy'))
        self.assertEqual(client.revoked, [])

    def test_connect_dm_attestation_makes_the_wakee_worthy(self):
        # HIVE-602: the wake fires the moment a DM lands for a STOPPED agent,
        # so the DM is undelivered by construction even if the Connect
        # snapshot lags the just-landed message.
        client = _FakeClient([_holder_row('holder1')])
        ok, detail = self._run(client)
        self.assertTrue(ok)
        self.assertIn('preempted agent-holder1', detail)
        self.assertEqual(client.revoked[0][0], 'agent-holder1')
        self.assertTrue(client.revoked[0][1])  # force
        self.assertEqual(client.granted[0][0], 'agent-kai')

    def test_pulse_assigned_tasks_make_the_wakee_worthy_without_dm(self):
        client = _FakeClient([_holder_row('holder1')])
        ok, _ = wake_preempt_lease_slot(
            agent=_wakee(), reason='pulse_due', client=client,
            configs=_configs(), now=1.0,
            fetch_signals=lambda: _signals(
                workload={'kai@shizuha.com': {'open': 2}},
            ),
            idle_confirmation=_idle_ok,
        )
        self.assertTrue(ok)

    def test_holder_with_personal_signal_is_never_victim(self):
        # Work never evicts work: a holder carrying its own actionable load
        # is not preemptable for another work-holder.
        client = _FakeClient([_holder_row('holder1')])
        ok, detail = wake_preempt_lease_slot(
            agent=_wakee(), reason='connect_dm:c1', client=client,
            configs=_configs(), now=1.0,
            fetch_signals=lambda: _signals(
                workload={'holder1@shizuha.com': {'open': 1}},
            ),
            idle_confirmation=_idle_ok,
            agents=[{
                'username': 'holder1', 'email': 'holder1@shizuha.com',
            }],
        )
        self.assertEqual((ok, detail), (False, 'no_preemptable_holder'))
        self.assertEqual(client.revoked, [])

    def test_holder_not_fresh_idle_is_never_victim(self):
        # The reconcile's apply-time fence, inverted: unknown/stale/working
        # holders are treated as active and never evicted.
        client = _FakeClient([_holder_row('holder1')])

        def working(username):
            return False, 'runtime reports working at apply time'

        ok, detail = wake_preempt_lease_slot(
            agent=_wakee(), reason='connect_dm:c1', client=client,
            configs=_configs(), now=1.0,
            fetch_signals=lambda: _signals(),
            idle_confirmation=working,
        )
        self.assertEqual((ok, detail), (False, 'no_preemptable_holder'))
        self.assertEqual(client.revoked, [])

    def test_idle_confirmation_exception_fails_closed(self):
        client = _FakeClient([_holder_row('holder1')])

        def raising(username):
            raise RuntimeError('telemetry down')

        ok, detail = wake_preempt_lease_slot(
            agent=_wakee(), reason='connect_dm:c1', client=client,
            configs=_configs(), now=1.0,
            fetch_signals=lambda: _signals(),
            idle_confirmation=raising,
        )
        self.assertEqual((ok, detail), (False, 'no_preemptable_holder'))
        self.assertEqual(client.revoked, [])

    def test_revoke_failure_defers(self):
        client = _FakeClient([_holder_row('holder1')], revoke_ok=False)
        ok, detail = self._run(client)
        self.assertEqual((ok, detail), (False, 'revoke_failed'))
        self.assertEqual(client.granted, [])

    def test_grant_failure_after_revoke_leaves_slot_to_the_tick(self):
        client = _FakeClient([_holder_row('holder1')], grant_ok=False)
        ok, detail = self._run(client)
        self.assertEqual((ok, detail), (False, 'grant_failed_after_revoke'))
        self.assertEqual(len(client.revoked), 1)
        # The grant was attempted and refused; the freed slot goes to the tick.
        self.assertEqual(len(client.granted), 1)

    def test_slot_freed_since_defer_defers_to_ordinary_grant(self):
        client = _FakeClient([], max_holders=2)
        ok, detail = self._run(client)
        self.assertEqual((ok, detail), (False, 'slot_freed_since_defer'))
        self.assertEqual(client.revoked, [])

    def test_board_unreadable_defers(self):
        class _DeadClient(_FakeClient):
            def board(self, model):
                return None

        client = _DeadClient([_holder_row('holder1')])
        ok, detail = self._run(client)
        self.assertEqual((ok, detail), (False, 'board_unreadable'))

    def test_wakee_already_holder_defers(self):
        client = _FakeClient([_holder_row('kai')])
        ok, detail = self._run(client)
        self.assertEqual((ok, detail), (False, 'already_holder'))

    def test_victim_prefers_the_unused_grant(self):
        # reserved_preempt_victim ranking: an unused grant (idle ≈ held) is
        # evicted before an idle-but-used one.
        client = _FakeClient([
            _holder_row('holder2', idle_for_s=900, held_for_s=3600),
            _holder_row('holder1', idle_for_s=900, held_for_s=900),
        ])
        ok, detail = self._run(client)
        self.assertTrue(ok)
        self.assertIn('preempted agent-holder1', detail)

    def test_reserved_holder_is_never_victim(self):
        client = _FakeClient([_holder_row('holder1')])
        ok, detail = wake_preempt_lease_slot(
            agent=_wakee(), reason='connect_dm:c1', client=client,
            configs=_configs(), now=1.0,
            fetch_signals=lambda: _signals(),
            idle_confirmation=_idle_ok,
            agents=[{
                'username': 'holder1', 'email': 'holder1@shizuha.com',
                'warm_pool_size': 1,
            }],
        )
        self.assertEqual((ok, detail), (False, 'no_preemptable_holder'))
        self.assertEqual(client.revoked, [])


class ConnectWakePreemptViewTests(SimpleTestCase):
    """The bypass is wired only on the unambiguous full-board defer."""

    def setUp(self):
        self.client = APIClient()

    def _post(self, **body):
        return self.client.post(
            URL, {'email': 'kai@shizuha.com', **body},
            format='json', headers={'X-Hive-Wake-Token': 'sekrit'},
        )

    def _agents(self, enabled=False):
        return [_wakee() | {'enabled': enabled}]

    def test_preempt_success_falls_through_to_start(self):
        with mock.patch.dict(os.environ, ENV), \
             mock.patch('fleet.views.list_lifecycle_agents',
                        return_value=self._agents()), \
             mock.patch('fleet.views._is_archived_username',
                        return_value=False), \
             mock.patch('fleet.model_lease.lease_wake_available',
                        return_value=(False, 'lease_pool_full')), \
             mock.patch('fleet.model_lease.wake_preempt_lease_slot',
                        return_value=(True, 'preempted agent-holder1')) as preempt, \
             mock.patch('fleet.views.set_lifecycle_agent_enabled',
                        return_value=(True, {'ok': 1})) as toggle:
            resp = self._post(reason='connect_dm:c1')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'started')
        preempt.assert_called_once()
        self.assertEqual(preempt.call_args.kwargs['reason'], 'connect_dm:c1')
        toggle.assert_called_once_with('kai', True)

    def test_preempt_failure_defers_202(self):
        with mock.patch.dict(os.environ, ENV), \
             mock.patch('fleet.views.list_lifecycle_agents',
                        return_value=self._agents()), \
             mock.patch('fleet.views._is_archived_username',
                        return_value=False), \
             mock.patch('fleet.model_lease.lease_wake_available',
                        return_value=(False, 'lease_pool_full')), \
             mock.patch('fleet.model_lease.wake_preempt_lease_slot',
                        return_value=(False, 'no_preemptable_holder')), \
             mock.patch('fleet.views.set_lifecycle_agent_enabled') as toggle:
            resp = self._post(reason='connect_dm:c1')
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.data['status'], 'lease_waiting')
        toggle.assert_not_called()

    def test_non_full_reason_skips_the_preempt_entirely(self):
        # await_reconcile_grant is pending (202 lane) but NOT the unambiguous
        # full-board case: the bypass must not fire for it.
        with mock.patch.dict(os.environ, ENV), \
             mock.patch('fleet.views.list_lifecycle_agents',
                        return_value=self._agents()), \
             mock.patch('fleet.views._is_archived_username',
                        return_value=False), \
             mock.patch('fleet.model_lease.lease_wake_available',
                        return_value=(False, 'await_reconcile_grant')), \
             mock.patch('fleet.model_lease.wake_preempt_lease_slot') as preempt, \
             mock.patch('fleet.views.set_lifecycle_agent_enabled') as toggle:
            resp = self._post(reason='connect_dm:c1')
        self.assertEqual(resp.status_code, 202)
        preempt.assert_not_called()
        toggle.assert_not_called()
