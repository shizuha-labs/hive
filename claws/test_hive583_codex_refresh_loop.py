"""HIVE-583: tests for the Codex/OpenAI token refresh loop (HA background worker).

Covers:
- ``jwt_expiry_seconds()`` — JWT expiry decoding
- ``_openai_entries_needing_refresh()`` — entry filtering
- ``run_codex_refresh_loop()`` — full loop with dry-run, refresh, and failure paths
- ``codex_refresh_metrics`` — liveness/invariant observability (PLAT-1254)
"""
import json
import time
import base64
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from hive_platform.models import TokenEntry, TokenPool
from .token_coordinator import (
    jwt_expiry_seconds,
    _openai_entries_needing_refresh,
    _xai_entries_needing_refresh,
    run_codex_refresh_loop,
    CODEX_REFRESH_PRE_EXPIRY_SECONDS,
)
from . import codex_refresh_metrics as metrics


def _make_jwt(exp_ts: int) -> str:
    """Create a minimal JWT with the given ``exp`` claim."""
    header = base64.b64encode(json.dumps({'alg': 'HS256'}).encode()).decode().rstrip('=')
    payload = base64.b64encode(json.dumps({'exp': exp_ts}).encode()).decode().rstrip('=')
    sig = base64.b64encode(b'fake-sig').decode().rstrip('=')
    return f'{header}.{payload}.{sig}'


@override_settings(HIVE_TOKEN_COORDINATOR_KEK='unit-test-kek', HIVE_TOKEN_COORDINATOR_KEK_ID='unit-v1')
class JwtExpirySecondsTests(TestCase):
    """Tests for ``jwt_expiry_seconds()``."""

    def test_returns_positive_for_future_expiry(self):
        future = int(time.time()) + 600  # 10 min from now
        token = _make_jwt(future)
        remaining = jwt_expiry_seconds(token)
        self.assertIsNotNone(remaining)
        self.assertGreater(remaining, 590)
        self.assertLessEqual(remaining, 610)

    def test_returns_negative_for_past_expiry(self):
        past = int(time.time()) - 60  # 1 min ago
        token = _make_jwt(past)
        remaining = jwt_expiry_seconds(token)
        self.assertIsNotNone(remaining)
        self.assertLess(remaining, 0)

    def test_returns_none_for_missing_exp(self):
        header = base64.b64encode(json.dumps({'alg': 'HS256'}).encode()).decode().rstrip('=')
        payload = base64.b64encode(json.dumps({'sub': 'no-exp'}).encode()).decode().rstrip('=')
        sig = base64.b64encode(b'fake-sig').decode().rstrip('=')
        token = f'{header}.{payload}.{sig}'
        self.assertIsNone(jwt_expiry_seconds(token))

    def test_returns_none_for_malformed_jwt(self):
        self.assertIsNone(jwt_expiry_seconds('not-a-jwt'))

    def test_returns_none_for_empty_string(self):
        self.assertIsNone(jwt_expiry_seconds(''))


