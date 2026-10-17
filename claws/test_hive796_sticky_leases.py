"""HIVE-796: caller-identity token-lease stickiness (sticky_key).

Upstream provider prefix caches are per-account; re-leasing a different
subscription account to the same agent forces a full upstream re-prefill
(2026-07-16 grok TTFT incident). The model-token broker path pins a
sticky_key to one entry while it stays eligible, fails over to the healthiest
sibling when it is not, and updates the pin on failover (no flap-back).
"""
import base64
import json
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from . import token_coordinator
from .authentication import FederatedUser
from .coordinator_views import (
    ModelTokenView,
    TokenPoolAllocatorQaView,
    _quota_routing_rank,
)
from hive_platform.models import TokenEntry, TokenLease, TokenPool, TokenStickyAssignment


@override_settings(
    HIVE_TOKEN_COORDINATOR_KEK='sticky-test-kek',
    HIVE_TOKEN_COORDINATOR_KEK_ID='sticky-v1',
    HIVE_COORDINATOR_AUDIENCE='hive-coordinator',
    HIVE_COORDINATOR_BROKER_SCOPE='coordinator:model-token',
    CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
)
class StickyLeaseTests(TestCase):
    def setUp(self):
        self.pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id='42',
            provider=TokenPool.Provider.ANTHROPIC,
        )
        self.a = self._entry('acct-a', 'sk-ant-oat01-acct-a')
        self.b = self._entry('acct-b', 'sk-ant-oat01-acct-b')

    def _entry(self, label, access):
        entry = TokenEntry(pool=self.pool, label=label, priority=1)
        entry.set_token(json.dumps({
            'type': 'claude_oauth',
            'access_token': access,
            'refresh_token': f'sk-ant-ort01-{label}',
        }))
        entry.save()
        return entry

    def _post(self, sticky_key='', exclude_entry_id='', provider=TokenPool.Provider.ANTHROPIC):
        claims = {
            'user_id': 0,
            'aud': ['hive-coordinator'],
            'scope': 'coordinator:model-token',
            'owner_kind': 'user',
            'owner_id': '42',
            'workload_id': 'sticky-test-broker',
        }
        body = {'provider': provider}
        if sticky_key:
            body['sticky_key'] = sticky_key
        if exclude_entry_id:
            body['exclude_entry_id'] = exclude_entry_id
        request = APIRequestFactory().post(
            '/hive/api/v1/coordinator/model-token', body, format='json',
        )
        force_authenticate(
            request,
            user=FederatedUser(user_id=0, email='broker@example.com', username='broker'),
            token=claims,
        )
        return ModelTokenView.as_view()(request)

    def test_same_sticky_key_keeps_the_same_entry(self):
        first = self._post(sticky_key='user:105')
        self.assertEqual(first.status_code, 200)
        token1 = first.data['token']
        # Without stickiness, lease-count balancing would now prefer the OTHER
        # entry (first one carries a live lease). With sticky_key it must not.
        second = self._post(sticky_key='user:105')
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.data['token'], token1)
        third = self._post(sticky_key='user:105')
        self.assertEqual(third.data['token'], token1)

    def test_different_sticky_keys_spread_across_entries(self):
        t1 = self._post(sticky_key='user:105').data['token']
        t2 = self._post(sticky_key='user:15').data['token']
        self.assertNotEqual(t1, t2)  # second caller lands on the least-leased sibling

    def test_failover_updates_pin_no_flap_back(self):
        token_a = self._post(sticky_key='user:105').data['token']
        pinned = TokenEntry.objects.get(label='acct-a' if 'acct-a' in token_a else 'acct-b')
        other = TokenEntry.objects.exclude(id=pinned.id).get()
        pinned.active = False
        pinned.save(update_fields=['active', 'updated_at'])

        failover = self._post(sticky_key='user:105')
        self.assertEqual(failover.status_code, 200)
        self.assertNotEqual(failover.data['token'], token_a)

        # Reactivate the original — the pin must now point at the failover
        # entry (no flap-back).
        pinned.active = True
        pinned.save(update_fields=['active', 'updated_at'])
        after = self._post(sticky_key='user:105')
        self.assertEqual(after.data['token'], failover.data['token'])
        self.assertEqual(
            TokenEntry.objects.get(id=other.id).leases.count(), 2,
        )

    def test_explicit_exclusion_rotates_and_updates_sticky_pin(self):
        first = self._post(sticky_key='user:105')
        pinned_id = first.data['entry_id']

        rotated = self._post(
            sticky_key='user:105', exclude_entry_id=pinned_id,
        )
        self.assertEqual(rotated.status_code, 200)
        self.assertNotEqual(rotated.data['entry_id'], pinned_id)

        # The same sticky caller stays on the replacement after the one-shot
        # exclusion has gone away; rotation must not flap back next turn.
        after = self._post(sticky_key='user:105')
        self.assertEqual(after.status_code, 200)
        self.assertEqual(after.data['entry_id'], rotated.data['entry_id'])

    def test_excluding_only_candidate_returns_not_found(self):
        self.b.active = False
        self.b.save(update_fields=['active', 'updated_at'])

        response = self._post(exclude_entry_id=str(self.a.id))
        self.assertEqual(response.status_code, 404)

    def test_absent_sticky_key_unchanged_behavior(self):
        r1 = self._post()
        r2 = self._post()
        self.assertEqual({r1.status_code, r2.status_code}, {200})
        # least-leases spread without stickiness: two callers alternate entries
        self.assertNotEqual(r1.data['token'], r2.data['token'])

    def test_grok_quota_guides_placement_and_rotates_exhausted_pin(self):
        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id='42',
            provider=TokenPool.Provider.XAI,
        )
        first = TokenEntry(pool=pool, label='grok-first', priority=0)
        first.set_token(json.dumps({'access_token': 'xai-first'}))
        first.quota_snapshot = {
            'confidence': 'ok',
            'fetched_at': token_coordinator._quota_now().isoformat(),
            'windows': [{'id': 'credits', 'left_percent': 80}],
        }
        first.save()
        positive = TokenEntry(pool=pool, label='grok-positive', priority=1)
        positive.set_token(json.dumps({'access_token': 'xai-positive'}))
        positive.quota_snapshot = {
            'confidence': 'ok',
            'fetched_at': token_coordinator._quota_now().isoformat(),
            'windows': [{'id': 'credits', 'left_percent': 60}],
        }
        positive.save()

        pinned = self._post(
            sticky_key='grok:pinned', provider=TokenPool.Provider.XAI,
        )
        self.assertEqual(pinned.data['entry_id'], str(first.id))

        first.quota_snapshot['windows'][0]['left_percent'] = 0
        first.save(update_fields=['quota_snapshot', 'updated_at'])
        first.refresh_from_db()
        # HIVE-796 merge: _quota_routing_rank returns (tier, drain_band) now.
        # These asserted the pre-merge scalar tier. Tier is what this test is
        # about (healthy vs exhausted), so assert element [0] rather than
        # pinning the band — the band is even-drain ordering, not this
        # test's subject, and hard-coding it would couple an unrelated test
        # to the banding width.
        self.assertEqual(_quota_routing_rank(positive)[0], 0)
        self.assertEqual(_quota_routing_rank(first)[0], 2)

        rotated_from_exhausted = self._post(
            sticky_key='grok:pinned', provider=TokenPool.Provider.XAI,
        )
        new_placement = self._post(
            sticky_key='grok:new', provider=TokenPool.Provider.XAI,
        )

        self.assertEqual(rotated_from_exhausted.data['entry_id'], str(positive.id))
        self.assertEqual(new_placement.data['entry_id'], str(positive.id))

    def test_grok_broker_never_routes_confirmed_zero_credit_account(self):
        now = token_coordinator._quota_now()
        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id='42',
            provider=TokenPool.Provider.XAI,
        )
        exhausted = TokenEntry(pool=pool, label='grok-exhausted', priority=0)
        exhausted.set_token(json.dumps({'access_token': 'xai-exhausted'}))
        exhausted.quota_snapshot = {
            'source': 'grok_xai_usage',
            'confidence': 'ok',
            'fetched_at': (now - timedelta(hours=2)).isoformat(),
            'windows': [{
                'id': 'credits',
                'left_percent': 0.0,
                'resets_at': (now + timedelta(days=3)).isoformat(),
            }],
        }
        exhausted.save()

        response = self._post(
            sticky_key='grok:only-exhausted',
            provider=TokenPool.Provider.XAI,
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(TokenLease.objects.filter(entry=exhausted).exists())

    def test_grok_broker_never_routes_confirmed_oauth_refresh_failure(self):
        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id='42',
            provider=TokenPool.Provider.XAI,
        )
        failed = TokenEntry(pool=pool, label='grok-auth-failed', priority=0)
        failed.set_token(json.dumps({'access_token': 'xai-auth-failed'}))
        failed.quota_snapshot = {
            'source': 'grok_xai_usage',
            'confidence': 'error',
            'fetched_at': token_coordinator._quota_now().isoformat(),
            'detail': 'oauth_refresh_failed:XaiOAuthRefreshError',
            'windows': [],
        }
        failed.save()
        self.assertEqual(failed.quota_snapshot['detail'], 'oauth_refresh_failed')

        response = self._post(
            sticky_key='grok:only-auth-failed',
            provider=TokenPool.Provider.XAI,
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(TokenLease.objects.filter(entry=failed).exists())

    def test_grok_distinct_callers_are_durably_balanced_and_stable(self):
        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id='42',
            provider=TokenPool.Provider.XAI,
        )
        entries = []
        for index in range(3):
            entry = TokenEntry(pool=pool, label=f'grok-{index}', priority=index)
            entry.set_token(json.dumps({'access_token': f'xai-{index}'}))
            entry.quota_snapshot = {
                'confidence': 'ok',
                'fetched_at': token_coordinator._quota_now().isoformat(),
                # HIVE-796: keep all three siblings inside ONE 10% drain band
                # (88/87/86 -> band 8). 80/79/78 straddled bands 8 and 7, so
                # once placement became correctly quota-weighted only the 80
                # account was eligible and there was nothing left to balance.
                # Cross-band placement is intentionally quota-weighted and is
                # already covered by the 20-vs-100 regression; this test is
                # about durable balance WITHIN a band.
                'windows': [{'id': 'credits', 'left_percent': 88 - index}],
            }
            entry.save()
            entries.append(entry)

        placements = {}
        for index in range(12):
            sticky_key = f'grok:agent-{index}'
            placements[sticky_key] = self._post(
                sticky_key=sticky_key,
                provider=TokenPool.Provider.XAI,
            ).data['entry_id']
            # Prove durable assignment counts—not the 15-minute live-lease
            # snapshot—drive later staggered first starts.
            TokenLease.objects.update(
                expires_at=timezone.now() - timedelta(seconds=1),
            )

        self.assertEqual(
            sorted(
                TokenStickyAssignment.objects.filter(entry=entry).count()
                for entry in entries
            ),
            [4, 4, 4],
        )
        self.assertEqual(set(placements.values()), {str(entry.id) for entry in entries})

        # Repeated turns remain on the original account, preserving xAI's
        # account-scoped prefix cache rather than rotating for request fairness.
        for sticky_key, entry_id in placements.items():
            repeated = self._post(
                sticky_key=sticky_key,
                provider=TokenPool.Provider.XAI,
            )
            self.assertEqual(repeated.data['entry_id'], entry_id)

        assignment = TokenStickyAssignment.objects.first()
        self.assertEqual(len(assignment.sticky_key_hash), 64)
        self.assertNotIn('grok:agent-', assignment.sticky_key_hash)


