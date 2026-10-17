"""HIVE-124 — Coordinator service-core tests: pick / cooldown / LRU / health-check.

Covers the selection algorithm (owner-scoped, priority -> least-loaded -> LRU),
the report-status cooldown semantics (ok / 429 / 401), session accounting, and the
health-check loop that auto-cools dead tokens before any agent picks them.
"""
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from hive_platform.models import TokenEntry, TokenPool
from . import token_coordinator as tc

KEK = dict(HIVE_TOKEN_COORDINATOR_KEK='selection-test-kek', HIVE_TOKEN_COORDINATOR_KEK_ID='sel-v1')


def _entry(owner_id, label, token, *, priority=0, active=True, sessions=0,
           cooldown_until=None, last_used_at=None, provider=TokenPool.Provider.ANTHROPIC,
           owner_kind=TokenPool.OwnerKind.USER, supported_models=None):
    pool, _ = TokenPool.objects.get_or_create(owner_kind=owner_kind, owner_id=str(owner_id), provider=provider)
    e = TokenEntry(pool=pool, label=label, priority=priority, active=active,
                   active_sessions=sessions, cooldown_until=cooldown_until, last_used_at=last_used_at,
                   supported_models=supported_models or [])
    e.set_token(token)
    e.save()
    return e


@override_settings(**KEK)
class ProviderAgnosticQuotaEligibilityTests(TestCase):
    """A credential Hive has proven unusable must never be leased.

    Regression for the 2026-08-04 finding: `quota_routing_eligible` only
    evaluated xAI, so for every other provider it returned True unconditionally.
    `active_for_scopes` filters active/leasing_enabled/cooldown but never
    `last_status`, so an entry the model itself labels "Auth failed (dead)" was
    still selectable. 24 of 59 agents sat provider_unavailable on OpenAI
    rate_limit errors while a pool with real headroom was available — pool
    SELECTION, not actual exhaustion.
    """

    def _openai(self, label, **kw):
        return _entry(900, label, f'tok-{label}',
                      provider=TokenPool.Provider.OPENAI, **kw)

    def test_auth_failed_openai_entry_is_not_eligible(self):
        e = self._openai('dead')
        e.last_status = TokenEntry.LastStatus.AUTH_FAILED
        e.save(update_fields=['last_status'])
        self.assertFalse(tc.quota_routing_eligible(e))

    def test_fully_exhausted_windows_are_not_eligible(self):
        e = self._openai('drained')
        future = (timezone.now() + timedelta(days=3)).isoformat()
        e.quota_snapshot = {'windows': [
            {'id': 'weekly', 'left_percent': 0.0, 'resets_at': future},
            {'id': 'spark', 'left_percent': 0.0, 'resets_at': future},
        ]}
        e.save(update_fields=['quota_snapshot'])
        self.assertFalse(tc.quota_routing_eligible(e))

    def test_any_remaining_headroom_keeps_it_eligible(self):
        """Conservative by design — one live window is enough."""
        e = self._openai('partial')
        future = (timezone.now() + timedelta(days=3)).isoformat()
        e.quota_snapshot = {'windows': [
            {'id': 'weekly', 'left_percent': 0.0, 'resets_at': future},
            {'id': 'spark', 'left_percent': 17.0, 'resets_at': future},
        ]}
        e.save(update_fields=['quota_snapshot'])
        self.assertTrue(tc.quota_routing_eligible(e))

    def test_missing_or_unparseable_snapshot_fails_open(self):
        self.assertTrue(tc.quota_routing_eligible(self._openai('nosnap')))
        e = self._openai('junk')
        e.quota_snapshot = {'windows': 'not-a-list'}
        e.save(update_fields=['quota_snapshot'])
        self.assertTrue(tc.quota_routing_eligible(e))

    def test_aware_reset_stamp_fully_exhausted_is_not_eligible(self):
        """Regression: live quota snapshots carry aware '+00:00' reset stamps
        while Hive runs USE_TZ=False (naive timezone.now()). Comparing them
        directly raised TypeError, was swallowed by the broad except, and FAILED
        OPEN — a proven-exhausted free account (0% left, reset weeks away) stayed
        eligible and kept the whole codex fleet pinned to it, freezing every
        codex agent's live-activity feed (2026-08-02 outage). The comparison must
        happen in one consistent aware-UTC frame."""
        e = self._openai('drained-aware')
        future = timezone.make_aware(
            timezone.now() + timedelta(days=3),
            timezone.get_current_timezone(),
        ).isoformat()  # e.g. '2026-08-10T...+00:00'
        e.quota_snapshot = {'windows': [
            {'id': 'weekly', 'left_percent': 0.0, 'resets_at': future},
        ]}
        e.save(update_fields=['quota_snapshot'])
        self.assertFalse(tc.quota_routing_eligible(e))
        self.assertTrue(tc._all_quota_windows_exhausted(e))

    def test_aware_reset_stamp_already_past_is_eligible_again(self):
        """A fully-exhausted window whose aware reset has already passed must
        become eligible again (not get stuck exhausted forever)."""
        e = self._openai('reset-aware')
        past = timezone.make_aware(
            timezone.now() - timedelta(minutes=5),
            timezone.get_current_timezone(),
        ).isoformat()
        e.quota_snapshot = {'windows': [
            {'id': 'weekly', 'left_percent': 0.0, 'resets_at': past},
        ]}
        e.save(update_fields=['quota_snapshot'])
        self.assertTrue(tc.quota_routing_eligible(e))
        self.assertFalse(tc._all_quota_windows_exhausted(e))

    def test_already_reset_window_is_eligible_again(self):
        e = self._openai('reset')
        past = (timezone.now() - timedelta(minutes=5)).isoformat()
        e.quota_snapshot = {'windows': [
            {'id': 'weekly', 'left_percent': 0.0, 'resets_at': past},
        ]}
        e.save(update_fields=['quota_snapshot'])
        self.assertTrue(tc.quota_routing_eligible(e))

    def test_pick_skips_the_dead_entry_and_returns_the_live_one(self):
        dead = self._openai('dead2', priority=0)
        dead.last_status = TokenEntry.LastStatus.AUTH_FAILED
        dead.save(update_fields=['last_status'])
        live = self._openai('live', priority=1)
        picked = tc.pick_token(TokenPool.OwnerKind.USER, '900', TokenPool.Provider.OPENAI)
        self.assertIsNotNone(picked, 'a healthy pool must still be leasable')
        self.assertEqual(picked.id, live.id)


