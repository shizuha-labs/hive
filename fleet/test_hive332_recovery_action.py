"""
Tests for HIVE-332 (HIVE-277 P3): recovery_action / recovery_result protocol,
AgentRecoveryAction audit state machine, and consumer wiring.

Covers (per acceptance criteria):
  1. Integration: Hive creates a recovery action for a persisted issue, sends a
     `recovery_action` frame over the daemon-link WS, receives `recovery_result`,
     and persists the full audit trail.
  2. Banto-style identity-alias fixture: dry-run result (denied vs would_apply)
     is visible in the action audit without mutating unsafe state.
  3. Capability-generation drift fixture: pull_desired_state_now /
     apply_effective_runtime_config dry-run records correlation/result.
  4. Deny tests: gated unsafe actions (cross-org identity change, external
     credential grants, TeamRoster mutation, broad daemon/fleet restart,
     privilege expansion) are rejected at creation; apply mode is P4-gated.
  5. No secret values are persisted: creation rejects secret-bearing params;
     inbound daemon results are redacted before storage.

Uses channels.testing.WebsocketCommunicator with InMemoryChannelLayer — no
Redis needed (same harness as test_hive235_daemon_ws.py).
"""
import hashlib
import uuid

from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator
from django.core.exceptions import ValidationError
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from fleet.consumers import DaemonLinkConsumer
from fleet.models import (
    GATED_RECOVERY_ACTIONS,
    SAFE_RECOVERY_ACTIONS,
    recovery_auto_apply_allowed,
    AgentHealthIssue,
    AgentRecoveryAction,
    AgentRuntimeDesiredState,
    DaemonCredential,
    DaemonRegistry,
    FleetAgent,
    recovery_secret_rejection_count,
)
from fleet.recovery import (
    build_recovery_action_frame,
    create_recovery_action,
    process_recovery_result,
    recovery_actions_to_prometheus,
    redact_secret_material,
    send_recovery_action,
)

TEST_CHANNEL_LAYER = {
    "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"},
}

_TEST_DAEMON = "daemon-recovery-001"
_TEST_TOKEN = "recovery-test-token"
_TOKEN_HASH = hashlib.sha256(_TEST_TOKEN.encode()).hexdigest()


def _mk_agent(username="banto"):
    return FleetAgent.objects.create(agent_username=username)


def _mk_daemon(daemon_id=_TEST_DAEMON):
    return DaemonRegistry.objects.create(daemon_id=daemon_id, fleet_id="fleet-r-1")


def _mk_issue(agent, issue_type=AgentHealthIssue.IssueType.IDENTITY_ALIAS_DRIFT):
    now = timezone.now()
    return AgentHealthIssue.objects.create(
        agent=agent,
        issue_type=issue_type,
        severity=AgentHealthIssue.Severity.MEDIUM,
        service="id",
        first_seen=now,
        last_seen=now,
    )


