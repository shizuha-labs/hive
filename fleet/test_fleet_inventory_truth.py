from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from fleet import external_agents
from fleet.models import FleetAgent
from fleet.views import FleetAgentListView, _compute_agent_health_summary


class FleetInventoryTruthTests(TestCase):
    def setUp(self):
        self.now = 1_789_321_300
        self.canonical = {
            'user_id': 76,
            'username': 'claude',
            'email': 'claude@shizuha.com',
            'created_by_id': 3,
            'created_by_username': 'operator',
            'display_name': 'Claude Anthropic',
            'agent_runtime_id': '',
        }

    def external_rows(self, heartbeats, bulk=None, fallback=None):
        with patch.object(external_agents, '_alive_heartbeats', return_value=heartbeats), \
                patch.object(external_agents, '_bulk_agents_by_email', return_value=(
                    bulk if bulk is not None else {self.canonical['email']: self.canonical}
                )), \
                patch.object(external_agents, '_resolve_agent_per_username', return_value=(
                    fallback if fallback is not None else {
                        **self.canonical, 'display_name': 'Claude',
                    }
                )), \
                patch.object(external_agents, 'agent_telemetry', return_value={}), \
                patch.object(external_agents, '_k3s_runtime_snapshot', return_value={}), \
                patch('time.time', return_value=self.now):
            return external_agents.list_external_agents(owner_id=3)

    def api_response(self, rows, cache_state='stale-revalidating'):
        request = APIRequestFactory().get('/v1/fleet/agents/?page_size=250')
        force_authenticate(request, user=SimpleNamespace(
            id=3, is_staff=True, is_authenticated=True,
            email='operator@shizuha.com', username='operator',
        ))
        with patch('fleet.views.agent_index_cache.get_or_build', return_value=(rows, cache_state)), \
                patch('fleet.views._overlay_live_pulse_memberships', side_effect=lambda agents, **kwargs: agents), \
                patch('fleet.views.pulse_roster_index_degradation', return_value=(False, None)):
            return FleetAgentListView.as_view()(request)

    def test_pulse_decimal_timestamps_remain_live_and_malformed_values_fail_closed(self):
        reader = MagicMock()
        values = {
            'pulse:agent_hb:claude@shizuha.com': '1789321299.98765',
            'pulse:agent_hb:codex@shizuha.com': '1789321290',
            'pulse:agent_hb:invalid@shizuha.com': 'not-a-timestamp',
            'pulse:agent_hb:infinite@shizuha.com': 'inf',
            'pulse:agent_hb:nan@shizuha.com': 'nan',
        }
        reader.scan_iter.return_value = list(values)
        reader.get.side_effect = values.__getitem__
        external_agents._alive_heartbeats.cache_clear()
        self.addCleanup(external_agents._alive_heartbeats.cache_clear)
        with patch('redis.Redis.from_url', return_value=reader):
            heartbeats = external_agents._alive_heartbeats()
        self.assertEqual(heartbeats['claude@shizuha.com'], self.now - 1)
        self.assertEqual(heartbeats['codex@shizuha.com'], self.now - 10)
        for username in ('invalid', 'infinite', 'nan'):
            self.assertEqual(heartbeats[f'{username}@shizuha.com'], 0)
        rows = self.external_rows({'claude@shizuha.com': heartbeats['claude@shizuha.com']})
        self.assertEqual(rows[0]['status'], 'running')
        self.assertEqual(rows[0]['heartbeat_age_sec'], 1)

    def test_email_aliases_coalesce_by_identity_using_latest_heartbeat(self):
        rows = self.external_rows({
            'claude@shizuha.com': self.now - 400,
            'claude@agents.shizuha.io': self.now - 2,
        })
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], 'ext:76')
        self.assertEqual(rows[0]['display_name'], 'Claude Anthropic')
        self.assertEqual(rows[0]['email'], 'claude@shizuha.com')
        self.assertEqual(rows[0]['heartbeat_age_sec'], 2)

    def test_fallback_only_aliases_still_coalesce(self):
        rows = self.external_rows({
            'claude@shizuha.com': self.now - 2,
            'claude@agents.shizuha.io': self.now - 3,
        }, bulk={})
        self.assertEqual([row['id'] for row in rows], ['ext:76'])

    def test_resolved_canonical_username_cannot_duplicate_managed_agent(self):
        FleetAgent.objects.create(agent_username='claude', status='running')
        rows = self.external_rows({'legacy-alias@agents.shizuha.io': self.now - 2})
        self.assertEqual(rows, [])

    def test_alias_cannot_cross_owner_boundary(self):
        foreign = {**self.canonical, 'created_by_id': 99}
        self.assertEqual(self.external_rows(
            {'claude@shizuha.com': self.now - 1},
            bulk={'claude@shizuha.com': foreign},
        ), [])

    def test_native_tombstone_does_not_erase_distinct_external_identity(self):
        retired = FleetAgent.objects.create(agent_username='claude', status='deleted')
        rows = self.external_rows({'claude@shizuha.com': self.now - 1})
        rows.extend([
            {'id': str(retired.pk), 'agent_username': 'claude', 'runtime_source': 'k3s-native'},
            {'id': 'old-daemon', 'agent_username': 'claude', 'runtime_source': 'rt-fleet'},
        ])
        response = self.api_response(rows)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row['id'] for row in response.data['results']], ['ext:76'])

    def test_cached_health_age_advances_without_repainting_health_or_mutating_cache(self):
        observed = datetime.fromtimestamp(self.now - 140, timezone.utc).isoformat()
        row = {
            'id': 'agent-aoi', 'agent_username': 'aoi', 'status': 'running',
            'last_telemetry_at': observed,
            'last_active_at': datetime.fromtimestamp(self.now - 300, timezone.utc).isoformat(),
            'needs_help': True, 'needs_help_reason': '5 ready tasks with no progress',
        }
        with patch('time.time', return_value=self.now - 130):
            row['health_summary'] = _compute_agent_health_summary(row)
        cached_rows = [deepcopy(row)]
        with patch('time.time', return_value=self.now):
            response = self.api_response(cached_rows)
        refreshed = response.data['results'][0]
        self.assertEqual(refreshed['health_summary']['last_snapshot_age_s'], 140)
        self.assertEqual(cached_rows[0]['health_summary']['last_snapshot_age_s'], 10)
        self.assertEqual(refreshed['health_summary']['issues'], row['health_summary']['issues'])
        self.assertFalse(refreshed['health_summary']['ok'])
        self.assertTrue(refreshed['needs_help'])
        self.assertEqual(refreshed['last_telemetry_at'], observed)

    def test_invalid_cached_timestamp_is_unknown_not_fresh(self):
        rows = [{
            'agent_username': 'unknown', 'last_telemetry_at': 'bad timestamp',
            'health_summary': {'ok': False, 'last_snapshot_age_s': 1},
        }]
        response = self.api_response(rows, cache_state='hit')
        self.assertIsNone(response.data['results'][0]['health_summary']['last_snapshot_age_s'])
        self.assertFalse(response.data['results'][0]['health_summary']['ok'])