@override_settings(**KEK)
class PickTokenTests(TestCase):
    def test_pick_is_owner_scoped(self):
        a = _entry(100, 'a', 'tok-A')
        _entry(200, 'b', 'tok-B')
        picked = tc.pick_token(TokenPool.OwnerKind.USER, '100', TokenPool.Provider.ANTHROPIC)
        self.assertEqual(picked.id, a.id)
        # owner 100 can never receive owner 200's token, even with none of its own
        a.active = False
        a.save(update_fields=['active'])
        self.assertIsNone(tc.pick_token(TokenPool.OwnerKind.USER, '100', TokenPool.Provider.ANTHROPIC))

    def test_pick_prefers_lowest_priority_tier(self):
        _entry(1, 'low-pri-num', 'tok-1', priority=0)
        hi = _entry(1, 'high-pri-num', 'tok-2', priority=5)
        picked = tc.pick_token(TokenPool.OwnerKind.USER, '1', TokenPool.Provider.ANTHROPIC)
        self.assertEqual(picked.label, 'low-pri-num')
        self.assertNotEqual(picked.id, hi.id)

    def test_pick_least_loaded_then_increments(self):
        _entry(1, 'busy', 'tok-1', priority=0, sessions=3)
        idle = _entry(1, 'idle', 'tok-2', priority=0, sessions=0)
        picked = tc.pick_token(TokenPool.OwnerKind.USER, '1', TokenPool.Provider.ANTHROPIC)
        self.assertEqual(picked.id, idle.id)
        self.assertEqual(picked.active_sessions, 1)  # incremented on pick
        picked.refresh_from_db()
        self.assertEqual(picked.active_sessions, 1)
        self.assertIsNotNone(picked.last_used_at)

    def test_pick_lru_among_equal_load(self):
        old = _entry(1, 'old', 'tok-1', last_used_at=timezone.now() - timedelta(hours=2))
        _entry(1, 'recent', 'tok-2', last_used_at=timezone.now())
        picked = tc.pick_token(TokenPool.OwnerKind.USER, '1', TokenPool.Provider.ANTHROPIC)
        self.assertEqual(picked.id, old.id)

    def test_pick_skips_inactive_and_cooled(self):
        _entry(1, 'dead', 'tok-1', active=False)
        _entry(1, 'cooling', 'tok-2', cooldown_until=timezone.now() + timedelta(minutes=30))
        self.assertIsNone(tc.pick_token(TokenPool.OwnerKind.USER, '1', TokenPool.Provider.ANTHROPIC))
        live = _entry(1, 'live', 'tok-3')
        self.assertEqual(tc.pick_token(TokenPool.OwnerKind.USER, '1', TokenPool.Provider.ANTHROPIC).id, live.id)


