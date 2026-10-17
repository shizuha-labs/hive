import json

from django.core.management.base import BaseCommand

from claws.usage_metering import seed_default_billing_catalog


class Command(BaseCommand):
    help = 'Upsert HIVE-346 default billing plans, rate rows, and quota envelopes.'

    def add_arguments(self, parser):
        parser.add_argument('--json', action='store_true')

    def handle(self, *args, **options):
        result = seed_default_billing_catalog()
        if options['json']:
            self.stdout.write(json.dumps(result, sort_keys=True))
            return
        self.stdout.write(self.style.SUCCESS(
            f"Seeded {result['plans']} billing plan(s) and {result['rates']} rate row(s)"
        ))
