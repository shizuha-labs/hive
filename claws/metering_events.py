"""Neutral CloudEvents ingestion and transactional producer outbox.

The raw ``UsageEvent`` row is the canonical append-only fact.  Projections may
be rebuilt, but a producer retry can only replay the identical canonical JSON
payload for the same CloudEvents ``(source, id)`` identity.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta, timezone as datetime_timezone
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import re
from typing import Mapping
from urllib.parse import urlsplit

import requests
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from hive_platform.models import AdmissionAuthoritySnapshot, UsageEvent, UsageEventOutbox


DECIMAL_STRING_RE = re.compile(r'^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$')
SHA256_RE = re.compile(r'^[0-9a-f]{64}$')
CORRECTION_TYPE_MARKERS = ('adjustment', 'adjusted', 'reversal', 'reversed', 'correction', 'corrected')


class MeteringEventError(ValueError):
    code = 'invalid_cloudevent'

    def __init__(self, detail: str, *, field: str = ''):
        super().__init__(detail)
        self.detail = detail
        self.field = field

    def as_dict(self):
        result = {'error': self.code, 'detail': self.detail}
        if self.field:
            result['field'] = self.field
        return result


class MeteringEventConflict(MeteringEventError):
    code = 'idempotency_conflict'

    def __init__(self, *, source: str, event_id: str, stored_hash: str, supplied_hash: str):
        super().__init__(
            'The CloudEvents source/id identity already exists with a different canonical payload.',
        )
        self.source = source
        self.event_id = event_id
        self.stored_hash = stored_hash
        self.supplied_hash = supplied_hash

    def as_dict(self):
        return {
            **super().as_dict(),
            'source': self.source,
            'id': self.event_id,
            'stored_payload_sha256': self.stored_hash,
            'supplied_payload_sha256': self.supplied_hash,
        }


class AdmissionAuthorityError(MeteringEventError):
    code = 'invalid_admission_authority'


@dataclass(frozen=True)
class NormalizedUsageEvent:
    envelope: dict
    payload_sha256: str
    source: str
    event_id: str
    event_type: str
    subject: str
    dataschema: str
    datacontenttype: str
    occurred_at: object
    data: dict
    metric: str
    unit: str
    quantity: Decimal
    authority: dict
    correction_source: str
    correction_event_id: str


@dataclass(frozen=True)
class IngestResult:
    event: UsageEvent
    replayed: bool

    def as_dict(self):
        return {
            'status': 'replayed' if self.replayed else 'accepted',
            'source': self.event.source,
            'id': self.event.event_id,
            'payload_sha256': self.event.payload_sha256,
            'event_store_id': str(self.event.id),
        }


def canonical_json(payload: Mapping | list) -> str:
    try:
        return json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise MeteringEventError('CloudEvent must be valid JSON.', field='body') from exc


def canonical_payload_sha256(payload: Mapping | list) -> str:
    return hashlib.sha256(canonical_json(payload).encode('utf-8')).hexdigest()


def authority_snapshot_sha256(authority: Mapping) -> str:
    claims = {
        'billing_account_id': authority['billing_account_id'],
        'reservation_id': authority.get('reservation_id', ''),
        'lease_id': authority.get('lease_id', ''),
        'payer_link_generation': authority['payer_link_generation'],
        'binding_generation': authority['binding_generation'],
        'admission_generation': authority['admission_generation'],
    }
    return canonical_payload_sha256(claims)


def _required_string(payload: Mapping, key: str, *, max_length: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MeteringEventError(f'{key} must be a non-empty string.', field=key)
    value = value.strip()
    if len(value) > max_length:
        raise MeteringEventError(f'{key} exceeds {max_length} characters.', field=key)
    if any(ord(char) < 32 for char in value):
        raise MeteringEventError(f'{key} contains control characters.', field=key)
    return value


def _optional_string(payload: Mapping, key: str, *, max_length: int) -> str:
    value = payload.get(key, '')
    if value in (None, ''):
        return ''
    if not isinstance(value, str):
        raise MeteringEventError(f'{key} must be a string.', field=key)
    value = value.strip()
    if len(value) > max_length:
        raise MeteringEventError(f'{key} exceeds {max_length} characters.', field=key)
    return value


def _uri(value: str, field: str, *, absolute: bool = False) -> str:
    if any(char.isspace() for char in value):
        raise MeteringEventError(f'{field} must be a valid URI-reference.', field=field)
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise MeteringEventError(f'{field} must be a valid URI-reference.', field=field) from exc
    if absolute and not parsed.scheme:
        raise MeteringEventError(f'{field} must be an absolute URI.', field=field)
    return value


def _generation(value, field: str) -> int:
    if isinstance(value, bool):
        raise MeteringEventError(f'{field} must be a non-negative integer.', field=field)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise MeteringEventError(f'{field} must be a non-negative integer.', field=field) from exc
    if parsed < 0 or str(parsed) != str(value):
        raise MeteringEventError(f'{field} must be a non-negative integer.', field=field)
    return parsed


def _authority_snapshot(envelope: Mapping) -> dict:
    """Read the primitive CloudEvents authority extensions.

    CloudEvents extension attribute values use the core scalar type system, so
    the canonical wire contract uses individual lower-case attributes.  The
    nested ``authority`` form is accepted as a compatibility adapter for early
    producers, but storage always normalizes to this snapshot.
    """
    nested = envelope.get('authority')
    if nested is not None and not isinstance(nested, dict):
        raise MeteringEventError('authority must be an object.', field='authority')
    nested = nested or {}

    aliases = {
        'billing_account_id': ('billingaccountid', 'billing_account_id'),
        'proof_id': ('authorityproofid', 'proof_id'),
        'proof_sha256': ('authorityproofhash', 'proof_sha256'),
        'reservation_id': ('reservationid', 'reservation_id'),
        'lease_id': ('leaseid', 'lease_id'),
        'payer_link_generation': ('payerlinkgen', 'payer_link_generation'),
        'binding_generation': ('bindinggen', 'binding_generation'),
        'admission_generation': ('admissiongen', 'admission_generation'),
    }

    values = {}
    for target, (extension_name, nested_name) in aliases.items():
        value = envelope.get(extension_name)
        if value is None:
            value = nested.get(nested_name)
        values[target] = value

    for field in ('billing_account_id', 'proof_id', 'proof_sha256'):
        value = values[field]
        if not isinstance(value, str) or not value.strip():
            raise MeteringEventError(f'{field} is required in the admission authority.', field=field)
        values[field] = value.strip()

    if len(values['billing_account_id']) > 128:
        raise MeteringEventError('billing_account_id exceeds 128 characters.', field='billing_account_id')
    if len(values['proof_id']) > 255:
        raise MeteringEventError('proof_id exceeds 255 characters.', field='proof_id')
    values['proof_sha256'] = values['proof_sha256'].lower()
    if not SHA256_RE.fullmatch(values['proof_sha256']):
        raise MeteringEventError('proof_sha256 must be a lowercase SHA-256 hex digest.', field='proof_sha256')

    for field in ('reservation_id', 'lease_id'):
        value = values[field]
        if value in (None, ''):
            values[field] = ''
        elif not isinstance(value, str) or len(value.strip()) > 255:
            raise MeteringEventError(f'{field} must be a string of at most 255 characters.', field=field)
        else:
            values[field] = value.strip()
    if not values['reservation_id'] and not values['lease_id']:
        raise MeteringEventError(
            'Admission authority must bind a reservation_id or lease_id.',
            field='reservation_id',
        )

    for field in ('payer_link_generation', 'binding_generation', 'admission_generation'):
        values[field] = _generation(values[field], field)
    return values


def normalize_usage_cloudevent(envelope: Mapping) -> NormalizedUsageEvent:
    if not isinstance(envelope, dict):
        raise MeteringEventError('CloudEvent body must be a JSON object.', field='body')

    specversion = _required_string(envelope, 'specversion', max_length=16)
    if specversion != '1.0':
        raise MeteringEventError('Only CloudEvents specversion 1.0 is supported.', field='specversion')
    event_id = _required_string(envelope, 'id', max_length=255)
    source = _uri(_required_string(envelope, 'source', max_length=512), 'source')
    event_type = _required_string(envelope, 'type', max_length=255)
    subject = _required_string(envelope, 'subject', max_length=512)
    dataschema = _uri(
        _required_string(envelope, 'dataschema', max_length=512),
        'dataschema',
        absolute=True,
    )
    datacontenttype = _optional_string(envelope, 'datacontenttype', max_length=255) or 'application/json'
    if datacontenttype.split(';', 1)[0].strip().lower() not in ('application/json', 'application/cloudevents+json'):
        raise MeteringEventError('Usage event data must use a JSON content type.', field='datacontenttype')

    time_value = _required_string(envelope, 'time', max_length=64)
    occurred_at = parse_datetime(time_value)
    if occurred_at is None or timezone.is_naive(occurred_at):
        raise MeteringEventError('time must be an RFC3339 timestamp with an explicit UTC offset.', field='time')
    occurred_at = occurred_at.astimezone(datetime_timezone.utc)
    if not getattr(settings, 'USE_TZ', False):
        occurred_at = occurred_at.replace(tzinfo=None)

    data = envelope.get('data')
    if not isinstance(data, dict):
        raise MeteringEventError('data must be a JSON object.', field='data')
    metric = data.get('meter', data.get('metric'))
    if not isinstance(metric, str) or not metric.strip():
        raise MeteringEventError('data.meter is required.', field='data.meter')
    metric = metric.strip()
    if len(metric) > 64:
        raise MeteringEventError('data.meter exceeds 64 characters.', field='data.meter')
    unit = data.get('unit')
    if not isinstance(unit, str) or not unit.strip() or len(unit.strip()) > 32:
        raise MeteringEventError('data.unit must be a non-empty string of at most 32 characters.', field='data.unit')
    unit = unit.strip()
    quantity_value = data.get('quantity')
    if not isinstance(quantity_value, str) or not DECIMAL_STRING_RE.fullmatch(quantity_value):
        raise MeteringEventError('data.quantity must be a decimal string, never a JSON number.', field='data.quantity')
    try:
        quantity = Decimal(quantity_value)
    except InvalidOperation as exc:
        raise MeteringEventError('data.quantity is not a valid decimal string.', field='data.quantity') from exc
    if not quantity.is_finite() or len(quantity.as_tuple().digits) > 24 or max(-quantity.as_tuple().exponent, 0) > 6:
        raise MeteringEventError('data.quantity exceeds the supported decimal(24,6) range.', field='data.quantity')

    authority = _authority_snapshot(envelope)
    correction_source = ''
    correction_event_id = ''
    correction = data.get('correction_of')
    correction_type = any(marker in event_type.lower() for marker in CORRECTION_TYPE_MARKERS)
    if correction is not None:
        if not isinstance(correction, dict):
            raise MeteringEventError('data.correction_of must be an object.', field='data.correction_of')
        correction_source = _required_string(correction, 'source', max_length=512)
        correction_event_id = _required_string(correction, 'id', max_length=255)
        if (correction_source, correction_event_id) == (source, event_id):
            raise MeteringEventError('A correction cannot reference itself.', field='data.correction_of')
    elif correction_type:
        raise MeteringEventError(
            'Reversal, adjustment and correction event types must reference data.correction_of.',
            field='data.correction_of',
        )

    envelope_copy = json.loads(canonical_json(envelope))
    return NormalizedUsageEvent(
        envelope=envelope_copy,
        payload_sha256=canonical_payload_sha256(envelope_copy),
        source=source,
        event_id=event_id,
        event_type=event_type,
        subject=subject,
        dataschema=dataschema,
        datacontenttype=datacontenttype,
        occurred_at=occurred_at,
        data=data,
        metric=metric,
        unit=unit,
        quantity=quantity,
        authority=authority,
        correction_source=correction_source,
        correction_event_id=correction_event_id,
    )


def register_admission_authority_snapshot(authority: Mapping) -> AdmissionAuthoritySnapshot:
    """Register a snapshot from the trusted admission path, never from ingress."""
    proof_id = str(authority.get('proof_id') or '').strip()
    supplied_hash = str(authority.get('proof_sha256') or '').strip().lower()
    if not proof_id or not SHA256_RE.fullmatch(supplied_hash):
        raise AdmissionAuthorityError('Admission authority proof id/hash is invalid.')
    expected_hash = authority_snapshot_sha256(authority)
    if not hmac.compare_digest(supplied_hash, expected_hash):
        raise AdmissionAuthorityError('Admission authority hash does not match its canonical claims.')
    defaults = {
        'proof_sha256': supplied_hash,
        'billing_account_id': authority['billing_account_id'],
        'reservation_id': authority.get('reservation_id', ''),
        'lease_id': authority.get('lease_id', ''),
        'payer_link_generation': authority['payer_link_generation'],
        'binding_generation': authority['binding_generation'],
        'admission_generation': authority['admission_generation'],
        'snapshot': dict(authority),
    }
    snapshot, created = AdmissionAuthoritySnapshot.objects.get_or_create(
        proof_id=proof_id,
        defaults=defaults,
    )
    if not created:
        stored = {
            'proof_sha256': snapshot.proof_sha256,
            'billing_account_id': snapshot.billing_account_id,
            'reservation_id': snapshot.reservation_id,
            'lease_id': snapshot.lease_id,
            'payer_link_generation': snapshot.payer_link_generation,
            'binding_generation': snapshot.binding_generation,
            'admission_generation': snapshot.admission_generation,
        }
        expected = {key: value for key, value in defaults.items() if key != 'snapshot'}
        if stored != expected:
            raise AdmissionAuthorityError('Admission authority proof id already names different claims.')
    return snapshot


def validate_registered_admission_authority(authority: Mapping) -> AdmissionAuthoritySnapshot:
    """Resolve payer authority from server state; never trust body payer fields."""
    try:
        snapshot = AdmissionAuthoritySnapshot.objects.get(proof_id=authority['proof_id'])
    except AdmissionAuthoritySnapshot.DoesNotExist as exc:
        raise AdmissionAuthorityError('Admission authority proof is unknown or expired.') from exc
    supplied = {
        'proof_sha256': authority['proof_sha256'],
        'billing_account_id': authority['billing_account_id'],
        'reservation_id': authority['reservation_id'],
        'lease_id': authority['lease_id'],
        'payer_link_generation': authority['payer_link_generation'],
        'binding_generation': authority['binding_generation'],
        'admission_generation': authority['admission_generation'],
    }
    stored = {
        'proof_sha256': snapshot.proof_sha256,
        'billing_account_id': snapshot.billing_account_id,
        'reservation_id': snapshot.reservation_id,
        'lease_id': snapshot.lease_id,
        'payer_link_generation': snapshot.payer_link_generation,
        'binding_generation': snapshot.binding_generation,
        'admission_generation': snapshot.admission_generation,
    }
    if supplied != stored:
        raise AdmissionAuthorityError('Admission authority claims do not match the server-issued proof.')
    return snapshot


def _conflict(existing: UsageEvent, normalized: NormalizedUsageEvent):
    raise MeteringEventConflict(
        source=normalized.source,
        event_id=normalized.event_id,
        stored_hash=existing.payload_sha256,
        supplied_hash=normalized.payload_sha256,
    )


def _event_defaults(normalized: NormalizedUsageEvent) -> dict:
    data = normalized.data
    authority = normalized.authority
    owner_id = data.get('owner_id')
    try:
        owner_id = int(owner_id) if owner_id not in (None, '') else None
    except (TypeError, ValueError) as exc:
        raise MeteringEventError('data.owner_id must be an integer when supplied.', field='data.owner_id') from exc
    metadata = data.get('metadata', {})
    if not isinstance(metadata, dict):
        raise MeteringEventError('data.metadata must be an object when supplied.', field='data.metadata')

    return {
        'specversion': '1.0',
        'event_type': normalized.event_type,
        'subject': normalized.subject,
        'dataschema': normalized.dataschema,
        'datacontenttype': normalized.datacontenttype,
        'event_payload': normalized.envelope,
        'payload_sha256': normalized.payload_sha256,
        'billing_account_id': authority['billing_account_id'],
        'authority_snapshot': authority,
        'authority_proof_id': authority['proof_id'],
        'authority_proof_sha256': authority['proof_sha256'],
        'reservation_id': authority['reservation_id'],
        'lease_id': authority['lease_id'],
        'payer_link_generation': authority['payer_link_generation'],
        'binding_generation': authority['binding_generation'],
        'admission_generation': authority['admission_generation'],
        'correction_of_source': normalized.correction_source,
        'correction_of_event_id': normalized.correction_event_id,
        'tenant_org_id': authority['billing_account_id'],
        'tenant_org_slug': str(data.get('tenant_org_slug') or '')[:120],
        'owner_id': owner_id,
        'metric': normalized.metric,
        'unit': normalized.unit,
        'quantity': normalized.quantity,
        'model': str(data.get('model') or '')[:128],
        'resource_id': str(data.get('resource_id') or '')[:160],
        'idempotency_key': 'ce:' + hashlib.sha256(
            f'{normalized.source}\0{normalized.event_id}'.encode('utf-8')
        ).hexdigest(),
        'occurred_at': normalized.occurred_at,
        'metadata': metadata,
    }


def _validate_correction_authority(
    normalized: NormalizedUsageEvent,
    corrected_event: UsageEvent,
) -> None:
    """Keep every correction in the original event's payer partition.

    A correction is a new immutable fact, but it must not be usable to move the
    corrected quantity to a different payer, reservation, lease, or authority
    generation.  Compare the denormalised server-validated fields rather than
    trusting either event's JSON body.
    """
    authority = normalized.authority
    supplied = {
        'billing_account_id': authority['billing_account_id'],
        'authority_proof_id': authority['proof_id'],
        'authority_proof_sha256': authority['proof_sha256'],
        'reservation_id': authority['reservation_id'],
        'lease_id': authority['lease_id'],
        'payer_link_generation': authority['payer_link_generation'],
        'binding_generation': authority['binding_generation'],
        'admission_generation': authority['admission_generation'],
    }
    original = {
        'billing_account_id': corrected_event.billing_account_id,
        'authority_proof_id': corrected_event.authority_proof_id,
        'authority_proof_sha256': corrected_event.authority_proof_sha256,
        'reservation_id': corrected_event.reservation_id,
        'lease_id': corrected_event.lease_id,
        'payer_link_generation': corrected_event.payer_link_generation,
        'binding_generation': corrected_event.binding_generation,
        'admission_generation': corrected_event.admission_generation,
    }
    if supplied != original:
        raise AdmissionAuthorityError(
            'Correction events must inherit the corrected event payer and admission authority.',
            field='data.correction_of',
        )


def ingest_usage_cloudevent(envelope: Mapping, *, trusted_context: Mapping | None = None) -> IngestResult:
    normalized = normalize_usage_cloudevent(envelope)
    validate_registered_admission_authority(normalized.authority)
    defaults = _event_defaults(normalized)
    if trusted_context:
        for field in ('tenant_org_slug', 'owner_id', 'claw'):
            if field in trusted_context:
                defaults[field] = trusted_context[field]
    try:
        with transaction.atomic():
            if normalized.correction_source:
                try:
                    corrected_event = UsageEvent.objects.select_for_update().get(
                        source=normalized.correction_source,
                        event_id=normalized.correction_event_id,
                    )
                except UsageEvent.DoesNotExist as exc:
                    raise MeteringEventError(
                        'data.correction_of must reference an existing immutable usage event.',
                        field='data.correction_of',
                    ) from exc
                _validate_correction_authority(normalized, corrected_event)
            event, created = UsageEvent.objects.get_or_create(
                source=normalized.source,
                event_id=normalized.event_id,
                defaults=defaults,
            )
    except IntegrityError:
        # A concurrent writer won the unique(source,id) race. Resolve it to the
        # same replay/conflict result instead of surfacing a transient 500.
        event = UsageEvent.objects.get(source=normalized.source, event_id=normalized.event_id)
        created = False
    if not created and not hmac.compare_digest(event.payload_sha256, normalized.payload_sha256):
        _conflict(event, normalized)
    return IngestResult(event=event, replayed=not created)


@transaction.atomic
def ingest_usage_cloudevents(envelopes: list[Mapping]) -> list[IngestResult]:
    if not isinstance(envelopes, list) or not envelopes:
        raise MeteringEventError('Batch body must be a non-empty JSON array.', field='body')
    if len(envelopes) > 1000:
        raise MeteringEventError('Batch body exceeds the 1000-event limit.', field='body')
    return [ingest_usage_cloudevent(envelope) for envelope in envelopes]


def enqueue_usage_cloudevent(envelope: Mapping) -> UsageEventOutbox:
    """Create a durable outbox row inside the caller's resource transaction."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError('enqueue_usage_cloudevent must run inside transaction.atomic().')
    normalized = normalize_usage_cloudevent(envelope)
    outbox, created = UsageEventOutbox.objects.get_or_create(
        source=normalized.source,
        event_id=normalized.event_id,
        defaults={
            'envelope': normalized.envelope,
            'payload_sha256': normalized.payload_sha256,
        },
    )
    if not created and not hmac.compare_digest(outbox.payload_sha256, normalized.payload_sha256):
        raise MeteringEventConflict(
            source=normalized.source,
            event_id=normalized.event_id,
            stored_hash=outbox.payload_sha256,
            supplied_hash=normalized.payload_sha256,
        )
    return outbox


