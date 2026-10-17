"""Converge missing org-agent Origin onboarding without changing existing roles."""

import re
import secrets

import requests


ORGANIZATION = 'shizuha-labs'
READER_TEAM = 'agents-readers'


class OriginIdentityError(ValueError):
    pass


def ensure_origin_identity(agent, identity, base_url, auth):
    username = str(agent.agent_username or '').strip().lower()
    if (not re.fullmatch(r'[a-z0-9][a-z0-9-]*', username)
            or not re.fullmatch(r'[a-z0-9][a-z0-9-]*', identity)
            or identity == 'shizuha'
            or not isinstance(agent.identity_user_id, int) or agent.identity_user_id <= 0
            or agent.status in {'deleted', 'blocked'}):
        raise OriginIdentityError('Origin onboarding requires an exact bound org-agent identity')
    expected_emails = {f'{username}@shizuha.com', f'{identity}@shizuha.com'}

    def request(method, path, allowed, **kwargs):
        response = getattr(requests, method)(
            f'{base_url.rstrip("/")}/api/v1{path}', auth=auth, timeout=10,
            allow_redirects=False, **kwargs,
        )
        if response.status_code not in allowed:
            raise OriginIdentityError(f'Origin {method.upper()} {path} returned HTTP {response.status_code}')
        return response

    def validate_user(payload, *, require_email=False):
        if (not isinstance(payload, dict) or payload.get('login') != identity
                or payload.get('is_admin') is not False or payload.get('active') is not True
                or payload.get('prohibit_login') is not False
                or not isinstance(payload.get('id'), int) or payload['id'] <= 0):
            raise OriginIdentityError('Origin account does not match the active nonadmin agent identity')
        if require_email and str(payload.get('email') or '').lower() not in expected_emails:
            raise OriginIdentityError('Origin account email does not match the agent onboarding identity')
        return payload

    def validate_team(payload):
        units = payload.get('units_map') if isinstance(payload, dict) else None
        if (not isinstance(payload, dict) or payload.get('name') != READER_TEAM
                or payload.get('permission') != 'read'
                or payload.get('can_create_org_repo') is not False
                or not isinstance(payload.get('id'), int) or payload['id'] <= 0
                or not isinstance(units, dict) or units.get('repo.code') != 'read'
                or any(permission not in {'none', 'read'} for permission in units.values())):
            raise OriginIdentityError('Origin agents-readers team is not a least-privilege read contract')
        return payload

    user_path = f'/users/{identity}'
    response = request('get', user_path, {200, 404})
    user = validate_user(response.json()) if response.status_code == 200 else None
    member_path = f'/orgs/{ORGANIZATION}/members/{identity}'
    if user is not None and request('get', member_path, {204, 404}).status_code == 204:
        return True
    # Operator 2026-09-23: owner-scoped (personal) agents get the SAME
    # least-privilege onboarding as system agents — restricted account +
    # agents-readers team — so shizuha-115/741-class seats can mint Origin
    # tokens. The old blanket ban left personal agents running tokenless.
    if user is not None:
        validate_user(user, require_email=True)

    reader_team = None
    for page in range(1, 21):
        teams = request('get', f'/orgs/{ORGANIZATION}/teams', {200},
                        params={'limit': 50, 'page': page}).json()
        if not isinstance(teams, list) or any(not isinstance(team, dict) for team in teams):
            raise OriginIdentityError('Origin team inventory is not a list of teams')
        matches = [team for team in teams if team.get('name') == READER_TEAM]
        if matches:
            if len(matches) != 1:
                raise OriginIdentityError('Origin agents-readers team is ambiguous')
            reader_team = validate_team(matches[0])
            break
        if len(teams) < 50:
            break
    if reader_team is None:
        raise OriginIdentityError('Origin agents-readers team is absent from the bounded inventory')

    if user is None:
        created = request('post', '/admin/users', {201, 409, 422}, json={
            'username': identity, 'email': f'{username}@shizuha.com',
            'full_name': str(agent.display_name or username),
            'password': 'Aa1!' + secrets.token_urlsafe(48),
            'must_change_password': False, 'restricted': True,
            'send_notify': False, 'visibility': 'private',
        })
        user = validate_user(request('get', user_path, {200}).json(), require_email=True)
        if created.status_code == 201:
            created_user = validate_user(created.json(), require_email=True)
            if (created_user['id'] != user['id'] or user.get('restricted') is not True
                    or user.get('visibility') != 'private'):
                raise OriginIdentityError('Origin account creation readback does not match the restricted identity')
        elif request('get', member_path, {204, 404}).status_code == 204:
            return True

    team_id = reader_team['id']
    current_team = validate_team(request('get', f'/teams/{team_id}', {200}).json())
    if current_team['id'] != team_id:
        raise OriginIdentityError('Origin reader team changed identity before membership write')
    team_member_path = f'/teams/{team_id}/members/{identity}'
    response = request('get', team_member_path, {200, 404})
    if response.status_code == 404:
        request('put', team_member_path, {204})
        response = request('get', team_member_path, {200})
    member = validate_user(response.json(), require_email=True)
    if member['id'] != user['id']:
        raise OriginIdentityError('Origin membership readback belongs to another user')
    validate_team(request('get', f'/teams/{team_id}', {200}).json())
    request('get', member_path, {204})
    return True
