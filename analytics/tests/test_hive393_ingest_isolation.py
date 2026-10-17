"""HIVE-393 tenant-isolation invariant tests (HLD §6, ADR-0013 dec.2, PLAT-1236).

The event's org is authorized against the caller's VERIFIED memberships — a
client-declared org the caller doesn't belong to is rejected and NOTHING written.
"""
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from analytics import views
from analytics.models import AnalyticsEvent
from analytics.ingest import user_ref_for

ORG_A, ORG_B = 9001, 9002


def _user(user_id, orgs):
    # Mirrors claws.authentication.FederatedUser: organization_memberships = {org: role}
    return SimpleNamespace(
        user_id=user_id, id=user_id, is_authenticated=True, is_active=True,
        is_anonymous=False, is_staff=False, is_superuser=False,
        organization_memberships={str(o): 'member' for o in orgs},
    )


def _evt(**kw):
    base = {'surface': 'home', 'event_type': 'page_view', 'target': '/x', 'props': {}}
    base.update(kw)
    return base


class IngestIsolationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def _post(self, user, events):
        if user is not None:
            self.client.force_authenticate(user=user)
        # reverse() resolves under BOTH the full hive urlconf (analytics is
        # mounted under /api/ and /hive/api/) and the standalone analytics.urls
        # used by authz_ci_settings — a bare '/analytics/events' only resolves in
        # the latter, so it 404'd in the full deploy.yml suite (HIVE-393 fix).
        return self.client.post(reverse('analytics-ingest'), {'events': events}, format='json')

    # ── the core security invariant ──
    def test_client_declared_foreign_org_rejected_and_not_written(self):
        # Caller belongs only to ORG_A but claims ORG_B → 403, nothing stored.
        resp = self._post(_user(1, [ORG_A]), [_evt(org_id=ORG_B)])
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(AnalyticsEvent.objects.count(), 0)

    def test_own_org_accepted_and_scoped(self):
        resp = self._post(_user(1, [ORG_A]), [_evt(org_id=ORG_A)])
        self.assertEqual(resp.status_code, 202)
        ev = AnalyticsEvent.objects.get()
        self.assertEqual(ev.org_id, ORG_A)

    def test_single_org_default_no_org_declared(self):
        resp = self._post(_user(1, [ORG_A]), [_evt()])  # no org_id → default to sole membership
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(AnalyticsEvent.objects.get().org_id, ORG_A)

    def test_multi_org_requires_explicit_org(self):
        resp = self._post(_user(1, [ORG_A, ORG_B]), [_evt()])  # ambiguous
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(AnalyticsEvent.objects.count(), 0)

    def test_multi_org_member_can_only_use_own_orgs(self):
        # member of A+B may post to B (theirs) but NOT to a third org.
        ok = self._post(_user(1, [ORG_A, ORG_B]), [_evt(org_id=ORG_B)])
        self.assertEqual(ok.status_code, 202)
        bad = self._post(_user(1, [ORG_A, ORG_B]), [_evt(org_id=7777)])
        self.assertEqual(bad.status_code, 403)
        self.assertFalse(AnalyticsEvent.objects.filter(org_id=7777).exists())

    def test_batch_atomic_on_forged_event(self):
        # One good + one forged event → whole batch rejected, no partial write.
        resp = self._post(_user(1, [ORG_A]), [_evt(org_id=ORG_A), _evt(org_id=ORG_B)])
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(AnalyticsEvent.objects.count(), 0)

    def test_unauthenticated_rejected(self):
        resp = self._post(None, [_evt(org_id=ORG_A)])
        self.assertIn(resp.status_code, (401, 403))
        self.assertEqual(AnalyticsEvent.objects.count(), 0)

    def test_invalid_surface_and_type_rejected(self):
        for bad in [_evt(surface='evil'), _evt(event_type='exfiltrate')]:
            AnalyticsEvent.objects.all().delete()
            resp = self._post(_user(1, [ORG_A]), [bad])
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(AnalyticsEvent.objects.count(), 0)


    def test_safe_props_contract_accepts_allowlisted_scalar_props(self):
        props = {'route': 'dashboard', 'section': 'hero', 'entrypoint': 'sidebar'}
        resp = self._post(_user(1, [ORG_A]), [_evt(org_id=ORG_A, props=props)])
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(AnalyticsEvent.objects.get().props, props)

    def test_props_reject_unknown_or_pii_like_keys_without_writes(self):
        for props in [
            {'email': 'user@example.com'},
            {'route': 'dashboard', 'account_id': 'acct_123'},
            {'route': {'nested': 'not allowed'}},
        ]:
            AnalyticsEvent.objects.all().delete()
            resp = self._post(_user(1, [ORG_A]), [_evt(org_id=ORG_A, props=props)])
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(AnalyticsEvent.objects.count(), 0)

    def test_props_reject_pii_like_values_without_writes(self):
        for value in [
            'alice@example.com',
            '+1 415 555 0100',
            'Bearer abc.def.ghi',
            'session=abcd1234',
            'jwt:eyJhbGciOiJIUzI1NiJ9.abc.def',
            'cookie=sessionid=abcd1234',
            'password=hunter2',
        ]:
            resp = self._post(_user(1, [ORG_A]), [_evt(org_id=ORG_A, props={'route': value})])
            self.assertEqual(resp.status_code, 400, value)
        self.assertEqual(AnalyticsEvent.objects.count(), 0)

    def test_pii_like_value_rejects_whole_batch_without_partial_write(self):
        resp = self._post(_user(1, [ORG_A]), [
            _evt(org_id=ORG_A, target='/safe', props={'route': 'dashboard'}),
            _evt(org_id=ORG_A, target='/bad', props={'route': 'Bearer abc.def.ghi'}),
        ])

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(AnalyticsEvent.objects.count(), 0)

    @patch.object(views.AnalyticsIngestRateThrottle, 'rate', '2/min', create=True)
    def test_rate_limit_allows_under_limit_writes(self):
        user = _user(7, [ORG_A])
        r1 = self._post(user, [_evt(org_id=ORG_A, target='/one')])
        r2 = self._post(user, [_evt(org_id=ORG_A, target='/two')])
        self.assertEqual((r1.status_code, r2.status_code), (202, 202))
        self.assertEqual(AnalyticsEvent.objects.filter(org_id=ORG_A).count(), 2)

    @patch.object(views.AnalyticsIngestRateThrottle, 'rate', '1/min', create=True)
    def test_rate_limit_rejects_over_limit_without_writes(self):
        user = _user(8, [ORG_A])
        ok = self._post(user, [_evt(org_id=ORG_A, target='/first')])
        before = AnalyticsEvent.objects.count()
        blocked = self._post(user, [_evt(org_id=ORG_A, target='/blocked')])
        self.assertEqual(ok.status_code, 202)
        self.assertEqual(blocked.status_code, 429)
        self.assertIn('Retry-After', blocked)
        self.assertEqual(AnalyticsEvent.objects.count(), before)
        self.assertFalse(AnalyticsEvent.objects.filter(target='/blocked').exists())

    @patch.object(views.AnalyticsIngestRateThrottle, 'rate', '1/min', create=True)
    def test_rate_limit_is_per_org_not_cross_tenant(self):
        user = _user(9, [ORG_A, ORG_B])
        self.assertEqual(self._post(user, [_evt(org_id=ORG_A, target='/a1')]).status_code, 202)
        blocked_a = self._post(user, [_evt(org_id=ORG_A, target='/a2')])
        allowed_b = self._post(user, [_evt(org_id=ORG_B, target='/b1')])
        self.assertEqual(blocked_a.status_code, 429)
        self.assertEqual(allowed_b.status_code, 202)
        self.assertFalse(AnalyticsEvent.objects.filter(org_id=ORG_A, target='/a2').exists())
        self.assertTrue(AnalyticsEvent.objects.filter(org_id=ORG_B, target='/b1').exists())

    def test_user_ref_is_pseudonymous(self):
        self._post(_user(42, [ORG_A]), [_evt(org_id=ORG_A)])
        ref = AnalyticsEvent.objects.get().user_ref
        # Pseudonymity = the stored ref is an opaque salted-hash digest, NOT the
        # raw user id. Assert that structurally (deterministic). The previous
        # `assertNotIn('42', ref)` was FLAKY: ref is a 32-char sha256 hex, which
        # contains the 2-digit id substring '42' by pure chance for ~10% of
        # SECRET_KEYs (a coincidental hex substring is not a PII leak) — it passed
        # locally but failed in CI whose key produced a '42'-containing digest.
        self.assertNotEqual(ref, '42')                      # not the raw user id
        self.assertRegex(ref, r'\A[0-9a-f]{32}\Z')          # opaque hex digest, no raw PII
        self.assertEqual(ref, user_ref_for(42, ORG_A))  # stable + server-derived
        self.assertNotEqual(ref, user_ref_for(42, ORG_B))  # per-(user,org)
