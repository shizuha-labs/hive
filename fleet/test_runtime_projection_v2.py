"""PLAT-4159/HIVE-277 §10 v16 reducer, H-manifest and barrier regressions."""

from datetime import timedelta
import uuid

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from .models import (
    AgentDaemonBinding,
    AgentLifecycleCommand,
    AgentRuntimeDesiredState,
    AgentRuntimeMigrationState,
    AgentRuntimeObservation,
    DaemonRegistry,
    FleetAgent,
    RuntimeSnapshotCandidate,
)
from .runtime_projection_v2 import (
    RuntimeProjectionError,
    RuntimeProjectionInput,
    activate_runtime_snapshot_candidate,
    canonical_json,
    canonical_digest,
    materialize_h_manifest,
    materialize_runtime_projection,
    migration_evidence_citation,
    reduce_runtime_state,
    resolve_migration_hold,
    stage_runtime_snapshot_candidate,
)


class RuntimeReducerV2Tests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.base = dict(
            desired_lifecycle="active",
            desired_generation=4,
            lifecycle_executor="daemon_v2",
            executor_generation=2,
            expected_deployment_uid="deploy-current",
            workload_identity_generation=3,
            migration_state="not_required",
            binding_state="bound",
            owner_generation=7,
            active_runtime_epoch=str(uuid.uuid4()),
            owner_lease_expires_at=self.now + timedelta(seconds=90),
            adapter_supported=True,
            observation_present=True,
            observation_owner_generation=7,
            observation_seq=11,
            source_ok=True,
            snapshot_complete=True,
            evidence_valid_until=self.now + timedelta(seconds=150),
            observation_desired_generation=4,
            runtime_present=True,
            resource_count=1,
            deployment_uid="deploy-current",
            deployment_generation=12,
            deployment_observed_generation=12,
            required_replicas=1,
            ready_replicas=1,
            required_containers=3,
            ready_containers=3,
            phase="Running",
        )
        self.base["observation_runtime_epoch"] = self.base["active_runtime_epoch"]

    def decision(self, **changes):
        return reduce_runtime_state(
            RuntimeProjectionInput(**{**self.base, **changes}), now=self.now,
        )

    def test_complete_current_ready_evidence_is_running_and_eligible(self):
        decision = self.decision()
        self.assertEqual(decision.effective_status, "running")
        self.assertTrue(decision.runtime_new_assignment_eligible)
        self.assertFalse(decision.runtime_owner_drain_required)
        self.assertEqual(decision.reason_codes, ("READY_CURRENT",))

    def test_one_ready_sidecar_never_proves_running(self):
        decision = self.decision(ready_containers=1)
        self.assertEqual(decision.effective_status, "starting")
        self.assertFalse(decision.runtime_new_assignment_eligible)
        self.assertEqual(decision.reason_codes, ("TRANSIENT_UNREADY",))

    def test_foreign_generation_fails_closed_without_owner_drain(self):
        decision = self.decision(observation_owner_generation=6)
        self.assertEqual(decision.effective_status, "unknown")
        self.assertFalse(decision.runtime_new_assignment_eligible)
        self.assertFalse(decision.runtime_owner_drain_required)
        self.assertEqual(decision.reason_codes, ("STALE_EVIDENCE",))

    def test_expired_observation_fails_closed_without_owner_drain(self):
        decision = self.decision(evidence_valid_until=self.now - timedelta(seconds=1))
        self.assertEqual(decision.reason_codes, ("OBSERVATION_EXPIRED",))
        self.assertFalse(decision.runtime_owner_drain_required)

    def test_migration_hold_dominates_healthy_runtime(self):
        decision = self.decision(migration_state="held")
        self.assertEqual(decision.effective_status, "unknown")
        self.assertEqual(decision.reason_codes, ("MIGRATION_HOLD",))
        self.assertFalse(decision.runtime_new_assignment_eligible)

    def test_desired_deleted_tombstone_cannot_resurrect(self):
        decision = self.decision(desired_lifecycle="deleted")
        self.assertEqual(decision.effective_status, "deleting")
        self.assertTrue(decision.runtime_hard_off)
        self.assertTrue(decision.runtime_owner_drain_required)

    def test_crash_loop_is_fresh_hard_failure_and_drains(self):
        decision = self.decision(ready_replicas=0, ready_containers=0, restart_count_5m=3)
        self.assertEqual(decision.effective_status, "unhealthy")
        self.assertEqual(decision.reason_codes, ("CRASH_LOOP",))
        self.assertTrue(decision.runtime_owner_drain_required)

    def test_unauthorized_workload_uid_is_quarantined(self):
        decision = self.decision(deployment_uid="foreign")
        self.assertEqual(decision.effective_status, "unknown")
        self.assertEqual(decision.reason_codes, ("UNAUTHORIZED_WORKLOAD_UID",))
        self.assertFalse(decision.runtime_owner_drain_required)

    def test_source_failure_never_fabricates_zero_or_running(self):
        decision = self.decision(source_ok=False, runtime_present=False, resource_count=0)
        self.assertEqual(decision.effective_status, "unknown")
        self.assertEqual(decision.reason_codes, ("SOURCE_FAILED",))

    def test_canonical_json_uses_rfc8785_utf16_key_order(self):
        # U+10000 sorts before U+E000 by UTF-16 code units, the reverse of
        # Python's code-point order.  This catches a subtle cross-language cut
        # digest split between Python Hive and JavaScript/TypeScript daemons.
        self.assertEqual(canonical_json({"\ue000": 1, "\U00010000": 2}), '{"𐀀":2,"":1}')


class RuntimeProjectionPersistenceTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.epoch = uuid.uuid4()
        self.daemon = DaemonRegistry.objects.create(
            daemon_id="rt-v2-test",
            fleet_id="fleet-v2-test",
            runtime=DaemonRegistry.RuntimeType.K3S,
            status=DaemonRegistry.Status.CONNECTED,
            selected_protocol_version=2,
            active_runtime_epoch=self.epoch,
        )
        self.agent = FleetAgent.objects.create(agent_username="projection-v2")
        self.desired = AgentRuntimeDesiredState.objects.create(
            agent=self.agent,
            desired_lifecycle=AgentRuntimeDesiredState.DesiredLifecycle.ACTIVE,
            desired_generation=4,
            lifecycle_executor=AgentRuntimeDesiredState.LifecycleExecutor.DAEMON_V2,
            executor_generation=2,
            expected_deployment_uid="deploy-current",
            workload_identity_generation=3,
            fleet_control_revision=9,
        )
        self.migration = AgentRuntimeMigrationState.objects.create(agent=self.agent)
        self.binding = AgentDaemonBinding.objects.create(
            agent=self.agent,
            active_owner_daemon=self.daemon,
            owner_generation=7,
            active_runtime_epoch=self.epoch,
            binding_state=AgentDaemonBinding.State.BOUND,
            owner_lease_expires_at=self.now + timedelta(seconds=90),
        )
        self.observation = AgentRuntimeObservation.objects.create(
            agent=self.agent,
            owner_daemon=self.daemon,
            owner_generation=7,
            runtime_epoch=self.epoch,
            frame_seq=41,
            observation_seq=11,
            source_adapter=AgentRuntimeObservation.Adapter.K3S,
            source_ok=True,
            snapshot_complete=True,
            observed_at=self.now,
            valid_until=self.now + timedelta(seconds=150),
            desired_generation=4,
            deployment_uid="deploy-current",
            deployment_generation=12,
            deployment_observed_generation=12,
            pod_uids=["pod-a"],
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
            final_u_digest="a" * 64,
        )

    def barrier_payload(self, **changes):
        entries = changes.pop("rl_entries", [])
        fences = []
        for index, collection_key in enumerate(("deployments", "pods"), start=1):
            fences.append({
                "schema_version": 1,
                "collection_key": collection_key,
                "mode": "list_watch",
                "list_resource_version": f"rv-{collection_key}/2",
                "barrier_resource_version": f"opaque:rv:{collection_key}",
                "watch_generation": index,
                "barrier_end_frame_seq": 80,
            })
        payload = {
            "snapshot_id": str(uuid.uuid4()),
            "R_collection_fences": fences,
            "L_snapshot_seq": 5,
            "L_barrier_seq": 5,
            "barrier_end_frame_seq": 80,
            "rl_count": len(entries),
            "rl_digest": canonical_digest(entries),
            "rl_entries": entries,
        }
        payload.update(changes)
        return payload

    def test_materialize_projection_writes_shared_facet_and_outbox(self):
        projection = materialize_runtime_projection(self.agent.pk, now=self.now)
        self.assertEqual(projection.effective_status, "running")
        self.assertTrue(projection.runtime_new_assignment_eligible)
        event = self.agent.runtime_availability_events.get()
        self.assertEqual(event.payload["effective_status"], "running")
        self.assertEqual(event.payload_hash, canonical_digest(event.payload))

    def test_h_manifest_contains_h_only_desired_member(self):
        self.observation.delete()
        manifest = materialize_h_manifest(self.daemon.pk)
        self.assertEqual(manifest["member_count"], 1)
        self.assertEqual(manifest["members"][0]["canonical_agent_id"], str(self.agent.pk))
        self.assertEqual(manifest["members_digest"], canonical_digest(manifest["members"]))

    def test_quiet_opaque_list_watch_barrier_activates_and_hive_owns_final_digest(self):
        candidate = stage_runtime_snapshot_candidate(
            daemon=self.daemon,
            owner_generation=7,
            executor_generation=2,
            runtime_epoch=self.epoch,
            barrier_payload=self.barrier_payload(),
            candidate_deadline=self.now + timedelta(minutes=5),
        )
        activated, universe = activate_runtime_snapshot_candidate(candidate.snapshot_id, now=self.now)
        self.assertEqual(activated.state, RuntimeSnapshotCandidate.State.ACTIVATED)
        self.assertTrue(activated.final_u_digest)
        self.assertTrue(any(entry["domain"] == "H" for entry in universe))

    def test_daemon_supplied_legacy_final_digest_is_rejected(self):
        with self.assertRaisesRegex(RuntimeProjectionError, "LEGACY_FINAL_DIGEST_REJECTED"):
            stage_runtime_snapshot_candidate(
                daemon=self.daemon,
                owner_generation=7,
                executor_generation=2,
                runtime_epoch=self.epoch,
                barrier_payload=self.barrier_payload(final_digest="daemon-must-not-own-this"),
                candidate_deadline=self.now + timedelta(minutes=5),
            )

    def test_h_mutation_between_ticket_and_activation_rejects(self):
        candidate = stage_runtime_snapshot_candidate(
            daemon=self.daemon,
            owner_generation=7,
            executor_generation=2,
            runtime_epoch=self.epoch,
            barrier_payload=self.barrier_payload(),
            candidate_deadline=self.now + timedelta(minutes=5),
        )
        self.desired.fleet_control_revision += 1
        self.desired.save(update_fields=["fleet_control_revision"])
        with self.assertRaisesRegex(RuntimeProjectionError, "H_CHANGED_DURING_STAGE"):
            activate_runtime_snapshot_candidate(candidate.snapshot_id, now=self.now)

    def test_staging_rejects_stale_owner_and_executor_ticket_generations(self):
        with self.assertRaisesRegex(RuntimeProjectionError, "OWNER_GENERATION_STALE"):
            stage_runtime_snapshot_candidate(
                daemon=self.daemon,
                owner_generation=999,
                executor_generation=2,
                runtime_epoch=self.epoch,
                barrier_payload=self.barrier_payload(),
                candidate_deadline=self.now + timedelta(minutes=5),
            )
        with self.assertRaisesRegex(RuntimeProjectionError, "EXECUTOR_GENERATION_STALE"):
            stage_runtime_snapshot_candidate(
                daemon=self.daemon,
                owner_generation=7,
                executor_generation=999,
                runtime_epoch=self.epoch,
                barrier_payload=self.barrier_payload(),
                candidate_deadline=self.now + timedelta(minutes=5),
            )

    def test_activation_rechecks_generation_ticket_against_current_h_edges(self):
        candidate = stage_runtime_snapshot_candidate(
            daemon=self.daemon,
            owner_generation=7,
            executor_generation=2,
            runtime_epoch=self.epoch,
            barrier_payload=self.barrier_payload(),
            candidate_deadline=self.now + timedelta(minutes=5),
        )
        self.binding.owner_generation = 8
        self.binding.save(update_fields=["owner_generation"])
        with self.assertRaisesRegex(RuntimeProjectionError, "OWNER_GENERATION_STALE"):
            activate_runtime_snapshot_candidate(candidate.snapshot_id, now=self.now)
        candidate.refresh_from_db()
        self.assertEqual(candidate.failure_code, "OWNER_GENERATION_STALE")

        self.binding.owner_generation = 7
        self.binding.save(update_fields=["owner_generation"])
        candidate = stage_runtime_snapshot_candidate(
            daemon=self.daemon,
            owner_generation=7,
            executor_generation=2,
            runtime_epoch=self.epoch,
            barrier_payload=self.barrier_payload(),
            candidate_deadline=self.now + timedelta(minutes=5),
        )
        self.desired.executor_generation = 3
        self.desired.save(update_fields=["executor_generation"])
        with self.assertRaisesRegex(RuntimeProjectionError, "EXECUTOR_GENERATION_STALE"):
            activate_runtime_snapshot_candidate(candidate.snapshot_id, now=self.now)
        candidate.refresh_from_db()
        self.assertEqual(candidate.failure_code, "EXECUTOR_GENERATION_STALE")

    def test_required_runtime_collections_and_closed_fences_fail_closed(self):
        payload = self.barrier_payload()
        payload["R_collection_fences"] = []
        with self.assertRaisesRegex(RuntimeProjectionError, "R_COLLECTIONS_MISSING"):
            stage_runtime_snapshot_candidate(
                daemon=self.daemon,
                owner_generation=7,
                executor_generation=2,
                runtime_epoch=self.epoch,
                barrier_payload=payload,
                candidate_deadline=self.now + timedelta(minutes=5),
            )

        payload = self.barrier_payload()
        del payload["R_collection_fences"][0]["watch_generation"]
        with self.assertRaisesRegex(RuntimeProjectionError, "R_FENCE_FIELDS_MISSING"):
            stage_runtime_snapshot_candidate(
                daemon=self.daemon,
                owner_generation=7,
                executor_generation=2,
                runtime_epoch=self.epoch,
                barrier_payload=payload,
                candidate_deadline=self.now + timedelta(minutes=5),
            )

    def test_barrier_schema_errors_are_stable_protocol_rejections(self):
        for changes, code in (
            ({"snapshot_id": "not-a-uuid"}, "SNAPSHOT_ID_INVALID"),
            ({"rl_count": "not-an-int"}, "RL_COUNT_INVALID"),
            ({"L_snapshot_seq": "NaN"}, "L_SNAPSHOT_SEQ_INVALID"),
        ):
            with self.subTest(code=code):
                with self.assertRaisesRegex(RuntimeProjectionError, code):
                    stage_runtime_snapshot_candidate(
                        daemon=self.daemon,
                        owner_generation=7,
                        executor_generation=2,
                        runtime_epoch=self.epoch,
                        barrier_payload=self.barrier_payload(**changes),
                        candidate_deadline=self.now + timedelta(minutes=5),
                    )

    def test_migration_hold_release_is_generation_fenced_and_idempotent(self):
        self.migration.migration_state = AgentRuntimeMigrationState.State.HELD
        self.migration.migration_generation = 3
        self.migration.source_row_version = "legacy-row-v17"
        self.migration.source_row_hash = "b" * 64
        self.migration.source_timestamp = self.now - timedelta(days=1)
        self.migration.hold_reason = "AMBIGUOUS_LEGACY_STATUS"
        self.migration.evidence = {"legacy_status": "provisioning", "source": "fleet_agent"}
        self.migration.save(update_fields=[
            "migration_state", "migration_generation", "source_row_version",
            "source_row_hash", "source_timestamp", "hold_reason", "evidence",
        ])
        citation = migration_evidence_citation(self.migration)
        operation_id = uuid.uuid4()
        released = resolve_migration_hold(
            self.agent.pk,
            migration_generation=3,
            target_lifecycle=AgentRuntimeDesiredState.DesiredLifecycle.STOPPED,
            actor="security-reviewer",
            reason="audited disposition",
            cited_evidence=citation,
            operation_id=operation_id,
            now=self.now,
        )
        self.desired.refresh_from_db()
        self.assertEqual(released.migration_state, AgentRuntimeMigrationState.State.RELEASED)
        self.assertEqual(self.desired.desired_lifecycle, AgentRuntimeDesiredState.DesiredLifecycle.STOPPED)
        self.assertEqual(self.desired.desired_generation, 5)
        command = AgentLifecycleCommand.objects.get(agent=self.agent)
        self.assertEqual(command.command_type, "stop")
        self.assertEqual(command.operation_id, operation_id)
        self.assertEqual(command.desired_generation, 5)
        self.assertEqual(command.payload["migration_evidence"], citation)
        self.assertEqual(command.payload_hash, canonical_digest(command.payload))
        again = resolve_migration_hold(
            self.agent.pk,
            migration_generation=3,
            target_lifecycle=AgentRuntimeDesiredState.DesiredLifecycle.STOPPED,
            actor="security-reviewer",
            reason="replay",
            cited_evidence=citation,
            operation_id=operation_id,
            now=self.now,
        )
        self.assertEqual(again.pk, released.pk)
        self.assertEqual(AgentLifecycleCommand.objects.filter(agent=self.agent).count(), 1)

    def test_migration_release_rejects_blank_audit_and_uncited_evidence(self):
        self.migration.migration_state = AgentRuntimeMigrationState.State.HELD
        self.migration.migration_generation = 3
        self.migration.source_row_version = "legacy-row-v17"
        self.migration.source_row_hash = "b" * 64
        self.migration.source_timestamp = self.now - timedelta(days=1)
        self.migration.hold_reason = "AMBIGUOUS_LEGACY_STATUS"
        self.migration.evidence = {"legacy_status": "provisioning"}
        self.migration.save()
        citation = migration_evidence_citation(self.migration)
        arguments = dict(
            agent_id=self.agent.pk,
            migration_generation=3,
            target_lifecycle=AgentRuntimeDesiredState.DesiredLifecycle.DELETED,
            actor="migration-reviewer",
            reason="audited disposition",
            cited_evidence=citation,
            operation_id=uuid.uuid4(),
            now=self.now,
        )
        with self.assertRaisesRegex(RuntimeProjectionError, "MIGRATION_ACTOR_REQUIRED"):
            resolve_migration_hold(**{**arguments, "actor": " "})
        with self.assertRaisesRegex(RuntimeProjectionError, "MIGRATION_REASON_REQUIRED"):
            resolve_migration_hold(**{**arguments, "reason": ""})
        with self.assertRaisesRegex(RuntimeProjectionError, "MIGRATION_EVIDENCE_MISMATCH"):
            resolve_migration_hold(**{
                **arguments,
                "cited_evidence": {**citation, "source_row_version": "other"},
            })
        self.migration.refresh_from_db()
        self.desired.refresh_from_db()
        self.assertEqual(self.migration.migration_state, AgentRuntimeMigrationState.State.HELD)
        self.assertEqual(self.desired.desired_generation, 4)
        self.assertFalse(AgentLifecycleCommand.objects.exists())

        released = resolve_migration_hold(**arguments)
        command = AgentLifecycleCommand.objects.get(agent=self.agent)
        self.assertEqual(released.target_lifecycle, AgentRuntimeDesiredState.DesiredLifecycle.DELETED)
        self.assertEqual(command.command_type, "delete")
        self.assertEqual(command.deadline, self.now + timedelta(minutes=10))
