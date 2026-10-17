"""
Tests for HIVE-235: Hive↔Fleet-Daemon WebSocket bridge.

Covers (per acceptance criteria):
  1. register/auth — valid token → register_ack; bad token → close_reason auth_failed
  2. snapshot + complete + delta ordering — deltas before snapshot_complete buffered, not applied
  3. resume cursor — reconnect returns resume_from_seq from DB
  4. stickiness — second connection for same daemon_id supersedes the first
  5. heartbeat timeout — missed heartbeats → DaemonRegistry status=degraded / down
  6. backpressure coalesce — _MAX_DELTA_BUFFER exceeded → request_snapshot emitted

Requires: channels[daphne], channels-redis (or InMemoryChannelLayer for tests).

NOTE: these tests use channels.testing.WebsocketCommunicator which runs the
consumer in-process with an InMemoryChannelLayer.  No Redis needed for the
test suite.
"""
import asyncio
import hashlib
import json
from datetime import timedelta
from unittest import mock

from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator
from django.core.cache import cache
from django.test import TransactionTestCase, override_settings
from rest_framework.response import Response

from fleet.consumers import (
    DaemonLinkConsumer,
    _MAX_DELTA_BUFFER,
    _REGISTER_TIMEOUT_S,
)
from fleet.models import DaemonCredential, DaemonRegistry, FleetAgent

# Use in-memory channel layer for tests — no Redis dependency.
TEST_CHANNEL_LAYER = {
    "default": {
        "BACKEND": "channels.layers.InMemoryChannelLayer",
    }
}

_TEST_DAEMON = "daemon-test-001"
_TEST_FLEET = "fleet-test-001"
_TEST_TOKEN = "super-secret-test-token"
_TOKEN_HASH = hashlib.sha256(_TEST_TOKEN.encode()).hexdigest()


def _state_frame(
    agent_id="agent-1",
    username="nagi",
    *,
    model="cortex/DeepSeek-V3.1",
    status="running",
    last_active_at=None,
    lifecycle_state=None,
):
    frame = {
        "type": "state_snapshot",
        "seq": 1,
        "child_id": agent_id,
        "data": {
            "id": agent_id,
            "agent_id": agent_id,
            "child_id": agent_id,
            "username": username,
            "agent_username": username,
            "email": f"{username}@shizuha.com",
            "name": username.title(),
            "display_name": username.title(),
            "role": "Engineer",
            "status": status,
            "enabled": status == "running",
            "lifecycle_state": (
                lifecycle_state
                or ('enabled' if status == 'running' else 'operator_stopped')
            ),
            "config": {
                "execution_method": "shizuha",
                "runtime_environment": "bare_metal",
                "model": model,
                "model_fallbacks": [{"model": model, "method": "shizuha", "reasoningEffort": "medium"}],
                "model_overrides": {"shizuha_reasoning_effort": "medium"},
                "skills": ["pulse-core"],
                "eager_skills": ["connect-messaging"],
                "enabled_mcp_server_ids": ["pulse", "connect"],
                "env": {"PUBLIC_FLAG": "1"},
                "context_prompt": "runtime prompt",
                "personality_traits": {"tone": "direct"},
            },
        },
    }
    if last_active_at is not None:
        frame["data"]["last_active_at"] = last_active_at
    return frame


def _make_app():
    return DaemonLinkConsumer.as_asgi()


def _ensure_test_credential():
    """Make websocket auth setup repeat-safe for ``--keepdb`` gate reruns."""
    DaemonCredential.objects.update_or_create(
        token_hash=_TOKEN_HASH,
        defaults={"daemon_id": _TEST_DAEMON, "revoked": False, "revoked_at": None},
    )


def _create_nagi_desired():
    return FleetAgent.objects.create(
        agent_username="nagi",
        agent_id="agent-1",
        display_name="Nagi",
        role="Engineer",
        model="cortex/DeepSeek-V3.1",
        model_fallbacks=[{
            "model": "cortex/DeepSeek-V3.1",
            "method": "shizuha",
            "reasoningEffort": "medium",
        }],
        model_overrides={"shizuha_reasoning_effort": "medium"},
        mcp_config={
            "execution_method": "shizuha",
            "runtime_environment": "bare_metal",
            "enabled_mcp_server_ids": ["pulse", "connect"],
            "mcp_servers": [{"slug": "pulse", "name": "pulse"}, {"slug": "connect", "name": "connect"}],
        },
        skill_config={
            "skills": ["pulse-core"],
            "eager_skills": ["connect-messaging"],
        },
        env={"PUBLIC_FLAG": "1"},
        context_prompt="runtime prompt",
        personality_traits={"tone": "direct"},
    )


async def _connect_and_register(app, daemon_id=_TEST_DAEMON, token=_TEST_TOKEN, fleet_id=_TEST_FLEET):
    """Helper: connect, send register, return (communicator, register_ack)."""
    comm = WebsocketCommunicator(app, "/v1/fleet/daemon-link")
    connected, _ = await comm.connect()
    assert connected
    await comm.send_json_to({
        "type": "register",
        "daemon_id": daemon_id,
        "fleet_id": fleet_id,
        "runtime": "docker",
        "token": token,
    })
    ack = await comm.receive_json_from()
    return comm, ack


@override_settings(CHANNEL_LAYERS=TEST_CHANNEL_LAYER)
class DaemonAuthTests(TransactionTestCase):
    """Test 1: register / auth."""

    def setUp(self):
        _ensure_test_credential()
        _create_nagi_desired()

    async def test_valid_token_sends_register_ack(self):
        app = _make_app()
        comm, ack = await _connect_and_register(app)
        self.assertEqual(ack["type"], "register_ack")
        self.assertIn("seq", ack)
        await comm.disconnect()

    async def test_invalid_token_sends_close_reason(self):
        app = _make_app()
        comm = WebsocketCommunicator(app, "/v1/fleet/daemon-link")
        connected, _ = await comm.connect()
        self.assertTrue(connected)
        await comm.send_json_to({
            "type": "register",
            "daemon_id": _TEST_DAEMON,
            "fleet_id": _TEST_FLEET,
            "runtime": "docker",
            "token": "wrong-token",
        })
        frame = await comm.receive_json_from()
        self.assertEqual(frame["type"], "close_reason")
        self.assertEqual(frame["code"], "auth_failed")
        await comm.disconnect()

    async def test_missing_daemon_id_sends_close_reason(self):
        app = _make_app()
        comm = WebsocketCommunicator(app, "/v1/fleet/daemon-link")
        connected, _ = await comm.connect()
        self.assertTrue(connected)
        await comm.send_json_to({
            "type": "register",
            "daemon_id": "",
            "token": _TEST_TOKEN,
        })
        frame = await comm.receive_json_from()
        self.assertEqual(frame["type"], "close_reason")
        self.assertEqual(frame["code"], "missing_fields")
        await comm.disconnect()

    async def test_revoked_credential_sends_close_reason(self):
        await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: DaemonCredential.objects.filter(daemon_id=_TEST_DAEMON).update(revoked=True),
        )
        app = _make_app()
        comm = WebsocketCommunicator(app, "/v1/fleet/daemon-link")
        connected, _ = await comm.connect()
        self.assertTrue(connected)
        await comm.send_json_to({
            "type": "register",
            "daemon_id": _TEST_DAEMON,
            "fleet_id": _TEST_FLEET,
            "runtime": "docker",
            "token": _TEST_TOKEN,
        })
        frame = await comm.receive_json_from()
        self.assertEqual(frame["type"], "close_reason")
        self.assertEqual(frame["code"], "auth_failed")
        await comm.disconnect()

    async def test_register_creates_daemon_registry_row(self):
        app = _make_app()
        comm, _ = await _connect_and_register(app)
        exists = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: DaemonRegistry.objects.filter(daemon_id=_TEST_DAEMON).exists(),
        )
        self.assertTrue(exists)
        await comm.disconnect()

    async def test_register_marks_registry_connected(self):
        app = _make_app()
        comm, _ = await _connect_and_register(app)
        reg = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: DaemonRegistry.objects.get(daemon_id=_TEST_DAEMON),
        )
        self.assertEqual(reg.status, "connected")
        await comm.disconnect()

    async def test_register_and_disconnect_are_the_only_projection_lease_writers(self):
        from channels.db import database_sync_to_async as _dsta
        from fleet.mcp_projection import AUTHORITY_UNKNOWN, projection_fingerprint
        from fleet.models import AgentMcpProjectionState

        app = _make_app()
        daemon = await _dsta(DaemonRegistry.objects.update_or_create)(
            daemon_id=_TEST_DAEMON,
            defaults={
                'fleet_id': _TEST_FLEET,
                'status': DaemonRegistry.Status.DISCONNECTED,
            },
        )
        daemon = daemon[0]
        agent = await _dsta(FleetAgent.objects.get)(agent_username='nagi')
        agent.managing_daemon = daemon
        await _dsta(agent.save)(update_fields=['managing_daemon'])
        await _dsta(AgentMcpProjectionState.objects.create)(
            agent=agent,
            generation=1,
            current_high_water=1,
            fingerprint=projection_fingerprint(['connect', 'pulse'], 1),
            authority_state=AUTHORITY_UNKNOWN,
        )

        comm, _ = await _connect_and_register(app)
        state = await _dsta(AgentMcpProjectionState.objects.get)(agent=agent)
        self.assertEqual(state.authority_state, 'current')
        self.assertIsNotNone(state.lease_deadline)

        await comm.disconnect()
        state = await _dsta(AgentMcpProjectionState.objects.get)(agent=agent)
        self.assertEqual(state.authority_state, AUTHORITY_UNKNOWN)
        self.assertIsNone(state.lease_deadline)


