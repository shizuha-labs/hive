"""PLAT-4485 — UsageRollup rebuild, rating order, period transitions, corrections."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from .metering_core import create_billing_account, seed_free_v1_catalog, seed_rate_version
from .metering_models import (
    BillingAccount,
    BillingStatement,
    MeterDefinition,
    RateVersion,
    RatedUsageLine,
    SpendAllocation,
    UsageRollup,
)
from .metering_statements import (
    PeriodLagError,
    StatementError,
    StatementStateError,
    begin_closing,
    build_books_draft_payload,
    check_producer_lag,
    compute_statement_id,
    create_spend_allocation,
    export_statement_draft,
    finalize_statement,
    open_adjustment_statement,
    open_or_get_statement,
    rate_rollups,
    rate_statement,
    rebuild_usage_rollups,
)
from hive_platform.models import UsageEvent


def _seed_event(account, *, metric, quantity, occurred_at, source='urn:test:producer', unit='token'):
    eid = str(uuid4())
    return UsageEvent.objects.create(
        source=source,
        event_id=eid,
        event_type='io.shizuha.usage.measured.v1',
        subject=f'urn:shizuha:billing-account:{account.id}',
        billing_account_id=str(account.id),
        metric=metric,
        unit=unit,
        quantity=Decimal(quantity),
        occurred_at=occurred_at,
        payload_sha256='a' * 64,
        event_payload={'id': eid},
        idempotency_key=f'test-{eid}',
    )


class StatementIdTests(TestCase):
    def test_deterministic_and_kind_sensitive(self):
        start = timezone.now().replace(microsecond=0)
        end = start + timedelta(days=30)
        a = compute_statement_id(
            billing_account_id='00000000-0000-0000-0000-000000000001',
            seller_legal_entity_id='sel-1',
            seller_gst_registration_id='gst-1',
            books_organization_id='books-1',
            currency='INR',
            period_start=start,
            period_end=end,
            statement_kind='ordinary',
        )
        b = compute_statement_id(
            billing_account_id='00000000-0000-0000-0000-000000000001',
            seller_legal_entity_id='sel-1',
            seller_gst_registration_id='gst-1',
            books_organization_id='books-1',
            currency='INR',
            period_start=start,
            period_end=end,
            statement_kind='ordinary',
        )
        self.assertEqual(a, b)
        adj = compute_statement_id(
            billing_account_id='00000000-0000-0000-0000-000000000001',
            seller_legal_entity_id='sel-1',
            seller_gst_registration_id='gst-1',
            books_organization_id='books-1',
            currency='INR',
            period_start=start,
            period_end=end,
            statement_kind='adjustment',
            adjustment_sequence=1,
            original_statement_id=a,
        )
        self.assertNotEqual(a, adj)


class RollupRebuildTests(TestCase):
    def test_rebuild_from_events_with_watermarks(self):
        acct = create_billing_account(
            account_type=BillingAccount.AccountType.INDIVIDUAL,
            owner_subject='user:rollup',
        )
        start = timezone.now().replace(microsecond=0) - timedelta(days=1)
        end = start + timedelta(days=1)
        _seed_event(acct, metric='inference.input_tokens', quantity='100', occurred_at=start + timedelta(hours=1))
        _seed_event(acct, metric='inference.input_tokens', quantity='50', occurred_at=start + timedelta(hours=2))
        _seed_event(acct, metric='inference.output_tokens', quantity='10', occurred_at=start + timedelta(hours=3))

        result = rebuild_usage_rollups(
            billing_account=acct,
            period_start=start,
            period_end=end,
        )
        self.assertEqual(result.event_count, 3)
        self.assertEqual(len(result.rollups), 2)
        by_meter = {r.meter_key: r for r in result.rollups}
        self.assertEqual(by_meter['inference.input_tokens'].gross_quantity, Decimal('150'))
        self.assertEqual(by_meter['inference.output_tokens'].gross_quantity, Decimal('10'))
        self.assertTrue(result.source_manifest_hash)
        # Rebuild bumps generation
        result2 = rebuild_usage_rollups(billing_account=acct, period_start=start, period_end=end)
        self.assertEqual(result2.generation, result.generation + 1)


class RatingOrderTests(TestCase):
    def test_free_grant_before_billable(self):
        acct = create_billing_account(
            account_type=BillingAccount.AccountType.INDIVIDUAL,
            owner_subject='user:rate',
        )
        catalog = seed_free_v1_catalog()
        # Ensure a rate exists for a free-v1 meter if catalog seeded any
        meters = list(MeterDefinition.objects.all()[:1])
        if meters:
            meter = meters[0]
        else:
            meter = MeterDefinition.objects.create(
                key='inference.input_tokens',
                version=1,
                display_name='Input tokens',
                event_type='io.shizuha.usage.measured.v1',
                value_selector='quantity',
                aggregation='sum',
                canonical_unit='token',
                effective_start=timezone.now() - timedelta(days=1),
                canonical_hash='b' * 64,
            )
        rate = seed_rate_version(
            meter=meter,
            rate_key=f'{meter.key}-inr',
            version=1,
            inr_micros_per_unit=1000,
            unit=meter.canonical_unit,
        )

        rollup = UsageRollup.objects.create(
            billing_account=acct,
            period_start=timezone.now() - timedelta(days=1),
            period_end=timezone.now(),
            meter_key=meter.key,
            meter_version=meter.version,
            unit=meter.canonical_unit,
            dimensions_hash='c' * 64,
            gross_quantity=Decimal('100'),
            generation=1,
            source_manifest_hash='d' * 64,
            event_count=1,
        )
        # Without free balance, all billable
        specs = rate_rollups(
            billing_account=acct,
            rollups=[rollup],
            rate_lookup={meter.key: rate},
        )
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].gross_quantity, Decimal('100'))
        self.assertEqual(specs[0].included_quantity, Decimal('0'))
        self.assertEqual(specs[0].billable_quantity, Decimal('100'))
        self.assertEqual(specs[0].customer_gross_rated_debit_inr_micros, 100 * 1000)
        self.assertEqual(specs[0].promotion_facts['rating_order'], 'free_grant_then_spend_credit')

    def test_billable_usage_without_rate_fails_closed(self):
        acct = create_billing_account(
            account_type=BillingAccount.AccountType.INDIVIDUAL,
            owner_subject='user:missing-rate',
        )
        rollup = UsageRollup.objects.create(
            billing_account=acct,
            period_start=timezone.now() - timedelta(days=1),
            period_end=timezone.now(),
            meter_key='unrated.billable_meter',
            meter_version=1,
            unit='request',
            dimensions_hash='1' * 64,
            gross_quantity=Decimal('1'),
            generation=1,
            source_manifest_hash='2' * 64,
            event_count=1,
        )

        with self.assertRaisesRegex(StatementError, 'no RateVersion'):
            rate_rollups(billing_account=acct, rollups=[rollup])


class PeriodTransitionTests(TestCase):
    def test_open_closing_rated_export_finalize_and_adjustment(self):
        acct = create_billing_account(
            account_type=BillingAccount.AccountType.INDIVIDUAL,
            owner_subject='user:period',
        )
        start = timezone.now().replace(microsecond=0) - timedelta(days=7)
        end = start + timedelta(days=7)
        _seed_event(
            acct,
            metric='inference.input_tokens',
            quantity='5',
            occurred_at=start + timedelta(hours=1),
            unit='token',
        )
        meter = MeterDefinition.objects.create(
            key='inference.input_tokens',
            version=1,
            display_name='Input',
            event_type='io.shizuha.usage.measured.v1',
            value_selector='quantity',
            aggregation='sum',
            canonical_unit='token',
            effective_start=start - timedelta(days=1),
            canonical_hash='e' * 64,
        )
        seed_rate_version(
            meter=meter,
            rate_key='input-inr',
            version=1,
            inr_micros_per_unit=100,
            unit='token',
        )

        stmt = open_or_get_statement(
            billing_account=acct,
            period_start=start,
            period_end=end,
            seller_legal_entity_id='sel-1',
            seller_gst_registration_id='gst-1',
            books_organization_id='books-1',
        )
        # Same business key → same statement_id
        stmt2 = open_or_get_statement(
            billing_account=acct,
            period_start=start,
            period_end=end,
            seller_legal_entity_id='sel-1',
            seller_gst_registration_id='gst-1',
            books_organization_id='books-1',
        )
        self.assertEqual(stmt.statement_id, stmt2.statement_id)
        self.assertEqual(stmt.status, BillingStatement.Status.OPEN)

        begin_closing(stmt)
        stmt.refresh_from_db()
        self.assertEqual(stmt.status, BillingStatement.Status.CLOSING)

        rate_statement(stmt)
        stmt.refresh_from_db()
        self.assertEqual(stmt.status, BillingStatement.Status.RATED)
        self.assertGreaterEqual(RatedUsageLine.objects.filter(statement=stmt).count(), 1)

        payload = export_statement_draft(stmt)
        self.assertEqual(payload['statement_id'], stmt.statement_id)
        self.assertIn('lines', payload)
        # Metering must not supply tax fields
        for line in payload['lines']:
            self.assertNotIn('sac_code', line)
            self.assertNotIn('taxable_value', line)
            self.assertNotIn('gst_split', line)
        stmt.refresh_from_db()
        self.assertEqual(stmt.status, BillingStatement.Status.EXPORTED_DRAFT)

        # Idempotent re-export
        with self.assertRaisesRegex(StatementError, 'If-Match'):
            export_statement_draft(stmt, if_match='stale-etag')
        payload2 = export_statement_draft(stmt, if_match=stmt.books_draft_etag)
        self.assertTrue(payload2.get('idempotent_replay'))

        finalize_statement(stmt, books_document_ref='books-doc-1')
        stmt.refresh_from_db()
        self.assertEqual(stmt.status, BillingStatement.Status.FINALIZED)

        adj = open_adjustment_statement(stmt, reason='late correction')
        self.assertEqual(adj.statement_kind, BillingStatement.Kind.ADJUSTMENT)
        self.assertEqual(adj.adjustment_sequence, 1)
        self.assertEqual(adj.original_statement_id, stmt.statement_id)
        self.assertNotEqual(adj.statement_id, stmt.statement_id)
        stmt.refresh_from_db()
        adj.refresh_from_db()
        self.assertEqual(adj.period_start, stmt.period_start)
        self.assertEqual(adj.period_end, stmt.period_end)

        # Finalized cannot re-export
        with self.assertRaises(StatementStateError):
            export_statement_draft(stmt)

    def test_lag_blocks_rating(self):
        acct = create_billing_account(
            account_type=BillingAccount.AccountType.INDIVIDUAL,
            owner_subject='user:lag',
        )
        start = timezone.now().replace(microsecond=0) - timedelta(days=1)
        end = start + timedelta(days=1)
        stmt = open_or_get_statement(
            billing_account=acct, period_start=start, period_end=end
        )
        begin_closing(stmt)
        with self.assertRaises(PeriodLagError):
            rate_statement(
                stmt,
                required_producer_watermarks={
                    'urn:shizuha:sites:publisher': (end + timedelta(hours=1)).isoformat()
                },
            )


class SpendAllocationTests(TestCase):
    def test_unique_source_slice_and_cumulative_fence(self):
        acct = create_billing_account(
            account_type=BillingAccount.AccountType.INDIVIDUAL,
            owner_subject='user:alloc',
        )
        start = timezone.now().replace(microsecond=0)
        end = start + timedelta(days=30)
        stmt = open_or_get_statement(
            billing_account=acct, period_start=start, period_end=end
        )
        a1 = create_spend_allocation(
            statement=stmt,
            source_books_posting_or_lot_slice='lot-1',
            spend_allocation_id='alloc-a',
            amount_inr_micros=500,
            source_slice_cap_inr_micros=1000,
        )
        # Idempotent same key/amount
        a1b = create_spend_allocation(
            statement=stmt,
            source_books_posting_or_lot_slice='lot-1',
            spend_allocation_id='alloc-a',
            amount_inr_micros=500,
            source_slice_cap_inr_micros=1000,
        )
        self.assertEqual(a1.id, a1b.id)

        create_spend_allocation(
            statement=stmt,
            source_books_posting_or_lot_slice='lot-1',
            spend_allocation_id='alloc-b',
            amount_inr_micros=500,
            source_slice_cap_inr_micros=1000,
        )
        # Exceeds cap
        with self.assertRaises(StatementError):
            create_spend_allocation(
                statement=stmt,
                source_books_posting_or_lot_slice='lot-1',
                spend_allocation_id='alloc-c',
                amount_inr_micros=1,
                source_slice_cap_inr_micros=1000,
            )
        self.assertEqual(
            SpendAllocation.objects.filter(source_books_posting_or_lot_slice='lot-1').count(),
            2,
        )


@override_settings(
    METERING_CORE_SERVICE_TOKEN='test-internal-token',
    HIVE_INTERNAL_AUTH_TOKEN='test-internal-token',
)
class StatementDraftAPITests(TestCase):
    def setUp(self):
        self.client = Client()
        self.acct = create_billing_account(
            account_type=BillingAccount.AccountType.INDIVIDUAL,
            owner_subject='user:api',
        )
        self.start = timezone.now().replace(microsecond=0) - timedelta(days=3)
        self.end = self.start + timedelta(days=3)
        _seed_event(
            self.acct,
            metric='inference.input_tokens',
            quantity='2',
            occurred_at=self.start + timedelta(hours=1),
            unit='token',
        )
        MeterDefinition.objects.create(
            key='inference.input_tokens',
            version=1,
            display_name='Input',
            event_type='io.shizuha.usage.measured.v1',
            value_selector='quantity',
            aggregation='sum',
            canonical_unit='token',
            effective_start=self.start - timedelta(days=1),
            canonical_hash='f' * 64,
        )
        meter = MeterDefinition.objects.get(key='inference.input_tokens')
        seed_rate_version(
            meter=meter,
            rate_key='api-input',
            version=1,
            inr_micros_per_unit=50,
            unit='token',
        )

    def test_draft_endpoint_rate_and_export(self):
        resp = self.client.post(
            '/internal/v2/billing/statements/drafts',
            data={
                'billing_account_id': str(self.acct.id),
                'period_start': self.start.isoformat(),
                'period_end': self.end.isoformat(),
                'seller_legal_entity_id': 'sel',
                'seller_gst_registration_id': 'gst',
                'books_organization_id': 'books',
                'action': 'rate_and_export',
            },
            content_type='application/json',
            HTTP_AUTHORIZATION='Bearer test-internal-token',
            HTTP_IDEMPOTENCY_KEY='statement:test:r1',
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertIn('statement_id', body)
        self.assertIn('lines', body)
        self.assertTrue(body['lines'])
        replay_data = {
            'billing_account_id': str(self.acct.id),
            'period_start': self.start.isoformat(),
            'period_end': self.end.isoformat(),
            'seller_legal_entity_id': 'sel',
            'seller_gst_registration_id': 'gst',
            'books_organization_id': 'books',
            'action': 'export_draft',
        }
        stale = self.client.post(
            '/internal/v2/billing/statements/drafts',
            data=replay_data,
            content_type='application/json',
            HTTP_AUTHORIZATION='Bearer test-internal-token',
            HTTP_IF_MATCH='stale-etag',
        )
        self.assertEqual(stale.status_code, 400, stale.content)

        # Replay with the current draft ETag.
        resp2 = self.client.post(
            '/internal/v2/billing/statements/drafts',
            data=replay_data,
            content_type='application/json',
            HTTP_AUTHORIZATION='Bearer test-internal-token',
            HTTP_IF_MATCH=body['books_draft_etag'],
        )
        self.assertEqual(resp2.status_code, 200)
        self.assertTrue(resp2.json().get('idempotent_replay'))

    def test_rate_and_export_fails_closed_when_billable_rate_is_missing(self):
        _seed_event(
            self.acct,
            metric='unrated.api_meter',
            quantity='1',
            occurred_at=self.start + timedelta(hours=2),
            unit='request',
        )
        resp = self.client.post(
            '/internal/v2/billing/statements/drafts',
            data={
                'billing_account_id': str(self.acct.id),
                'period_start': self.start.isoformat(),
                'period_end': self.end.isoformat(),
                'seller_legal_entity_id': 'sel',
                'seller_gst_registration_id': 'gst',
                'books_organization_id': 'books',
                'action': 'rate_and_export',
            },
            content_type='application/json',
            HTTP_AUTHORIZATION='Bearer test-internal-token',
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn('no RateVersion', resp.json()['detail'])
        self.assertFalse(RatedUsageLine.objects.exists())
        statement = BillingStatement.objects.get()
        self.assertEqual(statement.status, BillingStatement.Status.OPEN)

    def test_open_adjustment_validates_original_without_orphan_side_effects(self):
        request_business_key = {
            'billing_account_id': str(self.acct.id),
            'period_start': (self.start - timedelta(days=30)).isoformat(),
            'period_end': (self.end - timedelta(days=30)).isoformat(),
            'seller_legal_entity_id': 'phantom-seller',
            'seller_gst_registration_id': 'phantom-gst',
            'books_organization_id': 'phantom-books',
            'statement_kind': BillingStatement.Kind.ORDINARY,
            'adjustment_sequence': 99,
            'action': 'open_adjustment',
        }

        missing = self.client.post(
            '/internal/v2/billing/statements/drafts',
            data={**request_business_key, 'original_statement_id': 'missing-statement'},
            content_type='application/json',
            HTTP_AUTHORIZATION='Bearer test-internal-token',
        )
        self.assertEqual(missing.status_code, 400, missing.content)
        self.assertEqual(BillingStatement.objects.count(), 0)

        original = open_or_get_statement(
            billing_account=self.acct,
            period_start=self.start,
            period_end=self.end,
            seller_legal_entity_id='sel',
            seller_gst_registration_id='gst',
            books_organization_id='books',
        )
        not_finalized = self.client.post(
            '/internal/v2/billing/statements/drafts',
            data={**request_business_key, 'original_statement_id': original.statement_id},
            content_type='application/json',
            HTTP_AUTHORIZATION='Bearer test-internal-token',
        )
        self.assertEqual(not_finalized.status_code, 409, not_finalized.content)
        self.assertEqual(BillingStatement.objects.count(), 1)

        BillingStatement.objects.filter(pk=original.pk).update(
            status=BillingStatement.Status.FINALIZED,
            finalized_at=timezone.now(),
        )
        created = self.client.post(
            '/internal/v2/billing/statements/drafts',
            data={
                **request_business_key,
                'original_statement_id': original.statement_id,
                'reason': 'late correction',
            },
            content_type='application/json',
            HTTP_AUTHORIZATION='Bearer test-internal-token',
        )
        self.assertEqual(created.status_code, 201, created.content)
        body = created.json()
        self.assertEqual(body['statement_kind'], BillingStatement.Kind.ADJUSTMENT)
        self.assertEqual(body['adjustment_sequence'], 1)
        self.assertEqual(body['original_statement_id'], original.statement_id)
        self.assertEqual(
            BillingStatement.objects.filter(statement_kind=BillingStatement.Kind.ORDINARY).count(),
            1,
        )
        self.assertEqual(
            BillingStatement.objects.filter(statement_kind=BillingStatement.Kind.ADJUSTMENT).count(),
            1,
        )


class StatementCreationValidationTests(TestCase):
    def test_invalid_period_is_rejected_before_persist(self):
        acct = create_billing_account(
            account_type=BillingAccount.AccountType.INDIVIDUAL,
            owner_subject='user:invalid-period',
        )
        start = timezone.now().replace(microsecond=0)
        with self.assertRaisesRegex(StatementError, 'period_end must be after period_start'):
            open_or_get_statement(
                billing_account=acct,
                period_start=start,
                period_end=start,
            )
        self.assertFalse(BillingStatement.objects.exists())
