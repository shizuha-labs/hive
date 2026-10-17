"""HIVE-277 §10 v16 runtime projection and convergence primitives.

This module is deliberately independent of the legacy ``FleetAgent.status``
writer.  It provides the dark v2 control/read plane: a pure total reducer,
Hive-owned H-manifest composition, barrier activation, leased boundary timers,
and the at-least-once availability outbox.  No function in this module reads
Kubernetes directly; the bound daemon is the only runtime observer/materializer.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
import hashlib
import json
import secrets
import uuid
from typing import Any, Iterable

from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    AgentDaemonBinding,
    AgentLifecycleCommand,
    AgentRuntimeDesiredState,
    AgentRuntimeMigrationState,
    AgentRuntimeObservation,
    AgentRuntimeProjection,
    FleetAgent,
    RuntimeAvailabilityOutbox,
    RuntimeBoundaryTimer,
    RuntimeSnapshotCandidate,
)


REDUCER_VERSION = 5
AVAILABILITY_SCHEMA_VERSION = 1
CLAIM_LEASE_SECONDS = 30
RETRY_BACKOFF_SECONDS = (1, 2, 4, 8, 16, 30, 60)
SUPPORTED_V2_ADAPTERS = frozenset({AgentRuntimeObservation.Adapter.K3S})
REQUIRED_R_COLLECTIONS = {
    AgentRuntimeObservation.Adapter.K3S: frozenset({"deployments", "pods"}),
}
R_COLLECTION_FENCE_FIELDS = frozenset({
    "schema_version",
    "collection_key",
    "mode",
    "list_resource_version",
    "barrier_resource_version",
    "watch_generation",
    "barrier_end_frame_seq",
})


class RuntimeProjectionError(ValueError):
    """A stable, fail-closed v2 protocol/projection rejection."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _closed_nonnegative_int(value: Any, code: str) -> int:
    """Parse a protocol integer without accepting booleans or coercing floats."""

    if isinstance(value, bool):
        raise RuntimeProjectionError(code)
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeProjectionError(code) from exc
    if isinstance(value, float) or parsed < 0 or str(value).strip() != str(parsed):
        raise RuntimeProjectionError(code)
    return parsed


def _closed_positive_int(value: Any, code: str) -> int:
    parsed = _closed_nonnegative_int(value, code)
    if parsed == 0:
        raise RuntimeProjectionError(code)
    return parsed


