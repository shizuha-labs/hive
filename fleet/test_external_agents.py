import time
from types import SimpleNamespace

from django.test import SimpleTestCase

from fleet.external_agents import telemetry_fields
from fleet.views import (
    _agents_index_cache_suffix,
    _apply_provider_unavailable_health,
    _compute_agent_health_summary,
)


class AgentIndexCacheSuffixTests(SimpleTestCase):
    def _request(self, *, user_id, is_staff):
        return SimpleNamespace(user=SimpleNamespace(id=user_id, is_staff=is_staff))

    def test_staff_global_inventory_uses_shared_fleet_cache_scope(self):
        suffix = _agents_index_cache_suffix(
            self._request(user_id=10, is_staff=True),
            include_external=True,
            owner_only=False,
        )

        self.assertEqual(suffix, 'ext1:ownerOnly0:fleet:orgall')

    def test_staff_owner_scoped_inventory_includes_owner_id(self):
        first = _agents_index_cache_suffix(
            self._request(user_id=10, is_staff=True),
            include_external=True,
            owner_only=True,
        )
        second = _agents_index_cache_suffix(
            self._request(user_id=20, is_staff=True),
            include_external=True,
            owner_only=True,
        )

        self.assertEqual(first, 'ext1:ownerOnly1:owner10:orgall')
        self.assertEqual(second, 'ext1:ownerOnly1:owner20:orgall')
        self.assertNotEqual(first, second)

    def test_nonstaff_inventory_is_always_owner_scoped(self):
        suffix = _agents_index_cache_suffix(
            self._request(user_id=30, is_staff=False),
            include_external=True,
            owner_only=False,
        )

        self.assertEqual(suffix, 'ext1:ownerOnly0:owner30:orgall')


