import json
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from hive_platform.models import TenantSubscription
from claws.usage_metering import (
    build_books_invoice_draft,
    get_metric_rates_for_subscription,
    post_invoice_draft_to_books,
)


class Command(BaseCommand):
    help = 'Generate a Books-ready draft invoice payload from HIVE-319 usage rollups.'

    def add_arguments(self, parser):
        parser.add_argument('--tenant-org-id', required=True)
        parser.add_argument('--tenant-org-slug', default='')
        parser.add_argument('--customer-name', default='')
        parser.add_argument('--customer-gstin', default='')
        parser.add_argument('--billing-email', default='')
        parser.add_argument('--place-of-supply-state-code', default='')
        parser.add_argument('--period-start', required=True)
        parser.add_argument('--period-end', required=True)
        parser.add_argument('--metric-rates-json', help='JSON object: metric -> rate per unit')
        parser.add_argument(
            '--use-subscription-rates',
            action='store_true',
            help='Load metric rates from the tenant subscription plan catalog.',
        )
        parser.add_argument('--export', action='store_true', help='Use zero-rated export tax treatment instead of domestic GST')
        parser.add_argument('--post-to-books', action='store_true')

    def handle(self, *args, **options):
        try:
            period_start = date.fromisoformat(options['period_start'])
            period_end = date.fromisoformat(options['period_end'])
        except ValueError as exc:
            raise CommandError('period dates must be YYYY-MM-DD') from exc
        if period_end < period_start:
            raise CommandError('--period-end must be on/after --period-start')
        if options['use_subscription_rates']:
            try:
                subscription = TenantSubscription.objects.select_related('plan').get(
                    tenant_org_id=str(options['tenant_org_id']),
                )
            except TenantSubscription.DoesNotExist as exc:
                raise CommandError('tenant has no subscription; pass --metric-rates-json or bind a plan first') from exc
            rates = get_metric_rates_for_subscription(subscription)
        else:
            if not options['metric_rates_json']:
                raise CommandError('--metric-rates-json is required unless --use-subscription-rates is set')
            try:
                rates = json.loads(options['metric_rates_json'])
            except json.JSONDecodeError as exc:
                raise CommandError('--metric-rates-json must be valid JSON') from exc
            if not isinstance(rates, dict):
                raise CommandError('--metric-rates-json must be a JSON object')
        draft = build_books_invoice_draft(
            tenant_org_id=options['tenant_org_id'],
            tenant_org_slug=options['tenant_org_slug'],
            customer_name=options['customer_name'],
            customer_gstin=options['customer_gstin'],
            billing_email=options['billing_email'],
            period_start=period_start,
            period_end=period_end,
            metric_rates=rates,
            domestic_gst=not options['export'],
            place_of_supply_state_code=options['place_of_supply_state_code'],
        )
        if options['post_to_books']:
            post_invoice_draft_to_books(draft)
        self.stdout.write(json.dumps(draft.books_payload, indent=2, sort_keys=True))
