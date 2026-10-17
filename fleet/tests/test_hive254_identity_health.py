"""Tests for HIVE-254 P5: DaemonIdentityEvent + DaemonIdentityHealthView."""
from datetime import datetime, timezone as dt_tz

from asgiref.sync import async_to_sync
from django.test import TestCase, TransactionTestCase
from django.contrib.auth import get_user_model

from rest_framework.test import APIClient

from fleet.consumers import DaemonLinkConsumer
from fleet.models import DaemonRegistry, DaemonIdentityEvent

User = get_user_model()


def _make_daemon(daemon_id='daemon-1', status='connected'):
    return DaemonRegistry.objects.create(
        daemon_id=daemon_id,
        fleet_id='fleet-test',
        runtime='docker',
        status=status,
    )


class DaemonIdentityEventModelTest(TestCase):
    def test_verdict_choices(self):
        choices = {v for v, _ in DaemonIdentityEvent.Verdict.choices}
        self.assertEqual(choices, {'admitted', 'repaired', 'rejected'})

    def test_create_rejected_event(self):
        daemon = _make_daemon()
        ev = DaemonIdentityEvent.objects.create(
            daemon=daemon,
            child_id='agent-007',
            username='bond',
            verdict=DaemonIdentityEvent.Verdict.REJECTED,
            reason='account_type=human (must be "agent")',
            seen_at=datetime(2026, 6, 29, 12, 0, 0, tzinfo=dt_tz.utc),
        )
        self.assertIsNone(ev.resolved_at)
        # __str__ = DaemonIdentityEvent(<daemon FK pk>, <child_id>, <verdict>).
        # Assert the stable parts and derive the pk from the model so the
        # check survives a shared/kept test database where the pk is not 1.
        self.assertEqual(
            str(ev),
            f'DaemonIdentityEvent({ev.daemon_id}, agent-007, rejected)',
        )
        self.assertIn('agent-007', str(ev))
        self.assertIn('rejected', str(ev))

    def test_admitted_resolves_prior_rejections(self):
        """Simulate what _persist_identity_event does: admitted → resolves open rejections."""
        from django.utils import timezone
        daemon = _make_daemon()
        now = timezone.now()

        # Two unresolved rejected events
        DaemonIdentityEvent.objects.create(
            daemon=daemon, child_id='a1', username='u1',
            verdict=DaemonIdentityEvent.Verdict.REJECTED,
            reason='inactive', seen_at=now,
        )
        DaemonIdentityEvent.objects.create(
            daemon=daemon, child_id='a1', username='u1',
            verdict=DaemonIdentityEvent.Verdict.REJECTED,
            reason='inactive again', seen_at=now,
        )

        # Simulate admitted → resolve prior rejections
        DaemonIdentityEvent.objects.filter(
            daemon=daemon,
            child_id='a1',
            verdict=DaemonIdentityEvent.Verdict.REJECTED,
            resolved_at__isnull=True,
        ).update(resolved_at=now)
        DaemonIdentityEvent.objects.create(
            daemon=daemon, child_id='a1', username='u1',
            verdict=DaemonIdentityEvent.Verdict.ADMITTED,
            reason='', seen_at=now,
        )

        # All prior rejections resolved
        unresolved = DaemonIdentityEvent.objects.filter(
            daemon=daemon, child_id='a1',
            verdict=DaemonIdentityEvent.Verdict.REJECTED,
            resolved_at__isnull=True,
        ).count()
        self.assertEqual(unresolved, 0)

    def test_rejected_event_for_different_child_unaffected(self):
        from django.utils import timezone
        daemon = _make_daemon()
        now = timezone.now()

        DaemonIdentityEvent.objects.create(
            daemon=daemon, child_id='a1', username='u1',
            verdict=DaemonIdentityEvent.Verdict.REJECTED,
            reason='bad', seen_at=now,
        )
        DaemonIdentityEvent.objects.create(
            daemon=daemon, child_id='a2', username='u2',
            verdict=DaemonIdentityEvent.Verdict.REJECTED,
            reason='also bad', seen_at=now,
        )

        # Admit only a1
        DaemonIdentityEvent.objects.filter(
            daemon=daemon, child_id='a1',
            verdict=DaemonIdentityEvent.Verdict.REJECTED,
            resolved_at__isnull=True,
        ).update(resolved_at=now)

        # a2's rejection is still unresolved
        still_open = DaemonIdentityEvent.objects.filter(
            daemon=daemon, child_id='a2',
            verdict=DaemonIdentityEvent.Verdict.REJECTED,
            resolved_at__isnull=True,
        ).count()
        self.assertEqual(still_open, 1)


