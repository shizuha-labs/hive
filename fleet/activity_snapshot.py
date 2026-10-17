"""Persist native live-activity transcripts across hibernate/restart.

``_get_native_agent_activity`` stays a DB-free pod exec. This module stores the
last successful tail on the FleetAgent row's snapshot and merges it back into
the Agents drawer GET so history is never blank when Hive has seen the agent
work.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone as datetime_timezone

from django.db.models import CharField
from django.db.models.functions import Cast
from django.utils.dateparse import parse_datetime

from .models import FleetAgentActivitySnapshot

logger = logging.getLogger(__name__)

ACTIVITY_SNAPSHOT_LIMIT = 200


def _agent_pk(agent):
    if agent is None:
        return None
    pk = getattr(agent, 'pk', None)
    if pk is None:
        return None
    try:
        return uuid.UUID(str(pk))
    except (TypeError, ValueError, AttributeError):
        return None


def _event_key(event):
    if not isinstance(event, dict):
        return None
    event_id = event.get('id')
    ts = str(event.get('ts') or '')
    typ = str(event.get('type') or '')
    if event_id:
        return ('id', str(event_id), typ, ts)
    text = str(
        event.get('text') or event.get('input') or event.get('output') or '',
    )[:120]
    tool = str(event.get('tool') or '')
    return ('body', ts, typ, tool, text)


def _parse_ts(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=datetime_timezone.utc)
        return value
    text = str(value).replace('Z', '+00:00')
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        parsed = parse_datetime(str(value))
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime_timezone.utc)
    return parsed


def _iso(value):
    parsed = _parse_ts(value)
    if parsed is None:
        return None
    return parsed.astimezone(datetime_timezone.utc).isoformat().replace('+00:00', 'Z')


def _newest_ts(events):
    newest = None
    for event in events:
        parsed = _parse_ts((event or {}).get('ts'))
        if parsed is None:
            continue
        if newest is None or parsed > newest:
            newest = parsed
    return newest


def _sorted_unique(events, limit):
    seen = set()
    unique = []
    for event in events:
        key = _event_key(event)
        if key is None or key in seen:
            continue
        seen.add(key)
        unique.append(event)
    unique.sort(key=lambda event: str(event.get('ts') or ''), reverse=True)
    return unique[: max(1, min(int(limit), ACTIVITY_SNAPSHOT_LIMIT))]


def _json_safe(value):
    """Postgres JSON rejects unpaired UTF-16 surrogates from tool transcripts."""
    if isinstance(value, str):
        return ''.join(ch for ch in value if not (0xD800 <= ord(ch) <= 0xDFFF))
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return value


def persist_activity_snapshot(agent, data, *, limit=ACTIVITY_SNAPSHOT_LIMIT):
    """Union a live tail into the durable snapshot. Fail-soft: never blank the GET."""
    agent_pk = _agent_pk(agent)
    if agent_pk is None:
        return
    live_events = [_json_safe(event) for event in (data.get('events') or []) if isinstance(event, dict)]
    if not live_events:
        return
    try:
        existing = []
        snap = FleetAgentActivitySnapshot.objects.filter(agent_id=agent_pk).first()
        if snap and isinstance(snap.events, list):
            existing = [event for event in snap.events if isinstance(event, dict)]
        merged = _sorted_unique([*live_events, *existing], limit)
        FleetAgentActivitySnapshot.objects.update_or_create(
            agent_id=agent_pk,
            defaults={
                'events': merged,
                'newest_event_at': _newest_ts(merged),
                'source': str(data.get('source') or '')[:64],
                'resource_uid': str(data.get('resource_uid') or '')[:128],
                'pod_name': str(data.get('pod_name') or '')[:200],
                'source_state_at_capture': str(data.get('source_state') or '')[:32],
            },
        )
    except Exception:
        logger.warning(
            'activity snapshot persist failed for %s',
            getattr(agent, 'agent_username', '?'),
            exc_info=True,
        )


def load_activity_snapshot(agent, *, limit=ACTIVITY_SNAPSHOT_LIMIT):
    agent_pk = _agent_pk(agent)
    if agent_pk is None:
        return None
    try:
        # PostgreSQL's text representation retains the stored UTC offset.
        # USE_TZ=False ORM datetimes discard that offset (including DST fold),
        # so preserve it at the database adapter before parsing canonical UTC.
        snap = FleetAgentActivitySnapshot.objects.filter(agent_id=agent_pk).annotate(
            captured_at_with_offset=Cast('captured_at', output_field=CharField()),
            newest_event_at_with_offset=Cast('newest_event_at', output_field=CharField()),
        ).first()
    except Exception:
        logger.warning(
            'activity snapshot load failed for %s',
            getattr(agent, 'agent_username', '?'),
            exc_info=True,
        )
        return None
    if snap is None or not isinstance(snap.events, list):
        return None
    events = [event for event in snap.events if isinstance(event, dict)]
    if not events:
        return None
    return {
        'events': _sorted_unique(events, limit),
        'captured_at': _iso(snap.captured_at_with_offset),
        'newest_event_at': _iso(snap.newest_event_at_with_offset),
        'source': snap.source,
        'pod_name': snap.pod_name,
        'resource_uid': snap.resource_uid,
    }


def merge_activity_with_snapshot(agent, data, *, limit=ACTIVITY_SNAPSHOT_LIMIT):
    """Attach persisted history. Never relabel a live receipt as persisted."""
    if not isinstance(data, dict):
        data = {'events': [], 'total': 0, 'source_state': 'unavailable', 'history_events': []}
    else:
        data = dict(data)
    live_events = [event for event in (data.get('events') or []) if isinstance(event, dict)]
    snapshot = load_activity_snapshot(agent, limit=limit) if agent is not None else None
    snapshot_events = (snapshot or {}).get('events') or []

    live_state = str(data.get('source_state') or 'unavailable')
    cap = max(1, min(int(limit), ACTIVITY_SNAPSHOT_LIMIT))

    if live_events:
        persist_activity_snapshot(agent, data, limit=limit)
        snapshot = load_activity_snapshot(agent, limit=limit) or snapshot
        snapshot_events = (snapshot or {}).get('events') or []
        live_keys = {_event_key(event) for event in live_events}
        older = [event for event in snapshot_events if _event_key(event) not in live_keys]
        data['events'] = live_events[:cap]
        data['history_events'] = older[:cap]
        data['total'] = len(data['events'])
        if snapshot and snapshot.get('captured_at'):
            data['persisted_at'] = snapshot['captured_at']
        return data

    if snapshot_events and live_state in ('unavailable', 'degraded'):
        data['events'] = snapshot_events[:cap]
        data['history_events'] = []
        data['total'] = len(data['events'])
        data['source_state'] = 'persisted'
        data['live_source_state'] = live_state
        data['persisted_at'] = snapshot.get('captured_at')
        data['newest_event_at'] = snapshot.get('newest_event_at') or data.get('newest_event_at')
        if snapshot.get('pod_name') and not data.get('pod_name'):
            data['pod_name'] = snapshot['pod_name']
        if snapshot.get('resource_uid') and not data.get('resource_uid'):
            data['resource_uid'] = snapshot['resource_uid']
        return data

    if snapshot_events:
        # Readable empty tail (reset / just started): keep the live receipt
        # and surface Hive's last-known transcript as historical, not as live.
        data['history_events'] = snapshot_events[:cap]
        data['persisted_at'] = snapshot.get('captured_at')
        data.setdefault('events', [])
        data['total'] = len(data.get('events') or [])
        return data

    data.setdefault('history_events', data.get('history_events') or [])
    return data


def capture_native_activity_snapshot(agent, *, limit=ACTIVITY_SNAPSHOT_LIMIT):
    """Best-effort snapshot while the pod is still up (pre-hibernate/stop)."""
    if _agent_pk(agent) is None:
        return
    try:
        from .views import _get_native_agent_activity
        payload = _get_native_agent_activity(agent, limit=limit)
        persist_activity_snapshot(agent, payload, limit=limit)
    except Exception:
        logger.warning(
            'pre-scale activity snapshot failed for %s',
            getattr(agent, 'agent_username', '?'),
            exc_info=True,
        )


def ingest_native_activity_raw(agent, raw, *, limit=ACTIVITY_SNAPSHOT_LIMIT, pod_name='', source='workspace-pvc'):
    """Persist a pre-fetched audit+telemetry tail (hibernated PVC backfill)."""
    if _agent_pk(agent) is None:
        return 0
    from .views import parse_native_activity_raw
    events, newest = parse_native_activity_raw(raw, limit=limit)
    if not events:
        return 0
    persist_activity_snapshot(agent, {
        'events': events,
        'source_state': 'live',
        'source': source,
        'pod_name': pod_name,
        'newest_event_at': newest.isoformat().replace('+00:00', 'Z') if newest else None,
    }, limit=limit)
    return len(events)
