from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from kubernetes import client
from kubernetes.client.rest import ApiException
from rest_framework.test import APIRequestFactory, force_authenticate

from . import native_runtime_release as release, provisioner
from .lifecycle_fleet import hibernate_lifecycle_agent
from .models import AgentDaemonBinding, AgentRuntimeDesiredState, DaemonRegistry, FleetAgent
from .views import FleetAgentListView


@override_settings(HIVE_NATIVE_RUNTIME_RELEASE_ENABLED=True)
class NativeLifecycleProjectionCallerTests(TestCase):
    def setUp(self):
        self.daemon = DaemonRegistry.objects.create(daemon_id='legacy', fleet_id='legacy')
        self.agent = FleetAgent.objects.create(
            agent_username='mio', agent_id='platform-mio',
            namespace='shizuha-fleet', status='running', managing_daemon=self.daemon,
            image='localhost:30500/shizuha-agent-runtime@' + release.ANCHOR_DIGEST,
        )
        self.desired = AgentRuntimeDesiredState.objects.create(
            agent=self.agent, expected_deployment_uid='deployment-1',
        )
        record = {
            'generation': 27, 'image_digest': release.ANCHOR_DIGEST,
            'source_commit': release.ANCHOR_SOURCE, 'approved_at': '2026-09-09T00:00:00Z',
            'display_tag': 'localhost:30500/shizuha-agent-runtime:harness-native-27',
            'intent': 'promote', 'rollback_of_generation': None,
        }
        history = release.parse_document(json.dumps({
            'schema_version': 1, 'scope': release.SCOPE,
            'desired_generation': 27, 'releases': [record],
        }))
        self.authority = release.NativeRuntimeRelease(history[0], 'a' * 64, 'b' * 40, 'authority-1', '1', history)
        self.target = client.V1Deployment(
            metadata=client.V1ObjectMeta(
                name='agent-mio', namespace='shizuha-fleet', uid='deployment-1', resource_version='1',
                labels={'app.kubernetes.io/managed-by': 'shizuha-hive', 'shizuha.io/agent': 'mio'},
                annotations={'shizuha.io/runtime-owner': 'hive'},
            ),
            spec=client.V1DeploymentSpec(
                replicas=1, selector=client.V1LabelSelector(match_labels={'agent': 'mio'}),
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(),
                    spec=client.V1PodSpec(containers=[client.V1Container(name='agent', image=self.agent.image)]),
                ),
            ),
        )
        release.stamp_release(self.authority, self.target)
        self.apps, self.core = Mock(), Mock()
        self.apps.read_namespaced_deployment.side_effect = lambda *args, **kwargs: deepcopy(self.target)
        self.apps.patch_namespaced_deployment_scale.side_effect = self.scale
        self.core.list_namespaced_pod.return_value = SimpleNamespace(items=[])
        self.mock('fleet.provisioner.get_k8s_client', return_value=(self.core, self.apps, None))
        self.mock('fleet.native_runtime_release.get_native_runtime_release', return_value=self.authority)
        self.mock('fleet.provisioner.ensure_agent_image', return_value=True)
        self.mock('fleet.provisioner.ensure_agent_id_password_aligned', return_value='ok')
        self.mock('fleet.provisioner._personal_runtime_isolation_gate', return_value=False)
        self.mock('fleet.provisioner._lease_start_decision', return_value=SimpleNamespace(allowed=True))
        self.mock('fleet.provisioner.agent_needs_browser_mcp', return_value=False)
        self.mock('fleet.model_lease.release_leases_for_agent', return_value={'released': []})
        self.mock('fleet.lifecycle_fleet._best_effort_runtime_toggle')
        self.mock('fleet.lifecycle_fleet.notify_pulse_agent_lifecycle')
        self.cached = [{
            'id': self.agent.agent_id, 'agent_username': 'mio', 'status': 'running', 'enabled': True,
            'runtime_source': 'rt-fleet', 'model': 'cortex/GLM-5.3-Flash',
            'activity_state': 'working', 'activity_phase': 'tool', 'activity_tool_name': 'bash',
            'needs_help': True, 'health_summary': {'ok': False, 'issues': [{'type': 'heartbeat_needs_help'}]},
            'model_lease': {'managed': True, 'state': 'holder', 'is_holder': True, 'holder': 'agent-mio'},
        }]

    def mock(self, name, **kwargs):
        patcher = patch(name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def scale(self, *, body, **kwargs):
        metadata = body.get('metadata', {})
        if metadata and metadata != {'uid': self.target.metadata.uid, 'resourceVersion': self.target.metadata.resource_version}:
            raise ApiException(status=409)
        self.target.spec.replicas = body['spec']['replicas']
        self.target.metadata.resource_version = str(int(self.target.metadata.resource_version) + 1)
        return client.V1Scale(metadata=deepcopy(self.target.metadata), spec=client.V1ScaleSpec(replicas=body['spec']['replicas']))

    def page(self):
        request = APIRequestFactory().get('/v1/fleet/agents/?page_size=100')
        force_authenticate(request, user=SimpleNamespace(id=1, is_staff=True, is_authenticated=True))
        with patch('fleet.views.agent_index_cache.get_or_build', return_value=(self.cached, 'stale-revalidating')), \
                patch('fleet.views._overlay_live_pulse_memberships', side_effect=lambda agents, *args, **kwargs: agents), \
                patch('fleet.views.pulse_roster_index_degradation', return_value=(False, None)), \
                patch('fleet.provisioner.get_k8s_client', side_effect=AssertionError('foreground runtime read')):
            response = FleetAgentListView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        return response.data['results'][0]

    def hibernate(self):
        ok, detail = hibernate_lifecycle_agent(str(self.agent.pk), reason='idle capacity handoff')
        self.assertTrue(ok, detail)
        self.agent.refresh_from_db()

    def test_actual_hibernate_overrides_stale_cached_runtime_without_network(self):
        self.hibernate()
        self.assertEqual(self.target.spec.replicas, 0)
        result = self.page()
        self.assertEqual(result['status'], 'hibernated')
        self.assertFalse(result['enabled'])
        self.assertEqual(result['lifecycle_plane'], 'native')
        self.assertEqual(result['model_lease']['state'], 'unknown')
        self.assertIsNone(result['model_lease']['is_holder'])
        self.assertTrue(result['model_lease']['previous_observation']['is_holder'])
        self.assertIsNone(result['activity_phase'])
        self.assertEqual(result['previous_activity_observation']['activity_phase'], 'tool')
        self.assertTrue(result['needs_help'])
        self.assertEqual(result['health_summary']['issues'], self.cached[0]['health_summary']['issues'])
        self.assertFalse(result['health_summary']['ok'])
        self.assertEqual(self.cached[0]['status'], 'running')

    def rebuild(self):
        request = SimpleNamespace(user=SimpleNamespace(is_staff=True))
        def lease_read(agents):
            self.assertEqual(agents[0]['lifecycle_receipt_id'], self.agent.native_lifecycle_receipt['operation_id'])
            self.assertEqual(agents[0]['status'], self.agent.status)
            return {'mio': {'managed': True, 'state': 'holder', 'is_holder': True, 'holder': 'agent-mio'}}

        with patch('fleet.views.FleetAgentSerializer', return_value=SimpleNamespace(data=deepcopy(self.cached))), \
                patch('fleet.external_agents.agent_telemetry', return_value={}), \
                patch('fleet.views.get_runtime_activity_rates', return_value={}), \
                patch('fleet.views._attach_pulse_team_memberships', side_effect=lambda request, agents, **kwargs: agents), \
                patch('fleet.views._attach_identity_routing_health', side_effect=lambda agents: agents), \
                patch('fleet.views._attach_health_summaries', side_effect=lambda agents: agents), \
                patch('fleet.model_lease.lease_status_by_username', side_effect=lease_read):
            return FleetAgentListView()._build_agents_index(request, include_external=False)

    def test_actual_rebuild_preserves_fresh_hibernated_but_still_held_leak(self):
        self.hibernate()
        self.cached = self.rebuild()
        result = self.page()
        self.assertEqual(result['status'], 'hibernated')
        self.assertEqual(result['model_lease']['state'], 'holder')
        self.assertTrue(result['model_lease']['is_holder'])

    def test_second_stop_invalidates_prior_stop_lease_observation_without_new_generation(self):
        self.assertTrue(provisioner.stop_agent(self.agent))
        self.agent.refresh_from_db()
        self.cached = self.rebuild()
        first_id = self.cached[0]['lifecycle_receipt_id']
        self.assertTrue(provisioner.start_agent(self.agent))
        self.agent.refresh_from_db()
        self.assertTrue(provisioner.stop_agent(self.agent))
        self.agent.refresh_from_db()
        self.assertNotEqual(first_id, self.agent.native_lifecycle_receipt['operation_id'])
        result = self.page()
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(result['model_lease']['state'], 'unknown')
        self.cached = self.rebuild()
        self.assertEqual(self.page()['model_lease']['state'], 'holder')

    def test_stop_start_and_second_hibernate_rearm(self):
        self.assertTrue(provisioner.stop_agent(self.agent))
        self.agent.refresh_from_db()
        self.assertEqual(self.page()['status'], 'stopped')
        self.desired.desired_lifecycle = 'active'
        self.desired.save(update_fields=['desired_lifecycle'])
        self.assertTrue(provisioner.start_agent(self.agent))
        self.agent.refresh_from_db()
        self.assertEqual(self.page()['status'], 'running')
        self.hibernate()
        self.assertEqual(self.page()['status'], 'hibernated')

    def test_failed_stop_does_not_manufacture_completed_inactive_state(self):
        self.apps.patch_namespaced_deployment_scale.side_effect = ApiException(status=500)
        self.assertFalse(provisioner.stop_agent(self.agent))
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.status, 'running')
        self.assertEqual(self.page()['status'], 'running')
        self.assertNotEqual(self.agent.native_lifecycle_receipt.get('state'), 'completed')

    def test_start_attempt_retires_old_inactive_receipt_before_ambiguous_scale(self):
        self.hibernate()
        self.apps.patch_namespaced_deployment_scale.side_effect = ApiException(status=500)
        self.assertFalse(provisioner.start_agent(self.agent))
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.native_lifecycle_receipt['state'], 'pending')
        self.assertEqual(self.page()['status'], 'running')

    def test_changed_desired_generation_or_executor_does_not_override_daemon(self):
        self.hibernate()
        self.desired.desired_generation += 1
        self.desired.save(update_fields=['desired_generation'])
        self.assertEqual(self.page()['status'], 'running')
        self.desired.desired_generation -= 1
        self.desired.lifecycle_executor = 'daemon_v2'
        self.desired.save(update_fields=['desired_generation', 'lifecycle_executor'])
        self.assertEqual(self.page()['status'], 'running')

    def test_owner_binding_or_different_daemon_invalidates_native_receipt(self):
        self.hibernate()
        binding = AgentDaemonBinding.objects.create(agent=self.agent, binding_state='bound', active_owner_daemon=self.daemon)
        self.assertEqual(self.page()['status'], 'running')
        binding.delete()
        self.agent.managing_daemon = None
        self.agent.save(update_fields=['managing_daemon'])
        self.assertEqual(self.page()['status'], 'running')

    def test_unknown_native_owner_cannot_begin_stop_or_override_daemon(self):
        self.target.metadata.annotations['shizuha.io/runtime-owner'] = 'daemon'
        self.assertFalse(provisioner.stop_agent(self.agent))
        self.apps.patch_namespaced_deployment_scale.assert_not_called()
        self.assertEqual(self.page()['status'], 'running')

    def test_replaced_deployment_during_operation_cannot_complete_receipt(self):
        def replaced(*args, **kwargs):
            self.target.metadata.uid = 'deployment-replacement'
            return True

        with patch('fleet.provisioner.ensure_agent_image', side_effect=replaced):
            self.hibernate()
        self.assertEqual(self.agent.native_lifecycle_receipt['state'], 'pending')
        self.assertEqual(self.page()['status'], 'running')

    def test_scale_cas_uses_pending_target_and_cannot_stop_replacement(self):
        def replaced_before_cas(**kwargs):
            self.target.metadata.uid = 'replacement-before-cas'
            self.target.metadata.resource_version = 'replacement-version'
            return self.scale(**kwargs)

        self.apps.patch_namespaced_deployment_scale.side_effect = replaced_before_cas
        self.assertFalse(provisioner.stop_agent(self.agent))
        self.assertEqual(self.target.spec.replicas, 1)
        wire = self.apps.patch_namespaced_deployment_scale.call_args.kwargs['body']
        self.assertEqual(wire['metadata'], {'uid': 'deployment-1', 'resourceVersion': '1'})
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.native_lifecycle_receipt['state'], 'pending')

    def test_source_change_during_operation_cannot_complete_receipt(self):
        def changed(*args, **kwargs):
            self.desired.executor_generation += 1
            self.desired.save(update_fields=['executor_generation'])
            return True

        with patch('fleet.provisioner.ensure_agent_image', side_effect=changed):
            self.hibernate()
        self.assertEqual(self.agent.native_lifecycle_receipt['state'], 'pending')
        self.assertEqual(self.page()['status'], 'running')

    def test_legacy_or_incomplete_receipt_cannot_override_daemon_truth(self):
        self.hibernate()
        original = deepcopy(self.agent.native_lifecycle_receipt)
        for missing in ('source', 'operation_id', 'completed_at', 'authority_uid', 'applied_digest'):
            with self.subTest(missing=missing):
                self.agent.native_lifecycle_receipt = {key: value for key, value in original.items() if key != missing}
                self.agent.save(update_fields=['native_lifecycle_receipt'])
                self.assertEqual(self.page()['status'], 'running')

    def test_unstamped_release_cannot_produce_native_ownership_receipt(self):
        for metadata in (self.target.metadata, self.target.spec.template.metadata):
            metadata.annotations.pop(release.GENERATION_ANNOTATION)
            metadata.annotations.pop(release.DIGEST_ANNOTATION)
        self.assertFalse(provisioner.stop_agent(self.agent))
        self.apps.patch_namespaced_deployment_scale.assert_not_called()
        self.assertEqual(self.page()['status'], 'running')

    def test_inactive_legacy_status_alone_never_infers_native_receipt(self):
        self.agent.status = 'hibernated'
        self.agent.runtime_lifecycle_state = 'hibernated'
        self.agent.save(update_fields=['status', 'runtime_lifecycle_state'])
        self.assertEqual(self.agent.native_lifecycle_receipt, {})
        self.assertEqual(self.page()['status'], 'running')

    def test_legacy_start_path_retires_receipt_before_native_authority_is_reenabled(self):
        self.hibernate()
        with override_settings(HIVE_NATIVE_RUNTIME_RELEASE_ENABLED=False), patch(
            'fleet.runtime_release.get_authoritative_runtime_release',
            return_value=SimpleNamespace(release=self.authority.release),
        ):
            self.assertTrue(provisioner.start_agent(self.agent))
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.native_lifecycle_receipt['state'], 'pending')
        self.assertEqual(self.page()['status'], 'running')

    def test_cached_inactive_receipt_cannot_repaint_successful_start(self):
        self.hibernate()
        self.cached = self.rebuild()
        self.assertTrue(provisioner.start_agent(self.agent))
        self.agent.refresh_from_db()
        result = self.page()
        self.assertEqual(result['status'], 'unknown')
        self.assertIsNone(result['enabled'])
        self.assertEqual(result['model_lease']['state'], 'unknown')

    def test_failed_start_and_retry_keep_original_cached_receipt_retired(self):
        self.hibernate()
        self.cached = self.rebuild()
        retired_id = self.cached[0]['lifecycle_receipt_id']
        self.apps.patch_namespaced_deployment_scale.side_effect = ApiException(status=500)
        self.assertFalse(provisioner.start_agent(self.agent))
        self.agent.refresh_from_db()
        self.assertEqual(self.page()['status'], 'unknown')
        self.apps.patch_namespaced_deployment_scale.side_effect = self.scale
        self.assertTrue(provisioner.start_agent(self.agent))
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.native_lifecycle_receipt['supersedes_operation_id'], retired_id)
        self.assertEqual(self.page()['status'], 'unknown')
        self.hibernate()
        self.assertEqual(self.page()['status'], 'hibernated')
        self.cached = self.rebuild()
        self.assertEqual(self.page()['model_lease']['state'], 'holder')