@override_settings(CHANNEL_LAYERS=TEST_CHANNEL_LAYER)
class OwnerScopeDerivationTests(TransactionTestCase):
    """SEC-§17 / ADR-0081 Decision #4 (PLAT-7284): owner_subject is derived
    server-side from the authenticated daemon_id (rt-fleet-u<user_id>), never
    trusted from the client frame. A client-declared owner_subject that
    mismatches the derived value is refused (fail-closed) — a compromised
    per-user daemon cannot bind another user's agents (INV-OWNER-SCOPE bypass).
    """

    def setUp(self):
        self._user_token = "user-daemon-token-63"
        DaemonCredential.objects.update_or_create(
            token_hash=hashlib.sha256(self._user_token.encode()).hexdigest(),
            defaults={"daemon_id": "rt-fleet-u63", "revoked": False, "revoked_at": None},
        )

    async def _register(self, app, daemon_id, token, owner_subject=None):
        comm = WebsocketCommunicator(app, "/v1/fleet/daemon-link")
        connected, _ = await comm.connect()
        assert connected
        frame = {
            "type": "register",
            "daemon_id": daemon_id,
            "fleet_id": "fleet-test",
            "runtime": "docker",
            "token": token,
        }
        if owner_subject is not None:
            frame["owner_subject"] = owner_subject
        await comm.send_json_to(frame)
        reply = await comm.receive_json_from()
        return comm, reply

    async def test_mismatched_owner_subject_is_refused(self):
        # The exploit: a daemon with a valid credential for rt-fleet-u63 declares
        # owner_subject=2. Must be refused (no binding).
        app = _make_app()
        comm, reply = await self._register(
            app, "rt-fleet-u63", self._user_token, owner_subject=2,
        )
        self.assertEqual(reply["type"], "close_reason")
        self.assertEqual(reply["code"], "owner_scope_mismatch")
        await comm.disconnect()

    async def test_matching_owner_subject_is_accepted_and_derived(self):
        # A per-user daemon declaring its own owner (63) registers and the
        # registry owner_subject is the derived value.
        app = _make_app()
        comm, ack = await self._register(
            app, "rt-fleet-u63", self._user_token, owner_subject=63,
        )
        self.assertEqual(ack["type"], "register_ack")
        reg = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: DaemonRegistry.objects.get(daemon_id="rt-fleet-u63"),
        )
        self.assertEqual(reg.owner_subject, 63)
        await comm.disconnect()

    async def test_omitted_owner_subject_derives_from_daemon_id(self):
        # A per-user daemon that omits owner_subject still binds to its derived
        # user (63) — the client frame is never the authority.
        app = _make_app()
        comm, ack = await self._register(app, "rt-fleet-u63", self._user_token)
        self.assertEqual(ack["type"], "register_ack")
        reg = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: DaemonRegistry.objects.get(daemon_id="rt-fleet-u63"),
        )
        self.assertEqual(reg.owner_subject, 63)
        await comm.disconnect()

    async def test_org_daemon_claiming_user_owner_is_refused(self):
        # The shared org daemon (rt-fleet-s1, owner_subject=None) must NOT be
        # able to claim a user owner — derived None vs declared 2 → refused.
        await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: DaemonCredential.objects.update_or_create(
                token_hash=hashlib.sha256("org-token".encode()).hexdigest(),
                defaults={"daemon_id": "rt-fleet-s1", "revoked": False, "revoked_at": None},
            ),
        )
        app = _make_app()
        comm, reply = await self._register(
            app, "rt-fleet-s1", "org-token", owner_subject=2,
        )
        self.assertEqual(reply["type"], "close_reason")
        self.assertEqual(reply["code"], "owner_scope_mismatch")
        await comm.disconnect()

    async def test_org_daemon_omitted_owner_registers_null(self):
        # The shared org daemon with no owner_subject still registers and keeps
        # owner_subject NULL (manages owner_id=NULL org/team agents).
        await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: DaemonCredential.objects.update_or_create(
                token_hash=hashlib.sha256("org-token".encode()).hexdigest(),
                defaults={"daemon_id": "rt-fleet-s1", "revoked": False, "revoked_at": None},
            ),
        )
        app = _make_app()
        comm, ack = await self._register(app, "rt-fleet-s1", "org-token")
        self.assertEqual(ack["type"], "register_ack")
        reg = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: DaemonRegistry.objects.get(daemon_id="rt-fleet-s1"),
        )
        self.assertIsNone(reg.owner_subject)
        await comm.disconnect()


