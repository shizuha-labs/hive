"""Runtime actuation follows a confirmed lease, including cold bootstrap."""
from contextlib import ExitStack
from copy import deepcopy
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from kubernetes import client
from kubernetes.client.rest import ApiException
from rest_framework.test import APIRequestFactory, force_authenticate

from fleet import provisioner
from fleet.lifecycle_fleet import list_lifecycle_agents
from fleet.management.commands.reconcile_on_demand_agents import ConnectLifecycleSnapshot
from fleet.models import FleetAgent
from fleet.views import (
    ConnectWakeAgentView, FleetAgentEnableView, FleetAgentListView,
    _lease_wait_response, _rt_fleet_control,
)


class DeploymentAPI:
    """Kubernetes mutation boundary with real resourceVersion conflict behavior."""
    def __init__(self):
        self.deployments = {}
        self.writes = []
        self.version = 0
        self.read_error = None

    def read_namespaced_deployment(self, name, namespace):
        if self.read_error:
            raise self.read_error
        if (namespace, name) not in self.deployments:
            raise ApiException(status=404)
        return deepcopy(self.deployments[namespace, name])

    def _store(self, namespace, body):
        self.version += 1
        body = deepcopy(body)
        body.metadata.resource_version = str(self.version)
        self.deployments[namespace, body.metadata.name] = body
        self.writes.append((body.metadata.name, body.spec.replicas))

    def create_namespaced_deployment(self, namespace, body):
        if (namespace, body.metadata.name) in self.deployments:
            raise ApiException(status=409)
        self._store(namespace, body)

    def replace_namespaced_deployment(self, name, namespace, body):
        current = self.read_namespaced_deployment(name, namespace)
        if body.metadata.resource_version and body.metadata.resource_version != current.metadata.resource_version:
            raise ApiException(status=409)
        self._store(namespace, body)

    def patch_namespaced_deployment_scale(self, name, namespace, body):
        current = self.read_namespaced_deployment(name, namespace)
        current.spec.replicas = body['spec']['replicas']
        self._store(namespace, current)


