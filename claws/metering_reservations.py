"""PLAT-4484 — atomic quota reservations, spend leases, proofs, writer cutover.

Design pin: HLD v44 · ADR-0032 v15 · ADR-0022 v15.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Any, Mapping, Optional, Tuple

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .metering_core import CatalogError, MeteringCoreError, ordered_entitlement_balances
from .metering_models import (
    AdmissionProofReceipt,
    AdmissionWriterAuthority,
    BillingAccount,
    EntitlementBalance,
    ProducerHolder,
    QuotaReservation,
    SpendLease,
    canonical_sha256,
)

# Hard ceiling for SpendLease lifetime (HLD §6 / §12.3).
MAX_LEASE_SECONDS = 60
DEFAULT_RESERVATION_TTL_SECONDS = 300


class ReservationError(MeteringCoreError):
    """Reservation/lease/admission protocol error."""

    def __init__(self, message: str, *, code: str = 'reservation_error', http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.http_status = http_status

    def as_dict(self) -> dict:
        return {'error': self.code, 'detail': str(self)}


def _dec(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(',', ':'), default=str)


def _sha(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode('utf-8')).hexdigest()


def _new_id(prefix: str) -> str:
    return f'{prefix}_{uuid.uuid4().hex}'


def _qty_str(value) -> str:
    """Canonical decimal string without trailing zeros noise."""
    d = _dec(value).normalize()
    # normalize can yield scientific notation for tiny values; force fixed form.
    s = format(d, 'f')
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    return s or '0'


def _reservation_response(res: QuotaReservation) -> dict:
    snap = dict(res.response_snapshot or {})
    snap.update(
        {
            'decision_id': res.decision_id,
            'allowed': res.allowed,
            'reservation_id': str(res.id),
            'status': res.status,
            'derived_billing_account_id': str(res.billing_account_id),
            'free_reserved_quantity': _qty_str(res.original_reserved if res.allowed else 0),
            'server_rated_spend_reserved_inr_micros': res.server_rated_spend_reserved_inr_micros,
            'committed_actual': _qty_str(res.committed_actual),
            'released_unused': _qty_str(res.released_unused),
            'held_unknown': _qty_str(res.held_unknown),
            'entitlement_generation': res.entitlement_generation,
            'balance_generation': res.balance_generation,
            'rate_version': res.rate_version_id,
            'admission_epoch': res.admission_epoch,
            'expires_at': res.expires_at.isoformat().replace('+00:00', 'Z'),
            'reason': res.deny_reason or '',
            'meter': res.meter_key,
            'meter_version': res.meter_version,
            'unit': res.unit,
            'resource_uid': res.resource_uid,
        }
    )
    return snap


def _require_active_writer(product: str, generation: Optional[int]) -> AdmissionWriterAuthority:
    active = (
        AdmissionWriterAuthority.objects.filter(
            product=product, state=AdmissionWriterAuthority.State.ACTIVE
        )
        .order_by('-generation')
        .first()
    )
    if active is None:
        # Bootstrap: first writer for product.
        active = AdmissionWriterAuthority.objects.create(
            product=product,
            generation=1,
            state=AdmissionWriterAuthority.State.ACTIVE,
            activated_at=timezone.now(),
        )
    if generation is not None and int(generation) != active.generation:
        raise ReservationError(
            f'stale admission writer generation={generation} active={active.generation}',
            code='stale_writer_generation',
            http_status=409,
        )
    return active


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _b64url_decode(data: str) -> bytes:
    pad = '=' * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _validated_derivation_secret(secret: Any) -> str:
    if not isinstance(secret, str) or not secret.strip():
        raise ReservationError(
            'Configure METERING_ADMISSION_SIGNING_KEY or a nonempty SECRET_KEY',
            code='invalid_signing_key',
            http_status=500,
        )
    return secret


@lru_cache(maxsize=4)
def _admission_signing_material(configuration: Tuple[str, str, Optional[str]]) -> Tuple[Ed25519PrivateKey, str]:
    """Load Ed25519 private key + kid for Proof v4 (RFC 7515 compact JWS, alg=EdDSA).

    ``METERING_ADMISSION_SIGNING_KEY`` may be:
    - raw 32-byte seed, base64/base64url encoded
    - PEM PKCS8 Ed25519 private key

    When unset, derive a deterministic dev/test key from Django SECRET_KEY so
    unit tests exercise real EdDSA without external secrets. Production MUST
    set ``METERING_ADMISSION_SIGNING_KEY`` (ops-owned).
    """
    raw, kid, secret = configuration
    if raw:
        if 'BEGIN' in raw:
            private = serialization.load_pem_private_key(raw.encode('utf-8'), password=None)
            if not isinstance(private, Ed25519PrivateKey):
                raise ReservationError(
                    'METERING_ADMISSION_SIGNING_KEY must be Ed25519',
                    code='invalid_signing_key',
                    http_status=500,
                )
            return private, kid
        try:
            seed = _b64url_decode(raw.replace('+', '-').replace('/', '_'))
        except Exception as exc:
            raise ReservationError(
                'METERING_ADMISSION_SIGNING_KEY is not valid base64',
                code='invalid_signing_key',
                http_status=500,
            ) from exc
        if len(seed) != 32:
            raise ReservationError(
                'METERING_ADMISSION_SIGNING_KEY seed must be 32 bytes',
                code='invalid_signing_key',
                http_status=500,
            )
        return Ed25519PrivateKey.from_private_bytes(seed), kid

    # Preserve the configured derivation; never invent a shared default identity.
    secret = _validated_derivation_secret(secret)
    seed = hashlib.sha256(f'metering-admission-eddsa-v1:{secret}'.encode('utf-8')).digest()
    return Ed25519PrivateKey.from_private_bytes(seed), kid


def _get_admission_signer() -> Tuple[Ed25519PrivateKey, str]:
    raw = (os.environ.get('METERING_ADMISSION_SIGNING_KEY') or '').strip()
    kid = (os.environ.get('METERING_ADMISSION_SIGNING_KID') or 'metering-admission-v1').strip()
    # Validate before cache lookup: stringification can alias invalid types to
    # an earlier valid secret. A tuple also preserves delimiter-bearing inputs.
    secret = None if raw else _validated_derivation_secret(getattr(settings, 'SECRET_KEY', None))
    return _admission_signing_material((raw, kid, secret))


def _issue_proof(
    *,
    profile: str,
    payload: Mapping[str, Any],
    reservation: Optional[QuotaReservation] = None,
    lease: Optional[SpendLease] = None,
    kid: Optional[str] = None,
) -> AdmissionProofReceipt:
    """Issue Proof v4 as compact JWS with real Ed25519 (alg=EdDSA)."""
    body = dict(payload)
    body.setdefault('profile', profile)
    body.setdefault('iat', int(timezone.now().timestamp()))
    body.setdefault('aud', 'metering:admission')
    digest = _sha(body)
    private, default_kid = _get_admission_signer()
    use_kid = kid or default_kid
    header_obj = {'alg': 'EdDSA', 'kid': use_kid, 'typ': profile}
    header_b64 = _b64url(json.dumps(header_obj, separators=(',', ':')).encode('utf-8'))
    # RFC 8785-style canonical JSON payload (sorted keys, compact separators).
    payload_b64 = _b64url(_canonical(body).encode('utf-8'))
    signing_input = f'{header_b64}.{payload_b64}'.encode('ascii')
    signature = private.sign(signing_input)
    jws = f'{header_b64}.{payload_b64}.{_b64url(signature)}'
    return AdmissionProofReceipt.objects.create(
        receipt_id=_new_id('apr'),
        profile=profile,
        reservation=reservation,
        lease=lease,
        payload_sha256=digest,
        jws_compact=jws,
        kid=use_kid,
        algorithm='EdDSA',
        audience='metering:admission',
        payload=body,
    )


def verify_proof_envelope(
    *,
    jws_compact: str,
    expected_profile: str,
    expected_kid: str = 'metering-admission-v1',
    expected_audience: str = 'metering:admission',
    public_key: Optional[Ed25519PublicKey] = None,
) -> dict:
    """Fail closed on bad signature, kid, algorithm, type, or audience."""
    try:
        header_b64, payload_b64, sig_b64 = jws_compact.split('.')
    except ValueError as exc:
        raise ReservationError('malformed proof jws', code='invalid_proof') from exc
    try:
        header = json.loads(_b64url_decode(header_b64))
        payload = json.loads(_b64url_decode(payload_b64))
        signature = _b64url_decode(sig_b64)
    except Exception as exc:
        raise ReservationError('malformed proof jws encoding', code='invalid_proof') from exc
    if header.get('alg') != 'EdDSA':
        raise ReservationError('wrong proof algorithm', code='invalid_proof_algorithm')
    if header.get('kid') != expected_kid:
        raise ReservationError('wrong proof kid', code='invalid_proof_kid')
    if header.get('typ') != expected_profile:
        raise ReservationError('wrong proof type', code='invalid_proof_type')
    if payload.get('aud') != expected_audience:
        raise ReservationError('wrong proof audience', code='invalid_proof_audience')
    if payload.get('profile') != expected_profile:
        raise ReservationError('proof profile mismatch', code='invalid_proof_profile')

    if public_key is None:
        private, default_kid = _get_admission_signer()
        # When caller expects the default kid, use the configured signer public key.
        if expected_kid not in {default_kid, header.get('kid')}:
            raise ReservationError('no public key for proof kid', code='invalid_proof_kid')
        public_key = private.public_key()
    try:
        public_key.verify(signature, f'{header_b64}.{payload_b64}'.encode('ascii'))
    except Exception as exc:
        raise ReservationError('proof signature invalid', code='invalid_proof_signature') from exc
    return payload


def bind_inbound_proof(
    *,
    expected_profile: str,
    proof_jws: str = '',
    proof_sha256: str = '',
    required: bool = False,
) -> str:
    """Production chokepoint: verify inbound Proof v4 JWS when supplied.

    Prefer ``proof_jws`` (compact EdDSA). On success return the canonical payload
    digest to store. Bare ``proof_sha256`` remains accepted for internal callers
    that already verified out-of-band, but a JWS always wins and is verified.
    """
    jws = (proof_jws or '').strip()
    digest = (proof_sha256 or '').strip()
    if jws:
        payload = verify_proof_envelope(
            jws_compact=jws,
            expected_profile=expected_profile,
        )
        return _sha(payload)
    if required and not digest:
        raise ReservationError(
            f'{expected_profile} proof required (jws or sha256)',
            code='missing_proof',
        )
    return digest


@transaction.atomic
def reserve_quota(
    *,
    idempotency_key: str,
    meter: str,
    meter_version: int = 1,
    maximum_quantity,
    unit: str,
    resource_uid: str,
    billing_account_id: uuid.UUID | str,
    producer_principal: str = 'test-producer',
    product: str = 'hive',
    writer_generation: Optional[int] = None,
    dimensions: Optional[Mapping[str, Any]] = None,
    ttl_seconds: int = DEFAULT_RESERVATION_TTL_SECONDS,
    expected_billing_account_id: Optional[str] = None,
) -> dict:
    """POST /internal/v1/quota-reservations"""
    if not idempotency_key:
        raise ReservationError('Idempotency-Key required', code='missing_idempotency_key')
    qty = _dec(maximum_quantity)
    if qty <= 0:
        raise ReservationError('maximum_quantity must be positive', code='invalid_quantity')

    payload = {
        'meter': meter,
        'meter_version': int(meter_version),
        'maximum_quantity': str(qty),
        'unit': unit,
        'resource_uid': resource_uid,
        'billing_account_id': str(billing_account_id),
        'dimensions': dict(dimensions or {}),
        'producer_principal': producer_principal,
    }
    payload_hash = _sha(payload)

    existing = (
        QuotaReservation.objects.select_for_update()
        .filter(reserve_idempotency_key=idempotency_key, producer_principal=producer_principal)
        .first()
    )
    if existing:
        if existing.reserve_payload_sha256 != payload_hash:
            raise ReservationError(
                'idempotency key reused with different payload',
                code='idempotency_conflict',
                http_status=409,
            )
        return _reservation_response(existing)

    _require_active_writer(product, writer_generation)

    try:
        account = BillingAccount.objects.select_for_update().get(pk=billing_account_id)
    except BillingAccount.DoesNotExist as exc:
        raise ReservationError('billing account not found', code='account_not_found', http_status=404) from exc

    if expected_billing_account_id and str(expected_billing_account_id) != str(account.id):
        raise ReservationError(
            'expected_billing_account_id mismatch',
            code='account_assertion_failed',
            http_status=409,
        )
    if account.collection_status in {
        BillingAccount.CollectionStatus.BARRED,
        BillingAccount.CollectionStatus.CLOSED,
    }:
        raise ReservationError('billing account not admissible', code='account_not_admissible')

    # Pick highest-priority entitlement balance with remaining headroom.
    balances = ordered_entitlement_balances(account, meter_key=meter)
    chosen: Optional[EntitlementBalance] = None
    for bal in balances:
        bal = EntitlementBalance.objects.select_for_update().get(pk=bal.pk)
        if bal.remaining >= qty and bal.period_end_utc > timezone.now():
            chosen = bal
            break

    decision_id = _new_id('dec')
    expires_at = timezone.now() + timedelta(seconds=max(1, min(int(ttl_seconds), 3600)))

    if chosen is None:
        res = QuotaReservation.objects.create(
            decision_id=decision_id,
            reserve_idempotency_key=idempotency_key,
            reserve_payload_sha256=payload_hash,
            billing_account=account,
            meter_key=meter,
            meter_version=int(meter_version),
            unit=unit,
            resource_uid=resource_uid,
            producer_principal=producer_principal,
            dimensions=dict(dimensions or {}),
            original_reserved=qty,
            allowed=False,
            deny_reason='insufficient_entitlement',
            status=QuotaReservation.Status.RELEASED,
            admission_epoch=account.admission_epoch,
            expires_at=expires_at,
            terminal_at=timezone.now(),
            released_unused=qty,
        )
        res.response_snapshot = _reservation_response(res)
        res.save(update_fields=['response_snapshot', 'updated_at'])
        return _reservation_response(res)

    # Reserve headroom on the balance projection.
    chosen.reserved_amount = chosen.reserved_amount + qty
    chosen.available_amount = (
        chosen.granted_amount - chosen.reserved_amount - chosen.consumed_amount
    )
    chosen.save(update_fields=['reserved_amount', 'available_amount', 'updated_at'])

    rate_id = ''
    if chosen.grant.plan_version_id:
        # Prefer a rate linked via catalog if present on grant metadata.
        rate_id = str((chosen.grant.metadata or {}).get('rate_version_id') or '')

    res = QuotaReservation.objects.create(
        decision_id=decision_id,
        reserve_idempotency_key=idempotency_key,
        reserve_payload_sha256=payload_hash,
        billing_account=account,
        entitlement_balance=chosen,
        meter_key=meter,
        meter_version=int(meter_version),
        unit=unit,
        resource_uid=resource_uid,
        producer_principal=producer_principal,
        dimensions=dict(dimensions or {}),
        original_reserved=qty,
        allowed=True,
        status=QuotaReservation.Status.RESERVED,
        entitlement_generation=chosen.reset_generation,
        balance_generation=int(chosen.reset_generation),
        rate_version_id=rate_id,
        admission_epoch=account.admission_epoch,
        expires_at=expires_at,
        server_rated_spend_reserved_inr_micros=0,
    )
    proof = _issue_proof(
        profile=AdmissionProofReceipt.Profile.PREPARED,
        reservation=res,
        payload={
            'reservation_id': str(res.id),
            'decision_id': decision_id,
            'billing_account_id': str(account.id),
            'resource_uid': resource_uid,
            'quantity': str(qty),
            'meter': meter,
            'admission_epoch': account.admission_epoch,
        },
    )
    res.response_snapshot = {
        **_reservation_response(res),
        'prepared_proof_id': proof.receipt_id,
        'prepared_proof_jws': proof.jws_compact,
    }
    res.save(update_fields=['response_snapshot', 'updated_at'])
    return _reservation_response(res)


@transaction.atomic
def start_reservation(
    reservation_id: uuid.UUID | str,
    *,
    idempotency_key: str,
    occurrence_ref: str,
    outbox_ref: str,
    producer_principal: str = 'test-producer',
) -> dict:
    """POST /internal/v1/quota-reservations/{id}/start"""
    if not occurrence_ref or not outbox_ref:
        raise ReservationError('occurrence_ref and outbox_ref required', code='missing_start_bind')
    payload = {
        'reservation_id': str(reservation_id),
        'occurrence_ref': occurrence_ref,
        'outbox_ref': outbox_ref,
        'producer_principal': producer_principal,
    }
    payload_hash = _sha(payload)

    try:
        res = QuotaReservation.objects.select_for_update().get(pk=reservation_id)
    except QuotaReservation.DoesNotExist as exc:
        raise ReservationError('reservation not found', code='not_found', http_status=404) from exc

    if res.start_idempotency_key:
        if res.start_idempotency_key != idempotency_key or res.start_payload_sha256 != payload_hash:
            raise ReservationError(
                'start idempotency conflict', code='idempotency_conflict', http_status=409
            )
        return _reservation_response(res)

    if res.status != QuotaReservation.Status.RESERVED:
        raise ReservationError(
            f'cannot start from status={res.status}',
            code='invalid_state',
            http_status=409,
        )
    if res.expires_at <= timezone.now():
        # Unstarted expiry — release headroom.
        _release_balance_reserved(res, res.original_reserved)
        res.status = QuotaReservation.Status.EXPIRED_UNSTARTED
        res.released_unused = res.original_reserved
        res.terminal_at = timezone.now()
        res.save()
        raise ReservationError('reservation expired before start', code='expired', http_status=409)

    res.status = QuotaReservation.Status.STARTED
    res.started_at = timezone.now()
    res.occurrence_ref = occurrence_ref
    res.outbox_ref = outbox_ref
    res.start_idempotency_key = idempotency_key
    res.start_payload_sha256 = payload_hash
    proof = _issue_proof(
        profile=AdmissionProofReceipt.Profile.START_AUTH,
        reservation=res,
        payload={
            'reservation_id': str(res.id),
            'occurrence_ref': occurrence_ref,
            'outbox_ref': outbox_ref,
            'started_at': res.started_at.isoformat(),
        },
    )
    snap = _reservation_response(res)
    snap['start_proof_id'] = proof.receipt_id
    snap['start_proof_jws'] = proof.jws_compact
    res.response_snapshot = snap
    res.save()
    return snap


def _release_balance_reserved(res: QuotaReservation, amount: Decimal) -> None:
    if not res.entitlement_balance_id or amount <= 0:
        return
    bal = EntitlementBalance.objects.select_for_update().get(pk=res.entitlement_balance_id)
    bal.reserved_amount = max(Decimal('0'), bal.reserved_amount - amount)
    bal.available_amount = bal.granted_amount - bal.reserved_amount - bal.consumed_amount
    bal.save(update_fields=['reserved_amount', 'available_amount', 'updated_at'])


def _consume_balance(res: QuotaReservation, amount: Decimal) -> None:
    if not res.entitlement_balance_id or amount <= 0:
        return
    bal = EntitlementBalance.objects.select_for_update().get(pk=res.entitlement_balance_id)
    # Move from reserved → consumed.
    bal.reserved_amount = max(Decimal('0'), bal.reserved_amount - amount)
    bal.consumed_amount = bal.consumed_amount + amount
    bal.available_amount = bal.granted_amount - bal.reserved_amount - bal.consumed_amount
    bal.save(update_fields=['reserved_amount', 'consumed_amount', 'available_amount', 'updated_at'])


@transaction.atomic
def commit_reservation(
    reservation_id: uuid.UUID | str,
    *,
    idempotency_key: str,
    actual_quantity,
    held_unknown_quantity=0,
    terminal_proof_sha256: str = '',
    terminal_proof_jws: str = '',
    producer_principal: str = 'test-producer',
) -> dict:
    """POST /internal/v1/quota-reservations/{id}/commit — atomic split."""
    actual = _dec(actual_quantity)
    held = _dec(held_unknown_quantity)
    if actual < 0 or held < 0:
        raise ReservationError('quantities must be non-negative', code='invalid_quantity')

    terminal_proof_sha256 = bind_inbound_proof(
        expected_profile=AdmissionProofReceipt.Profile.TERMINAL,
        proof_jws=terminal_proof_jws,
        proof_sha256=terminal_proof_sha256,
        required=False,
    )

    payload = {
        'reservation_id': str(reservation_id),
        'actual_quantity': str(actual),
        'held_unknown_quantity': str(held),
        'terminal_proof_sha256': terminal_proof_sha256,
        'producer_principal': producer_principal,
    }
    payload_hash = _sha(payload)

    try:
        res = QuotaReservation.objects.select_for_update().get(pk=reservation_id)
    except QuotaReservation.DoesNotExist as exc:
        raise ReservationError('reservation not found', code='not_found', http_status=404) from exc

    if res.terminal_idempotency_key:
        if res.terminal_idempotency_key != idempotency_key or res.terminal_payload_sha256 != payload_hash:
            raise ReservationError(
                'terminal idempotency conflict', code='idempotency_conflict', http_status=409
            )
        return _reservation_response(res)

    if res.status not in {QuotaReservation.Status.STARTED, QuotaReservation.Status.ORPHANED_HOLD}:
        raise ReservationError(
            f'cannot commit from status={res.status}',
            code='invalid_state',
            http_status=409,
        )

    released = res.original_reserved - actual - held
    if released < 0:
        raise ReservationError(
            'commit split exceeds original_reserved',
            code='split_overflow',
            http_status=409,
        )

    # Atomic split.
    _consume_balance(res, actual)
    _release_balance_reserved(res, released)
    # held_unknown stays reserved on the balance until later proof.
    # (already counted in reserved_amount; leave it)

    res.committed_actual = actual
    res.released_unused = released
    res.held_unknown = held
    res.terminal_idempotency_key = idempotency_key
    res.terminal_payload_sha256 = payload_hash
    res.terminal_proof_sha256 = terminal_proof_sha256
    if held > 0:
        res.status = QuotaReservation.Status.ORPHANED_HOLD
        res.terminal_at = None
    else:
        res.status = QuotaReservation.Status.COMMITTED
        res.terminal_at = timezone.now()
        # Release any residual reserved that was held at 0.
    if not res.split_ok():
        raise ReservationError('internal split invariant broken', code='split_invariant')

    proof = _issue_proof(
        profile=AdmissionProofReceipt.Profile.TERMINAL,
        reservation=res,
        payload={
            'reservation_id': str(res.id),
            'committed_actual': str(actual),
            'released_unused': str(released),
            'held_unknown': str(held),
            'status': res.status,
        },
    )
    snap = _reservation_response(res)
    snap['terminal_proof_id'] = proof.receipt_id
    snap['terminal_proof_jws'] = proof.jws_compact
    res.response_snapshot = snap
    res.save()
    return snap


def release_reservation(
    reservation_id: uuid.UUID | str,
    *,
    idempotency_key: str,
    no_start_proof_sha256: str = '',
    no_start_proof_jws: str = '',
    producer_principal: str = 'test-producer',
) -> dict:
    """POST /internal/v1/quota-reservations/{id}/release — requires no-start proof if never started.

    Inbound no-start evidence is producer-attested (opaque sha256 or compact JWS).
    When a compact JWS is supplied it is verified as NoSideEffect only if it
    carries our admission kid/alg; otherwise the JWS bytes are hashed as the
    opaque proof digest (producer-signed evidence).
    """
    jws = (no_start_proof_jws or '').strip()
    if jws:
        try:
            bound_no_start = bind_inbound_proof(
                expected_profile=AdmissionProofReceipt.Profile.NO_SIDE_EFFECT,
                proof_jws=jws,
                required=True,
            )
        except ReservationError as exc:
            # Producer-signed evidence is still acceptable as an opaque digest.
            if exc.code in {
                'invalid_proof_kid',
                'invalid_proof_algorithm',
                'invalid_proof_type',
                'invalid_proof_profile',
                'invalid_proof_audience',
            }:
                bound_no_start = _sha({'no_start_proof_jws': jws})
            else:
                raise
    else:
        bound_no_start = (no_start_proof_sha256 or '').strip()
    payload = {
        'reservation_id': str(reservation_id),
        'no_start_proof_sha256': bound_no_start,
        'producer_principal': producer_principal,
    }
    payload_hash = _sha(payload)
    raise_after: Optional[ReservationError] = None

    with transaction.atomic():
        try:
            res = QuotaReservation.objects.select_for_update().get(pk=reservation_id)
        except QuotaReservation.DoesNotExist as exc:
            raise ReservationError('reservation not found', code='not_found', http_status=404) from exc

        if res.terminal_idempotency_key:
            if res.terminal_idempotency_key != idempotency_key or res.terminal_payload_sha256 != payload_hash:
                raise ReservationError(
                    'terminal idempotency conflict', code='idempotency_conflict', http_status=409
                )
            return _reservation_response(res)

        if res.status == QuotaReservation.Status.STARTED:
            # Started work cannot release without terminal commit; mark orphaned hold.
            # Persist BEFORE raising so the hold survives the error path.
            res.status = QuotaReservation.Status.ORPHANED_HOLD
            res.held_unknown = res.original_reserved - res.committed_actual - res.released_unused
            res.terminal_idempotency_key = idempotency_key
            res.terminal_payload_sha256 = payload_hash
            res.response_snapshot = _reservation_response(res)
            res.save()
            raise_after = ReservationError(
                'started reservation cannot release without commit; held as orphaned_hold',
                code='started_requires_commit',
                http_status=409,
            )
        elif res.status != QuotaReservation.Status.RESERVED:
            raise ReservationError(
                f'cannot release from status={res.status}',
                code='invalid_state',
                http_status=409,
            )
        elif not bound_no_start:
            raise ReservationError(
                'no_start_proof_jws or no_start_proof_sha256 required to release unstarted reservation',
                code='missing_no_start_proof',
            )
        else:
            _release_balance_reserved(res, res.original_reserved)
            res.status = QuotaReservation.Status.RELEASED
            res.released_unused = res.original_reserved
            res.committed_actual = Decimal('0')
            res.held_unknown = Decimal('0')
            res.no_start_proof_sha256 = bound_no_start
            res.terminal_idempotency_key = idempotency_key
            res.terminal_payload_sha256 = payload_hash
            res.terminal_at = timezone.now()
            proof = _issue_proof(
                profile=AdmissionProofReceipt.Profile.NO_SIDE_EFFECT,
                reservation=res,
                payload={
                    'reservation_id': str(res.id),
                    'no_start_proof_sha256': bound_no_start,
                },
            )
            snap = _reservation_response(res)
            snap['no_side_effect_proof_id'] = proof.receipt_id
            snap['no_side_effect_proof_jws'] = proof.jws_compact
            res.response_snapshot = snap
            res.save()
            return snap

    if raise_after is not None:
        raise raise_after
    raise ReservationError('release failed', code='release_failed')


@transaction.atomic
def resolve_orphaned_hold(
    reservation_id: uuid.UUID | str,
    *,
    idempotency_key: str,
    actual_quantity=0,
    released_quantity=None,
    terminal_proof_sha256: str = '',
    terminal_proof_jws: str = '',
    producer_principal: str = 'test-producer',
) -> dict:
    """Resolve ORPHANED_HOLD held_unknown into actual/released (P2-2 / PLAT-4484).

    First terminal commit may leave held_unknown reserved forever. This path
    accepts a later terminal proof and finishes the split without re-opening
    the original terminal_idempotency_key (which remains the first terminal
    event's key). Resolve uses response_snapshot['resolve_idempotency_key'].
    """
    if not idempotency_key:
        raise ReservationError('Idempotency-Key required', code='missing_idempotency_key')
    actual = _dec(actual_quantity)
    if actual < 0:
        raise ReservationError('quantities must be non-negative', code='invalid_quantity')

    terminal_proof_sha256 = bind_inbound_proof(
        expected_profile=AdmissionProofReceipt.Profile.TERMINAL,
        proof_jws=terminal_proof_jws,
        proof_sha256=terminal_proof_sha256,
        required=False,
    )

    payload = {
        'reservation_id': str(reservation_id),
        'actual_quantity': str(actual),
        'released_quantity': str(released_quantity) if released_quantity is not None else None,
        'terminal_proof_sha256': terminal_proof_sha256,
        'producer_principal': producer_principal,
        'op': 'resolve_orphaned_hold',
    }
    payload_hash = _sha(payload)

    try:
        res = QuotaReservation.objects.select_for_update().get(pk=reservation_id)
    except QuotaReservation.DoesNotExist as exc:
        raise ReservationError('reservation not found', code='not_found', http_status=404) from exc

    snap = dict(res.response_snapshot or {})
    prior_key = snap.get('resolve_idempotency_key')
    prior_hash = snap.get('resolve_payload_sha256')
    if prior_key:
        if prior_key != idempotency_key or prior_hash != payload_hash:
            raise ReservationError(
                'resolve idempotency conflict', code='idempotency_conflict', http_status=409
            )
        return _reservation_response(res)

    if res.status == QuotaReservation.Status.COMMITTED and res.held_unknown == 0:
        # Already fully resolved — idempotent success if caller retries after success
        # without prior_key (e.g. crash after commit). Require matching proof hash if set.
        return _reservation_response(res)

    if res.status != QuotaReservation.Status.ORPHANED_HOLD:
        raise ReservationError(
            f'cannot resolve orphan from status={res.status}',
            code='invalid_state',
            http_status=409,
        )

    held = _dec(res.held_unknown)
    if released_quantity is None:
        released = held - actual
    else:
        released = _dec(released_quantity)
    if released < 0 or actual + released != held:
        raise ReservationError(
            'resolve split must cover held_unknown exactly (actual+released=held)',
            code='split_overflow',
            http_status=409,
        )

    # held_unknown is still reserved on the balance — consume actual, release rest.
    if actual > 0:
        _consume_balance(res, actual)
    if released > 0:
        _release_balance_reserved(res, released)

    res.committed_actual = _dec(res.committed_actual) + actual
    res.released_unused = _dec(res.released_unused) + released
    res.held_unknown = Decimal('0')
    res.status = QuotaReservation.Status.COMMITTED
    res.terminal_at = timezone.now()
    if terminal_proof_sha256:
        res.terminal_proof_sha256 = terminal_proof_sha256
    if not res.split_ok():
        raise ReservationError('internal split invariant broken', code='split_invariant')

    proof = _issue_proof(
        profile=AdmissionProofReceipt.Profile.TERMINAL,
        reservation=res,
        payload={
            'reservation_id': str(res.id),
            'op': 'resolve_orphaned_hold',
            'committed_actual': str(res.committed_actual),
            'released_unused': str(res.released_unused),
            'held_unknown': '0',
            'status': res.status,
        },
    )
    out = _reservation_response(res)
    out['resolve_idempotency_key'] = idempotency_key
    out['resolve_payload_sha256'] = payload_hash
    out['terminal_proof_id'] = proof.receipt_id
    out['terminal_proof_jws'] = proof.jws_compact
    res.response_snapshot = out
    res.save()
    return out


@transaction.atomic
def expire_unstarted_if_due(reservation_id: uuid.UUID | str) -> dict:
    """Timer path: only unstarted reservations may auto-release (HLD expires_at rule)."""
    res = QuotaReservation.objects.select_for_update().get(pk=reservation_id)
    if res.status != QuotaReservation.Status.RESERVED:
        if res.status == QuotaReservation.Status.STARTED and res.expires_at <= timezone.now():
            res.status = QuotaReservation.Status.ORPHANED_HOLD
            res.held_unknown = res.original_reserved - res.committed_actual - res.released_unused
            res.save(update_fields=['status', 'held_unknown', 'updated_at'])
        return _reservation_response(res)
    if res.expires_at > timezone.now():
        return _reservation_response(res)
    _release_balance_reserved(res, res.original_reserved)
    res.status = QuotaReservation.Status.EXPIRED_UNSTARTED
    res.released_unused = res.original_reserved
    res.terminal_at = timezone.now()
    res.response_snapshot = _reservation_response(res)
    res.save()
    return _reservation_response(res)


# ---------------------------------------------------------------------------
# ProducerHolder + SpendLease
# ---------------------------------------------------------------------------


@transaction.atomic
def boot_producer_holder(*, holder_key: str, incarnation_id: str = '') -> ProducerHolder:
    """Boot/restore/fork: bump epoch and fence old leases."""
    inc = incarnation_id or secrets.token_hex(8)
    holder, created = ProducerHolder.objects.select_for_update().get_or_create(
        holder_key=holder_key,
        defaults={'epoch': 1, 'incarnation_id': inc},
    )
    if not created:
        holder.epoch = holder.epoch + 1
        holder.incarnation_id = inc
        holder.save(update_fields=['epoch', 'incarnation_id', 'updated_at', 'last_heartbeat_at'])
        # Fence leases issued under prior epochs.
        SpendLease.objects.filter(
            producer_principal=holder_key,
            producer_holder_epoch__lt=holder.epoch,
            status=SpendLease.Status.ACTIVE,
        ).update(status=SpendLease.Status.FENCED, updated_at=timezone.now())
    return holder


@transaction.atomic
def issue_spend_lease(
    *,
    idempotency_key: str,
    billing_account_id: uuid.UUID | str,
    resource_uid: str,
    meter_key: str,
    quantity,
    producer_principal: str,
    parent_reservation_id: Optional[str] = None,
    ttl_seconds: int = MAX_LEASE_SECONDS,
) -> dict:
    qty = _dec(quantity)
    if qty <= 0:
        raise ReservationError('lease quantity must be positive', code='invalid_quantity')
    ttl = max(1, min(int(ttl_seconds), MAX_LEASE_SECONDS))
    payload = {
        'billing_account_id': str(billing_account_id),
        'resource_uid': resource_uid,
        'meter_key': meter_key,
        'quantity': str(qty),
        'producer_principal': producer_principal,
        'parent_reservation_id': parent_reservation_id or '',
        'ttl_seconds': ttl,
    }
    payload_hash = _sha(payload)

    existing = (
        SpendLease.objects.select_for_update()
        .filter(issue_idempotency_key=idempotency_key, producer_principal=producer_principal)
        .first()
    )
    if existing:
        if existing.issue_payload_sha256 != payload_hash:
            raise ReservationError(
                'lease idempotency conflict', code='idempotency_conflict', http_status=409
            )
        return existing.response_snapshot or _lease_response(existing)

    holder = (
        ProducerHolder.objects.select_for_update()
        .filter(holder_key=producer_principal)
        .first()
    )
    if holder is None:
        holder = boot_producer_holder(holder_key=producer_principal)

    account = BillingAccount.objects.select_for_update().get(pk=billing_account_id)
    parent = None
    if parent_reservation_id:
        parent = QuotaReservation.objects.select_for_update().get(pk=parent_reservation_id)
        if parent.status not in {
            QuotaReservation.Status.RESERVED,
            QuotaReservation.Status.STARTED,
        }:
            raise ReservationError('parent reservation not active', code='invalid_parent')

    lease = SpendLease.objects.create(
        lease_id=_new_id('lease'),
        parent_reservation=parent,
        billing_account=account,
        resource_uid=resource_uid,
        producer_principal=producer_principal,
        producer_holder_epoch=holder.epoch,
        meter_key=meter_key,
        authorized_quantity=qty,
        status=SpendLease.Status.ACTIVE,
        lease_kid='metering-lease-v1',
        issue_idempotency_key=idempotency_key,
        issue_payload_sha256=payload_hash,
        expires_at=timezone.now() + timedelta(seconds=ttl),
    )
    # Sign lease envelope with distinct kid from close (HLD separate trust roots).
    proof = _issue_proof(
        profile='spend_lease_issue',
        lease=lease,
        kid='metering-lease-v1',
        payload={
            'lease_id': lease.lease_id,
            'quantity': str(qty),
            'expires_at': lease.expires_at.isoformat(),
            'holder_epoch': holder.epoch,
            'aud': 'metering:lease',
            'profile': 'spend_lease_issue',
        },
    )
    lease.lease_jws = proof.jws_compact
    snap = _lease_response(lease)
    snap['lease_proof_jws'] = proof.jws_compact
    lease.response_snapshot = snap
    lease.save(update_fields=['lease_jws', 'response_snapshot', 'updated_at'])
    return snap


def _lease_response(lease: SpendLease) -> dict:
    return {
        'lease_id': lease.lease_id,
        'status': lease.status,
        'billing_account_id': str(lease.billing_account_id),
        'resource_uid': lease.resource_uid,
        'meter_key': lease.meter_key,
        'authorized_quantity': _qty_str(lease.authorized_quantity),
        'debited_quantity': _qty_str(lease.debited_quantity),
        'producer_holder_epoch': lease.producer_holder_epoch,
        'expires_at': lease.expires_at.isoformat().replace('+00:00', 'Z'),
        'parent_reservation_id': str(lease.parent_reservation_id) if lease.parent_reservation_id else None,
        'lease_kid': lease.lease_kid,
        'close_kid': lease.close_kid,
    }


@transaction.atomic
def close_spend_lease(
    lease_id: str,
    *,
    idempotency_key: str,
    debited_quantity,
    producer_principal: str,
) -> dict:
    debited = _dec(debited_quantity)
    payload = {
        'lease_id': lease_id,
        'debited_quantity': str(debited),
        'producer_principal': producer_principal,
    }
    payload_hash = _sha(payload)

    try:
        lease = SpendLease.objects.select_for_update().get(lease_id=lease_id)
    except SpendLease.DoesNotExist as exc:
        raise ReservationError('lease not found', code='not_found', http_status=404) from exc

    if lease.close_idempotency_key:
        if lease.close_idempotency_key != idempotency_key or lease.close_payload_sha256 != payload_hash:
            raise ReservationError(
                'close idempotency conflict', code='idempotency_conflict', http_status=409
            )
        return lease.response_snapshot or _lease_response(lease)

    if lease.status == SpendLease.Status.FENCED:
        raise ReservationError('lease fenced by holder epoch', code='lease_fenced', http_status=409)
    if lease.status != SpendLease.Status.ACTIVE:
        raise ReservationError(f'cannot close status={lease.status}', code='invalid_state', http_status=409)

    holder = ProducerHolder.objects.filter(holder_key=producer_principal).first()
    if holder and lease.producer_holder_epoch != holder.epoch:
        lease.status = SpendLease.Status.FENCED
        lease.save(update_fields=['status', 'updated_at'])
        raise ReservationError('lease epoch mismatch', code='lease_fenced', http_status=409)

    if debited > lease.authorized_quantity:
        raise ReservationError('debit exceeds authorized', code='debit_overflow')

    lease.debited_quantity = debited
    lease.status = SpendLease.Status.CLOSED
    lease.closed_at = timezone.now()
    lease.close_idempotency_key = idempotency_key
    lease.close_payload_sha256 = payload_hash
    lease.close_kid = 'metering-lease-close-v1'
    proof = _issue_proof(
        profile='spend_lease_close',
        lease=lease,
        kid='metering-lease-close-v1',
        payload={
            'lease_id': lease.lease_id,
            'debited_quantity': str(debited),
            'aud': 'metering:lease-close',
            'profile': 'spend_lease_close',
        },
    )
    lease.close_jws = proof.jws_compact
    snap = _lease_response(lease)
    snap['close_proof_jws'] = proof.jws_compact
    lease.response_snapshot = snap
    lease.save()
    return snap


# ---------------------------------------------------------------------------
# AdmissionWriterAuthority cutover
# ---------------------------------------------------------------------------


@transaction.atomic
def cutover_admission_writer(
    *,
    product: str,
    action: str,
    source_watermark: str = '',
    cutover_manifest_sha256: str = '',
) -> dict:
    """freeze | import | activate for one product writer generation."""
    action = action.lower().strip()
    active = (
        AdmissionWriterAuthority.objects.select_for_update()
        .filter(product=product, state=AdmissionWriterAuthority.State.ACTIVE)
        .order_by('-generation')
        .first()
    )

    if action == 'freeze':
        if active is None:
            active = AdmissionWriterAuthority.objects.create(
                product=product,
                generation=1,
                state=AdmissionWriterAuthority.State.ACTIVE,
                activated_at=timezone.now(),
            )
        active.state = AdmissionWriterAuthority.State.FREEZING
        active.source_watermark = source_watermark or active.source_watermark
        active.save(update_fields=['state', 'source_watermark', 'updated_at'])
        return _writer_response(active)

    if action == 'import':
        target = (
            AdmissionWriterAuthority.objects.select_for_update()
            .filter(product=product, state=AdmissionWriterAuthority.State.FREEZING)
            .order_by('-generation')
            .first()
        )
        if target is None:
            raise ReservationError('no freezing writer to import', code='invalid_cutover_state')
        target.state = AdmissionWriterAuthority.State.IMPORTING
        target.source_watermark = source_watermark or target.source_watermark
        target.cutover_manifest_sha256 = cutover_manifest_sha256 or target.cutover_manifest_sha256
        target.save(
            update_fields=['state', 'source_watermark', 'cutover_manifest_sha256', 'updated_at']
        )
        return _writer_response(target)

    if action == 'activate':
        importing = (
            AdmissionWriterAuthority.objects.select_for_update()
            .filter(product=product, state=AdmissionWriterAuthority.State.IMPORTING)
            .order_by('-generation')
            .first()
        )
        if importing is None:
            raise ReservationError('no importing writer to activate', code='invalid_cutover_state')
        # Retire any still-active peers (should be none under constraint).
        AdmissionWriterAuthority.objects.filter(
            product=product, state=AdmissionWriterAuthority.State.ACTIVE
        ).exclude(pk=importing.pk).update(
            state=AdmissionWriterAuthority.State.RETIRED,
            retired_at=timezone.now(),
            updated_at=timezone.now(),
        )
        new_gen = importing.generation + 1
        # Retire the importing row and create the new active generation.
        importing.state = AdmissionWriterAuthority.State.RETIRED
        importing.retired_at = timezone.now()
        importing.save(update_fields=['state', 'retired_at', 'updated_at'])
        activated = AdmissionWriterAuthority.objects.create(
            product=product,
            generation=new_gen,
            state=AdmissionWriterAuthority.State.ACTIVE,
            source_watermark=source_watermark or importing.source_watermark,
            cutover_manifest_sha256=cutover_manifest_sha256 or importing.cutover_manifest_sha256,
            activated_at=timezone.now(),
        )
        return _writer_response(activated)

    raise ReservationError(f'unknown cutover action={action}', code='invalid_cutover_action')


def _writer_response(row: AdmissionWriterAuthority) -> dict:
    return {
        'product': row.product,
        'generation': row.generation,
        'state': row.state,
        'source_watermark': row.source_watermark,
        'cutover_manifest_sha256': row.cutover_manifest_sha256,
        'activated_at': row.activated_at.isoformat().replace('+00:00', 'Z') if row.activated_at else None,
        'retired_at': row.retired_at.isoformat().replace('+00:00', 'Z') if row.retired_at else None,
    }