def deliver_usage_outbox(outbox_id, *, http_post=requests.post) -> UsageEventOutbox:
    """Attempt one delivery; transient failures remain pending for retry."""
    with transaction.atomic():
        outbox = UsageEventOutbox.objects.select_for_update().get(id=outbox_id)
        if outbox.status != UsageEventOutbox.Status.PENDING:
            return outbox
        outbox.attempts += 1
        outbox.save(update_fields=['attempts'])

    url = str(getattr(settings, 'METERING_CORE_EVENTS_URL', '') or '').strip()
    if not url:
        url = 'http://shizuha-hive.shizuha.svc.cluster.local:8000/internal/v1/usage/events'
    token = str(getattr(settings, 'METERING_CORE_SERVICE_TOKEN', '') or '').strip()
    if not token:
        token = str(getattr(settings, 'HIVE_INTERNAL_AUTH_TOKEN', '') or '').strip()
    headers = {'Content-Type': 'application/cloudevents+json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'

    error = ''
    response = None
    try:
        response = http_post(
            url,
            data=canonical_json(outbox.envelope).encode('utf-8'),
            headers=headers,
            timeout=10,
        )
        if response.status_code not in (200, 201):
            error = f'HTTP {response.status_code}: {str(response.text)[:500]}'
    except requests.RequestException as exc:
        error = f'{type(exc).__name__}: {exc}'[:500]

    outcome = 'retry'
    with transaction.atomic():
        outbox = UsageEventOutbox.objects.select_for_update().get(id=outbox_id)
        if response is not None and response.status_code in (200, 201):
            outbox.status = UsageEventOutbox.Status.DELIVERED
            outbox.delivered_at = timezone.now()
            outbox.last_error = ''
            outcome = 'delivered'
        elif response is not None and response.status_code == 409:
            outbox.status = UsageEventOutbox.Status.CONFLICT
            outbox.last_error = error
            outcome = 'conflict'
        else:
            delay_seconds = min(2 ** min(outbox.attempts, 8), 300)
            outbox.next_attempt_at = timezone.now() + timedelta(seconds=delay_seconds)
            outbox.last_error = error or 'Unknown delivery failure'
        outbox.save(update_fields=[
            'status', 'attempts', 'next_attempt_at', 'last_error', 'delivered_at',
        ])
    # Fail-loud delivery telemetry is best-effort and must never mask the durable
    # outbox state transition above. Alertmanager routes conflicts/retry streaks
    # to the Metering owner via the existing usage-metering metrics endpoint.
    from . import usage_metering_metrics
    usage_metering_metrics.record_outbox_delivery(outcome, outbox.source)
    return outbox
