"""HIVE-296 P1 — classify health_snapshot frames into normalized AgentHealthIssue rows.

Classifiers are pure functions (dict → list[dict]); DB persistence lives in
consumers.py (_persist_health_snapshot).  This keeps classifiers unit-testable
without Django setup.
"""
from __future__ import annotations

from typing import Any

# Severity / service defaults per issue type (HIVE-277 §3.3)
_SEVERITY: dict[str, str] = {
    'identity_alias_drift': 'medium',
    'identity_unresolved': 'high',
    'platform_user_mismatch': 'high',
    'inactive_or_wrong_account_type': 'high',
    'credential_grant_missing': 'high',
    'provider_unavailable_or_quota': 'medium',
    'roster_unassignable': 'high',
    'heartbeat_identity_mismatch': 'medium',
    'desired_generation_unseen': 'medium',
    'applied_generation_stale': 'medium',
    'observed_config_drift': 'medium',
    'capability_grant_missing': 'medium',
    'unexpected_capability_grant': 'low',
    'mcp_access_drift': 'low',
    'skill_profile_drift': 'low',
}

_SERVICE: dict[str, str] = {
    'identity_alias_drift': 'id',
    'identity_unresolved': 'id',
    'platform_user_mismatch': 'id',
    'inactive_or_wrong_account_type': 'id',
    'credential_grant_missing': 'fleet',
    'provider_unavailable_or_quota': 'provider',
    'roster_unassignable': 'pulse',
    'heartbeat_identity_mismatch': 'runtime',
    'desired_generation_unseen': 'fleet',
    'applied_generation_stale': 'fleet',
    'observed_config_drift': 'fleet',
    'capability_grant_missing': 'fleet',
    'unexpected_capability_grant': 'fleet',
    'mcp_access_drift': 'fleet',
    'skill_profile_drift': 'fleet',
}


def _norm(value: Any) -> str:
    return str(value or '').strip().lower()


def _issue(
    issue_type: str,
    field: str,
    expected: Any,
    actual: Any,
    detail: str,
) -> dict[str, Any]:
    return {
        'issue_type': issue_type,
        'severity': _SEVERITY.get(issue_type, 'medium'),
        'service': _SERVICE.get(issue_type, 'unknown'),
        'field_path': field,
        'expected': expected,
        'actual': actual,
        'detail': detail,
    }


