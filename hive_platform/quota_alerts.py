"""HIVE-2804 Slice A: edge-triggered quota alerts on the existing refresh path.

Idempotency is the partial unique constraint on (org_id, entry_id, condition)
while state=open. Parallel refreshes cannot insert a second open row.
"""

from __future__ import annotations

import logging

from hive_platform.quota_alert_conditions import is_fetch_error_detail
from django.db import IntegrityError, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

# RFC deviation (HIVE-2804 condition 3): the bounded quota snapshot has no
# attempt counter, so fetch_error fires on a projected failure code
# (request_failed, http_5xx, quota_refresh_failed, timeout) rather than after
# N attempts. A counter belongs on the snapshot schema, not an implicit gate.

LOW_QUOTA_PERCENT = 15
CONDITIONS = (
    'low_quota',
    'stale_snapshot',
    'fetch_error',
    'revoked_token',
    'unexpected_cooldown',
    'token_expiry',
)


def canonical_org_id(raw) -> str:
    """Collapse org-id aliases so Org-A and org-a share one alert key."""
    return str(raw or 'org').strip().lower()


def org_id_for_entry(entry) -> str:
    pool = getattr(entry, 'pool', None)
    if pool is None:
        return 'unknown'
    if getattr(pool, 'owner_kind', '') == 'org':
        return canonical_org_id(pool.owner_id)
    return f"user:{canonical_org_id(pool.owner_id)}"


def active_conditions(entry, snapshot: dict | None) -> set[str]:
    """Enum-only condition set. Never reads token ciphertext."""
    snap = snapshot or {}
    found: set[str] = set()
    detail = str(snap.get('detail') or '')
    if detail in {'auth_failed', 'revoked', 'invalid_grant'}:
        found.add('revoked_token')
    if is_fetch_error_detail(detail):
        found.add('fetch_error')
    windows = snap.get('windows') if isinstance(snap.get('windows'), list) else []
    lefts = []
    for window in windows:
        if not isinstance(window, dict):
            continue
        left = window.get('left_percent')
        if left is None and window.get('used_percent') is not None:
            try:
                left = 100 - float(window['used_percent'])
            except (TypeError, ValueError):
                left = None
        if left is not None:
            try:
                lefts.append(float(left))
            except (TypeError, ValueError):
                pass
    if lefts and min(lefts) <= LOW_QUOTA_PERCENT:
        found.add('low_quota')
    if getattr(entry, 'last_status', '') == 'auth_failed':
        found.add('revoked_token')
    checked = getattr(entry, 'last_checked_at', None)
    if checked is not None:
        age = timezone.now() - checked
        if age.total_seconds() > 6 * 3600:
            found.add('stale_snapshot')
    reason = str(getattr(entry, 'cooldown_reason', '') or '')
    cooldown = getattr(entry, 'cooldown_until', None)
    if cooldown is not None and cooldown > timezone.now() and reason not in {'quota_exhausted', 'rate_limited', ''}:
        found.add('unexpected_cooldown')
    # Expiry is a snapshot field only. Never read token ciphertext.
    expires = snap.get('access_token_expires_at')
    if isinstance(expires, str) and expires:
        from datetime import datetime
        try:
            exp = datetime.fromisoformat(expires.replace('Z', '+00:00'))
            if timezone.is_naive(exp):
                exp = timezone.make_aware(exp, timezone.get_current_timezone())
            delta = exp - timezone.now()
            if delta.total_seconds() <= 24 * 3600:
                found.add('token_expiry')
        except ValueError:
            pass
    return found