@override_settings(**KEK)
class ReportStatusSemanticsTests(TestCase):
    def test_ok_clears_cooldown(self):
        e = _entry(1, 'e', 'tok', cooldown_until=timezone.now() + timedelta(hours=1))
        tc.apply_status_report(e, 'ok')
        e.refresh_from_db()
        self.assertIsNone(e.cooldown_until)
        self.assertEqual(e.last_status, TokenEntry.LastStatus.OK)
        self.assertTrue(e.active)

    def test_429_honors_retry_after_else_1h(self):
        now = timezone.now()
        e = _entry(1, 'e', 'tok')
        tc.apply_status_report(e, '429', retry_after=120, now=now)
        e.refresh_from_db()
        self.assertAlmostEqual((e.cooldown_until - now).total_seconds(), 120, delta=2)
        self.assertEqual(e.last_status, TokenEntry.LastStatus.RATE_LIMITED)
        self.assertTrue(e.active)  # transient, not dead
        e2 = _entry(2, 'e2', 'tok2')
        tc.apply_status_report(e2, 'quota', now=now)
        e2.refresh_from_db()
        self.assertAlmostEqual((e2.cooldown_until - now).total_seconds(), tc.RATE_LIMIT_COOLDOWN_SECONDS, delta=2)

    def test_429_caps_cool_when_model_specific_window_remains(self):
        """Weekly dead + Spark extra alive must not park the credential for 1h."""
        now = timezone.now()
        e = _entry(3, 'spark-capable', 'tok-spark', provider=TokenPool.Provider.OPENAI)
        e.quota_snapshot = {
            'plan': 'pro',
            'confidence': 'exact',
            'windows': [
                {'id': 'weekly', 'label': 'Weekly', 'left_percent': 0.0, 'used_percent': 100.0},
                {'id': 'extra:GPT-5.3-Codex-Spark', 'label': 'GPT-5.3-Codex-Spark',
                 'left_percent': 82.0, 'used_percent': 18.0},
            ],
        }
        e.save(update_fields=['quota_snapshot', 'updated_at'])
        tc.apply_status_report(e, 'quota', now=now)  # would default to 1h
        e.refresh_from_db()
        cool_secs = (e.cooldown_until - now).total_seconds()
        self.assertLessEqual(cool_secs, tc.MODEL_SPECIFIC_REMAINING_COOLDOWN_CAP_SECONDS + 2)
        self.assertGreater(cool_secs, 0)
        self.assertEqual(e.last_status, TokenEntry.LastStatus.RATE_LIMITED)
        self.assertTrue(e.active)

    def test_401_marks_dead(self):
        e = _entry(1, 'e', 'tok')
        tc.apply_status_report(e, '401')
        e.refresh_from_db()
        self.assertFalse(e.active)  # dead — pick_token will never hand it out
        self.assertEqual(e.last_status, TokenEntry.LastStatus.AUTH_FAILED)
        self.assertIsNotNone(e.cooldown_until)

    def test_release_session_floors_at_zero(self):
        e = _entry(1, 'e', 'tok', sessions=1)
        tc.release_session(e)
        e.refresh_from_db()
        self.assertEqual(e.active_sessions, 0)
        tc.release_session(e)  # idempotent floor
        e.refresh_from_db()
        self.assertEqual(e.active_sessions, 0)