class RecoveryActionModelTests(TestCase):
    """Gating + state machine on AgentRecoveryAction (deny tests, acceptance #4)."""

    def setUp(self):
        self.agent = _mk_agent()
        self.daemon = _mk_daemon()

    def test_safe_dry_run_action_creates(self):
        issue = _mk_issue(self.agent)
        action = create_recovery_action(
            self.agent, "reconcile_identity_alias", issues=[issue], actor="test",
        )
        self.assertEqual(action.status, AgentRecoveryAction.Status.PENDING)
        self.assertEqual(action.mode, AgentRecoveryAction.Mode.DRY_RUN)
        self.assertEqual(action.issue_ids, [str(issue.id)])
        issue.refresh_from_db()
        self.assertEqual(issue.recovery_action_id, action.id)
        self.assertEqual(issue.recovery_policy, "dry_run:reconcile_identity_alias")
        # Dry-run must not mutate issue state
        self.assertEqual(issue.status, AgentHealthIssue.Status.OPEN)

    def test_every_gated_unsafe_action_is_rejected_at_creation(self):
        # cross-org identity changes, external/billing credential grants,
        # TeamRoster mutation, disable/delete, broad daemon/fleet restart,
        # privilege expansion — all rejected in code (HLD §5 gated set).
        for action_type in GATED_RECOVERY_ACTIONS:
            with self.assertRaises(ValidationError, msg=action_type):
                create_recovery_action(self.agent, action_type)
        self.assertEqual(AgentRecoveryAction.objects.count(), 0)

    def test_unknown_action_type_is_default_denied(self):
        with self.assertRaises(ValidationError):
            create_recovery_action(self.agent, "reformat_disk")

    def test_apply_mode_is_p4_gated(self):
        for action_type in SAFE_RECOVERY_ACTIONS:
            with self.assertRaises(ValidationError, msg=action_type):
                create_recovery_action(
                    self.agent, action_type, mode=AgentRecoveryAction.Mode.APPLY,
                )

    @override_settings(
        HIVE_RECOVERY_AUTO_APPLY_ENABLED=True,
        HIVE_RECOVERY_AUTO_APPLY_ACTIONS=('reconcile_identity_alias',),
        HIVE_RECOVERY_AUTO_APPLY_DENY_ACTIONS=(),
    )
    def test_apply_mode_requires_per_action_allowlist(self):
        allowed, reason = recovery_auto_apply_allowed('reconcile_identity_alias')
        self.assertTrue(allowed, reason)
        action = create_recovery_action(
            self.agent,
            'reconcile_identity_alias',
            mode=AgentRecoveryAction.Mode.APPLY,
            params={'canonical_email_hash': 'a' * 64},
        )
        self.assertEqual(action.mode, AgentRecoveryAction.Mode.APPLY)

        allowed, reason = recovery_auto_apply_allowed('apply_effective_runtime_config')
        self.assertFalse(allowed)
        self.assertEqual(reason, 'not-in-allowlist')
        with self.assertRaises(ValidationError):
            create_recovery_action(
                self.agent,
                'apply_effective_runtime_config',
                mode=AgentRecoveryAction.Mode.APPLY,
            )

    @override_settings(
        HIVE_RECOVERY_AUTO_APPLY_ENABLED=True,
        HIVE_RECOVERY_AUTO_APPLY_ACTIONS=('restart_agent',),
        HIVE_RECOVERY_AUTO_APPLY_DENY_ACTIONS=('restart_agent',),
    )
    def test_auto_apply_denylist_overrides_allowlist(self):
        allowed, reason = recovery_auto_apply_allowed('restart_agent')
        self.assertFalse(allowed)
        self.assertEqual(reason, 'per-action-denylist')
        with self.assertRaises(ValidationError):
            create_recovery_action(
                self.agent, 'restart_agent', mode=AgentRecoveryAction.Mode.APPLY,
            )

    @override_settings(
        HIVE_RECOVERY_AUTO_APPLY_ENABLED=True,
        HIVE_RECOVERY_AUTO_APPLY_ACTIONS=('apply_effective_runtime_config',),
        HIVE_RECOVERY_AUTO_APPLY_DENY_ACTIONS=(),
    )
    def test_apply_runtime_config_requires_current_desired_state_provenance(self):
        config_hash = 'b' * 64
        with self.assertRaises(ValidationError):
            create_recovery_action(
                self.agent,
                'apply_effective_runtime_config',
                mode=AgentRecoveryAction.Mode.APPLY,
                params={'desired_generation': 7, 'desired_config_hash': config_hash},
            )

        AgentRuntimeDesiredState.objects.create(
            agent=self.agent,
            desired_generation=7,
            desired_config_hash=config_hash,
            source=AgentRuntimeDesiredState.Source.CAPABILITY_RECOMPUTE,
            source_actor='hive-277-test',
        )
        action = create_recovery_action(
            self.agent,
            'apply_effective_runtime_config',
            mode=AgentRecoveryAction.Mode.APPLY,
            params={'desired_generation': 7, 'desired_config_hash': config_hash},
        )
        self.assertEqual(action.mode, AgentRecoveryAction.Mode.APPLY)

        with self.assertRaises(ValidationError):
            create_recovery_action(
                self.agent,
                'apply_effective_runtime_config',
                mode=AgentRecoveryAction.Mode.APPLY,
                params={'desired_generation': 8, 'desired_config_hash': config_hash},
            )

    def test_secret_material_in_params_is_rejected(self):
        before = recovery_secret_rejection_count()
        with self.assertRaises(ValidationError):
            create_recovery_action(
                self.agent, "refresh_credential_grants",
                params={"token": "ghp_abcdefghijklmnopqrstuvwxyz0123456789"},
            )
        self.assertGreater(recovery_secret_rejection_count(), before)
        # hex config hashes are fine — they reference config, not secrets
        action = create_recovery_action(
            self.agent, "apply_effective_runtime_config",
            params={"desired_config_hash": "a" * 64},
        )
        self.assertEqual(action.params["desired_config_hash"], "a" * 64)

    def test_state_machine_rejects_invalid_transitions(self):
        action = create_recovery_action(self.agent, "restart_agent")
        with self.assertRaises(ValidationError):
            action.apply_daemon_result("would_apply")  # result before send
        action.mark_sent(self.daemon)
        self.assertEqual(action.status, AgentRecoveryAction.Status.SENT)
        self.assertIsNotNone(action.sent_at)
        action.apply_daemon_result("would_apply", reason="dry-run ok")
        self.assertEqual(action.status, AgentRecoveryAction.Status.WOULD_APPLY)
        self.assertIsNotNone(action.resolved_at)
        with self.assertRaises(ValidationError):
            action.apply_daemon_result("failed")  # already terminal

    def test_result_after_expiry_resolves_as_expired(self):
        action = create_recovery_action(self.agent, "restart_agent")
        action.mark_sent(self.daemon)
        AgentRecoveryAction.objects.filter(pk=action.pk).update(
            expires_at=timezone.now() - timezone.timedelta(minutes=1),
        )
        action.refresh_from_db()
        action.apply_daemon_result("would_apply", daemon_result={"result": "would_apply"})
        self.assertEqual(action.status, AgentRecoveryAction.Status.EXPIRED)
        self.assertIn("after expiry", action.result_reason)
        # daemon payload retained for audit
        self.assertEqual(action.daemon_result, {"result": "would_apply"})

    def test_recovery_metrics_include_alert_inputs_and_secret_rejections(self):
        create_recovery_action(self.agent, "restart_agent", mode=AgentRecoveryAction.Mode.DRY_RUN)
        failed = create_recovery_action(
            self.agent,
            "reconcile_identity_alias",
            mode=AgentRecoveryAction.Mode.DRY_RUN,
        )
        failed.mark_sent(self.daemon)
        failed.apply_daemon_result("failed", reason="daemon failed")
        stale = create_recovery_action(self.agent, "pull_desired_state_now")
        AgentRecoveryAction.objects.filter(pk=stale.pk).update(
            expires_at=timezone.now() - timezone.timedelta(minutes=1),
        )
        metrics = recovery_actions_to_prometheus(now=timezone.now())
        self.assertIn('hive_recovery_actions_total{action_type="reconcile_identity_alias",mode="dry_run",status="failed"} 1', metrics)
        self.assertIn('hive_recovery_actions_stale_total 1', metrics)
        self.assertIn('hive_recovery_secret_rejections_total', metrics)


