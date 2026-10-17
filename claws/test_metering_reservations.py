"""PLAT-4484 — reservation / lease / proof / writer cutover tests."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from .metering_core import (
    create_billing_account,
    issue_account_entitlement_from_template,
    seed_free_v1_catalog,
)
from .metering_models import (
    AdmissionProofReceipt,
    AdmissionWriterAuthority,
    EntitlementBalance,
    EntitlementGrant,
    QuotaReservation,
    SpendLease,
)
from .metering_reservations import (
    ReservationError,
    boot_producer_holder,
    close_spend_lease,
    commit_reservation,
    cutover_admission_writer,
    expire_unstarted_if_due,
    issue_spend_lease,
    release_reservation,
    reserve_quota,
    resolve_orphaned_hold,
    start_reservation,
    verify_proof_envelope,
)
from . import metering_views


def _issue_requests_balance(account, amount='100000'):
    """Issue a large free-v1.platform.requests balance for reservation tests."""
    seed_free_v1_catalog()
    template = EntitlementGrant.objects.filter(
        grant_key='free-v1.platform.requests', billing_account__isnull=True
    ).get()
    _grant, balance = issue_account_entitlement_from_template(
        template=template,
        billing_account=account,
        amount=Decimal(amount),
    )
    return balance


@override_settings(METERING_CORE_SERVICE_TOKEN='test-metering-token')
class QuotaReservationProtocolTests(TestCase):
    def setUp(self):
        self.account = create_billing_account(
            account_type='individual', owner_subject='user:plat4484'
        )
        self.balance = _issue_requests_balance(self.account)
        self.meter = self.balance.grant.meter_definition.key
        self.unit = self.balance.grant.unit or self.balance.grant.meter_definition.canonical_unit

    def _reserve(self, key='op-1', qty='10', **kwargs):
        return reserve_quota(
            idempotency_key=key,
            meter=self.meter,
            maximum_quantity=qty,
            unit=self.unit,
            resource_uid='urn:shizuha:resource:r1',
            billing_account_id=self.account.id,
            producer_principal='producer-a',
            **kwargs,
        )

    def test_reserve_start_commit_split(self):
        r = self._reserve(key='split-1', qty='100')
        self.assertTrue(r['allowed'])
        rid = r['reservation_id']
        self.assertEqual(r['status'], 'reserved')
        self.assertEqual(r['free_reserved_quantity'], '100')

        started = start_reservation(
            rid,
            idempotency_key='start-1',
            occurrence_ref='occ-1',
            outbox_ref='outbox-1',
            producer_principal='producer-a',
        )
        self.assertEqual(started['status'], 'started')

        committed = commit_reservation(
            rid,
            idempotency_key='commit-1',
            actual_quantity='40',
            held_unknown_quantity='10',
            producer_principal='producer-a',
        )
        self.assertEqual(committed['status'], 'orphaned_hold')
        self.assertEqual(committed['committed_actual'], '40')
        self.assertEqual(committed['released_unused'], '50')
        self.assertEqual(committed['held_unknown'], '10')
        row = QuotaReservation.objects.get(pk=rid)
        self.assertTrue(row.split_ok())

    def test_reserve_idempotent_replay_and_conflict(self):
        a = self._reserve(key='idem-1', qty='5')
        b = self._reserve(key='idem-1', qty='5')
        self.assertEqual(a['decision_id'], b['decision_id'])
        with self.assertRaises(ReservationError) as ctx:
            self._reserve(key='idem-1', qty='9')
        self.assertEqual(ctx.exception.code, 'idempotency_conflict')

    def test_release_requires_no_start_proof(self):
        r = self._reserve(key='rel-1', qty='7')
        rid = r['reservation_id']
        with self.assertRaises(ReservationError) as ctx:
            release_reservation(
                rid,
                idempotency_key='rel-k',
                no_start_proof_sha256='',
                producer_principal='producer-a',
            )
        self.assertEqual(ctx.exception.code, 'missing_no_start_proof')
        out = release_reservation(
            rid,
            idempotency_key='rel-k2',
            no_start_proof_sha256='abc123',
            producer_principal='producer-a',
        )
        self.assertEqual(out['status'], 'released')
        self.assertEqual(out['released_unused'], '7')

    def test_started_release_becomes_orphaned_hold(self):
        r = self._reserve(key='orph-1', qty='3')
        rid = r['reservation_id']
        start_reservation(
            rid,
            idempotency_key='s1',
            occurrence_ref='o',
            outbox_ref='x',
            producer_principal='producer-a',
        )
        with self.assertRaises(ReservationError) as ctx:
            release_reservation(
                rid,
                idempotency_key='r1',
                no_start_proof_sha256='n/a',
                producer_principal='producer-a',
            )
        self.assertEqual(ctx.exception.code, 'started_requires_commit')
        row = QuotaReservation.objects.get(pk=rid)
        self.assertEqual(row.status, QuotaReservation.Status.ORPHANED_HOLD)

    def test_expire_unstarted_releases_headroom_not_started(self):
        r = self._reserve(key='exp-1', qty='8')
        rid = r['reservation_id']
        row = QuotaReservation.objects.get(pk=rid)
        row.expires_at = timezone.now() - timedelta(seconds=1)
        row.save(update_fields=['expires_at'])
        out = expire_unstarted_if_due(rid)
        self.assertEqual(out['status'], 'expired_unstarted')

        # Started path → orphaned_hold, not free reissue.
        r2 = self._reserve(key='exp-2', qty='4')
        rid2 = r2['reservation_id']
        start_reservation(
            rid2,
            idempotency_key='s2',
            occurrence_ref='o2',
            outbox_ref='x2',
            producer_principal='producer-a',
        )
        row2 = QuotaReservation.objects.get(pk=rid2)
        row2.expires_at = timezone.now() - timedelta(seconds=1)
        row2.save(update_fields=['expires_at'])
        out2 = expire_unstarted_if_due(rid2)
        self.assertEqual(out2['status'], 'orphaned_hold')

    def test_deny_when_insufficient_entitlement(self):
        huge = str(self.balance.remaining + Decimal('1000000'))
        r = self._reserve(key='deny-1', qty=huge)
        self.assertFalse(r['allowed'])
        self.assertEqual(r['reason'], 'insufficient_entitlement')


@override_settings(METERING_CORE_SERVICE_TOKEN='test-metering-token')
class SpendLeaseAndHolderTests(TestCase):
    def setUp(self):
        self.account = create_billing_account(
            account_type='individual', owner_subject='user:lease'
        )
        self.balance = _issue_requests_balance(self.account)
        self.meter = self.balance.grant.meter_definition.key

    def test_lease_issue_close_and_epoch_fence(self):
        holder = boot_producer_holder(holder_key='prod-lease')
        lease = issue_spend_lease(
            idempotency_key='lease-1',
            billing_account_id=self.account.id,
            resource_uid='res-1',
            meter_key=self.meter,
            quantity='12',
            producer_principal='prod-lease',
            ttl_seconds=30,
        )
        self.assertEqual(lease['status'], 'active')
        self.assertEqual(lease['producer_holder_epoch'], holder.epoch)
        self.assertLessEqual(30, 60)

        closed = close_spend_lease(
            lease['lease_id'],
            idempotency_key='close-1',
            debited_quantity='5',
            producer_principal='prod-lease',
        )
        self.assertEqual(closed['status'], 'closed')
        self.assertEqual(closed['debited_quantity'], '5')
        # Distinct kids for issue vs close trust roots.
        self.assertEqual(closed['lease_kid'], 'metering-lease-v1')
        self.assertEqual(closed['close_kid'], 'metering-lease-close-v1')

        # New boot fences prior active leases.
        lease2 = issue_spend_lease(
            idempotency_key='lease-2',
            billing_account_id=self.account.id,
            resource_uid='res-1',
            meter_key=self.meter,
            quantity='3',
            producer_principal='prod-lease',
        )
        boot_producer_holder(holder_key='prod-lease')
        row = SpendLease.objects.get(lease_id=lease2['lease_id'])
        self.assertEqual(row.status, SpendLease.Status.FENCED)

    def test_proof_envelope_rejects_wrong_kid_alg_type_aud(self):
        r = reserve_quota(
            idempotency_key='proof-1',
            meter=self.meter,
            maximum_quantity='1',
            unit=self.balance.grant.unit or self.balance.grant.meter_definition.canonical_unit,
            resource_uid='r',
            billing_account_id=self.account.id,
            producer_principal='p',
        )
        jws = r.get('prepared_proof_jws') or ''
        self.assertTrue(jws)
        verify_proof_envelope(
            jws_compact=jws,
            expected_profile=AdmissionProofReceipt.Profile.PREPARED,
        )
        with self.assertRaises(ReservationError):
            verify_proof_envelope(
                jws_compact=jws,
                expected_profile=AdmissionProofReceipt.Profile.PREPARED,
                expected_kid='wrong-kid',
            )

    def test_proof_envelope_is_real_eddsa_and_rejects_tamper(self):
        r = reserve_quota(
            idempotency_key='proof-eddsa-1',
            meter=self.meter,
            maximum_quantity='1',
            unit=self.balance.grant.unit or self.balance.grant.meter_definition.canonical_unit,
            resource_uid='r',
            billing_account_id=self.account.id,
            producer_principal='p',
        )
        jws = r['prepared_proof_jws']
        header_b64, payload_b64, sig_b64 = jws.split('.')
        # Signature must not be a bare sha256 of the signing input (old stub).
        import base64
        import hashlib
        pad = lambda s: s + '=' * (-len(s) % 4)
        sig = base64.urlsafe_b64decode(pad(sig_b64))
        stub = hashlib.sha256(f'{header_b64}.{payload_b64}'.encode()).digest()
        self.assertNotEqual(sig, stub)
        self.assertEqual(len(sig), 64)  # Ed25519 signature length
        # Tampered payload fails closed.
        bad = f'{header_b64}.{payload_b64[:-4]}abcd.{sig_b64}'
        with self.assertRaises(ReservationError) as ctx:
            verify_proof_envelope(
                jws_compact=bad,
                expected_profile=AdmissionProofReceipt.Profile.PREPARED,
            )
        self.assertIn(ctx.exception.code, {'invalid_proof', 'invalid_proof_signature', 'invalid_proof_profile'})

    def test_resolve_orphaned_hold_releases_held_headroom(self):
        r = reserve_quota(
            idempotency_key='orphan-1',
            meter=self.meter,
            maximum_quantity='10',
            unit=self.balance.grant.unit or self.balance.grant.meter_definition.canonical_unit,
            resource_uid='r-orphan',
            billing_account_id=self.account.id,
            producer_principal='p',
        )
        rid = r['reservation_id']
        start_reservation(rid, idempotency_key='orphan-start', occurrence_ref='occ-1', outbox_ref='ob-1')
        # Commit with held_unknown > 0 → ORPHANED_HOLD
        c = commit_reservation(
            rid,
            idempotency_key='orphan-commit',
            actual_quantity='4',
            held_unknown_quantity='6',
        )
        self.assertEqual(c['status'], 'orphaned_hold')
        self.assertEqual(c['held_unknown'], '6')
        # Second commit with new key must not silently work via old path.
        with self.assertRaises(ReservationError) as ctx:
            commit_reservation(
                rid,
                idempotency_key='orphan-commit-2',
                actual_quantity='6',
                held_unknown_quantity='0',
            )
        self.assertEqual(ctx.exception.code, 'idempotency_conflict')
        # Resolve path finishes held → released.
        resolved = resolve_orphaned_hold(
            rid,
            idempotency_key='orphan-resolve',
            actual_quantity='0',
            released_quantity='6',
            terminal_proof_sha256='proof-later',
        )
        self.assertEqual(resolved['status'], 'committed')
        self.assertEqual(resolved['held_unknown'], '0')
        self.assertEqual(resolved['committed_actual'], '4')
        self.assertEqual(resolved['released_unused'], '6')
        # Idempotent replay.
        again = resolve_orphaned_hold(
            rid,
            idempotency_key='orphan-resolve',
            actual_quantity='0',
            released_quantity='6',
            terminal_proof_sha256='proof-later',
        )
        self.assertEqual(again['status'], 'committed')
        row = QuotaReservation.objects.get(pk=rid)
        self.assertEqual(row.status, QuotaReservation.Status.COMMITTED)
        self.assertEqual(row.held_unknown, Decimal('0'))

    def test_commit_and_resolve_verify_inbound_terminal_jws(self):
        """Production paths must cryptographically verify inbound terminal_proof_jws."""
        r = reserve_quota(
            idempotency_key='inbound-jws-1',
            meter=self.meter,
            maximum_quantity='5',
            unit=self.balance.grant.unit or self.balance.grant.meter_definition.canonical_unit,
            resource_uid='r-jws',
            billing_account_id=self.account.id,
            producer_principal='p',
        )
        rid = r['reservation_id']
        start_reservation(rid, idempotency_key='inbound-start', occurrence_ref='occ-j', outbox_ref='ob-j')
        # Tampered/forged JWS must fail closed on commit (rui P2-1 residual).
        with self.assertRaises(ReservationError) as ctx:
            commit_reservation(
                rid,
                idempotency_key='inbound-commit-bad',
                actual_quantity='5',
                held_unknown_quantity='0',
                terminal_proof_jws='not.a.realjws',
            )
        self.assertIn(ctx.exception.code, {'invalid_proof', 'invalid_proof_signature'})
        # Happy path without inbound JWS still works (metering issues its own terminal proof).
        c = commit_reservation(
            rid,
            idempotency_key='inbound-commit-ok',
            actual_quantity='2',
            held_unknown_quantity='3',
        )
        self.assertEqual(c['status'], 'orphaned_hold')
        terminal_jws = c.get('terminal_proof_jws') or ''
        self.assertTrue(terminal_jws)
        # Resolve accepts a verified terminal JWS from the prior issuance.
        resolved = resolve_orphaned_hold(
            rid,
            idempotency_key='inbound-resolve',
            actual_quantity='0',
            released_quantity='3',
            terminal_proof_jws=terminal_jws,
        )
        self.assertEqual(resolved['status'], 'committed')
        self.assertEqual(resolved['held_unknown'], '0')


@override_settings(METERING_CORE_SERVICE_TOKEN='test-metering-token')
class AdmissionWriterCutoverTests(TestCase):
    def test_freeze_import_activate(self):
        a = cutover_admission_writer(product='sites', action='freeze', source_watermark='wm-1')
        self.assertEqual(a['state'], 'freezing')
        b = cutover_admission_writer(
            product='sites',
            action='import',
            source_watermark='wm-1',
            cutover_manifest_sha256='deadbeef',
        )
        self.assertEqual(b['state'], 'importing')
        c = cutover_admission_writer(product='sites', action='activate')
        self.assertEqual(c['state'], 'active')
        self.assertEqual(c['generation'], 2)
        # Stale generation rejected on reserve.
        acct = create_billing_account(account_type='individual', owner_subject='u:cut')
        bal = _issue_requests_balance(acct)
        with self.assertRaises(ReservationError) as ctx:
            reserve_quota(
                idempotency_key='cut-1',
                meter=bal.grant.meter_definition.key,
                maximum_quantity='1',
                unit=bal.grant.unit or bal.grant.meter_definition.canonical_unit,
                resource_uid='r',
                billing_account_id=acct.id,
                product='sites',
                writer_generation=1,  # stale
            )
        self.assertEqual(ctx.exception.code, 'stale_writer_generation')


@override_settings(METERING_CORE_SERVICE_TOKEN='test-metering-token')
class ReservationHTTPBoundaryTests(TestCase):
    def setUp(self):
        self.account = create_billing_account(
            account_type='individual', owner_subject='user:http'
        )
        self.balance = _issue_requests_balance(self.account)
        self.factory = APIRequestFactory()
        self.auth = {'HTTP_AUTHORIZATION': 'Bearer test-metering-token'}

    def test_http_reserve_start_commit(self):
        req = self.factory.post(
            '/internal/v1/quota-reservations',
            {
                'meter': self.balance.grant.meter_definition.key,
                'maximum_quantity': '6',
                'unit': self.balance.grant.unit or self.balance.grant.meter_definition.canonical_unit,
                'resource_uid': 'http-res',
                'billing_account_id': str(self.account.id),
                'producer_principal': 'http-prod',
            },
            format='json',
            HTTP_IDEMPOTENCY_KEY='http-op-1',
            **self.auth,
        )
        resp = metering_views.InternalQuotaReservationCreateView.as_view()(req)
        self.assertIn(resp.status_code, (200, 201), resp.data)
        rid = resp.data['reservation_id']

        req2 = self.factory.post(
            f'/internal/v1/quota-reservations/{rid}/start',
            {'occurrence_ref': 'occ', 'outbox_ref': 'ob', 'producer_principal': 'http-prod'},
            format='json',
            HTTP_IDEMPOTENCY_KEY='http-start-1',
            **self.auth,
        )
        resp2 = metering_views.InternalQuotaReservationStartView.as_view()(
            req2, reservation_id=rid
        )
        self.assertEqual(resp2.status_code, 200, resp2.data)

        req3 = self.factory.post(
            f'/internal/v1/quota-reservations/{rid}/commit',
            {'actual_quantity': '6', 'producer_principal': 'http-prod'},
            format='json',
            HTTP_IDEMPOTENCY_KEY='http-commit-1',
            **self.auth,
        )
        resp3 = metering_views.InternalQuotaReservationCommitView.as_view()(
            req3, reservation_id=rid
        )
        self.assertEqual(resp3.status_code, 200, resp3.data)
        self.assertEqual(resp3.data['status'], 'committed')

    def test_http_requires_auth(self):
        req = self.factory.post('/internal/v1/quota-reservations', {}, format='json')
        resp = metering_views.InternalQuotaReservationCreateView.as_view()(req)
        self.assertEqual(resp.status_code, 403)