def _closed_uuid(value: Any, code: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise RuntimeProjectionError(code) from exc


def _is_sha256(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def canonical_json(value: Any) -> str:
    """Canonical JSON for the closed v2 schemas.

    HIVE-277 restricts the hashed structures to strings, booleans, nulls,
    lists, dictionaries and integers.  RFC 8785 sorts object keys by UTF-16
    code units (not Python's Unicode code-point order), so implement the small
    closed-domain serializer directly rather than relying on ``sort_keys``.
    """

    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, list):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("canonical JSON object keys must be strings")
        keys = sorted(value, key=lambda key: key.encode("utf-16-be", "surrogatepass"))
        return "{" + ",".join(
            f"{canonical_json(key)}:{canonical_json(value[key])}" for key in keys
        ) + "}"
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _iso(value):
    return value.isoformat() if value is not None else None


def _database_now():
    """Read the authoritative boundary clock from the database."""

    with connection.cursor() as cursor:
        cursor.execute("SELECT CURRENT_TIMESTAMP")
        return cursor.fetchone()[0]


def _is_expired(value, now) -> bool:
    return value is not None and now >= value


@dataclass(frozen=True)
class RuntimeProjectionInput:
    desired_lifecycle: str = AgentRuntimeDesiredState.DesiredLifecycle.ACTIVE
    desired_generation: int = 0
    operation_id: str | None = None
    operation_kind: str = ""
    operation_deadline: Any | None = None
    lifecycle_executor: str = AgentRuntimeDesiredState.LifecycleExecutor.HIVE_LEGACY
    executor_generation: int = 0
    expected_deployment_uid: str = ""
    workload_identity_generation: int = 0
    operator_disabled_transition: bool = False
    migration_state: str = AgentRuntimeMigrationState.State.NOT_REQUIRED
    binding_state: str = AgentDaemonBinding.State.UNBOUND
    owner_generation: int = 0
    active_runtime_epoch: str | None = None
    owner_lease_expires_at: Any | None = None
    adapter_supported: bool = False
    observation_present: bool = False
    observation_owner_generation: int = 0
    observation_runtime_epoch: str | None = None
    observation_seq: int = 0
    source_ok: bool = False
    snapshot_complete: bool = False
    evidence_valid_until: Any | None = None
    observation_desired_generation: int = 0
    runtime_present: bool = False
    daemon_local_present: bool = False
    resource_count: int = 0
    classification: str = ""
    deployment_uid: str = ""
    deployment_generation: int | None = None
    deployment_observed_generation: int | None = None
    required_replicas: int = 1
    ready_replicas: int = 0
    required_containers: int = 0
    ready_containers: int = 0
    phase: str = ""
    restart_count_5m: int = 0
    unready_since: Any | None = None
    progress_deadline_exceeded: bool = False
    workload_transition_state: str = ""


@dataclass(frozen=True)
class RuntimeDecision:
    effective_status: str
    runtime_new_assignment_eligible: bool
    runtime_owner_drain_required: bool
    runtime_hard_off: bool
    reason_codes: tuple[str, ...]
    action_code: str = ""
    evidence_valid_until: Any | None = None
    next_transition_at: Any | None = None
    reducer_version: int = REDUCER_VERSION

    def payload(self) -> dict[str, Any]:
        value = asdict(self)
        value["reason_codes"] = list(self.reason_codes)
        value["evidence_valid_until"] = _iso(self.evidence_valid_until)
        value["next_transition_at"] = _iso(self.next_transition_at)
        return value


def _decision(
    inp: RuntimeProjectionInput,
    status: str,
    eligible: bool,
    drain: bool,
    hard_off: bool,
    reason: str,
    action: str = "",
    *,
    now=None,
) -> RuntimeDecision:
    now = now or timezone.now()
    boundaries = [
        inp.evidence_valid_until,
        inp.operation_deadline,
        inp.owner_lease_expires_at,
    ]
    if inp.unready_since is not None:
        boundaries.append(inp.unready_since + timedelta(seconds=300))
    # A recomputation at a crossed boundary must schedule the *next* boundary,
    # not recreate the already-completed timer.  Otherwise an evidence-expiry
    # event could mask a later owner-lease/operation transition indefinitely.
    future = [value for value in boundaries if value is not None and value > now]
    return RuntimeDecision(
        effective_status=status,
        runtime_new_assignment_eligible=eligible,
        runtime_owner_drain_required=drain,
        runtime_hard_off=hard_off,
        reason_codes=(reason,),
        action_code=action,
        evidence_valid_until=inp.evidence_valid_until,
        next_transition_at=min(future) if future else None,
    )


def reduce_runtime_state(inp: RuntimeProjectionInput, *, now=None) -> RuntimeDecision:
    """Evaluate the accepted §10.5 table top-to-bottom.

    Unknown/transient evidence always excludes new work without stealing the
    existing owner. Only desired hard-offs or fresh authoritative hard failure
    require owner drain.
    """

    now = now or timezone.now()
    lifecycle = inp.desired_lifecycle

    def decide(status, eligible, drain, hard_off, reason, action=""):
        return _decision(
            inp, status, eligible, drain, hard_off, reason, action, now=now,
        )

    if lifecycle == AgentRuntimeDesiredState.DesiredLifecycle.DELETED:
        absence_proven = (
            inp.observation_present
            and inp.source_ok
            and inp.snapshot_complete
            and not _is_expired(inp.evidence_valid_until, now)
            and not inp.runtime_present
            and inp.resource_count == 0
        )
        if absence_proven:
            return decide("deleted", False, True, True, "DESIRED_DELETED")
        return decide("deleting", False, True, True, "DELETE_IN_PROGRESS", "delete")
    if lifecycle == AgentRuntimeDesiredState.DesiredLifecycle.ARCHIVED:
        return decide("archived", False, True, True, "DESIRED_ARCHIVED", "stop_leak")
    if lifecycle == AgentRuntimeDesiredState.DesiredLifecycle.BLOCKED:
        return decide("blocked", False, True, True, "DESIRED_BLOCKED")
    if inp.operator_disabled_transition:
        return decide("blocked", False, True, True, "LEGACY_OPERATOR_DISABLED", "migrate")
    if inp.migration_state == AgentRuntimeMigrationState.State.HELD:
        return decide("unknown", False, False, False, "MIGRATION_HOLD", "resolve_migration_hold")
    if lifecycle == AgentRuntimeDesiredState.DesiredLifecycle.STOPPED:
        if inp.operation_kind == AgentRuntimeDesiredState.OperationKind.STOP and not _is_expired(
            inp.operation_deadline, now
        ):
            return decide("stopping", False, True, True, "STOP_IN_PROGRESS", "wait")
        return decide("stopped", False, True, True, "DESIRED_STOPPED", "stop_leak")

    if inp.binding_state != AgentDaemonBinding.State.BOUND:
        reason = "AMBIGUOUS_OWNER" if inp.binding_state == AgentDaemonBinding.State.HANDING_OFF else "OWNER_UNBOUND"
        return decide("unknown", False, False, False, reason, "hold_alert")
    if not inp.adapter_supported:
        return decide("unknown", False, False, False, "PROTOCOL_UNSUPPORTED", "hold_alert")
    if not inp.observation_present:
        return decide("unknown", False, False, False, "OBSERVATION_MISSING", "request_snapshot")
    if not inp.source_ok:
        return decide("unknown", False, False, False, "SOURCE_FAILED", "hold_alert")
    if not inp.snapshot_complete:
        return decide("unknown", False, False, False, "SNAPSHOT_INCOMPLETE", "request_snapshot")
    if _is_expired(inp.owner_lease_expires_at, now):
        return decide("unknown", False, False, False, "OWNER_LEASE_EXPIRED", "request_snapshot")
    if _is_expired(inp.evidence_valid_until, now):
        return decide("unknown", False, False, False, "OBSERVATION_EXPIRED", "request_snapshot")
    if (
        inp.observation_owner_generation != inp.owner_generation
        or inp.observation_runtime_epoch != inp.active_runtime_epoch
        or inp.observation_desired_generation != inp.desired_generation
    ):
        return decide("unknown", False, False, False, "STALE_EVIDENCE", "request_snapshot")

    if inp.classification == "ambiguous_owner":
        return decide("unknown", False, False, False, "AMBIGUOUS_OWNER", "hold_alert")
    if inp.classification == "duplicate_runtime_resources" or inp.resource_count > 1:
        return decide("unknown", False, False, False, "DUPLICATE_RESOURCE", "hold_alert")
    if inp.classification == "runtime_unmanaged":
        return decide("unknown", False, False, False, "RUNTIME_UNMANAGED", "explicit_adoption")
    if inp.classification == "runtime_unmanaged_terminal":
        return decide("unhealthy", False, True, False, "RUNTIME_UNMANAGED_TERMINAL", "explicit_adoption")

    if inp.operation_kind in {
        AgentRuntimeDesiredState.OperationKind.PROVISION,
        AgentRuntimeDesiredState.OperationKind.START,
        AgentRuntimeDesiredState.OperationKind.ADOPT,
        AgentRuntimeDesiredState.OperationKind.HANDOFF,
    }:
        if not _is_expired(inp.operation_deadline, now) and not (
            inp.ready_replicas >= inp.required_replicas
            and inp.ready_containers >= inp.required_containers
        ):
            status = "provisioning" if inp.operation_kind == AgentRuntimeDesiredState.OperationKind.PROVISION else "starting"
            return decide(status, False, False, False, "OPERATION_IN_PROGRESS", "wait")
        if _is_expired(inp.operation_deadline, now):
            return decide("unhealthy", False, False, False, "OPERATION_DEADLINE_EXPIRED", "bounded_repair")

    if (
        inp.deployment_generation is not None
        and (
            inp.deployment_observed_generation is None
            or inp.deployment_observed_generation < inp.deployment_generation
        )
    ):
        return decide("starting", False, False, False, "ROLLOUT_NOT_OBSERVED", "wait")

    if inp.workload_transition_state in {"pending", "candidate_seen"}:
        return decide("starting", False, False, False, "WORKLOAD_REPLACEMENT_PENDING", "wait")
    if inp.workload_transition_state in {"failed", "superseded"}:
        return decide("unhealthy", False, False, False, "WORKLOAD_REPLACEMENT_FAILED")
    if inp.workload_transition_state == "expired":
        return decide("unhealthy", False, False, False, "WORKLOAD_REPLACEMENT_EXPIRED")

    if inp.deployment_uid and inp.expected_deployment_uid and inp.deployment_uid != inp.expected_deployment_uid:
        classification = inp.classification or "unauthorized_workload_uid"
        if classification == "retired_workload_reappeared":
            return decide("unhealthy", False, True, False, "RETIRED_WORKLOAD_REAPPEARED", "fenced_cleanup")
        return decide("unknown", False, False, False, "UNAUTHORIZED_WORKLOAD_UID", "quarantine_alert")

    ready = (
        inp.runtime_present
        and inp.resource_count == 1
        and inp.deployment_generation is not None
        and inp.deployment_observed_generation is not None
        and inp.deployment_observed_generation >= inp.deployment_generation
        and inp.ready_replicas >= inp.required_replicas
        and inp.ready_containers >= inp.required_containers
        and inp.phase.lower() not in {"failed", "stopped"}
    )
    if ready:
        return decide("running", True, False, False, "READY_CURRENT")

    if not inp.runtime_present or inp.phase.lower() in {"stopped", "succeeded"}:
        reason = "RUNTIME_STOPPED" if inp.phase.lower() == "stopped" else "RUNTIME_ABSENT"
        return decide("unhealthy", False, True, False, reason, "bounded_repair")
    if inp.progress_deadline_exceeded:
        return decide("unhealthy", False, True, False, "TERMINAL_FAILURE", "bounded_repair")
    if inp.restart_count_5m >= 3:
        return decide("unhealthy", False, True, False, "CRASH_LOOP", "bounded_repair")
    if inp.unready_since is not None and now >= inp.unready_since + timedelta(seconds=300):
        return decide("unhealthy", False, True, False, "UNREADY_DWELL_EXCEEDED", "bounded_repair")
    if inp.runtime_present:
        return decide("starting", False, False, False, "TRANSIENT_UNREADY", "wait")
    return decide("unknown", False, False, False, "UNCLASSIFIED_EVIDENCE", "hold_alert")


def build_reducer_input(agent: FleetAgent) -> RuntimeProjectionInput:
    try:
        desired = agent.desired_state
    except AgentRuntimeDesiredState.DoesNotExist:
        desired = None
    try:
        migration = agent.runtime_migration_state
    except AgentRuntimeMigrationState.DoesNotExist:
        migration = None
    try:
        binding = agent.daemon_binding
    except AgentDaemonBinding.DoesNotExist:
        binding = None
    try:
        observation = agent.runtime_observation
    except AgentRuntimeObservation.DoesNotExist:
        observation = None
    transition = agent.workload_identity_transitions.order_by("-created_at").first()

    return RuntimeProjectionInput(
        desired_lifecycle=(desired.desired_lifecycle if desired else AgentRuntimeDesiredState.DesiredLifecycle.ACTIVE),
        desired_generation=(desired.desired_generation if desired else 0),
        operation_id=(str(desired.operation_id) if desired and desired.operation_id else None),
        operation_kind=(desired.operation_kind if desired else ""),
        operation_deadline=(desired.operation_deadline if desired else None),
        lifecycle_executor=(desired.lifecycle_executor if desired else AgentRuntimeDesiredState.LifecycleExecutor.HIVE_LEGACY),
        executor_generation=(desired.executor_generation if desired else 0),
        expected_deployment_uid=(desired.expected_deployment_uid if desired else ""),
        workload_identity_generation=(desired.workload_identity_generation if desired else 0),
        operator_disabled_transition=(desired.operator_disabled_transition if desired else False),
        migration_state=(migration.migration_state if migration else AgentRuntimeMigrationState.State.NOT_REQUIRED),
        binding_state=(binding.binding_state if binding else AgentDaemonBinding.State.UNBOUND),
        owner_generation=(binding.owner_generation if binding else 0),
        active_runtime_epoch=(str(binding.active_runtime_epoch) if binding and binding.active_runtime_epoch else None),
        owner_lease_expires_at=(binding.owner_lease_expires_at if binding else None),
        adapter_supported=(observation.source_adapter in SUPPORTED_V2_ADAPTERS if observation else False),
        observation_present=observation is not None,
        observation_owner_generation=(observation.owner_generation if observation else 0),
        observation_runtime_epoch=(str(observation.runtime_epoch) if observation else None),
        observation_seq=(observation.observation_seq if observation else 0),
        source_ok=(observation.source_ok if observation else False),
        snapshot_complete=(observation.snapshot_complete if observation else False),
        evidence_valid_until=(observation.valid_until if observation else None),
        observation_desired_generation=(observation.desired_generation if observation else 0),
        runtime_present=(observation.runtime_present if observation else False),
        daemon_local_present=(observation.daemon_local_present if observation else False),
        resource_count=(observation.resource_count if observation else 0),
        classification=(observation.classification if observation else ""),
        deployment_uid=(observation.deployment_uid if observation else ""),
        deployment_generation=(observation.deployment_generation if observation else None),
        deployment_observed_generation=(observation.deployment_observed_generation if observation else None),
        required_replicas=(observation.required_replicas if observation else 1),
        ready_replicas=(observation.ready_replicas if observation else 0),
        required_containers=(observation.required_containers if observation else 0),
        ready_containers=(observation.ready_containers if observation else 0),
        phase=(observation.phase if observation else ""),
        restart_count_5m=(observation.restart_count_5m if observation else 0),
        unready_since=(observation.unready_since if observation else None),
        progress_deadline_exceeded=(observation.progress_deadline_exceeded if observation else False),
        workload_transition_state=(transition.state if transition else ""),
    )


def _availability_payload(agent: FleetAgent, projection: AgentRuntimeProjection) -> dict[str, Any]:
    return {
        "schema_version": AVAILABILITY_SCHEMA_VERSION,
        "agent_id": str(agent.pk),
        "agent_username": agent.agent_username,
        "effective_status": projection.effective_status,
        "runtime_new_assignment_eligible": projection.runtime_new_assignment_eligible,
        "runtime_owner_drain_required": projection.runtime_owner_drain_required,
        "runtime_hard_off": projection.runtime_hard_off,
        "reason_codes": projection.reason_codes,
        "reducer_version": projection.reducer_version,
        "decision_version": projection.decision_version,
        "evidence_valid_until": _iso(projection.evidence_valid_until),
    }


@transaction.atomic
def materialize_runtime_projection(agent_id, *, now=None) -> AgentRuntimeProjection:
    """Recompute one facet and transactionally emit timer + invalidation row."""

    now = now or _database_now()
    agent = FleetAgent.objects.select_for_update().get(pk=agent_id)
    inp = build_reducer_input(agent)
    decision = reduce_runtime_state(inp, now=now)
    decision_hash = canonical_digest(decision.payload())

    projection = AgentRuntimeProjection.objects.select_for_update().filter(agent=agent).first()
    changed = projection is None or projection.decision_hash != decision_hash
    if projection is None:
        projection = AgentRuntimeProjection(agent=agent, decision_version=1, decision_hash=decision_hash)
    elif changed:
        projection.decision_version += 1

    try:
        projection.observation = agent.runtime_observation
    except AgentRuntimeObservation.DoesNotExist:
        projection.observation = None
    projection.effective_status = decision.effective_status
    projection.runtime_new_assignment_eligible = decision.runtime_new_assignment_eligible
    projection.runtime_owner_drain_required = decision.runtime_owner_drain_required
    projection.runtime_hard_off = decision.runtime_hard_off
    projection.reason_codes = list(decision.reason_codes)
    projection.action_code = decision.action_code
    projection.evidence_valid_until = decision.evidence_valid_until
    projection.next_transition_at = decision.next_transition_at
    projection.reducer_version = REDUCER_VERSION
    projection.desired_generation = inp.desired_generation
    projection.owner_generation = inp.owner_generation
    projection.runtime_epoch = uuid.UUID(inp.active_runtime_epoch) if inp.active_runtime_epoch else None
    projection.observation_seq = inp.observation_seq
    projection.decision_hash = decision_hash
    projection.save()

    if changed:
        payload = _availability_payload(agent, projection)
        key = ":".join(
            [
                str(agent.pk),
                str(inp.owner_generation),
                inp.active_runtime_epoch or "none",
                str(inp.observation_seq),
                str(REDUCER_VERSION),
                str(projection.decision_version),
            ]
        )
        RuntimeAvailabilityOutbox.objects.get_or_create(
            idempotency_key=key,
            defaults={
                "agent": agent,
                "owner_generation": inp.owner_generation,
                "runtime_epoch": projection.runtime_epoch,
                "observation_seq": inp.observation_seq,
                "reducer_version": REDUCER_VERSION,
                "decision_version": projection.decision_version,
                "payload": payload,
                "payload_hash": canonical_digest(payload),
            },
        )
        RuntimeAvailabilityOutbox.objects.filter(agent=agent).exclude(
            decision_version=projection.decision_version
        ).filter(
            Q(state=RuntimeAvailabilityOutbox.State.PENDING)
            | Q(
                state=RuntimeAvailabilityOutbox.State.CLAIMED,
                claim_lease_until__lte=now,
            )
        ).update(
            state=RuntimeAvailabilityOutbox.State.SUPERSEDED,
            claim_owner="",
            claim_token="",
            claim_lease_until=None,
        )

    if decision.next_transition_at is not None:
        fingerprint_payload = {
            "agent_id": str(agent.pk),
            "owner_generation": inp.owner_generation,
            "runtime_epoch": inp.active_runtime_epoch,
            "observation_seq": inp.observation_seq,
            "desired_generation": inp.desired_generation,
            "boundary_time": _iso(decision.next_transition_at),
            "reducer_version": REDUCER_VERSION,
        }
        fingerprint = canonical_digest(fingerprint_payload)
        RuntimeBoundaryTimer.objects.get_or_create(
            boundary_fingerprint=fingerprint,
            defaults={
                "agent": agent,
                "boundary_kind": "projection_boundary",
                "due_at": decision.next_transition_at,
                "next_attempt_at": decision.next_transition_at,
            },
        )
        RuntimeBoundaryTimer.objects.filter(
            agent=agent,
            state=RuntimeBoundaryTimer.State.PENDING,
        ).exclude(boundary_fingerprint=fingerprint).update(
            state=RuntimeBoundaryTimer.State.SUPERSEDED,
            completed_at=now,
        )
    return projection


def _h_member(desired, binding, migration, role: str) -> dict[str, Any]:
    operation = {
        "id": str(desired.operation_id) if desired.operation_id else None,
        "kind": desired.operation_kind or None,
        "started_at": _iso(desired.operation_started_at),
        "deadline": _iso(desired.operation_deadline),
    }
    return {
        "canonical_agent_id": str(desired.agent_id),
        "membership_role": role,
        "desired_lifecycle": desired.desired_lifecycle,
        "desired_generation": desired.desired_generation,
        "operation": operation,
        "binding_state": binding.binding_state,
        "active_owner_daemon_id": binding.active_owner_daemon_id,
        "active_owner_generation": binding.owner_generation,
        "active_runtime_epoch": str(binding.active_runtime_epoch) if binding.active_runtime_epoch else None,
        "candidate_owner_daemon_id": binding.candidate_owner_daemon_id,
        "candidate_owner_generation": binding.candidate_owner_generation,
        "candidate_runtime_epoch": str(binding.candidate_runtime_epoch) if binding.candidate_runtime_epoch else None,
        "lifecycle_executor": desired.lifecycle_executor,
        "executor_generation": desired.executor_generation,
        "migration_state": migration.migration_state if migration else AgentRuntimeMigrationState.State.NOT_REQUIRED,
        "migration_generation": migration.migration_generation if migration else 0,
        "expected_deployment_uid": desired.expected_deployment_uid or None,
        "workload_identity_generation": desired.workload_identity_generation,
        "operator_disabled": desired.operator_disabled_transition,
        "fleet_control_revision": desired.fleet_control_revision,
    }


def materialize_h_manifest(daemon_id, *, lock=False) -> dict[str, Any]:
    """Build the closed Hive-local H manifest for one daemon.

    The daemon never reconstructs or supplies H. Candidate and active binding
    edges are materialized independently so a handoff cannot omit either role.
    """

    queryset = AgentRuntimeDesiredState.objects.select_related("agent")
    if lock:
        queryset = queryset.select_for_update()
    desired_rows = list(queryset.order_by("agent_id"))
    agent_ids = [desired.agent_id for desired in desired_rows]
    binding_queryset = AgentDaemonBinding.objects.filter(agent_id__in=agent_ids)
    migration_queryset = AgentRuntimeMigrationState.objects.filter(agent_id__in=agent_ids)
    if lock:
        # Lock each Hive-owned H table explicitly.  select_for_update across
        # nullable outer joins is rejected by PostgreSQL and would also leave
        # the ownership/migration rows outside the cut's write fence.
        binding_queryset = binding_queryset.select_for_update()
        migration_queryset = migration_queryset.select_for_update()
    bindings = {binding.agent_id: binding for binding in binding_queryset}
    migrations = {migration.agent_id: migration for migration in migration_queryset}
    members: list[dict[str, Any]] = []
    revisions: list[int] = []
    for desired in desired_rows:
        binding = bindings.get(desired.agent_id)
        if binding is None:
            continue
        migration = migrations.get(desired.agent_id)
        if (
            binding.binding_state == AgentDaemonBinding.State.BOUND
            and binding.active_owner_daemon_id == daemon_id
        ):
            members.append(_h_member(desired, binding, migration, "active_owner"))
            revisions.append(desired.fleet_control_revision)
        if (
            binding.binding_state in {AgentDaemonBinding.State.ADOPTING, AgentDaemonBinding.State.HANDING_OFF}
            and binding.candidate_owner_daemon_id == daemon_id
        ):
            members.append(_h_member(desired, binding, migration, "candidate_owner"))
            revisions.append(desired.fleet_control_revision)
    members.sort(key=lambda member: (
        str(member["canonical_agent_id"]).encode("utf-8"),
        str(member["membership_role"]).encode("utf-8"),
    ))
    return {
        "schema_version": 1,
        "daemon_id": daemon_id,
        "fleet_control_revision": max(revisions, default=0),
        "members": members,
        "member_count": len(members),
        "members_digest": canonical_digest(members),
    }


def _normalize_rl_entries(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(entries, list):
        raise RuntimeProjectionError("RL_ENTRIES_INVALID", "entries must be a list")
    normalized = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeProjectionError("RL_ENTRY_INVALID", "entry must be an object")
        closed_entry = dict(entry)
        try:
            canonical_json(closed_entry)
        except (TypeError, ValueError, UnicodeError) as exc:
            raise RuntimeProjectionError("RL_ENTRY_INVALID", "entry is not canonical JSON") from exc
        normalized.append(closed_entry)
    normalized.sort(key=lambda entry: canonical_json(entry).encode("utf-8"))
    return normalized


def _validate_ticket_generations(
    h_manifest: dict[str, Any],
    *,
    owner_generation: int,
    executor_generation: int,
) -> None:
    """Fence the daemon ticket against every matched Hive-owned H edge.

    The scalar ticket generations are valid only when every member in this
    daemon cut belongs to that exact owner/executor generation.  Mixed or stale
    edges require a new ticket rather than accepting a partly foreign cut.
    """

    members = h_manifest.get("members") or []
    if not members:
        if owner_generation != 0:
            raise RuntimeProjectionError("OWNER_GENERATION_STALE")
        if executor_generation != 0:
            raise RuntimeProjectionError("EXECUTOR_GENERATION_STALE")
        return
    for member in members:
        role = member.get("membership_role")
        expected_owner = (
            member.get("active_owner_generation")
            if role == "active_owner"
            else member.get("candidate_owner_generation")
        )
        if expected_owner != owner_generation:
            raise RuntimeProjectionError(
                "OWNER_GENERATION_STALE",
                f"{member.get('canonical_agent_id')}:{role}",
            )
        if member.get("executor_generation") != executor_generation:
            raise RuntimeProjectionError(
                "EXECUTOR_GENERATION_STALE",
                f"{member.get('canonical_agent_id')}:{role}",
            )


@transaction.atomic
def stage_runtime_snapshot_candidate(
    *,
    daemon,
    owner_generation: int,
    executor_generation: int,
    runtime_epoch,
    barrier_payload: dict[str, Any],
    candidate_deadline,
) -> RuntimeSnapshotCandidate:
    """Validate and durably stage a daemon-supplied R∪L barrier.

    A daemon-supplied ``final_digest`` is a hard protocol rejection; Hive alone
    computes the final H∪R∪L digest during activation.
    """

    if not isinstance(barrier_payload, dict):
        raise RuntimeProjectionError("BARRIER_PAYLOAD_INVALID")
    if "final_digest" in barrier_payload:
        raise RuntimeProjectionError("LEGACY_FINAL_DIGEST_REJECTED")
    required = {
        "snapshot_id",
        "R_collection_fences",
        "L_snapshot_seq",
        "L_barrier_seq",
        "barrier_end_frame_seq",
        "rl_count",
        "rl_digest",
        "rl_entries",
    }
    missing = sorted(required - set(barrier_payload))
    if missing:
        raise RuntimeProjectionError("BARRIER_FIELDS_MISSING", ",".join(missing))
    unknown = sorted(set(barrier_payload) - required)
    if unknown:
        raise RuntimeProjectionError("BARRIER_FIELDS_UNKNOWN", ",".join(unknown))

    snapshot_id = _closed_uuid(barrier_payload["snapshot_id"], "SNAPSHOT_ID_INVALID")
    if RuntimeSnapshotCandidate.objects.filter(snapshot_id=snapshot_id).exists():
        raise RuntimeProjectionError("SNAPSHOT_ID_REUSED")
    if not isinstance(candidate_deadline, datetime):
        raise RuntimeProjectionError("CANDIDATE_DEADLINE_INVALID")
    owner_generation = _closed_nonnegative_int(owner_generation, "OWNER_GENERATION_INVALID")
    executor_generation = _closed_nonnegative_int(executor_generation, "EXECUTOR_GENERATION_INVALID")
    runtime_epoch = _closed_uuid(runtime_epoch, "RUNTIME_EPOCH_INVALID")
    barrier_end_frame_seq = _closed_positive_int(
        barrier_payload["barrier_end_frame_seq"], "BARRIER_FRAME_SEQ_INVALID",
    )
    l_snapshot_seq = _closed_nonnegative_int(
        barrier_payload["L_snapshot_seq"], "L_SNAPSHOT_SEQ_INVALID",
    )
    l_barrier_seq = _closed_nonnegative_int(
        barrier_payload["L_barrier_seq"], "L_BARRIER_SEQ_INVALID",
    )
    if l_barrier_seq < l_snapshot_seq:
        raise RuntimeProjectionError("L_BARRIER_BEFORE_SNAPSHOT")

    fences = barrier_payload["R_collection_fences"]
    if not isinstance(fences, list):
        raise RuntimeProjectionError("R_FENCES_INVALID")
    seen_collections = set()
    normalized_fences = []
    for fence in fences:
        if not isinstance(fence, dict):
            raise RuntimeProjectionError("R_FENCE_INVALID")
        missing_fence_fields = sorted(R_COLLECTION_FENCE_FIELDS - set(fence))
        unknown_fence_fields = sorted(set(fence) - R_COLLECTION_FENCE_FIELDS)
        if missing_fence_fields:
            raise RuntimeProjectionError("R_FENCE_FIELDS_MISSING", ",".join(missing_fence_fields))
        if unknown_fence_fields:
            raise RuntimeProjectionError("R_FENCE_FIELDS_UNKNOWN", ",".join(unknown_fence_fields))
        if _closed_positive_int(fence["schema_version"], "R_FENCE_SCHEMA_INVALID") != 1:
            raise RuntimeProjectionError("R_FENCE_SCHEMA_INVALID")
        collection_key = str(fence.get("collection_key") or "")
        if not collection_key or collection_key in seen_collections:
            raise RuntimeProjectionError("R_FENCE_DUPLICATE", collection_key)
        seen_collections.add(collection_key)
        if fence.get("mode") not in {"streaming_list", "list_watch"}:
            raise RuntimeProjectionError("R_FENCE_MODE_INVALID", collection_key)
        # Resource versions remain opaque strings; do not parse/order them.
        for key in ("list_resource_version", "barrier_resource_version"):
            if not isinstance(fence.get(key), str) or not fence.get(key):
                raise RuntimeProjectionError("R_RESOURCE_VERSION_INVALID", f"{collection_key}:{key}")

        watch_generation = _closed_positive_int(
            fence["watch_generation"], "R_WATCH_GENERATION_INVALID",
        )
        fence_barrier_seq = _closed_positive_int(
            fence["barrier_end_frame_seq"], "R_BARRIER_FRAME_SEQ_INVALID",
        )
        if fence_barrier_seq != barrier_end_frame_seq:
            raise RuntimeProjectionError("R_BARRIER_FRAME_SEQ_MISMATCH", collection_key)
        normalized_fences.append({
            "schema_version": 1,
            "collection_key": collection_key,
            "mode": fence["mode"],
            "list_resource_version": fence["list_resource_version"],
            "barrier_resource_version": fence["barrier_resource_version"],
            "watch_generation": watch_generation,
            "barrier_end_frame_seq": fence_barrier_seq,
        })

    adapter = str(getattr(daemon, "runtime", "") or "")
    required_collections = REQUIRED_R_COLLECTIONS.get(adapter)
    if required_collections is None:
        raise RuntimeProjectionError("SOURCE_ADAPTER_UNSUPPORTED", adapter)
    missing_collections = sorted(required_collections - seen_collections)
    unknown_collections = sorted(seen_collections - required_collections)
    if missing_collections:
        raise RuntimeProjectionError("R_COLLECTIONS_MISSING", ",".join(missing_collections))
    if unknown_collections:
        raise RuntimeProjectionError("R_COLLECTIONS_UNKNOWN", ",".join(unknown_collections))

    rl_entries = _normalize_rl_entries(barrier_payload["rl_entries"])
    rl_count = _closed_nonnegative_int(barrier_payload["rl_count"], "RL_COUNT_INVALID")
    if rl_count != len(rl_entries):
        raise RuntimeProjectionError("RL_COUNT_MISMATCH")
    if not _is_sha256(barrier_payload["rl_digest"]):
        raise RuntimeProjectionError("RL_DIGEST_INVALID")
    if barrier_payload["rl_digest"] != canonical_digest(rl_entries):
        raise RuntimeProjectionError("RL_DIGEST_MISMATCH")
    h_manifest = materialize_h_manifest(daemon.pk, lock=True)
    _validate_ticket_generations(
        h_manifest,
        owner_generation=owner_generation,
        executor_generation=executor_generation,
    )
    candidate = RuntimeSnapshotCandidate.objects.create(
        snapshot_id=snapshot_id,
        daemon=daemon,
        owner_generation=owner_generation,
        executor_generation=executor_generation,
        runtime_epoch=runtime_epoch,
        h_manifest=h_manifest,
        h_revision=h_manifest["fleet_control_revision"],
        h_member_count=h_manifest["member_count"],
        h_members_digest=h_manifest["members_digest"],
        r_collection_fences=normalized_fences,
        l_snapshot_seq=l_snapshot_seq,
        l_barrier_seq=l_barrier_seq,
        barrier_end_frame_seq=barrier_end_frame_seq,
        rl_entries=rl_entries,
        rl_count=len(rl_entries),
        rl_digest=canonical_digest(rl_entries),
        state=RuntimeSnapshotCandidate.State.READY,
        candidate_deadline=candidate_deadline,
    )
    return candidate


def activate_runtime_snapshot_candidate(snapshot_id, *, now=None) -> tuple[RuntimeSnapshotCandidate, list[dict[str, Any]]]:
    """Atomically re-fence H and compute the Hive-owned final U digest.

    A semantic rejection is itself durable protocol evidence.  Commit the
    candidate's terminal state before raising the stable error to the caller;
    callers may then advance an exact replay cursor without leaving a READY
    candidate behind.
    """

    now = now or _database_now()
    rejection = None
    universe: list[dict[str, Any]] = []
    with transaction.atomic():
        candidate = RuntimeSnapshotCandidate.objects.select_for_update().select_related("daemon").get(
            snapshot_id=snapshot_id
        )
        if candidate.state != RuntimeSnapshotCandidate.State.READY:
            rejection = ("CANDIDATE_NOT_READY", candidate.state)
        elif now >= candidate.candidate_deadline:
            candidate.state = RuntimeSnapshotCandidate.State.EXPIRED
            candidate.failure_code = "CANDIDATE_DEADLINE_EXPIRED"
            candidate.save(update_fields=["state", "failure_code"])
            rejection = ("CANDIDATE_DEADLINE_EXPIRED", "")
        else:
            current_h = materialize_h_manifest(candidate.daemon_id, lock=True)
            try:
                _validate_ticket_generations(
                    current_h,
                    owner_generation=candidate.owner_generation,
                    executor_generation=candidate.executor_generation,
                )
            except RuntimeProjectionError as exc:
                candidate.state = RuntimeSnapshotCandidate.State.REJECTED
                candidate.failure_code = exc.code
                candidate.save(update_fields=["state", "failure_code"])
                rejection = (exc.code, exc.detail)
            if rejection is None:
                if (
                    current_h["fleet_control_revision"] != candidate.h_revision
                    or current_h["member_count"] != candidate.h_member_count
                    or current_h["members_digest"] != candidate.h_members_digest
                ):
                    candidate.state = RuntimeSnapshotCandidate.State.REJECTED
                    candidate.failure_code = "H_CHANGED_DURING_STAGE"
                    candidate.save(update_fields=["state", "failure_code"])
                    rejection = ("H_CHANGED_DURING_STAGE", "")
            if rejection is None:
                rl_entries = _normalize_rl_entries(candidate.rl_entries)
                if len(rl_entries) != candidate.rl_count or canonical_digest(rl_entries) != candidate.rl_digest:
                    candidate.state = RuntimeSnapshotCandidate.State.REJECTED
                    candidate.failure_code = "RL_DIGEST_MISMATCH"
                    candidate.save(update_fields=["state", "failure_code"])
                    rejection = ("RL_DIGEST_MISMATCH", "")
                else:
                    universe = [
                        {"domain": "H", "member": member} for member in current_h["members"]
                    ] + [
                        {"domain": str(entry.get("domain") or "R_L"), "entry": entry}
                        for entry in rl_entries
                    ]
                    universe.sort(key=lambda entry: canonical_json(entry).encode("utf-8"))
                    candidate.final_u_digest = canonical_digest(universe)
                    candidate.state = RuntimeSnapshotCandidate.State.ACTIVATED
                    candidate.activated_at = now
                    candidate.save(update_fields=["final_u_digest", "state", "activated_at"])
    if rejection is not None:
        raise RuntimeProjectionError(*rejection)
    return candidate, universe


@dataclass(frozen=True)
class AvailabilityClaim:
    outbox_id: uuid.UUID
    claim_token: str = field(repr=False)
    payload: dict[str, Any] = field(repr=False)

    def __repr__(self):
        return f"AvailabilityClaim(outbox_id={self.outbox_id!r}, claim_token=<redacted>)"


@transaction.atomic
def claim_availability_batch(worker_id: str, *, batch_size=100, now=None) -> list[AvailabilityClaim]:
    now = now or _database_now()
    eligible = RuntimeAvailabilityOutbox.objects.filter(
        Q(state=RuntimeAvailabilityOutbox.State.PENDING)
        | Q(
            state=RuntimeAvailabilityOutbox.State.CLAIMED,
            claim_lease_until__lte=now,
        )
    ).filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now)).order_by("created_at")
    if connection.features.has_select_for_update_skip_locked:
        eligible = eligible.select_for_update(skip_locked=True)
    else:
        eligible = eligible.select_for_update()
    claims = []
    for row in eligible[:batch_size]:
        # 24 random bytes = 192 bits; a new token is generated for every claim.
        claim_token = secrets.token_urlsafe(24)
        row.state = RuntimeAvailabilityOutbox.State.CLAIMED
        row.claim_owner = worker_id
        row.claim_token = claim_token
        row.claim_lease_until = now + timedelta(seconds=CLAIM_LEASE_SECONDS)
        row.attempt_count += 1
        row.save(update_fields=[
            "state", "claim_owner", "claim_token", "claim_lease_until", "attempt_count",
        ])
        claims.append(AvailabilityClaim(row.outbox_id, claim_token, dict(row.payload)))
    return claims


