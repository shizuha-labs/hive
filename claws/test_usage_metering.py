from datetime import date, datetime, timedelta
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import transaction
from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory
from django.utils import timezone

from .models import Claw
from hive_platform.models import (
    AdmissionAuthoritySnapshot, BillingPlan, PricingRate, TenantSubscription,
    UsageDailyRollup, UsageEvent, UsageEventOutbox, UsageInvoiceDraft,
)
from .metering_events import (
    MeteringEventConflict,
    authority_snapshot_sha256,
    deliver_usage_outbox,
    enqueue_usage_cloudevent,
    register_admission_authority_snapshot,
)
from .usage_metering import (
    assign_tenant_plan,
    build_books_invoice_draft,
    evaluate_quota,
    get_metric_rates_for_subscription,
    post_invoice_draft_to_books,
    rebuild_daily_rollups,
    record_agent_turn,
    record_cortex_token_usage,
    record_claw_uptime_seconds,
    record_storage_gib_hours,
    record_usage_event,
    seed_default_billing_catalog,
)


def _deliver_ok(outbox_id):
    """Mark an outbox row DELIVERED (stand-in for deliver_usage_outbox success)."""
    row = UsageEventOutbox.objects.select_for_update().get(id=outbox_id)
    row.status = UsageEventOutbox.Status.DELIVERED
    row.delivered_at = timezone.now()
    row.last_error = ''
    row.save(update_fields=['status', 'delivered_at', 'last_error'])
    return row