@override_settings(CHANNEL_LAYERS=TEST_CHANNEL_LAYER)
class SnapshotDeltaOrderingTests(TransactionTestCase):
    """Test 2: snapshot + complete + delta ordering (anti-CON-108)."""

    def setUp(self):
        _ensure_test_credential()
        _create_nagi_desired()
        FleetAgent.objects.create(agent_username="a1", agent_id="a1")

    async def test_delta_before_snapshot_complete_is_buffered(self):
        """A state_delta received before state_snapshot_complete must NOT be applied."""
        app = _make_app()
        comm, _ = await _connect_and_register(app)

        # Send state_snapshot then a state_delta — no snapshot_complete yet.
        await comm.send_json_to({"type": "state_snapshot", "seq": 1, "data": {}})
        await comm.send_json_to({"type": "state_delta", "seq": 2, "child_id": "agent-1", "data": {}})

        # Should be no ack for state_delta yet (it's buffered).
        self.assertTrue(await comm.receive_nothing(timeout=0.1))
        await comm.disconnect()

    async def test_delta_after_snapshot_complete_is_acked(self):
        """A state_delta after state_snapshot_complete must be acked immediately."""
        app = _make_app()
        comm, _ = await _connect_and_register(app)

        await comm.send_json_to({"type": "state_snapshot", "seq": 1, "data": {}})
        # Complete the snapshot first
        await comm.send_json_to({
            "type": "state_snapshot_complete",
            "seq": 2,
            "child_count": 1,
        })
        ack = await comm.receive_json_from()  # ack for snapshot_complete
        self.assertEqual(ack["type"], "ack")
        self.assertEqual(ack["seq"], 2)

        # Now a delta should be acked right away
        await comm.send_json_to({"type": "state_delta", "seq": 3, "child_id": "agent-1", "data": {}})
        delta_ack = await comm.receive_json_from()
        self.assertEqual(delta_ack["type"], "ack")
        self.assertEqual(delta_ack["seq"], 3)
        await comm.disconnect()

    async def test_buffered_delta_flushed_on_snapshot_complete(self):
        """Deltas buffered pre-complete must be flushed (and acked) after snapshot_complete."""
        app = _make_app()
        comm, _ = await _connect_and_register(app)

        # Buffer a delta before snapshot_complete
        await comm.send_json_to({"type": "state_snapshot", "seq": 1, "data": {}})
        await comm.send_json_to({"type": "state_delta", "seq": 2, "child_id": "a1", "data": {}})

        # Now complete — the buffered delta should be flushed
        await comm.send_json_to({
            "type": "state_snapshot_complete",
            "seq": 3,
            "child_count": 1,
        })
        # Expect ack for snapshot_complete (seq=3)
        ack = await comm.receive_json_from()
        self.assertEqual(ack["type"], "ack")
        self.assertEqual(ack["seq"], 3)
        await comm.disconnect()

    async def test_state_snapshot_updates_only_hive_owned_agent_observation(self):
        """A daemon snapshot updates runtime state for an existing Hive row."""
        from channels.db import database_sync_to_async as _dsta

        app = _make_app()
        comm, _ = await _connect_and_register(app)
        await comm.send_json_to(_state_frame(agent_id="agent-1", username="nagi"))
        await comm.send_json_to({"type": "state_snapshot_complete", "seq": 2, "child_count": 1})
        ack = await comm.receive_json_from()
        self.assertEqual(ack["type"], "ack")

        agent = await _dsta(lambda: FleetAgent.objects.select_related("managing_daemon").get(agent_username="nagi"))()
        self.assertEqual(agent.agent_id, "agent-1")
        self.assertEqual(agent.model, "cortex/DeepSeek-V3.1")
        self.assertEqual(agent.status, FleetAgent.Status.RUNNING)
        self.assertEqual(agent.runtime_lifecycle_state, 'enabled')
        self.assertEqual(agent.managing_daemon.daemon_id, _TEST_DAEMON)
        self.assertEqual(agent.skill_config["skills"], ["pulse-core"])
        self.assertEqual(agent.skill_config["eager_skills"], ["connect-messaging"])
        self.assertEqual(agent.mcp_config["enabled_mcp_server_ids"], ["pulse", "connect"])
        self.assertEqual(agent.env, {"PUBLIC_FLAG": "1"})
        await comm.disconnect()

    async def test_unknown_snapshot_child_is_deleted_only_after_complete_fenced_snapshot(self):
        from channels.db import database_sync_to_async as _dsta

        comm, _ = await _connect_and_register(_make_app())
        await comm.send_json_to(_state_frame(agent_id="ghost-1", username="ghost"))
        await comm.send_json_to({"type": "state_snapshot_complete", "seq": 2, "child_count": 1})
        delete = await comm.receive_json_from()
        self.assertEqual(delete["type"], "delete_agent")
        self.assertEqual(delete["agent_id"], "ghost-1")
        self.assertEqual(delete["reason"], "hive_unknown_after_complete_snapshot")
        ack = await comm.receive_json_from()
        self.assertEqual(ack["type"], "ack")
        exists = await _dsta(FleetAgent.objects.filter(agent_username="ghost").exists)()
        self.assertFalse(exists)
        await comm.disconnect()

    async def test_incomplete_snapshot_never_deletes_unknown_child(self):
        from channels.db import database_sync_to_async as _dsta

        comm, _ = await _connect_and_register(_make_app())
        await comm.send_json_to(_state_frame(agent_id="ghost-1", username="ghost"))
        await comm.send_json_to({"type": "state_snapshot_complete", "seq": 2, "child_count": 2})
        request = await comm.receive_json_from()
        self.assertEqual(request["type"], "request_snapshot")
        self.assertEqual(request["reason"], "snapshot_count_mismatch")
        ack = await comm.receive_json_from()
        self.assertEqual(ack["type"], "ack")
        self.assertTrue(await comm.receive_nothing(timeout=0.1))
        exists = await _dsta(FleetAgent.objects.filter(agent_username="ghost").exists)()
        self.assertFalse(exists)
        await comm.disconnect()

    async def test_real_activity_clock_flows_from_daemon_frame_to_stop_decision(self):
        """PLAT-4490: only explicit daemon workload activity refreshes the
        grace clock; a later running/config frame must not impersonate output."""
        from channels.db import database_sync_to_async as _dsta
        from datetime import datetime, timezone

        from fleet.on_demand import decide_on_demand_actions
        from fleet.runtime_fleet import list_runtime_agents

        def decisions():
            agents = list_runtime_agents(include_disabled=True)
            return agents, decide_on_demand_actions(
                agents=agents,
                rosters=[{
                    "team_name": "customer-support",
                    "is_active": True,
                    "tiers": [["nagi@shizuha.com"]],
                }],
                workload_by_email={},
                workload_by_team={},
                on_demand_teams={"customer-support"},
                warm_teams=set(),
                activity_grace_minutes=30,
            )

        app = _make_app()
        comm, _ = await _connect_and_register(app)
        await comm.send_json_to(_state_frame(
            agent_id="agent-1",
            username="nagi",
            last_active_at="2020-01-01T00:00:00Z",
        ))
        await comm.send_json_to({"type": "state_snapshot_complete", "seq": 2, "child_count": 1})
        await comm.receive_json_from()

        agents, actions = await _dsta(decisions)()
        self.assertEqual(agents[0]["last_active_at"], "2020-01-01T00:00:00")
        self.assertEqual([action.action for action in actions], ["stop"])

        # A normal running/config observation carries no activity timestamp and
        # must leave the old clock unchanged instead of stamping receipt-time.
        status_delta = _state_frame(agent_id="agent-1", username="nagi")
        status_delta.update({"type": "state_delta", "seq": 3})
        status_delta["data"]["reason"] = "config_applied"
        await comm.send_json_to(status_delta)
        await comm.receive_json_from()
        agents, actions = await _dsta(decisions)()
        self.assertEqual(agents[0]["last_active_at"], "2020-01-01T00:00:00")
        self.assertEqual([action.action for action in actions], ["stop"])

        # A real daemon activity delta is persisted into the WS cache and blocks
        # stop through the actual reconcile policy input.
        # The daemon protocol emits UTC ISO-8601. Do not use Django's
        # timezone.now() here: Hive intentionally runs USE_TZ=False and that
        # helper returns a naive TIME_ZONE wall clock, not the wire contract.
        recent = datetime.now(timezone.utc).isoformat()
        activity_delta = _state_frame(
            agent_id="agent-1",
            username="nagi",
            last_active_at=recent,
        )
        activity_delta.update({"type": "state_delta", "seq": 4})
        activity_delta["data"]["reason"] = "activity"
        await comm.send_json_to(activity_delta)
        await comm.receive_json_from()
        agents, actions = await _dsta(decisions)()
        # THE SUBJECT OF THIS TEST: the activity delta is persisted and reaches
        # the policy input, while the earlier config frame did not stamp
        # receipt-time over it.
        self.assertEqual(
            agents[0]["last_active_at"],
            datetime.fromisoformat(recent).replace(tzinfo=None).isoformat(),
        )
        # A fresh clock is necessary but NO LONGER SUFFICIENT to hold the seat.
        # A heartbeat refreshes last_active_at whether or not the turn did any
        # work, so grace now also needs evidence of work — an explicit
        # activity_state or a productive heartbeat_outcome, both of which are
        # projected from gateway telemetry (external_agents.telemetry_fields),
        # not from this daemon frame. banto sat Running with an empty queue for
        # exactly this reason (operator 2026-08-05).
        self.assertEqual([action.action for action in actions], ["stop"])
        # ...and with that evidence present, the same fresh clock does hold it.
        held = await _dsta(lambda: decide_on_demand_actions(
            agents=[{**agents[0], "activity_state": "working"}],
            rosters=[{
                "team_name": "customer-support",
                "is_active": True,
                "tiers": [["nagi@shizuha.com"]],
            }],
            workload_by_email={},
            workload_by_team={},
            activity_grace_minutes=30,
        ))()
        self.assertEqual(held, [])
        await comm.disconnect()

    async def test_state_delta_updates_fleet_agent_cache(self):
        """Runtime observations never overwrite Hive's desired model."""
        from channels.db import database_sync_to_async as _dsta

        app = _make_app()
        comm, _ = await _connect_and_register(app)
        await comm.send_json_to(_state_frame(agent_id="agent-1", username="nagi"))
        await comm.send_json_to({"type": "state_snapshot_complete", "seq": 2, "child_count": 1})
        await comm.receive_json_from()

        # 1) Plain runtime delta: the model change is IGNORED (Hive SSOT holds).
        delta = _state_frame(agent_id="agent-1", username="nagi", model="claude-sonnet-4-6")
        delta["type"] = "state_delta"
        delta["seq"] = 3
        await comm.send_json_to(delta)
        delta_ack = await comm.receive_json_from()
        self.assertEqual(delta_ack["type"], "ack")
        self.assertEqual(delta_ack["seq"], 3)
        model = await _dsta(lambda: FleetAgent.objects.get(agent_username="nagi").model)()
        self.assertEqual(model, "cortex/DeepSeek-V3.1")

        # 2) A config-applied observation is also non-authoritative. Exact
        # fingerprints/value matching confirm delivery without rewriting SoT.
        applied = _state_frame(agent_id="agent-1", username="nagi", model="claude-sonnet-4-6")
        applied["type"] = "state_delta"
        applied["seq"] = 4
        applied["data"]["reason"] = "config_applied"
        await comm.send_json_to(applied)
        applied_ack = await comm.receive_json_from()
        self.assertEqual(applied_ack["seq"], 4)
        model = await _dsta(lambda: FleetAgent.objects.get(agent_username="nagi").model)()
        self.assertEqual(model, "cortex/DeepSeek-V3.1")
        await comm.disconnect()

    async def test_two_top_level_pushes_fence_delayed_config_applied_revert(self):
        """HIVE-752 production order: push A, push B, then delayed A observe."""
        from channels.db import database_sync_to_async as _dsta
        from fleet.runtime_fleet import configure_runtime_agent

        comm, _ = await _connect_and_register(_make_app())
        await comm.send_json_to(_state_frame(agent_id="agent-1", username="nagi"))
        await comm.send_json_to({"type": "state_snapshot_complete", "seq": 2, "child_count": 1})
        await comm.receive_json_from()

        async def push(model):
            ok, detail = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: configure_runtime_agent("agent-1", {"model": model}),
            )
            self.assertTrue(ok, detail)
            self.assertIn("awaiting daemon config_result", detail)
            return await comm.receive_json_from(timeout=1)

        push_a = await push("cortex/DeepSeek-V4-Flash")
        push_b = await push("gpt-5.6-sol")

        # B is confirmed first, so pending is empty before delayed A's legacy
        # state_delta arrives — the exact window that used to revert the row.
        await comm.send_json_to({
            "type": "config_result",
            "seq": 3,
            "agent_id": "agent-1",
            "ok": True,
            "config_fingerprints": push_b["config_fingerprints"],
        })
        stale_a = _state_frame(
            agent_id="agent-1", username="nagi", model="cortex/DeepSeek-V4-Flash",
        )
        stale_a.update({"type": "state_delta", "seq": 4})
        stale_a["data"]["reason"] = "config_applied"
        await comm.send_json_to(stale_a)
        ack = await comm.receive_json_from()
        self.assertEqual(ack["seq"], 4)

        fresh = await _dsta(lambda: FleetAgent.objects.get(agent_username="nagi"))()
        self.assertEqual(fresh.model, "gpt-5.6-sol")
        self.assertEqual(fresh.pending_config, {})
        self.assertNotEqual(push_a["config_fingerprints"], push_b["config_fingerprints"])
        await comm.disconnect()

    async def test_heartbeat_retries_unacknowledged_pending_config(self):
        """A lost live group_send is re-driven without waiting for reconnect."""
        from fleet.runtime_fleet import configure_runtime_agent

        comm, _ = await _connect_and_register(_make_app())
        await comm.send_json_to(_state_frame(agent_id="agent-1", username="nagi"))
        await comm.send_json_to({"type": "state_snapshot_complete", "seq": 2, "child_count": 1})
        await comm.receive_json_from()

        ok, detail = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: configure_runtime_agent("agent-1", {"skills": ["pulse-core", "wiki-lifecycle"]}),
        )
        self.assertTrue(ok, detail)
        initial = await comm.receive_json_from(timeout=1)

        await comm.send_json_to({"type": "heartbeat", "seq": 3})
        heartbeat_ack = await comm.receive_json_from()
        retry = await comm.receive_json_from()
        self.assertEqual(heartbeat_ack, {"type": "ack", "seq": 3})
        self.assertEqual(retry["reason"], "hive_pending_config_retry")
        self.assertEqual(retry["config"], initial["config"])
        self.assertEqual(retry["config_fingerprints"], initial["config_fingerprints"])
        await comm.disconnect()

    async def test_heartbeat_with_boolean_pending_config_acks_and_continues(self):
        """Legacy scalar row → heartbeat ack → next heartbeat ack, no disconnect."""
        from channels.db import database_sync_to_async as _dsta

        comm, _ = await _connect_and_register(_make_app())
        await comm.send_json_to(_state_frame(agent_id="agent-1", username="nagi"))
        await comm.send_json_to({"type": "state_snapshot_complete", "seq": 2, "child_count": 1})
        await comm.receive_json_from(timeout=10)

        # QuerySet.update deliberately models a pre-contract/bypassed legacy
        # writer. Current FleetAgent.save() rejects this value.
        await _dsta(FleetAgent.objects.filter(agent_username="nagi").update)(
            pending_config=True,
        )
        with self.assertLogs("fleet.pending_config", level="ERROR") as captured:
            await comm.send_json_to({"type": "heartbeat", "seq": 3})
            self.assertEqual(
                await comm.receive_json_from(timeout=10),
                {"type": "ack", "seq": 3},
            )
            # The consumer processes frames serially. Receiving seq=4's ack
            # proves seq=3 completed its retry readers without disconnecting.
            await comm.send_json_to({"type": "heartbeat", "seq": 4})
            self.assertEqual(
                await comm.receive_json_from(timeout=10),
                {"type": "ack", "seq": 4},
            )

        self.assertIn("value_type=bool", "\n".join(captured.output))
        await comm.disconnect()

    async def test_configure_runtime_agent_sends_daemonlink_config_frame(self):
        """Hive config edits flow down over the daemon-link WS, not the dead HTTP poll."""
        from channels.db import database_sync_to_async as _dsta
        from fleet.runtime_fleet import configure_runtime_agent

        app = _make_app()
        comm, _ = await _connect_and_register(app)
        await comm.send_json_to(_state_frame(agent_id="agent-1", username="nagi"))
        await comm.send_json_to({"type": "state_snapshot_complete", "seq": 2, "child_count": 1})
        await comm.receive_json_from()

        ok, err = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: configure_runtime_agent("agent-1", {"skills": ["pulse-core", "wiki-lifecycle"]}),
        )
        self.assertTrue(ok, err)
        pushed = await comm.receive_json_from(timeout=1)
        self.assertEqual(pushed["type"], "config_update")
        self.assertEqual(pushed["agent_id"], "agent-1")
        self.assertEqual(pushed["config"], {"skills": ["pulse-core", "wiki-lifecycle"]})
        from fleet.config_fingerprints import config_value_fingerprint
        self.assertEqual(
            pushed["config_fingerprints"],
            {"skills": config_value_fingerprint(["pulse-core", "wiki-lifecycle"])},
        )
        skill_config = await _dsta(lambda: FleetAgent.objects.get(agent_username="nagi").skill_config)()
        self.assertEqual(skill_config["skills"], ["pulse-core", "wiki-lifecycle"])
        await comm.disconnect()

    async def test_config_result_fingerprint_clears_redacted_env_pending_immediately(self):
        """A successful apply must converge even when observed env is redacted.

        ``TOKEN`` appears in this non-secret tuning key, which caused the daemon
        observation to redact it and left the Agents UI syncing forever.
        """
        from channels.db import database_sync_to_async as _dsta
        from fleet.config_fingerprints import config_value_fingerprint

        comm, _ = await _connect_and_register(_make_app())
        await comm.send_json_to(_state_frame(agent_id="agent-1", username="nagi"))
        await comm.send_json_to({"type": "state_snapshot_complete", "seq": 2, "child_count": 1})
        await comm.receive_json_from()
        desired_env = {
            "PUBLIC_FLAG": "1",
            "SHIZUHA_CORTEX_COMPACTION_TRIGGER_TOKENS": "160000",
        }

        def set_pending():
            agent = FleetAgent.objects.get(agent_username="nagi")
            agent.env = desired_env
            agent.pending_config = {"env": desired_env}
            agent.save(update_fields=["env", "pending_config", "updated_at"])
        await _dsta(set_pending)()

        await comm.send_json_to({
            "type": "config_result",
            "seq": 3,
            "agent_id": "agent-1",
            "ok": True,
            "config_fingerprints": {"env": config_value_fingerprint(desired_env)},
        })
        get_pending = _dsta(
            lambda: FleetAgent.objects.get(agent_username="nagi").pending_config,
        )
        for _ in range(20):
            pending = await get_pending()
            if not pending:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(pending, {})
        await comm.disconnect()

    async def test_config_result_fingerprint_cannot_clear_newer_pending_edit(self):
        """A delayed result for push A cannot clear a newer push B."""
        from channels.db import database_sync_to_async as _dsta
        from fleet.config_fingerprints import config_value_fingerprint

        comm, _ = await _connect_and_register(_make_app())
        await comm.send_json_to(_state_frame(agent_id="agent-1", username="nagi"))
        await comm.send_json_to({"type": "state_snapshot_complete", "seq": 2, "child_count": 1})
        await comm.receive_json_from()
        old_env = {"SHIZUHA_CORTEX_COMPACTION_TRIGGER_TOKENS": "120000"}
        new_env = {"SHIZUHA_CORTEX_COMPACTION_TRIGGER_TOKENS": "160000"}
        await _dsta(
            lambda: FleetAgent.objects.filter(agent_username="nagi").update(
                env=new_env, pending_config={"env": new_env},
            )
        )()

        await comm.send_json_to({
            "type": "config_result",
            "seq": 3,
            "agent_id": "agent-1",
            "ok": True,
            "config_fingerprints": {"env": config_value_fingerprint(old_env)},
        })
        get_state = _dsta(
            lambda: (
                FleetAgent.objects.get(agent_username="nagi").pending_config,
                DaemonRegistry.objects.get(daemon_id=_TEST_DAEMON).last_acked_seq,
            ),
        )
        for _ in range(20):
            pending, cursor = await get_state()
            if cursor >= 3:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(pending, {"env": new_env})
        await comm.disconnect()

    async def test_failed_config_result_never_clears_matching_pending_value(self):
        from channels.db import database_sync_to_async as _dsta
        from fleet.config_fingerprints import config_value_fingerprint

        comm, _ = await _connect_and_register(_make_app())
        await comm.send_json_to(_state_frame(agent_id="agent-1", username="nagi"))
        await comm.send_json_to({"type": "state_snapshot_complete", "seq": 2, "child_count": 1})
        await comm.receive_json_from()
        desired_env = {"SHIZUHA_CORTEX_COMPACTION_TRIGGER_TOKENS": "160000"}
        await _dsta(
            lambda: FleetAgent.objects.filter(agent_username="nagi").update(
                env=desired_env, pending_config={"env": desired_env},
            )
        )()

        await comm.send_json_to({
            "type": "config_result",
            "seq": 3,
            "agent_id": "agent-1",
            "ok": False,
            "error": "state store unavailable",
            "config_fingerprints": {"env": config_value_fingerprint(desired_env)},
        })
        get_state = _dsta(
            lambda: (
                FleetAgent.objects.get(agent_username="nagi").pending_config,
                DaemonRegistry.objects.get(daemon_id=_TEST_DAEMON).last_acked_seq,
            ),
        )
        for _ in range(20):
            pending, cursor = await get_state()
            if cursor >= 3:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(pending, {"env": desired_env})
        await comm.disconnect()

    async def test_reconnect_snapshot_does_not_clobber_hive_desired_config(self):
        """After the one-time seed, a stale reconnect snapshot cannot overwrite Hive desired config."""
        from channels.db import database_sync_to_async as _dsta
        from django.utils import timezone as tz

        def setup_seeded_row():
            daemon = DaemonRegistry.objects.create(
                daemon_id=_TEST_DAEMON,
                fleet_id=_TEST_FLEET,
                config_seeded_at=tz.now(),
            )
            agent = FleetAgent.objects.get(agent_username="nagi")
            agent.model = "cortex/HiveDesired"
            agent.model_fallbacks = [{"model": "cortex/HiveDesired", "method": "shizuha"}]
            agent.mcp_config = {"execution_method": "shizuha", "runtime_environment": "k8s"}
            agent.managing_daemon = daemon
            agent.save()
            return str(agent.id)

        await _dsta(setup_seeded_row)()
        app = _make_app()
        comm, ack = await _connect_and_register(app)
        self.assertFalse(ack["seed_required"])
        pushed = await comm.receive_json_from(timeout=1)
        self.assertEqual(pushed["type"], "config_update")
        self.assertEqual(pushed["config"]["model"], "cortex/HiveDesired")
        snapshot_request = await comm.receive_json_from(timeout=1)
        self.assertEqual(snapshot_request["type"], "request_snapshot")

        await comm.send_json_to(_state_frame(agent_id="agent-1", username="nagi", model="cortex/StaleDaemon"))
        await comm.send_json_to({"type": "state_snapshot_complete", "seq": 2, "child_count": 1})
        await comm.receive_json_from()

        agent = await _dsta(lambda: FleetAgent.objects.get(agent_username="nagi"))()
        self.assertEqual(agent.model, "cortex/HiveDesired")
        self.assertEqual(agent.status, FleetAgent.Status.RUNNING)
        await comm.disconnect()

    async def test_reconnect_replays_deleted_agent_tombstone(self):
        """A Hive-deleted row must drive daemon deletion on every reconnect."""
        from channels.db import database_sync_to_async as _dsta
        from django.utils import timezone as tz

        def setup_deleted_row():
            daemon = DaemonRegistry.objects.create(
                daemon_id=_TEST_DAEMON,
                fleet_id=_TEST_FLEET,
                config_seeded_at=tz.now(),
            )
            FleetAgent.objects.create(
                agent_username='shizuha-3',
                agent_id='runtime-shizuha-3',
                managing_daemon=daemon,
                status=FleetAgent.Status.DELETED,
            )

        await _dsta(setup_deleted_row)()
        comm, ack = await _connect_and_register(_make_app())
        self.assertFalse(ack['seed_required'])
        frame = await comm.receive_json_from(timeout=1)
        self.assertEqual(frame['type'], 'delete_agent')
        self.assertEqual(frame['agent_id'], 'runtime-shizuha-3')
        self.assertEqual(frame['reason'], 'hive_desired_deleted_on_reconnect')
        await comm.disconnect()

    async def test_stale_daemon_snapshot_cannot_resurrect_hive_tombstone(self):
        from channels.db import database_sync_to_async as _dsta

        def setup_deleted_row():
            daemon = DaemonRegistry.objects.create(
                daemon_id=_TEST_DAEMON, fleet_id=_TEST_FLEET,
            )
            FleetAgent.objects.create(
                agent_username='shizuha-3',
                agent_id='runtime-shizuha-3',
                managing_daemon=daemon,
                status=FleetAgent.Status.DELETED,
            )

        await _dsta(setup_deleted_row)()
        comm, _ = await _connect_and_register(_make_app())
        await comm.send_json_to(_state_frame(
            agent_id='runtime-shizuha-3', username='shizuha-3', status='running'))
        await comm.send_json_to({'type': 'state_snapshot_complete', 'seq': 2, 'child_count': 1})
        await comm.receive_json_from()
        status_value = await _dsta(
            lambda: FleetAgent.objects.get(agent_username='shizuha-3').status)()
        self.assertEqual(status_value, FleetAgent.Status.DELETED)
        await comm.disconnect()

    async def test_initial_snapshot_rejects_unknown_child_and_marks_registry_seeded(self):
        """First connect observes daemon state but never creates Hive desired rows."""
        from channels.db import database_sync_to_async as _dsta

        await _dsta(FleetAgent.objects.all().delete)()
        app = _make_app()
        comm, ack = await _connect_and_register(app)
        self.assertTrue(ack["seed_required"])
        await comm.send_json_to(_state_frame(agent_id="agent-1", username="nagi", model="cortex/DaemonSeed"))
        await comm.send_json_to({"type": "state_snapshot_complete", "seq": 2, "child_count": 1})
        delete = await comm.receive_json_from()
        self.assertEqual(delete["type"], "delete_agent")
        self.assertEqual(delete["agent_id"], "agent-1")
        self.assertEqual(delete["reason"], "hive_unknown_after_complete_snapshot")
        await comm.receive_json_from()

        def read_state():
            return (
                FleetAgent.objects.filter(agent_username="nagi").exists(),
                DaemonRegistry.objects.get(daemon_id=_TEST_DAEMON).config_seeded_at is not None,
            )

        exists, seeded = await _dsta(read_state)()
        self.assertFalse(exists)
        self.assertTrue(seeded)
        await comm.disconnect()