class RecoveryResultProcessingTests(TestCase):
    """process_recovery_result validation + redaction (acceptance #1/#5)."""

    def setUp(self):
        self.agent = _mk_agent()
        self.daemon = _mk_daemon()

    def _sent_action(self, action_type="restart_agent"):
        action = create_recovery_action(self.agent, action_type)
        action.mark_sent(self.daemon)
        return action

    def test_result_persists_full_audit(self):
        action = self._sent_action()
        result = process_recovery_result({
            "action_id": str(action.id),
            "correlation_id": str(action.correlation_id),
            "action_type": "restart_agent",
            "mode": "dry_run",
            "result": "would_apply",
            "reason": "dry-run: would restart this single agent runtime",
            "before_summary": {"agent": "banto", "runtime_id": "agent-uuid-1"},
            "after_summary": {},
        }, self.daemon)
        self.assertIsNotNone(result)
        action.refresh_from_db()
        self.assertEqual(action.status, AgentRecoveryAction.Status.WOULD_APPLY)
        self.assertEqual(action.before_summary["agent"], "banto")
        self.assertIn("would restart", action.result_reason)
        self.assertEqual(action.daemon_result["result"], "would_apply")

    def test_unknown_action_id_and_correlation_mismatch_are_dropped(self):
        action = self._sent_action()
        self.assertIsNone(process_recovery_result(
            {"action_id": str(uuid.uuid4()), "result": "would_apply"}, self.daemon,
        ))
        self.assertIsNone(process_recovery_result({
            "action_id": str(action.id),
            "correlation_id": str(uuid.uuid4()),
            "result": "would_apply",
        }, self.daemon))
        action.refresh_from_db()
        self.assertEqual(action.status, AgentRecoveryAction.Status.SENT)

    def test_result_from_wrong_daemon_is_dropped(self):
        action = self._sent_action()
        other = _mk_daemon("daemon-recovery-002")
        self.assertIsNone(process_recovery_result({
            "action_id": str(action.id),
            "result": "would_apply",
        }, other))
        action.refresh_from_db()
        self.assertEqual(action.status, AgentRecoveryAction.Status.SENT)

    def test_invalid_result_state_is_dropped(self):
        action = self._sent_action()
        self.assertIsNone(process_recovery_result({
            "action_id": str(action.id),
            "result": "exploded",
        }, self.daemon))

    def test_secretlike_values_in_daemon_result_are_redacted(self):
        action = self._sent_action()
        process_recovery_result({
            "action_id": str(action.id),
            "result": "failed",
            "reason": "leaked ghp_abcdefghijklmnopqrstuvwxyz0123456789 in error",
            "before_summary": {
                "token": "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
                "config_hash": "b" * 64,
            },
        }, self.daemon)
        action.refresh_from_db()
        self.assertEqual(action.before_summary["token"], "[redacted-secret]")
        self.assertEqual(action.before_summary["config_hash"], "b" * 64)
        self.assertNotIn("ghp_", str(action.daemon_result))

    def test_redact_secret_material_is_deep(self):
        cleaned = redact_secret_material({
            "list": ["ok", "sk-abcdefghijklmnopqrstuvwxyz012345"],
            "nested": {"jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig"},
        })
        self.assertEqual(cleaned["list"], ["ok", "[redacted-secret]"])
        self.assertEqual(cleaned["nested"]["jwt"], "[redacted-secret]")


