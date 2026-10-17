"""Usage metering and billing helpers for HIVE-319.

The module is deliberately unit-agnostic: pricing is data passed in by Product /
Analytics, while raw usage events and daily rollups remain stable evidence.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone as datetime_timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping
from urllib.parse import urljoin

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import Count, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from .models import Claw
from hive_platform.models import (
    BillingPlan, PricingRate, TenantSubscription,
    UsageDailyRollup, UsageEvent, UsageInvoiceDraft,
)

MONEY = Decimal('0.01')
QUANTITY = Decimal('0.000001')


HIVE_346_PLAN_CATALOG = {
    'assistant': {
        'name': 'Assistant',
        'currency': 'INR',
        'monthly_base_price': '9999.00',
        'included_usage': {
            'agent_turn': '2000',
            'cortex_input_token': '3000000',
            'cortex_output_token': '800000',
            'storage_gib_hour': '3720',
        },
        'soft_quota': {
            'agent_turn': '1600',
            'cortex_input_token': '2400000',
            'cortex_output_token': '640000',
            'storage_gib_hour': '2976',
        },
        'hard_quota': {
            'agent_turn': '2000',
            'cortex_input_token': '3000000',
            'cortex_output_token': '800000',
            'storage_gib_hour': '3720',
        },
        'rates': {
            'agent_turn': {'unit': 'turn', 'unit_price': '0.50', 'overage_policy': 'overage'},
            'cortex_input_token': {'unit': 'token', 'unit_price': '0.000010', 'overage_policy': 'overage'},
            'cortex_output_token': {'unit': 'token', 'unit_price': '0.000050', 'overage_policy': 'overage'},
            'storage_gib_hour': {'unit': 'gib_hour', 'unit_price': '0.004000', 'overage_policy': 'overage'},
        },
    },
    'team-pod': {
        'name': 'Team Pod',
        'currency': 'INR',
        'monthly_base_price': '49999.00',
        'included_usage': {
            'agent_turn': '20000',
            'cortex_input_token': '60000000',
            'cortex_output_token': '16000000',
            'storage_gib_hour': '37200',
        },
        'soft_quota': {
            'agent_turn': '16000',
            'cortex_input_token': '48000000',
            'cortex_output_token': '12800000',
            'storage_gib_hour': '29760',
        },
        'hard_quota': {
            'agent_turn': '20000',
            'cortex_input_token': '60000000',
            'cortex_output_token': '16000000',
            'storage_gib_hour': '37200',
        },
        'rates': {
            'agent_turn': {'unit': 'turn', 'unit_price': '0.40', 'overage_policy': 'overage'},
            'cortex_input_token': {'unit': 'token', 'unit_price': '0.000008', 'overage_policy': 'overage'},
            'cortex_output_token': {'unit': 'token', 'unit_price': '0.000040', 'overage_policy': 'overage'},
            'storage_gib_hour': {'unit': 'gib_hour', 'unit_price': '0.003500', 'overage_policy': 'overage'},
        },
    },
    'autonomous-org': {
        'name': 'Autonomous Org',
        'currency': 'INR',
        'monthly_base_price': '250000.00',
        'included_usage': {
            'agent_turn': '100000',
            'cortex_input_token': '400000000',
            'cortex_output_token': '100000000',
            'storage_gib_hour': '186000',
        },
        'soft_quota': {
            'agent_turn': '80000',
            'cortex_input_token': '320000000',
            'cortex_output_token': '80000000',
            'storage_gib_hour': '148800',
        },
        'hard_quota': {
            'agent_turn': '100000',
            'cortex_input_token': '400000000',
            'cortex_output_token': '100000000',
            'storage_gib_hour': '186000',
        },
        'rates': {
            'agent_turn': {'unit': 'turn', 'unit_price': '0.35', 'overage_policy': 'manual_review'},
            'cortex_input_token': {'unit': 'token', 'unit_price': '0.000006', 'overage_policy': 'manual_review'},
            'cortex_output_token': {'unit': 'token', 'unit_price': '0.000035', 'overage_policy': 'manual_review'},
            'storage_gib_hour': {'unit': 'gib_hour', 'unit_price': '0.003000', 'overage_policy': 'manual_review'},
        },
    },
}


def _to_decimal(value) -> Decimal:
    return Decimal(str(value))


def _quantity(value) -> Decimal:
    return _to_decimal(value).quantize(QUANTITY)


def _money(value) -> Decimal:
    return _to_decimal(value).quantize(MONEY, rounding=ROUND_HALF_UP)


def _books_invoice_idempotency_key(tenant_org_id: str, period_start: date, period_end: date) -> str:
    return f'hive-usage-invoice:{tenant_org_id}:{period_start.isoformat()}:{period_end.isoformat()}'


def _gst_components(*, subtotal: Decimal, domestic_gst: bool, tax_rate: Decimal,
                    seller_state_code: str = '', place_of_supply_state_code: str = ''):
    if not domestic_gst or tax_rate == 0:
        return []
    seller = str(seller_state_code or getattr(settings, 'HIVE_BILLING_SELLER_GST_STATE_CODE', '') or '').strip()
    place = str(place_of_supply_state_code or '').strip()
    if seller and place and seller == place:
        half = _money(tax_rate / Decimal('2'))
        return [
            {'name': 'CGST', 'rate_percent': str(half), 'amount': str(_money(subtotal * half / Decimal('100')))},
            {'name': 'SGST', 'rate_percent': str(half), 'amount': str(_money(subtotal * half / Decimal('100')))},
        ]
    return [
        {'name': 'IGST', 'rate_percent': str(tax_rate), 'amount': str(_money(subtotal * tax_rate / Decimal('100')))}
    ]


def _day_bounds(day: date):
    start = datetime.combine(day, time.min)
    if getattr(settings, 'USE_TZ', False) and timezone.is_naive(start):
        start = timezone.make_aware(start, timezone.get_current_timezone())
    end = start + timedelta(days=1)
    return start, end


def _tenant_fields_for_claw(claw: Claw) -> dict:
    """Resolve the tenant dimensions currently available on a Hive Claw.

    Hive's first billing slice stores per-tenant usage evidence before the org
    subscription model is fully wired. Until a dedicated tenant org FK lands, the
    owner identity is the stable tenant dimension for claw/runtime producers.
    """
    return {
        'tenant_org_id': str(getattr(claw, 'owner_id', '') or ''),
        'tenant_org_slug': str(getattr(claw, 'owner_username', '') or ''),
        'owner_id': getattr(claw, 'owner_id', None),
        'claw': claw,
    }


def _source_id(prefix: str, claw: Claw, source_event_id: str) -> str:
    digest = hashlib.sha256(str(source_event_id).encode()).hexdigest()[:24]
    return f'{prefix}:{getattr(claw, "id", "unknown")}:{digest}'


def _decimal_gib(size: str) -> Decimal:
    value = str(size or '').strip()
    if not value:
        return Decimal('0')
    suffixes = (
        ('Ti', Decimal('1024')), ('T', Decimal('1000')),
        ('Gi', Decimal('1')), ('G', Decimal('1')),
        ('Mi', Decimal('0.0009765625')), ('M', Decimal('0.001')),
    )
    for suffix, multiplier in suffixes:
        if value.endswith(suffix):
            return _to_decimal(value[:-len(suffix)] or '0') * multiplier
    return _to_decimal(value)


def record_agent_turn(*, claw: Claw, source_event_id: str, occurred_at=None, metadata: Mapping | None = None) -> UsageEvent:
    """Record one real Hive/runtime assistant turn for billing metering."""
    return record_usage_event(
        **_tenant_fields_for_claw(claw),
        source=UsageEvent.Source.RUNTIME,
        metric=UsageEvent.Metric.AGENT_TURN,
        quantity=1,
        unit='turn',
        occurred_at=occurred_at,
        resource_id=str(getattr(claw, 'id', '')),
        idempotency_key=_source_id('runtime:agent-turn', claw, source_event_id),
        metadata=metadata,
    )


def record_cortex_token_usage(*, claw: Claw, source_event_id: str, usage: Mapping, model: str = '',
                              occurred_at=None, metadata: Mapping | None = None) -> list[UsageEvent]:
    """Bridge an OpenAI-compatible Cortex usage block into tenant UsageEvents."""
    usage = dict(usage or {})
    model = model or getattr(claw, 'model', '') or ''
    produced = []
    token_metrics = (
        (UsageEvent.Metric.CORTEX_INPUT_TOKEN, usage.get('prompt_tokens', usage.get('input_tokens')), 'input'),
        (UsageEvent.Metric.CORTEX_OUTPUT_TOKEN, usage.get('completion_tokens', usage.get('output_tokens')), 'output'),
    )
    for metric, quantity, direction in token_metrics:
        if quantity in (None, ''):
            continue
        produced.append(record_usage_event(
            **_tenant_fields_for_claw(claw),
            source=UsageEvent.Source.CORTEX,
            metric=metric,
            quantity=quantity,
            unit='token',
            model=model,
            occurred_at=occurred_at,
            resource_id=str(getattr(claw, 'id', '')),
            idempotency_key=_source_id(f'cortex:{direction}', claw, source_event_id),
            metadata={**dict(metadata or {}), 'usage': usage},
        ))
    return produced


def record_claw_uptime_seconds(*, claw: Claw, window_start, window_end, source_event_id: str,
                               metadata: Mapping | None = None) -> UsageEvent | None:
    """Record measured running-window uptime for a claw."""
    seconds = max(Decimal('0'), _to_decimal((window_end - window_start).total_seconds()))
    if seconds == 0:
        return None
    return record_usage_event(
        **_tenant_fields_for_claw(claw),
        source=UsageEvent.Source.RUNTIME,
        metric=UsageEvent.Metric.CLAW_UPTIME_SECOND,
        quantity=seconds,
        unit='second',
        occurred_at=window_end,
        resource_id=str(getattr(claw, 'id', '')),
        idempotency_key=_source_id('runtime:claw-uptime', claw, source_event_id),
        metadata={
            **dict(metadata or {}),
            'window_start': window_start.isoformat(),
            'window_end': window_end.isoformat(),
        },
    )


def record_storage_gib_hours(*, claw: Claw, window_start, window_end, source_event_id: str,
                             pvc_size: str | None = None, metadata: Mapping | None = None) -> UsageEvent | None:
    """Record tenant-owned claw storage allocation for a measured time window."""
    hours = _to_decimal((window_end - window_start).total_seconds()) / Decimal('3600')
    gib = _decimal_gib(pvc_size or getattr(claw, 'pvc_size', ''))
    quantity = gib * hours
    if quantity <= 0:
        return None
    return record_usage_event(
        **_tenant_fields_for_claw(claw),
        source=UsageEvent.Source.STORAGE,
        metric=UsageEvent.Metric.STORAGE_GIB_HOUR,
        quantity=quantity,
        unit='GiB-hour',
        occurred_at=window_end,
        resource_id=str(getattr(claw, 'id', '')),
        idempotency_key=_source_id('storage:gib-hour', claw, source_event_id),
        metadata={
            **dict(metadata or {}),
            'pvc_size': pvc_size or getattr(claw, 'pvc_size', ''),
            'window_start': window_start.isoformat(),
            'window_end': window_end.isoformat(),
        },
    )
@transaction.atomic
def seed_default_billing_catalog() -> dict[str, int]:
    """Upsert HIVE-317/HIVE-321 plan catalog and metric rate rows."""
    plan_count = 0
    rate_count = 0
    for plan_id, spec in HIVE_346_PLAN_CATALOG.items():
        plan, _ = BillingPlan.objects.update_or_create(
            plan_id=plan_id,
            defaults={
                'name': spec['name'],
                'currency': spec['currency'],
                'monthly_base_price': _money(spec['monthly_base_price']),
                'included_usage': dict(spec['included_usage']),
                'soft_quota': dict(spec['soft_quota']),
                'hard_quota': dict(spec['hard_quota']),
                'active': True,
            },
        )
        plan_count += 1
        for metric, rate in spec['rates'].items():
            PricingRate.objects.update_or_create(
                plan=plan,
                metric=metric,
                unit=rate['unit'],
                model=rate.get('model', ''),
                defaults={
                    'currency': spec['currency'],
                    'unit_price': _to_decimal(rate['unit_price']),
                    'included_quantity': _quantity(spec['included_usage'][metric]),
                    'overage_policy': rate.get('overage_policy', PricingRate.OveragePolicy.OVERAGE),
                    'active': True,
                    'metadata': {'source': 'HIVE-346', 'pricing_basis': 'HIVE-317/HIVE-321 v0.1'},
                },
            )
            rate_count += 1
    return {'plans': plan_count, 'rates': rate_count}


@transaction.atomic
def assign_tenant_plan(*, tenant_org_id: str, plan_id: str, tenant_org_slug: str = '',
                       customer_name: str = '', customer_gstin: str = '',
                       billing_email: str = '', period_start: date | None = None,
                       period_end: date | None = None,
                       status: str = TenantSubscription.Status.ACTIVE,
                       metadata: Mapping | None = None) -> TenantSubscription:
    """Bind or re-bind a tenant org to an active billing plan."""
    plan = BillingPlan.objects.get(plan_id=plan_id, active=True)
    subscription, _ = TenantSubscription.objects.update_or_create(
        tenant_org_id=str(tenant_org_id),
        defaults={
            'tenant_org_slug': tenant_org_slug,
            'customer_name': customer_name,
            'customer_gstin': customer_gstin,
            'billing_email': billing_email,
            'plan': plan,
            'status': status,
            'current_period_start': period_start,
            'current_period_end': period_end,
            'manual_collection': True,
            'metadata': dict(metadata or {}),
        },
    )
    return subscription


def get_metric_rates_for_subscription(subscription: TenantSubscription) -> dict[str, Decimal]:
    """Return the default active metric rate map expected by invoice generation."""
    if not subscription.plan_id:
        return {}
    return {
        rate.metric: rate.unit_price
        for rate in subscription.plan.rates.filter(active=True, model='').order_by('metric')
    }


@dataclass(frozen=True)
class QuotaDecision:
    metric: str
    quantity: Decimal
    projected_quantity: Decimal
    soft_quota: Decimal | None
    hard_quota: Decimal | None
    allowed: bool
    warning: bool
    level: str

    def as_dict(self):
        return {
            'metric': self.metric,
            'quantity': str(_quantity(self.quantity)),
            'projected_quantity': str(_quantity(self.projected_quantity)),
            'soft_quota': str(self.soft_quota) if self.soft_quota is not None else None,
            'hard_quota': str(self.hard_quota) if self.hard_quota is not None else None,
            'allowed': self.allowed,
            'warning': self.warning,
            'level': self.level,
        }


def _period_for_subscription(subscription: TenantSubscription, today: date | None = None):
    today = today or timezone.localdate()
    start = subscription.current_period_start or today.replace(day=1)
    end = subscription.current_period_end or today
    return start, end


def evaluate_quota(*, tenant_org_id: str, metric_quantities: Mapping[str, object] | None = None,
                   as_of: date | None = None) -> dict[str, object]:
    """Evaluate soft warning / hard-cap quota decisions for a tenant.

    `metric_quantities` represents prospective additional usage. The function
    reads only the target tenant's rollups, so one tenant crossing a cap cannot
    affect quota decisions for another tenant.
    """
    try:
        subscription = TenantSubscription.objects.select_related('plan').get(
            tenant_org_id=str(tenant_org_id),
            status__in=[TenantSubscription.Status.TRIAL, TenantSubscription.Status.ACTIVE],
        )
    except TenantSubscription.DoesNotExist:
        return {
            'tenant_org_id': str(tenant_org_id),
            'plan_id': None,
            'allowed': True,
            'level': 'no_subscription',
            'decisions': {},
        }
    if not subscription.plan:
        return {
            'tenant_org_id': str(tenant_org_id),
            'plan_id': None,
            'allowed': True,
            'level': 'no_plan',
            'decisions': {},
        }

    period_start, period_end = _period_for_subscription(subscription, as_of)
    rows = _rollups_for_period(str(tenant_org_id), period_start, period_end).values('metric').annotate(quantity=Sum('quantity'))
    current = {row['metric']: row['quantity'] or Decimal('0') for row in rows}
    metric_quantities = dict(metric_quantities or {})
    metrics = set(subscription.plan.soft_quota or {}) | set(subscription.plan.hard_quota or {}) | set(metric_quantities)

    decisions: dict[str, QuotaDecision] = {}
    allowed = True
    level = 'ok'
    for metric in sorted(metrics):
        quantity = current.get(metric, Decimal('0'))
        projected = quantity + _to_decimal(metric_quantities.get(metric, '0'))
        soft = subscription.plan.soft_quota.get(metric) if subscription.plan.soft_quota else None
        hard = subscription.plan.hard_quota.get(metric) if subscription.plan.hard_quota else None
        soft_decimal = _to_decimal(soft) if soft is not None else None
        hard_decimal = _to_decimal(hard) if hard is not None else None
        hard_hit = hard_decimal is not None and projected > hard_decimal
        soft_hit = soft_decimal is not None and projected >= soft_decimal
        if hard_hit:
            decision_level = 'hard_cap'
            allowed = False
            level = 'hard_cap'
        elif soft_hit:
            decision_level = 'soft_warning'
            if level == 'ok':
                level = 'soft_warning'
        else:
            decision_level = 'ok'
        decisions[metric] = QuotaDecision(
            metric=metric,
            quantity=quantity,
            projected_quantity=projected,
            soft_quota=soft_decimal,
            hard_quota=hard_decimal,
            allowed=not hard_hit,
            warning=soft_hit,
            level=decision_level,
        )

    return {
        'tenant_org_id': str(tenant_org_id),
        'tenant_org_slug': subscription.tenant_org_slug,
        'plan_id': subscription.plan.plan_id,
        'period': {'start': period_start.isoformat(), 'end': period_end.isoformat()},
        'allowed': allowed,
        'level': level,
        'decisions': {metric: decision.as_dict() for metric, decision in decisions.items()},
    }


def record_usage_event(*, source: str, metric: str, quantity, unit: str,
                       occurred_at=None, tenant_org_id: str = '', tenant_org_slug: str = '',
                       owner_id=None, claw=None, model: str = '', resource_id: str = '',
                       idempotency_key: str | None = None, metadata: Mapping | None = None) -> UsageEvent:
    """Persist one immutable usage event through the neutral CloudEvents path.

    Trusted in-process producers get a server-issued authority snapshot derived
    from the authenticated resource/tenant context. Reusing a producer identity
    with different semantics now fails loud instead of silently returning the
    first quantity (the HIVE-319 repeated-key integrity gap).
    """
    from .metering_events import (
        authority_snapshot_sha256,
        ingest_usage_cloudevent,
        register_admission_authority_snapshot,
    )

    # Legacy helpers historically omitted occurrence time. On a retry, reuse the
    # stored time so the identical producer key produces the identical envelope;
    # changed quantity/model/metadata still conflicts through the payload hash.
    if occurred_at is None and idempotency_key:
        existing = UsageEvent.objects.filter(source=str(source), event_id=str(idempotency_key)).first()
        occurred_at = existing.occurred_at if existing is not None else None
    occurred_at = occurred_at or timezone.now()
    if timezone.is_naive(occurred_at):
        occurred_at = timezone.make_aware(occurred_at, datetime_timezone.utc)
    event_id = str(idempotency_key or uuid.uuid4())
    billing_account_id = str(tenant_org_id or (f'user:{owner_id}' if owner_id is not None else 'platform'))
    proof_id = 'trusted-local:' + hashlib.sha256(
        f'{source}\0{event_id}'.encode('utf-8')
    ).hexdigest()[:40]
    authority = {
        'billing_account_id': billing_account_id,
        'proof_id': proof_id,
        'reservation_id': event_id,
        'lease_id': '',
        'payer_link_generation': 0,
        'binding_generation': 0,
        'admission_generation': 0,
    }
    authority['proof_sha256'] = authority_snapshot_sha256(authority)
    envelope = {
        'specversion': '1.0',
        'id': event_id,
        'source': str(source),
        'type': f'shizuha.usage.{metric}.v1',
        'subject': billing_account_id,
        'dataschema': 'urn:shizuha:metering:usage-event:v1',
        'datacontenttype': 'application/json',
        'time': occurred_at.isoformat().replace('+00:00', 'Z'),
        'data': {
            'meter': str(metric),
            'quantity': str(_to_decimal(quantity)),
            'unit': str(unit),
            'model': str(model or ''),
            'resource_id': str(resource_id or ''),
            'metadata': dict(metadata or {}),
        },
        'billingaccountid': billing_account_id,
        'authorityproofid': proof_id,
        'authorityproofhash': authority['proof_sha256'],
        'reservationid': event_id,
        'payerlinkgen': 0,
        'bindinggen': 0,
        'admissiongen': 0,
    }
    # HIVE-408 (PLAT-1254): record_usage_event is the single chokepoint for every
    # producer path (runtime turns, Cortex token attribution, storage/uptime). A
    # failure here is a missed/at-risk billing event, so count it fail-loud (labeled
    # by producer source) before re-raising — the caller still fails closed.
    try:
        register_admission_authority_snapshot(authority)
        result = ingest_usage_cloudevent(
            envelope,
            trusted_context={
                'tenant_org_slug': str(tenant_org_slug or ''),
                'owner_id': owner_id,
                'claw': claw,
            },
        )
        return result.event
    except Exception:
        from . import usage_metering_metrics
        usage_metering_metrics.record_failure(source)
        raise


@transaction.atomic
def rebuild_daily_rollups(day: date) -> int:
    """Rebuild all rollup rows for one UTC/local day from raw UsageEvent rows."""
    start, end = _day_bounds(day)
    aggregates = (
        UsageEvent.objects
        .filter(occurred_at__gte=start, occurred_at__lt=end)
        .annotate(day_value=TruncDate('occurred_at'))
        .values('day_value', 'tenant_org_id', 'tenant_org_slug', 'owner_id', 'source', 'metric', 'unit', 'model')
        .annotate(quantity_sum=Sum('quantity'), event_count_sum=Count('id'))
    )
    seen = []
    for row in aggregates:
        key = dict(
            day=row['day_value'],
            tenant_org_id=row['tenant_org_id'] or '',
            tenant_org_slug=row['tenant_org_slug'] or '',
            owner_id=row['owner_id'],
            source=row['source'],
            metric=row['metric'],
            unit=row['unit'],
            model=row['model'] or '',
        )
        UsageDailyRollup.objects.update_or_create(
            **key,
            defaults={
                'quantity': row['quantity_sum'] or Decimal('0'),
                'event_count': row['event_count_sum'] or 0,
            },
        )
        seen.append(key)
    # Remove stale rows for dimensions that disappeared since the previous rebuild.
    stale = UsageDailyRollup.objects.filter(day=day)
    for key in seen:
        stale = stale.exclude(**key)
    stale.delete()
    return len(seen)


def _rollups_for_period(tenant_org_id: str, period_start: date, period_end: date):
    return UsageDailyRollup.objects.filter(
        tenant_org_id=str(tenant_org_id),
        day__gte=period_start,
        day__lte=period_end,
    )


def build_books_invoice_draft(*, tenant_org_id: str, period_start: date, period_end: date,
                              metric_rates: Mapping[str, object], tenant_org_slug: str = '',
                              customer_name: str = '', customer_gstin: str = '',
                              billing_email: str = '', currency: str = 'INR',
                              domestic_gst: bool = True, tax_rate_percent=Decimal('18.00'),
                              sac_code: str | None = None, seller_state_code: str = '',
                              place_of_supply_state_code: str = '') -> UsageInvoiceDraft:
    """Create/update a Hive draft containing a Books-ready GST invoice payload.

    `metric_rates` maps metric -> money per usage unit. Product can swap pricing
    without schema changes; invoice lines are grouped by metric/unit/model.
    """
    rows = list(_rollups_for_period(tenant_org_id, period_start, period_end).order_by('metric', 'unit', 'model'))
    grouped = defaultdict(lambda: {'quantity': Decimal('0'), 'unit': '', 'model': ''})
    for row in rows:
        group_key = (row.metric, row.unit, row.model or '')
        grouped[group_key]['quantity'] += row.quantity
        grouped[group_key]['unit'] = row.unit
        grouped[group_key]['model'] = row.model or ''

    line_items = []
    subtotal = Decimal('0')
    for (metric, unit, model), data in sorted(grouped.items()):
        rate = _to_decimal(metric_rates.get(metric, '0'))
        amount = _money(data['quantity'] * rate)
        subtotal += amount
        line_items.append({
            'description': f'{metric.replace("_", " ").title()}' + (f' ({model})' if model else ''),
            'metric': metric,
            'model': model,
            'quantity': str(data['quantity']),
            'unit': unit,
            'rate': str(_money(rate)),
            'amount': str(amount),
            'sac_code': sac_code or getattr(settings, 'HIVE_BILLING_DEFAULT_SAC_CODE', ''),
        })

    subtotal = _money(subtotal)
    effective_tax_rate = _to_decimal(tax_rate_percent if domestic_gst else '0')
    tax_amount = _money(subtotal * effective_tax_rate / Decimal('100'))
    total = _money(subtotal + tax_amount)
    idempotency_key = _books_invoice_idempotency_key(str(tenant_org_id), period_start, period_end)
    tax_components = _gst_components(
        subtotal=subtotal,
        domestic_gst=domestic_gst,
        tax_rate=effective_tax_rate,
        seller_state_code=seller_state_code,
        place_of_supply_state_code=place_of_supply_state_code,
    )
    payload = {
        'source': 'hive',
        'source_task': 'HIVE-347',
        'idempotency_key': idempotency_key,
        'tenant_org_id': str(tenant_org_id),
        'tenant_org_slug': tenant_org_slug,
        'customer': {
            'name': customer_name,
            'gstin': customer_gstin,
            'billing_email': billing_email,
            'place_of_supply_state_code': place_of_supply_state_code,
        },
        'period': {'start': period_start.isoformat(), 'end': period_end.isoformat()},
        'currency': currency,
        'tax_treatment': 'domestic_gst' if domestic_gst else 'export_zero_rated',
        'gst_rate_percent': str(effective_tax_rate),
        'tax_components': tax_components,
        'line_items': line_items,
        'totals': {
            'subtotal': str(subtotal),
            'tax_amount': str(tax_amount),
            'total': str(total),
        },
        'status': 'draft',
    }
    draft, _ = UsageInvoiceDraft.objects.update_or_create(
        tenant_org_id=str(tenant_org_id),
        period_start=period_start,
        period_end=period_end,
        defaults={
            'tenant_org_slug': tenant_org_slug,
            'currency': currency,
            'subtotal': subtotal,
            'tax_rate_percent': effective_tax_rate,
            'tax_amount': tax_amount,
            'total': total,
            'books_payload': payload,
            'status': UsageInvoiceDraft.Status.DRAFT,
            'error': '',
        },
    )
    return draft


def post_invoice_draft_to_books(draft: UsageInvoiceDraft, *, timeout=15) -> UsageInvoiceDraft:
    """Create/update a live Books draft invoice for one tenant+period.

    The Hive-side row is already unique on (tenant, period).  The outbound
    request carries the same idempotency key in payload + header so Books can
    upsert safely; if a previous Books invoice id is known, Hive uses PUT to
    request an update instead of a duplicate create.
    """
    base = (getattr(settings, 'HIVE_BOOKS_API_URL', '') or '').rstrip('/')
    if not base:
        raise RuntimeError('HIVE_BOOKS_API_URL is not configured')
    token = getattr(settings, 'HIVE_BOOKS_SERVICE_TOKEN', '') or ''
    path = getattr(settings, 'HIVE_BOOKS_DRAFT_INVOICE_PATH', '/shizuha-books/api/hive/invoices/drafts/')
    path = path.strip() or '/shizuha-books/api/hive/invoices/drafts/'
    if not path.endswith('/'):
        path = f'{path}/'
    idempotency_key = draft.books_payload.get('idempotency_key') or _books_invoice_idempotency_key(
        draft.tenant_org_id, draft.period_start, draft.period_end,
    )
    headers = {'Content-Type': 'application/json'}
    headers['Idempotency-Key'] = idempotency_key
    if token:
        headers['Authorization'] = f'Bearer {token}'
    try:
        url = urljoin(f'{base}/', path.lstrip('/'))
        method = requests.post
        if draft.books_invoice_id:
            url = urljoin(url, f'{draft.books_invoice_id}/')
            method = requests.put
        resp = method(url, json=draft.books_payload, headers=headers, timeout=timeout)
        resp.raise_for_status()
        data = resp.json() if resp.content else {}
    except Exception as exc:  # pragma: no cover - caller records the concrete exception text
        draft.status = UsageInvoiceDraft.Status.FAILED
        draft.error = str(exc)
        draft.save(update_fields=['status', 'error', 'updated_at'])
        raise
    draft.status = UsageInvoiceDraft.Status.POSTED_TO_BOOKS
    draft.books_invoice_id = str(data.get('id') or data.get('invoice_id') or '')
    draft.error = ''
    draft.save(update_fields=['status', 'books_invoice_id', 'error', 'updated_at'])
    return draft
