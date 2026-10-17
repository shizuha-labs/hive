"""A transient Grok billing-probe failure must not be recorded as a permanent gap.

Operator report 2026-08-04: the Subscriptions dashboard showed "no usage API" on
8 xAI accounts, yet a per-row `Check now` returned real quota immediately.

Cause: `fetch_grok_billing` collapsed EVERY failure — timeout, 429, 5xx — into
`None`, indistinguishable from "this account has no SuperGrok credits". The
caller then stamped `confidence='unsupported'`, which
`_quota_snapshot_needs_refresh` treats as a permanent capability gap worth a
24-hour backoff (UNSUPPORTED_QUOTA_RECHECK_SECONDS). One blip therefore blanked
an account's quota for a day, while `Check now` (force=True) bypassed the
staleness gate and worked.

All 8 accounts were probed inside a 7-second burst (07:41:46-07:41:53) — the
shape that gets rate-limited.
"""
from unittest.mock import patch

from django.test import SimpleTestCase

from . import quota_fetchers as qf
from .token_coordinator import UNSUPPORTED_QUOTA_RECHECK_SECONDS


class _Resp:
    def __init__(self, status_code, content=b'', headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}


class GrokBillingTransientTests(SimpleTestCase):
    def test_timeout_is_transient(self):
        with patch.object(qf.requests, 'post', side_effect=TimeoutError('boom')):
            data, transient = qf.fetch_grok_billing_ex('tok')
        self.assertIsNone(data)
        self.assertTrue(transient, 'a network timeout says nothing about the plan')

    def test_rate_limit_is_transient(self):
        with patch.object(qf.requests, 'post', return_value=_Resp(429)):
            data, transient = qf.fetch_grok_billing_ex('tok')
        self.assertIsNone(data)
        self.assertTrue(transient, '429 is the provider throttling us, not a capability gap')

    def test_server_error_is_transient(self):
        for status in (500, 502, 503):
            with patch.object(qf.requests, 'post', return_value=_Resp(status)):
                _data, transient = qf.fetch_grok_billing_ex('tok')
            self.assertTrue(transient, f'{status} must be transient')

    def test_clean_rejection_is_definitive(self):
        # A 4xx that is not 429 is the provider saying "no", not "try later".
        with patch.object(qf.requests, 'post', return_value=_Resp(403)):
            data, transient = qf.fetch_grok_billing_ex('tok')
        self.assertIsNone(data)
        self.assertFalse(transient)

    def test_backcompat_wrapper_still_returns_plain_optional(self):
        with patch.object(qf.requests, 'post', return_value=_Resp(429)):
            self.assertIsNone(qf.fetch_grok_billing('tok'))


class GrokUsageClassificationTests(SimpleTestCase):
    """The classification is what drives the 24h backoff — assert it directly."""

    def test_transient_billing_failure_is_not_marked_unsupported(self):
        # 429 on billing, then the usage candidates also fail.
        with patch.object(qf.requests, 'post', return_value=_Resp(429)), \
             patch.object(qf.requests, 'get', return_value=_Resp(404)):
            out = qf.fetch_grok_usage('tok')
        self.assertNotEqual(
            out['confidence'], 'unsupported',
            'a throttled probe must not buy a 24h "no usage API" backoff',
        )
        self.assertEqual(out['confidence'], 'error')
        self.assertIn('billing_probe_transient', out['detail'])

    def test_genuine_capability_gap_still_marked_unsupported(self):
        # Billing cleanly rejects (403 => definitive) and both usage candidates 404.
        with patch.object(qf.requests, 'post', return_value=_Resp(403)), \
             patch.object(qf.requests, 'get', return_value=_Resp(404)):
            out = qf.fetch_grok_usage('tok')
        self.assertEqual(out['confidence'], 'unsupported')
        self.assertEqual(out['detail'], 'usage_api_unavailable')

    def test_unsupported_backoff_is_a_full_day(self):
        # Documents why misclassification is expensive rather than cosmetic.
        self.assertEqual(UNSUPPORTED_QUOTA_RECHECK_SECONDS, 24 * 60 * 60)
