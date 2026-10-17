"""HIVE-408: billing usage-metering record failures are counted fail-loud
(PLAT-1254), labeled by producer source, and exposed for Prometheus scraping.

Proves record_usage_event — the single chokepoint for every producer path — emits
hive_usage_metering_failure_total{source=…} on failure and re-raises (fail-closed),
that the counter buckets by source, that success does NOT increment, and that the
metrics endpoint renders the counters (always emitting every source at >=0).
"""
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.core.management import call_command
from rest_framework.test import APIClient

from claws import usage_metering_metrics as metrics
from hive_platform.models import UsageEvent
from claws.usage_metering import record_usage_event

METRICS_URL = '/api/v1/internal/usage-metering/metrics/'


class Hive408UsageMeteringFailureMetricTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def _record(self, source, *, idempotency_key=None):
        return record_usage_event(
            source=source, metric=UsageEvent.Metric.AGENT_TURN, quantity=1,
            unit='turn', tenant_org_id='org-1', idempotency_key=idempotency_key,
        )

    def test_generated_identity_persistence_failure_increments_counter_labeled_by_source(self):
        # PLAT-4482 routes every event, including a locally generated identity,
        # through the replay-safe source/id get-or-create path.
        with patch('claws.usage_metering.UsageEvent.objects.get_or_create', side_effect=RuntimeError('db down')):
            with self.assertRaises(RuntimeError):
                self._record(UsageEvent.Source.RUNTIME)
        snap = metrics.snapshot()
        self.assertEqual(snap['runtime'], 1)
        self.assertEqual(snap['cortex'], 0)
        self.assertEqual(snap['storage'], 0)

    def test_get_or_create_failure_also_counted(self):
        with patch('claws.usage_metering.UsageEvent.objects.get_or_create', side_effect=RuntimeError('db down')):
            with self.assertRaises(RuntimeError):
                self._record(UsageEvent.Source.CORTEX, idempotency_key='k1')  # get_or_create path
        self.assertEqual(metrics.snapshot()['cortex'], 1)

    def test_success_does_not_increment(self):
        self._record(UsageEvent.Source.RUNTIME)
        self.assertEqual(sum(metrics.snapshot().values()), 0)

    def test_unknown_source_is_bucketed(self):
        metrics.record_failure('not-a-real-source')
        self.assertEqual(metrics.snapshot()['unknown'], 1)

    def test_prometheus_emits_every_source_even_at_zero(self):
        metrics.record_failure('runtime')
        text = metrics.to_prometheus()
        self.assertIn('# TYPE hive_usage_metering_failure_total counter', text)
        self.assertIn('hive_usage_metering_failure_total{source="runtime"} 1', text)
        self.assertIn('hive_usage_metering_failure_total{source="cortex"} 0', text)
        self.assertIn('hive_usage_metering_failure_total{source="storage"} 0', text)

    def test_metrics_endpoint_renders_counter(self):
        metrics.record_failure('storage')
        resp = self.client.get(METRICS_URL)
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn('hive_usage_metering_failure_total{source="storage"} 1', body)
        self.assertTrue(resp['Content-Type'].startswith('text/plain'))

    def test_outbox_delivery_outcomes_and_dispatch_heartbeat_are_exported(self):
        metrics.record_outbox_delivery('retry', 'urn:shizuha:cortex:worker')
        metrics.record_outbox_delivery('conflict', 'urn:shizuha:cortex:worker')
        call_command('dispatch_usage_outbox', limit=1)

        body = metrics.to_prometheus()
        self.assertIn(
            'hive_usage_outbox_delivery_total{outcome="retry",source="cortex"} 1',
            body,
        )
        self.assertIn(
            'hive_usage_outbox_delivery_total{outcome="conflict",source="cortex"} 1',
            body,
        )
        self.assertGreater(metrics.outbox_snapshot()['last_dispatch'], 0)
        self.assertIn('hive_usage_outbox_last_dispatch_timestamp_seconds', body)