@override_settings(
    HIVE_TOKEN_COORDINATOR_KEK='drain-test-kek',
    HIVE_TOKEN_COORDINATOR_KEK_ID='drain-v1',
    HIVE_COORDINATOR_AUDIENCE='hive-coordinator',
    HIVE_COORDINATOR_BROKER_SCOPE='coordinator:model-token',
    CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
)
class EvenDrainTests(TestCase):
    """HIVE-796 even drain: quota-weighted account selection for ALL providers.

    Operator 2026-07-16: xAI org-xai-302a608a drained to 71% credits while
    org-xai-e4dd960e sat at 100% never used — the allocator only spread by
    live-lease count (15-min horizon), so long-term drain skew never
    corrected. New leases must prefer the sibling with the MOST remaining
    quota; stickiness still overrides for returning callers.
    """

    def setUp(self):
        self.pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id='42',
            provider=TokenPool.Provider.XAI,
        )

    def _entry(self, label, left_percent=None, confidence='ok', priority=1):
        from . import token_coordinator
        snapshot = {}
        if left_percent is not None:
            snapshot = {
                'confidence': confidence,
                'fetched_at': token_coordinator._quota_now().isoformat(),
                'windows': [{'id': 'credits', 'left_percent': left_percent}],
            }
        entry = TokenEntry(
            pool=self.pool, label=label, priority=priority,
            quota_snapshot=snapshot,
        )
        entry.set_token(f'xai-static-{label}')
        entry.save()
        return entry

    def _post(self, sticky_key=''):
        claims = {
            'user_id': 0,
            'aud': ['hive-coordinator'],
            'scope': 'coordinator:model-token',
            'owner_kind': 'user',
            'owner_id': '42',
            'workload_id': 'drain-test-broker',
        }
        body = {'provider': TokenPool.Provider.XAI}
        if sticky_key:
            body['sticky_key'] = sticky_key
        request = APIRequestFactory().post(
            '/hive/api/v1/coordinator/model-token', body, format='json',
        )
        force_authenticate(
            request,
            user=FederatedUser(user_id=0, email='broker@example.com', username='broker'),
            token=claims,
        )
        return ModelTokenView.as_view()(request)

    @staticmethod
    def _access(resp):
        import json as _json
        raw = resp.data['token']
        try:
            return _json.loads(raw).get('access_token', raw)
        except (TypeError, ValueError):
            return raw

    def test_new_lease_prefers_most_remaining_quota_sibling(self):
        # The 2026-07-16 shape: one account 71% drained-to, one untouched.
        self._entry('drained', left_percent=71, priority=0)   # better priority
        self._entry('untouched', left_percent=100, priority=9)
        resp = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._access(resp), 'xai-static-untouched')

    def test_same_band_falls_back_to_lease_count_spread(self):
        # 95 and 98 share the 90s band: burst callers must still spread.
        self._entry('a', left_percent=95)
        self._entry('b', left_percent=98)
        t1 = self._post().data['token']
        t2 = self._post().data['token']
        self.assertNotEqual(t1, t2)

    def test_unknown_quota_ranks_neutral_between_proven_states(self):
        self._entry('proven', left_percent=40)
        self._entry('opaque', left_percent=None)      # no snapshot at all
        self._entry('exhausted', left_percent=0)
        resp = self._post()
        # Proven capacity first; opaque would come next; exhausted last.
        self.assertEqual(self._access(resp), 'xai-static-proven')

    def test_partial_confidence_stays_neutral(self):
        self._entry('partial', left_percent=100, confidence='partial')
        self._entry('proven', left_percent=30, confidence='ok')
        resp = self._post()
        # A trusted 30% beats an untrusted 100% (partial ranks neutral tier 1).
        self.assertEqual(self._access(resp), 'xai-static-proven')

    def test_sticky_pin_overrides_quota_ranking(self):
        self._entry('low', left_percent=20)
        self._entry('high', left_percent=100)
        first = self._post(sticky_key='user:7')
        self.assertEqual(self._access(first), 'xai-static-high')
        # Drain the pinned account's snapshot below the sibling — the pin must
        # still win while the entry remains eligible (stickiness is primary).
        from . import token_coordinator
        high = TokenEntry.objects.get(label='high')
        high.quota_snapshot = {
            'confidence': 'ok',
            'fetched_at': token_coordinator._quota_now().isoformat(),
            'windows': [{'id': 'credits', 'left_percent': 5}],
        }
        high.save(update_fields=['quota_snapshot', 'updated_at'])
        second = self._post(sticky_key='user:7')
        self.assertEqual(self._access(second), 'xai-static-high')

    def _set_quota(self, label, left_percent):
        from . import token_coordinator
        entry = TokenEntry.objects.get(label=label)
        entry.quota_snapshot = {
            'confidence': 'ok',
            'fetched_at': token_coordinator._quota_now().isoformat(),
            'windows': [{'id': 'credits', 'left_percent': left_percent}],
        }
        entry.save(update_fields=['quota_snapshot', 'updated_at'])

    def test_pin_exhaustion_fails_over_and_updates_pin(self):
        """Review P1 regression (production order): pin -> exhaust -> failover
        -> no flap-back. A returning caller pinned to an account whose fresh
        trusted quota hits 0%% must be moved to the healthy sibling, and the
        pin must follow the new selection."""
        self._entry('a', left_percent=90)
        self._entry('b', left_percent=60)
        # 1st top-level request: pins the healthiest account (a).
        first = self._post(sticky_key='user:9')
        self.assertEqual(self._access(first), 'xai-static-a')
        # a's fresh trusted quota drops to 0 while b has capacity.
        self._set_quota('a', 0)
        # 2nd top-level request: MUST fail over to b (not re-issue a).
        second = self._post(sticky_key='user:9')
        self.assertEqual(self._access(second), 'xai-static-b')
        # a recovers — 3rd request stays on b (pin was updated; no flap-back).
        self._set_quota('a', 100)
        third = self._post(sticky_key='user:9')
        self.assertEqual(self._access(third), 'xai-static-b')

    def test_all_exhausted_pin_still_sticks_as_last_resort(self):
        """When EVERY sibling is proven exhausted, tier-2 issuance remains a
        last resort and stickiness applies among equals (no churn)."""
        self._entry('a', left_percent=50)
        self._entry('b', left_percent=40)
        first = self._post(sticky_key='user:11')
        self.assertEqual(self._access(first), 'xai-static-a')
        self._set_quota('a', 0)
        self._set_quota('b', 0)
        second = self._post(sticky_key='user:11')
        self.assertEqual(second.status_code, 200)
        self.assertEqual(self._access(second), 'xai-static-a')

    def test_openai_exact_quota_ranks_even_drain_not_priority(self):
        """Review P1 regression (PR #29): OpenAI/Codex exact-quota telemetry
        must rank quota like Anthropic/xAI, or the Codex pool keeps the
        original uneven-drain behaviour (one account drained, sibling idle).

        Two OpenAI siblings with fresh EXACT usage windows (codex.bar parity):
        one at 20% remaining but better priority, one at 100% remaining but
        worse priority. A NEW lease must select the 100% sibling — before the
        fix, `accepted_confidence` omitted OPENAI so both ranked neutral
        (1, 0) and the sort fell through to priority, handing the new lease
        to the 20%-remaining account.
        """
        from . import token_coordinator

        def _jwt(payload: dict) -> str:
            body = base64.urlsafe_b64encode(
                json.dumps(payload).encode()
            ).decode().rstrip('=')
            return f'h.{body}.s'

        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id='42',
            provider=TokenPool.Provider.OPENAI,
        )

        def make(label, left_percent, priority):
            snapshot = {
                'confidence': 'exact',
                'fetched_at': token_coordinator._quota_now().isoformat(),
                'windows': [{'id': 'credits', 'left_percent': left_percent}],
            }
            entry = TokenEntry(
                pool=pool, label=label, priority=priority, quota_snapshot=snapshot,
            )
            entry.set_token(json.dumps({
                'access_token': _jwt({'exp': 4_102_444_800, 'sub': label}),
                'refresh_token': f'rto-{label}',
                'id_token': f'id-{label}',
                'email': f'{label}@shizuha.com',
                'account_id': f'acc-{label}',
            }, sort_keys=True))
            entry.save()
            return entry

        make('low', left_percent=20, priority=0)    # better priority, less quota
        make('high', left_percent=100, priority=9)  # worse priority, more quota

        claims = {
            'user_id': 0,
            'aud': ['hive-coordinator'],
            'scope': 'coordinator:model-token',
            'owner_kind': 'user',
            'owner_id': '42',
            'workload_id': 'drain-test-broker',
        }
        body = {'provider': TokenPool.Provider.OPENAI}
        request = APIRequestFactory().post(
            '/hive/api/v1/coordinator/model-token', body, format='json',
        )
        force_authenticate(
            request,
            user=FederatedUser(user_id=0, email='broker@example.com', username='broker'),
            token=claims,
        )
        resp = ModelTokenView.as_view()(request)
        self.assertEqual(resp.status_code, 200)
        raw = resp.data['token']
        access = json.loads(raw).get('access_token', raw) if isinstance(raw, str) else raw
        # The broker returns the OPENAI payload as JSON with the JWT access_token;
        # decode its body to learn which account was selected.
        body = str(access).split('.')[1]
        body += '=' * (-len(body) % 4)
        selected = json.loads(base64.urlsafe_b64decode(body))['sub']
        self.assertEqual(selected, 'high')