@override_settings(HIVE_TOKEN_COORDINATOR_KEK='unit-test-kek', HIVE_TOKEN_COORDINATOR_KEK_ID='unit-v1')
class OpenaiEntriesNeedingRefreshTests(TestCase):
    """Tests for ``_openai_entries_needing_refresh()``."""

    _entry_counter = 0

    def _entry(self, *, access_token_exp_secs_from_now=600, has_refresh=True, active=True, cooldown=False):
        """Create an OpenAI TokenEntry with a JWT access_token."""
        from django.utils import timezone
        from datetime import timedelta

        type(self)._entry_counter += 1
        c = type(self)._entry_counter
        pool, _ = TokenPool.objects.get_or_create(
            owner_kind=TokenPool.OwnerKind.ORG,
            owner_id=TokenPool.ORG_OWNER_ID,
            provider=TokenPool.Provider.OPENAI,
        )
        exp = int(time.time()) + access_token_exp_secs_from_now
        payload = {
            'access_token': _make_jwt(exp),
            'refresh_token': 'rt-' + str(access_token_exp_secs_from_now) if has_refresh else '',
            'id_token': 'id-' + str(access_token_exp_secs_from_now),
        }
        e = TokenEntry(pool=pool, label=f'test-{c}', priority=0, active=active)
        e.set_token(json.dumps(payload))
        if cooldown:
            e.cooldown_until = timezone.now() + timedelta(seconds=300)
        e.save()
        return e

    def test_yields_entry_near_expiry(self):
        entry = self._entry(access_token_exp_secs_from_now=60)  # 1 min left
        results = list(_openai_entries_needing_refresh())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0].id, entry.id)

    def test_skips_fresh_entry(self):
        self._entry(access_token_exp_secs_from_now=6000)  # 100 min left
        results = list(_openai_entries_needing_refresh())
        self.assertEqual(len(results), 0)

    def test_skips_entry_without_refresh_token(self):
        self._entry(access_token_exp_secs_from_now=60, has_refresh=False)
        results = list(_openai_entries_needing_refresh())
        self.assertEqual(len(results), 0)

    def test_skips_inactive_entry(self):
        self._entry(access_token_exp_secs_from_now=60, active=False)
        results = list(_openai_entries_needing_refresh())
        self.assertEqual(len(results), 0)

    def test_skips_entry_in_cooldown(self):
        self._entry(access_token_exp_secs_from_now=60, cooldown=True)
        results = list(_openai_entries_needing_refresh())
        self.assertEqual(len(results), 0)

    def test_yields_only_near_expiry_among_mixed(self):
        self._entry(access_token_exp_secs_from_now=6000)  # fresh
        near = self._entry(access_token_exp_secs_from_now=60)  # near expiry
        self._entry(access_token_exp_secs_from_now=120, has_refresh=False)  # near but no refresh
        results = list(_openai_entries_needing_refresh())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0].id, near.id)


@override_settings(HIVE_TOKEN_COORDINATOR_KEK='unit-test-kek', HIVE_TOKEN_COORDINATOR_KEK_ID='unit-v1')
class XaiEntriesNeedingRefreshTests(TestCase):
    def _entry(self, *, exp_secs_from_now=60, active=True, cooldown=False):
        pool, _ = TokenPool.objects.get_or_create(
            owner_kind=TokenPool.OwnerKind.ORG,
            owner_id='xai-refresh-loop',
            provider=TokenPool.Provider.XAI,
        )
        entry = TokenEntry(
            pool=pool,
            label=f'xai-{exp_secs_from_now}-{active}-{cooldown}',
            active=active,
        )
        entry.set_token(json.dumps({
            'type': 'xai_oauth',
            'access_token': _make_jwt(int(time.time()) + exp_secs_from_now),
            'refresh_token': f'xai-refresh-{exp_secs_from_now}',
        }))
        if cooldown:
            entry.cooldown_until = timezone.now() + timedelta(hours=1)
            entry.last_status = TokenEntry.LastStatus.RATE_LIMITED
        entry.save()
        return entry

    def test_yields_near_expiry_and_keeps_cooled_credentials_fresh(self):
        near = self._entry(exp_secs_from_now=60)
        cooled = self._entry(exp_secs_from_now=120, cooldown=True)
        self._entry(exp_secs_from_now=7200)
        self._entry(exp_secs_from_now=30, active=False)

        ids = {entry.id for entry, _ in _xai_entries_needing_refresh()}

        self.assertEqual(ids, {near.id, cooled.id})


