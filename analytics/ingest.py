"""HIVE-393 ingest security core (HLD §6 / ADR-0013 dec.2).

The ONE invariant: an event's tenant (`org_id`) is authorized against the
caller's VERIFIED JWT `organization_memberships` — never trusted from the client
payload. A client naming an org it doesn't belong to is a cross-tenant forgery
and is rejected; the event is not stored.
"""
import hashlib
import re

from django.conf import settings

MAX_BATCH = 100
MAX_PROPS_BYTES = 4096
VALID_SURFACES = {'home', 'hive', 'pulse', 'admin'}
VALID_EVENT_TYPES = {
    'page_view', 'feature_use', 'drop_off', 'error_surface',
    'journey_step', 'journey_complete',
}

SAFE_PROP_KEYS_BY_EVENT = {
    'page_view': {'route', 'section', 'entrypoint', 'referrer_kind'},
    'feature_use': {'feature', 'action', 'component', 'variant'},
    'drop_off': {'journey', 'step', 'reason_code', 'component'},
    'error_surface': {'error_code', 'component', 'recoverable', 'severity'},
    'journey_step': {'journey', 'step', 'component', 'variant'},
    'journey_complete': {'journey', 'outcome', 'duration_ms', 'steps'},
}
SAFE_PROP_VALUE_TYPES = (str, int, float, bool, type(None))
PII_PROP_KEY_RE = re.compile(
    r'(email|e-mail|phone|mobile|name|first_name|last_name|full_name|address|ip|'
    r'token|secret|password|passcode|session|cookie|jwt|bearer|key)',
    re.IGNORECASE,
)
EMAIL_VALUE_RE = re.compile(r'\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b', re.IGNORECASE)
PHONE_VALUE_RE = re.compile(r'(?<!\w)(?:\+?\d[\d\s().-]{6,}\d)(?!\w)')
CREDENTIAL_VALUE_RE = re.compile(
    r'(?:'
    r'\bbearer\s+\S{8,}'
    r'|\b(?:jwt|token|session(?:id)?|cookie|secret|password)\s*[:=]\s*\S{4,}'
    r'|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}'
    r')',
    re.IGNORECASE,
)


class IngestError(Exception):
    """status: 400 (bad event) or 403 (cross-tenant / unauthorized org)."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def _membership_org_ids(user):
    memberships = getattr(user, 'organization_memberships', None) or {}
    out = set()
    for k in memberships.keys():
        try:
            out.add(int(k))
        except (TypeError, ValueError):
            continue
    return out


def authorize_org(user, requested_org):
    """Resolve the tenant for an event from the caller's verified memberships.

    - requested_org given: MUST be one the caller belongs to, else 403.
    - requested_org omitted: default to the caller's single org; ambiguous when
      the caller has 0 or >1 orgs (must be specified) → 400/403.
    """
    org_ids = _membership_org_ids(user)
    if requested_org is not None and str(requested_org) != '':
        try:
            req = int(requested_org)
        except (TypeError, ValueError):
            raise IngestError('org_id must be an integer', status=400)
        if req not in org_ids:
            # Cross-tenant forgery: the caller is not a member of this org.
            raise IngestError('not a member of the requested org', status=403)
        return req
    if len(org_ids) == 1:
        return next(iter(org_ids))
    if not org_ids:
        raise IngestError('caller has no organization membership', status=403)
    raise IngestError('org_id required (caller belongs to multiple orgs)', status=400)


def user_ref_for(user_id, org_id):
    """Server-side pseudonymous, per-(user,org) stable ref — no raw PII stored,
    salted with the server secret so refs aren't reversible/enumerable."""
    salt = getattr(settings, 'SECRET_KEY', '')
    return hashlib.sha256(f'{salt}|{org_id}|{user_id}'.encode()).hexdigest()[:32]


def _validate_safe_props(event_type, props):
    """Return the no-PII analytics props subset for a known event type.

    Contract: emitters may send only event-type allow-listed keys with scalar,
    non-PII values. Unknown keys are rejected rather than stored/scrubbed so a
    new emitter must extend this contract deliberately before rollout.
    """
    allowed = SAFE_PROP_KEYS_BY_EVENT.get(event_type, set())
    clean = {}
    for key, value in props.items():
        if not isinstance(key, str) or not key:
            raise IngestError('props keys must be non-empty strings')
        if key not in allowed:
            raise IngestError(f'props key not allowed for {event_type}: {key}')
        if PII_PROP_KEY_RE.search(key):
            raise IngestError(f'props key is PII-like: {key}')
        if not isinstance(value, SAFE_PROP_VALUE_TYPES):
            raise IngestError(f'props value for {key} must be scalar')
        if isinstance(value, str):
            if len(value) > 256:
                raise IngestError(f'props value for {key} too long')
            if (
                EMAIL_VALUE_RE.search(value)
                or PHONE_VALUE_RE.search(value)
                or CREDENTIAL_VALUE_RE.search(value)
            ):
                raise IngestError(f'props value for {key} looks like PII')
        clean[key] = value
    return clean


def validate_event(raw):
    """Validate one event's client-supplied fields (surface/type/target/props).
    org_id is handled separately by authorize_org — never trusted here."""
    if not isinstance(raw, dict):
        raise IngestError('event must be an object')
    surface = raw.get('surface')
    if surface not in VALID_SURFACES:
        raise IngestError(f'invalid surface: {surface!r}')
    event_type = raw.get('event_type')
    if event_type not in VALID_EVENT_TYPES:
        raise IngestError(f'invalid event_type: {event_type!r}')
    props = raw.get('props') or {}
    if not isinstance(props, dict):
        raise IngestError('props must be an object')
    props = _validate_safe_props(event_type, props)
    import json
    if len(json.dumps(props)) > MAX_PROPS_BYTES:
        raise IngestError('props too large')
    target = raw.get('target') or ''
    if not isinstance(target, str) or len(target) > 2048:
        raise IngestError('invalid target')
    return {'surface': surface, 'event_type': event_type, 'target': target, 'props': props}
