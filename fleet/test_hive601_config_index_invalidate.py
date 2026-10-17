"""HIVE-601: editing an rt-fleet agent's model via "Save & push" (PATCH
/v1/fleet/agents/{id}/) persisted + restarted the agent but the Agents GRID kept
showing the old model, because the grid is served from the agent_index_cache
read model (PLAT-1063) and the rt-fleet PATCH branch — unlike the native-row
branch — never invalidated that cache. It only overlaid the DETAIL response, so
the drawer updated while the grid lagged up to FRESH_TTL. Fix: bust the read
model on the rt-fleet (and Connect/BYO) config-push paths too.
"""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.response import Response
from rest_framework import status

from fleet.views import FleetAgentDetailView
from fleet.models import (
    AgentRuntimeDesiredState, DaemonRegistry, FleetAgent, RuntimeChange,
)
from hive_platform.models import TokenEntry, TokenPool


class Hive601ConfigIndexInvalidateTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.staff = SimpleNamespace(
            id=3, email='test-owner@example.com', username='hritik',
            is_staff=True, is_authenticated=True,
        )
        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id='3',
            provider=TokenPool.Provider.OPENAI,
            display_name='runtime-lane-test',
        )
        entry = TokenEntry(
            pool=pool,
            label='available',
            active=True,
        )
        entry.set_token(json.dumps({
            'access_token': 'codex-access-test',
            'refresh_token': 'codex-refresh-test',
        }))
        entry.save()

    def _patch(self, agent_id, body):
        req = self.factory.patch(f'/v1/fleet/agents/{agent_id}/', body, format='json')
        force_authenticate(req, user=self.staff, token={'email': self.staff.email, 'is_staff': True})
        return req

    def test_rtfleet_model_patch_busts_agents_index_cache(self):
        daemon = DaemonRegistry.objects.create(
            daemon_id='rt-config-daemon',
            fleet_id='rt-config-fleet',
            status=DaemonRegistry.Status.CONNECTED,
        )
        FleetAgent.objects.create(
            agent_username='shion',
            agent_id='rt-shion',
            model='cortex/DeepSeek-V4-Flash',
            mcp_config={'execution_method': 'shizuha'},
            managing_daemon=daemon,
        )
        agent = {
            'id': 'rt-shion', 'agent_username': 'shion',
            'email': 'shion@shizuha.com', 'model': 'cortex/DeepSeek-V4-Flash',
            'execution_method': 'shizuha', 'status': 'running',
        }
        req = self._patch('rt-shion', {'model': 'gpt-5.6-sol'})
        with patch('fleet.views._require_staff', return_value=None), \
                patch('fleet.views._get_agent', return_value=Response(status=status.HTTP_404_NOT_FOUND)), \
                patch('fleet.views.get_runtime_agent', return_value=agent), \
                patch('fleet.views._with_telemetry', side_effect=lambda a: a), \
                patch('fleet.views.attach_context_budget', side_effect=lambda a: a), \
                patch('fleet.views.agent_index_cache.invalidate') as invalidate:
            resp = FleetAgentDetailView.as_view()(req, agent_id='rt-shion')

        self.assertEqual(resp.status_code, 202, getattr(resp, 'data', resp))
        # The core fix: the grid read model is invalidated so the next poll shows
        # the new model instead of the stale cached one.
        self.assertTrue(invalidate.called)
        # And the detail response still overlays the new model for the drawer.
        self.assertEqual(resp.data.get('effective_model'), 'gpt-5.6-sol')
        self.assertEqual(resp.data['desired']['execution_method'], 'codex_app_server')
        self.assertEqual(resp.data['observed'], {'known': False, 'state': 'UNKNOWN'})
        self.assertEqual(resp.data['rollout_state'], 'ACTUATOR_PENDING')
        self.assertTrue(resp.data['change_id'])
        row = FleetAgent.objects.get(agent_id='rt-shion')
        self.assertEqual(row.model, 'gpt-5.6-sol')
        self.assertEqual(row.mcp_config['execution_method'], 'codex_app_server')
        self.assertEqual(row.pending_config['model'], 'gpt-5.6-sol')
        self.assertEqual(row.pending_config['execution_method'], 'codex_app_server')

    def test_connect_byo_model_patch_busts_agents_index_cache(self):
        req = self._patch('byo-1', {'model': 'glm-4.6'})
        with patch('fleet.views._require_staff', return_value=None), \
                patch('fleet.views._get_agent', return_value=Response(status=status.HTTP_404_NOT_FOUND)), \
                patch('fleet.views.get_runtime_agent', return_value=None), \
                patch('fleet.external_agents.telemetry_username_for_id', return_value='byouser'), \
                patch('fleet.external_agents.configure_agent_via_connect', return_value=(True, '')), \
                patch('fleet.views.agent_index_cache.invalidate') as invalidate:
            resp = FleetAgentDetailView.as_view()(req, agent_id='byo-1')

        self.assertEqual(resp.status_code, 200, getattr(resp, 'data', resp))
        invalidate.assert_called_once()

    def test_native_capability_patch_merges_established_config_fields(self):
        agent = FleetAgent.objects.create(
            agent_username='native-capability-test',
            agent_id='native-capability-test',
            skill_config={
                'skills': ['engineering'],
                'eager_skills': [],
                'inline_profiles': ['keep-profile'],
            },
            mcp_config={
                'enabled_mcp_server_ids': ['pulse'],
                'runtime_environment': 'k8s',
                'runtime_flags': {'keep': True},
            },
        )
        req = self._patch(str(agent.id), {
            'skills': ['engineering', 'devops'],
            'eager_skills': ['connect-messaging'],
            'enabled_mcp_server_ids': ['pulse', 'wiki'],
        })
        with patch('fleet.views._require_staff', return_value=None):
            resp = FleetAgentDetailView.as_view()(req, agent_id=str(agent.id))

        self.assertEqual(resp.status_code, 200, getattr(resp, 'data', resp))
        agent.refresh_from_db()
        self.assertEqual(agent.skill_config['skills'], ['engineering', 'devops'])
        self.assertEqual(agent.skill_config['eager_skills'], ['connect-messaging'])
        self.assertEqual(agent.skill_config['inline_profiles'], ['keep-profile'])
        self.assertEqual(agent.mcp_config['enabled_mcp_server_ids'], ['pulse', 'wiki'])
        self.assertEqual(
            agent.mcp_config['mcp_servers'],
            [{'slug': 'pulse', 'name': 'pulse'}, {'slug': 'wiki', 'name': 'wiki'}],
        )
        self.assertEqual(agent.mcp_config['runtime_environment'], 'k8s')
        self.assertEqual(agent.mcp_config['runtime_flags'], {'keep': True})

    def test_native_daemon_managed_model_patch_pushes_runtime_immediately(self):
        daemon = DaemonRegistry.objects.create(
            daemon_id='native-config-daemon',
            fleet_id='native-config-fleet',
            status=DaemonRegistry.Status.CONNECTED,
        )
        agent = FleetAgent.objects.create(
            agent_username='native-model-test',
            agent_id='native-model-test',
            model='gpt-5.5',
            model_overrides={'codex_app_server': 'gpt-5.5'},
            mcp_config={'execution_method': 'codex_app_server'},
            managing_daemon=daemon,
        )
        req = self._patch(str(agent.id), {
            'model': 'gpt-5.6-sol',
        })
        with patch('fleet.views._require_staff', return_value=None):
            resp = FleetAgentDetailView.as_view()(req, agent_id=str(agent.id))

        self.assertEqual(resp.status_code, 202, getattr(resp, 'data', resp))
        agent.refresh_from_db()
        self.assertEqual(agent.model, 'gpt-5.6-sol')
        self.assertEqual(agent.model_overrides, {})
        self.assertEqual(agent.mcp_config['execution_method'], 'codex_app_server')
        desired = AgentRuntimeDesiredState.objects.get(agent=agent)
        self.assertEqual(desired.desired_generation, 1)
        self.assertEqual(desired.runtime_profile['runtime_lane']['model'], 'gpt-5.6-sol')
        previous = desired.runtime_profile['previous_runtime_lane']
        self.assertEqual(previous['model'], 'gpt-5.5')
        self.assertEqual(previous['execution_method'], 'codex_app_server')
        self.assertTrue(previous['runtime_release_id'])
        self.assertEqual(previous['primary']['provider'], 'openai')

    def test_native_k3s_model_patch_converges_deployment_without_disable_enable(self):
        """Hive PATCH of model must roll the k3s Deployment env (kai/nagi 2026-09-05).

        admit_runtime_lane_update only enqueues a daemon config_update frame.
        Hive-rendered pods never see that frame; disable+enable was the only
        apply path. PATCH is the event — converge_running_lifecycle_agent must
        fire on it.
        """
        daemon = DaemonRegistry.objects.create(
            daemon_id='native-k3s-lane-daemon',
            fleet_id='native-k3s-lane-fleet',
            status=DaemonRegistry.Status.CONNECTED,
        )
        agent = FleetAgent.objects.create(
            agent_username='nagi-k3s-lane',
            agent_id='nagi-k3s-lane',
            model='cortex/GLM-5.3-Flash',
            mcp_config={'execution_method': 'shizuha'},
            managing_daemon=daemon,
            status=FleetAgent.Status.RUNNING,
        )
        req = self._patch(str(agent.id), {'model': 'cortex/grok-4.6'})  # deprecated pin
        with patch('fleet.views._require_staff', return_value=None), \
                patch(
                    'fleet.views.apply_k3s_lane_after_patch',
                    return_value={'applied': True, 'replicas': 1, 'reason': 'converged'},
                ) as k3s_apply:
            resp = FleetAgentDetailView.as_view()(req, agent_id=str(agent.id))

        self.assertEqual(resp.status_code, 202, getattr(resp, 'data', resp))
        agent.refresh_from_db()
        self.assertEqual(agent.model, 'cortex/grok-4.7')
        k3s_apply.assert_called_once()
        self.assertEqual(
            k3s_apply.call_args.kwargs.get('previous_model'),
            'cortex/GLM-5.3-Flash',
        )
        self.assertTrue(resp.data.get('k3s_lane_apply', {}).get('applied'))
        self.assertEqual(resp.data.get('rollout_state'), 'ROLLING')
        self.assertFalse(resp.data.get('pending_sync'))
        change = RuntimeChange.objects.get(id=resp.data['change_id'])
        self.assertEqual(change.state, RuntimeChange.State.ROLLING)

    def test_consecutive_pending_patches_never_report_desired_row_as_observed(self):
        daemon = DaemonRegistry.objects.create(
            daemon_id='native-observed-daemon', fleet_id='native-observed-fleet',
            status=DaemonRegistry.Status.CONNECTED,
        )
        agent = FleetAgent.objects.create(
            agent_username='native-observed-test', agent_id='native-observed-test',
            owner_id=3, model='gpt-5.5',
            mcp_config={'execution_method': 'codex_app_server'}, managing_daemon=daemon,
        )
        with patch('fleet.views._require_staff', return_value=None):
            first = FleetAgentDetailView.as_view()(
                self._patch(str(agent.id), {'model': 'gpt-5.6-sol'}), agent_id=str(agent.id),
            )
            second = FleetAgentDetailView.as_view()(
                self._patch(str(agent.id), {
                    'model': 'gpt-5.5', 'base_generation': first.data['desired']['generation'],
                }),
                agent_id=str(agent.id),
            )

        self.assertEqual(first.status_code, 202, getattr(first, 'data', first))
        self.assertEqual(second.status_code, 202, getattr(second, 'data', second))
        self.assertEqual(second.data['desired']['model'], 'gpt-5.5')
        self.assertEqual(second.data['observed'], {'known': False, 'state': 'UNKNOWN'})

    def test_explicit_incompatible_native_lane_rejects_without_persisting(self):
        agent = FleetAgent.objects.create(
            agent_username='native-reject-test',
            agent_id='native-reject-test',
            model='cortex/DeepSeek-V4-Flash',
            mcp_config={'execution_method': 'shizuha'},
        )
        req = self._patch(str(agent.id), {
            'model': 'gpt-5.6-sol',
            'execution_method': 'shizuha',
        })
        with patch('fleet.views._require_staff', return_value=None):
            resp = FleetAgentDetailView.as_view()(req, agent_id=str(agent.id))

        self.assertEqual(resp.status_code, 400, getattr(resp, 'data', resp))
        self.assertEqual(resp.data['code'], 'INCOMPATIBLE_RUNTIME_LANE')
        agent.refresh_from_db()
        self.assertEqual(agent.model, 'cortex/DeepSeek-V4-Flash')
        self.assertEqual(agent.mcp_config['execution_method'], 'shizuha')
        self.assertFalse(AgentRuntimeDesiredState.objects.filter(agent=agent).exists())

    def test_missing_broker_capability_rejects_before_persisting(self):
        agent = FleetAgent.objects.create(
            agent_username='native-no-token-test',
            agent_id='native-no-token-test',
            owner_id=99,
            model='cortex/DeepSeek-V4-Flash',
            mcp_config={'execution_method': 'shizuha'},
        )
        req = self._patch(str(agent.id), {'model': 'gpt-5.6-sol'})
        with patch('fleet.views._require_staff', return_value=None):
            resp = FleetAgentDetailView.as_view()(req, agent_id=str(agent.id))

        self.assertEqual(resp.status_code, 400, getattr(resp, 'data', resp))
        self.assertEqual(resp.data['code'], 'CREDENTIAL_CAPABILITY_UNAVAILABLE')
        agent.refresh_from_db()
        self.assertEqual(agent.model, 'cortex/DeepSeek-V4-Flash')
        self.assertFalse(AgentRuntimeDesiredState.objects.filter(agent=agent).exists())

    def test_static_openai_token_does_not_satisfy_codex_oauth_capability(self):
        pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id='77',
            provider=TokenPool.Provider.OPENAI,
            display_name='wrong-kind-token',
        )
        entry = TokenEntry(pool=pool, label='static-api-key', active=True)
        entry.set_token('sk-static-openai-test')
        entry.save()
        agent = FleetAgent.objects.create(
            agent_username='wrong-kind-capability-test',
            agent_id='wrong-kind-capability-test',
            owner_id=77,
            model='cortex/DeepSeek-V4-Flash',
            mcp_config={'execution_method': 'shizuha'},
        )

        req = self._patch(str(agent.id), {'model': 'gpt-5.6-sol'})
        with patch('fleet.views._require_staff', return_value=None):
            resp = FleetAgentDetailView.as_view()(req, agent_id=str(agent.id))

        self.assertEqual(resp.status_code, 400, getattr(resp, 'data', resp))
        self.assertEqual(resp.data['code'], 'CREDENTIAL_CAPABILITY_UNAVAILABLE')
        self.assertFalse(RuntimeChange.objects.filter(agent=agent).exists())

    def test_failed_forward_rollout_performs_one_fenced_bounded_rollback(self):
        """Production order: mutate/actuate forward, fail, actuate rollback, observe healthy."""
        from fleet.runtime_fleet import (
            admit_runtime_lane_update,
            process_runtime_lane_config_result,
            process_runtime_lane_health,
        )

        daemon = DaemonRegistry.objects.create(
            daemon_id='runtime-state-daemon', fleet_id='runtime-state-fleet',
            status=DaemonRegistry.Status.DISCONNECTED,
        )
        agent = FleetAgent.objects.create(
            agent_username='runtime-state-test', agent_id='runtime-state-test',
            owner_id=3, model='cortex/DeepSeek-V4-Flash',
            mcp_config={'execution_method': 'shizuha'}, managing_daemon=daemon,
        )
        ok, admission = admit_runtime_lane_update(
            str(agent.id), {'model': 'gpt-5.6-sol'}, actor='test',
        )
        self.assertTrue(ok, admission)
        forward = RuntimeChange.objects.get(id=admission['change_id'])

        applied_forward = process_runtime_lane_config_result(agent, {
            'ok': True,
            'desired_generation': forward.desired_generation,
            'runtime_lane_digest': forward.lane_digest,
        })
        self.assertEqual(applied_forward['state'], RuntimeChange.State.ROLLING)
        failed = process_runtime_lane_health(agent, {
            'observed_generation': forward.desired_generation,
            'runtime_lane_digest': forward.lane_digest,
            'apply_status': 'ok',
            'workload_ready': True,
            'container_ready': True,
            'harness_ready': True,
            'provider_health': {'available': False, 'quota_ok': True, 'in_backoff': False},
            'credential_grant_health': {},
        })
        self.assertTrue(failed['matched'])
        forward.refresh_from_db()
        self.assertEqual(forward.state, RuntimeChange.State.ROLLBACK_PENDING)
        rollback = forward.rollback_change
        self.assertEqual(rollback.desired_generation, forward.desired_generation + 1)
        self.assertEqual(rollback.state, RuntimeChange.State.ACTUATOR_PENDING)

        applied = process_runtime_lane_config_result(agent, {
            'ok': True,
            'desired_generation': rollback.desired_generation,
            'runtime_lane_digest': rollback.lane_digest,
        })
        self.assertEqual(applied['state'], RuntimeChange.State.ROLLING)
        observed = process_runtime_lane_health(agent, {
            'observed_generation': rollback.desired_generation,
            'runtime_lane_digest': rollback.lane_digest,
            'apply_status': 'ok',
            'workload_ready': True,
            'container_ready': True,
            'harness_ready': True,
            'provider_health': {'available': True, 'quota_ok': True, 'in_backoff': False},
            'credential_grant_health': {},
        })
        self.assertEqual(observed['state'], RuntimeChange.State.ROLLED_BACK)
        forward.refresh_from_db()
        self.assertEqual(forward.state, RuntimeChange.State.ROLLED_BACK)
        agent.refresh_from_db()
        self.assertEqual(agent.model, 'cortex/DeepSeek-V4-Flash')
        self.assertEqual(agent.desired_state.desired_generation, rollback.desired_generation)

    def test_runtime_result_requires_exact_generation_and_lane_digest(self):
        from fleet.runtime_fleet import admit_runtime_lane_update, process_runtime_lane_config_result

        agent = FleetAgent.objects.create(
            agent_username='runtime-fence-test', agent_id='runtime-fence-test',
            owner_id=3, model='cortex/DeepSeek-V4-Flash',
            mcp_config={'execution_method': 'shizuha'},
        )
        ok, admission = admit_runtime_lane_update(
            str(agent.id), {'model': 'gpt-5.6-sol'}, actor='test',
        )
        self.assertTrue(ok, admission)
        change = RuntimeChange.objects.get(id=admission['change_id'])
        stale = process_runtime_lane_config_result(agent, {
            'ok': True,
            'desired_generation': change.desired_generation,
            'runtime_lane_digest': '0' * 64,
        })
        self.assertFalse(stale['matched'])
        change.refresh_from_db()
        self.assertEqual(change.state, RuntimeChange.State.ACTUATOR_PENDING)
        self.assertIsNone(change.outbox.delivered_at)

    def test_newer_runtime_lane_terminally_supersedes_unacked_generation(self):
        """Production order: enqueue N, accept N+1, then never replay stale N."""
        from fleet.runtime_fleet import admit_runtime_lane_update

        daemon = DaemonRegistry.objects.create(
            daemon_id='runtime-supersede-daemon', fleet_id='runtime-supersede-fleet',
            status=DaemonRegistry.Status.DISCONNECTED,
        )
        agent = FleetAgent.objects.create(
            agent_username='runtime-supersede-test', agent_id='runtime-supersede-test',
            owner_id=3, model='cortex/DeepSeek-V4-Flash',
            mcp_config={'execution_method': 'shizuha'}, managing_daemon=daemon,
        )
        ok, first_admission = admit_runtime_lane_update(
            str(agent.id), {'model': 'gpt-5.6-sol'}, actor='test',
        )
        self.assertTrue(ok, first_admission)
        first = RuntimeChange.objects.get(id=first_admission['change_id'])
        self.assertIsNone(first.outbox.delivered_at)

        ok, second_admission = admit_runtime_lane_update(
            str(agent.id), {'model': 'cortex/DeepSeek-V4-Flash'}, actor='test',
        )
        self.assertTrue(ok, second_admission)
        second = RuntimeChange.objects.get(id=second_admission['change_id'])
        self.assertGreater(second.desired_generation, first.desired_generation)

        first.refresh_from_db()
        first.outbox.refresh_from_db()
        self.assertEqual(first.state, RuntimeChange.State.SUPERSEDED)
        self.assertEqual(first.failure_code, 'SUPERSEDED_BY_NEWER_GENERATION')
        self.assertIsNotNone(first.outbox.delivered_at)
        self.assertEqual(
            first.outbox.last_error,
            f'SUPERSEDED_BY_GENERATION_{second.desired_generation}',
        )
        self.assertIsNone(second.outbox.delivered_at)

    def test_rejected_forward_outbox_is_terminal_and_only_rollback_stays_pending(self):
        from fleet.runtime_fleet import (
            admit_runtime_lane_update,
            process_runtime_lane_config_result,
            runtime_lane_outbox_to_prometheus,
        )

        agent = FleetAgent.objects.create(
            agent_username='runtime-reject-test', agent_id='runtime-reject-test',
            owner_id=3, model='cortex/DeepSeek-V4-Flash',
            mcp_config={'execution_method': 'shizuha'},
        )
        ok, admission = admit_runtime_lane_update(
            str(agent.id), {'model': 'gpt-5.6-sol'}, actor='test',
        )
        self.assertTrue(ok, admission)
        forward = RuntimeChange.objects.get(id=admission['change_id'])

        rejected = process_runtime_lane_config_result(agent, {
            'ok': False,
            'error': 'runtime_lane_fence_state_write_failed',
            'desired_generation': forward.desired_generation,
            'runtime_lane_digest': forward.lane_digest,
        })

        self.assertTrue(rejected['matched'])
        forward.refresh_from_db()
        forward.outbox.refresh_from_db()
        self.assertEqual(forward.state, RuntimeChange.State.ROLLBACK_PENDING)
        self.assertIsNotNone(forward.outbox.delivered_at)
        self.assertEqual(forward.outbox.last_error, 'ACTUATOR_REJECTED')
        rollback = forward.rollback_change
        self.assertIsNone(rollback.outbox.delivered_at)
        self.assertIn(
            'hive_runtime_lane_outbox_pending{owner_team="engineering"} 1',
            runtime_lane_outbox_to_prometheus(),
        )

    def test_healthy_health_before_config_result_terminally_acks_forward_outbox(self):
        """Production order: admission -> exact health -> late config_result."""
        from fleet.runtime_fleet import (
            admit_runtime_lane_update,
            process_runtime_lane_config_result,
            process_runtime_lane_health,
            runtime_lane_outbox_to_prometheus,
        )

        agent = FleetAgent.objects.create(
            agent_username='runtime-health-first-ok', agent_id='runtime-health-first-ok',
            owner_id=3, model='cortex/DeepSeek-V4-Flash',
            mcp_config={'execution_method': 'shizuha'},
        )
        ok, admission = admit_runtime_lane_update(
            str(agent.id), {'model': 'gpt-5.6-sol'}, actor='test',
        )
        self.assertTrue(ok, admission)
        forward = RuntimeChange.objects.get(id=admission['change_id'])

        observed = process_runtime_lane_health(agent, {
            'observed_generation': forward.desired_generation,
            'runtime_lane_digest': forward.lane_digest,
            'apply_status': 'ok',
            'workload_ready': True,
            'container_ready': True,
            'harness_ready': True,
            'provider_health': {'available': True, 'quota_ok': True, 'in_backoff': False},
            'credential_grant_health': {},
        })

        self.assertEqual(observed['state'], RuntimeChange.State.HEALTHY)
        forward.outbox.refresh_from_db()
        self.assertIsNotNone(forward.outbox.delivered_at)
        self.assertEqual(forward.outbox.last_error, '')
        self.assertIn(
            'hive_runtime_lane_outbox_pending{owner_team="engineering"} 0',
            runtime_lane_outbox_to_prometheus(),
        )
        late_result = process_runtime_lane_config_result(agent, {
            'ok': True,
            'desired_generation': forward.desired_generation,
            'runtime_lane_digest': forward.lane_digest,
        })
        self.assertTrue(late_result['idempotent'])
        self.assertEqual(late_result['state'], RuntimeChange.State.HEALTHY)

    def test_failed_health_before_config_result_retires_forward_and_queues_rollback(self):
        """Production order: admission -> exact failed health; config_result is lost."""
        from fleet.runtime_fleet import (
            admit_runtime_lane_update,
            process_runtime_lane_health,
            runtime_lane_outbox_to_prometheus,
        )

        agent = FleetAgent.objects.create(
            agent_username='runtime-health-first-failed', agent_id='runtime-health-first-failed',
            owner_id=3, model='cortex/DeepSeek-V4-Flash',
            mcp_config={'execution_method': 'shizuha'},
        )
        ok, admission = admit_runtime_lane_update(
            str(agent.id), {'model': 'gpt-5.6-sol'}, actor='test',
        )
        self.assertTrue(ok, admission)
        forward = RuntimeChange.objects.get(id=admission['change_id'])

        failed = process_runtime_lane_health(agent, {
            'observed_generation': forward.desired_generation,
            'runtime_lane_digest': forward.lane_digest,
            'apply_status': 'failed',
            'workload_ready': False,
            'container_ready': True,
            'harness_ready': False,
            'provider_health': {'available': False, 'quota_ok': True, 'in_backoff': False},
            'credential_grant_health': {},
            'last_apply_error': 'provider readiness failed',
        })

        self.assertTrue(failed['matched'])
        forward.refresh_from_db()
        forward.outbox.refresh_from_db()
        self.assertEqual(forward.state, RuntimeChange.State.ROLLBACK_PENDING)
        self.assertIsNotNone(forward.outbox.delivered_at)
        self.assertEqual(forward.outbox.last_error, 'RUNTIME_HEALTH_FAILED')
        rollback = forward.rollback_change
        self.assertIsNone(rollback.outbox.delivered_at)
        self.assertIn(
            'hive_runtime_lane_outbox_pending{owner_team="engineering"} 1',
            runtime_lane_outbox_to_prometheus(),
        )

    def test_runtime_failure_evidence_redacts_structured_and_bearer_secrets(self):
        from fleet.runtime_fleet import _redact_runtime_evidence

        cases = [
            ({'token': 'abc123', 'nested': {'api_key': 'key-secret'}}, ['abc123', 'key-secret']),
            ('{"refresh_token":"rt-secret"}', ['rt-secret']),
            ("{'access_token': 'at-secret'}", ['at-secret']),
            ('Authorization: Bearer bearer-secret', ['bearer-secret']),
            ('api-key=plain-secret', ['plain-secret']),
        ]
        for value, forbidden in cases:
            with self.subTest(value=value):
                redacted = _redact_runtime_evidence(value)
                self.assertIn('[REDACTED]', redacted)
                for secret in forbidden:
                    self.assertNotIn(secret, redacted)

    def test_runtime_lane_alert_is_installed_by_the_ci_deploy(self):
        manifest = Path('k8s/hive-runtime-lane-alerts.yaml').read_text()
        workflow = "\n".join(Path(path).read_text() for path in (
            '.forgejo/workflows/deploy.yml',
            '.forgejo/scripts/ring-release-fleet.sh',
        ))

        self.assertIn('name: hive-runtime-lane', manifest)
        self.assertEqual(manifest.count('owner_group: engineering'), 2)
        self.assertIn(
            'kubectl apply --dry-run=server -f "$RUNTIME_LANE_ALERTS"', workflow,
        )
        self.assertIn('kubectl apply -f "$RUNTIME_LANE_ALERTS"', workflow)
        self.assertIn(
            'kubectl -n shizuha-hive get prometheusrule hive-runtime-lane', workflow,
        )

    def test_stale_base_generation_rejects_without_overwriting_newer_lane(self):
        agent = FleetAgent.objects.create(
            agent_username='native-cas-test',
            agent_id='native-cas-test',
            model='gpt-5.5',
            mcp_config={'execution_method': 'codex_app_server'},
        )
        AgentRuntimeDesiredState.objects.create(
            agent=agent,
            desired_generation=3,
            desired_config_hash='a' * 64,
            runtime_profile={'runtime_lane': {
                'model': 'gpt-5.5', 'execution_method': 'codex_app_server',
                'model_fallbacks': [], 'model_overrides': {},
            }},
        )
        req = self._patch(str(agent.id), {
            'model': 'gpt-5.6-sol',
            'base_generation': 2,
        })
        with patch('fleet.views._require_staff', return_value=None):
            resp = FleetAgentDetailView.as_view()(req, agent_id=str(agent.id))

        self.assertEqual(resp.status_code, 409, getattr(resp, 'data', resp))
        self.assertEqual(resp.data['code'], 'GENERATION_CONFLICT')
        agent.refresh_from_db()
        self.assertEqual(agent.model, 'gpt-5.5')
        self.assertEqual(agent.desired_state.desired_generation, 3)

    def test_native_daemon_managed_provider_switch_persists_and_pushes_method(self):
        anthropic_pool = TokenPool.objects.create(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id='3',
            provider=TokenPool.Provider.ANTHROPIC,
            display_name='native-provider-switch-test',
        )
        anthropic_entry = TokenEntry(
            pool=anthropic_pool, label='available', active=True,
        )
        anthropic_entry.set_token('sk-ant-provider-switch-test')
        anthropic_entry.save()
        daemon = DaemonRegistry.objects.create(
            daemon_id='native-provider-daemon',
            fleet_id='native-provider-fleet',
            status=DaemonRegistry.Status.CONNECTED,
        )
        agent = FleetAgent.objects.create(
            agent_username='native-provider-test',
            agent_id='native-provider-test',
            model='gpt-5.6-sol',
            mcp_config={
                'execution_method': 'codex_app_server',
                'enabled_mcp_server_ids': ['pulse', 'wiki'],
                'runtime_flags': {'keep': True},
            },
            managing_daemon=daemon,
        )
        req = self._patch(str(agent.id), {
            'model': 'claude-opus-4-8',
            'execution_method': 'claude_code_server',
            'reasoning_effort': '',
        })
        with patch('fleet.views._require_staff', return_value=None), \
                patch('fleet.views.configure_runtime_agent', return_value=(True, '')) as configure:
            resp = FleetAgentDetailView.as_view()(req, agent_id=str(agent.id))

        self.assertEqual(resp.status_code, 202, getattr(resp, 'data', resp))
        agent.refresh_from_db()
        self.assertEqual(agent.model, 'claude-opus-4-8')
        self.assertEqual(agent.mcp_config['execution_method'], 'claude_code_server')
        self.assertEqual(agent.mcp_config['enabled_mcp_server_ids'], ['pulse', 'wiki'])
        self.assertEqual(agent.mcp_config['runtime_flags'], {'keep': True})
        self.assertEqual(resp.data['desired']['model'], 'claude-opus-4-8')
        self.assertEqual(resp.data['desired']['execution_method'], 'claude_code_server')
        self.assertEqual(resp.data['rollout_state'], 'ACTUATOR_PENDING')
        # Runtime-lane mutations emit one complete, fenced outbox frame rather
        # than a second legacy partial configure push.
        configure.assert_not_called()