@transaction.atomic
def mark_availability_delivered(outbox_id, claim_token: str, receipt: dict[str, Any], *, now=None) -> bool:
    now = now or _database_now()
    updated = RuntimeAvailabilityOutbox.objects.filter(
        outbox_id=outbox_id,
        state=RuntimeAvailabilityOutbox.State.CLAIMED,
        claim_token=claim_token,
    ).update(
        state=RuntimeAvailabilityOutbox.State.DELIVERED,
        delivered_at=now,
        delivery_receipt=receipt,
        claim_owner="",
        claim_token="",
        claim_lease_until=None,
        last_error="",
        next_attempt_at=None,
    )
    return updated == 1


@transaction.atomic
def mark_availability_failed(outbox_id, claim_token: str, error: str, *, now=None) -> bool:
    now = now or _database_now()
    row = RuntimeAvailabilityOutbox.objects.select_for_update().filter(
        outbox_id=outbox_id,
        state=RuntimeAvailabilityOutbox.State.CLAIMED,
        claim_token=claim_token,
    ).first()
    if row is None:
        return False
    delay = RETRY_BACKOFF_SECONDS[min(max(row.attempt_count - 1, 0), len(RETRY_BACKOFF_SECONDS) - 1)]
    row.state = RuntimeAvailabilityOutbox.State.PENDING
    row.claim_owner = ""
    row.claim_token = ""
    row.claim_lease_until = None
    row.last_error = str(error)[:512]
    row.next_attempt_at = now + timedelta(seconds=delay)
    if row.attempt_count >= 10:
        row.failure_latched = True
        row.next_attempt_at = now + timedelta(seconds=60)
    row.save(update_fields=[
        "state", "claim_owner", "claim_token", "claim_lease_until", "last_error",
        "next_attempt_at", "failure_latched",
    ])
    return True