@override_settings(CHANNEL_LAYERS=TEST_CHANNEL_LAYER)
class RecoveryProtocolIntegrationTests(TransactionTestCase):
    """End-to-end WS integration (acceptance #1/#2/#3)."""

    def setUp(self):
        DaemonCredential.objects.create(daemon_id=_TEST_DAEMON, token_hash=_TOKEN_HASH)

    async def _register(self):
        comm = WebsocketCommunicator(DaemonLinkConsumer.as_asgi(), "/v1/fleet/daemon-link")
        connected, _ = await comm.connect()
        assert connected
        await comm.send_json_to({
            "type": "register",
            "daemon_id": _TEST_DAEMON,
            "fleet_id": "fleet-r-1",
            "runtime": "docker",
            "token": _TEST_TOKEN,
        })
        ack = await comm.receive_json_from()
        assert ack["type"] == "register_ack"
        return comm

    async def _send_banto_health_snapshot(self, comm, username):
        """Banto-style alias-drift fixture: runtime email differs from canonical
        with no same-platform-user proof → identity_alias_drift issue."""
        await comm.send_json_to({
            "type": "health_snapshot",
            "seq": 10,
            "snapshot_type": "full",
            "child_id": username,
            "runtime_username": username,
            "runtime_email": f"{username}@shizuha.com",
            "canonical_email": f"{username}@agents.shizuha.io",
            "platform_user_id": "u-123",
            "account_type": "agent",
            "account_active": True,
            "alias_resolution_evidence": {},
        })

    async def test_full_recovery_loop_banto_alias_fixture(self):
        """Create issue from snapshot → create+send recovery_action → daemon
        receives the frame → replies recovery_result → audit persisted."""
        from channels.db import database_sync_to_async

        agent = await database_sync_to_async(_mk_agent)("banto")
        comm = await self._register()
        try:
            await self._send_banto_health_snapshot(comm, "banto")
            # health_snapshot handler is async fire-and-forget on this socket;
            # poll for the classified issue row.
            issue = None
            for _ in range(50):
                issue = await database_sync_to_async(
                    lambda: AgentHealthIssue.objects.filter(
                        agent=agent, issue_type="identity_alias_drift",
                        status=AgentHealthIssue.Status.OPEN,
                    ).first()
                )()
                if issue:
                    break
                import asyncio
                await asyncio.sleep(0.05)
            assert issue is not None, "Banto alias-drift issue was not classified"

            daemon = await database_sync_to_async(
                DaemonRegistry.objects.get)(daemon_id=_TEST_DAEMON)

            # Unproven alias → daemon would deny; proven alias → would_apply.
            action = await database_sync_to_async(create_recovery_action)(
                agent, "reconcile_identity_alias",
                issues=[issue],
                params={
                    "canonical_email": "banto@agents.shizuha.io",
                    "canonical_platform_user_id": "u-123",
                    "same_platform_user": True,
                },
                actor="test-operator",
                source="hive_api",
            )
            frame = await send_recovery_action(action, daemon)
            self.assertEqual(frame["action_type"], "reconcile_identity_alias")

            # The daemon (this communicator) receives the recovery_action frame.
            received = await comm.receive_json_from(timeout=5)
            self.assertEqual(received["type"], "recovery_action")
            self.assertEqual(received["action_id"], str(action.id))
            self.assertEqual(received["correlation_id"], str(action.correlation_id))
            self.assertEqual(received["mode"], "dry_run")
            self.assertIn("seq", received)

            # Daemon replies with a recovery_result (as recovery-actions.ts would).
            await comm.send_json_to({
                "type": "recovery_result",
                "seq": 11,
                "action_id": received["action_id"],
                "correlation_id": received["correlation_id"],
                "action_type": received["action_type"],
                "mode": "dry_run",
                "result": "would_apply",
                "reason": "dry-run: alias proven same platform user",
                "before_summary": {"runtime_email": "banto@shizuha.com"},
                "after_summary": {"canonical_email": "banto@agents.shizuha.io"},
            })
            ack = await comm.receive_json_from(timeout=5)
            self.assertEqual(ack["type"], "ack")
            self.assertEqual(ack["seq"], 11)

            await database_sync_to_async(action.refresh_from_db)()
            self.assertEqual(action.status, AgentRecoveryAction.Status.WOULD_APPLY)
            self.assertEqual(action.after_summary["canonical_email"], "banto@agents.shizuha.io")
            self.assertEqual(action.result_reason, "dry-run: alias proven same platform user")
            daemon_pk = await database_sync_to_async(lambda: action.daemon_id)()
            self.assertEqual(daemon_pk, daemon.pk)
            # Audit visible from the issue without unsafe-state mutation:
            await database_sync_to_async(issue.refresh_from_db)()
            self.assertEqual(issue.recovery_action_id, action.id)
            self.assertEqual(issue.status, AgentHealthIssue.Status.OPEN)
        finally:
            await comm.disconnect()

    async def test_capability_drift_fixture_records_correlation_and_result(self):
        """Capability-generation drift fixture can request pull_desired_state_now
        and apply_effective_runtime_config dry-runs; correlation + result land."""
        from channels.db import database_sync_to_async

        agent = await database_sync_to_async(_mk_agent)("kaede")
        comm = await self._register()
        try:
            daemon = await database_sync_to_async(
                DaemonRegistry.objects.get)(daemon_id=_TEST_DAEMON)
            now = timezone.now()
            issue = await database_sync_to_async(AgentHealthIssue.objects.create)(
                agent=agent,
                issue_type=AgentHealthIssue.IssueType.APPLIED_GENERATION_STALE,
                severity=AgentHealthIssue.Severity.MEDIUM,
                service="fleet",
                first_seen=now, last_seen=now,
            )
            for action_type, params in [
                ("pull_desired_state_now", {}),
                ("apply_effective_runtime_config",
                 {"desired": {"skills": ["engineering-core"]}, "desired_generation": 7}),
            ]:
                action = await database_sync_to_async(create_recovery_action)(
                    agent, action_type, issues=[issue], params=params,
                )
                await send_recovery_action(action, daemon)
                received = await comm.receive_json_from(timeout=5)
                self.assertEqual(received["type"], "recovery_action")
                self.assertEqual(received["correlation_id"], str(action.correlation_id))
                await comm.send_json_to({
                    "type": "recovery_result",
                    "seq": 20,
                    "action_id": received["action_id"],
                    "correlation_id": received["correlation_id"],
                    "action_type": action_type,
                    "mode": "dry_run",
                    "result": "would_apply",
                    "reason": f"dry-run ok: {action_type}",
                })
                await comm.receive_json_from(timeout=5)  # ack
                await database_sync_to_async(action.refresh_from_db)()
                self.assertEqual(
                    action.status, AgentRecoveryAction.Status.WOULD_APPLY, action_type,
                )
                self.assertEqual(action.result_reason, f"dry-run ok: {action_type}")
        finally:
            await comm.disconnect()
