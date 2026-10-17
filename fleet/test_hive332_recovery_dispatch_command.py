"""PLAT-7631 — the HIVE-332 recovery-action dispatch trigger (management command).

Covers the required increment: a trigger that calls ``create_recovery_action``
+ ``send_recovery_action`` with the v1 safety contract intact —

- observe (dry_run) default; SAFE action → created + dispatched (SENT);
- apply mode → denied by the P4 policy (CommandError, no row);
- gated action type → denied (CommandError, no row);
- unknown action type → denied;
- secret material in params → denied;
- ``--no-send`` creates without dispatching (PENDING);
- integration: the dispatched channel event reaches the consumer's
  ``daemon_recovery_action`` handler and lands on the live daemon socket.

Same harness as fleet/test_hive332_recovery_action.py — WebsocketCommunicator
with InMemoryChannelLayer, no Redis.
"""
import hashlib
import json
from io import StringIO

from channels.testing import WebsocketCommunicator
from channels.db import database_sync_to_async
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, TransactionTestCase, override_settings

from fleet.consumers import DaemonLinkConsumer
from fleet.models import (
    AgentRecoveryAction,
    DaemonCredential,
    DaemonRegistry,
    FleetAgent,
)

TEST_CHANNEL_LAYER = {
    "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"},
}

_TEST_DAEMON = "daemon-dispatch-001"
_TEST_TOKEN = "dispatch-test-token"
_TOKEN_HASH = hashlib.sha256(_TEST_TOKEN.encode()).hexdigest()


def _mk_agent(username="dispatch-agent"):
    return FleetAgent.objects.create(agent_username=username)


def _mk_daemon(daemon_id=_TEST_DAEMON):
    return DaemonRegistry.objects.create(daemon_id=daemon_id, fleet_id="fleet-r-1")


@override_settings(CHANNEL_LAYERS=TEST_CHANNEL_LAYER)
class SendRecoveryActionCommandTests(TestCase):
    """Unit tests: creation + dispatch + v1 safety-contract enforcement."""

    def setUp(self):
        self.agent = _mk_agent()
        self.daemon = _mk_daemon()
        self.agent.managing_daemon = self.daemon
        self.agent.save(update_fields=['managing_daemon'])

    def _call(self, *args):
        out = StringIO()
        call_command('send_recovery_action', *args, stdout=out)
        return out.getvalue()

    def test_observe_default_creates_and_dispatches_safe_action(self):
        output = self._call(
            '--agent', 'dispatch-agent',
            '--action-type', 'pull_desired_state_now',
            '--reason', 'PLAT-7626 live round trip',
        )
        action = AgentRecoveryAction.objects.get()
        self.assertEqual(action.mode, AgentRecoveryAction.Mode.DRY_RUN)
        self.assertEqual(action.status, AgentRecoveryAction.Status.SENT)
        self.assertEqual(action.action_type, 'pull_desired_state_now')
        self.assertEqual(action.daemon_id, self.daemon.id)
        self.assertEqual(action.before_summary.get('reason'), 'PLAT-7626 live round trip')
        self.assertIn(f'action_id={action.id}', output)
        self.assertIn(f'correlation_id={action.correlation_id}', output)
        self.assertIn('status=sent', output)
        self.assertIn(f'daemon-link-{self.daemon.daemon_id}', output)

    def test_observe_alias_maps_to_dry_run(self):
        self._call(
            '--agent', 'dispatch-agent',
            '--action-type', 'refresh_team_roster_cache',
            '--mode', 'observe',
        )
        self.assertEqual(
            AgentRecoveryAction.objects.get().mode,
            AgentRecoveryAction.Mode.DRY_RUN,
        )

    def test_explicit_daemon_id_overrides_managing_daemon(self):
        other = _mk_daemon(daemon_id='daemon-dispatch-002')
        self._call(
            '--agent', 'dispatch-agent',
            '--action-type', 'restart_agent',
            '--daemon', other.daemon_id,
        )
        self.assertEqual(AgentRecoveryAction.objects.get().daemon_id, other.id)

    def test_daemon_by_owner_username_resolves(self):
        self.daemon.owner_username = 'dispatch-owner'
        self.daemon.save(update_fields=['owner_username'])
        self._call(
            '--agent', 'dispatch-agent',
            '--action-type', 'pull_desired_state_now',
            '--daemon', 'dispatch-owner',
        )
        self.assertEqual(AgentRecoveryAction.objects.get().daemon_id, self.daemon.id)

    def test_no_send_creates_pending_without_dispatch(self):
        output = self._call(
            '--agent', 'dispatch-agent',
            '--action-type', 'pull_desired_state_now',
            '--no-send',
        )
        action = AgentRecoveryAction.objects.get()
        self.assertEqual(action.status, AgentRecoveryAction.Status.PENDING)
        self.assertIn('--no-send', output)
        self.assertNotIn('dispatched frame', output)

    def test_apply_mode_is_denied_and_creates_no_row(self):
        with self.assertRaises(CommandError) as ctx:
            self._call(
                '--agent', 'dispatch-agent',
                '--action-type', 'pull_desired_state_now',
                '--mode', 'apply',
            )
        self.assertIn('safety contract', str(ctx.exception))
        self.assertEqual(AgentRecoveryAction.objects.count(), 0)

    def test_gated_action_type_is_denied(self):
        with self.assertRaises(CommandError) as ctx:
            self._call(
                '--agent', 'dispatch-agent',
                '--action-type', 'delete_user',
            )
        self.assertIn('gated', str(ctx.exception))
        self.assertEqual(AgentRecoveryAction.objects.count(), 0)

    def test_unknown_action_type_is_denied(self):
        with self.assertRaises(CommandError) as ctx:
            self._call(
                '--agent', 'dispatch-agent',
                '--action-type', 'totally_made_up_action',
            )
        self.assertIn('default-deny', str(ctx.exception))
        self.assertEqual(AgentRecoveryAction.objects.count(), 0)

    def test_secret_material_in_params_is_denied(self):
        with self.assertRaises(CommandError) as ctx:
            self._call(
                '--agent', 'dispatch-agent',
                '--action-type', 'pull_desired_state_now',
                '--params', json.dumps(
                    {'token': 'ghp_abcdefghijklmnopqrstuvwxyz0123456789'},
                ),
            )
        self.assertIn('safety contract', str(ctx.exception))
        self.assertEqual(AgentRecoveryAction.objects.count(), 0)

    def test_invalid_params_json_is_rejected(self):
        with self.assertRaises(CommandError) as ctx:
            self._call(
                '--agent', 'dispatch-agent',
                '--action-type', 'pull_desired_state_now',
                '--params', '{not json',
            )
        self.assertIn('not valid JSON', str(ctx.exception))

    def test_unknown_agent_is_rejected(self):
        with self.assertRaises(CommandError) as ctx:
            self._call(
                '--agent', 'no-such-agent',
                '--action-type', 'pull_desired_state_now',
            )
        self.assertIn('no FleetAgent', str(ctx.exception))

    def test_unknown_daemon_ref_is_rejected(self):
        with self.assertRaises(CommandError) as ctx:
            self._call(
                '--agent', 'dispatch-agent',
                '--action-type', 'pull_desired_state_now',
                '--daemon', 'daemon-does-not-exist',
            )
        self.assertIn('no DaemonRegistry', str(ctx.exception))

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(CommandError) as ctx:
            self._call(
                '--agent', 'dispatch-agent',
                '--action-type', 'pull_desired_state_now',
                '--mode', 'yolo',
            )
        self.assertIn('unknown mode', str(ctx.exception))

    def test_agent_without_daemon_and_no_flag_is_rejected(self):
        orphan = _mk_agent(username='daemonless-agent')
        with self.assertRaises(CommandError) as ctx:
            self._call(
                '--agent', 'daemonless-agent',
                '--action-type', 'pull_desired_state_now',
            )
        self.assertIn('dispatch target unresolved', str(ctx.exception))
        self.assertEqual(
            AgentRecoveryAction.objects.filter(agent=orphan).count(), 0,
        )


