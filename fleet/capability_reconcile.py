"""Hive-owned effective-capability → desired runtime config reconciliation."""
from __future__ import annotations

from collections.abc import Iterable

from fleet.effective_capabilities import compute_effective_capability_grant_for_memberships


def _canon_team(value):
    return str(value or '').strip().lower().replace('_', '-').replace(' ', '-')


def _member_email(member):
    if isinstance(member, dict):
        value = (
            member.get('email')
            or member.get('user_email')
            or member.get('agent_email')
            or member.get('username')
            or ''
        )
    else:
        value = member
    value = str(value or '').strip().lower()
    return value if '@' in value else ''


def _agent_email(agent):
    email = str((agent or {}).get('email') or '').strip().lower()
    if email:
        return email
    username = str(
        (agent or {}).get('agent_username')
        or (agent or {}).get('username')
        or ''
    ).strip().lower()
    return f'{username}@shizuha.com' if username else ''


def membership_index_from_rosters(rosters: Iterable[dict]) -> dict[str, list[dict]]:
    """Expand Pulse TeamRoster rows into email → org-qualified memberships."""
    index = {}
    for roster in rosters or []:
        if not isinstance(roster, dict):
            continue
        team_slug = _canon_team(
            roster.get('team_slug')
            or roster.get('team_name')
            or roster.get('slug')
            or roster.get('name')
        )
        if not team_slug:
            continue
        roster_org = str(
            roster.get('organization_slug')
            or roster.get('org_slug')
            or 'shizuha'
        ).strip().lower()
        members = []
        for tier in roster.get('tiers') or []:
            members.extend(tier or [])
        members.extend(roster.get('junior_emails') or [])
        seen = set()
        for member in members:
            email = _member_email(member)
            if not email or email in seen:
                continue
            seen.add(email)
            member_org = (
                str(
                    member.get('organization_slug')
                    or member.get('org_slug')
                    or roster_org
                ).strip().lower()
                if isinstance(member, dict)
                else roster_org
            )
            index.setdefault(email, []).append({
                'email': email,
                'team_slug': team_slug,
                'team_name': team_slug,
                'organization_slug': member_org,
                'membership_source': 'pulse_roster',
            })
    return index


def desired_capability_runtime_config(agent: dict, memberships: Iterable[dict]) -> dict:
    """Return the org-isolated capability overlay Hive should persist and push."""
    email = _agent_email(agent)
    identifiers = {
        str((agent or {}).get('id') or ''),
        str((agent or {}).get('agent_id') or ''),
        str((agent or {}).get('agent_username') or (agent or {}).get('username') or ''),
        email,
    }
    identifiers.discard('')
    grant = compute_effective_capability_grant_for_memberships(
        memberships,
        identifiers,
    )
    return {
        'skills': grant['skills'],
        'eager_skills': grant['eager_skills'],
        'enabled_mcp_server_ids': grant['enabled_mcp_servers'],
        'grant': grant,
    }


def plan_capability_reconcile(agents: Iterable[dict], rosters: Iterable[dict]) -> list[dict]:
    """Compare rostered agents with the effective org-qualified capability policy.

    Unrostered personal/general assistants can have explicit MCP config unrelated
    to team capabilities. The periodic repair pass must not replace that config
    with the base platform floor. Roster removal remains event-driven through
    ``_runtime_capability_reconcile_for_agent_emails``, which intentionally
    recomputes the just-removed agent down to its base grant.
    """
    membership_index = membership_index_from_rosters(rosters)
    plans = []
    for agent in agents or []:
        email = _agent_email(agent)
        memberships = membership_index.get(email, [])
        if not memberships:
            continue
        desired = desired_capability_runtime_config(agent, memberships)
        skill_config = (agent or {}).get('skill_config') or {}
        mcp_config = (agent or {}).get('mcp_config') or {}
        current = {
            'skills': sorted(set(
                (agent or {}).get('skills')
                or skill_config.get('skills')
                or []
            )),
            'eager_skills': sorted(set(
                (agent or {}).get('eager_skills')
                or skill_config.get('eager_skills')
                or []
            )),
            'enabled_mcp_server_ids': sorted(set(
                (agent or {}).get('enabled_mcp_server_ids')
                or mcp_config.get('enabled_mcp_server_ids')
                or []
            )),
        }
        updates = {
            key: desired[key]
            for key in ('skills', 'eager_skills', 'enabled_mcp_server_ids')
            if current[key] != desired[key]
        }
        plans.append({
            'agent_id': str((agent or {}).get('id') or (agent or {}).get('agent_id') or ''),
            'agent_username': str(
                (agent or {}).get('agent_username')
                or (agent or {}).get('username')
                or ''
            ),
            'email': email,
            'lifecycle_plane': str(
                (agent or {}).get('lifecycle_plane') or 'runtime'
            ).strip(),
            'organization_scopes': desired['grant']['organization_scopes'],
            'updates': updates,
            'desired': {
                key: desired[key]
                for key in ('skills', 'eager_skills', 'enabled_mcp_server_ids')
            },
        })
    return plans


def apply_capability_reconcile(plans: Iterable[dict], configure) -> dict:
    """Apply a reconcile plan through Hive's sole runtime-config actuator."""
    result = {'checked_agents': 0, 'drifted_agents': 0, 'updated_agents': 0, 'errors': []}
    for plan in plans or []:
        result['checked_agents'] += 1
        if not plan.get('updates'):
            continue
        result['drifted_agents'] += 1
        agent_id = plan.get('agent_id') or ''
        if not agent_id:
            result['errors'].append({
                'agent': plan.get('agent_username') or plan.get('email'),
                'error': 'runtime agent id is missing',
            })
            continue
        ok, detail = configure(agent_id, plan['updates'])
        if ok:
            result['updated_agents'] += 1
        else:
            result['errors'].append({
                'agent': agent_id,
                'email': plan.get('email'),
                'error': detail,
                'updates': sorted(plan['updates']),
            })
    return result