@override_settings(CHANNEL_LAYERS=TEST_CHANNEL_LAYER)
class ResumeCursorTests(TransactionTestCase):
    """Test 3: resume cursor (anti-CON-108)."""

    def setUp(self):
        _ensure_test_credential()
        _create_nagi_desired()

    async def test_reconnect_returns_resume_from_seq(self):
        """After disconnect, a reconnect register_ack must carry resume_from_seq."""
        # Pre-plant a DaemonRegistry with a known last_acked_seq.
        def setup_db():
            reg, _ = DaemonRegistry.objects.get_or_create(
                daemon_id=_TEST_DAEMON,
                defaults={"fleet_id": _TEST_FLEET, "last_acked_seq": 42},
            )
            reg.last_acked_seq = 42
            reg.save(update_fields=["last_acked_seq"])
        await asyncio.get_running_loop().run_in_executor(None, setup_db)

        app = _make_app()
        comm, ack = await _connect_and_register(app)
        self.assertEqual(ack["type"], "register_ack")
        self.assertEqual(ack.get("resume_from_seq"), 42)
        await comm.disconnect()

    async def test_no_resume_seq_on_first_connect(self):
        """First connection (last_acked_seq=0) must NOT send resume_from_seq."""
        app = _make_app()
        comm, ack = await _connect_and_register(app)
        self.assertEqual(ack["type"], "register_ack")
        self.assertNotIn("resume_from_seq", ack)
        await comm.disconnect()

    async def test_ack_frame_advances_cursor(self):
        """An ack frame from the daemon must advance last_acked_seq in DB."""
        app = _make_app()
        comm, _ = await _connect_and_register(app)

        # Send snapshot_complete so we're in normal operation
        await comm.send_json_to({
            "type": "state_snapshot_complete", "seq": 10, "child_count": 0,
        })
        await comm.receive_json_from()  # consume the ack

        # Daemon sends ack for seq 10; consumer awaits _db_advance_cursor (database_sync_to_async)
        await comm.send_json_to({"type": "ack", "seq": 10})
        # Yield then poll until the DB write propagates (avoids flaky fixed sleep on slow CI).
        from channels.db import database_sync_to_async as _dsta
        get_cursor = _dsta(lambda: DaemonRegistry.objects.get(daemon_id=_TEST_DAEMON).last_acked_seq)
        for _ in range(20):
            await asyncio.sleep(0.05)
            cursor = await get_cursor()
            if cursor == 10:
                break
        self.assertEqual(cursor, 10)
        await comm.disconnect()

    async def test_cursor_never_goes_backwards(self):
        """An ack with a lower seq than the current cursor must be ignored."""
        def seed():
            DaemonRegistry.objects.get_or_create(
                daemon_id=_TEST_DAEMON,
                defaults={"fleet_id": _TEST_FLEET, "last_acked_seq": 50},
            )
        await asyncio.get_running_loop().run_in_executor(None, seed)

        app = _make_app()
        comm, _ = await _connect_and_register(app)
        # Daemon sends an out-of-order ack with lower seq
        await comm.send_json_to({"type": "ack", "seq": 5})
        # Brief poll to let the consumer process the ack (avoids fixed sleep on slow CI).
        from channels.db import database_sync_to_async as _dsta
        get_cursor = _dsta(lambda: DaemonRegistry.objects.get(daemon_id=_TEST_DAEMON).last_acked_seq)
        for _ in range(10):
            await asyncio.sleep(0.05)
            cursor = await get_cursor()
            if cursor != 50:
                break  # unexpected change — let assertion catch it
        self.assertEqual(cursor, 50)  # unchanged
        await comm.disconnect()


