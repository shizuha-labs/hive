import json
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from claws.usage_metering import assign_tenant_plan


class Command(BaseCommand):
    help = 'Bind a tenant org to a HIVE billing plan (manual collection first).'

    def add_arguments(self, parser):
        parser.add_argument('--tenant-org-id', required=True)
        parser.add_argument('--plan-id', required=True)
        parser.add_argument('--tenant-org-slug', default='')
        parser.add_argument('--customer-name', default='')
        parser.add_argument('--customer-gstin', default='')
        parser.add_argument('--billing-email', default='')
        parser.add_argument('--period-start')
        parser.add_argument('--period-end')
        parser.add_argument('--status', default='active', choices=['trial', 'active', 'past_due', 'suspended', 'cancelled'])
        parser.add_argument('--metadata-json', default='{}')
        parser.add_argument('--json', action='store_true')

    def _date(self, raw, field):
        if not raw:
            return None
        try:
            return date.fromisoformat(raw)
        except ValueError as exc:
            raise CommandError(f'{field} must be YYYY-MM-DD') from exc

    def handle(self, *args, **options):
        try:
            metadata = json.loads(options['metadata_json'])
        except json.JSONDecodeError as exc:
            raise CommandError('--metadata-json must be valid JSON') from exc
        if not isinstance(metadata, dict):
            raise CommandError('--metadata-json must be a JSON object')

        subscription = assign_tenant_plan(
            tenant_org_id=options['tenant_org_id'],
            tenant_org_slug=options['tenant_org_slug'],
            customer_name=options['customer_name'],
            customer_gstin=options['customer_gstin'],
            billing_email=options['billing_email'],
            plan_id=options['plan_id'],
            period_start=self._date(options['period_start'], '--period-start'),
            period_end=self._date(options['period_end'], '--period-end'),
            status=options['status'],
            metadata=metadata,
        )
        payload = {
            'tenant_org_id': subscription.tenant_org_id,
            'tenant_org_slug': subscription.tenant_org_slug,
            'plan_id': subscription.plan.plan_id if subscription.plan else None,
            'status': subscription.status,
        }
        if options['json']:
            self.stdout.write(json.dumps(payload, sort_keys=True))
            return
        self.stdout.write(self.style.SUCCESS(
            f"{payload['tenant_org_id']} -> {payload['plan_id']} ({payload['status']})"
        ))
