from types import SimpleNamespace
from unittest import mock

import yaml
from django.test import RequestFactory, TestCase
from kubernetes.client.rest import ApiException
from rest_framework.test import APIRequestFactory, force_authenticate

from fleet import provisioner, runtime_contract_metrics
from fleet.models import FleetAgent, FleetAgentEvent
from fleet.ui_views import FleetAgentEnableUIView
from fleet.views import FleetAgentEnableView


class NativeWakeFailureTests(TestCase):
    def setUp(self):
        self.agent = FleetAgent.objects.create(
            agent_username='kenta',
            agent_id='kenta-runtime-id',
            display_name='Kenta',
            status=FleetAgent.Status.STOPPED,
            image='registry/shizuha-agent-runtime:current',
        )
        self.agent._reviewed_runtime_image = self.agent.image

    @mock.patch('fleet.provisioner._notify_runtime_contract_reconcile_failure')
    @mock.patch('fleet.provisioner._scale')
    @mock.patch('fleet.provisioner.get_k8s_client')
    def test_read_api_exception_stays_stopped_and_notifies_owner_once(
        self, get_k8s_client, scale, notify,
    ):
        apps = mock.MagicMock()
        apps.read_namespaced_deployment.side_effect = ApiException(
            status=403,
            reason='Forbidden',
            http_resp=SimpleNamespace(
                status=403,
                reason='Forbidden',
                data='must-not-appear-in-event FORGEJO_TOKEN=secret',
                getheaders=lambda: {},
            ),
        )
        get_k8s_client.return_value = (mock.MagicMock(), apps, mock.MagicMock())

        self.assertFalse(provisioner.start_agent(self.agent))

        scale.assert_not_called()
        apps.patch_namespaced_deployment_scale.assert_not_called()
        notify.assert_called_once_with('read_deployment')
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.status, FleetAgent.Status.STOPPED)
        self.assertIn('agent kenta', self.agent.status_message)
        self.assertIn('stage read_deployment', self.agent.status_message)
        self.assertIn('wake aborted before scale', self.agent.status_message)
        self.assertNotIn('must-not-appear', self.agent.status_message)
        event = FleetAgentEvent.objects.get(
            agent=self.agent,
            event_type='runtime_contract_reconcile_failed',
        )
        self.assertEqual(event.message, self.agent.status_message)

    @mock.patch('fleet.provisioner._notify_runtime_contract_reconcile_failure')
    @mock.patch('fleet.provisioner._scale')
    @mock.patch('fleet.provisioner.get_k8s_client')
    def test_read_deployment_503_retries_then_succeeds(
        self, get_k8s_client, scale, notify,
    ):
        """k8s API 503 used to abort wake on the first GET (sara 2026-09-02)."""
        apps = mock.MagicMock()
        apps.read_namespaced_deployment.side_effect = [
            ApiException(status=503, reason='Service Unavailable'),
            self._stale_runtime_deployment(),
        ]
        get_k8s_client.return_value = (mock.MagicMock(), apps, mock.MagicMock())

        self.assertTrue(provisioner.start_agent(self.agent))

        self.assertEqual(apps.read_namespaced_deployment.call_count, 2)
        scale.assert_called_once()
        notify.assert_not_called()

    def _stale_runtime_deployment(self):
        """Fresh Deployment snapshot with a contract that still needs reconcile.

        Each read must return a new object graph: ensure_agent_image mutates the
        in-memory containers before patching, and a real Kubernetes re-read after
        a 409 would not see those uncommitted local mutations.
        """
        container = SimpleNamespace(
            name='agent',
            image='registry/shizuha-agent-runtime:old',
            image_pull_policy='IfNotPresent',
            env=[],
            resources=SimpleNamespace(requests={}, limits={}),
        )
        return SimpleNamespace(
            spec=SimpleNamespace(
                template=SimpleNamespace(
                    spec=SimpleNamespace(containers=[container]),
                ),
            ),
        )

    @mock.patch('fleet.provisioner._notify_runtime_contract_reconcile_failure')
    @mock.patch('fleet.provisioner._scale')
    @mock.patch('fleet.provisioner.get_k8s_client')
    def test_patch_api_exception_records_patch_stage_without_scaling(
        self, get_k8s_client, scale, notify,
    ):
        apps = mock.MagicMock()
        apps.read_namespaced_deployment.side_effect = (
            lambda *a, **k: self._stale_runtime_deployment()
        )
        apps.replace_namespaced_deployment.side_effect = ApiException(
            status=409, reason='Conflict',
        )
        get_k8s_client.return_value = (mock.MagicMock(), apps, mock.MagicMock())

        self.assertFalse(provisioner.start_agent(self.agent))

        scale.assert_not_called()
        notify.assert_called_once_with('patch_deployment')
        # Optimistic-concurrency 409s are retried with a fresh read before the
        # sticky UI failure is recorded.
        self.assertEqual(
            apps.replace_namespaced_deployment.call_count,
            provisioner._RUNTIME_CONTRACT_PATCH_ATTEMPTS,
        )
        self.assertEqual(
            apps.read_namespaced_deployment.call_count,
            provisioner._RUNTIME_CONTRACT_PATCH_ATTEMPTS,
        )
        self.agent.refresh_from_db()
        self.assertIn('stage patch_deployment', self.agent.status_message)

    @mock.patch('fleet.provisioner._notify_runtime_contract_reconcile_failure')
    @mock.patch('fleet.provisioner._scale')
    @mock.patch('fleet.provisioner.get_k8s_client')
    def test_patch_409_retries_then_succeeds_and_clears_stale_message(
        self, get_k8s_client, scale, notify,
    ):
        apps = mock.MagicMock()
        apps.read_namespaced_deployment.side_effect = (
            lambda *a, **k: self._stale_runtime_deployment()
        )
        apps.replace_namespaced_deployment.side_effect = [
            ApiException(status=409, reason='Conflict'),
            None,
        ]
        get_k8s_client.return_value = (mock.MagicMock(), apps, mock.MagicMock())
        self.agent.status_message = (
            'Runtime contract reconciliation failed for agent kenta at stage '
            'patch_deployment (HTTP 409 Conflict); wake aborted before scale.'
        )
        self.agent.save(update_fields=['status_message'])

        self.assertTrue(provisioner.start_agent(self.agent))

        self.assertEqual(apps.replace_namespaced_deployment.call_count, 2)
        self.assertEqual(apps.read_namespaced_deployment.call_count, 2)
        scale.assert_called_once_with(self.agent, 1)
        notify.assert_not_called()
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.status, FleetAgent.Status.RUNNING)
        self.assertEqual(self.agent.status_message, '')

    @mock.patch('fleet.provisioner._notify_runtime_contract_reconcile_failure')
    @mock.patch('fleet.provisioner._log_event')
    @mock.patch('fleet.provisioner.get_k8s_client')
    def test_successful_noop_reconcile_clears_stale_runtime_contract_message(
        self, get_k8s_client, _log, notify,
    ):
        apps = mock.MagicMock()
        agent = self.agent
        agent.image = 'registry/shizuha-agent-runtime:current'
        agent.status_message = (
            'Runtime contract reconciliation failed for agent kenta at stage '
            'patch_deployment (HTTP 409 Conflict); wake aborted before scale.'
        )
        agent.save(update_fields=['image', 'status_message'])
        desired = provisioner._resolve_resources(agent)
        desired_container = provisioner._agent_container(
            agent, has_broker=True, has_dind=False,
        )
        broker = provisioner._broker_sidecar(agent)
        container = SimpleNamespace(
            name='agent',
            image=agent.image,
            image_pull_policy='IfNotPresent',
            # A true no-op snapshot includes Hive's complete rendered runtime
            # lane. Omitting it now correctly exercises drift reconciliation.
            env=desired_container.env,
            ports=desired_container.ports,
            command=desired_container.command,
            volume_mounts=desired_container.volume_mounts,
            readiness_probe=desired_container.readiness_probe,
            resources=SimpleNamespace(
                requests=dict(desired.requests or {}),
                limits=dict(desired.limits or {}),
            ),
        )
        apps.read_namespaced_deployment.return_value = SimpleNamespace(
            metadata=provisioner.client.V1ObjectMeta(
                labels={
                    provisioner.NATIVE_RUNTIME_LABEL:
                        provisioner.NATIVE_RUNTIME_LABEL_VALUE,
                },
                annotations={
                    provisioner.NATIVE_RUNTIME_OWNER_ANNOTATION:
                        provisioner.NATIVE_RUNTIME_OWNER,
                },
            ),
            spec=SimpleNamespace(
                template=SimpleNamespace(
                    metadata=provisioner.client.V1ObjectMeta(labels={
                        provisioner.NATIVE_RUNTIME_LABEL:
                            provisioner.NATIVE_RUNTIME_LABEL_VALUE,
                    }),
                    spec=SimpleNamespace(
                        containers=[container, broker],
                        volumes=[
                            provisioner.client.V1Volume(
                                name='broker-sock',
                                empty_dir=provisioner.client.V1EmptyDirVolumeSource(),
                            ),
                            provisioner.client.V1Volume(
                                name='mcp-projection-keyring',
                                secret=provisioner.client.V1SecretVolumeSource(
                                    secret_name=provisioner.MCP_PROJECTION_PUBLIC_SECRET,
                                    default_mode=0o444,
                                    items=[provisioner.client.V1KeyToPath(
                                        key='keyring.json', path='keyring.json',
                                    )],
                                ),
                            ),
                        ],
                    ),
                ),
            ),
        )
        get_k8s_client.return_value = (mock.MagicMock(), apps, mock.MagicMock())

        self.assertTrue(provisioner.ensure_agent_image(agent))

        apps.replace_namespaced_deployment.assert_not_called()
        notify.assert_not_called()
        agent.refresh_from_db()
        self.assertEqual(agent.status_message, '')

    @mock.patch('fleet.views._get_agent')
    @mock.patch('fleet.views._require_staff', return_value=None)
    @mock.patch('fleet.views.provisioner.enable_or_provision_agent', return_value=False)
    def test_api_surfaces_persisted_reconcile_failure(
        self, _start, _staff, get_agent,
    ):
        self.agent.status_message = (
            'Runtime contract reconciliation failed for agent kenta at stage '
            'read_deployment (HTTP 403 Forbidden); wake aborted before scale.'
        )
        get_agent.return_value = self.agent
        request = APIRequestFactory().post(
            f'/v1/fleet/agents/{self.agent.id}/enable', {}, format='json',
        )
        force_authenticate(
            request,
            user=SimpleNamespace(
                id=1,
                email='staff@shizuha.com',
                username='staff',
                is_staff=True,
                is_authenticated=True,
            ),
        )

        response = FleetAgentEnableView.as_view()(request, agent_id=str(self.agent.id))

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.data['error'], self.agent.status_message)

    @mock.patch('fleet.ui_views._auth_user')
    @mock.patch('fleet.ui_views.provisioner.enable_or_provision_agent', return_value=False)
    def test_staff_ui_surfaces_same_reconcile_failure(self, _start, auth_user):
        self.agent.status_message = (
            'Runtime contract reconciliation failed for agent kenta at stage '
            'read_deployment (HTTP 403 Forbidden); wake aborted before scale.'
        )
        self.agent.save(update_fields=['status_message'])
        auth_user.return_value = (SimpleNamespace(is_staff=True), None)
        request = RequestFactory().post(
            f'/hive/fleet/agents/{self.agent.id}/enable/', {},
        )

        response = FleetAgentEnableUIView.as_view()(request, agent_id=self.agent.id)

        self.assertEqual(response.status_code, 500)
        self.assertIn(self.agent.status_message, response.content.decode())


