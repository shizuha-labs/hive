"""
HIVE-235: Hive↔Fleet-Daemon WebSocket bridge — server-side consumer.

The daemon dials OUT to `wss://hive…/v1/fleet/daemon-link`.  Hive is the
server.  Auth, resume cursor, snapshot sequencing, heartbeat health, and
backpressure are all handled here.

Frame protocol (JSON, all frames carry `type` + `seq`):
  daemon→Hive: register, state_snapshot, state_snapshot_complete,
               state_delta, identity_event, heartbeat, ack,
               health_snapshot, recovery_result, config_result
  Hive→daemon: register_ack, heartbeat, ack, request_snapshot, close_reason,
               recovery_action, config_update, delete_agent
"""

import asyncio
import hashlib
import logging
import time
from datetime import timedelta as _timedelta
import uuid

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.db import transaction
from django.utils.dateparse import parse_datetime

from .timeutils import naive_utc, naive_utc_now
from .pending_config import pending_config_or_empty
from .installation_config import effective_agent_env, effective_runtime_config, effective_config_frame

logger = logging.getLogger(__name__)

# Tuning constants
_REGISTER_TIMEOUT_S = 10         # close if no register frame within this window
_HEARTBEAT_INTERVAL_S = 10       # Hive→daemon heartbeat cadence
_MISS_DEGRADED = 3               # missed heartbeat intervals → degraded
_MISS_DOWN = 5                   # missed heartbeat intervals → down
_MAX_DELTA_BUFFER = 1000         # distinct child_ids buffered before flood-coalesce
_DELTA_SNAPSHOT_RETENTION = 200  # delta AgentHealthSnapshot rows kept per (agent, daemon) — HIVE-313
# Tolerated clock disagreement between a daemon/node and Hive before a reported
# activity timestamp is treated as bogus and clamped to now. Node clocks are
# NTP-synced, so this only has to absorb transit + rounding — anything larger is
# a convention bug, not skew, and must not be allowed to pin an agent "fresh"
# forever through the monotonic guard in _apply_agent_fields (fleet outage
# 2026-07-25).
_ACTIVITY_CLOCK_SKEW_GRACE = _timedelta(minutes=5)


def _lifecycle_status_from_observation(
    *,
    current_status: str,
    hibernation_reason: str,
    observed_status: str,
) -> str:
    """Preserve Hive hibernation intent over daemon process observations."""
    observed = str(observed_status)
    current = str(current_status)
    if observed == "stopped":
        # Operator 2026-09-01: only the operator may STOP. A daemon
        # process-down observation is hibernation (auto-wakeable), never
        # a kill switch. Preserve an explicit Hive STOPPED row.
        if current == "stopped":
            return "stopped"
        return "hibernated"
    if (
        current == "hibernated"
        and str(hibernation_reason or "")
        and observed == "stopped"
    ):
        return "hibernated"
    return observed

_V2_BARRIER_ENVELOPE_FIELDS = frozenset({
    "type",
    "frame_type",
    "protocol_version",
    "daemon_id",
    "runtime_epoch",
    "frame_seq",
    "sent_at",
    "payload_digest",
    "owner_generation",
    "executor_generation",
    "candidate_deadline",
    "data",
})


def _validate_v2_barrier_envelope(frame: dict, expected_daemon_id: str) -> tuple[int, uuid.UUID]:
    """Validate transport integrity before touching the durable frame cursor."""

    from .runtime_projection_v2 import canonical_digest

    if not isinstance(frame, dict):
        raise ValueError("FRAME_ENVELOPE_INVALID")
    missing = sorted(_V2_BARRIER_ENVELOPE_FIELDS - set(frame))
    unknown = sorted(set(frame) - _V2_BARRIER_ENVELOPE_FIELDS)
    if missing:
        raise ValueError(f"FRAME_ENVELOPE_FIELDS_MISSING:{','.join(missing)}")
    if unknown:
        raise ValueError(f"FRAME_ENVELOPE_FIELDS_UNKNOWN:{','.join(unknown)}")
    if frame["type"] != "runtime_barrier_complete_v2" or frame["frame_type"] != frame["type"]:
        raise ValueError("FRAME_TYPE_INVALID")
    if type(frame["protocol_version"]) is not int or frame["protocol_version"] != 2:
        raise ValueError("PROTOCOL_VERSION_INVALID")
    if frame["daemon_id"] != expected_daemon_id:
        raise ValueError("DAEMON_ID_MISMATCH")
    if type(frame["frame_seq"]) is not int or frame["frame_seq"] <= 0:
        raise ValueError("FRAME_SEQ_INVALID")
    try:
        runtime_epoch = uuid.UUID(str(frame["runtime_epoch"]))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("RUNTIME_EPOCH_INVALID") from exc
    sent_at = parse_datetime(frame["sent_at"]) if isinstance(frame["sent_at"], str) else None
    if sent_at is None:
        raise ValueError("SENT_AT_INVALID")
    if not isinstance(frame["data"], dict):
        raise ValueError("PAYLOAD_INVALID")
    try:
        actual_digest = canonical_digest(frame["data"])
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("PAYLOAD_CANONICALIZATION_FAILED") from exc
    if frame["payload_digest"] != actual_digest:
        raise ValueError("PAYLOAD_DIGEST_MISMATCH")
    return frame["frame_seq"], runtime_epoch


def _pending_key_confirmed(key, pending_value, normalized, mcp_config):
    """HIVE-597: does a config_applied delta confirm this pending update key?

    Confirmed when the daemon-reported value equals the pending desired value
    (JSON-stable comparison), or when the daemon does not echo the key at all
    (extras like work_schedule ride mcp_config opaquely — the config_applied
    ack itself is the only confirmation available for those).
    """
    import json as _json

    def stable(value):
        try:
            return _json.dumps(value, sort_keys=True, default=str)
        except Exception:
            return repr(value)

    skill_config = normalized.get('skill_config') or {}
    reported = {
        'model': normalized.get('model'),
        'model_fallbacks': normalized.get('model_fallbacks'),
        'model_overrides': normalized.get('model_overrides'),
        'role': normalized.get('role'),
        'context_prompt': normalized.get('context_prompt'),
        'personality_traits': normalized.get('personality_traits'),
        'env': normalized.get('env'),
        'resource_limits': normalized.get('resource_overrides'),
        'reasoning_effort': normalized.get('reasoning_effort'),
        'failover_chain_id': normalized.get('failover_chain_id'),
        'execution_method': (mcp_config or {}).get('execution_method'),
        'runtime_environment': (mcp_config or {}).get('runtime_environment'),
        'enabled_mcp_server_ids': (mcp_config or {}).get('enabled_mcp_server_ids'),
        'skills': skill_config.get('skills'),
        'eager_skills': skill_config.get('eager_skills'),
    }
    if key not in reported or reported.get(key) is None:
        return True
    if key == 'env':
        pending_value = effective_agent_env(pending_value)
    return stable(reported[key]) == stable(pending_value)


