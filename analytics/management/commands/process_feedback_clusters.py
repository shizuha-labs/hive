from django.core.management.base import BaseCommand

from analytics.clustering import process_feedback_watermark


class Command(BaseCommand):
    help = 'HIVE-395: process the feedback clustering watermark into Theme rows.'

    def handle(self, *args, **options):
        result = process_feedback_watermark()
        self.stdout.write(self.style.SUCCESS(f'processed feedback clustering watermark: {result}'))