@override_settings(**KEK)
class HealthCheckTests(TestCase):
    def test_health_check_cools_and_kills_with_injected_probe(self):
        ok = _entry(1, 'ok', 'tok-ok')
        limited = _entry(1, 'limited', 'tok-limited')
        dead = _entry(1, 'dead', 'tok-dead')
        verdicts = {ok.id: 'ok', limited.id: 'rate_limited', dead.id: 'auth_failed'}
        summary = tc.run_health_check(probe=lambda e: verdicts[e.id])
        self.assertEqual(summary['checked'], 3)
        self.assertEqual(summary['dead'], 1)
        self.assertEqual(summary['rate_limited'], 1)
        dead.refresh_from_db(); limited.refresh_from_db(); ok.refresh_from_db()
        self.assertFalse(dead.active)
        self.assertIsNotNone(limited.cooldown_until)
        self.assertEqual(ok.last_status, TokenEntry.LastStatus.OK)
        for e in (ok, limited, dead):
            self.assertIsNotNone(e.last_checked_at)

    def test_health_check_skips_cooled_entries(self):
        # ren P1: a token on a live 429 cooldown must NOT be probed — an 'ok' probe
        # would otherwise clear its cooldown and feed it back to the picker early.
        future = timezone.now() + timedelta(minutes=45)
        cooled = _entry(1, 'cooled', 'tok-cooled', cooldown_until=future)
        live = _entry(1, 'live', 'tok-live')
        probed = []
        summary = tc.run_health_check(probe=lambda e: probed.append(e.id) or 'ok')
        self.assertEqual(probed, [live.id])         # cooled entry was never probed
        self.assertEqual(summary['checked'], 1)
        cooled.refresh_from_db()
        self.assertEqual(cooled.cooldown_until, future)  # cooldown preserved intact
        self.assertIsNone(cooled.last_checked_at)

    def test_health_check_network_error_does_not_cool(self):
        e = _entry(1, 'e', 'tok')
        summary = tc.run_health_check(probe=lambda _e: 'error')
        self.assertEqual(summary['errored'], 1)
        e.refresh_from_db()
        self.assertTrue(e.active)               # transient blip never kills a token
        self.assertIsNone(e.cooldown_until)
        self.assertIsNotNone(e.last_checked_at)  # but we record the attempt

    def test_health_check_respects_long_requested_cooldown(self):
        now = timezone.now()
        stale = _entry(
            1, 'long-cooldown', 'tok-long-cooldown',
            cooldown_until=now + timedelta(hours=1),
        )
        stale.last_rate_limit_at = now - timedelta(minutes=30)
        stale.last_status = TokenEntry.LastStatus.RATE_LIMITED
        stale.save(update_fields=['last_rate_limit_at', 'last_status'])

        probed = []
        summary = tc.run_health_check(now=now, probe=lambda e: probed.append(e.id) or 'ok')

        # HIVE-1747: a cooldown stamped for longer than the normal throttle window
        # (1.5h here) is a deliberate long cool — the proactive sweep respects it
        # (does not clear), so the entry stays non-routable and is not probed.
        self.assertEqual(summary['cooldown_cleared'], 0)
        self.assertEqual(summary['checked'], 0)
        self.assertEqual(probed, [])
        stale.refresh_from_db()
        self.assertIsNotNone(stale.cooldown_until)
        self.assertEqual(stale.last_status, TokenEntry.LastStatus.RATE_LIMITED)

    def test_health_check_respects_fresh_cooldown(self):
        now = timezone.now()
        fresh = _entry(
            1, 'fresh-cooldown', 'tok-fresh-cooldown',
            cooldown_until=now + timedelta(hours=1),
        )
        fresh.last_rate_limit_at = now - timedelta(minutes=1)
        fresh.last_status = TokenEntry.LastStatus.RATE_LIMITED
        fresh.save(update_fields=['last_rate_limit_at', 'last_status'])

        probed = []
        summary = tc.run_health_check(now=now, probe=lambda e: probed.append(e.id) or 'ok')

        self.assertEqual(summary['cooldown_cleared'], 0)
        self.assertEqual(summary['checked'], 0)
        self.assertEqual(probed, [])
        fresh.refresh_from_db()
        self.assertIsNotNone(fresh.cooldown_until)
        self.assertEqual(fresh.last_status, TokenEntry.LastStatus.RATE_LIMITED)
        self.assertIsNone(fresh.last_checked_at)

    def test_openai_entries_not_deactivated_by_health_probe(self):
        # Regression for HIVE-241 (2026-06-28): probe_token_health was sending
        # the stored JSON OAuth blob as a raw Bearer token to api.openai.com →
        # 401 → auth_failed → active=False → both org OpenAI entries deactivated
        # → fleet drifted entirely to Anthropic.  Fix: return 'error' early for
        # OPENAI provider so the synthetic probe never mutates live entries.
        e = _entry(1, 'codex', '{"access_token":"at","refresh_token":"rt","email":"u@x.com"}',
                   provider=TokenPool.Provider.OPENAI)
        summary = tc.run_health_check(probe=tc.probe_token_health)
        self.assertEqual(summary['errored'], 1)
        e.refresh_from_db()
        self.assertTrue(e.active)       # must never be deactivated by the probe
        self.assertIsNone(e.cooldown_until)
        self.assertIsNotNone(e.last_checked_at)

    def test_anthropic_bundle_not_deactivated_by_health_probe(self):
        # Regression (2026-07-10, third instance of this bug class after
        # HIVE-241/OAT): full-login Claude OAuth bundles fell through to the
        # raw Bearer probe (Bearer {json} -> 401 -> auth_failed) — "Check now"
        # killed freshly-authed org-claude entries, and recovery (same probe)
        # kept them dead. A fresh bundle (access not near expiry) must probe
        # 'ok' with no refresh attempt and no destructive state change.
        import json as jsonlib
        import time as timelib
        bundle = jsonlib.dumps({
            'access_token': 'at', 'refresh_token': 'rt', 'email': 'u@a.com',
            'expires_at': int(timelib.time()) + 3600,
        })
        e = _entry(1, 'claude-bundle', bundle, provider=TokenPool.Provider.ANTHROPIC)
        summary = tc.run_health_check(probe=tc.probe_token_health)
        self.assertEqual(summary['ok'], 1)
        e.refresh_from_db()
        self.assertTrue(e.active)
        self.assertIsNone(e.cooldown_until)
        self.assertEqual(e.last_status, TokenEntry.LastStatus.OK)

    def test_near_expiry_anthropic_probe_never_refreshes_on_request_path(self):
        # HIVE-619: the broker abandons after 10s.  A synchronous 20s OAuth
        # refresh in this probe survived the disconnect; Daphne then cancelled
        # the ASGI app and asgiref blocked its event loop joining the worker.
        import json as jsonlib
        import time as timelib

        entry = _entry(
            1,
            'claude-near-expiry',
            jsonlib.dumps({
                'access_token': 'at', 'refresh_token': 'rt',
                'expires_at': int(timelib.time()) + 60,
            }),
            provider=TokenPool.Provider.ANTHROPIC,
        )
        with patch('hive_platform.token_coordinator._refresh_and_persist_claude_bundle') as refresh:
            started = timelib.monotonic()
            result = tc.probe_token_health(entry)
            elapsed = timelib.monotonic() - started

        self.assertEqual(result, 'error')
        self.assertLess(elapsed, 0.1)
        refresh.assert_not_called()