@override_settings(HIVE_TOKEN_COORDINATOR_KEK='unit-test-kek', HIVE_TOKEN_COORDINATOR_KEK_ID='unit-v1')
class CodexRefreshLeaderLockTests(TestCase):
    """Regression tests for the HIVE-583 HA leader lock critical section."""

    def setUp(self):
        cache.clear()

    @patch('hive_platform.token_coordinator.clear_elapsed_rate_limit_cooldowns', return_value={'cleared': 0, 'candidates': 0})
    @patch('hive_platform.token_coordinator.recover_stale_cooldown_tokens', return_value={'cleared': 0, 'candidates': 0})
    @patch(
        'hive_platform.token_coordinator.recover_expired_dead_tokens',
        return_value={'recovered': 0, 'still_dead': 0, 'candidates': 0},
    )
    @patch('hive_platform.token_coordinator._anthropic_entries_needing_refresh', return_value=[])
    @patch('hive_platform.token_coordinator._xai_entries_needing_refresh', return_value=[])
    @patch('hive_platform.token_coordinator._openai_entries_needing_refresh')
    @patch('django.db.connection.cursor')
    def test_second_worker_cannot_enter_while_first_refresh_pass_is_inside_lock(
        self, mock_cursor, mock_entries, mock_xai, mock_anthropic,
        mock_recover, mock_stale, mock_elapsed,
    ):
        """The session advisory lock remains held while the refresh pass runs.

        HIVE-606: also stub the non-OpenAI selections so this leader-lock
        re-entrancy test doesn't run their provider queries through FakeCursor.
        Recovery/cooldown sweeps (and inactive-quota ORM) are stubbed for the
        same reason — FakeCursor only understands advisory-lock SQL.
        """
        lock_state = {'held': False, 'sql': []}
        nested = {}

        class FakeCursor:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, sql, params=None):
                lock_state['sql'].append(sql)
                if 'pg_try_advisory_lock' in sql:
                    if lock_state['held']:
                        self.result = (False,)
                    else:
                        lock_state['held'] = True
                        self.result = (True,)
                elif 'pg_advisory_unlock' in sql:
                    released = lock_state['held']
                    lock_state['held'] = False
                    self.result = (released,)
                else:
                    raise AssertionError(f'unexpected SQL: {sql}')

            def fetchone(self):
                return self.result

        mock_cursor.side_effect = lambda: FakeCursor()

        def entries(now):
            if 'summary' not in nested:
                nested['summary'] = run_codex_refresh_loop(dry_run=True)
            return []

        mock_entries.side_effect = entries

        outer = run_codex_refresh_loop(dry_run=True)

        self.assertTrue(outer['leader_held'])
        self.assertFalse(nested['summary']['leader_held'])
        self.assertEqual(lock_state['sql'].count('SELECT pg_try_advisory_lock(%s)'), 2)
        self.assertEqual(lock_state['sql'].count('SELECT pg_advisory_unlock(%s)'), 1)
        self.assertFalse(lock_state['held'])


