"""A broker added during wake must be reachable by its agent in that rollout."""
from copy import deepcopy
from unittest import mock

from django.test import SimpleTestCase, override_settings
from kubernetes import client

from fleet import provisioner
from fleet.models import FleetAgent


@override_settings(FLEET_COORDINATOR_MODEL_TOKEN_URL='')
class BrokerConsumerConvergenceTests(SimpleTestCase):
    def setUp(self):
        self.agent = FleetAgent(
            agent_username='scout', agent_id='scout-runtime-id',
            model='cortex/GLM-5.3-Flash',
            image='registry/shizuha-agent-runtime:reviewed', sidecar_set=['broker'],
        )
        self.agent.save = mock.Mock()
        self.agent._reviewed_runtime_image = self.agent.image
        self.apps = mock.Mock()
        for name, result in (
            ('ensure_agent_cortex_key', ''), ('ensure_agent_forgejo_token', ''),
            ('_ensure_coordinator_broker_token', True),
            ('_ensure_projection_keyring', True),
            ('get_k8s_client', (mock.Mock(), self.apps, mock.Mock())),
            ('_log_event', None),
        ):
            patcher = mock.patch.object(provisioner, name, return_value=result)
            patcher.start()
            self.addCleanup(patcher.stop)
        binding = mock.patch.object(provisioner, '_bind_reviewed_runtime_image', side_effect=lambda agent: agent.image)
        binding.start()
        self.addCleanup(binding.stop)
        self.apps.read_namespaced_deployment.side_effect = lambda *args: deepcopy(self.live)
        self.apps.replace_namespaced_deployment.side_effect = self.replace

    def replace(self, name, namespace, body):
        self.assertEqual(body.metadata.resource_version, self.live.metadata.resource_version)
        self.live = deepcopy(body)
        self.live.metadata.resource_version = str(int(body.metadata.resource_version) + 1)

    def legacy_deployment(self, *, has_broker=False, replicas=0):
        agent = provisioner._agent_container(self.agent, has_broker=False, has_dind=False)
        agent.env.append(client.V1EnvVar(name='USER_SETTING', value='preserved'))
        agent.volume_mounts.append(client.V1VolumeMount(name='user-data', mount_path='/user-data'))
        agent.liveness_probe = client.V1Probe(tcp_socket=client.V1TCPSocketAction(port=8080))
        agent.startup_probe = client.V1Probe(http_get=client.V1HTTPGetAction(path='/boot', port=8080))
        containers = [agent]
        if has_broker:
            containers.append(provisioner._broker_sidecar(self.agent))
        self.live = client.V1Deployment(
            metadata=client.V1ObjectMeta(
                name=self.agent.deployment_name, resource_version='11',
                labels={provisioner.NATIVE_RUNTIME_LABEL: provisioner.NATIVE_RUNTIME_LABEL_VALUE},
                annotations={provisioner.NATIVE_RUNTIME_OWNER_ANNOTATION: provisioner.NATIVE_RUNTIME_OWNER},
            ),
            spec=client.V1DeploymentSpec(
                replicas=replicas,
                selector=client.V1LabelSelector(match_labels={'app': 'fixture'}),
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels={
                        'app': 'fixture',
                        provisioner.NATIVE_RUNTIME_LABEL: provisioner.NATIVE_RUNTIME_LABEL_VALUE,
                    }),
                    spec=client.V1PodSpec(containers=containers, volumes=[
                        client.V1Volume(name='workspace', empty_dir=client.V1EmptyDirVolumeSource()),
                        client.V1Volume(name='user-data', empty_dir=client.V1EmptyDirVolumeSource()),
                    ]),
                ),
            ),
        )
        return deepcopy(agent)

    def assert_complete_contract(self, original, *, replicas):
        spec = self.live.spec.template.spec
        agent = next(c for c in spec.containers if c.name == 'agent')
        broker = next(c for c in spec.containers if c.name == 'broker')
        desired = provisioner._agent_container(self.agent, has_broker=True, has_dind=False)
        mounts = {m.mount_path: m for m in agent.volume_mounts}
        for mount in desired.volume_mounts:
            if mount.name in {'broker-sock', 'mcp-projection-keyring'}:
                self.assertIn(mount.mount_path, mounts)
                self.assertEqual(mounts[mount.mount_path].to_dict(), mount.to_dict())
                self.assertIn(mount.name, {v.name for v in spec.volumes})
        broker_socket = next(m for m in broker.volume_mounts if m.mount_path == provisioner.BROKER_SOCK_DIR)
        self.assertEqual(broker_socket.name, mounts[provisioner.BROKER_SOCK_DIR].name)
        env = {e.name: e for e in agent.env}
        self.assertEqual(env['MCP_AUTH_PROXY_SOCKET'].value, provisioner.BROKER_SOCK_PATH)
        self.assertEqual(env['AGENT_PASSWORD'].value_from.secret_key_ref.name, self.agent.identity_secret_name)
        self.assertEqual(env['USER_SETTING'].value, 'preserved')
        self.assertEqual(mounts['/user-data'].name, 'user-data')
        self.assertEqual(agent.liveness_probe.to_dict(), original.liveness_probe.to_dict())
        self.assertEqual(agent.startup_probe.to_dict(), original.startup_probe.to_dict())
        if desired.readiness_probe:
            self.assertEqual(agent.readiness_probe.to_dict(), desired.readiness_probe.to_dict())
        self.assertEqual(self.live.spec.replicas, replicas)
        self.apps.patch_namespaced_deployment_scale.assert_not_called()

    def assert_second_reconcile_is_noop(self):
        snapshot = self.live.to_dict()
        writes = self.apps.replace_namespaced_deployment.call_count
        self.assertFalse(provisioner.native_agent_runtime_contract_drift(self.agent, self.agent.image))
        self.assertTrue(provisioner.ensure_agent_image(self.agent))
        self.assertEqual(self.apps.replace_namespaced_deployment.call_count, writes)
        self.assertEqual(self.live.to_dict(), snapshot)

    def test_legacy_agent_gets_broker_and_complete_consumer_in_one_replace(self):
        # Scout/Yuki: the old template has no broker. Ordinary image convergence
        # used to add a sidecar and a wait-for-readyz command, but no socket mount.
        original = self.legacy_deployment()
        self.agent.sidecar_set = []
        self.assertTrue(provisioner.native_agent_runtime_contract_drift(self.agent, self.agent.image))
        self.assertTrue(provisioner.ensure_agent_image(self.agent))
        self.assertEqual(self.apps.replace_namespaced_deployment.call_count, 1)
        self.assert_complete_contract(original, replicas=0)
        self.assert_second_reconcile_is_noop()

    @override_settings(FLEET_BROKER_IMAGE='registry/mcp-auth-proxy:projection-capable')
    def test_broker_only_image_drift_is_replaced_without_runtime_or_replica_change(self):
        self.legacy_deployment(has_broker=True, replicas=1)
        self.assertTrue(provisioner.ensure_agent_image(self.agent))
        original = deepcopy(self.live)
        self.live.spec.template.spec.containers[1].image = 'registry/mcp-auth-proxy:before-projection'
        self.apps.replace_namespaced_deployment.reset_mock()
        self.assertTrue(provisioner.native_agent_runtime_contract_drift(self.agent, self.agent.image))
        self.assertTrue(provisioner.ensure_agent_image(self.agent))
        self.apps.replace_namespaced_deployment.assert_called_once()
        self.assertEqual(self.live.spec.template.spec.containers[1].image, 'registry/mcp-auth-proxy:projection-capable')
        self.assertEqual(self.live.spec.template.spec.containers[0].to_dict(), original.spec.template.spec.containers[0].to_dict())
        self.assertEqual(self.live.spec.replicas, 1)
        self.assert_second_reconcile_is_noop()

    def test_projection_producer_env_drift_repairs_exact_contract_preserving_unrelated(self):
        self.legacy_deployment(has_broker=True, replicas=0)
        self.assertTrue(provisioner.ensure_agent_image(self.agent))
        healthy = deepcopy(self.live)
        for defect in ('missing', 'wrong', 'duplicate'):
            with self.subTest(defect=defect):
                self.live = deepcopy(healthy)
                broker = self.live.spec.template.spec.containers[1]
                broker.env.append(client.V1EnvVar(name='USER_SETTING', value='preserved'))
                if defect == 'missing':
                    broker.env = [e for e in broker.env if e.name != 'MCP_AUTH_PROXY_PROJECTION_URL']
                elif defect == 'wrong':
                    next(e for e in broker.env if e.name == 'AGENT_ID').value = 'foreign-agent'
                else:
                    broker.env.append(client.V1EnvVar(name='MCP_AUTH_PROXY_PROJECTION_URL', value='http://foreign'))
                self.assertTrue(provisioner.native_agent_runtime_contract_drift(self.agent, self.agent.image))
                self.assertTrue(provisioner.ensure_agent_image(self.agent))
                current = self.live.spec.template.spec.containers[1]
                self.assertEqual(next(e.value for e in current.env if e.name == 'USER_SETTING'), 'preserved')
                self.assertEqual([e.to_dict() for e in current.env if e.name != 'USER_SETTING'],
                                 [e.to_dict() for e in provisioner._broker_sidecar(self.agent).env])
                self.assertEqual(self.live.spec.replicas, 0)
                self.assert_second_reconcile_is_noop()

    def test_existing_broker_repairs_consumer_and_missing_volume_preserving_sources(self):
        original = self.legacy_deployment(has_broker=True, replicas=1)
        retained = client.V1Volume(name='broker-sock', empty_dir=client.V1EmptyDirVolumeSource(
            medium='Memory', size_limit='32Mi',
        ))
        self.live.spec.template.spec.volumes.append(retained)
        agent = self.live.spec.template.spec.containers[0]
        agent.env.append(client.V1EnvVar(name='MCP_AUTH_PROXY_SOCKET', value='/stale/socket'))
        agent.volume_mounts.append(client.V1VolumeMount(name='broker-sock', mount_path='/diagnostics/socket'))
        self.assertTrue(provisioner.native_agent_runtime_contract_drift(self.agent, self.agent.image))
        self.assertTrue(provisioner.ensure_agent_image(self.agent))
        self.assert_complete_contract(original, replicas=1)
        volumes = {v.name: v for v in self.live.spec.template.spec.volumes}
        self.assertEqual(volumes['broker-sock'].to_dict(), retained.to_dict())
        self.assertIn('/diagnostics/socket', [m.mount_path for m in self.live.spec.template.spec.containers[0].volume_mounts])
        self.assert_second_reconcile_is_noop()

    def test_custom_image_keeps_its_command_and_readiness(self):
        self.agent.image = 'registry/custom-worker:stable'
        self.agent._reviewed_runtime_image = self.agent.image
        original = self.legacy_deployment(has_broker=True, replicas=1)
        agent = self.live.spec.template.spec.containers[0]
        agent.command = ['custom-worker', '--serve']
        agent.readiness_probe = client.V1Probe(http_get=client.V1HTTPGetAction(path='/custom-ready', port=8080))
        expected_probe = agent.readiness_probe.to_dict()
        self.assertTrue(provisioner.ensure_agent_image(self.agent))
        self.assert_complete_contract(original, replicas=1)
        agent = self.live.spec.template.spec.containers[0]
        self.assertEqual(agent.command, ['custom-worker', '--serve'])
        self.assertEqual(agent.readiness_probe.to_dict(), expected_probe)
        self.assert_second_reconcile_is_noop()

    def test_reserved_path_conflict_fails_without_replacing_or_scaling(self):
        self.legacy_deployment(has_broker=True, replicas=0)
        self.live.spec.template.spec.containers[0].volume_mounts.append(
            client.V1VolumeMount(name='user-data', mount_path=provisioner.BROKER_SOCK_DIR),
        )
        original = self.live.to_dict()
        self.assertTrue(provisioner.native_agent_runtime_contract_drift(self.agent, self.agent.image))
        self.assertFalse(provisioner.ensure_agent_image(self.agent))
        self.assertIn('reserved broker mount path conflicts', self.agent.status_message)
        self.apps.replace_namespaced_deployment.assert_not_called()
        self.apps.patch_namespaced_deployment_scale.assert_not_called()
        self.assertEqual(self.live.to_dict(), original)
        # Once the actual conflict is corrected, ordinary reconciliation clears
        # its banner and converges; an error must not poison the next wake.
        self.live.spec.template.spec.containers[0].volume_mounts = [
            m for m in self.live.spec.template.spec.containers[0].volume_mounts
            if m.mount_path != provisioner.BROKER_SOCK_DIR
        ]
        self.assertTrue(provisioner.ensure_agent_image(self.agent))
        self.assertEqual(self.agent.status_message, '')
        self.assert_second_reconcile_is_noop()

    def test_isolated_consumer_drift_is_detected_without_mutating_observed_state(self):
        self.legacy_deployment(has_broker=True, replicas=1)
        self.assertTrue(provisioner.ensure_agent_image(self.agent))
        healthy = deepcopy(self.live)
        for defect in ('mount', 'env', 'readiness', 'volume', 'conflict'):
            with self.subTest(defect=defect):
                self.live = deepcopy(healthy)
                spec = self.live.spec.template.spec
                agent = spec.containers[0]
                if defect == 'mount':
                    agent.volume_mounts = [m for m in agent.volume_mounts if m.name != 'broker-sock']
                elif defect == 'env':
                    agent.env = [e for e in agent.env if e.name != 'MCP_AUTH_PROXY_SOCKET']
                elif defect == 'readiness':
                    agent.readiness_probe = None
                elif defect == 'volume':
                    spec.volumes = [v for v in spec.volumes if v.name != 'mcp-projection-keyring']
                else:
                    next(m for m in agent.volume_mounts if m.name == 'broker-sock').name = 'user-data'
                snapshot = self.live.to_dict()
                self.agent.save.reset_mock()
                self.apps.replace_namespaced_deployment.reset_mock()
                self.assertTrue(provisioner.native_agent_runtime_contract_drift(self.agent, self.agent.image))
                self.assertEqual(self.live.to_dict(), snapshot)
                self.agent.save.assert_not_called()
                self.apps.replace_namespaced_deployment.assert_not_called()
                if defect != 'conflict':
                    self.assertTrue(provisioner.ensure_agent_image(self.agent))
                    self.assert_second_reconcile_is_noop()

    def test_built_seat_spec_carries_reconnect_state_volume_and_mount(self):
        # PLAT-12035: a full-replace build carries both halves of the writable
        # reconnect-state contract at the runtime's default path
        # (mcpReconnectStateDir() fallback), not just the container mount.
        # _affinity resolves the workspace PV's node affinity from the live
        # API; this fixture has no PV to bind, so stub it out.
        with mock.patch.object(provisioner, '_affinity', return_value=None):
            spec = provisioner._build_pod_spec(self.agent, forgejo_secret_name='fj')
        self.assertIn(provisioner.MCP_RECONNECT_STATE_VOLUME, {v.name for v in spec.volumes})
        volume = next(v for v in spec.volumes if v.name == provisioner.MCP_RECONNECT_STATE_VOLUME)
        self.assertIsNotNone(volume.empty_dir)
        agent = next(c for c in spec.containers if c.name == 'agent')
        mount = next(m for m in agent.volume_mounts
                     if m.name == provisioner.MCP_RECONNECT_STATE_VOLUME)
        self.assertEqual(mount.mount_path, provisioner.MCP_RECONNECT_STATE_DIR)

    def test_reconnect_state_volume_converges_on_patched_legacy_shape(self):
        # PLAT-12035: the full-replace builder carries the writable
        # mcp-reconnect-state volume; a patch-based repair of a legacy shape
        # must too, or the drift predicate reports drift forever (perpetual
        # reconcile churn instead of convergence).
        self.legacy_deployment(has_broker=True, replicas=1)
        self.assertTrue(provisioner.ensure_agent_image(self.agent))
        spec = self.live.spec.template.spec
        self.assertIn(provisioner.MCP_RECONNECT_STATE_VOLUME, {v.name for v in spec.volumes})
        agent = next(c for c in spec.containers if c.name == 'agent')
        mount = next(m for m in agent.volume_mounts
                     if m.name == provisioner.MCP_RECONNECT_STATE_VOLUME)
        self.assertEqual(mount.mount_path, provisioner.MCP_RECONNECT_STATE_DIR)
        self.assert_second_reconcile_is_noop()

    def test_reconnect_state_drift_requires_both_volume_and_mount(self):
        # PLAT-12035: the predicate must fire when either half is missing —
        # volume-only, mount-only, or both — and a single repair leg must
        # converge both halves so the second reconcile is a noop.
        self.legacy_deployment(has_broker=True)
        self.assertTrue(provisioner.ensure_agent_image(self.agent))
        healthy = deepcopy(self.live)
        for defect in ('volume-only', 'mount-only', 'both'):
            with self.subTest(defect=defect):
                self.live = deepcopy(healthy)
                spec = self.live.spec.template.spec
                agent = next(c for c in spec.containers if c.name == 'agent')
                if defect in ('volume-only', 'both'):
                    spec.volumes = [v for v in spec.volumes
                                    if v.name != provisioner.MCP_RECONNECT_STATE_VOLUME]
                if defect in ('mount-only', 'both'):
                    agent.volume_mounts = [m for m in agent.volume_mounts
                                           if m.name != provisioner.MCP_RECONNECT_STATE_VOLUME]
                self.assertTrue(provisioner.native_agent_runtime_contract_drift(
                    self.agent, self.agent.image))
                self.assertTrue(provisioner.ensure_agent_image(self.agent))
                self.assert_second_reconcile_is_noop()