@override_settings(**KEK)
class AuthKillRecoveryTests(TestCase):
    """PLAT-881: an auth-killed token must self-heal once its backoff lapses, so the
    pool never sits dry waiting for a manual operator PATCH (2026-06-26 outage)."""

    def _dead_entry(self, *, expired=True, label='dead'):
        # Mark dead the production way, then move its backoff into the past (expired)
        # or leave it in the future (not yet eligible).
        e = _entry(1, label, 'tok-' + label)
        tc.apply_status_report(e, '401')
        e.refresh_from_db()
        self.assertFalse(e.active)
        self.assertEqual(e.last_status, TokenEntry.LastStatus.AUTH_FAILED)
        if expired:
            e.cooldown_until = timezone.now() - timedelta(minutes=1)
            e.save(update_fields=['cooldown_until'])
        return e

    def test_auth_failed_sets_recovery_backoff_not_immediate(self):
        now = timezone.now()
        e = _entry(1, 'e', 'tok')
        tc.apply_status_report(e, '401', now=now)
        e.refresh_from_db()
        # cooldown is a real backoff window, not ``now`` — so a dead token is not
        # instantly re-eligible (which would crash-loop).
        self.assertAlmostEqual(
            (e.cooldown_until - now).total_seconds(),
            tc.AUTH_FAILED_RECOVERY_BACKOFF_SECONDS, delta=2,
        )

    def test_unprobeable_oauth_token_recovers_after_backoff(self):
        # Claude OAuth cannot be probed; bounded optimistic recovery remains.
        e = self._dead_entry()
        summary = tc.recover_expired_dead_tokens(probe=lambda _e: 'error')
        self.assertEqual(summary['recovered'], 1)
        e.refresh_from_db()
        self.assertTrue(e.active)
        self.assertIsNone(e.cooldown_until)
        self.assertEqual(e.last_status, TokenEntry.LastStatus.UNKNOWN)

    def test_revoked_openai_token_requires_reauth_and_is_never_resurrected(self):
        e = _entry(
            1,
            'revoked-openai',
            '{"access_token":"expired","refresh_token":"revoked","account_id":"acct"}',
            provider=TokenPool.Provider.OPENAI,
        )
        tc.apply_status_report(e, '401')
        e.cooldown_until = timezone.now() - timedelta(minutes=1)
        e.save(update_fields=['cooldown_until'])

        probe_calls = []
        first = tc.recover_expired_dead_tokens(
            probe=lambda row: probe_calls.append(row.id) or 'error',
        )
        second = tc.recover_expired_dead_tokens(
            probe=lambda row: probe_calls.append(row.id) or 'error',
        )

        for summary in (first, second):
            self.assertEqual(summary['recovered'], 0)
            self.assertEqual(summary['still_dead'], 1)
            self.assertEqual(summary['manual_reauth_required'], 1)
        self.assertEqual(probe_calls, [])
        e.refresh_from_db()
        self.assertFalse(e.active)
        self.assertEqual(e.last_status, TokenEntry.LastStatus.AUTH_FAILED)

    def test_stale_openai_access_success_cannot_revive_revoked_refresh_grant(self):
        e = _entry(
            1,
            'revoked-openai-stale-access',
            '{"access_token":"still-valid","refresh_token":"revoked","account_id":"acct"}',
            provider=TokenPool.Provider.OPENAI,
        )
        tc.apply_status_report(e, 'auth_failed')

        applied = tc.apply_status_report(e, 'ok')

        self.assertEqual(applied, 'auth_failed')
        e.refresh_from_db()
        self.assertFalse(e.active)
        self.assertEqual(e.last_status, TokenEntry.LastStatus.AUTH_FAILED)
        self.assertIsNotNone(e.cooldown_until)

    def test_probed_healthy_token_recovers(self):
        e = self._dead_entry()
        summary = tc.recover_expired_dead_tokens(probe=lambda _e: 'ok')
        self.assertEqual(summary['recovered'], 1)
        e.refresh_from_db()
        self.assertTrue(e.active)
        self.assertEqual(e.last_status, TokenEntry.LastStatus.OK)

    def test_still_dead_token_stays_dead_and_rearms_backoff(self):
        e = self._dead_entry()
        summary = tc.recover_expired_dead_tokens(probe=lambda _e: 'auth_failed')
        self.assertEqual(summary['still_dead'], 1)
        self.assertEqual(summary['recovered'], 0)
        e.refresh_from_db()
        self.assertFalse(e.active)              # still out of the pool
        self.assertGreater(e.cooldown_until, timezone.now())  # backoff re-armed

    def test_recovery_ignores_unexpired_backoff(self):
        e = self._dead_entry(expired=False)     # backoff still in the future
        summary = tc.recover_expired_dead_tokens(probe=lambda _e: 'ok')
        self.assertEqual(summary['candidates'], 0)
        e.refresh_from_db()
        self.assertFalse(e.active)

    def test_recovery_ignores_operator_disabled_token(self):
        # Operator disable (dashboard PATCH) sets active=False WITHOUT a cooldown
        # stamp and without AUTH_FAILED — must never be auto-re-enabled.
        e = _entry(1, 'manual', 'tok-manual', active=False)
        self.assertEqual(e.last_status, TokenEntry.LastStatus.UNKNOWN)
        summary = tc.recover_expired_dead_tokens(probe=lambda _e: 'ok')
        self.assertEqual(summary['candidates'], 0)
        e.refresh_from_db()
        self.assertFalse(e.active)

    def test_ok_report_reactivates_dead_entry(self):
        e = self._dead_entry()
        tc.apply_status_report(e, 'ok')
        e.refresh_from_db()
        self.assertTrue(e.active)
        self.assertIsNone(e.cooldown_until)

    def test_health_check_revives_dead_token(self):
        e = self._dead_entry()
        summary = tc.run_health_check(probe=lambda _e: 'error')
        self.assertEqual(summary['recovered'], 1)
        e.refresh_from_db()
        self.assertTrue(e.active)

    def test_select_recovers_before_declaring_pool_dry(self):
        # Only token in the pool is auth-killed but its backoff lapsed → selection
        # must revive it instead of returning None (the operator's explicit ask).
        e = self._dead_entry(label='only')
        selection = tc.select_token_for_owner(
            '1', TokenPool.Provider.ANTHROPIC, probe=lambda _e: 'error',
        )
        self.assertIsNotNone(selection)
        e.refresh_from_db()
        self.assertTrue(e.active)


