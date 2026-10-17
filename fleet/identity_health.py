"""Cross-service identity/routing health annotations for Hive fleet surfaces."""
from __future__ import annotations

from typing import Any


def _norm(value: Any) -> str:
    return str(value or '').strip().lower()


def _id_value(value: Any) -> str:
    return str(value or '').strip()


def _record_user_id(record: dict[str, Any]) -> str:
    return _id_value(record.get('user_id') or record.get('id') or record.get('pk'))


def _record_email(record: dict[str, Any]) -> str:
    return _norm(record.get('email') or record.get('canonical_email') or record.get('user_email'))


def _record_username(record: dict[str, Any]) -> str:
    return _norm(record.get('username') or record.get('agent_username'))


def _index_id_records(records: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    by_id: dict[str, dict[str, Any]] = {}
    by_email: dict[str, dict[str, Any]] = {}
    by_username: dict[str, dict[str, Any]] = {}
    for rec in records or []:
        uid = _record_user_id(rec)
        email = _record_email(rec)
        username = _record_username(rec)
        if uid:
            by_id[uid] = rec
        if email:
            by_email[email] = rec
        if username:
            by_username[username] = rec
    return {'id': by_id, 'email': by_email, 'username': by_username}


def _issue(*, service: str, issue_type: str, field: str, expected: Any, actual: Any,
           message: str, cleanup_action: dict[str, Any] | None = None) -> dict[str, Any]:
    issue = {
        'service': service,
        'issue_type': issue_type,
        'field': field,
        'expected': expected,
        'actual': actual,
        'message': message,
    }
    if cleanup_action:
        issue['cleanup_action'] = cleanup_action
    return issue


def _ok(warnings: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        'ok': not warnings,
        'warnings': warnings,
        'blocking_services': sorted({w['service'] for w in warnings}),
    }


def _agent_username(agent: dict[str, Any]) -> str:
    return _norm(agent.get('agent_username') or agent.get('username'))


def _agent_email(agent: dict[str, Any]) -> str:
    email = _norm(agent.get('email') or agent.get('runtime_email'))
    if email:
        return email
    username = _agent_username(agent)
    return f'{username}@shizuha.com' if username else ''


def _agent_platform_user_id(agent: dict[str, Any]) -> str:
    return _id_value(
        agent.get('platform_user_id')
        or agent.get('platformUserId')
        or agent.get('runtime_platform_user_id')
        or agent.get('runtimePlatformUserId')
    )


def _is_never_provisioned_stopped_agent(agent: dict[str, Any]) -> bool:
    """Conservative HIVE-599 cleanup classification for a real FleetAgent row."""
    username = _agent_username(agent)
    test_marker = (
        username.startswith(('qa-', 'test-'))
        or username.endswith(('-test', '_test', 'test'))
        or '-test-' in username
    )
    return bool(
        agent.get('id')
        and agent.get('lifecycle_managed') is not False
        and _norm(agent.get('status')) == 'stopped'
        and (not agent.get('provisioned_at') or test_marker)
    )


def _resolve_id_record(agent: dict[str, Any], index: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any] | None:
    platform_id = _agent_platform_user_id(agent)
    if platform_id and platform_id in index['id']:
        return index['id'][platform_id]
    email = _agent_email(agent)
    if email and email in index['email']:
        return index['email'][email]
    username = _agent_username(agent)
    if username and username in index['username']:
        return index['username'][username]
    return None


def identity_health_for_agent(agent: dict[str, Any], id_records: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate runtime identity against Shizuha-ID agent account records."""
    index = _index_id_records(id_records)
    platform_id = _agent_platform_user_id(agent)
    runtime_email = _agent_email(agent)
    username = _agent_username(agent)
    warnings: list[dict[str, Any]] = []
    record = _resolve_id_record(agent, index)

    if not record:
        # An ARCHIVED agent is a DELIBERATE retirement (hive is the source of
        # truth — archive declares 'this should not run'). It is NOT an orphan
        # that needs manual cleanup; flagging it 'Orphan — needs cleanup'
        # contradicts the archive decision and nags forever. Report a quiet,
        # terminal 'archived' state and let the reconciler reap the tombstone
        # row (never-provisioned + no identity + no runtime = nothing to keep).
        if agent.get('archived'):
            health = _ok([])
            health['archived_retired'] = True
            health['reap_candidate'] = _is_never_provisioned_stopped_agent(agent)
            return health
        if _is_never_provisioned_stopped_agent(agent):
            agent_id = _id_value(agent.get('id'))
            warnings.append(_issue(
                service='hive',
                issue_type='identity_orphan_cleanup',
                field='hive.FleetAgent.identity',
                expected='automatic retirement after the unprovisioned-row safety grace period',
                actual={'agent_id': agent_id, 'username': username, 'status': 'stopped'},
                message='No Shizuha-ID user exists for this stopped, never-provisioned agent row; retirement is pending.',
                cleanup_action={
                    'kind': 'delete_agent',
                    'label': 'Delete orphan agent',
                    'method': 'DELETE',
                    'url': f'/v1/fleet/agents/{agent_id}/',
                    'staff_only': True,
                    'confirmation': username,
                },
            ))
            health = _ok(warnings)
            health['orphan_cleanup_required'] = True
            return health
        warnings.append(_issue(
            service='id',
            issue_type='identity_unresolved',
            field='runtime.identity',
            expected='active account_type=agent Shizuha-ID user',
            actual={'platform_user_id': platform_id, 'email': runtime_email, 'username': username},
            message='Runtime identity does not resolve to an active Shizuha-ID agent user.',
        ))
        return _ok(warnings)

    canonical_id = _record_user_id(record)
    canonical_email = _record_email(record)
    canonical_username = _record_username(record)
    account_type = record.get('account_type') or record.get('type') or 'agent'
    active = record.get('is_active', record.get('active', True))

    if platform_id and canonical_id and platform_id != canonical_id:
        warnings.append(_issue(
            service='id',
            issue_type='platform_user_mismatch',
            field='runtime.platformUserId',
            expected=canonical_id,
            actual=platform_id,
            message='Runtime platform user id points at a different Shizuha-ID user.',
        ))
    if _norm(account_type) != 'agent':
        warnings.append(_issue(
            service='id',
            issue_type='inactive_or_wrong_account_type',
            field='id.account_type',
            expected='agent',
            actual=account_type,
            message='Resolved Shizuha-ID user is not an agent account.',
        ))
    if active is False:
        warnings.append(_issue(
            service='id',
            issue_type='inactive_or_wrong_account_type',
            field='id.is_active',
            expected=True,
            actual=False,
            message='Resolved Shizuha-ID agent user is inactive.',
        ))

    heartbeat_email = _norm(agent.get('last_heartbeat_identity_email') or agent.get('heartbeat_identity_email'))
    if heartbeat_email and heartbeat_email not in {runtime_email, canonical_email}:
        warnings.append(_issue(
            service='runtime',
            issue_type='heartbeat_identity_mismatch',
            field='runtime.last_heartbeat_identity_email',
            expected=canonical_email or runtime_email,
            actual=heartbeat_email,
            message='Last heartbeat identity email does not match runtime/canonical identity.',
        ))

    health = _ok(warnings)
    health.update({
        'canonical_user_id': canonical_id,
        'canonical_email': canonical_email,
        'canonical_username': canonical_username,
        'runtime_email': runtime_email,
        'runtime_username': username,
    })
    return health


def _membership_assignable(membership: dict[str, Any]) -> bool | None:
    if not isinstance(membership, dict):
        return None
    routing = membership.get('routing_health')
    if isinstance(routing, dict) and routing.get('ok') is False:
        return False
    for key in ('assignable', 'assignment_resolved', 'resolver_ok', 'pulse_assignable'):
        if key in membership:
            return bool(membership.get(key))
    return None


def routing_health_for_agent(agent: dict[str, Any]) -> dict[str, Any]:
    warnings: list[dict[str, Any]] = []
    for membership in agent.get('team_memberships') or []:
        assignable = _membership_assignable(membership)
        if assignable is not False:
            continue
        email = membership.get('email') or _agent_email(agent)
        team = membership.get('team_name') or membership.get('team_slug') or membership.get('team_id')
        reason = membership.get('routing_error') or membership.get('reason') or 'Pulse assignment resolver rejected this roster member.'
        warnings.append(_issue(
            service=membership.get('blocking_service') or 'pulse',
            issue_type='roster_unassignable',
            field='pulse.TeamRoster.member',
            expected='assignable roster member',
            actual={'team': team, 'email': email},
            message=reason,
        ))
    return _ok(warnings)


def summarize_agent_health(identity: dict[str, Any], routing: dict[str, Any]) -> dict[str, Any]:
    warnings = [*(identity.get('warnings') or []), *(routing.get('warnings') or [])]
    return {
        'ok': not warnings,
        'warning_count': len(warnings),
        'warnings': warnings,
        'blocking_services': sorted({w['service'] for w in warnings}),
        'issue_types': sorted({w['issue_type'] for w in warnings}),
    }


def annotate_agents(agents: list[dict[str, Any]], id_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for agent in agents:
        if agent.get('kind') == 'claw' or agent.get('runtime_source') == 'claw':
            identity = {
                'ok': True,
                'warnings': [],
                'blocking_services': [],
                'not_applicable': True,
                'reason': 'Claw/Hermes account agents are owner-scoped and do not use a per-agent Shizuha-ID account.',
            }
        else:
            identity = identity_health_for_agent(agent, id_records)
        routing = routing_health_for_agent(agent)
        agent['identity_health'] = identity
        agent['routing_health'] = routing
        agent['health_summary'] = summarize_agent_health(identity, routing)
    return agents


def routing_health_for_membership(member: dict[str, Any]) -> dict[str, Any]:
    assignable = _membership_assignable(member)
    if assignable is False:
        reason = member.get('routing_error') or member.get('reason') or 'Pulse assignment resolver rejected this roster member.'
        return _ok([_issue(
            service=member.get('blocking_service') or 'pulse',
            issue_type='roster_unassignable',
            field='pulse.TeamRoster.member',
            expected='assignable roster member',
            actual=member.get('email') or member.get('user_email') or member.get('username'),
            message=reason,
        )])
    return _ok([])