@override_settings(CHANNEL_LAYERS=TEST_CHANNEL_LAYER)
class StickinessTests(TransactionTestCase):
    """Test 4: stickiness — second connection supersedes the first."""

    def setUp(self):
        _ensure_test_credential()

    async def test_second_connection_supersedes_first(self):
        """When a second connection registers for the same daemon_id, the first is closed."""
        app = _make_app()
        comm1, _ = await _connect_and_register(app)

        # Second connection for the same daemon_id
        comm2, ack2 = await _connect_and_register(app)
        self.assertEqual(ack2["type"], "register_ack")

        # First connection should receive a close_reason: superseded
        # (via the group_send "daemon.supersede" message)
        frame = await comm1.receive_json_from(timeout=1)
        self.assertEqual(frame["type"], "close_reason")
        self.assertEqual(frame["code"], "superseded")

        await comm1.disconnect()
        await comm2.disconnect()


@override_settings(CHANNEL_LAYERS=TEST_CHANNEL_LAYER)
class HeartbeatHealthTests(TransactionTestCase):
    """Test 5: missed heartbeats → DaemonRegistry status=degraded / down."""

    def setUp(self):
        _ensure_test_credential()

    async def test_superseded_channel_late_loop_and_heartbeat_are_inert(self):
        """Production order A registers, B supersedes, then late A work runs.

        The database ownership transition can precede delivery of the
        channel-layer supersede event.  In that window A must not send or log a
        health heartbeat, change B's CONNECTED status/last_seen, or replace B's
        observed runtime report.
        """
        from channels.db import database_sync_to_async as _dsta
        from django.utils import timezone
        from fleet import consumers as consumers_mod

        async def register(consumer, channel_name, report):
            consumer._daemon_id = _TEST_DAEMON
            consumer._channel_name_at_register = channel_name
            registry = await consumer._db_upsert_registry(
                _TEST_DAEMON,
                _TEST_FLEET,
                "k8s",
                "test-credential",
                channel_name,
                harness_report=report,
            )
            consumer._registry_pk = registry["pk"]

        channel_a = DaemonLinkConsumer()
        channel_b = DaemonLinkConsumer()
        report_a = {"agent_runtime_image": "runtime:a"}
        report_b = {"agent_runtime_image": "runtime:b"}
        await register(channel_a, "channel-a", report_a)
        await register(channel_b, "channel-b", report_b)

        def read_registry():
            row = DaemonRegistry.objects.get(daemon_id=_TEST_DAEMON)
            return {
                "status": row.status,
                "last_seen": row.last_seen,
                "ws_channel": row.ws_channel,
                "harness_report": row.harness_report,
                "agent_runtime_image": row.agent_runtime_image,
            }

        after_b = await _dsta(read_registry)()
        self.assertEqual(after_b["ws_channel"], "channel-b")
        self.assertEqual(after_b["status"], DaemonRegistry.Status.CONNECTED)

        channel_a._last_daemon_hb = 0
        channel_a.send_json = mock.AsyncMock()
        with mock.patch.object(consumers_mod.asyncio, "sleep", new=mock.AsyncMock()):
            with self.assertNoLogs("fleet.consumers", level="WARNING"):
                await channel_a._heartbeat_loop()
        channel_a.send_json.assert_not_awaited()

        # A stale inbound heartbeat is another async continuation of the same
        # superseded lifecycle.  It must be dropped before touching the row or
        # acknowledging/retrying work on the old socket.
        stale_heartbeat_time = after_b["last_seen"] + timedelta(hours=1)
        with mock.patch.object(timezone, "now", return_value=stale_heartbeat_time):
            await channel_a._handle_heartbeat({
                "type": "heartbeat",
                "seq": 91,
                "harness_report": {"agent_runtime_image": "runtime:stale-a"},
            })
        channel_a.send_json.assert_not_awaited()

        # The general inbound-activity liveness path is fenced too.  A late
        # non-heartbeat response from A must return before its handler and must
        # not refresh or revive the registry row now owned by B.
        with mock.patch.object(timezone, "now", return_value=stale_heartbeat_time):
            await channel_a.receive_json({
                "type": "config_result",
                "seq": 92,
                "agent_id": "stale-config-result",
                "config_fingerprints": {},
            })
        channel_a.send_json.assert_not_awaited()

        final = await _dsta(read_registry)()
        self.assertEqual(final, after_b)

    async def test_superseded_heartbeat_cannot_claim_runtime_lane_retry(self):
        """A register → B supersede → late A cannot claim B's retry work."""
        from channels.db import database_sync_to_async as _dsta

        channel_a = DaemonLinkConsumer()
        channel_a._daemon_id = "daemon-stale-retry"
        channel_a._channel_name_at_register = "channel-a"
        registry_a = await channel_a._db_upsert_registry(
            channel_a._daemon_id,
            "fleet-stale-retry",
            "k8s",
            "test-credential",
            channel_a._channel_name_at_register,
        )
        channel_a._registry_pk = registry_a["pk"]

        channel_b = DaemonLinkConsumer()
        channel_b._daemon_id = channel_a._daemon_id
        channel_b._channel_name_at_register = "channel-b"
        registry_b = await channel_b._db_upsert_registry(
            channel_b._daemon_id,
            "fleet-stale-retry",
            "k8s",
            "test-credential",
            channel_b._channel_name_at_register,
        )
        channel_b._registry_pk = registry_b["pk"]
        self.assertEqual(channel_b._registry_pk, channel_a._registry_pk)

        def seed_retry():
            from django.utils import timezone
            from fleet.models import FleetAgent, RuntimeChange, RuntimeLaneOutbox

            registry = DaemonRegistry.objects.get(pk=channel_b._registry_pk)
            agent = FleetAgent.objects.create(
                agent_username="stale-retry",
                agent_id="stale-retry",
                model="gpt-5.6-sol",
                mcp_config={"execution_method": "codex_app_server"},
                managing_daemon=registry,
            )
            change = RuntimeChange.objects.create(
                agent=agent,
                base_generation=0,
                desired_generation=1,
                resolved_lane={"model": "gpt-5.6-sol"},
                previous_lane={},
                registry_version="v1",
                runtime_release_id="scli-1",
                lane_digest="a" * 64,
                state=RuntimeChange.State.ACTUATOR_PENDING,
            )
            outbox = RuntimeLaneOutbox.objects.create(
                change=change,
                payload={"type": "config_update", "agent_id": agent.agent_id},
                attempt_count=4,
                last_attempt_at=timezone.now() - timedelta(seconds=10),
                last_error="previous failure",
            )
            return outbox.pk

        outbox_pk = await _dsta(seed_retry)()

        self.assertEqual(await channel_a._db_retryable_runtime_lane_frames(), [])

        def read_attempt():
            from fleet.models import RuntimeLaneOutbox
            row = RuntimeLaneOutbox.objects.get(pk=outbox_pk)
            return row.attempt_count, row.last_error

        self.assertEqual(
            await _dsta(read_attempt)(),
            (4, "previous failure"),
        )

    async def test_missed_heartbeats_degrade_then_down(self):
        """Observe actual health writes at controlled source-clock boundaries."""
        import fleet.consumers as consumers_mod
        from channels.db import database_sync_to_async as _dsta
        from types import SimpleNamespace

        channel = DaemonLinkConsumer()
        channel._daemon_id = _TEST_DAEMON
        channel._channel_name_at_register = 'health-clock-channel'
        registry = await channel._db_upsert_registry(
            _TEST_DAEMON, _TEST_FLEET, 'k8s', 'test-credential',
            channel._channel_name_at_register,
        )
        channel._registry_pk = registry['pk']
        channel.send_json = mock.AsyncMock()
        get_status = _dsta(
            lambda: DaemonRegistry.objects.get(daemon_id=_TEST_DAEMON).status
        )
        interval = consumers_mod._HEARTBEAT_INTERVAL_S
        clock = [100.0]
        channel._last_daemon_hb = clock[0]
        # Replace only this source module's clock, never asyncio's own clock.
        # Database/scheduler delays cannot make a poll miss the degraded state.
        with mock.patch.object(consumers_mod, 'time',
                               SimpleNamespace(monotonic=lambda: clock[0])):
            for cycle in range(2):
                baseline = clock[0]
                for missed, expected in (
                    (consumers_mod._MISS_DEGRADED - 1, 'connected'),
                    (consumers_mod._MISS_DEGRADED, 'degraded'),
                    (consumers_mod._MISS_DOWN - 1, 'degraded'),
                    (consumers_mod._MISS_DOWN, 'down'),
                ):
                    clock[0] = baseline + missed * interval
                    self.assertTrue(await channel._heartbeat_tick())
                    self.assertEqual(await get_status(), expected,
                                     (cycle, missed))
                    self.assertEqual(channel.send_json.await_args.args[0]['type'],
                                     'heartbeat')
                # Actual inbound handling resets liveness and rearms the same
                # consumer; a later lapse must still degrade and go down.
                clock[0] += interval
                await channel.receive_json({'type': 'heartbeat', 'seq': cycle + 1})
                self.assertEqual(await get_status(), 'connected')
                self.assertEqual(channel.send_json.await_args.args[0],
                                 {'type': 'ack', 'seq': cycle + 1})
                self.assertTrue(await channel._heartbeat_tick())
                self.assertEqual(await get_status(), 'connected')

    async def test_heartbeat_resets_last_seen(self):
        """A daemon heartbeat frame updates last_seen in DaemonRegistry."""
        from channels.db import database_sync_to_async as _dsta

        app = _make_app()
        comm, _ = await _connect_and_register(app)

        await comm.send_json_to({"type": "heartbeat", "seq": 1})
        # Expect an ack back for the heartbeat
        ack = await comm.receive_json_from(timeout=1)
        self.assertEqual(ack["type"], "ack")
        self.assertEqual(ack["seq"], 1)

        get_last_seen = _dsta(lambda: DaemonRegistry.objects.get(daemon_id=_TEST_DAEMON).last_seen)
        last_seen = await get_last_seen()
        self.assertIsNotNone(last_seen)
        await comm.disconnect()

    async def test_config_result_stream_keeps_busy_daemon_connected(self):
        """Inbound work proves liveness while an explicit heartbeat is delayed.

        Production reconnect order is register -> desired config replay -> one
        config_result per agent.  Applying a large replay can exceed five
        heartbeat intervals, so counting only heartbeat frames falsely marked
        the actively responding daemon DOWN.
        """
        import fleet.consumers as consumers_mod
        from channels.db import database_sync_to_async as _dsta

        original_interval = consumers_mod._HEARTBEAT_INTERVAL_S
        consumers_mod._HEARTBEAT_INTERVAL_S = 0.05

        try:
            channel = DaemonLinkConsumer()
            channel._daemon_id = _TEST_DAEMON
            channel._channel_name_at_register = "busy-config-channel"
            registry = await channel._db_upsert_registry(
                _TEST_DAEMON,
                _TEST_FLEET,
                "k8s",
                "test-credential",
                channel._channel_name_at_register,
            )
            channel._registry_pk = registry["pk"]
            channel.send_json = mock.AsyncMock()
            get_registry = _dsta(
                lambda: DaemonRegistry.objects.get(daemon_id=_TEST_DAEMON)
            )
            before = await get_registry()

            # Keep the socket busy beyond the DOWN threshold (5 * 0.05s)
            # without an explicit daemon heartbeat. A deterministic monotonic
            # clock keeps this production-order regression fast and bounded.
            clock = [100.0]
            channel._last_daemon_hb = clock[0]
            with mock.patch.object(
                consumers_mod.time, "monotonic", side_effect=lambda: clock[0]
            ):
                for seq in range(1, 9):
                    clock[0] += 0.04
                    await channel.receive_json({
                        "type": "config_result",
                        "seq": seq,
                        "agent_id": "reconnect-config-result",
                        "ok": True,
                        "config_fingerprints": {},
                    })
                    clock[0] += 0.01
                    self.assertTrue(await channel._heartbeat_tick())

            after = await get_registry()
            self.assertEqual(after.status, DaemonRegistry.Status.CONNECTED)
            self.assertGreater(after.last_seen, before.last_seen)
        finally:
            consumers_mod._HEARTBEAT_INTERVAL_S = original_interval

    async def test_heartbeat_retries_pending_runtime_lane_without_reconnect(self):
        """A transient initial send failure is retried on the live socket."""
        from channels.db import database_sync_to_async as _dsta

        app = _make_app()
        comm, _ = await _connect_and_register(app)

        def create_pending():
            from datetime import timedelta
            from django.utils import timezone
            from fleet.models import FleetAgent, RuntimeChange, RuntimeLaneOutbox

            daemon = DaemonRegistry.objects.get(daemon_id=_TEST_DAEMON)
            agent = FleetAgent.objects.create(
                agent_username='lane-retry', agent_id='lane-retry',
                model='gpt-5.6-sol', mcp_config={'execution_method': 'codex_app_server'},
                managing_daemon=daemon,
            )
            change = RuntimeChange.objects.create(
                agent=agent, base_generation=0, desired_generation=1,
                resolved_lane={'model': 'gpt-5.6-sol'}, previous_lane={},
                registry_version='v1', runtime_release_id='scli-1',
                lane_digest='a' * 64, state=RuntimeChange.State.ACTUATOR_PENDING,
            )
            RuntimeLaneOutbox.objects.create(
                change=change,
                payload={
                    'type': 'config_update', 'agent_id': agent.agent_id,
                    'desired_generation': 1, 'runtime_lane_digest': 'a' * 64,
                },
                attempt_count=1,
                last_attempt_at=timezone.now() - timedelta(seconds=10),
                last_error='transient channel send failed',
            )

        await _dsta(create_pending)()
        await comm.send_json_to({'type': 'heartbeat', 'seq': 41})
        ack = await comm.receive_json_from(timeout=1)
        retried = await comm.receive_json_from(timeout=1)
        self.assertEqual(ack, {'type': 'ack', 'seq': 41})
        self.assertEqual(retried['type'], 'config_update')
        self.assertEqual(retried['agent_id'], 'lane-retry')
        self.assertEqual(retried['desired_generation'], 1)

        get_outbox = _dsta(lambda: __import__('fleet.models', fromlist=['RuntimeLaneOutbox']).RuntimeLaneOutbox.objects.get())
        outbox = await get_outbox()
        self.assertEqual(outbox.attempt_count, 2)
        self.assertEqual(outbox.last_error, '')
        await comm.disconnect()

    async def test_heartbeat_retires_orphan_lane_older_than_desired_generation(self):
        """Production order: newer desired truth exists, then heartbeat sees stale N twice."""
        from channels.db import database_sync_to_async as _dsta

        app = _make_app()
        comm, _ = await _connect_and_register(app)

        def create_orphan():
            from datetime import timedelta
            from django.utils import timezone
            from fleet.models import (
                AgentRuntimeDesiredState, FleetAgent, RuntimeChange, RuntimeLaneOutbox,
            )

            daemon = DaemonRegistry.objects.get(daemon_id=_TEST_DAEMON)
            agent = FleetAgent.objects.create(
                agent_username='lane-orphan', agent_id='lane-orphan',
                model='gpt-5.6-sol', mcp_config={'execution_method': 'codex_app_server'},
                managing_daemon=daemon,
            )
            AgentRuntimeDesiredState.objects.create(
                agent=agent, desired_generation=7, desired_config_hash='b' * 64,
            )
            change = RuntimeChange.objects.create(
                agent=agent, base_generation=2, desired_generation=3,
                resolved_lane={'model': 'gpt-5.6-sol'}, previous_lane={},
                registry_version='v1', runtime_release_id='scli-1',
                lane_digest='a' * 64, state=RuntimeChange.State.ACTUATOR_PENDING,
            )
            RuntimeLaneOutbox.objects.create(
                change=change,
                payload={
                    'type': 'config_update', 'agent_id': agent.agent_id,
                    'desired_generation': 3, 'runtime_lane_digest': 'a' * 64,
                },
                attempt_count=9,
                last_attempt_at=timezone.now() - timedelta(seconds=10),
            )
            return change.id

        change_id = await _dsta(create_orphan)()
        for seq in (51, 52):
            await comm.send_json_to({'type': 'heartbeat', 'seq': seq})
            ack = await comm.receive_json_from(timeout=1)
            self.assertEqual(ack, {'type': 'ack', 'seq': seq})
            self.assertTrue(await comm.receive_nothing(timeout=0.15))

        def get_terminal_state():
            from fleet.models import RuntimeChange
            change = RuntimeChange.objects.get(id=change_id)
            return change.state, change.failure_code, change.outbox.delivered_at, change.outbox.last_error

        state, code, delivered_at, last_error = await _dsta(get_terminal_state)()
        from fleet.models import RuntimeChange
        self.assertEqual(state, RuntimeChange.State.SUPERSEDED)
        self.assertEqual(code, 'SUPERSEDED_BY_NEWER_GENERATION')
        self.assertIsNotNone(delivered_at)
        self.assertEqual(last_error, 'SUPERSEDED_BY_GENERATION_7')
        await comm.disconnect()

    async def test_heartbeat_stores_observation_without_promoting_runtime(self):
        """A heartbeat updates evidence only and cannot author desired state."""
        from channels.db import database_sync_to_async as _dsta

        app = _make_app()
        comm, _ = await _connect_and_register(app)
        report = {
            "agent_runtime_image": "localhost:30500/shizuha-agent-runtime:harness-new",
            "image_harness": {"codex": "0.144.4"},
        }
        cache.set('fleet:desired-runtime-image', 'legacy-desired-sentinel', 60)
        await comm.send_json_to({"type": "heartbeat", "seq": 2, "harness_report": report})
        ack = await comm.receive_json_from(timeout=1)
        self.assertEqual(ack["type"], "ack")

        get_registry = _dsta(lambda: DaemonRegistry.objects.get(daemon_id=_TEST_DAEMON))
        registry = await get_registry()
        self.assertEqual(registry.agent_runtime_image, report["agent_runtime_image"])
        self.assertEqual(registry.harness_report, report)
        self.assertEqual(
            cache.get('fleet:desired-runtime-image'),
            'legacy-desired-sentinel',
        )
        cache.delete('fleet:desired-runtime-image')
        await comm.disconnect()

    async def test_second_heartbeat_replaces_observation_but_not_desired_state(self):
        """Observed image can change; legacy desired cache is never consulted."""
        from channels.db import database_sync_to_async as _dsta

        app = _make_app()
        comm, _ = await _connect_and_register(app)
        successor = "localhost:30500/shizuha-agent-runtime:harness-successor"
        intermediate = "localhost:30500/shizuha-agent-runtime:harness-intermediate"
        cache.set('fleet:desired-runtime-image', successor, 60)

        try:
            await _dsta(
                lambda: DaemonRegistry.objects.filter(daemon_id=_TEST_DAEMON).update(
                    agent_runtime_image=successor,
                    harness_report={
                        "agent_runtime_image": successor,
                        "image_harness": {"scli": "2"},
                    },
                )
            )()
            await comm.send_json_to({
                "type": "heartbeat",
                "seq": 3,
                "harness_report": {
                    "agent_runtime_image": intermediate,
                    "image_harness": {"scli": "1"},
                },
            })
            ack = await comm.receive_json_from(timeout=1)
            self.assertEqual(ack["type"], "ack")

            get_registry = _dsta(
                lambda: DaemonRegistry.objects.get(daemon_id=_TEST_DAEMON)
            )
            registry = await get_registry()
            self.assertEqual(cache.get('fleet:desired-runtime-image'), successor)
            self.assertEqual(registry.agent_runtime_image, intermediate)
            self.assertEqual(
                registry.harness_report["agent_runtime_image"],
                intermediate,
            )
        finally:
            cache.delete('fleet:desired-runtime-image')
            await comm.disconnect()


