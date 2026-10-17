"""
HIVE-127 — per-owner isolation negative tests.

These prove the keystone tenancy property from the isolation contract (wiki
``98ba5b0c…`` §5): **User A's tokens can never serve or be visible to User B.**
Each test maps to a numbered contract requirement and is written to FAIL on an
implementation that does not owner-scope its queries / broker-gate the raw-token
path / lease-bind report-status. Storage-layer scoping is covered separately in
``test_token_coordinator.py``; this file covers the API enforcement layer.
"""
from datetime import timedelta
import base64
import json
import time
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from .authentication import FederatedUser
from hive_platform.models import TokenPool, TokenEntry, TokenLease
from . import provisioner, token_coordinator
from .coordinator_views import (
    ModelTokenView,
    ReportStatusView,
    TokenEntryListCreateView,
    TokenEntryDetailView,
    _quota_routing_rank,
)

COORD_SETTINGS = dict(
    HIVE_TOKEN_COORDINATOR_KEK='isolation-test-kek',
    HIVE_TOKEN_COORDINATOR_KEK_ID='iso-v1',
    HIVE_COORDINATOR_AUDIENCE='hive-coordinator',
    HIVE_COORDINATOR_BROKER_SCOPE='coordinator:model-token',
)

factory = APIRequestFactory()


def _user(uid):
    return FederatedUser(user_id=uid, email=f'u{uid}@x.io', username=f'u{uid}')


def _user_claims(uid):
    """A plain owner JWT — no Coordinator audience, no broker scope."""
    return {'user_id': uid, 'aud': ['shizuha-id']}


def _broker_claims(owner_id, workload_id):
    """A broker/workload token acting for ``owner_id`` (owner from the claim, not body)."""
    return {
        'user_id': 0,
        'aud': ['hive-coordinator'],
        'scope': 'coordinator:model-token',
        'owner_kind': 'user',
        'owner_id': str(owner_id),
        'workload_id': workload_id,
    }


def _call(view, method, path, *, user, claims, data=None):
    req = getattr(factory, method)(path, data, format='json')
    force_authenticate(req, user=user, token=claims)
    return view.as_view()(req, **_path_kwargs(path))


def _path_kwargs(path):
    # extract a trailing uuid entry_id if present
    parts = [p for p in path.split('/') if p]
    if parts and len(parts[-1]) == 36 and '-' in parts[-1]:
        return {'entry_id': parts[-1]}
    return {}


def _mk_entry(owner_id, label, token, **kw):
    pool, _ = TokenPool.objects.get_or_create(
        owner_kind=TokenPool.OwnerKind.USER,
        owner_id=str(owner_id),
        provider=TokenPool.Provider.ANTHROPIC,
    )
    entry = TokenEntry(pool=pool, label=label, **kw)
    entry.set_token(token)
    entry.save()
    return entry


def _openai_jwt(account_id: str, *, expires_in: int = 3600) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({
        'exp': int(time.time()) + expires_in,
        'https://api.openai.com/auth': {
            'chatgpt_account_id': account_id,
            'chatgpt_plan_type': 'pro',
        },
    }).encode()).decode().rstrip('=')
    return f'h.{payload}.s'


