"""Lifecycle/backstop primitives for Hive claws.

This module is the controller-facing surface for HIVE-4/HIVE-7 follow-up work.
It centralizes the ownership-safe state changes that the owner/admin API views,
reclaim CronJob, and future export/hard-delete workers call into.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable
import hashlib
import hmac
import secrets

from django.conf import settings
from django.utils import timezone

from .models import Claw, ClawEvent

RECYCLE_AFTER_ANNOTATION = "hive.shizuha.io/recycle-after"
RECLAIM_NOW_ANNOTATION = "hive.shizuha.io/reclaim-now"
LEGAL_HOLD_ANNOTATION = "hive.shizuha.io/legal-hold"
AUTH_STATE_CACHE_TTL_SECONDS = 5
ORDINARY_DELETE_UNDO_WINDOW = timedelta(seconds=60)
UNDO_DELETE_WINDOW = ORDINARY_DELETE_UNDO_WINDOW
DELAYED_RECLAIM_RETENTION = timedelta(hours=24)


@dataclass(frozen=True)
class ReclaimCandidate:
    """A claw selected by the lifecycle backstop for later hard-delete work."""

    claw_id: str
    namespace: str
    reason: str


def _log(claw: Claw, event_type: str, message: str) -> None:
    ClawEvent.objects.create(claw=claw, event_type=event_type, message=message)


def internal_auth_token(claw: Claw) -> str:
    """Return the per-claw internal credential for OpenClaw handshakes.

    ``HIVE_INTERNAL_AUTH_TOKEN`` is the hive-api signing secret only; tenant
    pods receive an HMAC scoped to their own claw id/namespace. Internal
    endpoints recompute the scoped value for the URL claw id, so one claw's
    mounted credential cannot be replayed against another claw's auth/hold-state
    endpoint. Empty signing secret fails closed by returning an empty token.
    """
    secret = getattr(settings, 'HIVE_INTERNAL_AUTH_TOKEN', '')
    if not secret:
        return ''
    namespace = getattr(claw, 'namespace', '') or ''
    message = f"{claw.id}:{namespace}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def gateway_auth_token(claw: Claw) -> str:
    """Return the per-claw OpenClaw GATEWAY bind token (PLAT-905).

    Newer OpenClaw gateways refuse to bind to the LAN without an auth token
    (exit 78). This derives a stable per-claw token the same way as
    ``internal_auth_token`` but with a distinct ``:gateway`` purpose suffix, so
    it is reproducible across re-provisions (no stored-secret drift), unique per
    claw, and never equal to the internal-auth token. Injected as
    ``OPENCLAW_GATEWAY_TOKEN`` into the claw container so the gateway binds.
    Falls back to a random token when the signing secret is unset, so a claw
    still binds (just non-deterministically) rather than crash-looping.
    """
    secret = getattr(settings, 'HIVE_INTERNAL_AUTH_TOKEN', '')
    if not secret:
        return secrets.token_urlsafe(32)
    namespace = getattr(claw, 'namespace', '') or ''
    message = f"{claw.id}:{namespace}:gateway".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def annotation_state(claw: Claw) -> dict[str, str]:
    """Return the lifecycle annotations the controller would stamp on k8s objects.

    Precedence is intentionally visible to callers: legal hold wins over both
    reclaim-now and recycle-after (HIVE-4 §5.4), so those annotations are omitted
    while a hold is active even if their DB fields are set.
    """
    if claw.legal_hold:
        annotations = {LEGAL_HOLD_ANNOTATION: "true"}
        if claw.recycle_after_frozen_at:
            annotations["hive.shizuha.io/recycle-after-frozen-at"] = claw.recycle_after_frozen_at.isoformat()
        return annotations

    annotations: dict[str, str] = {}
    if claw.reclaim_now:
        annotations[RECLAIM_NOW_ANNOTATION] = "true"
    if claw.recycle_after:
        annotations[RECYCLE_AFTER_ANNOTATION] = claw.recycle_after.isoformat()
    return annotations


def hold_state(claw: Claw) -> dict:
    """Internal hold-state payload consumed by export/offboarding workers."""
    return {
        "claw_id": str(claw.id),
        "namespace": claw.namespace,
        "legal_hold": claw.legal_hold,
        "legal_hold_reason": claw.legal_hold_reason,
        "legal_hold_case_id": claw.legal_hold_case_id,
        "legal_hold_set_by": claw.legal_hold_set_by,
        "legal_hold_set_at": claw.legal_hold_set_at,
        "recycle_after": claw.recycle_after,
        "reclaim_now": claw.reclaim_now,
        "annotations": annotation_state(claw),
    }


def owner_hold_state(claw: Claw) -> dict:
    """Owner-safe lifecycle state for mutation responses.

    Owner routes may tell the caller that a legal hold blocks their action, but
    must not expose internal legal case metadata. Admin/internal hold-state
    routes continue to use ``hold_state()`` above.
    """
    return {
        "claw_id": str(claw.id),
        "legal_hold": claw.legal_hold,
        "recycle_after": claw.recycle_after,
        "reclaim_now": claw.reclaim_now,
        "annotations": annotation_state(claw),
    }


def set_legal_hold(claw: Claw, *, reason: str = "", case_id: str = "", actor: str = "") -> Claw:
    """Set legal hold and freeze any pending recycle timestamp.

    Legal hold has highest precedence. Existing reclaim/recycle intent remains in
    the database for audit/retry, but controller annotations suppress it until
    the hold is cleared.
    """
    if claw.legal_hold:
        # Idempotent update lets admins enrich the reason/case without changing
        # the timestamp unless a new value is provided.
        if reason:
            claw.legal_hold_reason = reason
        if case_id:
            claw.legal_hold_case_id = case_id
        if actor:
            claw.legal_hold_set_by = actor
        claw.save(update_fields=[
            "legal_hold_reason", "legal_hold_case_id", "legal_hold_set_by", "updated_at",
        ])
        _log(claw, "legal_hold_updated", "Legal hold metadata updated")
        return claw

    claw.legal_hold = True
    claw.legal_hold_reason = reason
    claw.legal_hold_case_id = case_id
    claw.legal_hold_set_by = actor
    now = timezone.now()
    claw.legal_hold_set_at = now
    if claw.recycle_after and not claw.recycle_after_frozen_at:
        # HIVE-4 freeze model: store the freeze moment, not a second copy of
        # recycle_after. Remaining time is derived as recycle_after - frozen_at.
        claw.recycle_after_frozen_at = now
    claw.save(update_fields=[
        "legal_hold", "legal_hold_reason", "legal_hold_case_id",
        "legal_hold_set_by", "legal_hold_set_at", "recycle_after_frozen_at", "updated_at",
    ])
    _log(claw, "legal_hold_set", "Legal hold set; reclaim/recycle annotations suppressed")
    return claw


def clear_legal_hold(claw: Claw, *, actor: str = "") -> Claw:
    """Clear legal hold and restore the frozen recycle timestamp if needed."""
    was_held = claw.legal_hold
    claw.legal_hold = False
    claw.legal_hold_reason = ""
    claw.legal_hold_case_id = ""
    claw.legal_hold_set_by = ""
    claw.legal_hold_set_at = None
    if claw.recycle_after is not None and claw.recycle_after_frozen_at is not None:
        remaining = claw.recycle_after - claw.recycle_after_frozen_at
        if remaining.total_seconds() < 0:
            remaining = timedelta(seconds=0)
        claw.recycle_after = timezone.now() + remaining
    claw.recycle_after_frozen_at = None
    claw.save(update_fields=[
        "legal_hold", "legal_hold_reason", "legal_hold_case_id",
        "legal_hold_set_by", "legal_hold_set_at", "recycle_after",
        "recycle_after_frozen_at", "updated_at",
    ])
    _log(claw, "legal_hold_cleared", f"Legal hold cleared by {actor or 'unknown'}" if was_held else "Legal hold already clear")
    return claw


def schedule_recycle(claw: Claw, recycle_after, *, reason: str = "") -> Claw:
    """Schedule a claw for delayed reclaim after export/offboarding finishes."""
    claw.recycle_after = recycle_after
    if claw.legal_hold and not claw.recycle_after_frozen_at:
        claw.recycle_after_frozen_at = timezone.now()
    claw.save(update_fields=["recycle_after", "recycle_after_frozen_at", "updated_at"])
    _log(claw, "recycle_scheduled", reason or f"Recycle scheduled after {recycle_after.isoformat()}")
    return claw



def schedule_delayed_reclaim(
    claw: Claw,
    *,
    now=None,
    retention: timedelta = DELAYED_RECLAIM_RETENTION,
    reason: str = "",
) -> Claw:
    """Move a claw from the 60s undo window into the 24h delayed reclaim pool."""
    now = now or timezone.now()
    claw.status = Claw.Status.DELETING
    if not claw.delete_requested_at:
        claw.delete_requested_at = now
    claw.reclaim_now = False
    claw.recycle_after = now + retention
    if claw.legal_hold and not claw.recycle_after_frozen_at:
        claw.recycle_after_frozen_at = now
    claw.save(update_fields=[
        "status", "delete_requested_at", "reclaim_now", "recycle_after",
        "recycle_after_frozen_at", "updated_at",
    ])
    _log(claw, "delayed_reclaim_scheduled", reason or f"Delayed reclaim scheduled for {claw.recycle_after.isoformat()}")
    return claw


def promote_expired_delete_windows(now=None) -> int:
    """Backstop hook: convert expired 60s deletes into 24h reclaim-pool entries."""
    now = now or timezone.now()
    cutoff = now - UNDO_DELETE_WINDOW
    claws = (
        Claw.objects
        .exclude(status=Claw.Status.DELETED)
        .filter(status=Claw.Status.DELETING, delete_requested_at__lte=cutoff)
        .filter(recycle_after__isnull=True, reclaim_now=False)
    )
    count = 0
    for claw in claws:
        schedule_delayed_reclaim(claw, now=now, reason="60s undo window expired")
        count += 1
    return count


def auth_state(claw: Claw) -> dict:
    """OpenClaw dashboard handshake state for HIVE-3 section 6.4.1."""
    if claw.status == Claw.Status.DELETED:
        authorized, reason = False, "not-found"
    elif claw.status in {Claw.Status.SUSPENDED_OWNER_REMOVED, Claw.Status.SUSPENDED_NO_CREDITS}:
        authorized, reason = False, claw.status
    else:
        authorized, reason = True, "ok"
    return {
        "claw_id": str(claw.id),
        "owner_id": claw.owner_id,
        "authorized": authorized,
        "reason": reason,
        "cache_ttl_seconds": AUTH_STATE_CACHE_TTL_SECONDS,
        "status": claw.status,
    }


def reclaim_pool_metrics(now=None) -> dict:
    """Return HIVE-3 reclaim-pool gauges for metrics exporters/backstop tests."""
    now = now or timezone.now()
    pending = (
        Claw.objects
        # HIVE-598: same DELETING scope as reclaim_candidates — provision-time
        # recycle_after stamps on live claws must not inflate the pool gauges.
        .filter(status=Claw.Status.DELETING)
        .filter(legal_hold=False, recycle_after__isnull=False)
        .order_by("recycle_after")
    )
    oldest = pending.first()
    return {
        "hive_recycle_pool_size": pending.count(),
        "hive_recycle_pool_oldest_age_seconds": (
            max(0, (now - oldest.recycle_after).total_seconds()) if oldest else 0
        ),
    }

def mark_reclaim_now(claw: Claw, *, reason: str = "", actor: str = "") -> tuple[Claw, bool]:
    """Request immediate reclaim/hard-delete.

    Returns ``(claw, accepted)``. A legal hold blocks immediate reclaim without
    mutating the claw, matching HIVE-4 precedence.
    """
    if claw.legal_hold:
        _log(claw, "reclaim_blocked", "Immediate reclaim blocked by legal hold")
        return claw, False
    previous_status = claw.status if claw.status != Claw.Status.DELETING else ""
    claw.reclaim_now = True
    claw.reclaim_previous_status = previous_status
    claw.recycle_after = None
    claw.recycle_after_frozen_at = None
    claw.status = Claw.Status.DELETING
    if not claw.delete_requested_at:
        claw.delete_requested_at = timezone.now()
    claw.save(update_fields=[
        "reclaim_now", "reclaim_previous_status", "recycle_after",
        "recycle_after_frozen_at", "status", "delete_requested_at", "updated_at",
    ])
    _log(claw, "reclaim_requested", reason or f"Immediate reclaim requested by {actor or 'unknown'}")
    return claw, True


def restore_from_reclaim(claw: Claw, *, actor: str = "") -> Claw:
    """Cancel owner/admin reclaim intent while preserving retained PVC state."""
    claw.reclaim_now = False
    claw.recycle_after = None
    claw.recycle_after_frozen_at = None
    claw.delete_requested_at = None
    if claw.status == Claw.Status.DELETING:
        claw.status = claw.reclaim_previous_status or Claw.Status.STOPPED
    claw.reclaim_previous_status = ""
    claw.save(update_fields=[
        "reclaim_now", "reclaim_previous_status", "recycle_after",
        "recycle_after_frozen_at", "delete_requested_at", "status", "updated_at",
    ])
    _log(claw, "reclaim_cancelled", f"Reclaim cancelled by {actor or 'unknown'}")
    return claw


def reclaim_candidates(now=None) -> Iterable[ReclaimCandidate]:
    """Return claws the backstop should hand to the hard-delete worker.

    This is intentionally side-effect free: HIVE-7 can attach the concrete export
    and k8s teardown workers here without duplicating hold/recycle precedence.
    """
    now = now or timezone.now()
    queryset = (
        Claw.objects
        # HIVE-598: only claws the delete flow parked in DELETING are reclaim
        # candidates. `recycle_after` is ALSO stamped at provision time
        # (created+30min) on live claws, so selecting on it alone would hand
        # RUNNING assistants to the hard-delete worker (observed 2026-07-09:
        # 8 of 9 candidates were running).
        .filter(status=Claw.Status.DELETING)
        .filter(legal_hold=False)
        .filter(models_q_reclaim_ready(now))
    )
    for claw in queryset.only(
        "id", "namespace", "status", "reclaim_now", "recycle_after", "delete_requested_at"
    ):
        if claw.reclaim_now:
            reason = "reclaim-now"
        else:
            reason = "recycle-after"
        yield ReclaimCandidate(str(claw.id), claw.namespace, reason)


def models_q_reclaim_ready(now):
    """Build the reclaim-ready predicate separately for tests/import hygiene."""
    from django.db.models import Q

    return (
        Q(reclaim_now=True)
        | Q(recycle_after__isnull=False, recycle_after__lte=now)
    )


def idle_stop_candidates(now=None):
    """Running OpenClaw tenants whose idle TTL has elapsed (ADR-0080).

    ``idle_recycle_after`` is stamped at provision/start. A NULL deadline on a
    RUNNING row is treated as already due — those are the legacy pods that sat
    Running for months because nothing consumed the field.
    """
    from django.db.models import Q

    now = now or timezone.now()
    return (
        Claw.objects
        .filter(status=Claw.Status.RUNNING, legal_hold=False)
        .filter(Q(idle_recycle_after__isnull=True) | Q(idle_recycle_after__lte=now))
        .order_by('idle_recycle_after')
    )