class TokenPoolAllocatorQaFixtureTests(TestCase):
    """HIVE-1824 production-order fixture through the shared allocator boundary."""

    def _post(self, user, fixture='HIVE-1824'):
        request = APIRequestFactory().post(
            '/hive/api/v1/qa/tokenpool-allocator',
            {'fixture': fixture},
            format='json',
        )
        force_authenticate(
            request,
            user=user,
            token={'user_id': user.id, 'is_staff': user.is_staff},
        )
        return TokenPoolAllocatorQaView.as_view()(request)

    def test_staff_probe_exercises_contract_without_secret_or_residue(self):
        staff = FederatedUser(
            user_id=77,
            email='qa@example.com',
            username='qa',
            is_staff=True,
        )

        with patch(
            'hive_platform.coordinator_views.token_coordinator._selection_from_entry',
            side_effect=AssertionError('QA fixture must not reach provider-token selection'),
        ) as provider_selection:
            response = self._post(staff)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['passed'], response.data)
        self.assertTrue(response.data['positive_controls']['passed'])
        self.assertEqual(
            set(response.data['positive_controls']['initial_candidate_refs']),
            {'fresh-high', 'fresh-mid', 'coarse-zero-0', 'coarse-zero-1'},
        )
        self.assertTrue(all(
            count == 2
            for count in response.data['positive_controls'][
                'rows_visible_by_shape'
            ].values()
        ))
        self.assertTrue(all(
            observation['passed']
            for observation in response.data['observations'].values()
        ))
        self.assertEqual(
            response.data['observations']['genuinely_dry_pool']['status'],
            404,
        )
        self.assertEqual(
            response.data['observations']['genuinely_dry_pool']['response']['reason'],
            'pool_dry',
        )
        self.assertEqual(response.data['safety']['provider_credential_rows'], 0)
        self.assertEqual(response.data['safety']['leases_issued'], 0)
        self.assertTrue(response.data['teardown']['passed'])
        self.assertFalse(any(response.data['teardown']['residue'].values()))
        provider_selection.assert_not_called()
        self.assertFalse(TokenPool.objects.exists())
        self.assertFalse(TokenEntry.objects.exists())
        self.assertFalse(TokenStickyAssignment.objects.exists())
        self.assertFalse(TokenLease.objects.exists())

    def test_non_staff_or_wrong_fixture_key_fails_closed(self):
        member = FederatedUser(
            user_id=78,
            email='member@example.com',
            username='member',
            is_staff=False,
        )
        staff = FederatedUser(
            user_id=79,
            email='staff@example.com',
            username='staff',
            is_staff=True,
        )

        self.assertEqual(self._post(member).status_code, 404)
        self.assertEqual(self._post(staff, fixture='wrong').status_code, 404)
        self.assertFalse(TokenPool.objects.exists())