@override_settings(**COORD_SETTINGS)
class TokenIsolationTests(TestCase):
    OWNER_A = 100
    OWNER_B = 200

    def setUp(self):
        self.entry_a = _mk_entry(self.OWNER_A, 'a-primary', 'oat-A-secret', priority=1)
        self.entry_b = _mk_entry(self.OWNER_B, 'b-primary', 'oat-B-secret', priority=1)

    # §5.1 — Pool list isolation
    def test_pool_list_isolation(self):
        resp = _call(TokenEntryListCreateView, 'get', '/coordinator/token-entries',
                     user=_user(self.OWNER_B), claims=_user_claims(self.OWNER_B))
        self.assertEqual(resp.status_code, 200)
        ids = {e['entry_id'] for e in resp.data['entries']}
        self.assertIn(str(self.entry_b.id), ids)
        self.assertNotIn(str(self.entry_a.id), ids)  # A is invisible to B

    def test_tombstoned_entry_is_absent_from_coordinator_metadata_api(self):
        from .token_coordinator import tombstone_token_entry

        tombstone_token_entry(self.entry_b, actor='owner-b@example.com')
        resp = _call(TokenEntryListCreateView, 'get', '/coordinator/token-entries',
                     user=_user(self.OWNER_B), claims=_user_claims(self.OWNER_B))

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(
            str(self.entry_b.id),
            {entry['entry_id'] for entry in resp.data['entries']},
        )

    # §5.2 — Raw token gate (broker-only)
    def test_raw_token_gate(self):
        # A user/admin JWT cannot read raw plaintext.
        user_resp = _call(ModelTokenView, 'post', '/coordinator/model-token',
                          user=_user(self.OWNER_A), claims=_user_claims(self.OWNER_A),
                          data={'provider': 'anthropic'})
        self.assertEqual(user_resp.status_code, 404)
        self.assertNotIn('token', getattr(user_resp, 'data', {}))
        # A broker/workload token for A can, and gets a lease.
        broker_resp = _call(ModelTokenView, 'post', '/coordinator/model-token',
                            user=_user(0), claims=_broker_claims(self.OWNER_A, 'broker-A'),
                            data={'provider': 'anthropic'})
        self.assertEqual(broker_resp.status_code, 200)
        self.assertEqual(broker_resp.data['token'], 'oat-A-secret')
        self.assertTrue(broker_resp.data['lease_id'])

    def test_broker_lease_updates_usage_metrics_for_every_provider(self):
        self.entry_a.active_sessions = 3
        self.entry_a.usage_count = 7
        self.entry_a.save(update_fields=['active_sessions', 'usage_count', 'updated_at'])
        entries = {TokenPool.Provider.ANTHROPIC: self.entry_a}
        token_material = {
            TokenPool.Provider.OPENAI: json.dumps({
                'access_token': _openai_jwt('acct-metrics'),
            }),
            TokenPool.Provider.XAI: json.dumps({'access_token': 'xai-metrics'}),
            TokenPool.Provider.GOOGLE: 'google-metrics',
        }
        for provider, token in token_material.items():
            pool = TokenPool.objects.create(
                owner_kind=TokenPool.OwnerKind.USER,
                owner_id=str(self.OWNER_A),
                provider=provider,
            )
            entry = TokenEntry(
                pool=pool,
                label=f'{provider}-metrics',
                active_sessions=3,
                usage_count=7,
            )
            entry.set_token(token)
            entry.save()
            entries[provider] = entry

        for provider, entry in entries.items():
            with self.subTest(provider=provider):
                before = timezone.now()
                response = _call(
                    ModelTokenView,
                    'post',
                    '/coordinator/model-token',
                    user=_user(0),
                    claims=_broker_claims(self.OWNER_A, f'broker-{provider}'),
                    data={'provider': provider},
                )

                self.assertEqual(response.status_code, 200)
                entry.refresh_from_db()
                self.assertEqual(entry.usage_count, 8)
                self.assertGreaterEqual(entry.last_used_at, before)
                self.assertEqual(entry.active_sessions, 3)
                self.assertEqual(entry.leases.count(), 1)

    def test_openai_broker_lease_never_exposes_refresh_or_id_token(self):
        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id=str(self.OWNER_A),
            provider=TokenPool.Provider.OPENAI,
        )
        entry = TokenEntry(pool=pool, label='openai-access-only', priority=1)
        entry.set_token(json.dumps({
            'access_token': _openai_jwt('acct-access-only'),
            'refresh_token': 'single-use-generation',
            'id_token': 'identity-token-must-stay-in-hive',
            'account_id': 'acct-access-only',
            'email': 'managed@example.com',
        }))
        entry.save()

        resp = _call(
            ModelTokenView,
            'post',
            '/coordinator/model-token',
            user=_user(0),
            claims=_broker_claims(self.OWNER_A, 'broker-A'),
            data={'provider': 'openai', 'sticky_key': 'agent:sora'},
        )

        self.assertEqual(resp.status_code, 200)
        served = json.loads(resp.data['token'])
        self.assertEqual(served['auth_mode'], 'chatgptAuthTokens')
        self.assertEqual(served['account_id'], 'acct-access-only')
        self.assertEqual(served['plan_type'], 'pro')
        self.assertNotIn('refresh_token', served)
        self.assertNotIn('id_token', served)
        self.assertNotIn('single-use-generation', resp.data['token'])

    @patch('hive_platform.token_coordinator.refresh_openai_oauth')
    def test_openai_forced_refresh_prefers_current_entry(self, mock_refresh):
        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id=str(self.OWNER_A),
            provider=TokenPool.Provider.OPENAI,
        )
        first = TokenEntry(pool=pool, label='first-openai', priority=1)
        first.set_token(json.dumps({
            'access_token': _openai_jwt('acct-first'),
            'refresh_token': 'refresh-first',
            'account_id': 'acct-first',
        }))
        first.save()
        preferred = TokenEntry(pool=pool, label='preferred-openai', priority=99)
        preferred.set_token(json.dumps({
            'access_token': _openai_jwt('acct-preferred'),
            'refresh_token': 'refresh-preferred',
            'account_id': 'acct-preferred',
        }))
        preferred.save()
        mock_refresh.return_value = {
            'access_token': _openai_jwt('acct-preferred'),
            'refresh_token': 'refresh-preferred-next',
            'account_id': 'acct-preferred',
        }

        resp = _call(
            ModelTokenView,
            'post',
            '/coordinator/model-token',
            user=_user(0),
            claims=_broker_claims(self.OWNER_A, 'broker-A'),
            data={
                'provider': 'openai',
                'force_refresh': True,
                'preferred_entry_id': str(preferred.id),
                'sticky_key': 'agent:sora',
            },
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['entry_id'], str(preferred.id))
        mock_refresh.assert_called_once_with(
            'refresh-preferred',
            timeout_seconds=5,
        )

    @patch('hive_platform.token_coordinator.refresh_openai_oauth')
    def test_openai_broker_parks_revoked_preferred_entry_and_uses_sibling(
        self,
        mock_refresh,
    ):
        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id=str(self.OWNER_A),
            provider=TokenPool.Provider.OPENAI,
        )
        revoked = TokenEntry(pool=pool, label='revoked-openai', priority=1)
        revoked.set_token(json.dumps({
            'access_token': _openai_jwt('acct-revoked'),
            'refresh_token': 'refresh-revoked',
            'account_id': 'acct-revoked',
        }))
        revoked.save()
        healthy = TokenEntry(pool=pool, label='healthy-openai', priority=2)
        healthy.set_token(json.dumps({
            'access_token': _openai_jwt('acct-healthy'),
            'refresh_token': 'refresh-healthy',
            'account_id': 'acct-healthy',
        }))
        healthy.save()
        mock_refresh.side_effect = [
            token_coordinator.OpenAIOAuthRefreshError(
                'invalid_grant',
                status_code=401,
            ),
            {
                'access_token': _openai_jwt('acct-healthy'),
                'refresh_token': 'refresh-healthy-next',
                'account_id': 'acct-healthy',
            },
        ]

        resp = _call(
            ModelTokenView,
            'post',
            '/coordinator/model-token',
            user=_user(0),
            claims=_broker_claims(self.OWNER_A, 'broker-A'),
            data={
                'provider': 'openai',
                'force_refresh': True,
                'preferred_entry_id': str(revoked.id),
            },
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['entry_id'], str(healthy.id))
        revoked.refresh_from_db()
        self.assertFalse(revoked.active)
        self.assertEqual(revoked.last_status, TokenEntry.LastStatus.AUTH_FAILED)

    def _openai_plan_entry(self, pool, label, account_id, plan, priority):
        entry = TokenEntry(pool=pool, label=label, priority=priority)
        entry.set_token(json.dumps({
            'access_token': _openai_jwt(account_id),
            'refresh_token': f'refresh-{account_id}',
            'account_id': account_id,
        }))
        entry.quota_snapshot = {'plan': plan}
        entry.save()
        return entry

    def test_astra_lease_skips_sticky_go_and_uses_pro(self):
        from django.core.cache import cache
        from fleet.models import FleetAgent

        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id=str(self.OWNER_A),
            provider=TokenPool.Provider.OPENAI,
        )
        go = self._openai_plan_entry(pool, 'go-acct', 'acct-go', 'go', priority=5)
        pro = self._openai_plan_entry(pool, 'pro-acct', 'acct-pro', 'pro', priority=0)
        cache.set(f'hive:tokenpool:sticky:openai:agent:ni', str(go.id), timeout=3600)
        FleetAgent.objects.create(agent_username='ni', agent_id='agent-ni', model='gpt-6-astra')

        explicit = _call(
            ModelTokenView,
            'post',
            '/coordinator/model-token',
            user=_user(0),
            claims=_broker_claims(self.OWNER_A, 'broker-astra'),
            data={'provider': 'openai', 'model': 'gpt-6-astra', 'sticky_key': 'agent:ni'},
        )
        self.assertEqual(explicit.status_code, 200)
        self.assertEqual(explicit.data['entry_id'], str(pro.id))

        inferred = _call(
            ModelTokenView,
            'post',
            '/coordinator/model-token',
            user=_user(0),
            claims=_broker_claims(self.OWNER_A, 'broker-astra-2'),
            data={'provider': 'openai', 'sticky_key': 'agent:ni'},
        )
        self.assertEqual(inferred.status_code, 200)
        self.assertEqual(inferred.data['entry_id'], str(pro.id))

    def test_astra_pool_is_dry_when_only_go_and_free_remain(self):
        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id=str(self.OWNER_A),
            provider=TokenPool.Provider.OPENAI,
        )
        self._openai_plan_entry(pool, 'go-only', 'acct-go-only', 'go', priority=0)
        self._openai_plan_entry(pool, 'free-only', 'acct-free-only', 'free', priority=1)
        resp = _call(
            ModelTokenView,
            'post',
            '/coordinator/model-token',
            user=_user(0),
            claims=_broker_claims(self.OWNER_A, 'broker-astra-dry'),
            data={'provider': 'openai', 'model': 'gpt-6-astra', 'sticky_key': 'agent:nami'},
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.data.get('reason'), 'pool_dry')

    # §5.3 — Cross-owner read
    def test_cross_owner_read_404(self):
        resp = _call(TokenEntryDetailView, 'get', f'/coordinator/token-entries/{self.entry_a.id}',
                     user=_user(self.OWNER_B), claims=_user_claims(self.OWNER_B))
        self.assertEqual(resp.status_code, 404)

    # §5.4 — Cross-owner mutate / delete
    def test_cross_owner_mutate_delete_404_no_state_change(self):
        deact = _call(TokenEntryDetailView, 'patch', f'/coordinator/token-entries/{self.entry_a.id}',
                      user=_user(self.OWNER_B), claims=_user_claims(self.OWNER_B),
                      data={'active': False, 'label': 'pwned'})
        self.assertEqual(deact.status_code, 404)
        dele = _call(TokenEntryDetailView, 'delete', f'/coordinator/token-entries/{self.entry_a.id}',
                     user=_user(self.OWNER_B), claims=_user_claims(self.OWNER_B))
        self.assertEqual(dele.status_code, 404)
        # A's entry is untouched.
        self.entry_a.refresh_from_db()
        self.assertTrue(self.entry_a.active)
        self.assertEqual(self.entry_a.label, 'a-primary')

    def test_coordinator_metadata_api_cannot_change_owner_leasing_policy(self):
        response = _call(
            TokenEntryDetailView,
            'patch',
            f'/coordinator/token-entries/{self.entry_a.id}',
            user=_user(self.OWNER_A),
            claims=_user_claims(self.OWNER_A),
            data={'agent_leasing_enabled': False, 'label': 'must-not-change'},
        )

        self.assertEqual(response.status_code, 403)
        self.entry_a.refresh_from_db()
        self.assertTrue(self.entry_a.agent_leasing_enabled)
        self.assertEqual(self.entry_a.label, 'a-primary')

    # §5.5 — Report-status poisoning resistance
    def test_report_status_lease_binding(self):
        duplicate_same_token = _mk_entry(
            self.OWNER_B, 'b-duplicate-a-token', 'oat-A-secret', priority=2,
        )
        # broker-A leases A's entry.
        lease_resp = _call(ModelTokenView, 'post', '/coordinator/model-token',
                          user=_user(0), claims=_broker_claims(self.OWNER_A, 'broker-A'),
                          data={'provider': 'anthropic'})
        lease_id = lease_resp.data['lease_id']

        # (a) a DIFFERENT broker presenting A's lease_id -> 404, no change.
        wrong_broker = _call(ReportStatusView, 'post', '/coordinator/report-status',
                            user=_user(0), claims=_broker_claims(self.OWNER_B, 'broker-B'),
                            data={'lease_id': lease_id, 'entry_id': str(self.entry_a.id),
                                  'action': 'deactivate'})
        self.assertEqual(wrong_broker.status_code, 404)
        self.entry_a.refresh_from_db()
        self.assertTrue(self.entry_a.active)

        # (b) a made-up lease_id -> 404.
        import uuid as _uuid
        bogus = _call(ReportStatusView, 'post', '/coordinator/report-status',
                     user=_user(0), claims=_broker_claims(self.OWNER_A, 'broker-A'),
                     data={'lease_id': str(_uuid.uuid4()), 'entry_id': str(self.entry_a.id)})
        self.assertEqual(bogus.status_code, 404)

        # (c) an EXPIRED lease -> 404.
        expired = TokenLease.objects.create(
            entry=self.entry_a, broker_identity='broker-A',
            owner_kind='user', owner_id=str(self.OWNER_A), provider='anthropic',
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        exp_resp = _call(ReportStatusView, 'post', '/coordinator/report-status',
                        user=_user(0), claims=_broker_claims(self.OWNER_A, 'broker-A'),
                        data={'lease_id': str(expired.id), 'entry_id': str(self.entry_a.id)})
        self.assertEqual(exp_resp.status_code, 404)

        # (d) the rightful broker + valid lease cools the entry; idempotent retry is safe.
        ok1 = _call(ReportStatusView, 'post', '/coordinator/report-status',
                   user=_user(0), claims=_broker_claims(self.OWNER_A, 'broker-A'),
                   data={'lease_id': lease_id, 'entry_id': str(self.entry_a.id), 'action': 'cool'})
        self.assertEqual(ok1.status_code, 200)
        ok2 = _call(ReportStatusView, 'post', '/coordinator/report-status',
                   user=_user(0), claims=_broker_claims(self.OWNER_A, 'broker-A'),
                   data={'lease_id': lease_id, 'entry_id': str(self.entry_a.id), 'action': 'cool'})
        self.assertEqual(ok2.status_code, 200)
        self.entry_a.refresh_from_db()
        self.assertIsNotNone(self.entry_a.cooldown_until)
        self.assertEqual(self.entry_a.last_status, TokenEntry.LastStatus.RATE_LIMITED)
        duplicate_same_token.refresh_from_db()
        self.assertIsNotNone(duplicate_same_token.cooldown_until)
        self.assertEqual(duplicate_same_token.last_status, TokenEntry.LastStatus.RATE_LIMITED)
        self.assertEqual(ok1.data['peer_entries_updated'], 1)

    # §5.6 — Agent enumeration: B's broker cannot pull A's token even guessing provider.
    def test_cross_owner_broker_cannot_read_other_owner(self):
        # broker for B asks for a token — only B's pool is reachable, never A's.
        resp = _call(ModelTokenView, 'post', '/coordinator/model-token',
                     user=_user(0), claims=_broker_claims(self.OWNER_B, 'broker-B'),
                     data={'provider': 'anthropic'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['token'], 'oat-B-secret')  # B's, never A's

    def test_raw_token_skips_undecryptable_entries(self):
        broken = _mk_entry(self.OWNER_A, 'broken-kek-row', 'bad-secret', priority=0)
        broken.wrapped_dek = 'not-a-fernet-token'
        broken.save(update_fields=['wrapped_dek'])

        resp = _call(ModelTokenView, 'post', '/coordinator/model-token',
                     user=_user(0), claims=_broker_claims(self.OWNER_A, 'broker-A'),
                     data={'provider': 'anthropic'})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['token'], 'oat-A-secret')
        broken.refresh_from_db()
        self.assertFalse(broken.active)
        self.assertEqual(broken.last_status, TokenEntry.LastStatus.AUTH_FAILED)

    def test_raw_token_all_undecryptable_returns_404_not_500(self):
        self.entry_a.wrapped_dek = 'not-a-fernet-token'
        self.entry_a.save(update_fields=['wrapped_dek'])

        resp = _call(ModelTokenView, 'post', '/coordinator/model-token',
                     user=_user(0), claims=_broker_claims(self.OWNER_A, 'broker-A'),
                     data={'provider': 'anthropic'})

        self.assertEqual(resp.status_code, 404)
        self.assertNotIn('token', getattr(resp, 'data', {}))

    def test_raw_token_prefers_fresh_measured_claude_capacity(self):
        """Known-good full OAuth capacity outranks an opaque setup token."""
        measured = _mk_entry(
            self.OWNER_A,
            'measured-capacity',
            'oat-measured-secret',
            priority=99,
            quota_snapshot={
                'confidence': 'exact',
                'fetched_at': token_coordinator._quota_now().isoformat(),
                'windows': [
                    {'id': '5h', 'left_percent': 96},
                    {'id': 'weekly', 'left_percent': 54},
                ],
            },
        )
        self.assertEqual(_quota_routing_rank(measured), (0, -5))
        self.assertEqual(_quota_routing_rank(self.entry_a), (1, 0))

        resp = _call(ModelTokenView, 'post', '/coordinator/model-token',
                     user=_user(0), claims=_broker_claims(self.OWNER_A, 'broker-A'),
                     data={'provider': 'anthropic'})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['entry_id'], str(measured.id))
        self.assertEqual(resp.data['token'], 'oat-measured-secret')

    def test_raw_token_places_fresh_exact_exhaustion_after_unknown(self):
        """A fresh zero weekly window must not win merely on priority/load."""
        exhausted = _mk_entry(
            self.OWNER_A,
            'measured-exhausted',
            'oat-exhausted-secret',
            priority=-99,
            quota_snapshot={
                'confidence': 'exact',
                'fetched_at': token_coordinator._quota_now().isoformat(),
                'windows': [
                    {'id': '5h', 'left_percent': 77},
                    {'id': 'weekly', 'left_percent': 0},
                ],
            },
        )
        self.assertEqual(_quota_routing_rank(exhausted), (2, 0))
        self.assertEqual(_quota_routing_rank(self.entry_a), (1, 0))

        resp = _call(ModelTokenView, 'post', '/coordinator/model-token',
                     user=_user(0), claims=_broker_claims(self.OWNER_A, 'broker-A'),
                     data={'provider': 'anthropic'})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['entry_id'], str(self.entry_a.id))
        self.assertNotEqual(resp.data['entry_id'], str(exhausted.id))

    # §5.7 — Namespace boundary (k8s NetworkPolicy denies cross-owner ingress)
    def test_namespace_boundary_network_policy(self):
        class _Claw:
            owner_id = self.OWNER_A
        ns = f'claw-{self.OWNER_A}-deadbeef'
        policy = provisioner.build_owner_isolation_network_policy(ns, _Claw())
        self.assertEqual(policy.spec.policy_types, ['Ingress'])
        # The single ingress peer is THIS namespace only — no cross-owner source.
        peers = policy.spec.ingress[0]._from
        self.assertEqual(len(peers), 1)
        self.assertEqual(
            peers[0].namespace_selector.match_labels,
            {'kubernetes.io/metadata.name': ns},
        )

    # §5.8 — 404 non-enumerability: absent vs not-yours are indistinguishable.
    def test_404_non_enumerability(self):
        import uuid as _uuid
        absent = _call(TokenEntryDetailView, 'get', f'/coordinator/token-entries/{_uuid.uuid4()}',
                      user=_user(self.OWNER_B), claims=_user_claims(self.OWNER_B))
        not_yours = _call(TokenEntryDetailView, 'get', f'/coordinator/token-entries/{self.entry_a.id}',
                         user=_user(self.OWNER_B), claims=_user_claims(self.OWNER_B))
        self.assertEqual(absent.status_code, not_yours.status_code)
        self.assertEqual(absent.data, not_yours.data)  # identical observable shape