@transaction.atomic
def process_due_runtime_timers(worker_id: str, *, batch_size=100, now=None) -> int:
    """Claim due boundaries, recompute at DB-time, and complete idempotently."""

    now = now or _database_now()
    eligible = RuntimeBoundaryTimer.objects.filter(
        Q(state=RuntimeBoundaryTimer.State.PENDING)
        | Q(state=RuntimeBoundaryTimer.State.CLAIMED, claim_lease_until__lte=now)
    ).filter(due_at__lte=now).filter(
        Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now)
    ).order_by("due_at", "created_at")
    if connection.features.has_select_for_update_skip_locked:
        eligible = eligible.select_for_update(skip_locked=True)
    else:
        eligible = eligible.select_for_update()
    timers = list(eligible[:batch_size])
    processed = 0
    for timer in timers:
        timer.state = RuntimeBoundaryTimer.State.CLAIMED
        timer.claim_owner = worker_id
        timer.claim_lease_until = now + timedelta(seconds=CLAIM_LEASE_SECONDS)
        timer.attempt_count += 1
        timer.save(update_fields=["state", "claim_owner", "claim_lease_until", "attempt_count"])
        try:
            materialize_runtime_projection(timer.agent_id, now=now)
        except Exception as exc:
            delay = RETRY_BACKOFF_SECONDS[min(max(timer.attempt_count - 1, 0), len(RETRY_BACKOFF_SECONDS) - 1)]
            timer.state = RuntimeBoundaryTimer.State.PENDING
            timer.claim_owner = ""
            timer.claim_lease_until = None
            timer.last_error = str(exc)[:512]
            timer.next_attempt_at = now + timedelta(seconds=delay)
            if timer.attempt_count >= 10:
                timer.failure_latched = True
                timer.next_attempt_at = now + timedelta(seconds=60)
            timer.save(update_fields=[
                "state", "claim_owner", "claim_lease_until", "last_error",
                "next_attempt_at", "failure_latched",
            ])
            continue
        timer.state = RuntimeBoundaryTimer.State.COMPLETED
        timer.claim_owner = ""
        timer.claim_lease_until = None
        timer.completed_at = now
        timer.last_error = ""
        timer.save(update_fields=[
            "state", "claim_owner", "claim_lease_until", "completed_at", "last_error",
        ])
        processed += 1
    return processed


