"""HIVE-314 S4 — per-org usage/consumption view tests.

Covers:
- org-scoped attribution (caller sees only their own org's usage);
- no cross-org leak (a caller can never read another org's usage, even with a
  forged X-Organization-ID header);
- org-owner-only visibility (membership validated against the admin authority);
- personal-org fallback when no header is supplied;
- days window clamping.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from hive_platform.models import UsageDailyRollup

from .org_usage import OrgUsageView, _clamp_days


def _rollup(day, tenant_org_id, metric, quantity, model='gpt-4o', unit='tokens', owner_id=None):
    return UsageDailyRollup.objects.create(
        day=day,
        tenant_org_id=tenant_org_id,
        tenant_org_slug='',
        owner_id=owner_id,
        source='cortex',
        metric=metric,
        unit=unit,
        model=model,
        quantity=Decimal(str(quantity)),
        event_count=1,
    )


class OrgUsageViewTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        # Default window is last 30 calendar days inclusive of today
        # (since = today - 29). A hardcoded 2026-08-01 falls outside
        # that window from 2026-08-31 and empties `usage`.
        self.today = timezone.now().date()
        # Org 100 (caller's org) and org 200 (foreign org).
        _rollup(self.today, '100', 'agent_turn', 42, model='gpt-4o')
        _rollup(self.today, '100', 'tokens_in', 1000, model='gpt-4o')
        _rollup(self.today, '200', 'agent_turn', 999, model='gpt-4o')  # foreign
        _rollup(self.today - timedelta(days=1), '100', 'agent_turn', 7, model='gpt-4o')

    def _user(self, uid=141):
        return type('User', (), {'id': uid, 'is_authenticated': True})()

    def _request(self, user, headers=None, params=None):
        request = self.factory.get('/api/v1/claws/org-usage/', params or {})
        force_authenticate(request, user=user)
        for key, value in (headers or {}).items():
            request.META[key] = value
        return request

    @patch('claws.org_usage._caller_org_ids', return_value=[100])
    def test_org_scoped_attribution_no_cross_org_leak(self, _):
        request = self._request(self._user(), headers={'HTTP_X_ORGANIZATION_ID': '100'})
        response = OrgUsageView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        body = response.data
        self.assertEqual(body['org_id'], 100)
        usage = {u['day']: u for u in body['usage']}
        today = str(self.today)
        self.assertIn(today, usage)
        metrics = usage[today]['metrics']
        self.assertEqual(metrics['agent_turn']['total'], 42)
        self.assertEqual(metrics['tokens_in']['total'], 1000)
        # Foreign org 200's usage (999) must never appear.
        totals = [m['total'] for u in body['usage'] for m in u['metrics'].values()]
        self.assertNotIn(999, totals)

    @patch('claws.org_usage._caller_org_ids', return_value=[100])
    def test_forged_org_header_fails_closed(self, _):
        # Caller belongs to org 100 only; a forged header for org 200 must be denied.
        request = self._request(self._user(), headers={'HTTP_X_ORGANIZATION_ID': '200'})
        response = OrgUsageView.as_view()(request)
        self.assertEqual(response.status_code, 403)
        self.assertNotIn('999', str(response.data))

    @patch('claws.org_usage._caller_org_ids', return_value=[100])
    @patch('claws.org_usage._is_personal_org', return_value=True)
    def test_personal_org_fallback(self, _personal, _orgs):
        request = self._request(self._user())  # no header
        response = OrgUsageView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['org_id'], 100)

    @patch('claws.org_usage._caller_org_ids', return_value=[])
    def test_no_org_context_fails_closed(self, _):
        request = self._request(self._user())
        response = OrgUsageView.as_view()(request)
        self.assertEqual(response.status_code, 403)

    def test_clamp_days(self):
        self.assertEqual(_clamp_days(None), 30)
        self.assertEqual(_clamp_days('7'), 7)
        self.assertEqual(_clamp_days('0'), 1)
        self.assertEqual(_clamp_days('500'), 90)
        self.assertEqual(_clamp_days('abc'), 30)
