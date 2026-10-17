"""Wave 2 metering: UsageRollup rebuild, rating, BillingStatement lifecycle (PLAT-4485).

Authority boundaries (HLD §8 / ADR-0022):
- Metering owns measured/rated quantity and customer gross rated debit.
- Books owns SAC, taxable value, GST split, cash/GL.
- statement_id is deterministic from the statement business key.
- Exactly one ordinary statement per business key; adjustments extend the key
  with original_statement_id + adjustment_sequence.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as datetime_timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, ROUND_HALF_UP
from typing import Iterable, Mapping, Optional, Sequence
from uuid import UUID, uuid4
import hashlib
import json

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from .metering_core import MeteringCoreError, ordered_entitlement_balances
from .metering_models import (
    BillingAccount,
    BillingStatement,
    RateVersion,
    RatedUsageLine,
    SpendAllocation,
    UsageRollup,
    canonical_sha256,
)
from hive_platform.models import UsageEvent


class StatementError(MeteringCoreError):
    code = 'statement_error'


class PeriodLagError(StatementError):
    code = 'period_lag_blocked'


class StatementStateError(StatementError):
    code = 'invalid_statement_transition'


# open → closing → rated → exported_draft → finalized
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    BillingStatement.Status.OPEN: {BillingStatement.Status.CLOSING},
    BillingStatement.Status.CLOSING: {
        BillingStatement.Status.RATED,
        BillingStatement.Status.OPEN,  # reopen if lag forces abort before rate
    },
    BillingStatement.Status.RATED: {BillingStatement.Status.EXPORTED_DRAFT},
    BillingStatement.Status.EXPORTED_DRAFT: {
        BillingStatement.Status.FINALIZED,
        BillingStatement.Status.RATED,  # supersede draft pre-finalization
    },
    BillingStatement.Status.FINALIZED: set(),
}

DEFAULT_CLOSING_GRACE = timedelta(hours=6)


def _dt(value) -> datetime:
    if isinstance(value, datetime):
        if settings.USE_TZ:
            if timezone.is_naive(value):
                return timezone.make_aware(value, datetime_timezone.utc)
            return value.astimezone(datetime_timezone.utc)
        if timezone.is_aware(value):
            return value.astimezone(datetime_timezone.utc).replace(tzinfo=None)
        return value
    raise StatementError(f'expected datetime, got {type(value)!r}')


def compute_statement_id(
    *,
    billing_account_id: UUID | str,
    seller_legal_entity_id: str,
    seller_gst_registration_id: str,
    books_organization_id: str,
    currency: str,
    period_start: datetime,
    period_end: datetime,
    statement_kind: str,
    adjustment_sequence: int | None = None,
    original_statement_id: str = '',
) -> str:
    """Deterministic statement_id from the HLD §8 business key."""
    payload = {
        'billing_account_id': str(billing_account_id),
        'seller_legal_entity_id': seller_legal_entity_id or '',
        'seller_gst_registration_id': seller_gst_registration_id or '',
        'books_organization_id': books_organization_id or '',
        'currency': (currency or 'INR').upper(),
        'period_start': _dt(period_start).isoformat(),
        'period_end': _dt(period_end).isoformat(),
        'statement_kind': statement_kind,
        'adjustment_sequence': adjustment_sequence,
        'original_statement_id': original_statement_id or '',
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
    ).hexdigest()
    return digest[:32]


def _dimensions_hash(dimensions: Mapping | None) -> str:
    if not dimensions:
        return canonical_sha256({})
    return canonical_sha256(dict(dimensions))


def _round_quantity(value: Decimal, mode: str) -> Decimal:
    quant = Decimal('0.000001')
    mapping = {
        RateVersion.Rounding.HALF_UP: ROUND_HALF_UP,
        RateVersion.Rounding.CEILING: ROUND_CEILING,
        RateVersion.Rounding.FLOOR: ROUND_FLOOR,
        RateVersion.Rounding.BANKERS: ROUND_HALF_EVEN,
    }
    return value.quantize(quant, rounding=mapping.get(mode, ROUND_HALF_UP))


def _rate_micros(quantity: Decimal, rate: RateVersion) -> int:
    q = _round_quantity(Decimal(quantity), rate.rounding)
    if rate.minimum_quantum and q > 0 and q < rate.minimum_quantum:
        q = Decimal(rate.minimum_quantum)
    micros = int((q * Decimal(rate.inr_micros_per_unit)).to_integral_value(rounding=ROUND_HALF_UP))
    return max(0, micros)


# ---------------------------------------------------------------------------
# UsageRollup rebuild
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RollupRebuildResult:
    rollups: list[UsageRollup]
    source_manifest_hash: str
    event_count: int
    generation: int


def rebuild_usage_rollups(
    *,
    billing_account: BillingAccount,
    period_start: datetime,
    period_end: datetime,
    required_producer_watermarks: Mapping[str, str] | None = None,
) -> RollupRebuildResult:
    """Rebuild UsageRollup rows from raw UsageEvent for [period_start, period_end).

    Half-open UTC interval. Producer watermarks are recorded; callers that need
    lag fencing pass required_producer_watermarks and check lag separately.
    """
    start = _dt(period_start)
    end = _dt(period_end)
    if end <= start:
        raise StatementError('period_end must be after period_start')

    events = list(
        UsageEvent.objects.filter(
            billing_account_id=str(billing_account.id),
            occurred_at__gte=start,
            occurred_at__lt=end,
        ).order_by('occurred_at', 'id')
    )

    # Group by meter/unit/dimensions
    buckets: dict[tuple, dict] = {}
    event_ids: list[str] = []
    watermarks: dict[str, str] = {}
    for ev in events:
        dims = {}
        if isinstance(ev.metadata, dict):
            dims = ev.metadata.get('dimensions') or {}
        if not isinstance(dims, dict):
            dims = {}
        dhash = _dimensions_hash(dims)
        key = (ev.metric, 1, ev.unit, dhash)
        bucket = buckets.setdefault(
            key,
            {
                'meter_key': ev.metric,
                'meter_version': 1,
                'unit': ev.unit,
                'dimensions_hash': dhash,
                'dimensions': dims,
                'gross': Decimal('0'),
                'count': 0,
            },
        )
        bucket['gross'] += Decimal(ev.quantity)
        bucket['count'] += 1
        event_ids.append(f'{ev.source}:{ev.event_id or ev.id}')
        # Per-source watermark = latest occurred_at iso
        prev = watermarks.get(ev.source)
        stamp = ev.occurred_at.isoformat()
        if prev is None or stamp > prev:
            watermarks[ev.source] = stamp

    if required_producer_watermarks:
        watermarks = {**required_producer_watermarks, **watermarks}

    manifest = canonical_sha256(
        {
            'billing_account_id': str(billing_account.id),
            'period_start': start.isoformat(),
            'period_end': end.isoformat(),
            'event_ids': sorted(event_ids),
            'watermarks': watermarks,
        }
    )

    # Next generation for this period window
    prior = (
        UsageRollup.objects.filter(
            billing_account=billing_account,
            period_start=start,
            period_end=end,
        )
        .order_by('-generation')
        .values_list('generation', flat=True)
        .first()
    )
    generation = int(prior or 0) + 1

    created: list[UsageRollup] = []
    with transaction.atomic():
        for bucket in buckets.values():
            row = UsageRollup.objects.create(
                billing_account=billing_account,
                period_start=start,
                period_end=end,
                meter_key=bucket['meter_key'],
                meter_version=bucket['meter_version'],
                unit=bucket['unit'],
                dimensions_hash=bucket['dimensions_hash'],
                dimensions=bucket['dimensions'],
                gross_quantity=bucket['gross'],
                generation=generation,
                source_manifest_hash=manifest,
                event_count=bucket['count'],
                producer_watermarks=watermarks,
            )
            created.append(row)
        # Empty period still records a zero-event generation marker via empty list
        # — callers rely on source_manifest_hash + generation on the statement.

    return RollupRebuildResult(
        rollups=created,
        source_manifest_hash=manifest,
        event_count=len(events),
        generation=generation,
    )


def check_producer_lag(
    *,
    rollups: Sequence[UsageRollup],
    required_sources: Mapping[str, str],
) -> list[str]:
    """Return list of lagging source names (missing or watermark behind required)."""
    if not required_sources:
        return []
    combined: dict[str, str] = {}
    for r in rollups:
        for src, wm in (r.producer_watermarks or {}).items():
            prev = combined.get(src)
            if prev is None or wm > prev:
                combined[src] = wm
    lagging = []
    for src, required_wm in required_sources.items():
        have = combined.get(src)
        if have is None or have < required_wm:
            lagging.append(src)
    return lagging


# ---------------------------------------------------------------------------
# Rating: gross → included (free grant) → billable
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RatedLineSpec:
    rollup: UsageRollup
    gross_quantity: Decimal
    included_quantity: Decimal
    billable_quantity: Decimal
    rate: RateVersion | None
    unit_rate_inr_micros: int
    customer_gross_rated_debit_inr_micros: int
    promotion_facts: dict
    line_key: str


def rate_rollups(
    *,
    billing_account: BillingAccount,
    rollups: Sequence[UsageRollup],
    rate_lookup: Mapping[str, RateVersion] | None = None,
) -> list[RatedLineSpec]:
    """Apply free-grant inclusion before spend-credit billable rating.

    Order (HLD §10): free grants consumed before spend credit. Cap/rate and
    funds are independent; this function only derives included vs billable
    quantities and the customer gross rated debit from RateVersion.
    """
    # Remaining free grant per meter_key for this rating pass
    remaining_free: dict[str, Decimal] = {}
    for bal in ordered_entitlement_balances(billing_account):
        grant = bal.grant
        if grant is None:
            continue
        meter_key = ''
        if grant.meter_definition_id:
            meter_key = grant.meter_definition.key
        elif grant.cap_key:
            meter_key = grant.cap_key
        if not meter_key:
            continue
        remaining_free[meter_key] = remaining_free.get(meter_key, Decimal('0')) + Decimal(
            bal.remaining
        )

    rates = dict(rate_lookup or {})
    if not rates:
        # Latest effective rate per meter_key for account currency
        for rv in RateVersion.objects.filter(currency=billing_account.currency).select_related(
            'meter_definition'
        ).order_by('meter_definition__key', '-version'):
            key = rv.meter_definition.key
            rates.setdefault(key, rv)

    specs: list[RatedLineSpec] = []
    for rollup in rollups:
        gross = Decimal(rollup.gross_quantity)
        free_left = remaining_free.get(rollup.meter_key, Decimal('0'))
        included = min(gross, free_left) if free_left > 0 else Decimal('0')
        billable = gross - included
        if free_left > 0:
            remaining_free[rollup.meter_key] = free_left - included

        rate = rates.get(rollup.meter_key)
        if billable > 0 and rate is None:
            raise StatementError(
                f'no RateVersion for billable meter {rollup.meter_key!r}'
            )
        unit_micros = int(rate.inr_micros_per_unit) if rate else 0
        debit = _rate_micros(billable, rate) if rate and billable > 0 else 0

        line_key = canonical_sha256(
            {
                'rollup_id': str(rollup.id),
                'meter_key': rollup.meter_key,
                'meter_version': rollup.meter_version,
                'dimensions_hash': rollup.dimensions_hash,
                'generation': rollup.generation,
            }
        )[:40]

        specs.append(
            RatedLineSpec(
                rollup=rollup,
                gross_quantity=gross,
                included_quantity=included,
                billable_quantity=billable,
                rate=rate,
                unit_rate_inr_micros=unit_micros,
                customer_gross_rated_debit_inr_micros=debit,
                promotion_facts={
                    'included_from_free_grant': str(included),
                    'rating_order': 'free_grant_then_spend_credit',
                },
                line_key=f'rul_{line_key}',
            )
        )
    return specs


# ---------------------------------------------------------------------------
# BillingStatement lifecycle
# ---------------------------------------------------------------------------


def open_or_get_statement(
    *,
    billing_account: BillingAccount,
    period_start: datetime,
    period_end: datetime,
    seller_legal_entity_id: str = '',
    seller_gst_registration_id: str = '',
    books_organization_id: str = '',
    currency: str | None = None,
    statement_kind: str = BillingStatement.Kind.ORDINARY,
    adjustment_sequence: int | None = None,
    original_statement_id: str = '',
) -> BillingStatement:
    cur = (currency or billing_account.currency or 'INR').upper()
    start = _dt(period_start)
    end = _dt(period_end)
    if end <= start:
        raise StatementError('period_end must be after period_start')
    if statement_kind == BillingStatement.Kind.ADJUSTMENT:
        if not original_statement_id:
            raise StatementError('adjustment requires original_statement_id')
        if adjustment_sequence is None or adjustment_sequence < 1:
            raise StatementError('adjustment requires monotonic adjustment_sequence >= 1')
    else:
        adjustment_sequence = None
        original_statement_id = ''

    sid = compute_statement_id(
        billing_account_id=billing_account.id,
        seller_legal_entity_id=seller_legal_entity_id,
        seller_gst_registration_id=seller_gst_registration_id,
        books_organization_id=books_organization_id,
        currency=cur,
        period_start=start,
        period_end=end,
        statement_kind=statement_kind,
        adjustment_sequence=adjustment_sequence,
        original_statement_id=original_statement_id,
    )
    existing = BillingStatement.objects.filter(statement_id=sid).first()
    if existing:
        return existing
    try:
        return BillingStatement.objects.create(
            statement_id=sid,
            billing_account=billing_account,
            seller_legal_entity_id=seller_legal_entity_id or '',
            seller_gst_registration_id=seller_gst_registration_id or '',
            books_organization_id=books_organization_id or '',
            currency=cur,
            period_start=start,
            period_end=end,
            statement_kind=statement_kind,
            adjustment_sequence=adjustment_sequence,
            original_statement_id=original_statement_id or '',
            status=BillingStatement.Status.OPEN,
        )
    except IntegrityError:
        return BillingStatement.objects.get(statement_id=sid)


def _transition(statement: BillingStatement, to_status: str, **fields) -> BillingStatement:
    allowed = _ALLOWED_TRANSITIONS.get(statement.status, set())
    if to_status not in allowed:
        raise StatementStateError(
            f'cannot transition statement {statement.statement_id} '
            f'{statement.status} → {to_status}'
        )
    statement.status = to_status
    for k, v in fields.items():
        setattr(statement, k, v)
    statement.save()
    return statement


def begin_closing(
    statement: BillingStatement,
    *,
    grace: timedelta = DEFAULT_CLOSING_GRACE,
    now: datetime | None = None,
) -> BillingStatement:
    if statement.status != BillingStatement.Status.OPEN:
        raise StatementStateError('begin_closing requires open statement')
    ts = now or timezone.now()
    return _transition(
        statement,
        BillingStatement.Status.CLOSING,
        closing_grace_until=ts + grace,
        closed_at=ts,
    )


def rate_statement(
    statement: BillingStatement,
    *,
    required_producer_watermarks: Mapping[str, str] | None = None,
    rate_lookup: Mapping[str, RateVersion] | None = None,
    force_override_lag_sources: Sequence[str] | None = None,
) -> BillingStatement:
    """Rebuild rollups, fence lag, rate lines, move closing → rated."""
    if statement.status not in {
        BillingStatement.Status.CLOSING,
        BillingStatement.Status.OPEN,
    }:
        raise StatementStateError('rate_statement requires open or closing statement')

    with transaction.atomic():
        if statement.status == BillingStatement.Status.OPEN:
            begin_closing(statement)

        statement.refresh_from_db()
        rebuild = rebuild_usage_rollups(
            billing_account=statement.billing_account,
            period_start=statement.period_start,
            period_end=statement.period_end,
            required_producer_watermarks=required_producer_watermarks,
        )

        lagging = check_producer_lag(
            rollups=rebuild.rollups,
            required_sources=required_producer_watermarks or {},
        )
        override = set(force_override_lag_sources or [])
        blocked = [s for s in lagging if s not in override]
        if blocked:
            statement.lag_blocked = True
            statement.lag_sources = blocked
            statement.save(update_fields=['lag_blocked', 'lag_sources', 'updated_at'])
            raise PeriodLagError(
                f'period lag blocks export; missing/behind sources: {", ".join(blocked)}'
            )

        specs = rate_rollups(
            billing_account=statement.billing_account,
            rollups=rebuild.rollups,
            rate_lookup=rate_lookup,
        )

        # Replace rated lines for this generation
        RatedUsageLine.objects.filter(statement=statement).delete()
        for spec in specs:
            RatedUsageLine.objects.create(
                statement=statement,
                rollup=spec.rollup,
                billing_account=statement.billing_account,
                line_key=spec.line_key,
                product_sku=spec.rollup.meter_key,
                meter_key=spec.rollup.meter_key,
                meter_version=spec.rollup.meter_version,
                unit=spec.rollup.unit,
                dimensions=spec.rollup.dimensions,
                dimensions_hash=spec.rollup.dimensions_hash,
                gross_quantity=spec.gross_quantity,
                included_quantity=spec.included_quantity,
                billable_quantity=spec.billable_quantity,
                rate_version=spec.rate,
                unit_rate_inr_micros=spec.unit_rate_inr_micros,
                customer_gross_rated_debit_inr_micros=spec.customer_gross_rated_debit_inr_micros,
                promotion_facts=spec.promotion_facts,
                allocation_facts={},
                generation=rebuild.generation,
            )

        statement.source_manifest_hash = rebuild.source_manifest_hash
        statement.rollup_ids = [str(r.id) for r in rebuild.rollups]
        statement.generation = rebuild.generation
        statement.lag_blocked = False
        statement.lag_sources = []
        statement.rated_at = timezone.now()
        if statement.status == BillingStatement.Status.CLOSING:
            _transition(statement, BillingStatement.Status.RATED)
        else:
            statement.status = BillingStatement.Status.RATED
            statement.save()
        return statement


def build_books_draft_payload(statement: BillingStatement) -> dict:
    """Source-neutral Metering → Books draft body (no SAC/tax/GST)."""
    lines = []
    for line in statement.rated_lines.order_by('line_key'):
        lines.append(
            {
                'line_key': line.line_key,
                'product_sku': line.product_sku or line.meter_key,
                'meter_key': line.meter_key,
                'meter_version': line.meter_version,
                'unit': line.unit,
                'dimensions': line.dimensions,
                'gross_quantity': str(line.gross_quantity),
                'included_quantity': str(line.included_quantity),
                'billable_quantity': str(line.billable_quantity),
                'customer_gross_rated_debit_inr_micros': line.customer_gross_rated_debit_inr_micros,
                'unit_rate_inr_micros': line.unit_rate_inr_micros,
                'rate_version_id': str(line.rate_version_id) if line.rate_version_id else None,
                'promotion_facts': line.promotion_facts,
                'allocation_facts': line.allocation_facts,
                # Explicitly absent — Books owns these:
                # sac_code, taxable_value, gst_split
            }
        )
    return {
        'statement_id': statement.statement_id,
        'revision': statement.revision,
        'statement_kind': statement.statement_kind,
        'adjustment_sequence': statement.adjustment_sequence,
        'original_statement_id': statement.original_statement_id or None,
        'billing_account_id': str(statement.billing_account_id),
        'seller_legal_entity_id': statement.seller_legal_entity_id,
        'seller_gst_registration_id': statement.seller_gst_registration_id,
        'books_organization_id': statement.books_organization_id,
        'currency': statement.currency,
        'period_start': statement.period_start.isoformat(),
        'period_end': statement.period_end.isoformat(),
        'source_manifest_hash': statement.source_manifest_hash,
        'rollup_ids': statement.rollup_ids,
        'generation': statement.generation,
        'lines': lines,
    }


def export_statement_draft(
    statement: BillingStatement,
    *,
    idempotency_key: str = '',
    if_match: str = '',
) -> dict:
    """Mark rated → exported_draft and return the Books draft payload.

    Idempotent on statement_id+revision: re-export returns the same payload
    when already exported_draft with matching etag.
    """
    if statement.status == BillingStatement.Status.FINALIZED:
        raise StatementStateError('finalized statements cannot be re-exported; use adjustment')
    if statement.status == BillingStatement.Status.EXPORTED_DRAFT:
        if if_match and if_match != statement.books_draft_etag:
            raise StatementError('If-Match does not match current draft etag')
        payload = build_books_draft_payload(statement)
        payload['books_draft_etag'] = statement.books_draft_etag
        payload['idempotent_replay'] = True
        return payload
    if statement.status != BillingStatement.Status.RATED:
        raise StatementStateError('export requires rated statement')
    if statement.lag_blocked:
        raise PeriodLagError('cannot export while lag_blocked')

    if if_match and statement.books_draft_etag and if_match != statement.books_draft_etag:
        raise StatementError('If-Match does not match current draft etag')

    payload = build_books_draft_payload(statement)
    etag = canonical_sha256(payload)[:32]
    payload['books_draft_etag'] = etag
    payload['idempotency_key'] = idempotency_key or f'statement:{statement.statement_id}:r{statement.revision}'
    payload['idempotent_replay'] = False

    _transition(
        statement,
        BillingStatement.Status.EXPORTED_DRAFT,
        books_draft_etag=etag,
        exported_at=timezone.now(),
    )
    return payload


def finalize_statement(
    statement: BillingStatement,
    *,
    books_document_ref: str = '',
) -> BillingStatement:
    if statement.status != BillingStatement.Status.EXPORTED_DRAFT:
        raise StatementStateError('finalize requires exported_draft')
    return _transition(
        statement,
        BillingStatement.Status.FINALIZED,
        books_document_ref=books_document_ref or statement.books_document_ref,
        finalized_at=timezone.now(),
    )


def open_adjustment_statement(
    original: BillingStatement,
    *,
    reason: str = '',
) -> BillingStatement:
    """Create next adjustment statement for a finalized ordinary/prior adjustment."""
    if original.status != BillingStatement.Status.FINALIZED:
        raise StatementStateError('adjustments only for finalized statements')
    root_id = original.original_statement_id or original.statement_id
    # Monotonic sequence across the original root
    last = (
        BillingStatement.objects.filter(
            original_statement_id=root_id,
            statement_kind=BillingStatement.Kind.ADJUSTMENT,
        )
        .order_by('-adjustment_sequence')
        .values_list('adjustment_sequence', flat=True)
        .first()
    )
    seq = int(last or 0) + 1
    adj = open_or_get_statement(
        billing_account=original.billing_account,
        period_start=original.period_start,
        period_end=original.period_end,
        seller_legal_entity_id=original.seller_legal_entity_id,
        seller_gst_registration_id=original.seller_gst_registration_id,
        books_organization_id=original.books_organization_id,
        currency=original.currency,
        statement_kind=BillingStatement.Kind.ADJUSTMENT,
        adjustment_sequence=seq,
        original_statement_id=root_id,
    )
    if reason:
        meta = dict(adj.metadata or {})
        meta['adjustment_reason'] = reason
        meta['adjusted_from_statement_id'] = original.statement_id
        adj.metadata = meta
        adj.save(update_fields=['metadata', 'updated_at'])
    return adj


# ---------------------------------------------------------------------------
# SpendAllocation identity + cumulative fence
# ---------------------------------------------------------------------------


def create_spend_allocation(
    *,
    statement: BillingStatement,
    source_books_posting_or_lot_slice: str,
    spend_allocation_id: str,
    amount_inr_micros: int,
    line_key: str = '',
    gateway_merchant_account: str = '',
    source_slice_cap_inr_micros: int | None = None,
) -> SpendAllocation:
    if amount_inr_micros < 0:
        raise StatementError('amount_inr_micros must be non-negative')
    if not source_books_posting_or_lot_slice or not spend_allocation_id:
        raise StatementError('source slice and spend_allocation_id are required')

    with transaction.atomic():
        if source_slice_cap_inr_micros is not None:
            applied = (
                SpendAllocation.objects.filter(
                    source_books_posting_or_lot_slice=source_books_posting_or_lot_slice,
                ).aggregate(total=Sum('amount_inr_micros'))['total']
                or 0
            )
            if int(applied) + int(amount_inr_micros) > int(source_slice_cap_inr_micros):
                raise StatementError(
                    'cumulative spend allocation exceeds source slice cap'
                )

        existing = SpendAllocation.objects.filter(
            source_books_posting_or_lot_slice=source_books_posting_or_lot_slice,
            spend_allocation_id=spend_allocation_id,
        ).first()
        if existing:
            if existing.amount_inr_micros != amount_inr_micros:
                raise StatementError(
                    'spend_allocation_id already exists with a different amount'
                )
            return existing

        canonical = canonical_sha256(
            {
                'source': source_books_posting_or_lot_slice,
                'spend_allocation_id': spend_allocation_id,
                'statement_id': statement.statement_id,
                'revision': statement.revision,
                'line_key': line_key,
                'amount_inr_micros': amount_inr_micros,
            }
        )
        try:
            return SpendAllocation.objects.create(
                spend_allocation_id=spend_allocation_id,
                source_books_posting_or_lot_slice=source_books_posting_or_lot_slice,
                statement=statement,
                statement_revision=statement.revision,
                line_key=line_key,
                amount_inr_micros=amount_inr_micros,
                currency=statement.currency,
                gateway_merchant_account=gateway_merchant_account,
                canonical_hash=canonical,
            )
        except IntegrityError:
            return SpendAllocation.objects.get(
                source_books_posting_or_lot_slice=source_books_posting_or_lot_slice,
                spend_allocation_id=spend_allocation_id,
            )