@override_settings(CHANNEL_LAYERS=TEST_CHANNEL_LAYER)
class SendRecoveryActionChannelIntegrationTests(TransactionTestCase):
    """Integration: the dispatched event reaches the consumer's
    ``daemon_recovery_action`` handler and lands on the live daemon socket."""

    def setUp(self):
        DaemonCredential.objects.create(
            daemon_id=_TEST_DAEMON, token_hash=_TOKEN_HASH,
        )

    async def _register(self, daemon_id=_TEST_DAEMON):
        comm = WebsocketCommunicator(
            DaemonLinkConsumer.as_asgi(), "/v1/fleet/daemon-link",
        )
        connected, _ = await comm.connect()
        assert connected
        await comm.send_json_to({
            "type": "register",
            "daemon_id": daemon_id,
            "fleet_id": "fleet-r-1",
            "runtime": "docker",
            "token": _TEST_TOKEN,
        })
        ack = await comm.receive_json_from()
        assert ack["type"] == "register_ack"
        return comm

    async def test_dispatched_event_reaches_daemon_socket(self):
        await database_sync_to_async(_mk_agent)("channel-agent")
        daemon = await database_sync_to_async(_mk_daemon)()
        comm = await self._register(daemon.daemon_id)
        try:
            out = StringIO()
            await database_sync_to_async(call_command)(
                'send_recovery_action',
                '--agent', 'channel-agent',
                '--action-type', 'pull_desired_state_now',
                '--reason', 'channel integration',
                '--daemon', daemon.daemon_id,
                stdout=out,
            )

            # The daemon (this communicator) receives the recovery_action
            # frame — the channel event reached daemon_recovery_action and was
            # pushed down the live socket.
            received = await comm.receive_json_from(timeout=5)
            self.assertEqual(received["type"], "recovery_action")
            self.assertEqual(received["action_type"], "pull_desired_state_now")
            self.assertEqual(received["mode"], "dry_run")
            self.assertEqual(received["agent_username"], "channel-agent")
            self.assertIn("seq", received)

            action = await AgentRecoveryAction.objects.afirst()
            self.assertEqual(str(action.correlation_id), received["correlation_id"])
            self.assertEqual(action.status, AgentRecoveryAction.Status.SENT)
            self.assertEqual(action.daemon_id, daemon.pk)
            self.assertIn(f'action_id={action.id}', out.getvalue())
        finally:
            await comm.disconnect()
