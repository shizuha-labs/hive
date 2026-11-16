"""CloudEvents v1.0 envelope builder for the runtime usage outbox.

Deterministic: the same outbox row always renders a byte-identical envelope,
so at-least-once redelivery replays as the same payload hash (replay success
at the core), never as a conflict. Mirrors the accepted scs#72 builder.
"""
from __future__ import annotations

from typing import Any, Mapping

DATASCHEMA = 'urn:shizuha:usage-event:1'


class EnvelopeError(Exception):
    """The outbox row cannot be rendered as a contract-valid envelope."""


def _rfc3339(value) -> str:
    value = value.isoformat()
    if value.endswith('+00:00'):
        value = value[:-6] + 'Z'
    return value


def build_envelope(row) -> dict:
    data = dict(row.data or {})
    for field in ('meter', 'unit', 'quantity'):
        if not str(data.get(field) or '').strip():
            raise EnvelopeError(f'runtime usage data missing {field}')
    data.setdefault('billing_account_id', row.subject)
    return {
        'specversion': '1.0',
        'id': str(row.event_id),
        'source': row.source,
        'type': row.event_type,
        'subject': row.subject,
        'dataschema': DATASCHEMA,
        'datacontenttype': 'application/json',
        'time': _rfc3339(row.occurred_at),
        'data': data,
    }
