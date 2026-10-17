"""Operator-ratified free-v1 + starter-999-v1-rc1 catalog configuration (PLAT-4483).

HLD §12.2 — all catalog data is configuration, not hardcoded product logic.
Values are normalized from PLAT-4242 operator ratification without changing
commercial magnitude. free-v1 becomes effective only after PlanVersion +
migrations + admission tests ship.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

# Seller partition placeholders for India R4 (Books/Legal fill exact IDs at cutover).
DEFAULT_SELLER = {
    'seller_legal_entity_id': 'shizuha-global-pvt-ltd',
    'seller_gst_registration_id': 'pending-books',
    'books_organization_id': 'books-org-shizuha',
    'gateway_merchant_account': 'razorpay-shizuha-inr',
    'currency': 'INR',
}

# Meter definitions: key → semantic fields (version assigned at seed time).
FREE_V1_METERS = [
    {
        'key': 'storage.byte_seconds',
        'display_name': 'Pooled standard storage byte-seconds',
        'event_type': 'shizuha.metering.storage.byte_seconds.v1',
        'value_selector': 'data.byte_seconds',
        'aggregation': 'sum',
        'canonical_unit': 'byte_second',
        'dimension_schema': {
            'allowlisted': ['product', 'bucket_class', 'region'],
            'group_by': ['product'],
        },
        'window_semantics': {'kind': 'continuous', 'split_at_grant_boundary': True},
        'reset_semantics': {'kind': 'none'},
        'late_arrival_semantics': {'accept_within_hours': 72, 'policy': 'include_in_open_period'},
    },
    {
        'key': 'compute.memory_byte_seconds',
        'display_name': 'Compute memory byte-seconds',
        'event_type': 'shizuha.metering.compute.memory_byte_seconds.v1',
        'value_selector': 'data.memory_byte_seconds',
        'aggregation': 'sum',
        'canonical_unit': 'byte_second',
        'dimension_schema': {'allowlisted': ['product', 'tier'], 'group_by': ['product']},
        'window_semantics': {'kind': 'continuous', 'split_at_grant_boundary': True},
        'reset_semantics': {'kind': 'none'},
        'late_arrival_semantics': {'accept_within_hours': 72, 'policy': 'include_in_open_period'},
    },
    {
        'key': 'compute.cpu_seconds',
        'display_name': 'Compute CPU-seconds',
        'event_type': 'shizuha.metering.compute.cpu_seconds.v1',
        'value_selector': 'data.cpu_seconds',
        'aggregation': 'sum',
        'canonical_unit': 'cpu_second',
        'dimension_schema': {'allowlisted': ['product', 'tier'], 'group_by': ['product']},
        'window_semantics': {'kind': 'continuous', 'split_at_grant_boundary': True},
        'reset_semantics': {'kind': 'none'},
        'late_arrival_semantics': {'accept_within_hours': 72, 'policy': 'include_in_open_period'},
    },
    {
        'key': 'inference.weighted_tokens',
        'display_name': 'Cortex weighted tokens (fairness pool)',
        'event_type': 'shizuha.metering.inference.weighted_tokens.v1',
        'value_selector': 'data.weighted_tokens',
        'aggregation': 'sum',
        'canonical_unit': 'weighted_token',
        'dimension_schema': {
            'allowlisted': ['model', 'provider', 'billable'],
            'group_by': ['model'],
        },
        'window_semantics': {'kind': 'event'},
        'reset_semantics': {'kind': 'none'},
        'late_arrival_semantics': {'accept_within_hours': 24, 'policy': 'include_in_open_period'},
    },
    {
        'key': 'network.public_egress_bytes',
        'display_name': 'Public egress bytes',
        'event_type': 'shizuha.metering.network.public_egress_bytes.v1',
        'value_selector': 'data.bytes',
        'aggregation': 'sum',
        'canonical_unit': 'byte',
        'dimension_schema': {'allowlisted': ['product', 'region'], 'group_by': ['product']},
        'window_semantics': {'kind': 'event'},
        'reset_semantics': {'kind': 'none'},
        'late_arrival_semantics': {'accept_within_hours': 72, 'policy': 'include_in_open_period'},
    },
    {
        'key': 'platform.requests',
        'display_name': 'Platform requests',
        'event_type': 'shizuha.metering.platform.requests.v1',
        'value_selector': 'data.count',
        'aggregation': 'sum',
        'canonical_unit': 'request',
        'dimension_schema': {'allowlisted': ['product', 'route_class'], 'group_by': ['product']},
        'window_semantics': {'kind': 'event'},
        'reset_semantics': {'kind': 'none'},
        'late_arrival_semantics': {'accept_within_hours': 24, 'policy': 'include_in_open_period'},
    },
]

# Monthly free-v1 grants (exact normalized amounts from HLD §12.2).
FREE_V1_MONTHLY_GRANTS = [
    {
        'grant_key': 'free-v1.storage.byte_seconds',
        'meter_key': 'storage.byte_seconds',
        # 5e9 bytes × exact grant-period seconds — seed stores the byte pool;
        # period expansion multiplies by period length at issuance.
        'amount': Decimal('5000000000'),
        'unit': 'byte',
        'amount_notes': '5e9 bytes pool; × grant-period seconds → storage.byte_seconds at issuance',
        'priority': 10,
        'behavior': 'hard',
        'issuance_mode': 'bytes_times_period_seconds',
        'issued_unit': 'byte_second',
    },
    {
        'grant_key': 'free-v1.compute.memory_byte_seconds',
        'meter_key': 'compute.memory_byte_seconds',
        'amount': Decimal('540000000000000'),  # 150 GB-RAM-hours
        'unit': 'byte_second',
        'priority': 20,
        'behavior': 'hard',
        'issuance_mode': 'fixed',
        'issued_unit': 'byte_second',
    },
    {
        'grant_key': 'free-v1.compute.cpu_seconds',
        'meter_key': 'compute.cpu_seconds',
        'amount': Decimal('360000'),  # 100 vCPU-hours
        'unit': 'cpu_second',
        'priority': 30,
        'behavior': 'hard',
        'issuance_mode': 'fixed',
        'issued_unit': 'cpu_second',
    },
    {
        'grant_key': 'free-v1.inference.weighted_tokens',
        'meter_key': 'inference.weighted_tokens',
        'amount': Decimal('1000000'),
        'unit': 'weighted_token',
        'priority': 40,
        'behavior': 'hard',
        'issuance_mode': 'fixed',
        'issued_unit': 'weighted_token',
    },
    {
        'grant_key': 'free-v1.network.public_egress_bytes',
        'meter_key': 'network.public_egress_bytes',
        'amount': Decimal('5000000000'),
        'unit': 'byte',
        'priority': 50,
        'behavior': 'hard',
        'issuance_mode': 'fixed',
        'issued_unit': 'byte',
    },
    {
        'grant_key': 'free-v1.platform.requests',
        'meter_key': 'platform.requests',
        'amount': Decimal('1000000'),
        'unit': 'request',
        'priority': 60,
        'behavior': 'hard',
        'issuance_mode': 'fixed',
        'issued_unit': 'request',
    },
]

# Independent instantaneous caps (not monthly grants).
FREE_V1_CAPS = {
    'storage.current_bytes': Decimal('5000000000'),
    'hive.personal_agents.active': Decimal('1'),
    'sites.static.count': Decimal('3'),
    'compute.containers.concurrent': Decimal('1'),
    'compute.ram_per_container_mib': Decimal('512'),
    'compute.ram_aggregate_mib': Decimal('512'),
    'compute.vcpu_per_container': Decimal('0.5'),
    'compute.vcpu_aggregate': Decimal('0.5'),
    'cortex.requests_per_minute': Decimal('60'),
    'cortex.concurrency': Decimal('1'),
}

STARTER_999_V1_RC1 = {
    'plan_key': 'starter-999-v1-rc1',
    'display_name': 'Starter ₹999 (RC1)',
    'advertised_price_inr_micros': 999_00_0000,  # ₹999.00 in micros (1 INR = 1_000_000 micros)
    'gross_service_spend_inr_micros': 1_498_50_0000,  # ₹1,498.50 proposed
    'paid_component_inr_micros': 999_00_0000,
    'promotional_component_inr_micros': 499_50_0000,
    'status': 'proposed',  # pending Product/Books/Legal exact-version approval
    'caps': {
        'storage.current_bytes': Decimal('50000000000'),  # 50 GB
        'hive.personal_agents.active': Decimal('5'),
        'sites.static.count': Decimal('20'),
        'compute.containers.concurrent': Decimal('3'),
        'compute.ram_per_container_mib': Decimal('2048'),
        'compute.ram_aggregate_mib': Decimal('4096'),
        'compute.vcpu_per_container': Decimal('1'),
        'compute.vcpu_aggregate': Decimal('2'),
        'cortex.requests_per_minute': Decimal('240'),
        'cortex.concurrency': Decimal('4'),
    },
}

FREE_V1_PLAN = {
    'plan_key': 'free-v1',
    'display_name': 'Free (operator-ratified v1)',
    'advertised_price_inr_micros': 0,
    'status': 'active',
    'tax_inclusive': True,
    'cadence': 'monthly',
    'caps': {k: str(v) for k, v in FREE_V1_CAPS.items()},
    'components': [
        {
            'kind': 'monthly_free_grant',
            'grant_key': g['grant_key'],
            'meter_key': g['meter_key'],
            'amount': str(g['amount']),
            'unit': g['unit'],
            'priority': g['priority'],
            'issuance_mode': g['issuance_mode'],
            'issued_unit': g['issued_unit'],
        }
        for g in FREE_V1_MONTHLY_GRANTS
    ],
}


def catalog_snapshot() -> dict:
    """Return a deep-copied configuration snapshot suitable for seeding."""
    return deepcopy(
        {
            'design_pin': {
                'hld': 'b131fbff-1f9a-48b9-800a-5c294a44ab3c',
                'hld_version': 44,
                'adr_0032': '59a92688-5bf4-4659-91c7-a74161bd2803',
                'adr_0032_version': 15,
                'section': '12.2',
            },
            'seller': DEFAULT_SELLER,
            'meters': FREE_V1_METERS,
            'plans': [FREE_V1_PLAN, STARTER_999_V1_RC1],
            'free_v1_monthly_grants': FREE_V1_MONTHLY_GRANTS,
            'free_v1_caps': FREE_V1_CAPS,
        }
    )