def sync_quota_alerts(entry, snapshot: dict | None) -> list[str]:
    """Open or resolve alerts for this entry. Returns conditions that opened."""
    from hive_platform.models import ProviderQuotaAlert

    org_id = org_id_for_entry(entry)
    wanted = active_conditions(entry, snapshot)
    opened: list[str] = []
    now = timezone.now()
    version = str((snapshot or {}).get('fetched_at') or '')[:64]
    with transaction.atomic():
        existing = {
            row.condition: row
            for row in ProviderQuotaAlert.objects.select_for_update().filter(
                org_id=org_id, entry=entry, state=ProviderQuotaAlert.State.OPEN,
            )
        }
        for condition in CONDITIONS:
            row = existing.get(condition)
            if condition in wanted:
                if row is None:
                    try:
                        row = ProviderQuotaAlert.objects.create(
                            org_id=org_id,
                            entry=entry,
                            condition=condition,
                            snapshot_version=version,
                            diagnostic=condition,
                            actor='hive-quota-refresh',
                            service='hive',
                            last_transition='opened',
                        )
                        opened.append(condition)
                        _file_pulse_once(row)
                    except IntegrityError:
                        # Concurrent refresh won the unique open row.
                        pass
                else:
                    row.snapshot_version = version
                    row.save(update_fields=['snapshot_version', 'updated_at'])
                    if not row.pulse_item_key:
                        _file_pulse_once(row)
            elif row is not None:
                row.state = ProviderQuotaAlert.State.RESOLVED
                row.resolved_at = now
                row.actor = 'hive-quota-refresh'
                row.service = 'hive'
                row.last_transition = 'resolved'
                row.save(update_fields=[
                    'state', 'resolved_at', 'actor', 'service',
                    'last_transition', 'updated_at',
                ])
                _comment_pulse_cleared(row)
    return opened


def _file_pulse_once(row) -> None:
    """Create one Pulse task for a newly opened row. Failure leaves the row.

    The unique open constraint is the idempotency key. A later refresh sees
    the open row and does not create again. Payload is the condition enum only.
    """
    if row.pulse_item_key:
        return
    try:
        from django.conf import settings
        import requests
        base = getattr(settings, 'HIVE_PULSE_API_URL', '').rstrip('/')
        if not base:
            return
        token = getattr(settings, 'HIVE_PULSE_API_TOKEN', '')
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        payload = {
            'title': f'Provider quota {row.condition}',
            'description': (
                f'org_id={row.org_id} entry_id={row.entry_id} '
                f'condition={row.condition} snapshot={row.snapshot_version}'
            ),
            'priority': 'high',
            'labels': ['quota-alert', row.condition],
        }
        resp = requests.post(f'{base}/tasks/', json=payload, headers=headers, timeout=8)
        if resp.status_code in (200, 201):
            body = resp.json() or {}
            key = body.get('item_key') or (body.get('item') or {}).get('item_key') or ''
            if key:
                row.pulse_item_key = str(key)[:64]
                row.last_transition = 'filed'
                row.actor = row.actor or 'hive-quota-refresh'
                row.service = row.service or 'hive'
                row.save(update_fields=[
                    'pulse_item_key', 'last_transition', 'actor', 'service', 'updated_at',
                ])
        else:
            logger.warning(
                'quota alert pulse file failed condition=%s entry_id=%s status=%s',
                row.condition, row.entry_id, resp.status_code,
            )
    except Exception:
        logger.warning(
            'quota alert pulse file failed condition=%s entry_id=%s',
            row.condition, row.entry_id, exc_info=True,
        )
        return


def _comment_pulse_cleared(row) -> None:
    """Comment on the carrier when the condition clears. No new task."""
    if not row.pulse_item_key:
        return
    try:
        from django.conf import settings
        import requests
        base = getattr(settings, 'HIVE_PULSE_API_URL', '').rstrip('/')
        if not base:
            return
        token = getattr(settings, 'HIVE_PULSE_API_TOKEN', '')
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        requests.post(
            f'{base}/tasks/{row.pulse_item_key}/comments/',
            json={'content': f'condition {row.condition} cleared'},
            headers=headers,
            timeout=8,
        )
    except Exception:
        logger.warning(
            'quota alert pulse clear comment failed condition=%s entry_id=%s',
            row.condition, row.entry_id, exc_info=True,
        )
        return