def migration_evidence_citation(migration: AgentRuntimeMigrationState) -> dict[str, Any]:
    """Return the immutable source citation required to release a legacy hold."""

    try:
        evidence_digest = canonical_digest(migration.evidence)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise RuntimeProjectionError("MIGRATION_EVIDENCE_INVALID") from exc
    return {
        "schema_version": 1,
        "source_row_version": migration.source_row_version,
        "source_row_hash": migration.source_row_hash,
        "source_timestamp": _iso(migration.source_timestamp),
        "hold_reason": migration.hold_reason,
        "evidence_digest": evidence_digest,
    }


def _validate_migration_evidence(
    migration: AgentRuntimeMigrationState,
    cited_evidence: Any,
) -> dict[str, Any]:
    if (
        not migration.source_row_version
        or not _is_sha256(migration.source_row_hash)
        or migration.source_timestamp is None
        or not migration.hold_reason
        or not isinstance(migration.evidence, dict)
        or not migration.evidence
    ):
        raise RuntimeProjectionError("MIGRATION_EVIDENCE_UNAVAILABLE")
    expected = migration_evidence_citation(migration)
    if not isinstance(cited_evidence, dict) or cited_evidence != expected:
        raise RuntimeProjectionError("MIGRATION_EVIDENCE_MISMATCH")
    return expected