class DaemonIdentityEventConsumerTest(TransactionTestCase):
    # HIVE-1401: async consumer tests must use TransactionTestCase — mixing
    # async_to_sync DB access into a transactional TestCase leaves the async
    # thread's connection closed for the next sync query ("connection already
    # closed"). Mirrors the canonical async-DB pattern in test_hive235_daemon_ws.
    def _consumer(self, daemon):
        consumer = DaemonLinkConsumer()
        consumer._daemon_id = daemon.daemon_id
        consumer._registry_pk = daemon.pk
        return consumer

    def test_identity_event_rejects_unknown_verdict_before_db_write(self):
        daemon = _make_daemon()
        consumer = self._consumer(daemon)

        with self.assertLogs('fleet.consumers', level='WARNING') as logs:
            async_to_sync(consumer._handle_identity_event)({
                'type': 'identity_event',
                'child_id': 'agent-bad',
                'username': 'agent-bad',
                'verdict': 'surprising',
                'reason': 'bad daemon frame',
            })

        self.assertEqual(DaemonIdentityEvent.objects.count(), 0)
        self.assertIn('unknown verdict', '\n'.join(logs.output))

    def test_identity_event_accepts_valid_verdicts(self):
        daemon = _make_daemon()
        consumer = self._consumer(daemon)

        for verdict in DaemonIdentityEvent.Verdict.values:
            async_to_sync(consumer._handle_identity_event)({
                'type': 'identity_event',
                'child_id': f'agent-{verdict}',
                'username': f'user-{verdict}',
                'verdict': verdict,
                'reason': '',
            })

        self.assertEqual(
            list(DaemonIdentityEvent.objects.order_by('child_id').values_list('verdict', flat=True)),
            sorted(DaemonIdentityEvent.Verdict.values),
        )


class DaemonIdentityHealthViewTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.staff = User.objects.create_user(
            username='staff', email='staff@test.com', password='pw', is_staff=True
        )
        self.user = User.objects.create_user(
            username='plain', email='plain@test.com', password='pw', is_staff=False
        )

    def test_non_staff_returns_403(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.get('/hive/api/v1/fleet/daemon/identity-health/')
        self.assertEqual(resp.status_code, 403)

    def test_empty_registry_returns_empty_list(self):
        self.client.force_authenticate(user=self.staff)
        resp = self.client.get('/hive/api/v1/fleet/daemon/identity-health/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['daemons'], [])

    def test_daemon_with_no_violations_identity_ok_true(self):
        from django.utils import timezone
        _make_daemon('d1')
        self.client.force_authenticate(user=self.staff)
        resp = self.client.get('/hive/api/v1/fleet/daemon/identity-health/')
        self.assertEqual(resp.status_code, 200)
        daemons = resp.json()['daemons']
        self.assertEqual(len(daemons), 1)
        self.assertTrue(daemons[0]['identity_ok'])
        self.assertEqual(daemons[0]['violations'], [])

    def test_daemon_with_unresolved_rejection_identity_ok_false(self):
        from django.utils import timezone
        daemon = _make_daemon('d1')
        DaemonIdentityEvent.objects.create(
            daemon=daemon, child_id='bad-agent', username='badbot',
            verdict=DaemonIdentityEvent.Verdict.REJECTED,
            reason='account_type=human',
            seen_at=timezone.now(),
        )
        self.client.force_authenticate(user=self.staff)
        resp = self.client.get('/hive/api/v1/fleet/daemon/identity-health/')
        self.assertEqual(resp.status_code, 200)
        daemons = resp.json()['daemons']
        self.assertEqual(len(daemons), 1)
        self.assertFalse(daemons[0]['identity_ok'])
        self.assertEqual(len(daemons[0]['violations']), 1)
        v = daemons[0]['violations'][0]
        self.assertEqual(v['child_id'], 'bad-agent')
        self.assertEqual(v['username'], 'badbot')
        self.assertEqual(v['reason'], 'account_type=human')

    def test_resolved_rejection_does_not_appear_in_violations(self):
        from django.utils import timezone
        daemon = _make_daemon('d1')
        now = timezone.now()
        DaemonIdentityEvent.objects.create(
            daemon=daemon, child_id='ex-bad', username='x',
            verdict=DaemonIdentityEvent.Verdict.REJECTED,
            reason='was inactive', seen_at=now,
            resolved_at=now,  # already resolved
        )
        self.client.force_authenticate(user=self.staff)
        resp = self.client.get('/hive/api/v1/fleet/daemon/identity-health/')
        daemons = resp.json()['daemons']
        self.assertTrue(daemons[0]['identity_ok'])
        self.assertEqual(daemons[0]['violations'], [])

    def test_multiple_daemons_reported_separately(self):
        from django.utils import timezone
        d1 = _make_daemon('d1')
        d2 = _make_daemon('d2')
        DaemonIdentityEvent.objects.create(
            daemon=d2, child_id='bad', username='x',
            verdict=DaemonIdentityEvent.Verdict.REJECTED,
            reason='bad type', seen_at=timezone.now(),
        )
        self.client.force_authenticate(user=self.staff)
        resp = self.client.get('/hive/api/v1/fleet/daemon/identity-health/')
        daemons = {d['daemon_id']: d for d in resp.json()['daemons']}
        self.assertTrue(daemons['d1']['identity_ok'])
        self.assertFalse(daemons['d2']['identity_ok'])
        self.assertEqual(len(daemons['d2']['violations']), 1)

    def test_response_includes_daemon_status_fields(self):
        _make_daemon('d1', status='connected')
        self.client.force_authenticate(user=self.staff)
        resp = self.client.get('/hive/api/v1/fleet/daemon/identity-health/')
        d = resp.json()['daemons'][0]
        for field in ('daemon_id', 'fleet_id', 'runtime', 'status', 'identity_ok', 'violations'):
            self.assertIn(field, d)
        self.assertEqual(d['status'], 'connected')
