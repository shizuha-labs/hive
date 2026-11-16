"""PLAT-9124: Hive runtime-hours meters — the five required test classes.

Replay-conflict, late-event, watermark, two-account isolation, outbox retry —
against the transactional emitter + at-least-once delivery, mirroring the
accepted R1B producer-adapter test suite (scs#72).
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from fleet.models import FleetAgent
from fleet.runtime_metering import core_client, delivery, envelope, events
from fleet.runtime_metering.models import RuntimeMeterCheckpoint, RuntimeUsageOutbox


def _agent(username='metered-agent'):
    return FleetAgent.objects.create(
        agent_username=username,
        status=FleetAgent.Status.RUNNING,
    )


class ReplayConflictTests(TestCase):
    """Same (source, event_id) + byte-identical envelope replays once; a
    changed payload hash under the same id is a 409 conflict."""

    def test_same_row_builds_byte_identical_envelope(self):
        agent = _agent()
        row = events.enqueue_runtime_hours(
            agent=agent, subject='acct-1',
            now=timezone.now() + timedelta(hours=1),
        )
        self.assertIsNotNone(row)
        env1 = envelope.build_envelope(row)
        env2 = envelope.build_envelope(row)
        self.assertEqual(env1, env2)
        self.assertEqual(env1['specversion'], '1.0')
        self.assertEqual(env1['type'], 'io.shizuha.usage.measured.v1')
        self.assertEqual(env1['source'], 'urn:shizuha:hive:fleet')

    def test_changed_payload_same_id_is_reported_conflict(self):
        agent = _agent()
        row = events.enqueue_runtime_hours(
            agent=agent, subject='acct-1',
            now=timezone.now() + timedelta(hours=1),
        )
        # Tamper with the durable data AFTER the fact: the core would answer
        # 409 for the changed hash under the same (source, event_id).
        row.data = dict(row.data, quantity='9.999999')
        row.save(update_fields=['data'])
        with mock.patch.object(
            core_client, 'post_usage_events_batch',
            side_effect=core_client.MeteringCoreError(
                code='payload_hash_conflict', detail='hash mismatch', http_status=409),
        ):
            stats = delivery.deliver_pending()
        self.assertEqual(stats['conflict'], 1)
        row.refresh_from_db()
        self.assertEqual(row.status, RuntimeUsageOutbox.Status.FAILED)
        self.assertIn('conflict', row.last_error)


class LateEventTests(TestCase):
    """A meter whose interval ended well before delivery time is still
    accepted by shape (the core's late-arrival window is 72h) and carries the
    interval explicitly."""

    def test_late_event_carries_explicit_interval_and_is_deliverable(self):
        agent = _agent()
        past = timezone.now() - timedelta(hours=70)
        row = events.enqueue_runtime_hours(agent=agent, subject='acct-1', now=past + timedelta(hours=1))
        self.assertIsNotNone(row)
        env = envelope.build_envelope(row)
        self.assertIn('interval', env['data'])
        self.assertLess(env['data']['interval']['end'], timezone.now().isoformat())
        with mock.patch.object(
            core_client, 'post_usage_events_batch', return_value=[{'status': 'accepted'}],
        ) as post:
            stats = delivery.deliver_pending()
        self.assertEqual(stats['delivered'], 1)
        args = post.call_args
        self.assertEqual(args[0][0][0]['id'], str(row.event_id))


class WatermarkTests(TestCase):
    """The per-agent checkpoint is the watermark: emission meters only the
    interval since the last checkpoint, and the checkpoint advances
    transactionally with the outbox insert."""

    def test_checkpoint_advances_and_intervals_do_not_overlap(self):
        agent = _agent()
        t0 = timezone.now()
        row1 = events.enqueue_runtime_hours(agent=agent, subject='acct-1', now=t0 + timedelta(minutes=30))
        self.assertIsNotNone(row1)
        checkpoint = RuntimeMeterCheckpoint.objects.get(agent=agent)
        self.assertEqual(checkpoint.last_emitted_at, row1.interval_end)
        row2 = events.enqueue_runtime_hours(agent=agent, subject='acct-1', now=t0 + timedelta(minutes=90))
        self.assertIsNotNone(row2)
        self.assertGreaterEqual(row2.interval_start, row1.interval_end)
        self.assertLess(row1.interval_end, row2.interval_end)

    def test_first_observation_seeds_checkpoint_without_emitting(self):
        agent = _agent()
        row = events.enqueue_runtime_hours(agent=agent, subject='acct-1', now=timezone.now())
        self.assertIsNone(row)
        self.assertTrue(RuntimeMeterCheckpoint.objects.filter(agent=agent).exists())
        self.assertEqual(RuntimeUsageOutbox.objects.count(), 0)

    def test_interval_capped_at_max_one_hour(self):
        agent = _agent()
        t0 = timezone.now()
        events.enqueue_runtime_hours(agent=agent, subject='acct-1', now=t0)
        row = events.enqueue_runtime_hours(agent=agent, subject='acct-1', now=t0 + timedelta(hours=5))
        self.assertIsNotNone(row)
        elapsed = (row.interval_end - row.interval_start).total_seconds()
        self.assertLessEqual(elapsed, 3600.0)
        quantity = Decimal(row.data['quantity'])
        self.assertLessEqual(quantity, Decimal('1.0'))


class TwoAccountIsolationTests(TestCase):
    """Meters for different billing subjects never share an event id or blur
    dimensions — each row carries exactly its own subject."""

    def test_same_agent_interval_different_subjects_are_distinct_facts(self):
        agent = _agent()
        now = timezone.now() + timedelta(hours=1)
        row_a = events.enqueue_runtime_hours(agent=agent, subject='acct-a', now=now)
        # A second emission for the same agent would normally be blocked by the
        # checkpoint; simulate a parallel producer for another account by
        # resetting the checkpoint (the isolation property under test is the
        # subject binding, not the watermark).
        RuntimeMeterCheckpoint.objects.filter(agent=agent).update(
            last_emitted_at=now - timedelta(minutes=30))
        row_b = events.enqueue_runtime_hours(agent=agent, subject='acct-b', now=now)
        self.assertIsNotNone(row_a)
        self.assertIsNotNone(row_b)
        self.assertNotEqual(row_a.event_id, row_b.event_id)
        self.assertEqual(row_a.subject, 'acct-a')
        self.assertEqual(row_b.subject, 'acct-b')
        self.assertEqual(row_a.data['billing_account_id'], 'acct-a')
        self.assertEqual(row_b.data['billing_account_id'], 'acct-b')


class OutboxRetryTests(TestCase):
    """Transport outage keeps rows retryable; redelivery is the SAME event and
    succeeds when the core returns; rows are never deleted."""

    def test_unavailable_then_recover_delivers_same_event(self):
        agent = _agent()
        row = events.enqueue_runtime_hours(
            agent=agent, subject='acct-1', now=timezone.now() + timedelta(hours=1))
        with mock.patch.object(
            core_client, 'post_usage_events_batch',
            side_effect=core_client.MeteringCoreUnavailable('connection refused'),
        ):
            stats = delivery.deliver_pending()
        self.assertEqual(stats['unavailable'], 1)
        row.refresh_from_db()
        self.assertEqual(row.status, RuntimeUsageOutbox.Status.FAILED)
        delivery.requeue_failed(row)
        with mock.patch.object(
            core_client, 'post_usage_events_batch', return_value=[{'status': 'replayed'}],
        ) as post:
            stats = delivery.deliver_pending()
        self.assertEqual(stats['delivered'], 1)
        sent = post.call_args[0][0][0]
        self.assertEqual(sent['id'], str(row.event_id))
        self.assertEqual(sent, envelope.build_envelope(row))
        row.refresh_from_db()
        self.assertEqual(row.status, RuntimeUsageOutbox.Status.DELIVERED)
        self.assertEqual(RuntimeUsageOutbox.objects.count(), 1)