@override_settings(CHANNEL_LAYERS=TEST_CHANNEL_LAYER)
class BackpressureTests(TransactionTestCase):
    """Test 6: backpressure coalesce."""

    def setUp(self):
        _ensure_test_credential()

    async def test_delta_queue_overflow_triggers_request_snapshot(self):
        """When >_MAX_DELTA_BUFFER child_ids are buffered, request_snapshot is sent."""
        import fleet.consumers as consumers_mod

        app = _make_app()
        comm, _ = await _connect_and_register(app)

        # Send snapshot but NOT snapshot_complete (so deltas are buffered)
        await comm.send_json_to({"type": "state_snapshot", "seq": 1, "data": {}})

        # Exercise the overflow boundary without turning the unit regression
        # into 1,001 durable liveness writes. Production uses the larger bound;
        # three frames are enough to prove the same coalescing transition.
        with mock.patch.object(consumers_mod, "_MAX_DELTA_BUFFER", 2):
            for i in range(3):
                await comm.send_json_to({
                    "type": "state_delta",
                    "seq": i + 2,
                    "child_id": f"child-{i}",
                    "data": {},
                })

            frame = await comm.receive_json_from(timeout=1)
        self.assertEqual(frame["type"], "request_snapshot")
        self.assertEqual(frame.get("reason"), "delta_queue_overflow")
        await comm.disconnect()