@override_settings(HIVE_TOKEN_COORDINATOR_KEK='unit-test-kek', HIVE_TOKEN_COORDINATOR_KEK_ID='unit-v1')
class RunCodexRefreshLoopTests(TestCase):
    """Tests for ``run_codex_refresh_loop()``."""

    def setUp(self):
        cache.clear()
        release_patcher = patch('hive_platform.token_coordinator._release_leader_lock', return_value=True)
        self.mock_release_leader_lock = release_patcher.start()
        self.addCleanup(release_patcher.stop)
        self.pool, _ = TokenPool.objects.get_or_create(
            owner_kind=TokenPool.OwnerKind.ORG,
            owner_id=TokenPool.ORG_OWNER_ID,
            provider=TokenPool.Provider.OPENAI,
        )

    _entry_counter = 0

    def _entry(self, *, exp_secs_from_now=60, has_refresh=True):
        type(self)._entry_counter += 1
        c = type(self)._entry_counter
        exp = int(time.time()) + exp_secs_from_now
        payload = {
            'access_token': _make_jwt(exp),
            'refresh_token': 'rt-' + str(exp_secs_from_now) if has_refresh else '',
            'id_token': 'id-' + str(exp_secs_from_now),
        }
        e = TokenEntry(pool=self.pool, label=f'test-{c}', priority=0, active=True)
        e.set_token(json.dumps(payload))
        e.save()
        return e

    @patch('hive_platform.token_coordinator._try_acquire_leader_lock')
    def test_non_leader_returns_immediately(self, mock_lock):
        """When pg_try_advisory_xact_lock returns False, the loop returns with leader_held=False."""
        mock_lock.return_value = False
        summary = run_codex_refresh_loop()
        self.assertFalse(summary['leader_held'])
        self.assertEqual(summary['checked'], 0)

    @patch('hive_platform.token_coordinator._try_acquire_leader_lock')
    def test_dry_run_counts_entries(self, mock_lock):
        """Dry-run mode counts entries needing refresh without calling the OAuth endpoint."""
        mock_lock.return_value = True
        self._entry(exp_secs_from_now=60)
        self._entry(exp_secs_from_now=120)
        summary = run_codex_refresh_loop(dry_run=True)
        self.assertTrue(summary['leader_held'])
        self.assertEqual(summary['refreshed'], 2)
        self.assertEqual(summary['failed'], 0)

    @patch('hive_platform.token_coordinator._try_acquire_leader_lock')
    @patch('hive_platform.token_coordinator.refresh_openai_oauth')
    def test_successful_refresh_updates_entry(self, mock_refresh, mock_lock):
        """A successful OAuth refresh persists the new tokens and clears cooldown."""
        mock_lock.return_value = True
        mock_refresh.return_value = {
            'access_token': 'new-access',
            'refresh_token': 'new-refresh',
            'id_token': 'new-id',
        }
        entry = self._entry(exp_secs_from_now=60)
        summary = run_codex_refresh_loop()
        self.assertTrue(summary['leader_held'])
        self.assertEqual(summary['refreshed'], 1)
        self.assertEqual(summary['failed'], 0)
        entry.refresh_from_db()
        stored = json.loads(entry.get_token())
        self.assertEqual(stored['access_token'], 'new-access')
        self.assertEqual(stored['refresh_token'], 'new-refresh')

    @patch('hive_platform.token_coordinator._try_acquire_leader_lock')
    @patch('hive_platform.token_coordinator.refresh_openai_oauth')
    def test_failure_cools_entry(self, mock_refresh, mock_lock):
        """A transient OAuth refresh failure cools the entry and records the failure."""
        from hive_platform.token_coordinator import OpenAIOAuthRefreshError

        mock_lock.return_value = True
        mock_refresh.side_effect = OpenAIOAuthRefreshError('token revoked')
        entry = self._entry(exp_secs_from_now=60)
        summary = run_codex_refresh_loop()
        self.assertTrue(summary['leader_held'])
        self.assertEqual(summary['refreshed'], 0)
        self.assertEqual(summary['failed'], 1)
        self.assertIn('token revoked', summary['errors'])
        entry.refresh_from_db()
        self.assertIsNotNone(entry.cooldown_until)
        self.assertTrue(entry.active)
        self.assertEqual(entry.last_status, TokenEntry.LastStatus.RATE_LIMITED)

    @patch('hive_platform.token_coordinator._try_acquire_leader_lock')
    @patch('hive_platform.token_coordinator.refresh_openai_oauth')
    def test_terminal_refresh_responses_disable_entry_until_exact_row_reauth(
        self,
        mock_refresh,
        mock_lock,
    ):
        from hive_platform.token_coordinator import OpenAIOAuthRefreshError

        mock_lock.return_value = True
        for offset, status_code in enumerate((400, 401, 403)):
            with self.subTest(status_code=status_code):
                mock_refresh.side_effect = OpenAIOAuthRefreshError(
                    'invalid_grant',
                    status_code=status_code,
                    error_code='invalid_grant' if status_code == 400 else '',
                )
                entry = self._entry(exp_secs_from_now=60 + offset)

                summary = run_codex_refresh_loop()

                self.assertEqual(summary['failed'], 1)
                entry.refresh_from_db()
                self.assertFalse(entry.active)
                self.assertEqual(
                    entry.last_status,
                    TokenEntry.LastStatus.AUTH_FAILED,
                )

    @patch('hive_platform.token_coordinator._try_acquire_leader_lock')
    @patch('hive_platform.token_coordinator.refresh_openai_oauth')
    def test_mixed_success_and_failure_records_failed_metric(self, mock_refresh, mock_lock):
        """Partial refresh failures increment the fail-loud failure counter."""
        from hive_platform.token_coordinator import OpenAIOAuthRefreshError

        mock_lock.return_value = True
        mock_refresh.side_effect = [
            {
                'access_token': 'new-access',
                'refresh_token': 'new-refresh',
                'id_token': 'new-id',
            },
            OpenAIOAuthRefreshError('token revoked'),
        ]
        self._entry(exp_secs_from_now=60)
        self._entry(exp_secs_from_now=120)

        summary = run_codex_refresh_loop()

        self.assertTrue(summary['leader_held'])
        self.assertEqual(summary['checked'], 2)
        self.assertEqual(summary['refreshed'], 1)
        self.assertEqual(summary['failed'], 1)
        self.assertIn('token revoked', summary['errors'])
        snap = metrics.snapshot()
        self.assertEqual(snap['leader_held'], 1)
        self.assertEqual(snap['refreshed'], 1)
        self.assertEqual(snap['failed'], 1)
        self.assertEqual(snap['skipped_fresh'], 0)

    @patch('hive_platform.token_coordinator._try_acquire_leader_lock')
    def test_skips_fresh_entries(self, mock_lock):
        """Entries with plenty of time before expiry are skipped."""
        mock_lock.return_value = True
        self._entry(exp_secs_from_now=6000)  # 100 min left
        summary = run_codex_refresh_loop(dry_run=True)
        self.assertTrue(summary['leader_held'])
        self.assertEqual(summary['refreshed'], 0)
        self.assertEqual(summary['skipped_fresh'], 0)  # not even checked
        self.assertEqual(summary['checked'], 0)

    @patch('hive_platform.token_coordinator._try_acquire_leader_lock')
    def test_leader_pass_reasserts_confirmed_xai_cooldown(self, mock_lock):
        mock_lock.return_value = True
        reset_at = timezone.now() + timedelta(days=2)
        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.ORG,
            owner_id='xai-rollout-race',
            provider=TokenPool.Provider.XAI,
        )
        entry = TokenEntry(pool=pool, label='zero-credit', active=True)
        entry.set_token('{"access_token":"xai-zero"}')
        entry.quota_snapshot = {
            'source': 'grok_xai_usage',
            'confidence': 'ok',
            'windows': [{
                'id': 'credits',
                'left_percent': 0.0,
                'resets_at': reset_at.isoformat(),
            }],
        }
        entry.last_status = TokenEntry.LastStatus.OK
        entry.save()

        summary = run_codex_refresh_loop()

        self.assertEqual(summary['xai_cooldowns_checked'], 1)
        self.assertEqual(summary['xai_cooldowns_reasserted'], 1)
        entry.refresh_from_db()
        self.assertGreaterEqual(entry.cooldown_until, reset_at)
        self.assertEqual(entry.last_status, TokenEntry.LastStatus.RATE_LIMITED)

    @patch('hive_platform.token_coordinator._try_acquire_leader_lock')
    @patch('hive_platform.token_coordinator.refresh_xai_oauth')
    def test_leader_proactively_rotates_xai_and_preserves_zero_credit_cooldown(
        self,
        mock_refresh,
        mock_lock,
    ):
        mock_lock.return_value = True
        reset_at = timezone.now() + timedelta(days=1)
        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.ORG,
            owner_id='xai-proactive-refresh',
            provider=TokenPool.Provider.XAI,
        )
        entry = TokenEntry(pool=pool, label='xai-expiring', active=True)
        entry.set_token(json.dumps({
            'type': 'xai_oauth',
            # Seven minutes is inside the background loop's 10-minute safety
            # window but outside the broker helper's historical 5-minute
            # default. This pins the locked recheck to the proactive window.
            'access_token': _make_jwt(int(time.time()) + 7 * 60),
            'refresh_token': 'generation-a',
        }))
        entry.quota_snapshot = {
            'source': 'grok_xai_usage',
            'confidence': 'ok',
            'windows': [{
                'id': 'credits',
                'left_percent': 0,
                'resets_at': reset_at.isoformat(),
            }],
        }
        entry.save()
        mock_refresh.return_value = {
            'access_token': _make_jwt(int(time.time()) + 21600),
            'refresh_token': 'generation-b',
            'expires_in': 21600,
        }

        summary = run_codex_refresh_loop()

        self.assertEqual(summary['xai_refresh_checked'], 1)
        self.assertEqual(summary['xai_refreshed'], 1)
        self.assertEqual(summary['xai_refresh_failed'], 0)
        mock_refresh.assert_called_once()
        entry.refresh_from_db()
        self.assertEqual(json.loads(entry.get_token())['refresh_token'], 'generation-b')
        self.assertEqual(entry.last_status, TokenEntry.LastStatus.RATE_LIMITED)
        self.assertGreaterEqual(entry.cooldown_until, reset_at)

    @patch('hive_platform.token_coordinator._try_acquire_leader_lock')
    @patch('hive_platform.token_coordinator.refresh_xai_oauth')
    def test_leader_marks_terminal_xai_refresh_failure_for_reauth(
        self,
        mock_refresh,
        mock_lock,
    ):
        from hive_platform.token_coordinator import XaiOAuthRefreshError

        mock_lock.return_value = True
        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.ORG,
            owner_id='xai-terminal-refresh',
            provider=TokenPool.Provider.XAI,
        )
        entry = TokenEntry(pool=pool, label='xai-terminal', active=True)
        entry.set_token(json.dumps({
            'type': 'xai_oauth',
            'access_token': _make_jwt(int(time.time()) + 60),
            'refresh_token': 'dead-generation',
        }))
        entry.save()
        mock_refresh.side_effect = XaiOAuthRefreshError(
            'xai oauth refresh failed: HTTP 400 (invalid_grant)',
            status_code=400,
            error_code='invalid_grant',
        )

        summary = run_codex_refresh_loop()

        self.assertEqual(summary['xai_refresh_checked'], 1)
        self.assertEqual(summary['xai_refreshed'], 0)
        self.assertEqual(summary['xai_refresh_failed'], 1)
        entry.refresh_from_db()
        self.assertFalse(entry.active)
        self.assertEqual(entry.last_status, TokenEntry.LastStatus.AUTH_FAILED)


