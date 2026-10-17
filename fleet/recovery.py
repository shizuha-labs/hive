"""HIVE-332 (HIVE-277 P3) — Hive-side recovery-action service.

Creates AgentRecoveryAction rows for persisted AgentHealthIssue rows, builds
and sends the Hive→daemon `recovery_action` frame over the authenticated
daemon-link WebSocket (via the channel-layer group the consumer joins), and
processes daemon→Hive `recovery_result` frames into the audit trail.

v1 is observe/dry-run only: the model's clean() enforces the safe-action
allowlist and the P4 apply gate; this module additionally redacts any
secret-looking material a daemon result might carry before persisting it.
"""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any

from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import (
    AgentHealthIssue,
    AgentRecoveryAction,
    _EMBEDDED_SECRET_RE,
    _HEX_DIGEST_RE,
    _looks_like_plaintext_secret,
    record_recovery_secret_rejection,
    recovery_secret_rejection_count,
)

logger = logging.getLogger(__name__)

# Default validity window for a recovery action (frame carries the absolute
# expires_at; the daemon independently skips expired actions).
DEFAULT_ACTION_TTL = timedelta(minutes=15)

REDACTED_MARKER = '[redacted-secret]'


def redact_secret_material(value: Any) -> Any:
    """Deep-copy ``value`` replacing secret-looking strings with a marker.

    Used on inbound daemon payloads before persistence so a misbehaving daemon
    can never make Hive store token material. Pure-hex digests (config hashes)
    pass through — they reference config without carrying it.
    """
    if isinstance(value, str):
        candidate = value.strip()
        if _HEX_DIGEST_RE.match(candidate.lower()):
            return value
        if _looks_like_plaintext_secret(candidate):
            record_recovery_secret_rejection()
            return REDACTED_MARKER
        # Also scrub token material embedded mid-string (e.g. interpolated
        # into a daemon error message).
        cleaned = _EMBEDDED_SECRET_RE.sub(REDACTED_MARKER, value)
        if cleaned != value:
            record_recovery_secret_rejection()
        return cleaned
    if isinstance(value, dict):
        return {key: redact_secret_material(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_secret_material(item) for item in value]
    return value


def create_recovery_action(
    agent,
    action_type: str,
    *,
    issues=None,
    mode: str = AgentRecoveryAction.Mode.DRY_RUN,
    params: dict | None = None,
    target_runtime_id: str = '',
    ttl: timedelta = DEFAULT_ACTION_TTL,
    actor: str = '',
    source: str = 'hive_api',
    daemon=None,
) -> AgentRecoveryAction:
    """Create a recovery action for ``agent`` targeting ``issues``.

    Raises ValidationError for gated/unknown action types, apply mode (P4
    gate), or secret material in params — the model's clean() is the enforcer.
    Successfully created actions are stamped onto the target issues
    (recovery_action_id + recovery_policy) so the audit is visible from the
    issue side without mutating issue status (dry-run fixes nothing).
    """
    issues = list(issues or [])
    action = AgentRecoveryAction(
        agent=agent,
        daemon=daemon,
        issue_ids=[str(issue.id) for issue in issues],
        action_type=action_type,
        mode=mode,
        target_runtime_id=target_runtime_id or agent.agent_username,
        params=params or {},
        expires_at=timezone.now() + ttl,
        actor=actor,
        source=source,
    )
    action.save()
    if issues:
        AgentHealthIssue.objects.filter(pk__in=[issue.pk for issue in issues]).update(
            recovery_action_id=action.id,
            recovery_policy=f'{mode}:{action_type}',
        )
    return action


def build_recovery_action_frame(action: AgentRecoveryAction) -> dict:
    """Build the Hive→daemon `recovery_action` frame body (no seq — the
    consumer stamps its outbound seq at send time)."""
    return {
        'type': 'recovery_action',
        'action_id': str(action.id),
        'correlation_id': str(action.correlation_id),
        'action_type': action.action_type,
        'mode': action.mode,
        'target_runtime_id': action.target_runtime_id,
        'agent_username': action.agent.agent_username,
        'params': action.params or {},
        'expires_at': action.expires_at.isoformat() if action.expires_at else None,
        'issue_ids': list(action.issue_ids or []),
    }


async def send_recovery_action(action: AgentRecoveryAction, daemon) -> dict:
    """Send the action to ``daemon`` over its daemon-link group and mark SENT.

    The consumer for the live WebSocket joined group ``daemon-link-<daemon_id>``
    at register time; `daemon.recovery.action` events are delivered by its
    ``daemon_recovery_action`` handler. Async because callers already live in
    consumer/async contexts; sync callers wrap with async_to_sync.
    """
    from asgiref.sync import sync_to_async
    from channels.layers import get_channel_layer

    frame = await sync_to_async(build_recovery_action_frame)(action)
    channel_layer = get_channel_layer()
    await channel_layer.group_send(
        f'daemon-link-{daemon.daemon_id}',
        {'type': 'daemon.recovery.action', 'frame': frame},
    )
    await sync_to_async(action.mark_sent)(daemon)
    return frame


def process_recovery_result(frame: dict, daemon) -> AgentRecoveryAction | None:
    """Persist a daemon `recovery_result` frame onto its AgentRecoveryAction.

    Returns the updated action, or None when the frame is unmatchable/invalid
    (unknown action id, correlation mismatch, daemon mismatch, bad result
    state, already-resolved action). Secret-looking values in the daemon
    payload are redacted before persistence.
    """
    action_id = str(frame.get('action_id') or '').strip()
    if not action_id:
        logger.warning('recovery_result missing action_id from daemon %s', daemon.daemon_id)
        return None
    try:
        action = AgentRecoveryAction.objects.get(pk=uuid.UUID(action_id))
    except (AgentRecoveryAction.DoesNotExist, ValueError):
        logger.warning(
            'recovery_result for unknown action %s from daemon %s — dropping',
            action_id, daemon.daemon_id,
        )
        return None

    correlation = str(frame.get('correlation_id') or '').strip()
    if correlation and correlation != str(action.correlation_id):
        logger.warning(
            'recovery_result correlation mismatch for action %s (got %s) — dropping',
            action_id, correlation,
        )
        return None

    # The result must come from the daemon the action was sent to (when bound).
    if action.daemon_id is not None and action.daemon_id != daemon.pk:
        logger.warning(
            'recovery_result for action %s from wrong daemon %s — dropping',
            action_id, daemon.daemon_id,
        )
        return None

    result_state = str(frame.get('result') or '').strip()
    if result_state not in AgentRecoveryAction.DAEMON_RESULT_STATES:
        logger.warning(
            'recovery_result with invalid result state %r for action %s — dropping',
            result_state, action_id,
        )
        return None

    if action.status in AgentRecoveryAction.TERMINAL_STATUSES:
        logger.warning(
            'recovery_result for already-resolved action %s (status=%s) — dropping',
            action_id, action.status,
        )
        return None

    cleaned_result = redact_secret_material({
        key: frame.get(key)
        for key in ('result', 'reason', 'action_type', 'mode')
        if frame.get(key) is not None
    })
    before = redact_secret_material(frame.get('before_summary') or {})
    after = redact_secret_material(frame.get('after_summary') or {})

    try:
        action.apply_daemon_result(
            result_state,
            daemon_result=cleaned_result,
            before=before,
            after=after,
            reason=str(redact_secret_material(str(frame.get('reason') or ''))),
        )
    except ValidationError as exc:
        logger.warning(
            'recovery_result rejected for action %s: %s', action_id, exc,
        )
        return None
    if action.daemon_id is None:
        action.daemon = daemon
        action.save(update_fields=['daemon'])
    return action


def recovery_actions_to_prometheus(now=None) -> str:
    """Prometheus text exposition for HIVE-277 recovery-action liveness.

    Covers the P4 runbook alert signals: action volume by type/mode/status,
    stale pending/sent actions, and whether the auto-apply feature flag is on.
    """
    from django.conf import settings
    from django.db.models import Count

    now = now or timezone.now()
    rows = (
        AgentRecoveryAction.objects
        .values('action_type', 'mode', 'status')
        .annotate(count=Count('id'))
        .order_by('action_type', 'mode', 'status')
    )
    lines = [
        '# HELP hive_recovery_actions_total Recovery actions by type, mode, and status.',
        '# TYPE hive_recovery_actions_total gauge',
    ]
    for row in rows:
        lines.append(
            'hive_recovery_actions_total{'
            f'action_type="{row["action_type"]}",mode="{row["mode"]}",status="{row["status"]}"'
            f'}} {row["count"]}'
        )
    stale_count = AgentRecoveryAction.objects.filter(
        status__in=[AgentRecoveryAction.Status.PENDING, AgentRecoveryAction.Status.SENT],
        expires_at__lt=now,
    ).count()
    lines.extend([
        '# HELP hive_recovery_actions_stale_total Pending/sent recovery actions past expiry.',
        '# TYPE hive_recovery_actions_stale_total gauge',
        f'hive_recovery_actions_stale_total {stale_count}',
        '# HELP hive_recovery_secret_rejections_total Recovery payloads redacted or rejected for secret material.',
        '# TYPE hive_recovery_secret_rejections_total counter',
        f'hive_recovery_secret_rejections_total {recovery_secret_rejection_count()}',
        '# HELP hive_recovery_auto_apply_enabled Whether HIVE-277 P4 auto-apply is enabled.',
        '# TYPE hive_recovery_auto_apply_enabled gauge',
        f'hive_recovery_auto_apply_enabled {1 if getattr(settings, "HIVE_RECOVERY_AUTO_APPLY_ENABLED", False) else 0}',
    ])
    return '\n'.join(lines) + '\n'