class UsageMeteringTests(TestCase):
    def _dt(self, hour):
        value = datetime(2026, 7, 1, hour, 0, 0)
        return timezone.make_aware(value) if getattr(settings, 'USE_TZ', False) else value

    def _claw(self, owner_id=42, username='acme'):
        return Claw.objects.create(
            owner_id=owner_id,
            owner_username=username,
            owner_email=f'{username}@example.com',
            name='Billing Claw',
            status=Claw.Status.RUNNING,
            pvc_size='10Gi',
        )

    def test_real_producer_helpers_are_idempotent_and_roll_up_by_tenant(self):
        claw = self._claw()
        occurred = self._dt(4)
        start = occurred - timedelta(hours=2)

        record_agent_turn(claw=claw, source_event_id='wa-msg-1', occurred_at=occurred)
        record_agent_turn(claw=claw, source_event_id='wa-msg-1', occurred_at=occurred)
        record_cortex_token_usage(
            claw=claw,
            source_event_id='wa-msg-1',
            usage={'prompt_tokens': 12, 'completion_tokens': 5},
            model='Qwen',
            occurred_at=occurred,
        )
        record_cortex_token_usage(
            claw=claw,
            source_event_id='wa-msg-1',
            usage={'prompt_tokens': 12, 'completion_tokens': 5},
            model='Qwen',
            occurred_at=occurred,
        )
        record_claw_uptime_seconds(
            claw=claw,
            window_start=start,
            window_end=occurred,
            source_event_id='uptime-window-1',
        )
        record_storage_gib_hours(
            claw=claw,
            window_start=start,
            window_end=occurred,
            source_event_id='storage-window-1',
        )

        self.assertEqual(UsageEvent.objects.count(), 5)
        self.assertTrue(UsageEvent.objects.filter(tenant_org_id='42', tenant_org_slug='acme').exists())
        self.assertEqual(
            UsageEvent.objects.get(metric=UsageEvent.Metric.CORTEX_INPUT_TOKEN).quantity,
            Decimal('12'),
        )
        self.assertEqual(
            UsageEvent.objects.get(metric=UsageEvent.Metric.STORAGE_GIB_HOUR).quantity,
            Decimal('20.000000'),
        )

        self.assertEqual(rebuild_daily_rollups(date(2026, 7, 1)), 5)
        rollups = {row.metric: row for row in UsageDailyRollup.objects.all()}
        self.assertEqual(rollups[UsageEvent.Metric.AGENT_TURN].quantity, Decimal('1.000000'))
        self.assertEqual(rollups[UsageEvent.Metric.CLAW_UPTIME_SECOND].quantity, Decimal('7200.000000'))
        self.assertEqual(rollups[UsageEvent.Metric.STORAGE_GIB_HOUR].quantity, Decimal('20.000000'))


    @override_settings(HIVE_INTERNAL_AUTH_TOKEN='internal-test-token',
                       CORTEX_BASE_URL='http://cortex:8040',
                       HIVE_CORTEX_DEFAULT_MODEL='Qwen3.6-27B')
    @patch('claws.views.requests.post')
    def test_whatsapp_runtime_path_emits_turn_and_cortex_usage_events(self, mock_post):
        from types import SimpleNamespace
        from django.core.cache import cache
        from claws import lifecycle, views

        cache.clear()
        claw = self._claw()
        mock_post.return_value = SimpleNamespace(
            status_code=200,
            text='ok',
            json=lambda: {
                'choices': [{'message': {'content': 'metered reply'}}],
                'usage': {'prompt_tokens': 21, 'completion_tokens': 8},
            },
        )
        token = lifecycle.internal_auth_token(claw)
        request = APIRequestFactory().post(
            '/',
            {
                'phone_jid': '15551234567@s.whatsapp.net',
                'body': 'hello',
                'message_id': 'wamid-1',
            },
            format='json',
            HTTP_X_HIVE_INTERNAL_TOKEN=token,
        )

        response = views.InternalWhatsAppMessageView.as_view()(request, claw_id=claw.id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'replied')
        self.assertEqual(UsageEvent.objects.count(), 3)
        self.assertEqual(
            UsageEvent.objects.get(metric=UsageEvent.Metric.AGENT_TURN).tenant_org_id,
            '42',
        )
        self.assertEqual(
            UsageEvent.objects.get(metric=UsageEvent.Metric.CORTEX_INPUT_TOKEN).quantity,
            Decimal('21'),
        )
        self.assertEqual(
            UsageEvent.objects.get(metric=UsageEvent.Metric.CORTEX_OUTPUT_TOKEN).quantity,
            Decimal('8'),
        )

        # Same upstream message id must not double-count on retry/replay.
        replay = APIRequestFactory().post(
            '/',
            {'phone_jid': '15551234567@s.whatsapp.net', 'body': 'hello', 'message_id': 'wamid-1'},
            format='json',
            HTTP_X_HIVE_INTERNAL_TOKEN=token,
        )
        response = views.InternalWhatsAppMessageView.as_view()(replay, claw_id=claw.id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(UsageEvent.objects.count(), 3)

    @override_settings(HIVE_INTERNAL_AUTH_TOKEN='internal-test-token',
                       CORTEX_BASE_URL='http://cortex:8040',
                       HIVE_CORTEX_DEFAULT_MODEL='Qwen3.6-27B')
    @patch('claws.views.record_agent_turn')
    @patch('claws.views.requests.post')
    def test_whatsapp_metering_failure_fails_request_before_success(self, mock_post, mock_turn):
        from types import SimpleNamespace
        from django.core.cache import cache
        from claws import lifecycle, views

        cache.clear()
        claw = self._claw()
        mock_post.return_value = SimpleNamespace(
            status_code=200,
            text='ok',
            json=lambda: {
                'choices': [{'message': {'content': 'reply that must not be acknowledged'}}],
                'usage': {'prompt_tokens': 21, 'completion_tokens': 8},
            },
        )
        mock_turn.side_effect = RuntimeError('db down')
        token = lifecycle.internal_auth_token(claw)
        request = APIRequestFactory().post(
            '/',
            {'phone_jid': '15551234567@s.whatsapp.net', 'body': 'hello', 'message_id': 'wamid-fail'},
            format='json',
            HTTP_X_HIVE_INTERNAL_TOKEN=token,
        )

        response = views.InternalWhatsAppMessageView.as_view()(request, claw_id=claw.id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'upstream_unavailable')
        self.assertEqual(UsageEvent.objects.count(), 0)

    @override_settings(HIVE_INTERNAL_AUTH_TOKEN='internal-test-token',
                       CORTEX_BASE_URL='http://cortex:8040',
                       HIVE_CORTEX_DEFAULT_MODEL='Qwen3.6-27B')
    @patch('claws.views.record_cortex_token_usage')
    @patch('claws.views.requests.post')
    def test_whatsapp_cortex_metering_failure_rolls_back_turn_event(self, mock_post, mock_tokens):
        from types import SimpleNamespace
        from django.core.cache import cache
        from claws import lifecycle, views

        cache.clear()
        claw = self._claw()
        mock_post.return_value = SimpleNamespace(
            status_code=200,
            text='ok',
            json=lambda: {
                'choices': [{'message': {'content': 'reply that must not be acknowledged'}}],
                'usage': {'prompt_tokens': 21, 'completion_tokens': 8},
            },
        )
        mock_tokens.side_effect = RuntimeError('token usage persistence failed')
        token = lifecycle.internal_auth_token(claw)
        request = APIRequestFactory().post(
            '/',
            {'phone_jid': '15551234567@s.whatsapp.net', 'body': 'hello', 'message_id': 'wamid-token-fail'},
            format='json',
            HTTP_X_HIVE_INTERNAL_TOKEN=token,
        )

        response = views.InternalWhatsAppMessageView.as_view()(request, claw_id=claw.id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'upstream_unavailable')
        self.assertEqual(UsageEvent.objects.count(), 0)

    @override_settings(HIVE_INTERNAL_AUTH_TOKEN='internal-test-token',
                       CORTEX_BASE_URL='http://cortex:8040',
                       HIVE_CORTEX_DEFAULT_MODEL='Qwen3.6-27B')
    @patch('claws.views.record_agent_turn')
    @patch('claws.views.requests.post')
    def test_whatsapp_empty_cortex_reply_does_not_record_agent_turn(self, mock_post, mock_turn):
        from types import SimpleNamespace
        from django.core.cache import cache
        from claws import lifecycle, views

        cache.clear()
        claw = self._claw()
        mock_post.return_value = SimpleNamespace(
            status_code=200,
            text='ok',
            json=lambda: {
                'choices': [{'message': {'content': ''}}],
                'usage': {'prompt_tokens': 21, 'completion_tokens': 8},
            },
        )
        token = lifecycle.internal_auth_token(claw)
        request = APIRequestFactory().post(
            '/',
            {'phone_jid': '15551234567@s.whatsapp.net', 'body': 'hello', 'message_id': 'wamid-empty'},
            format='json',
            HTTP_X_HIVE_INTERNAL_TOKEN=token,
        )

        response = views.InternalWhatsAppMessageView.as_view()(request, claw_id=claw.id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'upstream_unavailable')
        mock_turn.assert_not_called()
        self.assertEqual(UsageEvent.objects.count(), 0)

    def test_record_usage_event_rejects_changed_payload_for_producer_key(self):
        first = record_usage_event(
            tenant_org_id='42', tenant_org_slug='acme', owner_id=7,
            source=UsageEvent.Source.CORTEX, metric=UsageEvent.Metric.CORTEX_INPUT_TOKEN,
            quantity=100, unit='token', model='Qwen', occurred_at=self._dt(1),
            idempotency_key='cortex:req-1:input',
        )
        replay = record_usage_event(
            tenant_org_id='42', tenant_org_slug='acme', owner_id=7,
            source=UsageEvent.Source.CORTEX, metric=UsageEvent.Metric.CORTEX_INPUT_TOKEN,
            quantity=100, unit='token', model='Qwen', occurred_at=self._dt(1),
            idempotency_key='cortex:req-1:input',
        )
        self.assertEqual(first.id, replay.id)
        with self.assertRaises(MeteringEventConflict):
            record_usage_event(
                tenant_org_id='42', tenant_org_slug='acme', owner_id=7,
                source=UsageEvent.Source.CORTEX, metric=UsageEvent.Metric.CORTEX_INPUT_TOKEN,
                quantity=999, unit='token', model='Qwen', occurred_at=self._dt(1),
                idempotency_key='cortex:req-1:input',
            )
        self.assertEqual(UsageEvent.objects.count(), 1)
        self.assertEqual(replay.quantity, Decimal('100'))

    def test_rebuild_daily_rollups_sums_by_tenant_metric_unit_and_model(self):
        record_usage_event(
            tenant_org_id='42', tenant_org_slug='acme', owner_id=7,
            source=UsageEvent.Source.CORTEX, metric=UsageEvent.Metric.CORTEX_INPUT_TOKEN,
            quantity=100, unit='token', model='Qwen', occurred_at=self._dt(1),
        )
        record_usage_event(
            tenant_org_id='42', tenant_org_slug='acme', owner_id=7,
            source=UsageEvent.Source.CORTEX, metric=UsageEvent.Metric.CORTEX_INPUT_TOKEN,
            quantity=25, unit='token', model='Qwen', occurred_at=self._dt(2),
        )
        record_usage_event(
            tenant_org_id='42', tenant_org_slug='acme', owner_id=7,
            source=UsageEvent.Source.RUNTIME, metric=UsageEvent.Metric.AGENT_HOUR,
            quantity='1.5', unit='hour', occurred_at=self._dt(3),
        )

        self.assertEqual(rebuild_daily_rollups(date(2026, 7, 1)), 2)
        self.assertEqual(rebuild_daily_rollups(date(2026, 7, 1)), 2)
        self.assertEqual(UsageDailyRollup.objects.count(), 2)
        tokens = UsageDailyRollup.objects.get(metric=UsageEvent.Metric.CORTEX_INPUT_TOKEN)
        self.assertEqual(tokens.quantity, Decimal('125.000000'))
        self.assertEqual(tokens.event_count, 2)
        hours = UsageDailyRollup.objects.get(metric=UsageEvent.Metric.AGENT_HOUR)
        self.assertEqual(hours.quantity, Decimal('1.500000'))

    def test_build_books_invoice_draft_applies_domestic_gst(self):
        UsageDailyRollup.objects.create(
            tenant_org_id='42', tenant_org_slug='acme', owner_id=7,
            day=date(2026, 7, 1), source='cortex', metric='cortex_input_token',
            unit='token', model='Qwen', quantity=Decimal('1000'), event_count=1,
        )
        UsageDailyRollup.objects.create(
            tenant_org_id='42', tenant_org_slug='acme', owner_id=7,
            day=date(2026, 7, 1), source='runtime', metric='agent_hour',
            unit='hour', model='', quantity=Decimal('2'), event_count=1,
        )

        draft = build_books_invoice_draft(
            tenant_org_id='42', tenant_org_slug='acme', customer_name='Acme Pvt Ltd',
            customer_gstin='29ABCDE1234F1Z5', billing_email='ap@acme.example',
            period_start=date(2026, 7, 1), period_end=date(2026, 7, 31),
            metric_rates={'cortex_input_token': '0.01', 'agent_hour': '50'},
            sac_code='998313',
        )

        self.assertEqual(draft.subtotal, Decimal('110.00'))
        self.assertEqual(draft.tax_rate_percent, Decimal('18.00'))
        self.assertEqual(draft.tax_amount, Decimal('19.80'))
        self.assertEqual(draft.total, Decimal('129.80'))
        self.assertEqual(draft.books_payload['tax_treatment'], 'domestic_gst')
        self.assertEqual(draft.books_payload['customer']['gstin'], '29ABCDE1234F1Z5')
        self.assertEqual(draft.books_payload['idempotency_key'], 'hive-usage-invoice:42:2026-07-01:2026-07-31')
        self.assertEqual(draft.books_payload['tax_components'], [
            {'name': 'IGST', 'rate_percent': '18.00', 'amount': '19.80'},
        ])
        self.assertEqual(len(draft.books_payload['line_items']), 2)

    def test_build_books_invoice_draft_splits_intra_state_gst_and_zero_rates_exports(self):
        UsageDailyRollup.objects.create(
            tenant_org_id='42', tenant_org_slug='acme', owner_id=7,
            day=date(2026, 7, 1), source='runtime', metric='agent_hour',
            unit='hour', model='', quantity=Decimal('2'), event_count=1,
        )

        domestic = build_books_invoice_draft(
            tenant_org_id='42', tenant_org_slug='acme', customer_name='Acme Pvt Ltd',
            period_start=date(2026, 7, 1), period_end=date(2026, 7, 31),
            metric_rates={'agent_hour': '50'},
            seller_state_code='29',
            place_of_supply_state_code='29',
        )
        self.assertEqual(domestic.tax_amount, Decimal('18.00'))
        self.assertEqual(domestic.books_payload['tax_components'], [
            {'name': 'CGST', 'rate_percent': '9.00', 'amount': '9.00'},
            {'name': 'SGST', 'rate_percent': '9.00', 'amount': '9.00'},
        ])

        export = build_books_invoice_draft(
            tenant_org_id='42', tenant_org_slug='acme', customer_name='Acme LLC',
            period_start=date(2026, 7, 1), period_end=date(2026, 7, 31),
            metric_rates={'agent_hour': '50'},
            domestic_gst=False,
            currency='USD',
        )
        self.assertEqual(export.tax_amount, Decimal('0.00'))
        self.assertEqual(export.books_payload['tax_treatment'], 'export_zero_rated')
        self.assertEqual(export.books_payload['tax_components'], [])

    @override_settings(HIVE_BOOKS_API_URL='https://books.internal', HIVE_BOOKS_SERVICE_TOKEN='secret')
    @patch('claws.usage_metering.requests.post')
    def test_post_invoice_draft_to_books_marks_posted(self, mock_post):
        draft = UsageInvoiceDraft.objects.create(
            tenant_org_id='42', period_start=date(2026, 7, 1), period_end=date(2026, 7, 31),
            books_payload={'hello': 'world', 'idempotency_key': 'hive-usage-invoice:42:2026-07-01:2026-07-31'},
        )
        response = MagicMock()
        response.content = b'{"id":"inv-1"}'
        response.json.return_value = {'id': 'inv-1'}
        response.raise_for_status.return_value = None
        mock_post.return_value = response

        post_invoice_draft_to_books(draft)

        draft.refresh_from_db()
        self.assertEqual(draft.status, UsageInvoiceDraft.Status.POSTED_TO_BOOKS)
        self.assertEqual(draft.books_invoice_id, 'inv-1')
        mock_post.assert_called_once()
        self.assertEqual(mock_post.call_args.kwargs['headers']['Authorization'], 'Bearer secret')

        self.assertEqual(
            mock_post.call_args.kwargs['headers']['Idempotency-Key'],
            'hive-usage-invoice:42:2026-07-01:2026-07-31',
        )
        self.assertEqual(mock_post.call_args.args[0], 'https://books.internal/shizuha-books/api/hive/invoices/drafts/')

    @override_settings(HIVE_BOOKS_API_URL='https://books.internal', HIVE_BOOKS_SERVICE_TOKEN='secret')
    @patch('claws.usage_metering.requests.put')
    def test_post_invoice_draft_to_books_updates_existing_books_invoice(self, mock_put):
        draft = UsageInvoiceDraft.objects.create(
            tenant_org_id='42', period_start=date(2026, 7, 1), period_end=date(2026, 7, 31),
            books_invoice_id='inv-1',
            books_payload={'hello': 'world', 'idempotency_key': 'hive-usage-invoice:42:2026-07-01:2026-07-31'},
        )
        response = MagicMock()
        response.content = b'{"invoice_id":"inv-1"}'
        response.json.return_value = {'invoice_id': 'inv-1'}
        response.raise_for_status.return_value = None
        mock_put.return_value = response

        post_invoice_draft_to_books(draft)

        mock_put.assert_called_once()
        self.assertEqual(
            mock_put.call_args.args[0],
            'https://books.internal/shizuha-books/api/hive/invoices/drafts/inv-1/',
        )

@override_settings(HIVE_INTERNAL_AUTH_TOKEN='metering-test-token')
class CloudEventIngestionTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        authority = {
            'proof_id': 'proof-42',
            'billing_account_id': 'billing-account-42',
            'reservation_id': 'reservation-42',
            'lease_id': '',
            'payer_link_generation': 3,
            'binding_generation': 7,
            'admission_generation': 11,
        }
        authority['proof_sha256'] = authority_snapshot_sha256(authority)
        register_admission_authority_snapshot(authority)
        self.proof_hash = authority['proof_sha256']

    def event(self, event_id='evt-1', *, quantity='12.500000', event_type='shizuha.usage.tokens.v1'):
        return {
            'specversion': '1.0',
            'id': event_id,
            'source': 'urn:shizuha:cortex:test-producer',
            'type': event_type,
            'subject': 'billing-account-42',
            'dataschema': 'urn:shizuha:metering:usage-event:v1',
            'datacontenttype': 'application/json',
            'time': '2025-01-02T03:04:05Z',
            'data': {
                'meter': 'cortex_input_token',
                'quantity': quantity,
                'unit': 'token',
                'resource_id': 'request-7',
                'metadata': {'model': 'Qwen'},
            },
            'billingaccountid': 'billing-account-42',
            'authorityproofid': 'proof-42',
            'authorityproofhash': self.proof_hash,
            'reservationid': 'reservation-42',
            'payerlinkgen': 3,
            'bindinggen': 7,
            'admissiongen': 11,
        }

    def post_one(self, event, *, token='metering-test-token'):
        from .metering_views import InternalUsageEventIngestView

        request = self.factory.post(
            '/internal/v1/usage/events',
            event,
            format='json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        return InternalUsageEventIngestView.as_view()(request)

    def test_authenticated_ingest_replays_same_hash_and_conflicts_on_changed_payload(self):
        event = self.event()
        created = self.post_one(event)
        replay = self.post_one(event)
        changed = self.event(quantity='99')
        conflict = self.post_one(changed)

        self.assertEqual(created.status_code, 201)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.data['status'], 'replayed')
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.data['error'], 'idempotency_conflict')
        self.assertEqual(UsageEvent.objects.count(), 1)
        stored = UsageEvent.objects.get()
        self.assertEqual(stored.quantity, Decimal('12.500000'))
        self.assertEqual(stored.billing_account_id, 'billing-account-42')
        self.assertEqual(stored.authority_snapshot['admission_generation'], 11)

    def test_ingest_fails_closed_and_rejects_json_number_quantity(self):
        unauthenticated = self.post_one(self.event(), token='wrong')
        numeric_quantity = self.event(event_id='evt-number')
        numeric_quantity['data']['quantity'] = 1.5
        invalid = self.post_one(numeric_quantity)

        self.assertEqual(unauthenticated.status_code, 403)
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.data['field'], 'data.quantity')
        self.assertEqual(UsageEvent.objects.count(), 0)

        invalid_source = self.event(event_id='evt-source')
        invalid_source['source'] = 'not a valid source'
        invalid = self.post_one(invalid_source)
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.data['field'], 'source')

        forged_payer = self.event(event_id='evt-forged-payer')
        forged_payer['billingaccountid'] = 'attacker-selected-account'
        invalid = self.post_one(forged_payer)
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.data['error'], 'invalid_admission_authority')
        self.assertFalse(UsageEvent.objects.filter(event_id='evt-forged-payer').exists())
        self.assertEqual(AdmissionAuthoritySnapshot.objects.count(), 1)

    def test_batch_ingest_is_atomic_and_accepts_late_arrivals(self):
        from .metering_views import InternalUsageEventBatchIngestView

        first = self.event('late-1')
        second = self.event('late-2', quantity='3')
        request = self.factory.post(
            '/internal/v1/usage/events:batch',
            [first, second],
            format='json',
            HTTP_X_HIVE_INTERNAL_TOKEN='metering-test-token',
        )
        response = InternalUsageEventBatchIngestView.as_view()(request)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(response.data['results']), 2)
        self.assertEqual(UsageEvent.objects.count(), 2)
        self.assertEqual(UsageEvent.objects.order_by('event_id').first().occurred_at.year, 2025)

        conflicting = self.event('late-1', quantity='999')
        third = self.event('late-3')
        request = self.factory.post(
            '/internal/v1/usage/events:batch',
            [conflicting, third],
            format='json',
            HTTP_AUTHORIZATION='Bearer metering-test-token',
        )
        response = InternalUsageEventBatchIngestView.as_view()(request)
        self.assertEqual(response.status_code, 409)
        self.assertFalse(UsageEvent.objects.filter(event_id='late-3').exists())

    def test_corrections_form_an_append_only_chain(self):
        self.assertEqual(self.post_one(self.event('original')).status_code, 201)
        adjustment = self.event(
            'adjustment-1',
            quantity='-2.5',
            event_type='shizuha.usage.tokens.adjustment.v1',
        )
        adjustment['data']['correction_of'] = {
            'source': adjustment['source'],
            'id': 'original',
        }
        self.assertEqual(self.post_one(adjustment).status_code, 201)
        reversal = self.event(
            'reversal-1',
            quantity='2.5',
            event_type='shizuha.usage.tokens.reversal.v1',
        )
        reversal['data']['correction_of'] = {
            'source': reversal['source'],
            'id': 'adjustment-1',
        }
        self.assertEqual(self.post_one(reversal).status_code, 201)

        stored = UsageEvent.objects.get(event_id='adjustment-1')
        self.assertEqual(stored.correction_of_event_id, 'original')
        self.assertEqual(UsageEvent.objects.count(), 3)
        stored.metric = 'mutated'
        with self.assertRaises(ValidationError):
            stored.save()
        with self.assertRaises(ValidationError):
            UsageEvent.objects.filter(event_id='original').delete()

    def test_correction_cannot_change_payer_or_admission_authority(self):
        self.assertEqual(self.post_one(self.event('original')).status_code, 201)
        other_authority = {
            'proof_id': 'proof-other',
            'billing_account_id': 'billing-account-other',
            'reservation_id': 'reservation-other',
            'lease_id': 'lease-other',
            'payer_link_generation': 4,
            'binding_generation': 8,
            'admission_generation': 12,
        }
        other_authority['proof_sha256'] = authority_snapshot_sha256(other_authority)
        register_admission_authority_snapshot(other_authority)

        adjustment = self.event(
            'cross-payer-adjustment',
            quantity='-12.500000',
            event_type='shizuha.usage.tokens.adjustment.v1',
        )
        adjustment['data']['correction_of'] = {
            'source': adjustment['source'],
            'id': 'original',
        }
        adjustment.update({
            'billingaccountid': other_authority['billing_account_id'],
            'authorityproofid': other_authority['proof_id'],
            'authorityproofhash': other_authority['proof_sha256'],
            'reservationid': other_authority['reservation_id'],
            'leaseid': other_authority['lease_id'],
            'payerlinkgen': other_authority['payer_link_generation'],
            'bindinggen': other_authority['binding_generation'],
            'admissiongen': other_authority['admission_generation'],
        })

        response = self.post_one(adjustment)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['error'], 'invalid_admission_authority')
        self.assertEqual(response.data['field'], 'data.correction_of')
        self.assertFalse(UsageEvent.objects.filter(event_id='cross-payer-adjustment').exists())
        self.assertEqual(UsageEvent.objects.count(), 1)

    def test_transactional_outbox_retries_same_envelope_until_delivery(self):
        event = self.event('outbox-1')
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                enqueue_usage_cloudevent(event)
                raise RuntimeError('resource mutation rolled back')
        self.assertEqual(UsageEventOutbox.objects.count(), 0)

        with transaction.atomic():
            outbox = enqueue_usage_cloudevent(event)
        failed = deliver_usage_outbox(
            outbox.id,
            http_post=lambda *args, **kwargs: SimpleNamespace(status_code=503, text='retry'),
        )
        self.assertEqual(failed.status, UsageEventOutbox.Status.PENDING)
        self.assertEqual(failed.attempts, 1)
        delivered = deliver_usage_outbox(
            outbox.id,
            http_post=lambda *args, **kwargs: SimpleNamespace(status_code=201, text='ok'),
        )
        self.assertEqual(delivered.status, UsageEventOutbox.Status.DELIVERED)
        self.assertEqual(delivered.attempts, 2)
        self.assertEqual(delivered.envelope, event)

    def test_dispatch_command_honors_time_budget(self):
        # PLAT-6295: a burst of pending rows or a slow Metering Core endpoint
        # must not push the dispatcher past its CronJob deadline. The command
        # stops mid-batch when the time budget is exhausted, leaving remaining
        # rows PENDING for the next run.
        from django.core.management import call_command
        from io import StringIO

        with transaction.atomic():
            for i in range(3):
                enqueue_usage_cloudevent(self.event(f'budget-{i}'))
        self.assertEqual(UsageEventOutbox.objects.filter(
            status=UsageEventOutbox.Status.PENDING).count(), 3)

        out = StringIO()
        # Simulate the budget elapsing before the first row is processed: the
        # first monotonic() read (deadline = now + budget) is far in the past by
        # the loop's first budget check, so the run stops immediately.
        with patch(
            'claws.management.commands.dispatch_usage_outbox.deliver_usage_outbox',
            side_effect=lambda outbox_id: UsageEventOutbox.objects.get(id=outbox_id),
        ), patch(
            'claws.management.commands.dispatch_usage_outbox.time.monotonic',
            side_effect=[0.0, 1000.0],
        ):
            call_command(
                'dispatch_usage_outbox', '--limit', '1000',
                '--time-budget-seconds', '60', stdout=out,
            )
        # Budget exhausted before any row could be delivered — all stay PENDING.
        self.assertIn('time_budget_exhausted', out.getvalue())
        self.assertEqual(UsageEventOutbox.objects.filter(
            status=UsageEventOutbox.Status.PENDING).count(), 3)

    def test_dispatch_command_delivers_within_budget(self):
        from django.core.management import call_command
        from io import StringIO

        with transaction.atomic():
            for i in range(2):
                enqueue_usage_cloudevent(self.event(f'ok-{i}'))
        out = StringIO()
        with patch(
            'claws.management.commands.dispatch_usage_outbox.deliver_usage_outbox',
            side_effect=lambda outbox_id: _deliver_ok(outbox_id),
        ):
            call_command(
                'dispatch_usage_outbox', '--limit', '1000',
                '--time-budget-seconds', '60', stdout=out,
            )
        self.assertIn('processed=2 delivered=2', out.getvalue())
        self.assertEqual(UsageEventOutbox.objects.filter(
            status=UsageEventOutbox.Status.DELIVERED).count(), 2)