@override_settings(CHANNEL_LAYERS=TEST_CHANNEL_LAYER)
class RegisterTimeoutTests(TransactionTestCase):
    """Register timeout: close if no register frame within _REGISTER_TIMEOUT_S."""

    async def test_no_register_frame_closes_with_timeout(self):
        """A connection that never sends register must be closed after timeout."""
        import fleet.consumers as consumers_mod
        original_timeout = consumers_mod._REGISTER_TIMEOUT_S
        consumers_mod._REGISTER_TIMEOUT_S = 0.1  # accelerate for test

        try:
            app = _make_app()
            comm = WebsocketCommunicator(app, "/v1/fleet/daemon-link")
            connected, _ = await comm.connect()
            self.assertTrue(connected)
            # Wait for timeout + buffer
            frame = await comm.receive_json_from(timeout=0.5)
            self.assertEqual(frame["type"], "close_reason")
            self.assertEqual(frame["code"], "register_timeout")
        finally:
            consumers_mod._REGISTER_TIMEOUT_S = original_timeout
        await comm.disconnect()


@override_settings(CHANNEL_LAYERS=TEST_CHANNEL_LAYER)
class PollDemotionTests(TransactionTestCase):
    """Test: only a complete daemon WS snapshot demotes the HTTP poll."""

    def setUp(self):
        _ensure_test_credential()

    def test_poll_skipped_when_ws_snapshot_complete(self):
        """list_runtime_agents serves daemon-link cache without hitting HTTP."""
        from unittest.mock import patch
        from fleet.runtime_fleet import list_runtime_agents

        daemon = DaemonRegistry.objects.create(
            daemon_id=_TEST_DAEMON,
            fleet_id=_TEST_FLEET,
            status='connected',
            snapshot_complete=True,
        )
        FleetAgent.objects.create(
            agent_username='nagi',
            agent_id='agent-1',
            display_name='Nagi',
            role='Engineer',
            model='cortex/DeepSeek-V3.1',
            status=FleetAgent.Status.RUNNING,
            managing_daemon=daemon,
            skill_config={'skills': ['pulse-core'], 'eager_skills': []},
            mcp_config={'execution_method': 'shizuha', 'runtime_environment': 'bare_metal'},
        )
        with patch('fleet.runtime_fleet.requests.get') as mock_get:
            result = list_runtime_agents()
        mock_get.assert_not_called()
        self.assertEqual([item['agent_username'] for item in result], ['nagi'])
        self.assertEqual(result[0]['model'], 'cortex/DeepSeek-V3.1')
        self.assertTrue(result[0]['external_runtime'])

    def test_poll_runs_when_ws_snapshot_incomplete(self):
        """A half-seeded daemon-link cache must not hide live HTTP state."""
        from unittest.mock import patch, MagicMock
        from fleet.runtime_fleet import _RUNTIME_CACHE, list_runtime_agents

        _RUNTIME_CACHE.clear()

        daemon = DaemonRegistry.objects.create(
            daemon_id=_TEST_DAEMON,
            fleet_id=_TEST_FLEET,
            status='connected',
            snapshot_complete=False,
        )
        FleetAgent.objects.create(
            agent_username='nagi',
            agent_id='agent-1',
            display_name='Nagi',
            role='Engineer',
            model='cortex/DeepSeek-V3.1',
            status=FleetAgent.Status.PROVISIONING,
            managing_daemon=daemon,
        )
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            'agents': [{
                'id': 'agent-1',
                'username': 'nagi',
                'name': 'Nagi',
                'role': 'Engineer',
                'status': 'running',
                'enabled': True,
                'model': 'cortex/Qwen3.6',
            }]
        }
        mock_resp.raise_for_status.return_value = None
        with self.settings(RUNTIME_FLEET_API_URL='http://fake-daemon:8016'):
            with patch('fleet.runtime_fleet.requests.get', return_value=mock_resp) as mock_get:
                result = list_runtime_agents()
        mock_get.assert_called_once()
        self.assertEqual(result[0]['agent_username'], 'nagi')
        self.assertEqual(result[0]['status'], 'running')
        self.assertEqual(result[0]['model'], 'cortex/Qwen3.6')

    def test_poll_runs_when_no_ws_connected(self):
        """list_runtime_agents falls back to HTTP poll when no WS is active."""
        from unittest.mock import patch, MagicMock
        from fleet.runtime_fleet import list_runtime_agents

        DaemonRegistry.objects.filter(status='connected').update(status='disconnected')
        mock_resp = MagicMock()
        mock_resp.json.return_value = {'agents': []}
        mock_resp.raise_for_status.return_value = None
        with patch('fleet.runtime_fleet.requests.get', return_value=mock_resp) as mock_get:
            with self.settings(RUNTIME_FLEET_API_URL='http://fake-daemon:8016'):
                list_runtime_agents()
        mock_get.assert_called_once()