def classify_snapshot(
    snapshot: dict[str, Any],
    desired_state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return a list of issue dicts from one health_snapshot frame.

    ``desired_state`` is an optional dict with fields from AgentRuntimeDesiredState
    (desired_generation, desired_config_hash, effective_capabilities, …).
    Generation and capability drift checks are skipped when it is None.
    """
    issues: list[dict[str, Any]] = []
    _check_identity(snapshot, issues)
    _check_generation_drift(snapshot, desired_state, issues)
    _check_credential_grants(snapshot, issues)
    _check_provider(snapshot, issues)
    _check_routing(snapshot, issues)
    _check_capability_drift(snapshot, desired_state, issues)
    return issues


# ── individual checkers ────────────────────────────────────────────────────────

def _check_identity(snap: dict[str, Any], issues: list[dict[str, Any]]) -> None:
    runtime_email = _norm(snap.get('runtime_email'))
    canonical_email = _norm(snap.get('canonical_email'))
    platform_user_id = str(snap.get('platform_user_id') or '').strip()
    account_type = _norm(snap.get('account_type'))
    account_active = snap.get('account_active')
    alias_evidence = snap.get('alias_resolution_evidence') or {}

    if not canonical_email and not platform_user_id:
        issues.append(_issue(
            'identity_unresolved',
            'runtime.identity',
            'active account_type=agent Shizuha-ID user',
            {'runtime_email': runtime_email, 'platform_user_id': platform_user_id},
            'Runtime identity does not resolve to an active Shizuha-ID agent user.',
        ))
        return

    # Alias drift: runtime differs from canonical but alias is unproven
    if runtime_email and canonical_email and runtime_email != canonical_email:
        if not alias_evidence.get('same_platform_user'):
            issues.append(_issue(
                'identity_alias_drift',
                'runtime.email',
                canonical_email,
                runtime_email,
                f'Runtime email {runtime_email!r} differs from canonical {canonical_email!r} '
                'and alias is unresolved.',
            ))

    if account_type and account_type != 'agent':
        issues.append(_issue(
            'inactive_or_wrong_account_type',
            'id.account_type',
            'agent',
            account_type,
            'Resolved Shizuha-ID user is not an agent account.',
        ))

    if account_active is False:
        issues.append(_issue(
            'inactive_or_wrong_account_type',
            'id.is_active',
            True,
            False,
            'Resolved Shizuha-ID agent user is inactive.',
        ))

    heartbeat_email = _norm(snap.get('last_heartbeat_identity_email'))
    if heartbeat_email and heartbeat_email not in {runtime_email, canonical_email}:
        issues.append(_issue(
            'heartbeat_identity_mismatch',
            'runtime.last_heartbeat_identity_email',
            canonical_email or runtime_email,
            heartbeat_email,
            'Last heartbeat identity email does not match runtime/canonical identity.',
        ))


def _check_generation_drift(
    snap: dict[str, Any],
    desired: dict[str, Any] | None,
    issues: list[dict[str, Any]],
) -> None:
    if desired is None:
        return

    target_gen = desired.get('desired_generation')
    target_hash = str(desired.get('desired_config_hash') or '').strip()
    desired_gen_seen = snap.get('desired_generation_seen')
    applied_gen = snap.get('applied_generation')
    observed_hash = str(snap.get('observed_config_hash') or '').strip()

    if target_gen is not None and desired_gen_seen is not None:
        if int(desired_gen_seen) < int(target_gen):
            issues.append(_issue(
                'desired_generation_unseen',
                'runtime.desired_generation_seen',
                target_gen,
                desired_gen_seen,
                f'Daemon has not seen desired generation {target_gen}; last seen {desired_gen_seen}.',
            ))

    if target_gen is not None and applied_gen is not None:
        if int(applied_gen) < int(target_gen):
            issues.append(_issue(
                'applied_generation_stale',
                'runtime.applied_generation',
                target_gen,
                applied_gen,
                f'Daemon applied generation {applied_gen} is behind desired {target_gen}.',
            ))

    if target_hash and observed_hash and observed_hash != target_hash:
        if applied_gen is not None and int(applied_gen) == int(target_gen or 0):
            issues.append(_issue(
                'observed_config_drift',
                'runtime.observed_config_hash',
                target_hash,
                observed_hash,
                'Observed runtime config hash does not match desired config hash.',
            ))


def _check_credential_grants(
    snap: dict[str, Any],
    issues: list[dict[str, Any]],
) -> None:
    for missing in snap.get('missing_grants') or []:
        issues.append(_issue(
            'credential_grant_missing',
            'runtime.credential_grants',
            f'grant:{missing}',
            None,
            f'Required credential grant {missing!r} is missing from the runtime.',
        ))


def _check_provider(snap: dict[str, Any], issues: list[dict[str, Any]]) -> None:
    ph = snap.get('provider_health')
    if not isinstance(ph, dict):
        return
    unavailable = ph.get('available') is False
    quota_fail = ph.get('quota_ok') is False
    in_backoff = bool(ph.get('in_backoff'))
    if unavailable or quota_fail or in_backoff:
        issues.append(_issue(
            'provider_unavailable_or_quota',
            'provider.health',
            {'available': True, 'quota_ok': True, 'in_backoff': False},
            ph,
            'Provider is unavailable, quota exhausted, or in backoff.',
        ))


def _check_routing(snap: dict[str, Any], issues: list[dict[str, Any]]) -> None:
    if snap.get('pulse_assignable') is False:
        reason = snap.get('roster_assignable_reason') or 'Pulse assignment resolver rejected this agent.'
        issues.append(_issue(
            'roster_unassignable',
            'pulse.TeamRoster.member',
            'assignable',
            reason,
            reason,
        ))


def _check_capability_drift(
    snap: dict[str, Any],
    desired: dict[str, Any] | None,
    issues: list[dict[str, Any]],
) -> None:
    if desired is None:
        return
    desired_caps = set(desired.get('effective_capabilities') or [])
    observed_caps = set(snap.get('effective_capabilities') or [])
    for cap in sorted(desired_caps - observed_caps):
        issues.append(_issue(
            'capability_grant_missing',
            'runtime.effective_capabilities',
            f'capability:{cap}',
            None,
            f'Expected capability {cap!r} is not in the runtime effective capabilities.',
        ))
    for cap in sorted(observed_caps - desired_caps):
        issues.append(_issue(
            'unexpected_capability_grant',
            'runtime.effective_capabilities',
            None,
            f'capability:{cap}',
            f'Runtime has capability {cap!r} not expected by desired state.',
        ))
