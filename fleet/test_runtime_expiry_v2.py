"""PLAT-4159 time-driven convergence and availability-outbox crash tests."""

from datetime import timedelta
import uuid

from django.test import TestCase
from django.utils import timezone

from .models import (
    AgentDaemonBinding,
    AgentRuntimeDesiredState,
    AgentRuntimeMigrationState,
    AgentRuntimeObservation,
    DaemonRegistry,
    FleetAgent,
    RuntimeAvailabilityOutbox,
    RuntimeBoundaryTimer,
)
from .runtime_projection_v2 import (
    claim_availability_batch,
    mark_availability_delivered,
    mark_availability_failed,
    materialize_runtime_projection,
    process_due_runtime_timers,
)


class RuntimeExpiryV2Tests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.epoch = uuid.uuid4()
        self.daemon = DaemonRegistry.objects.create(
            daemon_id="rt-expiry-v2",
            fleet_id="fleet-expiry-v2",
            runtime=DaemonRegistry.RuntimeType.K3S,
            status=DaemonRegistry.Status.CONNECTED,
            selected_protocol_version=2,
            active_runtime_epoch=self.epoch,
        )
        self.agent = FleetAgent.objects.create(agent_username="expiry-v2")
        AgentRuntimeDesiredState.objects.create(
            agent=self.agent,
            desired_generation=1,
            desired_lifecycle=AgentRuntimeDesiredState.DesiredLifecycle.ACTIVE,
            lifecycle_executor=AgentRuntimeDesiredState.LifecycleExecutor.DAEMON_V2,
            executor_generation=1,
            expected_deployment_uid="deploy-expiry",
        )
        AgentRuntimeMigrationState.objects.create(agent=self.agent)
        AgentDaemonBinding.objects.create(
            agent=self.agent,
            active_owner_daemon=self.daemon,
            owner_generation=1,
            active_runtime_epoch=self.epoch,
            binding_state=AgentDaemonBinding.State.BOUND,
            owner_lease_expires_at=self.now + timedelta(minutes=10),
        )
        self.observation = AgentRuntimeObservation.objects.create(
            agent=self.agent,
            owner_daemon=self.daemon,
            owner_generation=1,
            runtime_epoch=self.epoch,
            frame_seq=1,
            observation_seq=1,
            source_adapter=AgentRuntimeObservation.Adapter.K3S,
            source_ok=True,
            snapshot_complete=True,
            observed_at=self.now,
            valid_until=self.now + timedelta(seconds=5),
            desired_generation=1,
            deployment_uid="deploy-expiry",
            deployment_generation=1,
            deployment_observed_generation=1,
            phase="Running",
            required_replicas=1,
            ready_replicas=1,
            required_containers=3,
            ready_containers=3,
            runtime_present=True,
            daemon_local_present=True,
            resource_count=1,
            classification="current",
            active_cut_id=uuid.uuid4(),
            final_u_digest="b" * 64,
        )

    def test_due_timer_expires_running_without_new_frame(self):
        first = materialize_runtime_projection(self.agent.pk, now=self.now)
        self.assertEqual(first.effective_status, "running")
        timer = RuntimeBoundaryTimer.objects.get(agent=self.agent)
        self.assertEqual(timer.due_at, self.observation.valid_until)

        later = self.observation.valid_until + timedelta(seconds=1)
        processed = process_due_runtime_timers("timer-worker", now=later)
        self.assertEqual(processed, 1)
        self.agent.runtime_projection.refresh_from_db()
        self.assertEqual(self.agent.runtime_projection.effective_status, "unknown")
        self.assertEqual(self.agent.runtime_projection.reason_codes, ["OBSERVATION_EXPIRED"])
        self.assertFalse(self.agent.runtime_projection.runtime_owner_drain_required)

    def test_claim_token_is_fresh_redacted_and_cas_gated(self):
        materialize_runtime_projection(self.agent.pk, now=self.now)
        first = claim_availability_batch("publisher-a", now=self.now)
        self.assertEqual(len(first), 1)
        claim = first[0]
        self.assertNotIn(claim.claim_token, repr(claim))
        self.assertGreaterEqual(len(claim.claim_token), 32)
        self.assertFalse(mark_availability_delivered(claim.outbox_id, "wrong-token", {"ack": "x"}, now=self.now))
        self.assertTrue(mark_availability_delivered(claim.outbox_id, claim.claim_token, {"ack": "ok"}, now=self.now))
        row = RuntimeAvailabilityOutbox.objects.get(outbox_id=claim.outbox_id)
        self.assertEqual(row.state, RuntimeAvailabilityOutbox.State.DELIVERED)
        self.assertEqual(row.claim_token, "")

    def test_claim_crash_reclaims_with_new_token_and_identical_key(self):
        materialize_runtime_projection(self.agent.pk, now=self.now)
        first = claim_availability_batch("publisher-a", now=self.now)[0]
        row = RuntimeAvailabilityOutbox.objects.get(outbox_id=first.outbox_id)
        idempotency_key = row.idempotency_key
        reclaimed_at = self.now + timedelta(seconds=31)
        second = claim_availability_batch("publisher-b", now=reclaimed_at)[0]
        self.assertNotEqual(first.claim_token, second.claim_token)
        self.assertEqual(second.outbox_id, first.outbox_id)
        self.assertEqual(
            RuntimeAvailabilityOutbox.objects.get(outbox_id=second.outbox_id).idempotency_key,
            idempotency_key,
        )
        self.assertFalse(mark_availability_delivered(first.outbox_id, first.claim_token, {}, now=reclaimed_at))
        self.assertTrue(mark_availability_delivered(second.outbox_id, second.claim_token, {"ack": "ok"}, now=reclaimed_at))

    def test_failed_delivery_retries_same_row_and_latches_without_drop(self):
        materialize_runtime_projection(self.agent.pk, now=self.now)
        claim = claim_availability_batch("publisher-a", now=self.now)[0]
        self.assertTrue(mark_availability_failed(claim.outbox_id, claim.claim_token, "receiver timeout", now=self.now))
        row = RuntimeAvailabilityOutbox.objects.get(outbox_id=claim.outbox_id)
        self.assertEqual(row.state, RuntimeAvailabilityOutbox.State.PENDING)
        self.assertEqual(row.attempt_count, 1)
        self.assertFalse(row.failure_latched)
        self.assertIsNotNone(row.next_attempt_at)

        row.attempt_count = 10
        row.next_attempt_at = self.now
        row.save(update_fields=["attempt_count", "next_attempt_at"])
        later = self.now + timedelta(seconds=2)
        claim2 = claim_availability_batch("publisher-a", now=later)[0]
        self.assertTrue(mark_availability_failed(claim2.outbox_id, claim2.claim_token, "still down", now=later))
        row.refresh_from_db()
        self.assertTrue(row.failure_latched)
        self.assertEqual(row.state, RuntimeAvailabilityOutbox.State.PENDING)
        self.assertEqual(RuntimeAvailabilityOutbox.objects.filter(outbox_id=row.outbox_id).count(), 1)

    def test_new_decision_supersedes_old_unclaimed_but_keeps_latest(self):
        first = materialize_runtime_projection(self.agent.pk, now=self.now)
        first_event = RuntimeAvailabilityOutbox.objects.get(agent=self.agent)
        self.observation.ready_containers = 1
        self.observation.observation_seq = 2
        self.observation.frame_seq = 2
        self.observation.save(update_fields=["ready_containers", "observation_seq", "frame_seq"])
        second = materialize_runtime_projection(self.agent.pk, now=self.now + timedelta(seconds=1))
        self.assertGreater(second.decision_version, first.decision_version)
        first_event.refresh_from_db()
        self.assertEqual(first_event.state, RuntimeAvailabilityOutbox.State.SUPERSEDED)
        latest = RuntimeAvailabilityOutbox.objects.get(
            agent=self.agent, decision_version=second.decision_version,
        )
        self.assertEqual(latest.state, RuntimeAvailabilityOutbox.State.PENDING)