class TelemetryFieldsTests(SimpleTestCase):
    def test_maps_server_receipt_and_runtime_activity_to_card_timestamps(self):
        fields = telemetry_fields(
            {
                'ts': 1_700_000_123_456,
                'received_at': 1_700_000_000,
                'activity': {'last_activity_ms_ago': 12_500, 'state': 'working'},
            },
            configured={'last_active_at': '2020-01-01T00:00:00Z'},
        )

        self.assertEqual(fields['last_telemetry_at'], '2023-11-14T22:13:20Z')
        self.assertEqual(fields['last_active_at'], '2023-11-14T22:13:07.500000Z')
        self.assertEqual(fields['activity_state'], 'working')

    def test_maps_fine_activity_phase_fields(self):
        fields = telemetry_fields({
            'received_at': 1_700_000_000,
            'activity': {
                'state': 'working',
                'phase': 'tool',
                'phase_for_ms': 4200,
                'tool_name': 'bash',
                'tool_count': 3,
                'seconds_24h': {'idle': 10, 'thinking': 20, 'responding': 5, 'tool': 8},
            },
        })
        self.assertEqual(fields['activity_phase'], 'tool')
        self.assertEqual(fields['activity_phase_for_ms'], 4200)
        self.assertEqual(fields['activity_tool_name'], 'bash')
        self.assertEqual(fields['activity_tool_count'], 3)
        self.assertEqual(fields['activity_seconds_24h']['tool'], 8)

    def test_does_not_regress_newer_configured_activity_timestamp(self):
        fields = telemetry_fields(
            {
                'received_at': 1_700_000_000,
                'activity': {'last_activity_ms_ago': 60_000},
            },
            configured={'last_active_at': '2023-11-14T22:14:00Z'},
        )

        self.assertNotIn('last_active_at', fields)
        self.assertEqual(fields['last_telemetry_at'], '2023-11-14T22:13:20Z')

    def test_health_snapshot_age_uses_live_telemetry_receipt(self):
        fields = telemetry_fields({'received_at': int(time.time())})

        summary = _compute_agent_health_summary(fields)

        self.assertLessEqual(summary['last_snapshot_age_s'], 1)

    def test_health_summary_never_reports_ok_for_explicitly_unhealthy_runtime(self):
        fields = telemetry_fields({
            'received_at': int(time.time()),
            'health': {
                'ok': False,
                'provider_unavailable': False,
                'consecutive_error_turns': 1,
                'recent_errors': ['model_not_found'],
            },
        })

        summary = _compute_agent_health_summary(fields)

        self.assertFalse(summary['ok'])
        self.assertEqual(summary['highest_severity'], 'error')
        self.assertIn('runtime_unhealthy', summary['issue_types'])
        self.assertIn('runtime', summary['blocking_services'])

    def test_provider_unavailable_does_not_duplicate_generic_runtime_issue(self):
        fields = telemetry_fields({
            'received_at': int(time.time()),
            'health': {
                'ok': False,
                'provider_unavailable': True,
                'consecutive_error_turns': 1,
                'recent_errors': ['provider unavailable'],
            },
        })

        summary = _compute_agent_health_summary(fields)

        self.assertFalse(summary['ok'])
        self.assertIn('provider_unavailable_or_quota', summary['issue_types'])
        self.assertNotIn('runtime_unhealthy', summary['issue_types'])

    def test_preserves_provider_unavailable_health_fields(self):
        fields = telemetry_fields({
            'runtime': {'model': 'claude-sonnet-4-6', 'provider': 'anthropic', 'harness': 'claude-bridge'},
            'health': {
                'ok': False,
                'token_pool_unavailable': True,
                'token_pool_unavailable_since': 1782647000000,
                'provider_unavailable': True,
                'consecutive_error_turns': 0,
            },
        })

        self.assertEqual(fields['model'], 'claude-sonnet-4-6')
        self.assertFalse(fields['health_ok'])
        self.assertTrue(fields['health_token_pool_unavailable'])
        self.assertEqual(fields['health_token_pool_unavailable_since'], 1782647000000)
        self.assertTrue(fields['provider_unavailable'])
        self.assertTrue(fields['has_telemetry'])

    def test_prefers_hive_configured_model_over_stale_process_telemetry(self):
        """After Agents UI convert, desired config must win over stale bridge telemetry."""
        fields = telemetry_fields(
            {
                'runtime': {'model': 'gpt-5.5', 'provider': 'openai', 'harness': 'codex-bridge'},
                'health': {'ok': True},
            },
            configured={
                'execution_method': 'shizuha',
                'runtime_harness': 'Shizuha CLI',
                'model_provider': 'Cortex',
                'model': 'cortex/grok-4.5',
                'effective_model': 'cortex/grok-4.5',
                'model_source': 'runtime_daemon',
            },
        )
        self.assertEqual(fields['model'], 'cortex/grok-4.5')
        self.assertEqual(fields['runtime_harness'], 'Shizuha CLI')
        self.assertEqual(fields['model_provider'], 'Cortex')
        self.assertEqual(fields['running_model'], 'gpt-5.5')
        self.assertEqual(fields['running_runtime_harness'], 'codex-bridge')
        self.assertTrue(fields['runtime_config_drift'])
        self.assertFalse(fields['health_ok'])
        self.assertEqual(fields['health_reason'], 'runtime_config_drift')
        self.assertEqual(fields['availability_status'], 'degraded')

        summary = _compute_agent_health_summary(fields)
        self.assertFalse(summary['ok'])
        self.assertEqual(summary['highest_severity'], 'error')
        self.assertTrue(summary['config_drift'])
        self.assertIn('runtime_config_drift', summary['issue_types'])
        self.assertNotIn('runtime_unhealthy', summary['issue_types'])

    def test_matching_configured_and_running_model_remains_healthy(self):
        fields = telemetry_fields(
            {
                'runtime': {
                    'model': 'grok-4.5',
                    'provider': 'cortex',
                    'harness': 'shizuha-gateway',
                },
                'health': {'ok': True},
            },
            configured={
                'execution_method': 'grok_build',
                'model': 'cortex/grok-4.5',
                'effective_model': 'cortex/grok-4.5',
            },
        )

        self.assertFalse(fields['runtime_config_drift'])
        self.assertTrue(fields['health_ok'])
        self.assertNotIn('health_reason', fields)
        self.assertTrue(_compute_agent_health_summary(fields)['ok'])

    def test_maps_native_heartbeat_needs_help_from_connect_telemetry(self):
        fields = telemetry_fields({
            'received_at': 1_784_241_500,
            'heartbeat': {
                'outcome': 'needs_help',
                'reason': '2 ready task(s) with no progress for 4 heartbeat(s)',
                'observed_at': '2026-07-16T22:31:49.958Z',
                'ready_task_count': 2,
                'blocked_task_count': 20,
                'consecutive_ready_no_progress_heartbeats': 4,
            },
        })

        self.assertTrue(fields['needs_help'])
        self.assertEqual(fields['heartbeat_outcome'], 'needs_help')
        self.assertEqual(fields['heartbeat_ready_task_count'], 2)
        self.assertEqual(fields['heartbeat_consecutive_ready_no_progress'], 4)
        self.assertIn('4 heartbeat(s)', fields['needs_help_reason'])

        summary = _compute_agent_health_summary(fields)
        self.assertFalse(summary['ok'])
        # 'attention', not 'error' (operator 2026-08-05): health_ok is TRUE here
        # — the runtime is serving fine and the agent is telling us its WORK is
        # stuck. Rendering that in the same red as a broken runtime made a
        # healthy fleet read as failing. Still surfaced, still not 'ok'.
        self.assertEqual(summary['highest_severity'], 'attention')
        self.assertIn('heartbeat_needs_help', summary['issue_types'])
        issue = next(
            issue for issue in summary['issues']
            if issue['type'] == 'heartbeat_needs_help'
        )
        # 'work', not 'runtime': the runtime is healthy; what is stuck is the
        # agent's progress on its queue. The service label is what the operator
        # reads on the chip tooltip, so it must name the right subsystem.
        self.assertEqual(issue['service'], 'work')
        self.assertIn('4 heartbeat(s)', issue['detail'])

    def test_later_healthy_telemetry_clears_configured_needs_help(self):
        fields = telemetry_fields(
            {
                'heartbeat': {
                    'outcome': 'worked_task',
                    'reason': '3 progress event(s) recorded',
                    'observed_at': '2026-07-16T22:45:00Z',
                },
            },
            configured={
                'needs_help': True,
                'needs_help_reason': 'stuck',
                'heartbeat_outcome': 'needs_help',
                'heartbeat_observed_at': '2026-07-16T22:40:00Z',
            },
        )

        self.assertFalse(fields['needs_help'])
        self.assertEqual(fields['needs_help_reason'], '')
        self.assertEqual(fields['heartbeat_outcome'], 'worked_task')
        self.assertTrue(_compute_agent_health_summary(fields)['ok'])

    def test_older_healthy_telemetry_does_not_clear_newer_daemon_state(self):
        fields = telemetry_fields(
            {
                'heartbeat': {
                    'outcome': 'worked_task',
                    'observed_at': '2026-07-16T22:39:59Z',
                },
            },
            configured={
                'needs_help': True,
                'needs_help_reason': 'stuck',
                'heartbeat_outcome': 'needs_help',
                'heartbeat_observed_at': '2026-07-16T22:40:00Z',
            },
        )

        self.assertNotIn('needs_help', fields)
        self.assertNotIn('heartbeat_outcome', fields)