class NativeWakeFailureAlertTests(TestCase):
    def setUp(self):
        for stage in runtime_contract_metrics.FAILURE_STAGES:
            runtime_contract_metrics.cache.delete(
                f'{runtime_contract_metrics._KEY}:{stage}'
            )

    def test_owner_notification_counter_increments_once_per_trigger(self):
        provisioner._notify_runtime_contract_reconcile_failure('read_deployment')

        self.assertEqual(
            runtime_contract_metrics.snapshot(),
            {'read_deployment': 1, 'patch_deployment': 0, 'unknown': 0},
        )
        rendered = runtime_contract_metrics.to_prometheus()
        self.assertIn(
            'hive_fleet_runtime_contract_reconcile_failed_total'
            '{stage="read_deployment"} 1',
            rendered,
        )

    def test_prometheus_rule_routes_failure_to_devops_owner(self):
        manifest = yaml.safe_load(
            open('k8s/hive-runtime-contract-alerts.yaml', encoding='utf-8')
        )
        [rule] = manifest['spec']['groups'][0]['rules']

        self.assertEqual(rule['alert'], 'HiveFleetRuntimeContractReconcileFailed')
        self.assertIn(
            'hive_fleet_runtime_contract_reconcile_failed_total',
            rule['expr'],
        )
        self.assertEqual(rule['labels']['team'], 'devops')
        self.assertEqual(rule['labels']['owner_group'], 'devops')