@override_settings(CHANNEL_LAYERS=TEST_CHANNEL_LAYER)
class NeedsHelpPropagationTests(TransactionTestCase):
    """PLAT-4172: a queue-blind agent's heartbeat needs_help escalation must reach
    Hive's FleetAgent row (and thus the Agents page) through the DaemonLink frame.
    Detection alone was invisible — nova fired needs_help but /hive/agents showed 0."""

    def setUp(self):
        _ensure_test_credential()
        _create_nagi_desired()

    def _frame_with_help(self, *, needs_help, reason="", outcome="", seq=1, ftype="state_snapshot"):
        frame = _state_frame(agent_id="agent-1", username="nagi")
        frame["type"] = ftype
        frame["seq"] = seq
        frame["data"]["needs_help"] = needs_help
        frame["data"]["needs_help_reason"] = reason
        frame["data"]["heartbeat_outcome"] = outcome
        return frame

    async def test_snapshot_persists_needs_help(self):
        from channels.db import database_sync_to_async as _dsta
        app = _make_app()
        comm, _ = await _connect_and_register(app)
        await comm.send_json_to(self._frame_with_help(
            needs_help=True, reason="4 ready task(s) with no progress for 2 heartbeat(s)",
            outcome="needs_help"))
        await comm.send_json_to({"type": "state_snapshot_complete", "seq": 2, "child_count": 1})
        await comm.receive_json_from()
        agent = await _dsta(lambda: FleetAgent.objects.get(agent_username="nagi"))()
        self.assertTrue(agent.needs_help)
        self.assertEqual(agent.needs_help_reason, "4 ready task(s) with no progress for 2 heartbeat(s)")
        self.assertEqual(agent.heartbeat_outcome, "needs_help")
        await comm.disconnect()

    async def test_delta_clears_needs_help_when_agent_resumes(self):
        from channels.db import database_sync_to_async as _dsta
        app = _make_app()
        comm, _ = await _connect_and_register(app)
        await comm.send_json_to(self._frame_with_help(needs_help=True, reason="stuck", outcome="needs_help"))
        await comm.send_json_to({"type": "state_snapshot_complete", "seq": 2, "child_count": 1})
        await comm.receive_json_from()
        # a later frame reports the agent exposed its queue again -> needs_help cleared
        await comm.send_json_to(self._frame_with_help(
            needs_help=False, reason="", outcome="queue_empty", seq=3, ftype="state_delta"))
        await comm.receive_json_from()
        agent = await _dsta(lambda: FleetAgent.objects.get(agent_username="nagi"))()
        self.assertFalse(agent.needs_help)
        self.assertEqual(agent.needs_help_reason, "")
        self.assertEqual(agent.heartbeat_outcome, "queue_empty")
        await comm.disconnect()

    async def test_frame_without_field_does_not_clobber(self):
        from channels.db import database_sync_to_async as _dsta
        app = _make_app()
        comm, _ = await _connect_and_register(app)
        await comm.send_json_to(self._frame_with_help(needs_help=True, reason="stuck", outcome="needs_help"))
        await comm.send_json_to({"type": "state_snapshot_complete", "seq": 2, "child_count": 1})
        await comm.receive_json_from()
        # an older daemon's delta omits the field entirely -> must not reset the flag
        legacy = _state_frame(agent_id="agent-1", username="nagi")
        legacy["type"] = "state_delta"
        legacy["seq"] = 3
        legacy["data"].pop("needs_help", None)
        await comm.send_json_to(legacy)
        await comm.receive_json_from()
        agent = await _dsta(lambda: FleetAgent.objects.get(agent_username="nagi"))()
        self.assertTrue(agent.needs_help)  # preserved, not clobbered
        await comm.disconnect()
class HIVE597ReconnectReplayTest(TransactionTestCase):
    """Regression tests for HIVE-597: desired-config outbox + reconnect replay."""

    async def test_reconnect_replays_runtime_lane_generation_fence(self):
        """A reconnect cannot downgrade a complete admitted lane to an unversioned patch."""
        from channels.db import database_sync_to_async as _dsta
        from fleet.models import AgentRuntimeDesiredState, DaemonRegistry, FleetAgent

        daemon = await _dsta(DaemonRegistry.objects.create)(
            daemon_id="hive803-daemon", fleet_id="hive803-fleet",
            status=DaemonRegistry.Status.CONNECTED,
        )
        agent = await _dsta(FleetAgent.objects.create)(
            agent_id="hive803-agent", agent_username="hive803-test",
            managing_daemon=daemon, model="gpt-5.6-sol",
            mcp_config={"execution_method": "codex_app_server"},
        )
        await _dsta(AgentRuntimeDesiredState.objects.create)(
            agent=agent,
            desired_generation=4,
            desired_config_hash="d" * 64,
            runtime_profile={
                "runtime_lane": {
                    "model": "gpt-5.6-sol",
                    "execution_method": "codex_app_server",
                    "model_fallbacks": [],
                    "model_overrides": {},
                },
                "registry_version": "2026-07-16.1",
                "lane_digest": "d" * 64,
            },
        )

        from fleet.consumers import DaemonLinkConsumer
        consumer = DaemonLinkConsumer()
        consumer._registry_pk = daemon.pk
        frames = await consumer._db_current_desired_config_frames()

        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["desired_generation"], 4)
        self.assertEqual(frames[0]["runtime_lane_registry_version"], "2026-07-16.1")
        self.assertEqual(frames[0]["runtime_lane_digest"], "d" * 64)
        self.assertEqual(frames[0]["config"]["model"], "gpt-5.6-sol")
        self.assertEqual(frames[0]["config"]["execution_method"], "codex_app_server")

    async def test_disconnected_patch_persists_optional_mcp_field(self):
        """PATCH while daemon-link is disconnected persists an optional mcp_config field
        (work_schedule) and replays it on reconnect."""
        from channels.db import database_sync_to_async as _dsta
        from fleet.runtime_fleet import configure_runtime_agent
        from fleet.models import DaemonRegistry, FleetAgent

        daemon = await _dsta(DaemonRegistry.objects.create)(
            daemon_id="hive597-daemon-1", fleet_id="hive597-fleet",
            status=DaemonRegistry.Status.DISCONNECTED,
        )
        agent = await _dsta(FleetAgent.objects.create)(
            agent_id="hive597-agent-1", agent_username="hive597-test",
            managing_daemon=daemon,
        )

        # PATCH with an optional mcp_config field while daemon is disconnected
        ok, err = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: configure_runtime_agent("hive597-agent-1", {"work_schedule": {"timezone": "UTC", "active_hours": {"start": "09:00", "end": "17:00"}}}),
        )
        self.assertTrue(ok, err)
        self.assertIn("queued", err, "should return queued detail when daemon is disconnected")

        # Verify persisted in pending_config
        fresh = await _dsta(lambda: FleetAgent.objects.get(agent_username="hive597-test"))()
        self.assertEqual(fresh.mcp_config.get("work_schedule"), {"timezone": "UTC", "active_hours": {"start": "09:00", "end": "17:00"}})
        self.assertIn("work_schedule", (fresh.pending_config or {}),
                      "work_schedule should be in pending_config")

        # Simulate reconnect: _db_current_desired_config_frames should include work_schedule
        from fleet.consumers import DaemonLinkConsumer
        consumer = DaemonLinkConsumer()
        consumer._registry_pk = daemon.pk
        frames = await consumer._db_current_desired_config_frames()
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["config"].get("work_schedule"), {"timezone": "UTC", "active_hours": {"start": "09:00", "end": "17:00"}})
        from fleet.config_fingerprints import config_value_fingerprint
        self.assertEqual(
            frames[0]["config_fingerprints"]["work_schedule"],
            config_value_fingerprint({"timezone": "UTC", "active_hours": {"start": "09:00", "end": "17:00"}}),
        )

    async def test_reconnect_omits_absent_optional_fields(self):
        """Reconnect frames must NOT emit null for absent optional mcp_config fields."""
        from channels.db import database_sync_to_async as _dsta
        from fleet.models import DaemonRegistry, FleetAgent

        daemon = await _dsta(DaemonRegistry.objects.create)(
            daemon_id="hive597-daemon-2", fleet_id="hive597-fleet",
            status=DaemonRegistry.Status.CONNECTED,
        )
        agent = await _dsta(FleetAgent.objects.create)(
            agent_id="hive597-agent-2", agent_username="hive597-absent",
            managing_daemon=daemon,
            mcp_config={"execution_method": "shizuha"},
        )

        from fleet.consumers import DaemonLinkConsumer
        consumer = DaemonLinkConsumer()
        consumer._registry_pk = daemon.pk
        frames = await consumer._db_current_desired_config_frames()
        self.assertEqual(len(frames), 1)
        config = frames[0]["config"]
        # Optional fields must not be present when absent from mcp_config
        for key in ("work_schedule", "max_concurrent_tasks", "allow_parallel_execution",
                    "token_budget", "agent_memory", "warm_pool_size", "tier"):
            self.assertNotIn(key, config, f"absent optional field '{key}' must not appear in reconnect config")

    def test_api_patch_returns_202_when_daemon_disconnected(self):
        """PATCH /v1/fleet/agents/{id}/ returns HTTP 202 + pending_sync when daemon-link is down."""
        from unittest.mock import patch, MagicMock
        from rest_framework.test import APIRequestFactory, force_authenticate
        from django.contrib.auth.models import User
        from fleet.models import DaemonRegistry, FleetAgent
        from fleet.views import FleetAgentDetailView
        from hive_platform.models import TokenEntry, TokenPool

        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id='3',
            provider=TokenPool.Provider.ANTHROPIC,
            display_name='hive597-runtime-lane-test',
        )
        TokenEntry.objects.create(
            pool=pool,
            label='available',
            token_ciphertext='not-read-by-preflight',
            wrapped_dek='not-read-by-preflight',
            dek_key_id='test',
            token_fingerprint='e' * 64,
            active=True,
        )

        daemon = DaemonRegistry.objects.create(
            daemon_id="hive597-daemon-api", fleet_id="hive597-fleet",
            status=DaemonRegistry.Status.DISCONNECTED,
        )
        FleetAgent.objects.create(
            agent_id="hive597-api-agent", agent_username="hive597-api",
            managing_daemon=daemon,
        )

        staff_user = User.objects.create_user(username="hive597-staff", is_staff=True)
        factory = APIRequestFactory()
        req = factory.patch(
            '/v1/fleet/agents/hive597-api-agent/',
            {'model': 'claude-opus-4', 'work_schedule': {'timezone': 'UTC', 'active_hours': {'start': '09:00', 'end': '17:00'}}},
            format='json',
        )
        force_authenticate(req, user=staff_user)

        # Mock runtime agent path (rt-fleet agent, no native DB row)
        mock_agent = {
            'id': 'hive597-api-agent', 'agent_username': 'hive597-api',
            'model': 'claude-sonnet-4', 'status': 'running',
        }
        with patch('fleet.views.get_runtime_agent', return_value=mock_agent), \
                patch('fleet.views._get_agent', return_value=Response(status=404)), \
                patch('fleet.views.agent_index_cache'):
            resp = FleetAgentDetailView.as_view()(req, agent_id='hive597-api-agent')

        self.assertEqual(resp.status_code, 202,
                         f"expected 202 when daemon is disconnected, got {resp.status_code}: {resp.data}")
        self.assertTrue(resp.data.get('pending_sync'),
                        f"expected pending_sync=true, got: {resp.data}")
        self.assertIn('queued', resp.data.get('pending_sync_reason', ''),
                      f"expected queued reason, got: {resp.data}")

        # Verify desired state persisted
        fresh = FleetAgent.objects.get(agent_username="hive597-api")
        self.assertEqual(fresh.mcp_config.get("work_schedule"), {"timezone": "UTC", "active_hours": {"start": "09:00", "end": "17:00"}})