@override_settings(HIVE_TOKEN_COORDINATOR_KEK='unit-test-kek', HIVE_TOKEN_COORDINATOR_KEK_ID='unit-v1')
class CodexRefreshMetricsTests(TestCase):
    """Tests for the codex refresh liveness metrics (PLAT-1254)."""

    METRICS_URL = '/api/v1/internal/codex/refresh/metrics/'

    def setUp(self):
        cache.clear()

    def test_record_and_snapshot(self):
        metrics.record('leader_held')
        metrics.record('refreshed')
        snap = metrics.snapshot()
        self.assertEqual(snap['leader_held'], 1)
        self.assertEqual(snap['refreshed'], 1)
        self.assertEqual(snap['failed'], 0)
        self.assertEqual(snap['skipped_fresh'], 0)

    def test_record_is_exhaustive(self):
        for o in metrics.OUTCOMES:
            metrics.record(o)
        snap = metrics.snapshot()
        for o in metrics.OUTCOMES:
            self.assertEqual(snap[o], 1)

    def test_to_prometheus_format(self):
        metrics.record('leader_held')
        metrics.record('refreshed')
        output = metrics.to_prometheus()
        self.assertIn('hive_codex_refresh_total{outcome="leader_held"} 1', output)
        self.assertIn('hive_codex_refresh_total{outcome="refreshed"} 1', output)
        self.assertIn('hive_codex_refresh_total{outcome="failed"} 0', output)
        self.assertIn('# HELP hive_codex_refresh_total', output)
        self.assertIn('# TYPE hive_codex_refresh_total counter', output)

    def test_metrics_endpoint_renders_prometheus(self):
        from rest_framework.test import APIClient
        client = APIClient()
        metrics.record('leader_held')
        resp = client.get(self.METRICS_URL)
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn('hive_codex_refresh_total{outcome="leader_held"} 1', body)

    def test_failure_counter_has_prometheus_alert_route(self):
        import yaml
        from pathlib import Path

        rule = yaml.safe_load(Path('k8s/hive-codex-refresh-alerts.yaml').read_text())
        alerts = {r['alert']: r for group in rule['spec']['groups'] for r in group['rules'] if 'alert' in r}
        failure_alert = alerts['HiveCodexRefreshLoopFailures']
        self.assertIn('hive_codex_refresh_total{outcome="failed"}', failure_alert['expr'])
        self.assertEqual(failure_alert['labels']['owner_group'], 'engineering')
        self.assertIn('https://pulse.shizuha?task=HIVE-583', failure_alert['annotations']['description'])