class Hive1747QuotaCooldownSweepTests(TestCase):
    """HIVE-1747 — real-caller-sequence regression for the pre-dry self-heal sweep.

    The pre-dry ``recover_stale_cooldown_tokens`` must NOT wipe a provider-confirmed
    quota-exhaustion cooldown that Cortex asked for with a 6-hour duration, ~15
    minutes after it is stamped, when the usage API is unreadable
    (confidence='unsupported'). The fixture drives the REAL caller sequence across
    two top-level cycles — ``model-token lease -> upstream 403 -> report-status(
    action=cool, cooldown_seconds=21600, reason=quota_exhausted) -> model-token
    lease again 20 minutes later`` — and asserts the second lease is REFUSED (pool
    dry), not a re-lease of a provably out-of-credit token. A direct call to
    ``recover_stale_cooldown_tokens`` cannot reproduce this (SCLI-324 doctrine).
    """

    def _xai_unsupported_entry(self, owner_id, label):
        pool, _ = TokenPool.objects.get_or_create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id=str(owner_id),
            provider=TokenPool.Provider.XAI,
        )
        entry = TokenEntry(pool=pool, label=label, priority=0)
        entry.set_token('{"access_token":"xai-unsupported"}')
        entry.active = True
        # Usage API unreadable -> no positive confirmation of exhaustion.
        entry.quota_snapshot = {
            'source': 'grok_xai_usage',
            'confidence': 'unsupported',
            'fetched_at': (timezone.now() - timedelta(hours=1)).isoformat(),
            'windows': [],
        }
        entry.save()
        return entry

    def test_quota_exhausted_cooldown_survives_the_pre_dry_sweep(self):
        entry = self._xai_unsupported_entry(99, 'grok-quota-dead')
        now = timezone.now()

        # Cycle 1: lease succeeds (healthy, no cooldown).
        lease1 = _call(ModelTokenView, 'post', '/coordinator/model-token',
                       user=_user(0), claims=_broker_claims(99, 'broker-xai'),
                       data={'provider': 'xai'})
        self.assertEqual(lease1.status_code, 200)
        lease_id = lease1.data['lease_id']

        # Upstream 403 -> Cortex reports quota_exhausted with a 6h cool + reason.
        report = _call(ReportStatusView, 'post', '/coordinator/report-status',
                       user=_user(0), claims=_broker_claims(99, 'broker-xai'),
                       data={'lease_id': lease_id, 'entry_id': str(entry.id),
                             'action': 'cool', 'cooldown_seconds': 21600,
                             'reason': 'quota_exhausted'})
        self.assertEqual(report.status_code, 200)
        entry.refresh_from_db()
        self.assertEqual(entry.cooldown_reason, 'quota_exhausted')
        self.assertIsNotNone(entry.cooldown_until)
        self.assertEqual(entry.last_status, TokenEntry.LastStatus.RATE_LIMITED)

        # Cycle 2: 20 minutes later the pool is re-probed. The pre-dry sweep runs
        # inside the lease path (no eligible candidates). It must NOT wipe the 6h
        # cooldown (last_rate_limit_at is now 20m old > 15m cutoff) — the second
        # lease must be REFUSED, not a re-lease of a provably out-of-credit token.
        later = now + timedelta(minutes=20)
        with patch('django.utils.timezone.now', return_value=later):
            lease2 = _call(ModelTokenView, 'post', '/coordinator/model-token',
                           user=_user(0), claims=_broker_claims(99, 'broker-xai'),
                           data={'provider': 'xai'})
        self.assertEqual(lease2.status_code, 404)
        self.assertEqual(lease2.data.get('reason'), 'pool_dry')
        entry.refresh_from_db()
        self.assertIsNotNone(entry.cooldown_until)
        self.assertEqual(entry.last_status, TokenEntry.LastStatus.RATE_LIMITED)
        self.assertEqual(entry.cooldown_reason, 'quota_exhausted')