@override_settings(**KEK)
class ModelCapabilitySelectionTests(TestCase):
    """PLAT-5850: pick_token must never lease an account whose plan rejects the
    agent's pinned model.

    Regression for the 2026-08-08 recurrence: shizuhatrading (a ChatGPT account
    that 400s "model is not supported" for gpt-5.6-sol) was leased to codex
    agents because pick_token had no model-capability knowledge. A non-empty
    supported_models allowlist is authoritative; empty/unknown preserves the
    existing fail-open lease path.
    """

    def _openai(self, label, **kw):
        return _entry(900, label, f'tok-{label}',
                      provider=TokenPool.Provider.OPENAI, **kw)

    def test_entry_with_nonempty_allowlist_skips_unsupported_model(self):
        supported = self._openai('global', supported_models=['gpt-5.6-sol'])
        unsupported = self._openai('trading', supported_models=['gpt-5.3-codex'])
        # The unsupported entry is lower priority (picked first) but must be
        # skipped for gpt-5.6-sol.
        unsupported.priority = 0
        unsupported.save(update_fields=['priority'])
        supported.priority = 1
        supported.save(update_fields=['priority'])
        picked = tc.pick_token(
            TokenPool.OwnerKind.USER, '900', TokenPool.Provider.OPENAI,
            model='gpt-5.6-sol',
        )
        self.assertIsNotNone(picked)
        self.assertEqual(picked.id, supported.id)

    def test_unknown_allowlist_preserves_fail_open_lease_path(self):
        # Empty/unknown supported_models means "no capability constraint" — the
        # entry stays selectable even when a model is requested.
        e = self._openai('unprobed')
        picked = tc.pick_token(
            TokenPool.OwnerKind.USER, '900', TokenPool.Provider.OPENAI,
            model='gpt-5.6-sol',
        )
        self.assertIsNotNone(picked)
        self.assertEqual(picked.id, e.id)

    def test_no_model_parameter_ignores_allowlist(self):
        self._openai('trading', supported_models=['gpt-5.3-codex'])
        picked = tc.pick_token(
            TokenPool.OwnerKind.USER, '900', TokenPool.Provider.OPENAI,
        )
        self.assertIsNotNone(picked)

    def test_all_entries_unsupported_returns_none(self):
        self._openai('trading', supported_models=['gpt-5.3-codex'])
        picked = tc.pick_token(
            TokenPool.OwnerKind.USER, '900', TokenPool.Provider.OPENAI,
            model='gpt-5.6-sol',
        )
        self.assertIsNone(picked)

    def _with_plan(self, label, plan, *, priority=0, supported_models=None):
        entry = self._openai(label, priority=priority, supported_models=supported_models)
        entry.quota_snapshot = {'plan': plan}
        entry.save(update_fields=['quota_snapshot'])
        return entry

    def test_go_plan_empty_allowlist_skipped_for_gpt6_astra(self):
        go = self._with_plan('go-acct', 'go', priority=0)
        picked = tc.pick_token(
            TokenPool.OwnerKind.USER, '900', TokenPool.Provider.OPENAI,
            model='gpt-6-astra',
        )
        self.assertIsNone(picked)
        go.refresh_from_db()
        self.assertEqual(go.active_sessions, 0)

    def test_pro_selected_over_higher_priority_go_for_astra(self):
        self._with_plan('go-acct', 'go', priority=0)
        pro = self._with_plan('pro-acct', 'pro', priority=5)
        picked = tc.pick_token(
            TokenPool.OwnerKind.USER, '900', TokenPool.Provider.OPENAI,
            model='gpt-6-astra',
        )
        self.assertIsNotNone(picked)
        self.assertEqual(picked.id, pro.id)

    def test_free_plan_skipped_for_astra_unknown_plan_stays_open(self):
        self._with_plan('free-acct', 'free', priority=0)
        unknown = self._with_plan('mystery-acct', 'mystery', priority=3)
        picked = tc.pick_token(
            TokenPool.OwnerKind.USER, '900', TokenPool.Provider.OPENAI,
            model='openai/gpt-6-astra',
        )
        self.assertEqual(picked.id, unknown.id)

    def test_explicit_astra_allowlist_on_go_still_leases(self):
        go = self._with_plan(
            'go-granted', 'go', priority=0, supported_models=['gpt-6-astra'],
        )
        picked = tc.pick_token(
            TokenPool.OwnerKind.USER, '900', TokenPool.Provider.OPENAI,
            model='gpt-6-astra',
        )
        self.assertEqual(picked.id, go.id)

    def test_empty_plan_fail_open_for_astra(self):
        bare = self._openai('unprobed-astra')
        picked = tc.pick_token(
            TokenPool.OwnerKind.USER, '900', TokenPool.Provider.OPENAI,
            model='gpt-6-astra',
        )
        self.assertEqual(picked.id, bare.id)
