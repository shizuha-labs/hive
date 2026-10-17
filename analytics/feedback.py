"""HIVE-394 feedback intake (HLD 89891ffa §5.2 / §6) — the ONE normalizer every
feedback channel funnels through, on top of the analytics security core.

Reuses ``analytics.ingest`` for the shared tenant invariants (``user_ref_for``
pseudonymization, ``IngestError``); the caller resolves ``org_id`` server-side via
``analytics.ingest.authorize_org`` (end-user channels) or a trusted service
identity (support/GlitchTip) BEFORE calling in — this module never reads a tenant
from client data. ``sentiment``/``theme_id`` are clustering-owned and never set here.
"""
import json

from .ingest import IngestError, user_ref_for
from .clustering import mark_feedback_watermark
from .models import FEEDBACK_SOURCES, FeedbackEvent

MAX_TEXT = 20_000       # bound stored feedback text (HLD §6 DoS/abuse)
MAX_META_BYTES = 8_192
VALID_SOURCES = {s for s, _ in FEEDBACK_SOURCES}
VALID_SURFACES = {'home', 'hive', 'pulse', 'admin'}


def record_feedback_event(*, org_id, source, source_ref='', text='',
                          user_id=None, surface='', meta=None):
    """Normalize one feedback signal into feedback_event. Returns (event, created).

    ``org_id`` MUST already be server-derived (never client-declared). Idempotent on
    (source, source_ref) when a source_ref is present, so a redelivered upstream
    ticket/message ingests exactly once. Raises IngestError on invalid input.
    """
    if not org_id:
        raise IngestError('org_id required (server-derived)', status=400)
    if source not in VALID_SOURCES:
        raise IngestError(f'invalid feedback source: {source!r}', status=400)
    if surface and surface not in VALID_SURFACES:
        raise IngestError(f'invalid surface: {surface!r}', status=400)

    text = (text or '')[:MAX_TEXT]
    meta = meta if isinstance(meta, dict) else {}
    try:
        if len(json.dumps(meta)) > MAX_META_BYTES:
            meta = {'_truncated': True}
    except (TypeError, ValueError):
        meta = {}

    # Pseudonymous per-(user,org) ref via the shared analytics helper (no raw PII).
    user_ref = user_ref_for(user_id, org_id) if user_id is not None else ''

    org_id = int(org_id)
    fields = dict(text=text, user_ref=user_ref,
                  surface=surface or '', meta=meta, schema_version=1)
    if source_ref:
        event, created = FeedbackEvent.objects.get_or_create(
            org_id=org_id, source=source, source_ref=source_ref, defaults=fields)
        if created:
            mark_feedback_watermark()
        return event, created
    event = FeedbackEvent.objects.create(org_id=org_id, source=source, source_ref='', **fields)
    mark_feedback_watermark()
    return event, True


def ingest_support_ticket(*, org_id, ticket_id, body, reporter_id=None, surface='', meta=None):
    """Adapter: a HIVE-322 support ticket → feedback_event. ``org_id`` is the
    ticket's owning-customer org, resolved server-side by the caller (support
    service), NOT a client body. Idempotent on the ticket id."""
    return record_feedback_event(
        org_id=org_id, source='support_ticket', source_ref=str(ticket_id),
        text=body or '', user_id=reporter_id, surface=surface, meta=meta,
    )
