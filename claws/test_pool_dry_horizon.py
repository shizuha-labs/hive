"""A dry pool must tell the broker HOW LONG it will stay dry.

Operator 2026-08-05: with every xAI account cooling for 11-160h, Cortex could
only guess a TTL, so grok-4.5 kept reappearing as routable every few minutes and
agents kept probing a pool that could not possibly serve.
"""
from datetime import timedelta

from django.test import SimpleTestCase
from django.utils import timezone
from unittest.mock import patch

from . import coordinator_views as cv


class _Entry:
    def __init__(self, cooldown_until):
        self.cooldown_until = cooldown_until


class PoolDryHorizonTests(SimpleTestCase):
    def _response(self, entries):
        class _QS:
            def for_scopes(_s, scopes, provider):
                return entries
        with patch.object(cv, 'TokenEntry', create=True), \
             patch('hive_platform.models.TokenEntry.objects', _QS()):
            return cv._pool_dry_response('xai', [])

    def test_reports_the_earliest_cooldown(self):
        now = timezone.now()
        resp = self._response([
            _Entry(now + timedelta(hours=11)),
            _Entry(now + timedelta(hours=160)),
        ])
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.data['reason'], 'pool_dry')
        # Capped at 1h so the broker still re-checks in case an account is added.
        self.assertEqual(resp.data['retry_after_seconds'], 3600)

    def test_short_cooldown_is_reported_verbatim(self):
        now = timezone.now()
        resp = self._response([_Entry(now + timedelta(seconds=90))])
        self.assertLessEqual(resp.data['retry_after_seconds'], 90)
        self.assertGreater(resp.data['retry_after_seconds'], 0)

    def test_no_cooldowns_means_no_horizon_claimed(self):
        resp = self._response([])
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn(
            'retry_after_seconds', resp.data,
            'never invent a horizon — the broker must fall back to its own TTL',
        )

    def test_expired_cooldowns_are_ignored(self):
        now = timezone.now()
        resp = self._response([_Entry(now - timedelta(hours=2))])
        self.assertNotIn('retry_after_seconds', resp.data)

    def test_shape_stays_a_plain_404_for_older_brokers(self):
        resp = self._response([])
        self.assertEqual(resp.data['error'], 'Not found')
