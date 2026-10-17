from datetime import timedelta
import importlib
import json

from django.apps import apps
from django.db.migrations.state import ProjectState
from django.test import TestCase, override_settings
from django.utils import timezone

from hive_platform.models import TokenEntry, TokenPool
from fleet.models import FleetAgent
from fleet.runtime_lane import RuntimeLaneError, resolve_runtime_lane


@override_settings(
    HIVE_TOKEN_COORDINATOR_KEK='grok-capacity-test-kek',
    HIVE_TOKEN_COORDINATOR_KEK_ID='grok-capacity-v1',
    HIVE_FLEET_OWNER_USER_ID='42',
    HIVE_INTERNAL_AUTH_TOKEN='metrics-test-token',
)
class GrokCapacityFallbackTests(TestCase):
    def _grok_agent(self, username, *, status=FleetAgent.Status.RUNNING):
        return FleetAgent.objects.create(
            agent_username=username,
            status=status,
            model='cortex/grok-4.5',
            mcp_config={'execution_method': 'grok_build'},
        )

    def _xai_entry(self, *, left, reset_at, leasing=True):
        pool, _ = TokenPool.objects.get_or_create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id='42',
            provider=TokenPool.Provider.XAI,
        )
        entry = TokenEntry(
            pool=pool,
            label=f'xai-{left}-{TokenEntry.objects.count()}',
            agent_leasing_enabled=leasing,
        )
        entry.set_token(json.dumps({'access_token': f'xai-{TokenEntry.objects.count()}'}))
        entry.quota_snapshot = {
            'source': 'grok_xai_usage',
            'confidence': 'ok',
            'fetched_at': timezone.now().isoformat(),
            'windows': [{
                'id': 'credits',
                'left_percent': left,
                'resets_at': reset_at.isoformat(),
            }],
        }
        entry.save()
        return entry

    def _xai_auth_error_entry(self):
        pool, _ = TokenPool.objects.get_or_create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id='42',
            provider=TokenPool.Provider.XAI,
        )
        entry = TokenEntry(pool=pool, label='xai-auth-error')
        entry.set_token(json.dumps({'access_token': 'xai-auth-error'}))
        entry.quota_snapshot = {
            'source': 'grok_xai_usage',
            'confidence': 'error',
            'fetched_at': timezone.now().isoformat(),
            'detail': 'oauth_refresh_failed:XaiOAuthRefreshError',
            'windows': [],
        }
        entry.save()
        self.assertEqual(entry.quota_snapshot['detail'], 'oauth_refresh_failed')
        return entry

    def test_runtime_lane_issues_no_fallback_tail_for_any_lane(self):
        """Operator 2026-08-04: an agent is pinned to one backend, full stop.

        This inverts the 2026-08-01 exception that gave Grok Build a
        `grok -> cortex/DeepSeek-V4-Flash` tail. That chain made the Agents page
        show `grok-4.5 pinned` while the agent actually ran DeepSeek, made every
        legitimate failover look like `runtime_config_drift`, split one agent's
        traffic across two models in Cortex attribution, and hid a failing
        primary instead of surfacing it.
        """
        lane = resolve_runtime_lane({}, {
            'model': 'cortex/grok-4.5',
            'execution_method': 'grok_build',
        })
        self.assertEqual(
            lane['model_fallbacks'], [],
            'Grok Build must no longer receive a managed DeepSeek tail',
        )

        deepseek = resolve_runtime_lane({}, {
            'model': 'cortex/DeepSeek-V4-Flash',
            'execution_method': 'shizuha',
        })
        self.assertEqual(deepseek['model_fallbacks'], [])

    def test_arbitrary_caller_supplied_fallback_chain_remains_rejected(self):
        with self.assertRaises(RuntimeLaneError) as raised:
            resolve_runtime_lane({}, {
                'model': 'cortex/grok-4.5',
                'execution_method': 'grok_build',
                'model_fallbacks': [
                    {'method': 'codex_app_server', 'model': 'gpt-5.6-sol'},
                ],
            })
        self.assertEqual(raised.exception.code, 'MODEL_FALLBACKS_DISABLED')

    def test_grok_fleet_agent_persists_no_fallback_chain(self):
        agent = self._grok_agent('goro-test')
        self.assertEqual(agent.model_fallbacks, [])

    def test_saving_an_agent_scrubs_a_pre_existing_chain(self):
        """FleetAgent.save() is the chokepoint — a stale chain cannot survive it.

        Migration 0044 clears the rows written by 0041/0042, but a row could
        also be re-introduced by an older writer; normalising on save means the
        chain cannot come back through admin, reconcilers, daemon sync or REST.
        """
        agent = self._grok_agent('goro-stale')
        FleetAgent.objects.filter(pk=agent.pk).update(model_fallbacks=[
            {'method': 'grok_build', 'model': 'cortex/grok-4.5'},
            {'method': 'shizuha', 'model': 'cortex/DeepSeek-V4-Flash'},
        ])
        agent.refresh_from_db()
        self.assertNotEqual(agent.model_fallbacks, [])

        agent.save()
        agent.refresh_from_db()
        self.assertEqual(
            agent.model_fallbacks, [],
            'save() must scrub any chain that reached the row another way',
        )

    def test_metrics_cover_every_enabled_grok_agent_and_ignore_reserved_tokens(self):
        self._grok_agent('goro')
        self._grok_agent('hana')
        self._grok_agent('nagi', status=FleetAgent.Status.HIBERNATED)
        self._grok_agent('stopped-grok', status=FleetAgent.Status.STOPPED)
        reset_at = timezone.now() + timedelta(days=3)
        self._xai_entry(left=0.0, reset_at=reset_at, leasing=True)
        self._xai_entry(left=75.0, reset_at=reset_at, leasing=False)

        from fleet.grok_provider_metrics import to_prometheus
        text = to_prometheus()

        for username in ('goro', 'hana', 'nagi'):
            labels = f'agent="{username}",backend="grok_build"'
            self.assertIn(f'shizuha_agent_provider_healthy{{{labels}}} 0', text)
            self.assertIn(f'shizuha_agent_capacity_unavailable{{{labels}}} 1', text)
        self.assertNotIn('agent="stopped-grok"', text)

    def test_followup_migration_reasserts_fallback_for_running_and_hibernated_agents(self):
        agents = [
            self._grok_agent('goro', status=FleetAgent.Status.HIBERNATED),
            self._grok_agent('hana'),
            self._grok_agent('nagi'),
        ]
        FleetAgent.objects.filter(pk__in=[agent.pk for agent in agents]).update(
            model_fallbacks=[], pending_config={},
        )
        migration = importlib.import_module(
            'fleet.migrations.0041_reassert_managed_grok_deepseek_fallback'
        )
        migration_apps = ProjectState.from_apps(apps).apps

        migration.reassert_managed_grok_deepseek_fallback(migration_apps, None)

        for agent in agents:
            agent.refresh_from_db()
            self.assertEqual(agent.model_fallbacks, [
                {'method': 'grok_build', 'model': 'cortex/grok-4.5'},
                {'method': 'shizuha', 'model': 'cortex/DeepSeek-V4-Flash'},
            ])
            self.assertEqual(
                agent.pending_config['model_fallbacks'], agent.model_fallbacks,
            )

    def test_lane_wide_migration_repairs_grok_agents_outside_initial_name_allowlist(self):
        kumo = self._grok_agent('kumo')
        sleeping = self._grok_agent('future-grok', status=FleetAgent.Status.HIBERNATED)
        FleetAgent.objects.filter(pk__in=[kumo.pk, sleeping.pk]).update(
            model_fallbacks=[], pending_config={},
        )
        migration = importlib.import_module(
            'fleet.migrations.0042_backfill_all_managed_grok_fallbacks'
        )
        migration_apps = ProjectState.from_apps(apps).apps

        migration.backfill_all_managed_grok_fallbacks(migration_apps, None)

        for agent in (kumo, sleeping):
            agent.refresh_from_db()
            self.assertEqual(agent.model_fallbacks, [
                {'method': 'grok_build', 'model': 'cortex/grok-4.5'},
                {'method': 'shizuha', 'model': 'cortex/DeepSeek-V4-Flash'},
            ])
            self.assertEqual(
                agent.pending_config['model_fallbacks'], agent.model_fallbacks,
            )

    def test_cooldown_repair_migration_persists_confirmed_resets_only(self):
        now = timezone.now()
        zero = self._xai_entry(
            left=0.0, reset_at=now + timedelta(days=3), leasing=True,
        )
        positive = self._xai_entry(
            left=10.0, reset_at=now + timedelta(days=3), leasing=True,
        )
        reserved = self._xai_entry(
            left=0.0, reset_at=now + timedelta(days=3), leasing=False,
        )
        migration = importlib.import_module(
            'claws.migrations.0034_preserve_confirmed_xai_credit_cooldowns'
        )
        migration_apps = ProjectState.from_apps(apps).apps

        migration.preserve_confirmed_xai_credit_cooldowns(migration_apps, None)

        zero.refresh_from_db()
        positive.refresh_from_db()
        reserved.refresh_from_db()
        self.assertEqual(zero.last_status, TokenEntry.LastStatus.RATE_LIMITED)
        self.assertIsNotNone(zero.cooldown_until)
        self.assertIsNone(positive.cooldown_until)
        self.assertIsNone(reserved.cooldown_until)

    def test_followup_migration_reasserts_cooldown_after_stale_writer(self):
        reset_at = timezone.now() + timedelta(days=3)
        zero = self._xai_entry(left=0.0, reset_at=reset_at, leasing=True)
        TokenEntry.objects.filter(pk=zero.pk).update(
            cooldown_until=None,
            last_status=TokenEntry.LastStatus.OK,
        )
        migration = importlib.import_module(
            'claws.migrations.0035_reassert_confirmed_xai_credit_cooldowns'
        )
        migration_apps = ProjectState.from_apps(apps).apps

        migration.reassert_confirmed_xai_credit_cooldowns(migration_apps, None)

        zero.refresh_from_db()
        self.assertGreaterEqual(zero.cooldown_until, reset_at)
        self.assertEqual(zero.last_status, TokenEntry.LastStatus.RATE_LIMITED)

    def test_metrics_recover_when_a_leasable_xai_account_has_capacity(self):
        self._grok_agent('hana')
        reset_at = timezone.now() + timedelta(days=3)
        self._xai_entry(left=0.0, reset_at=reset_at, leasing=True)
        self._xai_entry(left=25.0, reset_at=reset_at, leasing=True)

        from fleet.grok_provider_metrics import to_prometheus
        text = to_prometheus()

        labels = 'agent="hana",backend="grok_build"'
        self.assertIn(f'shizuha_agent_provider_healthy{{{labels}}} 1', text)
        self.assertIn(f'shizuha_agent_capacity_unavailable{{{labels}}} 0', text)

    def test_metrics_fail_closed_when_only_xai_account_has_auth_failure(self):
        self._grok_agent('hana')
        self._xai_auth_error_entry()

        from fleet.grok_provider_metrics import to_prometheus
        text = to_prometheus()

        labels = 'agent="hana",backend="grok_build"'
        self.assertIn(f'shizuha_agent_provider_healthy{{{labels}}} 0', text)
        self.assertIn(f'shizuha_agent_capacity_unavailable{{{labels}}} 1', text)

    def test_quota_refresh_event_sets_cooldown_to_confirmed_reset(self):
        from hive_platform.quota_fetchers import persist_quota_snapshot

        reset_at = timezone.now() + timedelta(days=3)
        entry = self._xai_entry(left=50.0, reset_at=reset_at, leasing=True)
        persist_quota_snapshot(entry, {
            'source': 'grok_xai_usage',
            'confidence': 'ok',
            'fetched_at': timezone.now().isoformat(),
            'windows': [{
                'id': 'credits',
                'left_percent': 0.0,
                'resets_at': reset_at.isoformat(),
            }],
        })

        entry.refresh_from_db()
        self.assertGreaterEqual(entry.cooldown_until, reset_at)
        self.assertEqual(entry.last_status, TokenEntry.LastStatus.RATE_LIMITED)

    def test_existing_internal_metrics_scrape_exports_grok_series(self):
        self._grok_agent('nagi')

        response = self.client.get(
            '/api/v1/internal/connect/delivery/metrics/',
            HTTP_AUTHORIZATION='Bearer metrics-test-token',
        )

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn(
            'shizuha_agent_provider_healthy{agent="nagi",backend="grok_build"}',
            body,
        )
        self.assertIn(
            'shizuha_agent_capacity_unavailable{agent="nagi",backend="grok_build"}',
            body,
        )

    def test_data_migration_backfills_named_live_grok_agents_and_pending_replay(self):
        agents = [self._grok_agent(username) for username in ('goro', 'hana', 'nagi')]
        FleetAgent.objects.filter(pk__in=[agent.pk for agent in agents]).update(
            model_fallbacks=[], pending_config={},
        )

        migration = importlib.import_module(
            'fleet.migrations.0040_enable_managed_grok_deepseek_fallback'
        )
        # Run against historical model classes, as Django does during migrate;
        # the current FleetAgent.save() intentionally derives the managed chain.
        migration_apps = ProjectState.from_apps(apps).apps
        migration.enable_managed_grok_deepseek_fallback(migration_apps, None)

        for agent in agents:
            agent.refresh_from_db()
            self.assertEqual(
                agent.pending_config['model_fallbacks'],
                agent.model_fallbacks,
            )
            self.assertEqual(
                agent.model_fallbacks[-1],
                {'method': 'shizuha', 'model': 'cortex/DeepSeek-V4-Flash'},
            )

        migration.disable_managed_grok_deepseek_fallback(migration_apps, None)
        for agent in agents:
            agent.refresh_from_db()
            self.assertEqual(agent.model_fallbacks, [])
            self.assertEqual(agent.pending_config['model_fallbacks'], [])