class ProviderUnavailableHealthTests(SimpleTestCase):
    def test_marks_same_provider_agent_without_telemetry_unavailable(self):
        agents = [{
            'agent_username': 'aoi',
            'model_provider': 'Anthropic',
            'model': 'claude-opus-4-8',
            'status': 'running',
        }]
        telemetry = {
            'nao': {
                'ts': int(time.time() * 1000),
                'runtime': {'provider': 'anthropic', 'model': 'claude-sonnet-4-6'},
                'health': {'token_pool_unavailable': True},
            },
        }

        _apply_provider_unavailable_health(agents, telemetry)

        self.assertFalse(agents[0]['health_ok'])
        self.assertTrue(agents[0]['health_token_pool_unavailable'])
        self.assertTrue(agents[0]['provider_unavailable'])
        self.assertEqual(agents[0]['availability_status'], 'unavailable')

    def test_marks_same_provider_agent_on_provider_unavailable_signal(self):
        agents = [{
            'agent_username': 'nao',
            'model_provider': 'Anthropic',
            'model': 'claude-sonnet-4-6',
            'status': 'running',
        }]
        telemetry = {
            'shizuha': {
                'ts': int(time.time() * 1000),
                'runtime': {'provider': 'anthropic', 'model': 'claude-sonnet-4-6'},
                'health': {'provider_unavailable': True},
            },
        }

        _apply_provider_unavailable_health(agents, telemetry)

        self.assertFalse(agents[0]['health_ok'])
        self.assertTrue(agents[0]['health_token_pool_unavailable'])
        self.assertTrue(agents[0]['provider_unavailable'])
        self.assertEqual(agents[0]['availability_status'], 'unavailable')

    def test_healthy_idle_agent_not_painted_by_same_provider_poison(self):
        # Operator 2026-09-23 (Hina): a voice seat ticking every 30m has
        # explicitly-healthy telemetry older than the old 10-minute window;
        # another bridge's provider outage must not paint it unavailable.
        agents = [{
            'agent_username': 'hina',
            'model_provider': 'grok',
            'model': 'cortex/grok-voice-think-fast-2.0',
            'status': 'running',
            'health_ok': True,
            'telemetry_ts': int(time.time() * 1000) - 22 * 60 * 1000,
        }]
        telemetry = {
            'ena': {
                'ts': int(time.time() * 1000),
                'runtime': {'provider': 'grok'},
                'health': {'token_pool_unavailable': True},
            },
        }

        _apply_provider_unavailable_health(agents, telemetry)

        self.assertTrue(agents[0]['health_ok'])
        self.assertNotEqual(agents[0].get('availability_status'), 'unavailable')

    def test_ignores_stale_provider_unavailable_snapshot(self):
        # 2026-07-02: a bridge that hits a token limit reports pool-unavailable
        # once and then goes idle, so its poisoned snapshot lingers in the
        # telemetry store. Older than the 10-minute freshness window it must NOT
        # paint same-provider agents (the ghost "Unavailable" badge bug).
        agents = [{
            'agent_username': 'aoi',
            'model_provider': 'Anthropic',
            'model': 'claude-opus-4-8',
            'status': 'running',
        }]
        telemetry = {
            'nao': {
                'ts': int(time.time() * 1000) - 11 * 60 * 1000,
                'runtime': {'provider': 'anthropic', 'model': 'claude-sonnet-4-6'},
                'health': {'token_pool_unavailable': True},
            },
        }

        _apply_provider_unavailable_health(agents, telemetry)

        self.assertNotEqual(agents[0].get('availability_status'), 'unavailable')
        self.assertNotEqual(agents[0].get('health_ok'), False)

    def test_ignores_untimestamped_provider_unavailable_snapshot(self):
        # A snapshot with no ts/received_at has unknowable age — real Connect
        # snapshots always carry one, so treat ts-less poison as stale/synthetic
        # rather than painting the provider on it indefinitely.
        agents = [{
            'agent_username': 'aoi',
            'model_provider': 'Anthropic',
            'model': 'claude-opus-4-8',
            'status': 'running',
        }]
        telemetry = {
            'nao': {
                'runtime': {'provider': 'anthropic', 'model': 'claude-sonnet-4-6'},
                'health': {'token_pool_unavailable': True},
            },
        }

        _apply_provider_unavailable_health(agents, telemetry)

        self.assertNotEqual(agents[0].get('availability_status'), 'unavailable')

    def test_keeps_recent_explicit_healthy_telemetry(self):
        agents = [{
            'agent_username': 'aoi',
            'model_provider': 'Anthropic',
            'model': 'claude-opus-4-8',
            'status': 'running',
            'health_ok': True,
            'health_token_pool_unavailable': False,
            'telemetry_ts': int(time.time() * 1000),
        }]
        telemetry = {
            'nao': {
                'ts': int(time.time() * 1000),
                'runtime': {'provider': 'anthropic', 'model': 'claude-sonnet-4-6'},
                'health': {'token_pool_unavailable': True},
            },
        }

        _apply_provider_unavailable_health(agents, telemetry)

        self.assertTrue(agents[0]['health_ok'])
        self.assertFalse(agents[0]['health_token_pool_unavailable'])

    def test_does_not_mark_other_provider(self):
        agents = [{
            'agent_username': 'scout',
            'model_provider': 'OpenAI',
            'model': 'gpt-5.5',
            'status': 'running',
        }]
        telemetry = {
            'nao': {
                'ts': int(time.time() * 1000),
                'runtime': {'provider': 'anthropic', 'model': 'claude-sonnet-4-6'},
                'health': {'token_pool_unavailable': True},
            },
        }

        _apply_provider_unavailable_health(agents, telemetry)

        self.assertNotIn('health_ok', agents[0])
        self.assertNotIn('provider_unavailable', agents[0])