def _migration_release_command(target_lifecycle: str) -> tuple[str, str, int]:
    if target_lifecycle == AgentRuntimeDesiredState.DesiredLifecycle.ACTIVE:
        return AgentRuntimeDesiredState.OperationKind.START, "start", 10
    if target_lifecycle == AgentRuntimeDesiredState.DesiredLifecycle.DELETED:
        return AgentRuntimeDesiredState.OperationKind.DELETE, "delete", 10
    return AgentRuntimeDesiredState.OperationKind.STOP, "stop", 5


@transaction.atomic
def resolve_migration_hold(
    agent_id,
    *,
    migration_generation: int,
    target_lifecycle: str,
    actor: str,
    reason: str,
    cited_evidence: dict[str, Any],
    operation_id,
    now=None,
) -> AgentRuntimeMigrationState:
    """The sole audited/idempotent exit from ``MIGRATION_HOLD``.

    Release, desired generation change, and the generation-bound lifecycle
    command are one transaction.  Runtime evidence can never release a hold by
    assertion alone: the caller must cite the immutable legacy source record.
    """

    if target_lifecycle not in AgentRuntimeDesiredState.DesiredLifecycle.values:
        raise RuntimeProjectionError("TARGET_LIFECYCLE_INVALID")
    actor = str(actor or "").strip()
    reason = str(reason or "").strip()
    if not actor:
        raise RuntimeProjectionError("MIGRATION_ACTOR_REQUIRED")
    if not reason:
        raise RuntimeProjectionError("MIGRATION_REASON_REQUIRED")
    operation_id = _closed_uuid(operation_id, "MIGRATION_OPERATION_ID_INVALID")
    migration_generation = _closed_positive_int(
        migration_generation, "MIGRATION_GENERATION_INVALID",
    )
    now = now or _database_now()
    migration = AgentRuntimeMigrationState.objects.select_for_update().get(agent_id=agent_id)
    desired = AgentRuntimeDesiredState.objects.select_for_update().get(agent_id=agent_id)
    binding = AgentDaemonBinding.objects.select_for_update().filter(agent_id=agent_id).first()
    citation = _validate_migration_evidence(migration, cited_evidence)
    if migration.migration_state == AgentRuntimeMigrationState.State.RELEASED:
        if (
            migration.migration_generation == migration_generation
            and migration.target_lifecycle == target_lifecycle
            and migration.release_operation_id == operation_id
        ):
            return migration
        raise RuntimeProjectionError("MIGRATION_ALREADY_RELEASED")
    if migration.migration_state != AgentRuntimeMigrationState.State.HELD:
        raise RuntimeProjectionError("MIGRATION_NOT_HELD")
    if migration.migration_generation != migration_generation:
        raise RuntimeProjectionError("MIGRATION_GENERATION_STALE")

    operation_kind, command_type, deadline_minutes = _migration_release_command(target_lifecycle)
    deadline = now + timedelta(minutes=deadline_minutes)
    desired.desired_lifecycle = target_lifecycle
    desired.previous_generation = desired.desired_generation
    desired.desired_generation += 1
    desired.operation_id = operation_id
    desired.operation_kind = operation_kind
    desired.operation_started_at = now
    desired.operation_deadline = deadline
    desired.fleet_control_revision += 1
    desired.save(update_fields=[
        "desired_lifecycle", "previous_generation", "desired_generation",
        "operation_id", "operation_kind", "operation_started_at", "operation_deadline",
        "fleet_control_revision", "updated_at",
    ])

    command_payload = {
        "schema_version": 1,
        "target_lifecycle": target_lifecycle,
        "migration_generation": migration_generation,
        "migration_evidence": citation,
    }
    AgentLifecycleCommand.objects.create(
        agent_id=agent_id,
        payload=command_payload,
        payload_hash=canonical_digest(command_payload),
        command_type=command_type,
        desired_generation=desired.desired_generation,
        owner_generation=binding.owner_generation if binding else 0,
        runtime_epoch=binding.active_runtime_epoch if binding else None,
        lifecycle_executor=desired.lifecycle_executor,
        executor_generation=desired.executor_generation,
        expected_resource_uid=desired.expected_deployment_uid,
        operation_id=operation_id,
        deadline=deadline,
        actor=actor,
        reason=reason,
    )

    migration.migration_state = AgentRuntimeMigrationState.State.RELEASED
    migration.released_at = now
    migration.released_by = actor
    migration.release_reason = reason
    migration.release_operation_id = operation_id
    migration.target_lifecycle = target_lifecycle
    migration.released_desired_generation = desired.desired_generation
    migration.save(update_fields=[
        "migration_state", "released_at", "released_by", "release_reason",
        "release_operation_id", "target_lifecycle", "released_desired_generation",
        "updated_at",
    ])
    return migration
