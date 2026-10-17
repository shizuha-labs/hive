"""Runtime event timestamps, separate from the reconciler's silence signal.

This module is also sent as a standalone, read-only Python probe into an agent.
It emits only timestamps, never log messages, tool inputs/results or credentials.
"""
import ast
import datetime
import json
import math
import os
from pathlib import Path


LOG_PATHS = {
    'pino': (
        '/home/agent/.config/shizuha/logs/shizuha.log',
        '/home/agent/.shizuha/logs/shizuha.log',
    ),
    'audit': ('/home/agent/.shizuha/.audit-log.jsonl',),
    'telemetry': ('/home/agent/.shizuha/.telemetry.jsonl',),
}
MAX_TAIL_BYTES = 256 * 1024


def unwrap_text(raw):
    if isinstance(raw, bytes):
        return raw.decode('utf-8', 'replace')
    if not isinstance(raw, str):
        return ''
    if raw[:2] in ("b'", 'b"'):
        try:
            value = ast.literal_eval(raw)
            return value.decode('utf-8', 'replace') if isinstance(value, bytes) else ''
        except (ValueError, SyntaxError):
            return ''
    return raw


def classify_exec_error(exc) -> str:
    """Typed exec-failure class for liveness probe lines (PLAT-9950).

    The reconciler must print an actionable reason for every exec failure,
    never a bare ``'NoneType' object has no attribute 'decode'`` crash string.
    Classes:
      - ``exec_denied``: RBAC/auth denial (ApiException 401/403) — the
        isolated-namespace class; cured only via PLAT-9114's scoped
        Role/RoleBinding bootstrap, never by widening CI exec privileges.
      - ``transport``: kubernetes-client/websocket crashes (including the
        AttributeError NoneType.decode class) and any other client-side
        transport error.
    """
    try:
        from kubernetes.client.rest import ApiException
    except Exception:
        return 'transport'
    if isinstance(exc, ApiException) and getattr(exc, 'status', None) in (401, 403):
        return 'exec_denied'
    return 'transport'


def runtime_event_epoch(record, source, observed_at=None):
    """Recognize native runtime schemas and use their embedded event clock.

    Runtime activity includes idle/error records; this does not claim useful
    task progress. Transport timestamps and file mtimes are never event clocks.
    """
    if not isinstance(record, dict):
        return None
    raw = None
    milliseconds = False
    if source == 'pino':
        if (type(record.get('level')) is not int or record['level'] not in (10, 20, 30, 40, 50, 60)
                or type(record.get('pid')) is not int or record['pid'] <= 0
                or not isinstance(record.get('hostname'), str) or not record['hostname']
                or not isinstance(record.get('msg'), str) or not record['msg']):
            return None
        raw = record.get('time')
        milliseconds = True
    elif source == 'audit':
        if (record.get('phase') not in ('before', 'after')
                or not isinstance(record.get('tool'), str) or not record['tool']
                or not isinstance(record.get('id'), str) or not record['id']):
            return None
        raw = record.get('timestamp')
    elif source == 'telemetry':
        if record.get('name') != 'llm-turn':
            return None
        raw = record.get('endTime') or record.get('startTime')
    else:
        return None
    try:
        if milliseconds:
            if type(raw) not in (int, float):
                return None
            epoch = raw / 1000.0
        else:
            if not isinstance(raw, str):
                return None
            timestamp = datetime.datetime.fromisoformat(raw.replace('Z', '+00:00'))
            if timestamp.tzinfo is None:
                return None  # Never interpret an unspecified clock as host local time.
            epoch = timestamp.timestamp()
        observed = observed_at if observed_at is not None else datetime.datetime.now(datetime.timezone.utc).timestamp()
        if not math.isfinite(epoch) or epoch <= 0 or epoch > observed:
            return None
        return epoch
    except (TypeError, ValueError, OverflowError):
        return None


def stdout_activity_epoch(raw):
    # Kube prepends a transport timestamp, which may be current when replaying
    # an old record. Only a recognized record's inner timestamp is activity.
    text = unwrap_text(raw).strip()
    body = text.partition(' ')[2]
    try:
        return runtime_event_epoch(json.loads(body), 'pino')
    except (TypeError, ValueError):
        return None


def read_file_signals(paths=LOG_PATHS):
    mtime = None
    activity = None
    source = None
    for kind, filenames in paths.items():
        for name in filenames:
            # Preserve the established silence signal: only the two pino files
            # contribute mtime. Audit/telemetry timestamps do not change policy.
            if kind == 'pino':
                try:
                    value = os.path.getmtime(name)
                    mtime = max(mtime, value) if mtime is not None else value
                except OSError:
                    pass
            try:
                with open(name, 'rb') as log:
                    size = log.seek(0, os.SEEK_END)
                    start = max(0, size - MAX_TAIL_BYTES)
                    log.seek(start)
                    tail = log.read(MAX_TAIL_BYTES)
                if start:
                    tail = tail.partition(b'\n')[2]  # Discard the clipped first record.
                observed = datetime.datetime.now(datetime.timezone.utc).timestamp()
                for line in tail.splitlines():
                    try:
                        epoch = runtime_event_epoch(json.loads(line), kind, observed)
                    except (TypeError, ValueError):
                        continue
                    if epoch is not None and (activity is None or epoch > activity):
                        activity, source = epoch, kind
            except OSError:
                pass
    return {'file_mtime': mtime, 'activity_epoch': activity, 'activity_source': source}


def remote_probe_source():
    # Constants are passed explicitly so the actual subprocess boundary is
    # exercisable against isolated files, without mounting an agent HOME.
    return Path(__file__).read_text() + '\nprint(json.dumps(read_file_signals(' + repr(LOG_PATHS) + ')))\n'