class BillingPlanCatalogTests(TestCase):
    def test_seed_default_catalog_creates_plans_rates_and_quota_envelopes(self):
        result = seed_default_billing_catalog()

        self.assertEqual(result, {'plans': 3, 'rates': 12})
        team = BillingPlan.objects.get(plan_id='team-pod')
        self.assertEqual(team.monthly_base_price, Decimal('49999.00'))
        self.assertEqual(team.included_usage['agent_turn'], '20000')
        self.assertEqual(team.soft_quota['cortex_output_token'], '12800000')
        self.assertEqual(team.hard_quota['storage_gib_hour'], '37200')
        self.assertEqual(team.rates.count(), 4)
        rate = PricingRate.objects.get(plan=team, metric=UsageEvent.Metric.CORTEX_OUTPUT_TOKEN)
        self.assertEqual(rate.unit, 'token')
        self.assertEqual(rate.included_quantity, Decimal('16000000.000000'))

        # Idempotent seed: rerunning updates in place, not duplicate rows.
        seed_default_billing_catalog()
        self.assertEqual(BillingPlan.objects.count(), 3)
        self.assertEqual(PricingRate.objects.count(), 12)

    def test_assign_tenant_plan_and_rate_lookup(self):
        seed_default_billing_catalog()

        subscription = assign_tenant_plan(
            tenant_org_id='tenant-42',
            tenant_org_slug='acme',
            customer_name='Acme Pvt Ltd',
            billing_email='ap@acme.example',
            plan_id='team-pod',
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            metadata={'source': 'test'},
        )

        self.assertEqual(subscription.status, TenantSubscription.Status.ACTIVE)
        self.assertEqual(subscription.plan.plan_id, 'team-pod')
        rates = get_metric_rates_for_subscription(subscription)
        self.assertEqual(rates[UsageEvent.Metric.AGENT_TURN], Decimal('0.400000'))
        self.assertIn(UsageEvent.Metric.CORTEX_INPUT_TOKEN, rates)

    def test_invoice_command_can_use_subscription_plan_rates(self):
        seed_default_billing_catalog()
        assign_tenant_plan(
            tenant_org_id='tenant-42',
            tenant_org_slug='acme',
            customer_name='Acme Pvt Ltd',
            billing_email='ap@acme.example',
            plan_id='team-pod',
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
        )
        UsageDailyRollup.objects.create(
            tenant_org_id='tenant-42', tenant_org_slug='acme', owner_id=7,
            day=date(2026, 7, 1), source='runtime', metric='agent_turn',
            unit='turn', model='', quantity=Decimal('10'), event_count=1,
        )

        out = StringIO()
        call_command(
            'generate_usage_invoice',
            tenant_org_id='tenant-42',
            tenant_org_slug='acme',
            customer_name='Acme Pvt Ltd',
            period_start='2026-07-01',
            period_end='2026-07-31',
            use_subscription_rates=True,
            stdout=out,
        )

        self.assertIn('"metric": "agent_turn"', out.getvalue())
        self.assertIn('"subtotal": "4.00"', out.getvalue())

    def test_evaluate_quota_warns_and_caps_only_target_tenant(self):
        seed_default_billing_catalog()
        assign_tenant_plan(
            tenant_org_id='tenant-42',
            tenant_org_slug='acme',
            plan_id='team-pod',
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
        )
        assign_tenant_plan(
            tenant_org_id='tenant-99',
            tenant_org_slug='other',
            plan_id='team-pod',
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
        )
        UsageDailyRollup.objects.create(
            tenant_org_id='tenant-42', tenant_org_slug='acme', owner_id=7,
            day=date(2026, 7, 1), source='runtime', metric='agent_turn',
            unit='turn', model='', quantity=Decimal('15999'), event_count=1,
        )
        UsageDailyRollup.objects.create(
            tenant_org_id='tenant-99', tenant_org_slug='other', owner_id=8,
            day=date(2026, 7, 1), source='runtime', metric='agent_turn',
            unit='turn', model='', quantity=Decimal('25000'), event_count=1,
        )

        warning = evaluate_quota(
            tenant_org_id='tenant-42',
            metric_quantities={'agent_turn': '1'},
            as_of=date(2026, 7, 15),
        )
        self.assertTrue(warning['allowed'])
        self.assertEqual(warning['level'], 'soft_warning')
        self.assertEqual(warning['decisions']['agent_turn']['projected_quantity'], '16000.000000')

        capped = evaluate_quota(
            tenant_org_id='tenant-42',
            metric_quantities={'agent_turn': '5000'},
            as_of=date(2026, 7, 15),
        )
        self.assertFalse(capped['allowed'])
        self.assertEqual(capped['level'], 'hard_cap')
        self.assertEqual(capped['decisions']['agent_turn']['level'], 'hard_cap')

        # The over-quota tenant-99 rollup is isolated from tenant-42 decisions.
        other = evaluate_quota(
            tenant_org_id='tenant-42',
            metric_quantities={'cortex_output_token': '1'},
            as_of=date(2026, 7, 15),
        )
        self.assertTrue(other['allowed'])
        self.assertEqual(other['decisions']['cortex_output_token']['level'], 'ok')