@override_settings(
    CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    CORTEX_BASE_URL='https://cortex.invalid', HIVE_CORTEX_LEASE_API_KEY='test-only',
    SHIZUHA_PLATFORM_URL='', FLEET_AGENT_IMAGE='registry.invalid/runtime:qualified',
)
class LeaseStartAuthorityTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.apps = DeploymentAPI()
        self.trace = []
        self.workload = {}
        self.board = {'model': 'GLM-5.3-Flash', 'leased': True, 'max_holders': 1,
                      'admission_capacity': 2, 'holders': [],
                      'governed_principals': [], 'eligible_principals': []}
        self.config = [{'model_id': 'GLM-5.3-Flash', 'enabled': True,
                        'members': ['sara', 'kumo'], 'max_holders': 1,
                        'min_hold_seconds': 900, 'max_hold_seconds': 2700,
                        'idle_release_seconds': 180}]
        self.browser = False
        patches = {
            'fleet.provisioner.get_k8s_client': (Mock(), self.apps, Mock()),
            'fleet.provisioner._bind_reviewed_runtime_image': 'registry.invalid/runtime:qualified',
            'fleet.provisioner._daemon_routing_guard': None,
            'fleet.provisioner._ensure_namespace': None,
            'fleet.provisioner._create_identity_secret': None,
            'fleet.provisioner._create_workspace_pvc': None,
            'fleet.provisioner._ensure_coordinator_broker_token': True,
            'fleet.provisioner._ensure_projection_keyring': True,
            'fleet.provisioner.ensure_agent_cortex_key': 'test-key',
            'fleet.provisioner.ensure_agent_forgejo_token': None,
            'fleet.provisioner.ensure_agent_role_credentials': None,
            'fleet.provisioner.ensure_agent_id_password_aligned': 'aligned',
            'fleet.provisioner.ensure_agent_image': True,
            'fleet.provisioner._build_pod_spec': client.V1PodSpec(containers=[client.V1Container(name='agent', image='test')]),
            'fleet.provisioner._browser_worker_pod_spec': client.V1PodSpec(containers=[client.V1Container(name='browser', image='test')]),
            'fleet.provisioner._ensure_browser_namespace': None,
            'fleet.provisioner._ensure_browser_mcp_jwt_secrets': True,
            'fleet.provisioner._create_browser_worker_pvc': None,
            'fleet.provisioner._create_browser_worker_service': None,
            'fleet.lifecycle_fleet.list_runtime_agents': [],
            'fleet.model_lease.all_pool_configs': self.config,
            'fleet.model_serviceability.unserviceable_cortex_models': frozenset(),
            'fleet.lifecycle_fleet._best_effort_runtime_toggle': None,
        }
        self.mocks = {target: self.stack.enter_context(patch(target, return_value=value))
                      for target, value in patches.items()}
        self.stack.enter_context(patch('fleet.provisioner.agent_needs_browser_mcp', side_effect=lambda agent: self.browser))
        self.onboarding = self.stack.enter_context(patch(
            'fleet.provisioner._trigger_onboarding_async', side_effect=self.onboard,
        ))
        self.stack.enter_context(patch('requests.request', side_effect=self.cortex_request))

    def agent(self, name='sara', **kwargs):
        kwargs.setdefault('model', 'cortex/GLM-5.3-Flash')
        return FleetAgent.objects.create(
            agent_username=name, agent_id=name + '-runtime',
            identity_user_id=100 if name == 'sara' else 101,
            display_name=name.title(), **kwargs,
        )

    def onboard(self, agent):
        # The existing asynchronous Pulse producer is the external boundary.
        # Its real receipt becomes normal assigned demand on the next tick.
        self.workload[agent.agent_username + '@shizuha.com'] = {'open': 1}
        self.trace.append(('onboarding', agent.agent_username))

    def cortex_request(self, method, url, **kwargs):
        base = 'https://cortex.invalid/v1/admin/model-leases/GLM-5.3-Flash/'
        self.assertTrue(url.startswith(base), url)
        self.trace.append((method, url.removeprefix(base)))
        if method == 'PUT':
            field = {'governed/': 'governed_principals', 'eligible/': 'eligible_principals'}[url.removeprefix(base)]
            self.board[field] = kwargs['json']['principals']
        elif method == 'POST' and url == base + 'grant/':
            principal = kwargs['json']['holder']
            self.assertIn(principal, self.board['eligible_principals'])
            if len(self.board['holders']) >= self.board['max_holders']:
                return Mock(status_code=409, text='full')
            self.board['holders'].append({'principal': principal, 'lease_id': principal + '-lease'})
        elif method == 'POST' and url == base + 'yield-homes/':
            return Mock(status_code=200, json=Mock(return_value={'count': 0}))
        else:
            self.assertEqual((method, url), ('GET', base))
        return Mock(status_code=200, json=Mock(return_value=deepcopy(self.board)))

    def test_cold_bootstrap_demand_then_planner_grant_starts_only_selected_holder(self):
        self.assert_cold_bootstrap_then_planner_grant(model='cortex/GLM-5.3-Flash')

    @override_settings(FLEET_AGENT_MODEL='cortex/GLM-5.3-Flash')
    def test_blank_model_uses_launch_default_for_cold_wait_planner_and_start(self):
        self.assert_cold_bootstrap_then_planner_grant(model='')

    def assert_cold_bootstrap_then_planner_grant(self, *, model):
        self.browser = True
        agents = [self.agent(model=model), self.agent('kumo', model=model)]
        for agent in agents:
            self.assertFalse(provisioner.provision_agent(agent, agent_password='test-password'))
            self.assertEqual(agent.status, FleetAgent.Status.HIBERNATED)
            self.assertIsNotNone(agent.provisioned_at)
            self.assertIsNone(agent.last_active_at)
            self.assertEqual(_lease_wait_response(agent).status_code, 202)
        self.assertEqual(self.onboarding.call_count, 2)
        self.assertEqual(len(self.deployments()), 4)
        self.assertTrue(all(value.spec.replicas == 0 for value in self.deployments()))
        projected = list_lifecycle_agents()
        self.assertEqual({a['agent_username'] for a in projected}, {'sara', 'kumo'})
        self.assertEqual({a['effective_model'] for a in projected}, {'cortex/GLM-5.3-Flash'})
        self.assertFalse(any(event[0] in {'POST', 'PUT'} for event in self.trace))

        # The real planner chooses, grants through the real HTTP client,
        # re-reads the board and calls the real native lifecycle actuator.
        self.reconcile()
        self.assertEqual(len(self.board['holders']), 1)
        selected = self.board['holders'][0]['principal'].removeprefix('agent-')
        for agent in agents:
            agent.refresh_from_db()
            expected = 1 if agent.agent_username == selected else 0
            self.assertEqual(self.apps.read_namespaced_deployment(agent.deployment_name, provisioner.agent_namespace(agent)).spec.replicas, expected)
            self.assertEqual(self.apps.read_namespaced_deployment('browser-' + agent.agent_username, provisioner.browser_worker_namespace()).spec.replicas, expected)
            self.assertEqual(agent.status, FleetAgent.Status.RUNNING if expected else FleetAgent.Status.HIBERNATED)
        self.assertEqual(self.onboarding.call_count, 2)
        grant_idx = self.trace.index(('POST', 'grant/'))
        self.assertIn(('GET', ''), self.trace[grant_idx + 1:])

    def deployments(self):
        return list(self.apps.deployments.values())

    def reconcile(self, *, planner_failed=False):
        prefix = 'fleet.management.commands.reconcile_on_demand_agents.'
        with ExitStack() as stack:
            for target, value in {
                # Existing fleet roster; the two new agents are unrostered.
                prefix + 'Command._fetch_pulse_rosters': [{
                    'team_name': 'existing-team', 'is_active': True,
                    'tiers': [['other@shizuha.com']],
                }],
                prefix + 'Command._fetch_pulse_snapshot': {'workload': self.workload, 'team_workload': {}},
                prefix + 'Command._fetch_connect_state': ConnectLifecycleSnapshot(
                    available=True, unread_by_email={}, last_dm_at_by_email={}),
                prefix + 'Command._apply_health_signals': None,
                'fleet.external_agents.fresh_agent_telemetry': {},
                'fleet.models.archived_usernames': set(),
            }.items():
                stack.enter_context(patch(target, return_value=value))
            if planner_failed:
                stack.enter_context(patch(prefix + 'reconcile_model_lease', side_effect=TimeoutError('synthetic cache failure')))
            output = StringIO()
            call_command('reconcile_on_demand_agents', '--apply', stdout=output)
            return output.getvalue()

    def test_fallback_cannot_spend_one_free_slot_twice(self):
        for agent in (self.agent(), self.agent('kumo')):
            self.assertFalse(provisioner.provision_agent(agent, agent_password='test-password'))
        self.apps.writes.clear()
        self.trace.clear()
        output = self.reconcile(planner_failed=True)
        self.assertEqual(self.apps.writes, [])
        self.assertEqual(self.board['holders'], [])
        self.assertIn('DEFER START sara', output)
        self.assertIn('DEFER START kumo', output)
        self.assertEqual(self.trace, [('GET', ''), ('GET', '')])

    def test_lost_holder_between_plan_and_start_defers_without_retry_then_recovers(self):
        agent = self.agent()
        self.assertFalse(provisioner.provision_agent(agent, agent_password='test-password'))
        self.apps.writes.clear()
        real_start = provisioner.start_agent

        def lose_lease_before_start(row):
            self.assertEqual(self.board['holders'][0]['principal'], 'agent-sara')
            self.board['holders'] = []
            return real_start(row)

        with patch('fleet.lifecycle_fleet.provisioner.start_agent', side_effect=lose_lease_before_start) as start:
            output = self.reconcile()
        start.assert_called_once()
        self.assertIn('DEFER START sara', output)
        self.assertNotIn('RETRY', output)
        self.assertEqual(self.apps.writes, [])
        self.assertEqual(self.board['holders'], [])
        self.reconcile()
        agent.refresh_from_db()
        self.assertEqual(agent.status, FleetAgent.Status.RUNNING)
        self.assertEqual(self.board['holders'][0]['principal'], 'agent-sara')
        self.assertEqual(self.apps.read_namespaced_deployment(
            agent.deployment_name, provisioner.agent_namespace(agent),
        ).spec.replicas, 1)

    def test_gate_exception_does_not_scale_or_claim_pending(self):
        agent = self.agent(status=FleetAgent.Status.STOPPED)
        with patch('fleet.model_lease.lease_wake_available', side_effect=TimeoutError('synthetic cache failure')):
            self.assertFalse(provisioner.start_agent(agent))
        self.assertEqual(agent.status, FleetAgent.Status.STOPPED)
        self.assertIsNone(provisioner.lease_start_pending_reason(agent))
        self.assertEqual(agent._lease_start_decision.reason, 'gate_error_fail_closed')
        self.assertEqual(self.apps.writes, [])

    @override_settings(FLEET_AGENT_MODEL='')
    def test_unresolved_gateway_model_is_failure_but_external_bridge_default_starts(self):
        agent = self.agent(model='')
        self.assertFalse(provisioner.provision_agent(agent, agent_password='test-password'))
        self.assertEqual(agent._lease_start_decision.reason, 'model_unresolved')
        self.assertIsNone(provisioner.lease_start_pending_reason(agent))
        self.assertEqual(self.apps.writes, [])
        self.onboarding.assert_not_called()

        external = self.agent('kumo', model='', mcp_config={'execution_method': 'claude_code_server'})
        self.assertTrue(provisioner.provision_agent(external, agent_password='test-password'))
        self.assertEqual(external.status, FleetAgent.Status.RUNNING)
        self.assertEqual(self.apps.read_namespaced_deployment(
            external.deployment_name, provisioner.agent_namespace(external),
        ).spec.replicas, 1)
        self.assertEqual(self.trace, [('onboarding', 'kumo')])

    def test_automatic_cold_provision_preserves_operator_stop(self):
        agent = self.agent(status=FleetAgent.Status.STOPPED)
        self.assertFalse(provisioner.provision_agent(agent, agent_password='test-password'))
        self.assertEqual(agent.status, FleetAgent.Status.STOPPED)
        from fleet.model_lease import _agent_dict_auto_wakeable
        self.assertFalse(_agent_dict_auto_wakeable(list_lifecycle_agents()[0]))

    def test_positive_and_unknown_deployments_are_not_replaced_or_marked_hibernated(self):
        agent = self.agent(status=FleetAgent.Status.RUNNING)
        provisioner._create_deployment(self.apps, provisioner.agent_namespace(agent), agent)
        before = deepcopy(self.apps.deployments)
        self.apps.writes.clear()
        self.assertFalse(provisioner.provision_agent(agent, agent_password='test-password'))
        self.assertEqual(agent.status, FleetAgent.Status.RUNNING)
        self.assertEqual(self.apps.deployments, before)
        self.assertEqual(self.apps.writes, [])
        self.onboarding.assert_not_called()
        self.apps.read_error = ApiException(status=503)
        self.assertFalse(provisioner.provision_agent(agent, agent_password='test-password'))
        self.assertEqual(agent.status, FleetAgent.Status.RUNNING)
        self.assertIsNone(provisioner.lease_start_pending_reason(agent))
        self.assertEqual(self.apps.writes, [])

    def test_positive_browser_is_not_replaced_during_cold_agent_provision(self):
        self.browser = True
        agent = self.agent()
        provisioner._create_browser_worker_deployment(
            self.apps, provisioner.browser_worker_namespace(), agent, 'browser:test',
        )
        self.apps.writes.clear()
        self.assertFalse(provisioner.provision_agent(agent, agent_password='test-password'))
        self.assertEqual(agent.status, FleetAgent.Status.PENDING)
        self.assertEqual(self.apps.writes, [])
        self.assertEqual(self.apps.read_namespaced_deployment(
            'browser-sara', provisioner.browser_worker_namespace(),
        ).spec.replicas, 1)
        self.onboarding.assert_not_called()

    def test_cold_write_conflict_cannot_stop_a_concurrent_start(self):
        agent = self.agent()
        ns = provisioner.agent_namespace(agent)
        absent = provisioner._cold_deployment_state(self.apps, ns, agent.deployment_name)
        provisioner._create_deployment(self.apps, ns, agent)
        with self.assertRaises(ApiException):
            provisioner._create_deployment(self.apps, ns, agent, cold_state=absent)
        self.apps.patch_namespaced_deployment_scale(agent.deployment_name, ns, {'spec': {'replicas': 0}})
        sleeping = provisioner._cold_deployment_state(self.apps, ns, agent.deployment_name)
        self.apps.patch_namespaced_deployment_scale(agent.deployment_name, ns, {'spec': {'replicas': 1}})
        with self.assertRaises(ApiException):
            provisioner._create_deployment(self.apps, ns, agent, cold_state=sleeping)
        self.assertEqual(self.apps.read_namespaced_deployment(agent.deployment_name, ns).spec.replicas, 1)

    def test_enable_http_wait_is_distinct_from_true_failure(self):
        agent = self.agent(status=FleetAgent.Status.HIBERNATED)
        agent.provisioned_at = timezone.now()
        agent.save(update_fields=['provisioned_at'])
        request = APIRequestFactory().post('/enable/', {})
        force_authenticate(request, user=SimpleNamespace(is_authenticated=True, is_staff=True, id=1))
        response = FleetAgentEnableView.as_view()(request, agent_id=agent.id)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data['pending_sync_reason'], 'await_reconcile_grant')
        with patch('fleet.model_lease.lease_wake_available', side_effect=TimeoutError('synthetic')):
            response = FleetAgentEnableView.as_view()(request, agent_id=agent.id)
        self.assertEqual(response.status_code, 500)

    def test_explicit_enable_arms_a_confirmed_cold_operator_stopped_waiter(self):
        agent = self.agent(status=FleetAgent.Status.STOPPED)
        self.assertFalse(provisioner.provision_agent(agent, agent_password='test-password'))
        request = APIRequestFactory().post('/enable/', {})
        force_authenticate(request, user=SimpleNamespace(is_authenticated=True, is_staff=True, id=1))
        response = FleetAgentEnableView.as_view()(request, agent_id=agent.id)
        self.assertEqual(response.status_code, 202)
        agent.refresh_from_db()
        self.assertEqual(agent.status, FleetAgent.Status.HIBERNATED)
        self.assertEqual(agent.runtime_lifecycle_state, 'hibernated')
        self.assertTrue(all(dep.spec.replicas == 0 for dep in self.deployments()))
        self.reconcile()
        agent.refresh_from_db()
        self.assertEqual(agent.status, FleetAgent.Status.RUNNING)
        self.onboarding.assert_called_once()

    def test_create_api_returns_cold_resources_as_pending_not_failed_or_running(self):
        request = APIRequestFactory().post('/agents/', {
            'agent_username': 'newbie', 'agent_id': 'newbie-runtime',
            'display_name': 'Newbie', 'model': 'cortex/GLM-5.3-Flash',
            'agent_password': 'test-password',
        }, format='json')
        force_authenticate(request, user=SimpleNamespace(is_authenticated=True, is_staff=True, id=1))
        with patch('fleet.provisioner.bind_existing_identity_user'):
            response = FleetAgentListView.as_view()(request)
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(response.data['status'], 'hibernated')
        self.assertTrue(response.data['pending_sync'])
        self.assertEqual(response.data['pending_sync_reason'], 'await_reconcile_grant')
        self.assertIsNotNone(response.data['provisioned_at'])
        self.onboarding.assert_called_once()
        self.mocks['fleet.provisioner._create_identity_secret'].assert_called_once()
        self.mocks['fleet.provisioner._create_workspace_pvc'].assert_called_once()
        self.assertTrue(all(dep.spec.replicas == 0 for dep in self.deployments()))

    def test_explicit_enable_preserves_stop_intent_for_positive_and_unknown_runtime(self):
        agent = self.agent(status=FleetAgent.Status.STOPPED, runtime_lifecycle_state='operator_stopped')
        agent.provisioned_at = timezone.now()
        agent.save(update_fields=['provisioned_at'])
        provisioner._create_deployment(self.apps, provisioner.agent_namespace(agent), agent)
        self.apps.writes.clear()
        for error, expected_http in [(None, 202), (ApiException(status=503), 500)]:
            with self.subTest(read_error=error):
                self.apps.read_error = error
                request = APIRequestFactory().post('/enable/', {})
                force_authenticate(request, user=SimpleNamespace(is_authenticated=True, is_staff=True, id=1))
                response = FleetAgentEnableView.as_view()(request, agent_id=agent.id)
                self.assertEqual(response.status_code, expected_http)
                agent.refresh_from_db()
                self.assertEqual(agent.status, FleetAgent.Status.STOPPED)
                self.assertEqual(agent.runtime_lifecycle_state, 'operator_stopped')
                self.assertEqual(self.apps.writes, [])

    def test_hostplane_config_preserves_replicas_and_cannot_create_a_runtime(self):
        agent = self.agent(hostplane_ssh=True)
        ns = provisioner.agent_namespace(agent)
        with patch('fleet.provisioner.ensure_fleet_ssh_secret'):
            with self.assertRaises(ApiException):
                provisioner.apply_hostplane_ssh(agent)
            self.assertEqual(self.apps.writes, [])
            for replicas in (0, 1):
                with self.subTest(replicas=replicas):
                    if not self.apps.deployments:
                        provisioner._create_deployment(self.apps, ns, agent, replicas=replicas)
                    else:
                        self.apps.patch_namespaced_deployment_scale(agent.deployment_name, ns, {'spec': {'replicas': replicas}})
                    self.apps.writes.clear()
                    provisioner.apply_hostplane_ssh(agent)
                    self.assertEqual(self.apps.writes, [(agent.deployment_name, replicas)])
            self.apps.read_error = ApiException(status=503)
            self.apps.writes.clear()
            with self.assertRaises(ApiException):
                provisioner.apply_hostplane_ssh(agent)
            self.assertEqual(self.apps.writes, [])

    def test_hostplane_config_cannot_overwrite_a_concurrent_scale(self):
        agent = self.agent(hostplane_ssh=True)
        ns = provisioner.agent_namespace(agent)
        provisioner._create_deployment(self.apps, ns, agent, replicas=0)

        def concurrent_start(*args):
            self.apps.patch_namespaced_deployment_scale(agent.deployment_name, ns, {'spec': {'replicas': 1}})

        with patch('fleet.provisioner.ensure_fleet_ssh_secret', side_effect=concurrent_start):
            with self.assertRaises(ApiException):
                provisioner.apply_hostplane_ssh(agent)
        self.assertEqual(self.apps.read_namespaced_deployment(agent.deployment_name, ns).spec.replicas, 1)

    def test_connect_actuator_wait_and_failure_have_distinct_responses(self):
        self.agent(status=FleetAgent.Status.HIBERNATED)
        self.board['holders'] = [{'principal': 'agent-sara'}]
        for detail, expected in [
            ({'lease_waiting': True, 'reason': 'await_reconcile_grant'}, 202),
            ({'error': 'synthetic actuator failure'}, 502),
        ]:
            with self.subTest(detail=detail), patch.dict('os.environ', {'HIVE_CONNECT_WAKE_TOKEN': 'test-wake'}), patch(
                'fleet.views.set_lifecycle_agent_enabled', return_value=(False, detail),
            ) as actuator:
                request = APIRequestFactory().post('/connect-wake/', {'email': 'sara@shizuha.com'},
                                                  HTTP_X_HIVE_WAKE_TOKEN='test-wake')
                response = ConnectWakeAgentView.as_view()(request)
                self.assertEqual(response.status_code, expected)
                actuator.assert_called_once()
                self.assertNotEqual(response.data['status'], 'started')

    def test_connect_unknown_authority_is_error_before_actuation(self):
        self.agent(status=FleetAgent.Status.HIBERNATED)
        with patch.dict('os.environ', {'HIVE_CONNECT_WAKE_TOKEN': 'test-wake'}), patch(
            'requests.request', return_value=Mock(status_code=503, text='synthetic board outage'),
        ), patch('fleet.views.set_lifecycle_agent_enabled') as actuator:
            request = APIRequestFactory().post('/connect-wake/', {'email': 'sara@shizuha.com'},
                                              HTTP_X_HIVE_WAKE_TOKEN='test-wake')
            response = ConnectWakeAgentView.as_view()(request)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.data['reason'], 'board_unreadable')
            actuator.assert_not_called()

    def test_daemon_control_only_projects_typed_wait_as_pending(self):
        request = SimpleNamespace(user=SimpleNamespace(email='staff@test.invalid'))
        for detail, expected in [
            ({'lease_waiting': True, 'reason': 'await_reconcile_grant'}, 202),
            ({'error': 'lease_waiting in unrelated error prose'}, 502),
            ({'lease_waiting': 'true', 'reason': 'malformed'}, 502),
        ]:
            with self.subTest(detail=detail), patch('fleet.views.get_runtime_agent', return_value={
                'agent_username': 'sara', 'model': 'cortex/GLM-5.3-Flash',
            }), patch('fleet.views.set_runtime_agent_enabled', return_value=(False, detail)):
                response = _rt_fleet_control(request, 'test-agent-id', 'enable')
                self.assertEqual(response.status_code, expected)