class CodexRefreshDeployPathTests(TestCase):
    """Static deploy-path regressions for the live HIVE-583 worker wiring."""

    def test_live_backend_manifest_does_not_run_codex_refresh_sidecar(self):
        """API pod HA: codex-refresh must not share readiness with hive."""
        import yaml
        from pathlib import Path

        doc = yaml.safe_load(Path('k8s/hive-backend-live.yaml').read_text())
        self.assertEqual(doc['kind'], 'Deployment')
        self.assertEqual(doc['metadata']['name'], 'hive')
        self.assertEqual(doc['metadata']['namespace'], 'shizuha-hive')
        containers = {c['name']: c for c in doc['spec']['template']['spec']['containers']}
        self.assertIn('hive', containers)
        self.assertNotIn('codex-refresh', containers)

        separate = yaml.safe_load(Path('k8s/hive-codex-refresh-deploy.yaml').read_text())
        self.assertEqual(separate['kind'], 'Deployment')
        self.assertEqual(separate['metadata']['name'], 'hive-codex-refresh')
        # Must not use app=hive (Service endpoint risk) but MUST carry
        # app.kubernetes.io/name=hive so platform-db-primary-ingress admits it.
        pod_labels = separate['spec']['template']['metadata']['labels']
        self.assertEqual(pod_labels.get('app'), 'hive-codex-refresh')
        self.assertEqual(pod_labels.get('app.kubernetes.io/name'), 'hive')
        self.assertNotEqual(pod_labels.get('app.kubernetes.io/component'), 'backend')
        refresh = {
            c['name']: c for c in separate['spec']['template']['spec']['containers']
        }['codex-refresh']
        self.assertEqual(
            refresh['command'],
            [
                'python', 'manage.py', 'codex_refresh_loop', '--loop', '--interval', '300',
                '--quota-stale-after', '600',
            ],
        )
        refresh_env = {e['name']: e for e in refresh['env']}
        for name in (
            'HIVE_TOKEN_COORDINATOR_KEK',
            'HIVE_TOKEN_COORDINATOR_KEK_ID',
            'DB_HOST',
            'DB_NAME',
            'DB_USER',
            'DB_PASSWORD',
            'CACHE_REDIS_URL',
        ):
            self.assertIn(name, refresh_env)
        # Separate Deploy pins s1 (amd64 workstation) so hostPath can expose the
        # live Antigravity quota file for subscription auto-refresh; also keeps
        # the often-amd64 image off GB10 readiness paths.
        ns = separate['spec']['template']['spec'].get('nodeSelector') or {}
        self.assertEqual(ns.get('kubernetes.io/hostname'), 's1')
        # hostPath /tmp -> /tmp/shizuha-host for shizuha-agy-quota.json
        mounts = separate['spec']['template']['spec']['containers'][0].get('volumeMounts') or []
        self.assertTrue(
            any(m.get('mountPath') == '/tmp/shizuha-host' for m in mounts),
            msg='codex-refresh must mount host tmp for Antigravity quota file',
        )

    def test_deploy_workflow_rolls_codex_refresh_with_hive_image(self):
        from pathlib import Path

        workflow = "\n".join(Path(path).read_text() for path in (
            '.forgejo/workflows/deploy.yml',
            '.forgejo/scripts/ring-release-fleet.sh',
        ))
        self.assertIn('hive-codex-refresh-deploy-patch.json', workflow)
        self.assertIn('hive-codex-refresh-deploy.yaml', workflow)
        self.assertIn('deploy/hive-codex-refresh', workflow)
        self.assertIn('codex-refresh quota freshness missing', workflow)
        self.assertIn('codex-refresh image mismatch', workflow)
        self.assertIn('codex-refresh worker missing', workflow)
        self.assertIn('codex-refresh reappeared as hive API sidecar', workflow)
        self.assertIn('CACHE_REDIS_URL', workflow)
        self.assertIn('def remove_retired_env(container):', workflow)
        self.assertIn('remove_retired_env(container)', workflow)
        self.assertIn('retired credential environment survived deployment reconciliation', workflow)
        self.assertIn('retired credential references remain:', workflow)
        self.assertIn('kubectl get deploy,cronjob -n shizuha-hive -o json', workflow)

    def test_deploy_workflow_installs_all_checked_in_codex_refresh_alerts(self):
        import yaml
        from pathlib import Path

        checked_in = yaml.safe_load(Path('k8s/hive-codex-refresh-alerts.yaml').read_text())
        expected_alerts = {
            rule['alert']
            for group in checked_in['spec']['groups']
            for rule in group['rules']
            if 'alert' in rule
        }
        workflow = "\n".join(Path(path).read_text() for path in (
            '.forgejo/workflows/deploy.yml',
            '.forgejo/scripts/ring-release-fleet.sh',
        ))
        for alert in expected_alerts:
            self.assertIn(alert, workflow)
        self.assertIn('hive_codex_refresh_total{outcome="failed"}', workflow)