class DaemonLinkConsumer(AsyncJsonWebsocketConsumer):
    """
    WebSocket consumer for the daemon→Hive link (HIVE-235).

    Stickiness guarantee: `last_acked_seq` is persisted to DaemonRegistry on
    every ack, so any Hive replica can return the correct `resume_from_seq` on
    reconnect (shared-cursor model, per §hard-constraints).  A "supersede"
    channel-layer message forces any prior connection for the same daemon_id to
    close, so at most one live WS exists per daemon_id at any moment.
    """

    # ------------------------------------------------------------------ init

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._daemon_id: str | None = None
        self._registry_pk: int | None = None
        self._owner_subject: int | None = None  # PLAT-4235 P1: per-user daemon scope
        self._selected_protocol_version: int = 1
        self._group_name: str | None = None

        # Resume cursor loaded from DB on register
        self._resume_seq: int = 0
        # Outbound seq counter for Hive-originated frames
        self._out_seq: int = 0

        # Snapshot state machine
        self._snapshot_complete: bool = False
        self._snapshot_child_ids: set[str] = set()
        self._unknown_snapshot_children: dict[str, dict] = {}
        # child_id → latest state_delta frame (buffered until snapshot_complete)
        self._delta_buffer: dict[str, dict] = {}

        # Heartbeat tracking
        self._last_daemon_hb: float = time.monotonic()
        self._heartbeat_task: asyncio.Task | None = None
        self._register_timeout_task: asyncio.Task | None = None
        self._closed: bool = False
        # Stored on successful register to discriminate disconnect() from a superseded
        # connection vs. the live one (HIVE-235 P1 race fix — see _db_set_status).
        self._channel_name_at_register: str = ""

    # ---------------------------------------------------------------- lifecycle

    async def connect(self):
        await self.accept()
        self._register_timeout_task = asyncio.ensure_future(
            self._register_timeout()
        )

    async def disconnect(self, close_code):
        self._closed = True
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._register_timeout_task:
            self._register_timeout_task.cancel()
        if self._registry_pk is not None:
            active_connection = await self._db_set_status(self.Status.DISCONNECTED)
            if active_connection:
                await self._db_fence_mcp_projection_leases()
        if self._group_name:
            await self.channel_layer.group_discard(
                self._group_name, self.channel_name
            )

    async def receive_json(self, content, **kwargs):
        frame_type = content.get("type")
        if not frame_type:
            await self._close_with_reason("bad_frame", "Missing 'type' field")
            return

        # Heartbeats are not the only proof that the authenticated peer is
        # alive.  A reconnect can legitimately spend longer than the health
        # threshold streaming config_result and snapshot frames before its
        # next explicit heartbeat.  Counting only heartbeat frames made that
        # healthy, busy socket flap CONNECTED -> DEGRADED -> DOWN.  Fence the
        # activity update to the registering channel so a superseded socket
        # still cannot revive the durable registry row.
        if self._registry_pk is not None:
            if not await self._db_touch_last_seen():
                return
            self._last_daemon_hb = time.monotonic()

        handlers = {
            "register": self._handle_register,
            "heartbeat": self._handle_heartbeat,
            "ack": self._handle_ack,
            "state_snapshot": self._handle_state_snapshot,
            "state_snapshot_complete": self._handle_snapshot_complete,
            "state_delta": self._handle_state_delta,
            "identity_event": self._handle_identity_event,
            "health_snapshot": self._handle_health_snapshot,
            "recovery_result": self._handle_recovery_result,
            "config_result": self._handle_config_result,
            "runtime_barrier_complete_v2": self._handle_runtime_barrier_complete_v2,
        }
        handler = handlers.get(frame_type)
        if handler:
            await handler(content)
        else:
            logger.warning(
                "DaemonLink: unknown frame type %r from daemon %s",
                frame_type, self._daemon_id,
            )

    # ------------------------------------------------------------------ auth

    async def _handle_register(self, frame: dict):
        if self._daemon_id is not None:
            return  # ignore duplicate register

        daemon_id = frame.get("daemon_id", "").strip()
        fleet_id = frame.get("fleet_id", "").strip()
        runtime = frame.get("runtime", "docker").strip()
        token = frame.get("token", "")
        # PLAT-4235 P1: per-user daemon ownership. A per-user daemon declares the
        # Shizuha-ID user subject it serves; omitted/blank = the shared org/team
        # fleet daemon (owner_subject stays NULL, manages owner_id=NULL agents).
        owner_subject = frame.get("owner_subject")
        try:
            owner_subject = int(owner_subject) if owner_subject not in (None, "", "null") else None
        except (TypeError, ValueError):
            owner_subject = None
        owner_username = str(frame.get("owner_username") or "").strip()

        # HIVE-277 §10 v16 consumer-first negotiation. The tolerant v2 consumer
        # may be deployed dark, but a daemon must not emit/execute v2 until Hive
        # explicitly selects it. Enforcement is a separate default-off gate.
        from django.conf import settings
        advertised_versions = frame.get("protocol_versions")
        if not isinstance(advertised_versions, list):
            advertised_versions = [1]
        try:
            advertised_versions = {int(version) for version in advertised_versions}
        except (TypeError, ValueError):
            advertised_versions = {1}
        dark_v2 = bool(getattr(settings, "HIVE_RUNTIME_PROJECTION_V2_DARK", True))
        # Protocol readiness and authority enforcement are deliberately
        # separate.  DARK selects and ingests v2 so a real shadow soak can run;
        # ENFORCE remains the independent default-off read/routing/action gate.
        selected_protocol_version = 2 if dark_v2 and 2 in advertised_versions else 1
        active_runtime_epoch = None
        if selected_protocol_version == 2:
            try:
                active_runtime_epoch = uuid.UUID(str(frame.get("runtime_epoch") or ""))
            except (TypeError, ValueError, AttributeError):
                await self._close_with_reason(
                    "runtime_epoch_required",
                    "protocol v2 requires a durable runtime_epoch UUID",
                )
                return
        capabilities = frame.get("protocol_capabilities")
        if not isinstance(capabilities, dict):
            capabilities = {}

        if not daemon_id or not token:
            await self._close_with_reason(
                "missing_fields", "daemon_id and token are required in register"
            )
            return

        valid, cred_ref = await self._validate_credential(daemon_id, token)
        if not valid:
            logger.warning("DaemonLink: auth failed for daemon_id=%s", daemon_id)
            await self._close_with_reason("auth_failed", "Invalid or revoked credential")
            return

        # SEC-§17 / ADR-0081 Decision #4 (PLAT-7284): owner_subject is derived
        # server-side from the authenticated daemon_id (rt-fleet-u<user_id>), never
        # trusted from the client frame. A client-declared owner_subject that
        # mismatches the derived value is refused (fail-closed) — a compromised
        # per-user daemon cannot bind another user's agents (INV-OWNER-SCOPE
        # bypass). The derived value is authoritative even when the client omits
        # owner_subject.
        derived_owner_subject = self._derive_owner_subject_from_daemon_id(daemon_id)
        if owner_subject is not None and owner_subject != derived_owner_subject:
            logger.warning(
                "DaemonLink: owner-scope mismatch — daemon %s declared "
                "owner_subject=%s, derived %s from daemon_id; refusing register "
                "(INV-OWNER-SCOPE)",
                daemon_id, owner_subject, derived_owner_subject,
            )
            await self._close_with_reason(
                "owner_scope_mismatch",
                "owner_subject does not match the authenticated daemon scope",
            )
            return
        owner_subject = derived_owner_subject

        self._daemon_id = daemon_id
        self._group_name = f"daemon-link-{daemon_id}"

        # Join group and supersede any prior connection for this daemon_id.
        await self.channel_layer.group_add(self._group_name, self.channel_name)
        await self.channel_layer.group_send(
            self._group_name,
            {"type": "daemon.supersede", "new_channel": self.channel_name},
        )

        self._owner_subject = owner_subject
        registry = await self._db_upsert_registry(
            daemon_id, fleet_id, runtime, cred_ref, self.channel_name,
            harness_report=frame.get("harness_report") if isinstance(frame.get("harness_report"), dict) else None,
            owner_subject=owner_subject, owner_username=owner_username,
            selected_protocol_version=selected_protocol_version,
            active_runtime_epoch=active_runtime_epoch,
            protocol_capabilities=capabilities,
        )
        self._registry_pk = registry["pk"]
        self._resume_seq = registry["last_acked_seq"]
        # Protocol compatibility: first-time daemons still receive
        # seed_required=True so they immediately emit a complete snapshot. The
        # snapshot is observation-only; Hive never creates desired rows from it.
        self._selected_protocol_version = selected_protocol_version
        seed_required = not bool(registry.get("config_seeded"))
        # Record our channel_name so disconnect() can guard against clobbering
        # a superseding connection's CONNECTED status (P1 race fix).
        self._channel_name_at_register = self.channel_name

        # The authenticated, in-order Hive↔daemon link is the only writer of
        # the bounded MCP authority lease. A projection HTTP request may read
        # this witness, but can never renew it itself.
        await self._db_renew_mcp_projection_leases()

        if self._register_timeout_task:
            self._register_timeout_task.cancel()

        ack: dict = {
            "type": "register_ack",
            "seq": self._next_out_seq(),
            "seed_required": seed_required,
            "supported_protocol_versions": [1, 2] if dark_v2 else [1],
            "selected_protocol_version": selected_protocol_version,
        }
        if self._resume_seq > 0:
            ack["resume_from_seq"] = self._resume_seq
        await self.send_json(ack)
        for desired_frame in await self._db_current_desired_config_frames():
            desired_frame["seq"] = self._next_out_seq()
            await self.send_json(desired_frame)
        # A first-time daemon emits its snapshot in response to seed_required.
        # Reconnecting daemons need an explicit fresh observation request.
        if not seed_required:
            await self.send_json({
                "type": "request_snapshot",
                "seq": self._next_out_seq(),
                "seed_required": False,
                "reason": "runtime_observation_sync",
            })

        self._heartbeat_task = asyncio.ensure_future(self._heartbeat_loop())
        logger.info(
            "DaemonLink: daemon %s registered (fleet=%s runtime=%s resume_seq=%d)",
            daemon_id, fleet_id, runtime, self._resume_seq,
        )

    @staticmethod
    @database_sync_to_async
    def _validate_credential(daemon_id: str, token: str):
        from .models import DaemonCredential
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        try:
            DaemonCredential.objects.get(
                daemon_id=daemon_id, token_hash=token_hash, revoked=False
            )
            return True, token_hash[:16]
        except DaemonCredential.DoesNotExist:
            return False, ""

    @staticmethod
    def _derive_owner_subject_from_daemon_id(daemon_id: str):
        """Derive the owner_subject a daemon is authorized to serve.

        SEC-§17 / ADR-0081 Decision #4 (PLAT-7284): per-user daemons are minted
        with ``daemon_id = rt-fleet-u<user_id>`` (see provision_user_daemon.py),
        so the owner subject is recoverable from the authenticated daemon_id and
        must NEVER be trusted from the client frame. Returns the int user_id for
        a per-user daemon, or None for the shared org/team daemon (e.g.
        ``rt-fleet-s1``) which manages owner_id=NULL agents.
        """
        prefix = "rt-fleet-u"
        if not daemon_id or not daemon_id.startswith(prefix):
            return None
        suffix = daemon_id[len(prefix):]
        if not suffix.isdigit():
            return None
        return int(suffix)

    @staticmethod
    @database_sync_to_async
    def _db_upsert_registry(daemon_id, fleet_id, runtime, cred_ref, channel_name: str,
                            harness_report=None, owner_subject=None, owner_username="",
                            selected_protocol_version=1, active_runtime_epoch=None,
                            protocol_capabilities=None) -> dict:
        from .models import DaemonRegistry
        from django.utils import timezone as tz
        registry, _ = DaemonRegistry.objects.get_or_create(
            daemon_id=daemon_id,
            defaults={"fleet_id": fleet_id, "runtime": runtime, "cred_ref": cred_ref},
        )
        registry.fleet_id = fleet_id
        registry.runtime = runtime
        registry.cred_ref = cred_ref
        registry.status = DaemonRegistry.Status.CONNECTED
        registry.last_seen = tz.now()
        registry.snapshot_complete = False
        # Store active channel_name so disconnect() can guard (P1 race fix).
        registry.ws_channel = channel_name
        # PLAT-4235 P1: per-user daemon ownership. Trust the registered owner on
        # (re)connect so a daemon promoted to per-user scope updates in place.
        registry.owner_subject = owner_subject
        registry.owner_username = owner_username or ""
        prior_epoch = registry.active_runtime_epoch
        registry.selected_protocol_version = selected_protocol_version
        registry.active_runtime_epoch = active_runtime_epoch
        registry.protocol_capabilities = protocol_capabilities or {}
        if selected_protocol_version == 2 and prior_epoch != active_runtime_epoch:
            # A new epoch owns a fresh, gapless committed-cursor domain. Never
            # reuse the v1/process sequence under a new epoch.
            registry.last_committed_frame_seq = 0
        update_fields = [
            "fleet_id", "runtime", "cred_ref",
            "status", "last_seen", "snapshot_complete", "ws_channel",
            "owner_subject", "owner_username",
            "selected_protocol_version", "active_runtime_epoch",
            "protocol_capabilities", "last_committed_frame_seq",
        ]
        # HIVE-600: record the daemon's observed runtime projection. This is
        # evidence only; reviewed desired state is projected separately from
        # ``desired_runtime_release`` and Hive never rewrites the observation.
        if harness_report:
            img = str(harness_report.get("agent_runtime_image") or "").strip()
            if img:
                registry.agent_runtime_image = img[:512]
                update_fields.append("agent_runtime_image")
            registry.harness_report = harness_report
            update_fields.append("harness_report")
        registry.save(update_fields=update_fields)
        return {
            "pk": registry.pk,
            "last_acked_seq": (
                registry.last_committed_frame_seq
                if selected_protocol_version == 2
                else registry.last_acked_seq
            ),
            "config_seeded": registry.config_seeded_at is not None,
        }

    @database_sync_to_async
    def _db_renew_mcp_projection_leases(self):
        from .mcp_projection import renew_projection_authority_leases
        from .models import FleetAgent
        if self._registry_pk is None:
            return 0
        agent_ids = list(FleetAgent.objects.filter(
            managing_daemon_id=self._registry_pk,
        ).values_list('id', flat=True))
        return renew_projection_authority_leases(agent_ids)

    @database_sync_to_async
    def _db_fence_mcp_projection_leases(self):
        from .mcp_projection import fence_projection_authority_leases
        from .models import FleetAgent
        if self._registry_pk is None:
            return 0
        agent_ids = list(FleetAgent.objects.filter(
            managing_daemon_id=self._registry_pk,
        ).values_list('id', flat=True))
        return fence_projection_authority_leases(agent_ids)

    # ------------------------------------------------------------------ heartbeat

    async def _heartbeat_loop(self):
        try:
            while not self._closed:
                await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
                if self._closed:
                    break
                if not await self._heartbeat_tick():
                    return

        except asyncio.CancelledError:
            pass

    async def _heartbeat_tick(self):
        """Send one heartbeat and derive health for the current channel."""
        # A newer socket may already own the registry row even though this
        # consumer has not processed its supersede event yet.  Check the
        # durable ownership fence before sending or deriving health from this
        # socket. A stale loop must become inert: no heartbeat, no status write,
        # and no misleading DOWN log.
        if not await self._db_is_current_channel():
            return False
        await self.send_json({"type": "heartbeat", "seq": self._next_out_seq()})
        elapsed = time.monotonic() - self._last_daemon_hb
        missed = int(elapsed / _HEARTBEAT_INTERVAL_S)
        if missed >= _MISS_DOWN:
            if await self._db_set_status(self.Status.DOWN):
                await self._db_fence_mcp_projection_leases()
                logger.warning(
                    "DaemonLink: daemon %s DOWN (%d missed heartbeats)",
                    self._daemon_id, missed,
                )
        elif missed >= _MISS_DEGRADED:
            if await self._db_set_status(self.Status.DEGRADED):
                logger.warning(
                    "DaemonLink: daemon %s DEGRADED (%d missed heartbeats)",
                    self._daemon_id, missed,
                )
        return True

    async def _handle_heartbeat(self, frame: dict):
        # The registry row is the authority boundary.  Supersession can reach
        # the database before its channel-layer event reaches the old consumer,
        # so a late frame from that socket must not revive or otherwise mutate
        # the live connection's state.
        if not await self._db_touch_last_seen():
            return
        self._last_daemon_hb = time.monotonic()
        await self._db_renew_mcp_projection_leases()
        harness_report = frame.get("harness_report")
        if isinstance(harness_report, dict):
            if not await self._db_update_harness_report(harness_report):
                return
        if not await self._db_is_current_channel():
            return
        # Ack the daemon's heartbeat frame
        await self.send_json({"type": "ack", "seq": frame.get("seq", 0)})
        # A channel-layer group_send can fail while this socket stays healthy.
        # Retry the durable lane outbox on every live daemon heartbeat instead
        # of waiting indefinitely for an unrelated reconnect.
        for pending in await self._db_retryable_runtime_lane_frames():
            pending["seq"] = self._next_out_seq()
            await self.send_json(pending)
        # pending_config is the durable outbox for non-lane configuration. A
        # live group_send can race channel registration/supersession, so retry
        # its exact unacknowledged values directly on every daemon heartbeat.
        for pending in await self._db_pending_config_frames():
            pending["seq"] = self._next_out_seq()
            await self.send_json(pending)

    # ------------------------------------------------------------------ ack / cursor

    async def _handle_ack(self, frame: dict):
        seq = frame.get("seq")
        if seq is not None and self._registry_pk is not None:
            await self._db_advance_cursor(int(seq))

    # ------------------------------------------------------------------ snapshot / delta

    async def _handle_state_snapshot(self, frame: dict):
        child_id = str(
            frame.get("child_id")
            or (frame.get("data") or {}).get("child_id")
            or (frame.get("data") or {}).get("agent_id")
            or (frame.get("data") or {}).get("id")
            or ""
        )
        self._snapshot_child_ids.add(child_id)
        result = await self._persist_agent_state(child_id, frame, "snapshot")
        if result and result.get("unknown"):
            self._unknown_snapshot_children[result["runtime_id"]] = result
        logger.debug(
            "DaemonLink: state_snapshot seq=%s child_id=%s daemon=%s",
            frame.get("seq"), child_id, self._daemon_id,
        )

    async def _handle_snapshot_complete(self, frame: dict):
        seq = int(frame.get("seq", 0))
        child_count = int(frame.get("child_count", 0))
        observed_count = len(self._snapshot_child_ids)
        if observed_count != child_count:
            logger.warning(
                "DaemonLink: incomplete snapshot daemon=%s declared=%d observed=%d; "
                "refusing unknown-runtime cleanup and requesting a fresh snapshot",
                self._daemon_id, child_count, observed_count,
            )
            self._snapshot_complete = False
            self._snapshot_child_ids.clear()
            self._unknown_snapshot_children.clear()
            await self._db_set_snapshot_complete(False)
            await self.send_json({
                "type": "request_snapshot",
                "seq": self._next_out_seq(),
                "seed_required": False,
                "reason": "snapshot_count_mismatch",
            })
            await self.send_json({"type": "ack", "seq": seq})
            await self._db_advance_cursor(seq)
            return
        self._snapshot_complete = True
        await self._db_set_snapshot_complete(True)
        await self._db_mark_config_seeded()
        logger.info(
            "DaemonLink: snapshot complete seq=%d child_count=%d daemon=%s",
            seq, child_count, self._daemon_id,
        )
        # Flush buffered deltas now that the snapshot base is established.
        for child_id, delta_frame in list(self._delta_buffer.items()):
            await self._apply_delta(child_id, delta_frame)
        self._delta_buffer.clear()
        for unknown in self._unknown_snapshot_children.values():
            await self._send_unknown_runtime_delete(unknown, "hive_unknown_after_complete_snapshot")
        self._snapshot_child_ids.clear()
        self._unknown_snapshot_children.clear()
        # Ack the snapshot_complete frame to advance the cursor.
        await self.send_json({"type": "ack", "seq": seq})
        await self._db_advance_cursor(seq)

    async def _handle_state_delta(self, frame: dict):
        child_id = str(frame.get("child_id", ""))
        seq = int(frame.get("seq", 0))

        if not self._snapshot_complete:
            # Buffer: always keep the latest delta per child_id (coalesces naturally).
            self._delta_buffer[child_id] = frame
            if len(self._delta_buffer) > _MAX_DELTA_BUFFER:
                # Flood: discard buffer and request a fresh snapshot.
                self._delta_buffer.clear()
                self._snapshot_complete = False
                await self._db_set_snapshot_complete(False)
                await self.send_json({
                    "type": "request_snapshot",
                    "seq": self._next_out_seq(),
                    "reason": "delta_queue_overflow",
                })
            return

        await self._apply_delta(child_id, frame)
        await self.send_json({"type": "ack", "seq": seq})
        await self._db_advance_cursor(seq)

    async def _apply_delta(self, child_id: str, frame: dict):
        result = await self._persist_agent_state(child_id, frame, "delta")
        if result and result.get("unknown"):
            await self._send_unknown_runtime_delete(result, "hive_unknown_runtime_delta")
        logger.debug(
            "DaemonLink: state_delta child_id=%s seq=%s daemon=%s",
            child_id, frame.get("seq"), self._daemon_id,
        )

    async def _handle_runtime_barrier_complete_v2(self, frame: dict):
        """Commit one v2 R∪L barrier before ACKing its global frame cursor."""

        try:
            seq, _runtime_epoch = _validate_v2_barrier_envelope(frame, self._daemon_id or "")
        except ValueError as exc:
            # Transport-corrupt frames are not deterministic semantic events:
            # never advance their cursor, and require a clean reconnect/replay.
            code = str(exc).split(":", 1)[0].lower()
            await self._close_with_reason(code, str(exc))
            return
        if self._selected_protocol_version != 2:
            await self._close_with_reason(
                "protocol_not_selected",
                "runtime_barrier_complete_v2 received before protocol v2 selection",
            )
            return
        outcome = await self._db_commit_runtime_barrier_v2(frame)
        if outcome["status"] == "gap":
            await self.send_json({
                "type": "nack",
                "expected_frame_seq": outcome["expected"],
                "runtime_epoch": outcome.get("runtime_epoch"),
            })
            return
        if outcome["status"] == "epoch_mismatch":
            await self._close_with_reason("runtime_epoch_mismatch", outcome["code"])
            return
        ack = {"type": "ack", "seq": seq, "frame_seq": seq, "protocol_version": 2}
        if outcome["status"] == "rejected":
            ack["rejected"] = outcome["code"]
        await self.send_json(ack)

    @database_sync_to_async
    def _db_commit_runtime_barrier_v2(self, frame: dict):
        from datetime import timezone as datetime_timezone
        from django.db import transaction
        from django.utils import timezone as tz
        from django.utils.dateparse import parse_datetime

        from .models import DaemonRegistry
        from .runtime_projection_v2 import (
            RuntimeProjectionError,
            activate_runtime_snapshot_candidate,
            stage_runtime_snapshot_candidate,
        )

        seq = frame["frame_seq"]
        runtime_epoch_raw = str(frame.get("runtime_epoch") or "")
        try:
            runtime_epoch = uuid.UUID(runtime_epoch_raw)
        except (TypeError, ValueError, AttributeError):
            return {"status": "epoch_mismatch", "code": "runtime_epoch invalid"}
        with transaction.atomic():
            registry = DaemonRegistry.objects.select_for_update().get(pk=self._registry_pk)
            if registry.selected_protocol_version != 2 or registry.active_runtime_epoch != runtime_epoch:
                return {"status": "epoch_mismatch", "code": "foreign or inactive runtime_epoch"}
            expected = registry.last_committed_frame_seq + 1
            if seq <= registry.last_committed_frame_seq:
                return {"status": "duplicate", "expected": expected, "runtime_epoch": runtime_epoch_raw}
            if seq != expected:
                return {"status": "gap", "expected": expected, "runtime_epoch": runtime_epoch_raw}
            try:
                if isinstance(frame.get("owner_generation"), bool) or isinstance(
                    frame.get("executor_generation"), bool
                ):
                    raise ValueError
                owner_generation = int(frame.get("owner_generation"))
                executor_generation = int(frame.get("executor_generation"))
                if (
                    owner_generation < 0
                    or executor_generation < 0
                    or str(frame.get("owner_generation")).strip() != str(owner_generation)
                    or str(frame.get("executor_generation")).strip() != str(executor_generation)
                ):
                    raise ValueError
            except (TypeError, ValueError):
                registry.last_committed_frame_seq = seq
                registry.save(update_fields=["last_committed_frame_seq"])
                return {"status": "rejected", "code": "GENERATION_INVALID"}
            deadline = parse_datetime(str(frame.get("candidate_deadline") or ""))
            if deadline is None:
                registry.last_committed_frame_seq = seq
                registry.save(update_fields=["last_committed_frame_seq"])
                return {"status": "rejected", "code": "CANDIDATE_DEADLINE_INVALID"}
            if tz.is_naive(deadline) and tz.is_aware(tz.now()):
                deadline = tz.make_aware(deadline, datetime_timezone.utc)
            payload = frame["data"]
            try:
                raw_barrier_seq = payload.get("barrier_end_frame_seq")
                if (
                    isinstance(raw_barrier_seq, bool)
                    or int(raw_barrier_seq) != seq
                    or str(raw_barrier_seq).strip() != str(seq)
                ):
                    raise RuntimeProjectionError("BARRIER_FRAME_SEQ_MISMATCH")
                candidate = stage_runtime_snapshot_candidate(
                    daemon=registry,
                    owner_generation=owner_generation,
                    executor_generation=executor_generation,
                    runtime_epoch=runtime_epoch,
                    barrier_payload=payload,
                    candidate_deadline=deadline,
                )
                activate_runtime_snapshot_candidate(candidate.snapshot_id, now=tz.now())
                status = "committed"
                code = ""
            except RuntimeProjectionError as exc:
                # Deterministically rejected semantic frames advance the cursor
                # in the same transaction so exact replay cannot wedge forever.
                status = "rejected"
                code = exc.code
            except (TypeError, ValueError, OverflowError):
                # Closed semantic inputs must never escape the protocol path as
                # consumer/ORM exceptions.  They consume their exact cursor and
                # leave a stable fail-closed rejection instead.
                status = "rejected"
                code = "PAYLOAD_SEMANTIC_INVALID"
            registry.last_committed_frame_seq = seq
            registry.save(update_fields=["last_committed_frame_seq"])
            return {"status": status, "code": code, "runtime_epoch": runtime_epoch_raw}

    @database_sync_to_async
    @transaction.atomic
    def _persist_agent_state(self, child_id: str, frame: dict, source: str):
        """Persist observations only for Hive-owned FleetAgent rows."""
        from django.conf import settings
        from django.core.exceptions import ValidationError
        from datetime import timezone as datetime_timezone
        from django.utils import timezone as tz
        from django.utils.dateparse import parse_datetime

        from .models import DaemonRegistry, FleetAgent
        from .runtime_fleet import normalize_runtime_agent
        from . import agent_index_cache

        if self._registry_pk is None:
            logger.warning("DaemonLink: %s before register daemon=%s — dropping", source, self._daemon_id)
            return
        try:
            daemon = DaemonRegistry.objects.get(pk=self._registry_pk)
        except DaemonRegistry.DoesNotExist:
            logger.warning("DaemonLink: registry pk %s not found for %s", self._registry_pk, source)
            return

        data = frame.get("data") if isinstance(frame.get("data"), dict) else {}
        config = data.get("config") if isinstance(data.get("config"), dict) else {}

        def pick(*keys, default=None):
            for container in (data, config, frame):
                if not isinstance(container, dict):
                    continue
                for key in keys:
                    if key in container and container.get(key) is not None:
                        return container.get(key)
            return default

        runtime_id = str(pick("id", "agent_id", "child_id", default=child_id) or child_id or "").strip()
        # PLAT-7523: pre-advance activity clock, captured when this frame
        # advances last_active_at (metering enqueue happens after save, in
        # this same transaction).
        prev_last_active_for_metering = None
        username = str(pick("agent_username", "username", default="") or "").strip().lower()
        if not username and runtime_id:
            username = (
                FleetAgent.objects.filter(agent_id=runtime_id)
                .values_list("agent_username", flat=True)
                .first()
                or ""
            )
        if not username:
            # As a fallback, accept a DNS-shaped child id as the username.  UUIDs
            # and blank values are not valid FleetAgent usernames and are dropped.
            candidate = str(runtime_id or child_id or "").strip().lower()
            username = candidate if candidate and len(candidate) <= 47 and candidate.replace('-', '').isalnum() else ""
        if not username:
            logger.warning("DaemonLink: %s missing username child_id=%s daemon=%s", source, child_id, self._daemon_id)
            return

        deleted = bool(pick("deleted", "tombstone", default=False))
        status = str(pick("status", default="") or "").lower()
        if deleted:
            status = FleetAgent.Status.DELETED

        skills = pick("skills", default=[])
        eager_skills = pick("eagerSkills", "eager_skills", default=[])
        enabled_mcp = pick("enabledMcpServerIds", "enabled_mcp_server_ids", default=[])
        mcp_servers = pick("mcpServers", "mcp_servers", default=[])
        if not mcp_servers and enabled_mcp:
            mcp_servers = [{"slug": slug, "name": slug} for slug in enabled_mcp if slug]

        raw_agent = {
            "id": runtime_id,
            "platformUserId": runtime_id,
            "username": username,
            "email": pick("email", default="") or (f"{username}@shizuha.com" if username else ""),
            "name": pick("display_name", "name", default=username),
            "role": pick("role", default="Agent"),
            "status": status or "running",
            "enabled": bool(pick("enabled", "desired_enabled", default=status not in {"disabled", "stopped", "deleted"})),
            "lifecycleState": pick(
                "lifecycleState", "lifecycle_state", default="",
            ),
            "error": pick("status_message", "error", default=""),
            "executionMethod": pick("executionMethod", "execution_method", default="shizuha"),
            # Hive fleet placement is k3s-only (never bare_metal host spawn).
            "runtimeEnvironment": "k8s",
            "model": pick("model", default=""),
            "modelFallbacks": pick("modelFallbacks", "model_fallbacks", default=[]),
            "modelOverrides": pick("modelOverrides", "model_overrides", default={}),
            "reasoningEffort": pick("reasoningEffort", "reasoning_effort", default=""),
            "resourceLimits": pick("resourceLimits", "resource_limits", default={}),
            "skills": skills or [],
            "eagerSkills": eager_skills or [],
            "mcpServers": mcp_servers or [],
            "mcpConfig": pick("mcpConfig", "mcp_config", default={}) or {},
            "env": pick("env", default={}) or {},
            "contextPrompt": pick("contextPrompt", "context_prompt", default=""),
            "personalityTraits": pick("personalityTraits", "personality_traits", default={}),
            "failoverChainId": pick("failoverChainId", "failover_chain_id", default=""),
            "maxConcurrentTasks": pick("maxConcurrentTasks", "max_concurrent_tasks", default=None),
            "allowParallelExecution": pick("allowParallelExecution", "allow_parallel_execution", default=False),
            "warmPoolSize": pick("warmPoolSize", "warm_pool_size", default=0),
            "tier": pick("tier", default="normal"),
        }
        normalized = normalize_runtime_agent(raw_agent)
        mcp_config = dict(normalized.get("mcp_config") or {})
        mcp_config["execution_method"] = normalized.get("execution_method") or "shizuha"
        from .runtime_placement import force_mcp_config_k8s
        mcp_config = force_mcp_config_k8s(mcp_config)
        mcp_config["enabled_mcp_server_ids"] = normalized.get("enabled_mcp_server_ids") or []
        mcp_config["mcp_servers"] = normalized.get("mcp_servers") or []

        # Serialize the final full-row save against top-level Hive edits and
        # config_result confirmation. Otherwise an observation loaded before
        # push B could save afterward and clobber B with its stale ORM fields.
        agent = (
            FleetAgent.objects
            .select_for_update(of=('self',))
            .filter(agent_username=username)
            .first()
        )
        if agent is None:
            logger.warning(
                "DaemonLink: %s reported unknown runtime child %s (%s) daemon=%s; "
                "Hive will not create desired state from an observation",
                source, username, runtime_id, self._daemon_id,
            )
            return {
                "unknown": True,
                "runtime_id": runtime_id or child_id or username,
                "username": username,
            }
        # A Hive delete is a durable desired-state tombstone.  A stale daemon
        # snapshot/delta from the workload being torn down must never resurrect
        # it; reconnect reconciliation below keeps sending delete_agent until
        # the daemon has removed its own desired-state row.
        if agent.status == FleetAgent.Status.DELETED and not deleted:
            logger.info(
                "DaemonLink: ignored stale %s for Hive-deleted agent %s daemon=%s",
                source, username, self._daemon_id,
            )
            return
        # INV-OWNER-SCOPE (PLAT-4235 P1): each user's daemon manages ONLY that
        # user's fleet. A per-user daemon (owner_subject set) that reports a brand
        # new agent stamps it with that owner (initial set — owner_id is immutable
        # thereafter). A daemon may never bind an agent whose owner differs from its
        # own scope; the shared org/team daemon (owner_subject=None) owns the
        # owner_id=None org/team agents (backward-compatible with the single daemon).
        if (agent.owner_id or None) != (daemon.owner_subject or None):
            logger.warning(
                "DaemonLink: owner-scope violation — daemon %s (owner=%s) reported "
                "agent %s (owner=%s); refusing to bind (INV-OWNER-SCOPE)",
                self._daemon_id, daemon.owner_subject, username, agent.owner_id,
            )
            return {
                "unknown": True,
                "runtime_id": runtime_id or child_id or username,
                "username": username,
            }
        # Runtime observations never mutate Hive desired config. Even an
        # explicit config_applied delta is observation/legacy acknowledgement:
        # delayed A may arrive after config_result B cleared pending state, so
        # copying A back into the row would revert B. Exact fingerprints (or the
        # legacy value match below) confirm delivery without rewriting desired.
        reason = str(pick("reason", default="") or "")

        agent.agent_id = (runtime_id or agent.agent_id or "")[:64]
        # First adoption can create the per-agent projection row before this
        # FleetAgent has managing_daemon set. Bind it now and require a later
        # authenticated heartbeat (not this observation) to renew its lease.
        if agent.managing_daemon_id != daemon.id:
            from .mcp_projection import fence_projection_authority_leases
            fence_projection_authority_leases([agent.id])
        agent.display_name = normalized.get("display_name") or username
        reported_lifecycle_state = str(
            normalized.get("lifecycle_state") or ""
        ).strip().lower()
        normalized_status = str(normalized.get("status") or "").lower()
        status_map = {
            "running": FleetAgent.Status.RUNNING,
            "starting": FleetAgent.Status.PROVISIONING,
            "provisioning": FleetAgent.Status.PROVISIONING,
            "pending": FleetAgent.Status.PENDING,
            "stopped": FleetAgent.Status.STOPPED,
            "disabled": FleetAgent.Status.STOPPED,
            "offline": FleetAgent.Status.STOPPED,
            "failed": FleetAgent.Status.FAILED,
            "error": FleetAgent.Status.FAILED,
            "deleted": FleetAgent.Status.DELETED,
        }
        observed_status = (
            status
            if status in FleetAgent.Status.values
            else status_map.get(normalized_status, FleetAgent.Status.RUNNING)
        )
        from .runtime_placement import is_retired_host_spawn_observation
        retired_host_spawn = is_retired_host_spawn_observation({
            'status_message': normalized.get('status_message') or '',
            'error': pick('error', default='') or '',
            'status': normalized_status,
        })
        if retired_host_spawn:
            # Host CLI cannot spawn k3s agents. Do not persist Failed /
            # shizuha-runtime-fleet placeholders over a live Deployment.
            if observed_status == FleetAgent.Status.FAILED:
                if agent.hibernation_reason:
                    observed_status = FleetAgent.Status.HIBERNATED
                elif str(agent.runtime_lifecycle_state or '') == 'operator_stopped':
                    observed_status = FleetAgent.Status.STOPPED
                else:
                    observed_status = FleetAgent.Status.RUNNING
            normalized['status_message'] = ''
        # rt-fleet only knows process enabled/disabled. Hive owns the semantic
        # distinction between an automatic, auto-resumable hibernation and an
        # explicit operator stop. Preserve Hive's hibernation intent when the
        # daemon reports its expected disabled process observation.
        agent.status = _lifecycle_status_from_observation(
            current_status=agent.status,
            hibernation_reason=agent.hibernation_reason,
            observed_status=observed_status,
        )
        if agent.status in {
            FleetAgent.Status.RUNNING,
            FleetAgent.Status.PROVISIONING,
            FleetAgent.Status.PENDING,
        }:
            agent.hibernation_reason = ""
            agent.hibernated_at = None
        elif agent.status == FleetAgent.Status.HIBERNATED:
            if not agent.hibernation_reason:
                agent.hibernation_reason = "runtime process not running"
        elif agent.status == FleetAgent.Status.STOPPED:
            # ``stopped`` is the explicit operator kill switch.  It must never
            # retain blue-state metadata: an older daemon consumer briefly
            # wrote migrated hibernated rows back to stopped during the 0037
            # expand/roll rollout.  The follow-up data migration repairs those
            # exact legacy rows; this invariant prevents any later observation
            # or idempotent stop from recreating the ambiguous state.
            agent.hibernation_reason = ""
            agent.hibernated_at = None
        # Hive status is SoT. A stale daemon operator_stopped must not paint
        # a running/hibernated seat as a kill-switch (operator 2026-09-01).
        if agent.status == FleetAgent.Status.STOPPED:
            agent.runtime_lifecycle_state = "operator_stopped"
        elif agent.status == FleetAgent.Status.HIBERNATED:
            agent.runtime_lifecycle_state = "hibernated"
        elif agent.status in {
            FleetAgent.Status.RUNNING,
            FleetAgent.Status.PROVISIONING,
            FleetAgent.Status.PENDING,
        }:
            agent.runtime_lifecycle_state = "enabled"
        elif reported_lifecycle_state in {
            "enabled", "hibernated", "operator_stopped",
        }:
            agent.runtime_lifecycle_state = reported_lifecycle_state
        elif pick("lifecycleState", "lifecycle_state", default=None) is not None:
            agent.runtime_lifecycle_state = ""
        agent.status_message = normalized.get("status_message") or ""
        reported_pod = normalized.get("pod_name") or ""
        if retired_host_spawn and (
            not reported_pod or reported_pod == "shizuha-runtime-fleet"
        ):
            pass
        else:
            agent.pod_name = reported_pod or "shizuha-runtime-fleet"
        agent.managing_daemon = daemon
        # PLAT-4172: surface the heartbeat needs-help escalation from the daemon.
        # Presence-gated so an older daemon that omits the field can't clobber a
        # true flag; a PLAT-4172+ daemon includes it on every frame (register/
        # snapshot/delta), carrying the current value (incl. cleared -> False).
        needs_help = pick("needs_help", default=None)
        if needs_help is not None:
            agent.needs_help = bool(needs_help)
            agent.needs_help_reason = str(pick("needs_help_reason", default="") or "")
            agent.heartbeat_outcome = str(pick("heartbeat_outcome", default="") or "")[:32]
        now = tz.now()
        if agent.status == FleetAgent.Status.RUNNING and agent.provisioned_at is None:
            agent.provisioned_at = now
        # PLAT-4490: status/config frames are observations, not workload
        # activity. Only preserve the daemon's explicit real turn/output clock;
        # never refresh it just because a running state frame arrived.
        raw_last_active_at = pick("last_active_at", "lastActiveAt", default=None)
        if raw_last_active_at:
            parsed_last_active_at = parse_datetime(str(raw_last_active_at))
            if parsed_last_active_at is not None:
                if getattr(settings, "USE_TZ", False):
                    if tz.is_naive(parsed_last_active_at):
                        parsed_last_active_at = tz.make_aware(parsed_last_active_at, datetime_timezone.utc)
                elif tz.is_aware(parsed_last_active_at):
                    # Hive intentionally runs USE_TZ=False and treats stored
                    # lifecycle clocks as naive UTC. Passing an aware value to
                    # PostgreSQL first converts it through TIME_ZONE (Chicago by
                    # Django default), then _within_grace_window later reads the
                    # naive value as UTC — a six-hour skew that can stop an
                    # active agent. Normalize to naive UTC before persistence.
                    parsed_last_active_at = naive_utc(parsed_last_active_at)
                # Never accept a clock from the future. A skewed node clock (or a
                # reintroduced convention slip) must not be able to pin an agent
                # permanently "fresh": the monotonic guard below makes any such
                # value unrecoverable without manual DB surgery, and a
                # permanently-fresh agent is one no staleness check can ever
                # flag. Clamp instead, so the worst case decays into staleness.
                # NOTE: compare in the column's own naive-UTC convention —
                # `tz.now()` here is a naive TIME_ZONE wall clock, NOT UTC.
                utc_now = naive_utc_now()
                if parsed_last_active_at > utc_now + _ACTIVITY_CLOCK_SKEW_GRACE:
                    logger.warning(
                        'agent %s reported last_active_at %s ahead of UTC now %s; clamping',
                        getattr(agent, 'agent_username', None) or agent.pk,
                        parsed_last_active_at, utc_now,
                    )
                    parsed_last_active_at = utc_now
                if agent.last_active_at is None or parsed_last_active_at > agent.last_active_at:
                    # PLAT-7523: capture the pre-advance clock — the half-open
                    # interval [prev, new] is the runtime fact this heartbeat
                    # proves. Enqueued into the metering outbox in the SAME
                    # transaction below (after save), so the fact is durable
                    # the moment the heartbeat commits.
                    prev_last_active_for_metering = agent.last_active_at
                    agent.last_active_at = parsed_last_active_at
        # HIVE-597: desired-over-observed. While a config push awaits the
        # daemon's confirmation, runtime frames must not clobber the desired
        # values the operator was already shown in the UI. A legacy
        # config_applied delta confirms only keys whose REPORTED value matches —
        # so a confirmation for push A can't discard a newer in-flight push B
        # to the same agent (B's keys stay pending + re-asserted until B's own
        # confirmation, or until a daemon reconnect replays them).
        raw_pending = agent.pending_config
        pending = pending_config_or_empty(
            raw_pending,
            agent_identifier=agent.agent_username or agent.pk,
            boundary="daemon_observation",
        )
        if not isinstance(raw_pending, dict):
            # This observation is already persisting the row. Repair the legacy
            # value so the model write boundary remains valid during rollout.
            agent.pending_config = pending
        if pending:
            if source == "delta" and reason == "config_applied":
                remaining = {
                    k: v for k, v in pending.items()
                    if not _pending_key_confirmed(k, v, normalized, mcp_config)
                }
                agent.pending_config = remaining
                if remaining:
                    from .runtime_fleet import _apply_cached_runtime_config
                    _apply_cached_runtime_config(agent, remaining, save=False)
            else:
                from .runtime_fleet import _apply_cached_runtime_config
                _apply_cached_runtime_config(agent, pending, save=False)
        try:
            agent.save()
        except ValidationError as exc:
            logger.warning(
                "DaemonLink: rejected %s for invalid FleetAgent username=%r daemon=%s: %s",
                source, username, self._daemon_id, exc,
            )
            return
        # PLAT-7523: same-transaction runtime-metering enqueue. The interval
        # [prev_last_active, new] was proven alive by this heartbeat; the
        # outbox row commits atomically with the clock advance. Metering
        # failures NEVER break the heartbeat path — logged, and the catch-up
        # sweeper's watermark-gap reporting covers the loss.
        prev_active = prev_last_active_for_metering
        if prev_active is not None:
            try:
                from .metering.events import enqueue_runtime_interval
                enqueue_runtime_interval(
                    agent=agent, prev_active=prev_active,
                    now=agent.last_active_at,
                )
            except Exception as exc:  # noqa: BLE001 — metering must not break heartbeats
                logger.warning(
                    'runtime metering: enqueue failed for agent %s over [%s, %s): %s '
                    '(watermark gap; sweeper covers the final interval)',
                    agent.pk, prev_active, agent.last_active_at, exc,
                )
        # Snapshot/reconnect bursts contain one frame per agent.  Coalesce those
        # observations into a bounded shared-cache generation bump; direct
        # operator mutations still invalidate immediately.
        agent_index_cache.invalidate_observation(f"daemon-link {source}")
        return {"unknown": False, "runtime_id": runtime_id, "username": username}

    async def _send_unknown_runtime_delete(self, observation: dict, reason: str):
        runtime_id = str(observation.get("runtime_id") or "").strip()
        username = str(observation.get("username") or "").strip()
        if not runtime_id:
            return
        await self.send_json({
            "type": "delete_agent",
            "seq": self._next_out_seq(),
            "agent_id": runtime_id,
            "child_id": runtime_id,
            "agent_username": username,
            "reason": reason,
        })
        logger.warning(
            "DaemonLink: rejected unknown runtime child %s (%s) daemon=%s reason=%s",
            username, runtime_id, self._daemon_id, reason,
        )

    async def _handle_identity_event(self, frame: dict):
        """Persist identity_event frame → DaemonIdentityEvent row (HIVE-254 P5)."""
        child_id = frame.get("child_id", "")
        username = frame.get("username", "")
        verdict = frame.get("verdict", "")
        reason = frame.get("reason", "")
        if not child_id or not verdict:
            logger.warning(
                "DaemonLink: identity_event missing child_id or verdict daemon=%s frame=%s",
                self._daemon_id, frame,
            )
            return
        from .models import DaemonIdentityEvent
        if verdict not in DaemonIdentityEvent.Verdict.values:
            logger.warning(
                "DaemonLink: identity_event unknown verdict %r daemon=%s child=%s — dropping",
                verdict, self._daemon_id, child_id,
            )
            return
        logger.info(
            "DaemonLink: identity_event daemon=%s child=%s verdict=%s",
            self._daemon_id, child_id, verdict,
        )
        await self._persist_identity_event(child_id, username, verdict, reason)

    @database_sync_to_async
    def _persist_identity_event(
        self, child_id: str, username: str, verdict: str, reason: str
    ):
        from django.utils import timezone
        from .models import DaemonIdentityEvent, DaemonRegistry

        try:
            daemon = DaemonRegistry.objects.get(pk=self._registry_pk)
        except DaemonRegistry.DoesNotExist:
            logger.warning(
                "DaemonLink: identity_event — registry pk %s not found", self._registry_pk
            )
            return

        now = timezone.now()

        if verdict in (DaemonIdentityEvent.Verdict.ADMITTED, DaemonIdentityEvent.Verdict.REPAIRED):
            # Mark any prior unresolved rejected event for this child as resolved.
            DaemonIdentityEvent.objects.filter(
                daemon=daemon,
                child_id=child_id,
                verdict=DaemonIdentityEvent.Verdict.REJECTED,
                resolved_at__isnull=True,
            ).update(resolved_at=now)

        DaemonIdentityEvent.objects.create(
            daemon=daemon,
            child_id=child_id,
            username=username,
            verdict=verdict,
            reason=reason,
            seen_at=now,
        )

    # ------------------------------------------------------------------ stickiness

    async def daemon_supersede(self, event: dict):
        """Channel-layer handler: a newer connection for this daemon_id took over."""
        if event.get("new_channel") != self.channel_name:
            # Quiesce this lifecycle before the closing handshake.  The durable
            # ws_channel fence below covers the delivery race; this local fence
            # prevents any already-scheduled heartbeat iteration from continuing
            # once the supersede event itself has arrived.
            self._closed = True
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
            logger.info(
                "DaemonLink: superseded by %s for daemon %s",
                event.get("new_channel"), self._daemon_id,
            )
            await self._close_with_reason(
                "superseded", "A newer connection for this daemon has taken over"
            )

    # ------------------------------------------------------------------ helpers

    # Alias DaemonRegistry.Status so _heartbeat_loop and _db_set_status can reference
    # it as self.Status without importing the model at class definition time.
    @property
    def Status(self):
        from .models import DaemonRegistry
        return DaemonRegistry.Status

    def _next_out_seq(self) -> int:
        self._out_seq += 1
        return self._out_seq

    async def _close_with_reason(self, code: str, message: str):
        try:
            await self.send_json({"type": "close_reason", "code": code, "message": message})
        except Exception:
            pass
        await self.close(code=4001)

    async def _register_timeout(self):
        try:
            await asyncio.sleep(_REGISTER_TIMEOUT_S)
            if self._daemon_id is None and not self._closed:
                await self._close_with_reason(
                    "register_timeout",
                    f"No register frame received within {_REGISTER_TIMEOUT_S}s",
                )
        except asyncio.CancelledError:
            pass

    @database_sync_to_async
    def _db_set_status(self, status_str: str):
        from .models import DaemonRegistry
        if self._registry_pk is None or not self._channel_name_at_register:
            return False
        # Every lifecycle status write is fenced to the registering channel.
        # Return the ownership decision so callers only log transitions they
        # actually authored.
        return bool(DaemonRegistry.objects.filter(
            pk=self._registry_pk,
            ws_channel=self._channel_name_at_register,
        ).exclude(status=status_str).update(status=status_str))

    @database_sync_to_async
    def _db_is_current_channel(self):
        from .models import DaemonRegistry
        if self._registry_pk is None or not self._channel_name_at_register:
            return False
        return DaemonRegistry.objects.filter(
            pk=self._registry_pk,
            ws_channel=self._channel_name_at_register,
        ).exists()

    @database_sync_to_async
    def _db_touch_last_seen(self):
        from .models import DaemonRegistry
        from django.utils import timezone as tz
        if self._registry_pk is None or not self._channel_name_at_register:
            return False
        return bool(DaemonRegistry.objects.filter(
            pk=self._registry_pk,
            ws_channel=self._channel_name_at_register,
        ).update(
            last_seen=tz.now(),
            status=DaemonRegistry.Status.CONNECTED,
        ))

    @database_sync_to_async
    def _db_update_harness_report(self, harness_report: dict):
        """Store daemon-observed runtime evidence without authoring intent."""
        from .models import DaemonRegistry
        if self._registry_pk is None or not self._channel_name_at_register:
            return False
        image = str(harness_report.get('agent_runtime_image') or '').strip()
        updates = {'harness_report': harness_report}
        if image:
            updates['agent_runtime_image'] = image[:512]
        return bool(DaemonRegistry.objects.filter(
            pk=self._registry_pk,
            ws_channel=self._channel_name_at_register,
        ).update(**updates))

    @database_sync_to_async
    def _db_advance_cursor(self, seq: int):
        """Advance the resume cursor monotonically (never go backwards)."""
        from .models import DaemonRegistry
        if self._registry_pk is not None:
            DaemonRegistry.objects.filter(
                pk=self._registry_pk,
                last_acked_seq__lt=seq,
            ).update(last_acked_seq=seq)

    @database_sync_to_async
    def _db_mark_config_seeded(self):
        from .models import DaemonRegistry
        from django.utils import timezone as tz
        if self._registry_pk is not None:
            DaemonRegistry.objects.filter(pk=self._registry_pk, config_seeded_at__isnull=True).update(
                config_seeded_at=tz.now()
            )

    @database_sync_to_async
    def _db_current_desired_config_frames(self):
        from django.utils import timezone
        from .config_fingerprints import config_fingerprints
        from .runtime_fleet import supersede_stale_runtime_lane_outboxes
        from .models import (
            AgentRuntimeDesiredState, DaemonRegistry, FleetAgent,
            RuntimeChange, RuntimeLaneOutbox,
        )
        if self._registry_pk is None:
            return []
        try:
            daemon = DaemonRegistry.objects.get(pk=self._registry_pk)
        except DaemonRegistry.DoesNotExist:
            return []
        supersede_stale_runtime_lane_outboxes(daemon_id=self._registry_pk)
        frames = []
        for agent in FleetAgent.objects.filter(managing_daemon=daemon):
            if agent.status == FleetAgent.Status.DELETED:
                runtime_id = agent.agent_id or str(agent.id)
                frames.append({
                    "type": "delete_agent",
                    "agent_id": runtime_id,
                    "child_id": runtime_id,
                    "agent_username": agent.agent_username,
                    "reason": "hive_desired_deleted_on_reconnect",
                })
                continue
            pending_outbox = (
                RuntimeLaneOutbox.objects
                .select_related('change')
                .filter(
                    change__agent=agent,
                    change__state=RuntimeChange.State.ACTUATOR_PENDING,
                    delivered_at__isnull=True,
                )
                .order_by('-change__desired_generation')
                .first()
            )
            if pending_outbox is not None:
                pending_outbox.attempt_count += 1
                pending_outbox.last_attempt_at = timezone.now()
                pending_outbox.save(update_fields=['attempt_count', 'last_attempt_at', 'updated_at'])
                frames.append(effective_config_frame(pending_outbox.payload))
                continue
            mcp_config = agent.mcp_config or {}
            skill_config = agent.skill_config or {}
            config = {
                "model": agent.model or "",
                "model_fallbacks": agent.model_fallbacks or [],
                "model_overrides": agent.model_overrides or {},
                "execution_method": mcp_config.get("execution_method") or "shizuha",
                # Always push k8s so the fleet daemon never re-opens bare_metal spawn.
                "runtime_environment": "k8s",
                "enabled_mcp_server_ids": mcp_config.get("enabled_mcp_server_ids") or [],
                "skills": skill_config.get("skills") or [],
                "eager_skills": skill_config.get("eager_skills") or [],
                "context_prompt": agent.context_prompt or "",
                "personality_traits": agent.personality_traits or {},
                "env": effective_agent_env(agent.env),
                "resource_limits": agent.resource_overrides or {},
                "reasoning_effort": agent.reasoning_effort or "",
                "failover_chain_id": agent.failover_chain_id or "",
            }
            # HIVE-597: include optional mcp_config fields only when set
            # (never emit null — that would synthesize a null-clear on reconnect)
            for opt_key in ("work_schedule", "max_concurrent_tasks", "allow_parallel_execution",
                            "token_budget", "agent_memory", "warm_pool_size", "tier"):
                val = mcp_config.get(opt_key)
                if val is not None:
                    config[opt_key] = val
            frame = {
                "type": "config_update",
                "agent_id": agent.agent_id or str(agent.id),
                "child_id": agent.agent_id or str(agent.id),
                "agent_username": agent.agent_username,
                "reason": "hive_desired_on_reconnect",
                "config": config,
            }
            desired = AgentRuntimeDesiredState.objects.filter(agent=agent).first()
            if desired is not None:
                profile = desired.runtime_profile or {}
                frame.update({
                    "desired_generation": desired.desired_generation,
                    "runtime_lane_registry_version": profile.get("registry_version") or "",
                    "runtime_lane_digest": profile.get("lane_digest") or desired.desired_config_hash,
                    "runtime_lane": profile.get("runtime_lane") or {},
                    "runtime_release_id": (profile.get("runtime_lane") or {}).get("runtime_release_id") or "",
                })
            frame["config_fingerprints"] = config_fingerprints(config)
            frames.append(frame)
        return frames

    @database_sync_to_async
    def _db_retryable_runtime_lane_frames(self):
        """Claim a bounded batch of pending RuntimeLane frames for this daemon."""
        from datetime import timedelta
        from django.db import transaction
        from django.db.models import Q
        from django.utils import timezone
        from .models import DaemonRegistry, RuntimeChange, RuntimeLaneOutbox
        from .runtime_fleet import supersede_stale_runtime_lane_outboxes

        if self._registry_pk is None or not self._channel_name_at_register:
            return []
        retry_before = timezone.now() - timedelta(seconds=5)
        with transaction.atomic():
            # Serialize the ownership decision with the retry-attempt mutation.
            # If B has already superseded A, the channel predicate matches no
            # row and A cannot claim or mutate B's durable outbox work. If A
            # locks first, B's registry update waits until this claim commits,
            # preserving a single linear ownership order.
            current = (
                DaemonRegistry.objects.select_for_update()
                .only('pk')
                .filter(
                    pk=self._registry_pk,
                    ws_channel=self._channel_name_at_register,
                )
                .first()
            )
            if current is None:
                return []
            supersede_stale_runtime_lane_outboxes(daemon_id=self._registry_pk)
            rows = list(
                RuntimeLaneOutbox.objects.select_for_update(skip_locked=True)
                .select_related('change')
                .filter(
                    change__agent__managing_daemon_id=self._registry_pk,
                    change__state=RuntimeChange.State.ACTUATOR_PENDING,
                    delivered_at__isnull=True,
                )
                .filter(Q(last_attempt_at__isnull=True) | Q(last_attempt_at__lte=retry_before))
                .order_by('created_at')[:10]
            )
            now = timezone.now()
            frames = []
            for row in rows:
                row.attempt_count += 1
                row.last_attempt_at = now
                row.last_error = ''
                row.save(update_fields=[
                    'attempt_count', 'last_attempt_at', 'last_error', 'updated_at',
                ])
                frames.append(effective_config_frame(row.payload))
            return frames

    @database_sync_to_async
    def _db_pending_config_frames(self):
        """Return a bounded, receiver-unconfirmed config retry batch."""
        from .config_fingerprints import config_fingerprints
        from .models import FleetAgent, RuntimeChange, RuntimeLaneOutbox

        if self._registry_pk is None:
            return []
        lane_agent_ids = RuntimeLaneOutbox.objects.filter(
            change__agent__managing_daemon_id=self._registry_pk,
            change__state=RuntimeChange.State.ACTUATOR_PENDING,
            delivered_at__isnull=True,
        ).values_list('change__agent_id', flat=True)
        frames = []
        rows = (
            FleetAgent.objects
            .filter(managing_daemon_id=self._registry_pk)
            .exclude(id__in=lane_agent_ids)
            .exclude(pending_config={})
            .order_by('updated_at')[:10]
        )
        for agent in rows:
            config = pending_config_or_empty(
                agent.pending_config,
                agent_identifier=agent.agent_username or agent.pk,
                boundary="heartbeat_retry",
            )
            if not config:
                continue
            runtime_id = agent.agent_id or str(agent.id)
            outgoing_config = effective_runtime_config(config)
            frames.append({
                'type': 'config_update',
                'agent_id': runtime_id,
                'child_id': runtime_id,
                'agent_username': agent.agent_username,
                'reason': 'hive_pending_config_retry',
                'config': outgoing_config,
                'config_fingerprints': config_fingerprints(outgoing_config),
            })
        return frames

    @database_sync_to_async
    def _db_set_snapshot_complete(self, value: bool):
        from .models import DaemonRegistry
        if self._registry_pk is not None:
            DaemonRegistry.objects.filter(pk=self._registry_pk).update(
                snapshot_complete=value
            )

    # ---------------------------------------------------------------- recovery actions (HIVE-332 P3)

    async def daemon_recovery_action(self, event: dict):
        """Channel-layer handler: Hive-side code requests sending a
        `recovery_action` frame to this daemon (fleet.recovery.send_recovery_action
        group-sends to ``daemon-link-<daemon_id>``)."""
        frame = dict(event.get("frame") or {})
        if not frame:
            return
        frame.setdefault("type", "recovery_action")
        frame["seq"] = self._next_out_seq()
        await self.send_json(frame)
        logger.info(
            "DaemonLink: sent recovery_action %s (%s) to daemon %s",
            frame.get("action_id"), frame.get("action_type"), self._daemon_id,
        )

    async def daemon_config(self, event: dict):
        """Channel-layer handler for Hive→daemon top-down config pushes."""
        frame = dict(event.get("frame") or {})
        if not frame:
            return
        frame.setdefault("type", "config_update")
        frame["seq"] = self._next_out_seq()
        await self.send_json(frame)
        logger.info(
            "DaemonLink: sent config_update agent=%s fields=%s daemon=%s",
            frame.get("agent_id") or frame.get("child_id"),
            sorted((frame.get("config") or {}).keys()) if isinstance(frame.get("config"), dict) else [],
            self._daemon_id,
        )

    async def daemon_delete_agent(self, event: dict):
        """Channel-layer handler for Hive's durable agent tombstone."""
        frame = dict(event.get("frame") or {})
        if not frame:
            return
        frame.setdefault("type", "delete_agent")
        frame["seq"] = self._next_out_seq()
        await self.send_json(frame)
        logger.info(
            "DaemonLink: sent delete_agent agent=%s daemon=%s",
            frame.get("agent_id") or frame.get("child_id"), self._daemon_id,
        )

    async def _handle_recovery_result(self, frame: dict):
        """Persist a daemon `recovery_result` frame onto its AgentRecoveryAction."""
        if self._registry_pk is None:
            logger.warning(
                "DaemonLink: recovery_result before register daemon=%s — dropping",
                self._daemon_id,
            )
            return
        seq = int(frame.get("seq", 0))
        await self._persist_recovery_result(frame)
        await self.send_json({"type": "ack", "seq": seq})
        await self._db_advance_cursor(seq)

    async def _handle_config_result(self, frame: dict):
        """Converge pending config from the daemon's fail-closed apply result."""
        seq = int(frame.get("seq", 0) or frame.get("ack_seq", 0) or 0)
        ok = bool(frame.get("ok"))
        runtime_result = await self._db_record_runtime_lane_config_result(frame)
        rollback_frame = runtime_result.get("rollback_frame")
        if rollback_frame:
            rollback_frame["seq"] = self._next_out_seq()
            await self.send_json(rollback_frame)
        if ok and not runtime_result.get("fence_rejected"):
            confirmation = await self._db_confirm_config_result(frame)
            logger.info(
                "DaemonLink: config_result ok agent=%s daemon=%s confirmed=%s remaining=%s",
                frame.get("agent_id") or frame.get("child_id"), self._daemon_id,
                confirmation.get("confirmed", []), confirmation.get("remaining", []),
            )
        else:
            logger.warning(
                "DaemonLink: config_result failed agent=%s daemon=%s error=%s",
                frame.get("agent_id") or frame.get("child_id"), self._daemon_id, frame.get("error"),
            )
        if seq:
            await self._db_advance_cursor(seq)

    @database_sync_to_async
    def _db_record_runtime_lane_config_result(self, frame: dict) -> dict:
        from django.db.models import Q
        from .models import FleetAgent, RuntimeChange
        from .runtime_fleet import process_runtime_lane_config_result

        runtime_id = str(frame.get("agent_id") or frame.get("child_id") or "").strip()
        if self._registry_pk is None or not runtime_id:
            return {}
        from .runtime_lane import RUNTIME_LANE_FIELDS
        fingerprint_keys = set((frame.get("config_fingerprints") or {}).keys())
        if not RUNTIME_LANE_FIELDS.intersection(fingerprint_keys):
            return {}
        agent = (
            FleetAgent.objects.filter(managing_daemon_id=self._registry_pk)
            .filter(Q(agent_id=runtime_id) | Q(agent_username=runtime_id))
            .first()
        )
        if agent is None or not RuntimeChange.objects.filter(
            agent=agent,
            state__in=[RuntimeChange.State.ACTUATOR_PENDING, RuntimeChange.State.ROLLING],
        ).exists():
            return {}
        result = process_runtime_lane_config_result(agent, frame)
        result["fence_rejected"] = not result.get("matched")
        if result["fence_rejected"]:
            logger.warning(
                "DaemonLink: rejected unfenced/stale runtime result agent=%s daemon=%s reason=%s",
                runtime_id, self._daemon_id, result.get("reason"),
            )
        return result

    @database_sync_to_async
    def _db_confirm_config_result(self, frame: dict) -> dict:
        """Clear only pending values proven applied by this daemon response.

        Fingerprints are generated by Hive and merely echoed by the daemon
        after its fail-closed persistence gate succeeds.  Comparing each echo
        to the *current* pending value fences out delayed acknowledgements for
        an older edit while avoiding any secret-value round trip.
        """
        import hmac
        from django.db import transaction
        from django.db.models import Q

        from . import agent_index_cache
        from .config_fingerprints import config_value_fingerprint
        from .models import FleetAgent

        raw = frame.get("config_fingerprints")
        if self._registry_pk is None or not isinstance(raw, dict) or not raw:
            return {"confirmed": [], "remaining": []}

        fingerprints = {
            str(key): value.lower()
            for key, value in raw.items()
            if isinstance(value, str)
            and len(value) == 64
            and all(char in "0123456789abcdefABCDEF" for char in value)
        }
        if not fingerprints:
            return {"confirmed": [], "remaining": []}

        runtime_id = str(frame.get("agent_id") or frame.get("child_id") or "").strip()
        if not runtime_id:
            return {"confirmed": [], "remaining": []}
        with transaction.atomic():
            # Serialize against a simultaneous Hive edit. Without this row lock,
            # an acknowledgement could read push A, race push B's save, then
            # overwrite B's newly pending value with A's reduced dictionary.
            agent = (
                FleetAgent.objects
                .select_for_update()
                .filter(managing_daemon_id=self._registry_pk)
                .filter(Q(agent_id=runtime_id) | Q(agent_username=runtime_id))
                .first()
            )
            if agent is None:
                logger.warning(
                    "DaemonLink: ignored config_result for unowned agent=%s daemon=%s",
                    runtime_id, self._daemon_id,
                )
                return {"confirmed": [], "remaining": []}

            raw_pending = agent.pending_config
            pending = pending_config_or_empty(
                raw_pending,
                agent_identifier=agent.agent_username or agent.pk,
                boundary="config_result",
            )
            confirmed = [
                key for key, value in effective_runtime_config(pending).items()
                if key in fingerprints
                and hmac.compare_digest(
                    fingerprints[key], config_value_fingerprint(value),
                )
            ]
            if confirmed or not isinstance(raw_pending, dict):
                for key in confirmed:
                    pending.pop(key, None)
                agent.pending_config = pending
                agent.save(update_fields=["pending_config", "updated_at"])
        if confirmed:
            agent_index_cache.invalidate("daemon-link config-result confirmation")
        return {"confirmed": sorted(confirmed), "remaining": sorted(pending)}


    @database_sync_to_async
    def _persist_recovery_result(self, frame: dict):
        from .models import DaemonRegistry
        from .recovery import process_recovery_result

        try:
            daemon = DaemonRegistry.objects.get(pk=self._registry_pk)
        except DaemonRegistry.DoesNotExist:
            return
        process_recovery_result(frame, daemon)

    # ---------------------------------------------------------------- health_snapshot (HIVE-296 P1)

    async def _handle_health_snapshot(self, frame: dict):
        """Persist a health_snapshot frame → AgentHealthSnapshot + classified AgentHealthIssue rows."""
        if self._registry_pk is None:
            logger.warning("DaemonLink: health_snapshot before register daemon=%s — dropping", self._daemon_id)
            return
        child_id = str(frame.get("child_id") or frame.get("agent_id") or "")
        snap_type = str(frame.get("snapshot_type") or "full")
        logger.debug(
            "DaemonLink: health_snapshot child=%s type=%s daemon=%s",
            child_id, snap_type, self._daemon_id,
        )
        runtime_result = await self._persist_health_snapshot(frame, child_id, snap_type)
        rollback_frame = (runtime_result or {}).get("rollback_frame")
        if rollback_frame:
            rollback_frame["seq"] = self._next_out_seq()
            await self.send_json(rollback_frame)

    @database_sync_to_async
    def _persist_health_snapshot(self, frame: dict, child_id: str, snap_type: str):
        return self._persist_health_snapshot_sync(frame, child_id, snap_type)

    def _persist_health_snapshot_sync(self, frame: dict, child_id: str, snap_type: str):
        from django.utils import timezone as tz
        from .models import (
            AgentHealthSnapshot, AgentHealthIssue, AgentRuntimeDesiredState,
            DaemonRegistry, FleetAgent,
        )
        from .health_snapshot import classify_snapshot

        try:
            daemon = DaemonRegistry.objects.get(pk=self._registry_pk)
        except DaemonRegistry.DoesNotExist:
            return

        # Resolve agent row (by child_id username, then by runtime_email)
        agent = None
        username = str(frame.get("runtime_username") or child_id or "").strip().lower()
        if username:
            agent = FleetAgent.objects.filter(agent_username=username).first()
        if agent is None and frame.get("runtime_email"):
            runtime_email = str(frame["runtime_email"]).strip().lower()
            agent = FleetAgent.objects.filter(
                agent_username=runtime_email.split("@")[0]
            ).first()

        # Fetch desired state for generation/capability drift checks
        desired_dict = None
        if agent is not None:
            try:
                ds = AgentRuntimeDesiredState.objects.get(agent=agent)
                desired_dict = {
                    "desired_generation": ds.desired_generation,
                    "desired_config_hash": ds.desired_config_hash,
                    "effective_capabilities": ds.effective_capabilities,
                    "skills": ds.skills,
                    "mcp_access": ds.mcp_access,
                    "credential_grant_expectations": ds.credential_grant_expectations,
                }
            except AgentRuntimeDesiredState.DoesNotExist:
                pass

        now = tz.now()

        # Upsert snapshot (one row per agent+daemon for full snapshots; append for delta)
        snap_fields = {
            "runtime_id": str(frame.get("runtime_id") or ""),
            "container_pod_id": str(frame.get("container_pod_id") or ""),
            "runtime_username": str(frame.get("runtime_username") or ""),
            "runtime_email": str(frame.get("runtime_email") or ""),
            "last_heartbeat_identity_email": str(frame.get("last_heartbeat_identity_email") or ""),
            "platform_user_id": str(frame.get("platform_user_id") or ""),
            "canonical_email": str(frame.get("canonical_email") or ""),
            "account_type": str(frame.get("account_type") or ""),
            "account_active": frame.get("account_active"),
            "alias_resolution_evidence": frame.get("alias_resolution_evidence") or {},
            "desired_generation_seen": frame.get("desired_generation_seen"),
            "applied_generation": frame.get("applied_generation"),
            "observed_generation": frame.get("observed_generation"),
            "observed_config_hash": str(frame.get("observed_config_hash") or ""),
            "apply_status": str(frame.get("apply_status") or AgentHealthSnapshot.ApplyStatus.UNKNOWN),
            "last_apply_error": str(frame.get("last_apply_error") or ""),
            "effective_capabilities": frame.get("effective_capabilities") or [],
            "skills": frame.get("skills") or {},
            "mcp_access": frame.get("mcp_access") or [],
            "credential_grant_health": frame.get("credential_grant_health") or {},
            "missing_grants": frame.get("missing_grants") or [],
            "extra_grants": frame.get("extra_grants") or [],
            "pulse_assignable": frame.get("pulse_assignable"),
            "roster_assignable_reason": str(frame.get("roster_assignable_reason") or ""),
            "team_roster_memberships": frame.get("team_roster_memberships") or [],
            "provider_health": frame.get("provider_health") or {},
            "snapshot_type": snap_type if snap_type in ("full", "delta") else "full",
            "source_error": str(frame.get("source_error") or ""),
            "last_heartbeat_at": frame.get("last_heartbeat_at"),
        }

        if snap_type == "full" and agent is not None:
            # Upsert: replace the single latest-full snapshot for this (agent, daemon) pair
            snapshot, _ = AgentHealthSnapshot.objects.update_or_create(
                agent=agent,
                daemon=daemon,
                snapshot_type="full",
                defaults=snap_fields,
            )
        else:
            snapshot = AgentHealthSnapshot.objects.create(
                agent=agent,
                daemon=daemon,
                **snap_fields,
            )
            # HIVE-313: delta frames append a new row each time — cap retention so
            # they cannot grow unbounded (full snapshots stay a single upserted row).
            stale_ids = list(
                AgentHealthSnapshot.objects.filter(
                    agent=agent, daemon=daemon, snapshot_type="delta",
                ).order_by("-received_at").values_list("id", flat=True)[_DELTA_SNAPSHOT_RETENTION:]
            )
            if stale_ids:
                AgentHealthSnapshot.objects.filter(id__in=stale_ids).delete()

        if agent is None:
            return  # Can't classify without an agent row

        from .runtime_fleet import process_runtime_lane_health
        runtime_result = process_runtime_lane_health(agent, frame)

        # Classify issues
        classified = classify_snapshot(frame, desired_dict)
        # HIVE-313: the classifier can emit N instances of one issue_type (e.g. two
        # missing grants). Rows stay keyed per (agent, issue_type) — documented
        # choice — so instances are MERGED into the one row (expected/actual/
        # field_path carry ALL instances) instead of last-writer-wins dropping all
        # but one.
        merged = self._merge_issue_instances(classified)
        active_types = set(merged.keys())

        # Resolve open issues that no longer fire
        AgentHealthIssue.objects.filter(
            agent=agent,
            status=AgentHealthIssue.Status.OPEN,
        ).exclude(
            issue_type__in=list(active_types),
        ).update(status=AgentHealthIssue.Status.RESOLVED, resolved_at=now)

        # Upsert each active issue. first_seen is set only on CREATE (HIVE-313 P2:
        # it previously sat in update defaults, so every snapshot reset it and the
        # issue age that staleness/SLA views need was destroyed).
        for d in merged.values():
            updatable = {
                "snapshot": snapshot,
                "severity": d["severity"],
                "service": d["service"],
                "field_path": d["field_path"],
                "expected": d["expected"],
                "actual": d["actual"],
                "detail": d["detail"],
                "last_seen": now,
            }
            issue, created = AgentHealthIssue.objects.get_or_create(
                agent=agent,
                issue_type=d["issue_type"],
                status=AgentHealthIssue.Status.OPEN,
                defaults={**updatable, "first_seen": now},
            )
            if not created:
                for field, value in updatable.items():
                    setattr(issue, field, value)
                issue.save(update_fields=list(updatable.keys()))
        return runtime_result

    @staticmethod
    def _merge_issue_instances(classified: list) -> dict:
        """Merge classifier instances into one dict per issue_type (HIVE-313).

        Single-instance types pass through untouched. For multi-instance types the
        merged row lists every instance: expected/actual become instance lists,
        field_path joins the distinct paths, detail concatenates, severity keeps
        the worst instance.
        """
        severity_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
        merged: dict = {}
        for d in classified:
            existing = merged.get(d["issue_type"])
            if existing is None:
                merged[d["issue_type"]] = dict(d)
                continue
            if "_instances" not in existing:
                existing["_instances"] = [
                    {k: existing[k] for k in ("field_path", "expected", "actual", "detail")}
                ]
            existing["_instances"].append(
                {k: d[k] for k in ("field_path", "expected", "actual", "detail")}
            )
            instances = existing["_instances"]
            paths = []
            for inst in instances:
                if inst["field_path"] and inst["field_path"] not in paths:
                    paths.append(inst["field_path"])
            existing["field_path"] = ",".join(paths)[:256]
            existing["expected"] = [inst["expected"] for inst in instances]
            existing["actual"] = [inst["actual"] for inst in instances]
            existing["detail"] = "; ".join(inst["detail"] for inst in instances if inst["detail"])
            if severity_rank.get(d["severity"], 0) > severity_rank.get(existing["severity"], 0):
                existing["severity"] = d["severity"]
        for d in merged.values():
            d.pop("_instances", None)
        return merged
