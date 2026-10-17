"""Renewable, read-only Pulse credentials for Hive's lifecycle inputs.

Never reuse an agent's access token: its expiry previously stopped the entire
on-demand reconciler. ID issues short-lived service tokens; Pulse grants this
identity only the roster and workload snapshot reads required by Hive.

Reader changes must exercise command parser creation and the provisioning
caller; testing only fetch methods misses startup imports and credential grants.
"""
from __future__ import annotations

import threading
import time

import requests
from django.conf import settings
from django.core.management.base import CommandError


SCOPE = 'pulse:fleet-snapshot.read'
CLIENT_ID = 'hive-pulse-fleet-read'
_READ_PATHS = {'/admin/team-roster/', '/admin/org-chart/snapshot/'}
_TOKEN_LOCK = threading.Lock()
_TOKEN_CACHE = {'key': None, 'token': '', 'expires_at': 0.0}


def pulse_fleet_headers(*, force=False):
    """Exchange client credentials, caching strictly inside ID's token TTL."""
    client_id = getattr(settings, 'HIVE_PULSE_OAUTH_CLIENT_ID', '')
    secret = getattr(settings, 'HIVE_PULSE_OAUTH_CLIENT_SECRET', '')
    url = getattr(settings, 'HIVE_PULSE_OAUTH_TOKEN_URL', '')
    if client_id != CLIENT_ID or not secret or not url:
        raise CommandError('Hive Pulse fleet-read OAuth client is not configured.')
    key = (url, client_id, secret)
    with _TOKEN_LOCK:
        if (not force and _TOKEN_CACHE['key'] == key
                and _TOKEN_CACHE['expires_at'] > time.monotonic()):
            return {'Authorization': f"Bearer {_TOKEN_CACHE['token']}"}
        # A failed refresh must not leave the rejected credential reusable.
        _TOKEN_CACHE.update(key=None, token='', expires_at=0.0)
        started = time.monotonic()
        try:
            response = requests.post(url, data={
                'grant_type': 'client_credentials',
                'client_id': client_id,
                'client_secret': secret,
                'scope': SCOPE,
            }, timeout=10, allow_redirects=False)
        except requests.RequestException:
            raise CommandError('Hive Pulse fleet-read OAuth exchange unavailable.') from None
        if response.status_code != 200:
            raise CommandError(
                f'Hive Pulse fleet-read OAuth exchange failed: HTTP {response.status_code}'
            )
        try:
            payload = response.json()
            token = payload['access_token']
            ttl = int(payload['expires_in'])
            token_type = payload['token_type']
            scope = payload['scope']
        except (ValueError, TypeError, KeyError):
            raise CommandError('Hive Pulse fleet-read OAuth exchange returned invalid credentials.') from None
        if (not isinstance(token, str) or not token.strip()
                or str(token_type).lower() != 'bearer' or scope != SCOPE or ttl <= 30):
            raise CommandError('Hive Pulse fleet-read OAuth exchange returned invalid credentials.')
        _TOKEN_CACHE.update(
            key=key, token=token,
            expires_at=started + min(ttl, 900) - 30,
        )
        return {'Authorization': f'Bearer {token}'}


def pulse_fleet_get(path, *, params=None, timeout=None, warning=None):
    """Read fresh lifecycle input, refreshing once on 401 and failing closed.

    Transient failures get three bounded attempts. Authentication failures never
    become an empty queue or fall back to a stale snapshot or a user's token.
    """
    if path.split('?', 1)[0] not in _READ_PATHS:
        raise CommandError('Pulse fleet-read client does not allow this endpoint.')
    base = getattr(settings, 'HIVE_PULSE_API_URL', '').rstrip('/')
    if not base:
        raise CommandError('HIVE_PULSE_API_URL is not configured.')
    timeout = timeout or (30 if 'org-chart/snapshot' in path else 15)
    headers = pulse_fleet_headers()
    refreshed = False
    attempt = 1
    while True:
        try:
            response = requests.get(
                f'{base}{path}', headers=headers, params=params,
                timeout=timeout, allow_redirects=False,
            )
        except requests.RequestException:
            if attempt >= 3:
                raise CommandError(f'Pulse request failed for {path} after 3 attempts.') from None
            response = None
        if response is not None:
            if response.status_code == 401:
                with _TOKEN_LOCK:
                    if headers.get('Authorization') == f"Bearer {_TOKEN_CACHE['token']}":
                        _TOKEN_CACHE.update(key=None, token='', expires_at=0.0)
                if not refreshed:
                    headers = pulse_fleet_headers(force=True)
                    refreshed = True
                    continue
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError:
                    raise CommandError(f'Pulse returned non-JSON for {path}') from None
            if response.status_code not in {502, 503, 504} or attempt >= 3:
                raise CommandError(f'Pulse request failed for {path}: HTTP {response.status_code}')
        if warning:
            warning(f'Pulse request transient failure for {path} (attempt {attempt}/3); retrying')
        time.sleep(attempt)
        attempt += 1


def pulse_fleet_rosters(*, timeout=10):
    """Read normalized roster rows for lifecycle and credential consumers."""
    payload = pulse_fleet_get('/admin/team-roster/', timeout=timeout)
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ('teams', 'rosters', 'results', 'items'):
            if isinstance(payload.get(key), list):
                return [row for row in payload[key] if isinstance(row, dict)]
    raise CommandError('Pulse returned an invalid fleet roster payload.')
