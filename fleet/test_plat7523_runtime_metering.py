"""PLAT-7523 (R2 Slice 2) runtime-hours meter — test matrix.

Same matrix as Slice 1: replay-conflict, late-event, watermark, two-account
isolation, outbox retry — plus envelope shape, dead-seat grace cap, and
owner-less skip.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from fleet.metering import core_client, delivery
from fleet.metering.events import (
    DEAD_SEAT_GRACE_SECONDS,
    EVENT_TYPE_MEASURED,
    METER_RUNTIME_SECONDS,
    SOURCE_RUNTIME,
    UNIT_SECONDS,
    billing_subject,
    build_envelope,
    derive_interval,
    enqueue_runtime_interval,
    runtime_event_id,
)
from fleet.metering.models import FleetRuntimeOutbox, canonical_payload_sha256


def make_agent(**overrides):
    """Minimal unsaved FleetAgent-like object for the pure/enqueue paths."""
    from fleet.models import FleetAgent
    defaults = {
        'agent_username': 'agent-test',
        'owner_id': 11,
    }
    defaults.update(overrides)
    return FleetAgent(**defaults)


class PureDerivationTests(TestCase):
    def test_derive_interval_half_open(self):
        start = timezone.now()
        end = start + timedelta(seconds=300)
        self.assertEqual(derive_interval(start, end), (start, end))

    def test_derive_interval_none_on_first_heartbeat(self):
        self.assertIsNone(derive_interval(None, timezone.now()))

    def test_derive_interval_none_on_nonpositive_advance(self):
        now = timezone.now()
        self.assertIsNone(derive_interval(now, now))
        self.assertIsNone(derive_interval(now + timedelta(seconds=5), now))

    def test_event_id_deterministic_and_interval_keyed(self):
        start = timezone.now()
        a = runtime_event_id(7, start)
        b = runtime_event_id(7, start)
        self.assertEqual(a, b)
        self.assertTrue(a.startswith('hive-runtime:7:'))
        # a later interval gets a different identity (late-event safety)
        self.assertNotEqual(a, runtime_event_id(7, start + timedelta(seconds=300)))

    def test_billing_subject_shape_and_ownerless(self):
        self.assertEqual(billing_subject(owner_id=11), 'user:11')
        self.assertIsNone(billing_subject(owner_id=None))

    def test_envelope_shape_matches_core_contract(self):
        start = timezone.now()
        end = start + timedelta(seconds=90)
        env = build_envelope(
            event_id='hive-runtime:7:123', subject='user:11',
            occurred_at=end, interval_start=start, interval_end=end,
            quantity=Decimal('90'), agent_pk=7, agent_username='agent-test',
        )
        self.assertEqual(env['specversion'], '1.0')
        self.assertEqual(env['source'], SOURCE_RUNTIME)
        self.assertEqual(env['type'], EVENT_TYPE_MEASURED)
        self.assertEqual(env['subject'], 'user:11')
        self.assertTrue(env['dataschema'].startswith('urn:'))
        self.assertTrue(env['time'].endswith('Z'))
        data = env['data']
        self.assertEqual(data['meter'], METER_RUNTIME_SECONDS)
        self.assertEqual(data['unit'], UNIT_SECONDS)
        # quantity is a DECIMAL STRING, never a JSON number
        self.assertIsInstance(data['quantity'], str)
        self.assertEqual(data['quantity'], '90')
        self.assertEqual(data['dimensions'], {'product': 'hive', 'agent': 7})
        self.assertEqual(data['quality'], 'measured')
        # naive-UTC clocks render with the explicit Z offset (core contract)
        expected_start = start.isoformat() + 'Z' if not start.isoformat().endswith(('Z', '+00:00')) else start.isoformat().replace('+00:00', 'Z')
        expected_end = end.isoformat() + 'Z' if not end.isoformat().endswith(('Z', '+00:00')) else end.isoformat().replace('+00:00', 'Z')
        self.assertEqual(data['interval']['start'], expected_start)
        self.assertEqual(data['interval']['end'], expected_end)

    def test_envelope_deterministic_byte_identical(self):
        start = timezone.now()
        end = start + timedelta(seconds=90)
        kw = dict(event_id='hive-runtime:7:123', subject='user:11',
                  occurred_at=end, interval_start=start, interval_end=end,
                  quantity=Decimal('90'), agent_pk=7)
        e1 = build_envelope(**kw)
        e2 = build_envelope(**kw)
        self.assertEqual(canonical_payload_sha256(e1), canonical_payload_sha256(e2))


@override_settings(HIVE_INTERNAL_AUTH_TOKEN='metering-test-token')
class EnqueueOutboxTests(TestCase):
    # Fixed interval so replay tests derive the SAME event id.
    START = timezone.now() - timedelta(seconds=300)
    END = START + timedelta(seconds=300)

    def enqueue(self, agent=None, **agent_overrides):
        # NOTE: FleetAgent pk is a per-instance UUID default — replay tests
        # must pass the SAME agent instance so the event id is identical.
        if agent is None:
            agent = make_agent(**agent_overrides)
        with transaction_atomic():
            row = enqueue_runtime_interval(
                agent=agent, prev_active=self.START, now=self.END)
        return row

    def test_enqueue_creates_durable_row(self):
        row = self.enqueue()
        self.assertIsNotNone(row)
        row.refresh_from_db()
        self.assertEqual(row.source, SOURCE_RUNTIME)
        self.assertEqual(row.status, FleetRuntimeOutbox.State.PENDING)
        self.assertEqual(row.subject, 'user:11')
        self.assertEqual(row.quantity, Decimal('300.000000'))
        self.assertEqual(row.interval_start, self.START)
        self.assertEqual(row.interval_end, self.END)

    def test_replay_same_identity_same_hash_is_replay(self):
        agent = make_agent()
        row1 = self.enqueue(agent=agent)
        row2 = self.enqueue(agent=agent)  # same agent/interval → replay
        self.assertEqual(row1.pk, row2.pk)
        self.assertEqual(FleetRuntimeOutbox.objects.count(), 1)

    def test_replay_conflict_same_identity_different_hash(self):
        from fleet.metering.events import RuntimeMeteringConflict
        agent = make_agent()
        row = self.enqueue(agent=agent)
        # Same identity, different payload: patch the envelope builder to
        # return a tampered quantity for the SAME event id.
        with transaction_atomic():
            with self.assertRaises(RuntimeMeteringConflict):
                with patch(
                    'fleet.metering.events.build_envelope',
                    return_value={**row.envelope, 'data': {**row.envelope['data'], 'quantity': '999'}},
                ):
                    enqueue_runtime_interval(
                        agent=agent, prev_active=self.START, now=self.END)

    def test_ownerless_agent_skipped_no_payer_invented(self):
        row = self.enqueue(owner_id=None)
        self.assertIsNone(row)
        self.assertEqual(FleetRuntimeOutbox.objects.count(), 0)

    def test_late_event_older_interval_gets_own_identity(self):
        agent = make_agent()
        now = timezone.now()
        with transaction_atomic():
            newer = enqueue_runtime_interval(
                agent=agent, prev_active=now - timedelta(seconds=60), now=now)
            older = enqueue_runtime_interval(
                agent=agent, prev_active=now - timedelta(seconds=600),
                now=now - timedelta(seconds=300))
        self.assertIsNotNone(newer)
        self.assertIsNotNone(older)
        self.assertNotEqual(newer.event_id, older.event_id)
        self.assertEqual(FleetRuntimeOutbox.objects.count(), 2)

    def test_two_account_isolation(self):
        agent_a = make_agent(agent_username='agent-a', owner_id=11)
        agent_b = make_agent(agent_username='agent-b', owner_id=22)
        start = timezone.now() - timedelta(seconds=120)
        end = start + timedelta(seconds=120)
        with transaction_atomic():
            row_a = enqueue_runtime_interval(agent=agent_a, prev_active=start, now=end)
            row_b = enqueue_runtime_interval(agent=agent_b, prev_active=start, now=end)
        self.assertEqual(row_a.subject, 'user:11')
        self.assertEqual(row_b.subject, 'user:22')
        self.assertNotEqual(row_a.event_id, row_b.event_id)
        # no cross-credit: each row's envelope names only its own subject
        self.assertEqual(row_a.envelope['subject'], 'user:11')
        self.assertEqual(row_b.envelope['subject'], 'user:22')


def transaction_atomic():
    from django.db import transaction
    return transaction.atomic()


@override_settings(HIVE_INTERNAL_AUTH_TOKEN='metering-test-token')
class DeliveryTests(TestCase):
    def _row(self, **kw):
        agent = make_agent()
        start = timezone.now() - timedelta(seconds=60)
        end = start + timedelta(seconds=60)
        with transaction_atomic():
            row = enqueue_runtime_interval(agent=agent, prev_active=start, now=end)
        for field, value in kw.items():
            setattr(row, field, value)
        row.save()
        return row

    def test_outbox_retry_transport_failure_retains_row(self):
        row = self._row()
        with patch.object(core_client, 'ingest_events',
                          side_effect=core_client.MeteringCoreUnavailable('conn refused')):
            summary = delivery.dispatch_runtime_metering()
        self.assertEqual(summary['failed'], 1)
        row.refresh_from_db()
        self.assertEqual(row.status, FleetRuntimeOutbox.State.FAILED)
        self.assertGreaterEqual(row.attempts, 1)
        self.assertGreater(row.next_attempt_at, timezone.now() - timedelta(seconds=1))
        # retry picks it up again once due
        row.next_attempt_at = timezone.now() - timedelta(seconds=1)
        row.save()
        with patch.object(core_client, 'ingest_events',
                          return_value=[{'status': 'accepted'}]):
            summary = delivery.dispatch_runtime_metering()
        self.assertEqual(summary['delivered'], 1)
        row.refresh_from_db()
        self.assertEqual(row.status, FleetRuntimeOutbox.State.DELIVERED)

    def test_conflict_is_terminal_and_excluded_from_retry(self):
        row = self._row()
        with patch.object(core_client, 'ingest_events',
                          return_value=[{'error': 'idempotency_conflict', 'detail': 'hash mismatch'}]):
            summary = delivery.dispatch_runtime_metering()
        self.assertEqual(summary['conflict'], 1)
        row.refresh_from_db()
        self.assertEqual(row.status, FleetRuntimeOutbox.State.CONFLICT)
        # CONFLICT rows are never re-picked
        self.assertEqual(delivery.pending_rows(), [])

    def test_replayed_result_counts_as_delivered(self):
        row = self._row()
        with patch.object(core_client, 'ingest_events',
                          return_value=[{'status': 'replayed'}]):
            summary = delivery.dispatch_runtime_metering()
        self.assertEqual(summary['delivered'], 1)
        row.refresh_from_db()
        self.assertEqual(row.status, FleetRuntimeOutbox.State.DELIVERED)

    def test_dry_run_writes_nothing(self):
        self._row()
        summary = delivery.dispatch_runtime_metering(dry_run=True)
        self.assertEqual(summary['picked'], 1)
        self.assertEqual(summary['delivered'], 0)
        self.assertEqual(FleetRuntimeOutbox.objects.get().status,
                         FleetRuntimeOutbox.State.PENDING)


@override_settings(HIVE_INTERNAL_AUTH_TOKEN='metering-test-token')
class SweeperTests(TestCase):
    def _quiet_agent(self, quiet_seconds, **overrides):
        from fleet.models import FleetAgent
        agent = FleetAgent.objects.create(
            agent_username=overrides.pop('agent_username', 'agent-quiet'),
            owner_id=overrides.pop('owner_id', 11),
            last_active_at=timezone.now() - timedelta(seconds=quiet_seconds),
            **overrides,
        )
        return agent

    def test_sweep_emits_final_interval_within_grace_cap(self):
        agent = self._quiet_agent(DEAD_SEAT_GRACE_SECONDS + 60)
        from django.core.management import call_command
        call_command('sweep_runtime_metering')
        rows = FleetRuntimeOutbox.objects.filter(subject='user:11')
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        expected_start = agent.last_active_at
        expected_end = agent.last_active_at + timedelta(seconds=DEAD_SEAT_GRACE_SECONDS)
        self.assertEqual(row.interval_start, expected_start)
        self.assertEqual(row.interval_end, expected_end)
        self.assertEqual(row.quantity, Decimal(str(DEAD_SEAT_GRACE_SECONDS)))

    def test_sweep_is_idempotent(self):
        agent = self._quiet_agent(DEAD_SEAT_GRACE_SECONDS + 60)
        from django.core.management import call_command
        call_command('sweep_runtime_metering')
        call_command('sweep_runtime_metering')
        self.assertEqual(FleetRuntimeOutbox.objects.count(), 1)

    def test_sweep_skips_seats_still_inside_grace_window(self):
        self._quiet_agent(60)  # quiet but inside the grace window
        from django.core.management import call_command
        call_command('sweep_runtime_metering')
        self.assertEqual(FleetRuntimeOutbox.objects.count(), 0)

    def test_sweep_skips_ownerless_with_watermark_gap(self):
        self._quiet_agent(DEAD_SEAT_GRACE_SECONDS + 60, owner_id=None)
        from django.core.management import call_command
        call_command('sweep_runtime_metering')
        self.assertEqual(FleetRuntimeOutbox.objects.count(), 0)
