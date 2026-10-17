"""HIVE-394 feedback-intake tenant-isolation invariants (HLD 89891ffa §6, ADR-0013
dec.2, PLAT-1247). Cross-tenant data path: prove org_id is SERVER-AUTHORIZED (a
client can't attribute feedback to an org it isn't a member of), reads are Model-A
org-scoped (tenant A never sees tenant B's feedback), ingest is idempotent, and
user_ref is pseudonymous. Positive controls prove the paths work → green means
"isolated", not "broken". Reuses the analytics security core (authorize_org /
user_ref_for), so a single source of truth governs both event types.
"""
from types import SimpleNamespace

from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.test import APIClient

from analytics import views
from analytics.feedback import ingest_support_ticket
from analytics.ingest import user_ref_for
from analytics.models import FeedbackEvent

ORG_A, ORG_B = 9101, 9102


def _user(user_id, orgs, is_staff=False):
    return SimpleNamespace(
        user_id=user_id, id=user_id, pk=user_id, is_authenticated=True, is_active=True,
        is_anonymous=False, is_staff=is_staff, is_superuser=False,
        organization_memberships={str(o): 'member' for o in orgs},
    )


class FeedbackWidgetIsolationTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def _widget(self, user, body):
        self.client.force_authenticate(user=user)
        # reverse() resolves under BOTH the full hive urlconf (analytics mounts
        # at /hive/api/) and the analytics-only test urlconf — a bare
        # '/feedback/widget' 404s in the full suite (same class as HIVE-393,
        # fixed in 54ecc40).
        return self.client.post(reverse('feedback-widget'), body, format='json')

    def _list(self, user, **params):
        self.client.force_authenticate(user=user)
        return self.client.get(reverse('feedback-list'), params)

    def test_client_declared_foreign_org_rejected_and_not_written(self):
        # Caller belongs only to ORG_A but proposes ORG_B → 403, nothing stored.
        resp = self._widget(_user(1, [ORG_A]), {'text': 'x', 'org_id': ORG_B})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(FeedbackEvent.objects.count(), 0)

    def test_widget_defaults_to_sole_membership_and_stamps_it(self):
        resp = self._widget(_user(1, [ORG_A]), {'text': 'the editor is slow', 'surface': 'hive'})
        self.assertEqual(resp.status_code, 201, resp.data)
        ev = FeedbackEvent.objects.get()
        self.assertEqual(ev.org_id, ORG_A)
        self.assertEqual(ev.source, 'feedback_widget')
        self.assertIsNone(ev.sentiment)          # clustering-owned, null on ingest
        self.assertIsNone(ev.theme_id)

    @override_settings(SECRET_KEY="disposable-feedback-fixture-28")
    def test_user_ref_pseudonymous_and_per_org(self):
        resp = self._widget(_user(7, [ORG_A]), {'text': 'hi'})
        ev = FeedbackEvent.objects.get(event_id=resp.data['event_id'])
        self.assertEqual(ev.user_ref, user_ref_for(7, ORG_A))
        # A valid digest may begin with the raw ID's first digit. This fixture
        # deliberately exercises that case; the full reference must be opaque.
        self.assertRegex(ev.user_ref, r'^[0-9a-f]{32}$')
        self.assertNotEqual(ev.user_ref, '7')
        self.assertNotEqual(user_ref_for(7, ORG_A), user_ref_for(7, ORG_B))

    def test_cross_tenant_read_isolation(self):
        self._widget(_user(1, [ORG_A]), {'text': 'org A secret'})
        self._widget(_user(2, [ORG_B]), {'text': 'org B secret'})

        a_texts = [r['text'] for r in self._list(_user(1, [ORG_A])).data['results']]
        self.assertIn('org A secret', a_texts)
        self.assertNotIn('org B secret', a_texts)

        b_texts = [r['text'] for r in self._list(_user(2, [ORG_B])).data['results']]
        self.assertIn('org B secret', b_texts)
        self.assertNotIn('org A secret', b_texts)

    def test_multi_org_widget_requires_disambiguation(self):
        multi = _user(5, [ORG_A, ORG_B])
        self.assertEqual(self._widget(multi, {'text': 'x'}).status_code, 400)
        ok = self._widget(multi, {'text': 'x', 'org_id': ORG_B})
        self.assertEqual(ok.status_code, 201)
        self.assertEqual(FeedbackEvent.objects.get(event_id=ok.data['event_id']).org_id, ORG_B)

    def test_widget_client_ref_idempotency_is_tenant_scoped(self):
        """Same untrusted client_ref in two orgs must not leak/suppress cross-tenant."""
        body_a = {'text': 'org A widget feedback', 'org_id': ORG_A, 'client_ref': 'dup-client-ref'}
        body_b = {'text': 'org B widget feedback', 'org_id': ORG_B, 'client_ref': 'dup-client-ref'}
        r1 = self._widget(_user(1, [ORG_A]), body_a)
        r2 = self._widget(_user(2, [ORG_B]), body_b)

        self.assertEqual((r1.status_code, r1.data['created']), (201, True))
        self.assertEqual((r2.status_code, r2.data['created']), (201, True))
        self.assertNotEqual(r1.data['event_id'], r2.data['event_id'])
        self.assertEqual(
            set(FeedbackEvent.objects.filter(source='feedback_widget',
                                             source_ref='dup-client-ref').values_list('org_id', flat=True)),
            {ORG_A, ORG_B},
        )

    def test_blank_text_rejected(self):
        self.assertEqual(self._widget(_user(1, [ORG_A]), {'text': '   '}).status_code, 400)

    def test_feedback_ingest_views_enable_scoped_throttle(self):
        self.assertEqual(views.FeedbackWidgetView.throttle_scope, 'discovery-feedback-ingest')
        self.assertEqual(views.FeedbackSupportIngestView.throttle_scope, 'discovery-feedback-ingest')
        self.assertIn(ScopedRateThrottle, views.FeedbackWidgetView.throttle_classes)
        self.assertIn(ScopedRateThrottle, views.FeedbackSupportIngestView.throttle_classes)


class FeedbackSupportIngestTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def _support(self, user, body):
        self.client.force_authenticate(user=user)
        return self.client.post(reverse('feedback-support'), body, format='json')

    def test_support_ingest_is_service_only(self):
        resp = self._support(_user(1, [ORG_A]), {'org_id': ORG_A, 'ticket_id': 'T-1', 'body': 'help'})
        self.assertEqual(resp.status_code, 403)

    def test_support_ingest_idempotent_on_ticket_id(self):
        svc = _user(900, [], is_staff=True)
        body = {'org_id': ORG_A, 'ticket_id': 'T-42', 'body': 'login broken'}
        r1 = self._support(svc, body)
        r2 = self._support(svc, body)
        self.assertEqual((r1.status_code, r1.data['created']), (201, True))
        self.assertEqual((r2.status_code, r2.data['created']), (200, False))    # redelivery no-op
        self.assertEqual(FeedbackEvent.objects.filter(source='support_ticket', source_ref='T-42').count(), 1)

    def test_two_channels_land_in_one_store(self):
        """Epic acceptance: ≥2 channels normalized into the single feedback_event store."""
        self.client.force_authenticate(user=_user(1, [ORG_A]))
        self.client.post(reverse('feedback-widget'), {'text': 'widget feedback'}, format='json')
        ingest_support_ticket(org_id=ORG_A, ticket_id='T-7', body='ticket feedback')
        sources = set(FeedbackEvent.objects.filter(org_id=ORG_A).values_list('source', flat=True))
        self.assertEqual(sources, {'feedback_widget', 'support_ticket'})
