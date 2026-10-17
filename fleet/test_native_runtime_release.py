"""Actual native actuation and API callers, with real ORM intent and Kubernetes wire objects."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from kubernetes import client
from kubernetes.client.rest import ApiException
from rest_framework.test import APIClient

from fleet import native_runtime_release as nr, provisioner as p
from fleet.models import FleetAgent, AgentRuntimeDesiredState, DaemonRegistry


def record(generation=27, digest=nr.ANCHOR_DIGEST, source=nr.ANCHOR_SOURCE, **kw):
    return dict(generation=generation, image_digest=digest,
                display_tag=f'localhost:30500/shizuha-agent-runtime:harness-native-{generation}',
                source_commit=source, approved_at='2026-09-09T00:00:00Z', intent='promote',
                rollback_of_generation=None, **kw)


@override_settings(HIVE_NATIVE_RUNTIME_RELEASE_ENABLED=True)
class NativeRuntimeReleaseCallerTests(TestCase):
    def setUp(self):
        self.history = [record()]
        self.agent = FleetAgent.objects.create(agent_username='native-sara', agent_id='native-sara-id',
            namespace='shizuha-fleet', image=self.anchor, model='cortex/GLM-5.3-Flash',
            mcp_config={'execution_method': 'shizuha'}, sidecar_set=['broker'], status='running')
        self.desired = AgentRuntimeDesiredState.objects.create(agent=self.agent, expected_deployment_uid='deployment-1')
        self.core, self.apps = mock.MagicMock(), mock.MagicMock()
        self.core.read_namespaced_persistent_volume_claim.return_value = client.V1PersistentVolumeClaim(
            metadata=client.V1ObjectMeta(uid='workspace-1'),
            spec=client.V1PersistentVolumeClaimSpec(),
            status=client.V1PersistentVolumeClaimStatus(phase='Pending'),
        )
        self.start_patch('fleet.provisioner.get_k8s_client', return_value=(self.core, self.apps, mock.MagicMock()))
        for name, value in [('ensure_agent_cortex_key', ''), ('ensure_agent_forgejo_token', ''),
                            ('_ensure_coordinator_broker_token', True), ('_ensure_projection_keyring', True),
                            ('agent_needs_browser_mcp', False), ('_log_event', None)]:
            self.start_patch('fleet.provisioner.' + name, return_value=value)
        self.lease = self.start_patch('fleet.provisioner._lease_start_decision', return_value=SimpleNamespace(allowed=True))
        with override_settings(HIVE_NATIVE_RUNTIME_RELEASE_ENABLED=False):
            self.agent._reviewed_runtime_image = self.anchor
            p._create_deployment(self.apps, 'shizuha-fleet', self.agent)
            self.deployment = deepcopy(self.apps.create_namespaced_deployment.call_args.args[1])
        self.deployment.metadata.uid = 'deployment-1'
        self.deployment.metadata.resource_version = '1'
        self.deployment.status = client.V1DeploymentStatus(ready_replicas=1)
        self.apps.read_namespaced_deployment.side_effect = lambda *a, **kw: deepcopy(self.deployment)
        self.apps.replace_namespaced_deployment.side_effect = self.replace
        self.core.read_namespaced_config_map.side_effect = lambda *a, **kw: self.cm()
        self.core.list_namespaced_pod.side_effect = lambda *a, **kw: SimpleNamespace(items=deepcopy(self.pods))
        self.core.read_namespaced_pod.side_effect = lambda *a, **kw: deepcopy(self.pods[0])
        self.rs = client.V1ReplicaSet(metadata=client.V1ObjectMeta(name='rs1', uid='rs-1',
            owner_references=[client.V1OwnerReference(api_version='apps/v1',kind='Deployment',name=self.agent.deployment_name,uid='deployment-1',controller=True)]))
        self.apps.read_namespaced_replica_set.return_value = self.rs
        self.apps.list_namespaced_replica_set.return_value = SimpleNamespace(items=[self.rs])
        self.apps.list_namespaced_deployment.side_effect = lambda *a, **kw: SimpleNamespace(items=[deepcopy(self.deployment)])
        self.pods = [self.pod()]
        self.stream = self.start_patch('fleet.provisioner.stream', side_effect=self.wire)
        self.start_patch('fleet.provisioner.time.sleep')
        # Canonicalize the existing non-release contract once through its actual
        # caller. The native27 adoption test then proves it makes zero writes.
        with override_settings(HIVE_NATIVE_RUNTIME_RELEASE_ENABLED=False):
            self.assertTrue(p.ensure_agent_image(self.agent))
        self.apps.reset_mock()
        self.lease.reset_mock()
        self.stream.reset_mock()

    @property
    def anchor(self):
        return 'localhost:30500/shizuha-agent-runtime@' + nr.ANCHOR_DIGEST

    def start_patch(self, target, **kwargs):
        patcher = mock.patch(target, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def cm(self):
        raw = json.dumps({'schema_version':1,'scope':nr.SCOPE,'desired_generation':self.history[-1]['generation'],'releases':self.history},sort_keys=True)
        return client.V1ConfigMap(data={nr.DOCUMENT_KEY:raw},metadata=client.V1ObjectMeta(uid='cm-1',resource_version='1',
            annotations={nr.HASH_ANNOTATION:hashlib.sha256(raw.encode()).hexdigest(),nr.SOURCE_ANNOTATION:'c'*40}))

    def pod(self):
        return client.V1Pod(metadata=client.V1ObjectMeta(name='pod1',uid='pod-1',
            owner_references=[client.V1OwnerReference(api_version='apps/v1',kind='ReplicaSet',name='rs1',uid='rs-1',controller=True)]),
            spec=client.V1PodSpec(containers=[client.V1Container(name='agent',image=self.anchor)]),
            status=client.V1PodStatus(phase='Running',conditions=[client.V1PodCondition(type='Ready',status='True')],
            container_statuses=[client.V1ContainerStatus(name='agent',image=self.anchor,image_id='native-arm-child',ready=True,restart_count=0,container_id='container-1')]))

    def promote(self):
        self.history.append(record(28,'sha256:'+'a'*64,'b'*40))

    def replace(self, name, ns, deployment, **kw):
        self.deployment = deepcopy(deployment)
        self.deployment.metadata.resource_version = str(int(deployment.metadata.resource_version) + 1)

    def wire(self, *args, **kw):
        import shlex
        command = kw['command'][-1]
        if '/health' in command:
            return json.dumps({'runtimeRollDrain':self.response})
        tokens = shlex.split(command)
        body = json.loads(tokens[tokens.index('--data-binary') + 1])
        self.response = dict(body, protocol=2, state='ready', busy=False, acceptingTurns=False,
                             ingressFenced=True, admissionVersion=4, pendingAcceptedTurns=1,
                             leaseUntil=time.time()*1000+60000)
        return json.dumps(self.response)+'\n__SHIZUHA_HTTP_STATUS__:200'

    def retained_terminal_pod(self):
        from datetime import datetime, timezone
        pod = self.pod()
        pod.metadata.name = 'retained-evicted'
        pod.metadata.uid = 'retained-uid'
        pod.status.phase = 'Failed'
        pod.status.reason = 'Evicted'
        pod.status.conditions = [client.V1PodCondition(type='Ready', status='False')]
        pod.spec.containers.append(client.V1Container(name='broker', image='broker'))
        pod.status.container_statuses = [client.V1ContainerStatus(
            name=name, image='old', image_id='old-id', ready=False, started=False,
            restart_count=0, container_id='containerd://' + name,
            state=client.V1ContainerState(terminated=client.V1ContainerStateTerminated(
                container_id='containerd://' + name, exit_code=143,
                started_at=datetime(2026, 10, 17, 8, 40, tzinfo=timezone.utc),
                finished_at=datetime(2026, 10, 17, 8, 44, tzinfo=timezone.utc), reason='Error')),
        ) for name in ('agent', 'broker')]
        return pod

    def test_retained_terminated_history_allows_actual_reserved_promotion(self):
        self.promote()
        self.pods.append(self.retained_terminal_pod())
        self.assertTrue(p.ensure_agent_image(self.agent))
        self.apps.replace_namespaced_deployment.assert_called_once()
        self.assertEqual(self.deployment.metadata.annotations[nr.GENERATION_ANNOTATION], '28')
        self.assertEqual(self.stream.call_count, 2)  # one arm, same-reservation final proof
        self.assertTrue(all(call.args[1] == 'pod1' for call in self.stream.call_args_list))

    def test_completed_init_and_ephemeral_history_is_accounted_for(self):
        self.promote()
        retained = self.retained_terminal_pod()
        retained.status.phase = 'Succeeded'
        for spec_field, status_field, name in (
                ('init_containers', 'init_container_statuses', 'init'),
                ('ephemeral_containers', 'ephemeral_container_statuses', 'debug')):
            container_type = client.V1EphemeralContainer if name == 'debug' else client.V1Container
            setattr(retained.spec, spec_field, [container_type(name=name)])
            status = deepcopy(retained.status.container_statuses[0])
            status.name = name
            status.container_id = 'containerd://' + name
            status.state.terminated.container_id = status.container_id
            setattr(retained.status, status_field, [status])
        self.pods.append(retained)
        self.assertTrue(p.ensure_agent_image(self.agent))
        self.apps.replace_namespaced_deployment.assert_called_once()

    def test_cold_start_with_proven_history_materializes_then_scales_with_cas(self):
        self.promote()
        self.deployment.spec.replicas = 0
        self.pods = [self.retained_terminal_pod()]
        self.agent.status = 'hibernated'
        self.agent.save(update_fields=['status'])
        with mock.patch('fleet.provisioner.ensure_agent_id_password_aligned', return_value='ok'):
            self.assertTrue(p.start_agent(self.agent))
        self.apps.replace_namespaced_deployment.assert_called_once()
        self.apps.patch_namespaced_deployment_scale.assert_called_once()
        self.assertEqual(self.apps.patch_namespaced_deployment_scale.call_args.kwargs['body'], {
            'metadata': {'uid': 'deployment-1', 'resourceVersion': '2'}, 'spec': {'replicas': 1}})
        self.stream.assert_not_called()

    def test_cold_stage_with_proven_history_keeps_replicas_zero_without_lease(self):
        self.promote()
        self.deployment.spec.replicas = 0
        self.pods = [self.retained_terminal_pod()]
        self.assertTrue(p.ensure_agent_image(self.agent))
        self.assertEqual(self.deployment.spec.replicas, 0)
        self.apps.replace_namespaced_deployment.assert_called_once()
        self.apps.patch_namespaced_deployment_scale.assert_not_called()
        self.lease.assert_not_called()
        self.stream.assert_not_called()

    def test_cold_materialization_rechecks_process_absence_before_cas(self):
        self.promote()
        self.deployment.spec.replicas = 0
        calls = []
        def observe(*args, **kwargs):
            retained = self.retained_terminal_pod()
            if calls:
                retained.status.container_statuses[1].state = client.V1ContainerState(
                    running=client.V1ContainerStateRunning())
            calls.append(True)
            return SimpleNamespace(items=[retained])
        self.core.list_namespaced_pod.side_effect = observe
        self.assertFalse(p.ensure_agent_image(self.agent))
        self.assertGreater(len(calls), 1)
        self.apps.replace_namespaced_deployment.assert_not_called()
        self.apps.patch_namespaced_deployment_scale.assert_not_called()

    def prepare_reviewed_cold_scale(self):
        self.deployment.spec.replicas = 0
        p._bind_reviewed_runtime_image(self.agent)
        authority = self.agent._native_runtime_release
        nr.stamp_release(authority, self.deployment)
        self.deployment.spec.template.spec.containers[0].image = authority.release.deployment_image

    def test_cold_scale_refuses_uncertain_terminal_processes_before_any_write(self):
        self.prepare_reviewed_cold_scale()
        for kind in ('running', 'missing-status', 'unknown-exit', 'foreign-owner', 'deleting', 'pending'):
            with self.subTest(kind=kind):
                retained = self.retained_terminal_pod()
                if kind == 'running':
                    retained.status.container_statuses[1].state = client.V1ContainerState(
                        running=client.V1ContainerStateRunning())
                elif kind == 'missing-status':
                    retained.status.container_statuses.pop()
                elif kind == 'unknown-exit':
                    retained.status.container_statuses[1].state.terminated.reason = 'ContainerStatusUnknown'
                elif kind == 'foreign-owner':
                    retained.metadata.owner_references[0].uid = 'foreign-rs'
                elif kind == 'deleting':
                    retained.metadata.deletion_timestamp = time.time()
                else:
                    retained.status.phase = 'Pending'
                self.pods = [retained]
                self.assertFalse(p._scale(self.agent, 1))
                self.apps.patch_namespaced_deployment_scale.assert_not_called()
                self.apps.replace_namespaced_deployment.assert_not_called()

    def test_cold_scale_rechecks_history_after_lease_and_source_reads(self):
        self.prepare_reviewed_cold_scale()
        calls = []
        def observe(*args, **kwargs):
            retained = self.retained_terminal_pod()
            if calls:
                retained.status.container_statuses[0].state = client.V1ContainerState(
                    running=client.V1ContainerStateRunning())
            calls.append(True)
            return SimpleNamespace(items=[retained])
        self.core.list_namespaced_pod.side_effect = observe
        self.assertFalse(p._scale(self.agent, 1))
        self.assertEqual(len(calls), 2)
        self.apps.patch_namespaced_deployment_scale.assert_not_called()

    def test_cold_start_history_does_not_override_lease_loss_after_materialization(self):
        self.promote()
        self.deployment.spec.replicas = 0
        self.pods = [self.retained_terminal_pod()]
        replace = self.replace
        def replaced(*args, **kwargs):
            replace(*args, **kwargs)
            self.lease.return_value = SimpleNamespace(allowed=False)
        self.apps.replace_namespaced_deployment.side_effect = replaced
        with mock.patch('fleet.provisioner.ensure_agent_id_password_aligned', return_value='ok'):
            self.assertFalse(p.start_agent(self.agent))
        self.apps.replace_namespaced_deployment.assert_called_once()
        self.apps.patch_namespaced_deployment_scale.assert_not_called()
        self.assertEqual(self.deployment.spec.replicas, 0)

    def mutate_on_final_history_owner_read(self, kind):
        reads = []
        def owner(*args, **kwargs):
            reads.append(kwargs.get('_request_timeout'))
            if len(reads) == 2:
                if kind == 'source':
                    self.history.append(record(self.history[-1]['generation'] + 1, 'sha256:' + 'c' * 64, 'd' * 40))
                elif kind == 'lease':
                    self.lease.return_value = SimpleNamespace(allowed=False)
            return self.rs
        self.apps.read_namespaced_replica_set.side_effect = owner
        return reads

    def test_cold_scale_source_change_during_final_owner_read_refuses(self):
        self.prepare_reviewed_cold_scale()
        self.pods = [self.retained_terminal_pod()]
        reads = self.mutate_on_final_history_owner_read('source')
        with self.assertRaises(nr.RuntimeReleaseProjectionUnavailable):
            p._scale(self.agent, 1)
        self.assertEqual(len(reads), 2)
        self.apps.patch_namespaced_deployment_scale.assert_not_called()

    def test_cold_scale_lease_loss_during_final_owner_read_refuses(self):
        self.prepare_reviewed_cold_scale()
        self.pods = [self.retained_terminal_pod()]
        reads = self.mutate_on_final_history_owner_read('lease')
        self.assertFalse(p._scale(self.agent, 1))
        self.assertEqual(len(reads), 2)
        self.apps.patch_namespaced_deployment_scale.assert_not_called()

    def test_history_owner_reads_use_existing_api_transport_bound(self):
        self.prepare_reviewed_cold_scale()
        self.pods = [self.retained_terminal_pod()]
        reads = self.mutate_on_final_history_owner_read('unchanged')
        self.assertTrue(p._scale(self.agent, 1))
        self.assertEqual(reads, [(3, 5), (3, 5)])

    def test_cold_materialization_source_change_during_final_owner_read_refuses(self):
        self.promote()
        self.deployment.spec.replicas = 0
        self.pods = [self.retained_terminal_pod()]
        reads = self.mutate_on_final_history_owner_read('source')
        with self.assertRaises(nr.RuntimeReleaseProjectionUnavailable):
            p.ensure_agent_image(self.agent)
        self.assertEqual(len(reads), 2)
        self.apps.replace_namespaced_deployment.assert_not_called()
        self.apps.patch_namespaced_deployment_scale.assert_not_called()

    def test_cold_materialization_lease_loss_during_final_owner_read_refuses(self):
        self.promote()
        self.deployment.spec.replicas = 0
        self.deployment.spec.template.spec.containers[0].image = 'localhost:30500/shizuha-agent-runtime:older'
        self.pods = [self.retained_terminal_pod()]
        reads = self.mutate_on_final_history_owner_read('lease')
        with mock.patch('fleet.provisioner.ensure_agent_id_password_aligned', return_value='ok'):
            self.assertFalse(p.start_agent(self.agent))
        self.apps.replace_namespaced_deployment.assert_not_called()
        self.apps.patch_namespaced_deployment_scale.assert_not_called()
        self.assertEqual(len(reads), 2)

    def test_retained_history_foreign_deployment_or_unreadable_owner_refuses(self):
        self.promote()
        self.pods.append(self.retained_terminal_pod())
        for mode in ('foreign-deployment', 'unreadable-rs'):
            with self.subTest(mode=mode):
                if mode == 'foreign-deployment':
                    self.rs.metadata.owner_references[0].uid = 'foreign-deployment'
                else:
                    self.apps.read_namespaced_replica_set.side_effect = ApiException(status=404)
                self.assertFalse(p.ensure_agent_image(self.agent))
                self.stream.assert_not_called()
                self.apps.replace_namespaced_deployment.assert_not_called()

    def test_uncertain_history_never_arms_or_mutates(self):
        mutations = {
            'pending': lambda pod: setattr(pod.status, 'phase', 'Pending'),
            'unknown': lambda pod: setattr(pod.status, 'phase', 'Unknown'),
            'deleting': lambda pod: setattr(pod.metadata, 'deletion_timestamp', time.time()),
            'ready': lambda pod: setattr(pod.status.conditions[0], 'status', 'True'),
            'missing-status': lambda pod: pod.status.container_statuses.pop(),
            'running': lambda pod: setattr(pod.status.container_statuses[1], 'state',
                                           client.V1ContainerState(running=client.V1ContainerStateRunning())),
            'waiting': lambda pod: setattr(pod.status.container_statuses[1], 'state',
                                           client.V1ContainerState(waiting=client.V1ContainerStateWaiting())),
            'started': lambda pod: setattr(pod.status.container_statuses[1], 'started', True),
            'missing-finish': lambda pod: setattr(pod.status.container_statuses[1].state.terminated, 'finished_at', None),
            'unknown-termination': lambda pod: setattr(pod.status.container_statuses[1].state.terminated,
                                                       'reason', 'ContainerStatusUnknown'),
            'duplicate-process-id': lambda pod: (
                setattr(pod.status.container_statuses[1], 'container_id', 'containerd://agent'),
                setattr(pod.status.container_statuses[1].state.terminated, 'container_id', 'containerd://agent')),
            'changed-container': lambda pod: setattr(pod.status.container_statuses[1].state.terminated,
                                                     'container_id', 'different'),
            'foreign-owner': lambda pod: setattr(pod.metadata.owner_references[0], 'uid', 'foreign-rs'),
            'missing-owner': lambda pod: setattr(pod.metadata, 'owner_references', []),
            'unobserved-init': lambda pod: setattr(pod.spec, 'init_containers', [client.V1Container(name='init')]),
            'unobserved-ephemeral': lambda pod: setattr(pod.spec, 'ephemeral_containers',
                                                        [client.V1EphemeralContainer(name='debug')]),
        }
        self.promote()
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                retained = self.retained_terminal_pod()
                mutate(retained)
                self.pods = [self.pod(), retained]
                self.assertFalse(p.ensure_agent_image(self.agent))
                self.apps.replace_namespaced_deployment.assert_not_called()
                self.stream.assert_not_called()

    def test_history_appearing_after_drain_refuses_final_cas(self):
        self.promote()
        self.pods.append(self.retained_terminal_pod())
        wire = self.wire
        def change(*args, **kwargs):
            result = wire(*args, **kwargs)
            self.pods[1].status.container_statuses[1].state = client.V1ContainerState(
                running=client.V1ContainerStateRunning())
            return result
        self.stream.side_effect = change
        self.assertFalse(p.ensure_agent_image(self.agent))
        self.stream.assert_called_once()
        self.apps.replace_namespaced_deployment.assert_not_called()

    def test_bootstrap_same_image_makes_no_mutation_or_drain_or_lease_call(self):
        self.assertTrue(p.ensure_agent_image(self.agent))
        self.apps.replace_namespaced_deployment.assert_not_called()
        self.stream.assert_not_called()
        self.lease.assert_not_called()
        self.assertNotIn(nr.GENERATION_ANNOTATION,self.deployment.metadata.annotations)

    def test_api_defaulted_tcp_ports_do_not_rearm_same_image_drain(self):
        for container in self.deployment.spec.template.spec.containers:
            for port in container.ports or []:
                if port.protocol is None:
                    port.protocol = 'TCP'
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                self.assertTrue(p.ensure_agent_image(self.agent))
                self.apps.replace_namespaced_deployment.assert_not_called()
                self.stream.assert_not_called()
                self.lease.assert_not_called()

    def test_real_promotion_remains_noop_after_server_defaulting(self):
        replace = self.replace

        def server_defaulted_replace(*args, **kwargs):
            result = replace(*args, **kwargs)
            for container in self.deployment.spec.template.spec.containers:
                for port in container.ports or []:
                    if port.protocol is None:
                        port.protocol = 'TCP'
            return result

        self.apps.replace_namespaced_deployment.side_effect = server_defaulted_replace
        self.promote()
        self.assertTrue(p.ensure_agent_image(self.agent))
        self.assertEqual(self.apps.replace_namespaced_deployment.call_count, 1)
        self.apps.reset_mock()
        self.stream.reset_mock()
        self.assertTrue(p.ensure_agent_image(self.agent))
        self.apps.replace_namespaced_deployment.assert_not_called()
        self.stream.assert_not_called()

    def load_apiserver_container_fixture(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures' /
                              'native_runtime_apiserver_container.json').read_text())
        for name, value in fixture['agent'].items():
            if name != 'image':
                setattr(self.agent, name, value)
        self.agent.save()
        self.start_patch('fleet.provisioner._coordinator_model_token_enabled',
                         return_value=fixture['coordinator_enabled'])
        platform = override_settings(SHIZUHA_PLATFORM_URL=fixture['platform_url'])
        platform.enable()
        self.addCleanup(platform.disable)
        response = SimpleNamespace(data=json.dumps(fixture['container']))
        observed = client.ApiClient().deserialize(response, 'V1Container')
        observed.image = self.anchor
        containers = self.deployment.spec.template.spec.containers
        containers[next(index for index, container in enumerate(containers)
                        if container.name == 'agent')] = observed
        # This capture replaces only the agent container and changes platform
        # settings. Keep the companion producer coherent so consumer-only
        # no-op assertions do not conceal legitimate broker contract drift.
        containers[next(index for index, container in enumerate(containers)
                        if container.name == 'broker')] = p._broker_sidecar(self.agent)
        return observed

    def test_captured_api_container_repeated_converge_makes_no_write_or_drain(self):
        observed = self.load_apiserver_container_fixture()
        # This captured container predates purpose grants. An unconstrained
        # Mock returning a Secret for every name would now represent a newly
        # provisioned grant and correctly require a credential-reference roll.
        self.core.read_namespaced_secret.side_effect = ApiException(status=404)
        desired = p._agent_container(self.agent, has_broker=True, has_dind=False)
        self.assertFalse(p._converge_broker_consumer_contract(deepcopy(observed), desired))
        before = deepcopy(self.deployment.to_dict())
        for attempt in range(3):
            with self.subTest(attempt=attempt):
                self.assertTrue(p.ensure_agent_image(self.agent))
                self.apps.replace_namespaced_deployment.assert_not_called()
                self.stream.assert_not_called()
                self.lease.assert_not_called()
                self.assertEqual(self.deployment.to_dict(), before)

    def test_captured_api_container_real_probe_drift_converges_once(self):
        observed = self.load_apiserver_container_fixture()
        observed.readiness_probe.success_threshold = 2
        self.assertTrue(p.ensure_agent_image(self.agent))
        self.apps.replace_namespaced_deployment.assert_called_once()
        self.assertGreater(self.stream.call_count, 0)
        current = next(container for container in self.deployment.spec.template.spec.containers
                       if container.name == 'agent')
        self.assertEqual(current.readiness_probe.success_threshold, 1)
        self.apps.reset_mock()
        self.stream.reset_mock()
        self.assertTrue(p.ensure_agent_image(self.agent))
        self.apps.replace_namespaced_deployment.assert_not_called()
        self.stream.assert_not_called()

    def test_captured_api_container_real_image_roll_keeps_server_defaults(self):
        self.load_apiserver_container_fixture()
        self.promote()
        self.assertTrue(p.ensure_agent_image(self.agent))
        self.apps.replace_namespaced_deployment.assert_called_once()
        current = next(container for container in self.deployment.spec.template.spec.containers
                       if container.name == 'agent')
        self.assertEqual(current.readiness_probe.success_threshold, 1)
        self.assertEqual(current.termination_message_policy, 'File')
        self.apps.reset_mock()
        self.stream.reset_mock()
        self.assertTrue(p.ensure_agent_image(self.agent))
        self.apps.replace_namespaced_deployment.assert_not_called()
        self.stream.assert_not_called()

    def test_real_caller_promotes_main_and_runtime_init_together_and_second_pass_is_noop(self):
        self.deployment.spec.template.spec.init_containers=[client.V1Container(name='runtime-init',image=self.anchor)]
        self.promote()
        self.assertTrue(p.ensure_agent_image(self.agent))
        self.assertEqual(self.apps.replace_namespaced_deployment.call_count,1)
        self.assertEqual(self.deployment.metadata.annotations[nr.GENERATION_ANNOTATION],'28')
        self.assertEqual(self.deployment.spec.template.spec.init_containers[0].image,self.deployment.spec.template.spec.containers[0].image)
        self.apps.reset_mock();self.stream.reset_mock()
        self.assertTrue(p.ensure_agent_image(self.agent))
        self.apps.replace_namespaced_deployment.assert_not_called();self.stream.assert_not_called()

    def test_busy_continuation_arms_once_then_gets_same_reservation_without_extending(self):
        self.promote()
        wire=self.wire
        def delayed(*a,**kw):
            raw=wire(*a,**kw)
            if '/health' not in kw['command'][-1]:
                body=dict(self.response,state='draining',busy=True,acceptingTurns=True,ingressFenced=False)
                return json.dumps(body)+'\n__SHIZUHA_HTTP_STATUS__:200'
            return raw
        self.stream.side_effect=delayed
        self.assertTrue(p.ensure_agent_image(self.agent))
        self.assertEqual(self.stream.call_count,3)
        self.assertIn('/health',self.stream.call_args.kwargs['command'][-1])

    def test_missing_cm_after_activation_cannot_fall_back_to_bound_image(self):
        self.core.read_namespaced_config_map.side_effect=ApiException(status=404)
        with self.assertRaises(nr.RuntimeReleaseProjectionUnavailable):p.ensure_agent_image(self.agent)
        self.apps.replace_namespaced_deployment.assert_not_called();self.stream.assert_not_called()

    def test_deleting_cm_refuses_before_credentials_or_runtime_mutation(self):
        from django.utils import timezone
        authority = self.cm()
        authority.metadata.deletion_timestamp = timezone.now()
        self.core.read_namespaced_config_map.side_effect = lambda *a, **kw: authority
        with mock.patch('fleet.provisioner.ensure_agent_cortex_key') as credential:
            with self.assertRaises(nr.RuntimeReleaseProjectionUnavailable):
                p.ensure_agent_image(self.agent)
        credential.assert_not_called()
        self.apps.replace_namespaced_deployment.assert_not_called()
        self.stream.assert_not_called()

    def test_changed_cm_at_final_boundary_refuses_without_write(self):
        self.promote();wire=self.wire
        def changed(*a,**kw):
            result=wire(*a,**kw);self.history.append(record(29,'sha256:'+'d'*64,'e'*40));return result
        self.stream.side_effect=changed
        with self.assertRaises(nr.RuntimeReleaseProjectionUnavailable):p.ensure_agent_image(self.agent)
        self.apps.replace_namespaced_deployment.assert_not_called()

    def test_lost_lease_after_drain_still_rolls_in_place(self):
        # Start-lease is a wake gate. An already-running home must still take
        # the reviewed harness even if it is not a current holder.
        self.promote();wire=self.wire
        def changed(*a,**kw):
            result=wire(*a,**kw);self.lease.return_value=SimpleNamespace(allowed=False);return result
        self.stream.side_effect=changed
        self.assertTrue(p.ensure_agent_image(self.agent))
        self.assertEqual(self.apps.replace_namespaced_deployment.call_count,1)
        self.assertEqual(self.deployment.metadata.annotations[nr.GENERATION_ANNOTATION],'28')

    def test_in_place_roll_does_not_require_start_lease(self):
        self.promote()
        self.lease.return_value=SimpleNamespace(allowed=False)
        self.assertTrue(p.ensure_agent_image(self.agent))
        self.assertEqual(self.apps.replace_namespaced_deployment.call_count,1)
        self.assertEqual(self.deployment.metadata.annotations[nr.GENERATION_ANNOTATION],'28')

    def test_changed_desired_generation_refuses_without_completing_work(self):
        self.promote();wire=self.wire
        def changed(*a,**kw):
            result=wire(*a,**kw);AgentRuntimeDesiredState.objects.filter(pk=self.desired.pk).update(desired_generation=2);return result
        self.stream.side_effect=changed
        with self.assertRaises(nr.RuntimeReleaseProjectionUnavailable):p.ensure_agent_image(self.agent)
        self.apps.replace_namespaced_deployment.assert_not_called()

    def test_pod_replacement_or_container_restart_invalidates_receipt(self):
        for field,value in [('uid','pod-2'),('container_id','container-2')]:
            with self.subTest(field=field):
                self.pods=[self.pod()];self.history=[record()];self.promote();wire=self.wire
                def changed(*a,**kw):
                    result=wire(*a,**kw)
                    if field=='uid':self.pods[0].metadata.uid=value
                    else:self.pods[0].status.container_statuses[0].container_id=value
                    return result
                self.stream.side_effect=changed
                self.assertFalse(p.ensure_agent_image(self.agent));self.apps.replace_namespaced_deployment.assert_not_called()

    def test_scaled_zero_with_remaining_pod_is_not_a_safe_boundary(self):
        self.promote();self.deployment.spec.replicas=0
        self.assertFalse(p.ensure_agent_image(self.agent));self.apps.replace_namespaced_deployment.assert_not_called();self.stream.assert_not_called()

    def test_existing_stop_or_config_path_can_stage_known_cold_image_without_wake(self):
        self.promote();self.deployment.spec.replicas=0;self.pods=[]
        self.assertTrue(p.ensure_agent_image(self.agent));self.assertEqual(self.deployment.spec.replicas,0)
        self.lease.assert_not_called();self.stream.assert_not_called()

    def test_unknown_or_404_protocol_refuses_active_roll(self):
        self.promote();self.stream.side_effect=None;self.stream.return_value='{}\n__SHIZUHA_HTTP_STATUS__:404'
        self.assertFalse(p.ensure_agent_image(self.agent));self.apps.replace_namespaced_deployment.assert_not_called()

    def test_expired_reservation_refuses(self):
        self.promote();wire=self.wire
        def expired(*a,**kw):
            wire(*a,**kw);self.response['leaseUntil']=time.time()*1000-1
            return json.dumps(self.response)+'\n__SHIZUHA_HTTP_STATUS__:200'
        self.stream.side_effect=expired
        self.assertFalse(p.ensure_agent_image(self.agent));self.apps.replace_namespaced_deployment.assert_not_called()

    def test_foreign_legacy_deployment_refused_before_credentials(self):
        self.deployment.metadata.labels['app.kubernetes.io/managed-by']='shizuha-runtime-fleet'
        with self.assertRaises(nr.RuntimeReleaseProjectionUnavailable):p.ensure_agent_image(self.agent)
        self.apps.replace_namespaced_deployment.assert_not_called();self.stream.assert_not_called()

    def test_actual_snapshot_selects_daemon_linked_hive_owned_row_and_preserves_foreign_legacy(self):
        daemon=DaemonRegistry.objects.create(daemon_id='legacy',fleet_id='legacy',runtime='k8s')
        FleetAgent.objects.filter(pk=self.agent.pk).update(managing_daemon=daemon)
        foreign=deepcopy(self.deployment);foreign.metadata.name='agent-codex';foreign.metadata.uid='legacy-1'
        foreign.metadata.labels['app.kubernetes.io/managed-by']='shizuha-runtime-fleet'
        self.apps.list_namespaced_deployment.return_value=None
        self.apps.list_namespaced_deployment.side_effect=lambda *a,**kw:SimpleNamespace(items=[deepcopy(self.deployment),foreign])
        snapshot=nr.runtime_snapshot()
        self.assertEqual([r['username'] for r in snapshot['census']],[self.agent.agent_username])
        self.assertEqual(snapshot['excluded'][0]['uid'],'legacy-1')
        self.apps.replace_namespaced_deployment.assert_not_called();self.stream.assert_not_called()

    def test_api_native_authority_is_independent_of_missing_legacy_daemon(self):
        user=get_user_model().objects.create_user(username='native-admin',is_staff=True)
        api=APIClient();api.force_authenticate(user=user)
        self.promote()
        response=api.get('/api/v1/fleet/native-runtime-image')
        self.assertEqual(response.status_code,200);self.assertEqual(response.data['generation'],28)
        self.assertEqual(api.post('/api/v1/fleet/native-runtime-image',{}).status_code,409)

    def test_conflict_reacquires_drain_and_fresh_resource_version(self):
        self.promote();calls=[]
        def conflict(name,ns,deployment,**kw):
            calls.append(deployment.metadata.resource_version)
            if len(calls)==1:
                self.deployment.metadata.resource_version='2'
                raise ApiException(status=409)
            self.replace(name,ns,deployment,**kw)
        self.apps.replace_namespaced_deployment.side_effect=conflict
        self.assertTrue(p.ensure_agent_image(self.agent))
        self.assertEqual(calls,['1','2'])
        self.assertEqual(len([c for c in self.stream.call_args_list if 'rollout-drain' in c.kwargs['command'][-1]]),2)

    def test_source_change_after_conflict_cannot_authorize_old_target(self):
        self.promote()
        def conflict(*a,**kw):
            self.history.append(record(29,'sha256:'+'d'*64,'e'*40))
            raise ApiException(status=409)
        self.apps.replace_namespaced_deployment.side_effect=conflict
        with self.assertRaises(nr.RuntimeReleaseProjectionUnavailable):p.ensure_agent_image(self.agent)
        self.assertEqual(self.apps.replace_namespaced_deployment.call_count,1)

    def test_extra_pending_pod_after_drain_invalidates_fence(self):
        self.promote();wire=self.wire
        def extra(*a,**kw):
            result=wire(*a,**kw);pod=self.pod();pod.metadata.uid='extra';pod.status.phase='Pending';self.pods.append(pod);return result
        self.stream.side_effect=extra
        self.assertFalse(p.ensure_agent_image(self.agent));self.apps.replace_namespaced_deployment.assert_not_called()

    def test_no_pod_stage_rechecks_arrival_before_replace(self):
        self.promote();self.deployment.spec.replicas=0;self.pods=[];calls=[]
        def pods(*a,**kw):
            calls.append(1)
            return SimpleNamespace(items=[] if len(calls)==1 else [self.pod()])
        self.core.list_namespaced_pod.side_effect=pods
        self.assertFalse(p.ensure_agent_image(self.agent));self.apps.replace_namespaced_deployment.assert_not_called()

    def test_blocking_final_read_that_exhausts_lease_refuses_mutation(self):
        self.promote();clock=[10.0]
        self.start_patch('fleet.provisioner.time.monotonic',side_effect=lambda:clock[0])
        wire=self.wire;read_cm=self.cm
        def source(*a,**kw):
            if self.stream.call_count:clock[0]=71.0
            return read_cm()
        self.core.read_namespaced_config_map.side_effect=source
        self.assertFalse(p.ensure_agent_image(self.agent));self.apps.replace_namespaced_deployment.assert_not_called()

    def test_mutation_timeout_is_bounded_by_original_lease(self):
        self.promote();self.assertTrue(p.ensure_agent_image(self.agent))
        timeout=self.apps.replace_namespaced_deployment.call_args.kwargs['_request_timeout']
        self.assertGreater(timeout,0);self.assertLessEqual(timeout,60)

    def test_busy_forever_finishes_original_lease_without_rearming(self):
        self.promote();mono=[10.0];wall=[time.time()]
        self.start_patch('fleet.provisioner.time.monotonic',side_effect=lambda:mono[0])
        self.start_patch('fleet.provisioner.time.time',side_effect=lambda:wall[0])
        def advance(seconds):mono[0]+=seconds;wall[0]+=seconds
        self.start_patch('fleet.provisioner.time.sleep',side_effect=advance)
        wire=self.wire
        def busy(*a,**kw):
            wire(*a,**kw);self.response.update(state='draining',busy=True,acceptingTurns=True,ingressFenced=False)
            if '/health' in kw['command'][-1]:return json.dumps({'runtimeRollDrain':self.response})
            return json.dumps(self.response)+'\n__SHIZUHA_HTTP_STATUS__:200'
        self.stream.side_effect=busy
        self.assertFalse(p.ensure_agent_image(self.agent));self.apps.replace_namespaced_deployment.assert_not_called()
        posts=[c for c in self.stream.call_args_list if 'rollout-drain' in c.kwargs['command'][-1]]
        self.assertEqual(len(posts),1);self.assertLessEqual(mono[0],70.01)

    @override_settings(FLEET_BROKER_IMAGE='localhost:30500/mcp-auth-proxy:projection-capable')
    def test_broker_only_roll_waits_for_busy_turn_then_converges_at_idle(self):
        broker = next(c for c in self.deployment.spec.template.spec.containers if c.name == 'broker')
        broker.image = 'localhost:30500/mcp-auth-proxy:before-projection'
        original = deepcopy(self.deployment)
        mono = [10.0]
        wall = [time.time()]
        self.start_patch('fleet.provisioner.time.monotonic', side_effect=lambda: mono[0])
        self.start_patch('fleet.provisioner.time.time', side_effect=lambda: wall[0])
        def advance(seconds):
            mono[0] += seconds
            wall[0] += seconds
        self.start_patch('fleet.provisioner.time.sleep', side_effect=advance)
        def busy(*args, **kwargs):
            self.wire(*args, **kwargs)
            self.response.update(state='draining', busy=True, acceptingTurns=True, ingressFenced=False)
            if '/health' in kwargs['command'][-1]:
                return json.dumps({'runtimeRollDrain': self.response})
            return json.dumps(self.response) + '\n__SHIZUHA_HTTP_STATUS__:200'
        self.stream.side_effect = busy
        self.assertTrue(p.native_agent_runtime_contract_drift(self.agent, self.anchor))
        self.assertFalse(p.ensure_agent_image(self.agent))
        self.apps.replace_namespaced_deployment.assert_not_called()
        self.assertEqual(self.deployment.to_dict(), original.to_dict())
        self.stream.side_effect = self.wire
        self.assertTrue(p.ensure_agent_image(self.agent))
        self.apps.replace_namespaced_deployment.assert_called_once()
        self.assertEqual(next(c.image for c in self.deployment.spec.template.spec.containers if c.name == 'broker'),
                         'localhost:30500/mcp-auth-proxy:projection-capable')
        self.assertEqual(self.deployment.spec.replicas, original.spec.replicas)
        self.assertEqual(next(c.to_dict() for c in self.deployment.spec.template.spec.containers if c.name == 'agent'),
                         next(c.to_dict() for c in original.spec.template.spec.containers if c.name == 'agent'))
        self.assertFalse(p.native_agent_runtime_contract_drift(self.agent, self.anchor))

    def test_invalid_history_or_receipt_does_not_touch_native_workload(self):
        for edit in ('scope','source','checksum','rollback','duplicate'):
            with self.subTest(edit=edit):
                self.history=[record()]
                if edit=='rollback':self.history.append(dict(record(28,'sha256:'+'f'*64,'b'*40),intent='rollback',rollback_of_generation=27))
                if edit=='duplicate':self.history.append(record())
                cm=self.cm()
                if edit=='scope':
                    obj=json.loads(cm.data[nr.DOCUMENT_KEY]);obj['scope']='legacy';cm.data[nr.DOCUMENT_KEY]=json.dumps(obj)
                    cm.metadata.annotations[nr.HASH_ANNOTATION]=hashlib.sha256(cm.data[nr.DOCUMENT_KEY].encode()).hexdigest()
                elif edit=='source':cm.metadata.annotations[nr.SOURCE_ANNOTATION]='short'
                elif edit=='checksum':cm.metadata.annotations[nr.HASH_ANNOTATION]='0'*64
                self.core.read_namespaced_config_map.side_effect=lambda *a,**kw:cm
                with self.assertRaises(nr.RuntimeReleaseProjectionUnavailable):p.ensure_agent_image(self.agent)
                self.apps.replace_namespaced_deployment.assert_not_called();self.stream.assert_not_called()

    def test_higher_rollback_generation_can_return_to_named_native_anchor(self):
        self.promote();self.assertTrue(p.ensure_agent_image(self.agent))
        self.pods[0].spec.containers[0].image=self.deployment.spec.template.spec.containers[0].image
        self.history.append(dict(record(29),intent='rollback',rollback_of_generation=27))
        self.assertTrue(p.ensure_agent_image(self.agent))
        self.assertEqual(self.deployment.metadata.annotations[nr.GENERATION_ANNOTATION],'29')
        self.assertEqual(self.deployment.spec.template.spec.containers[0].image,self.anchor)

    def test_cold_create_conflict_does_not_replace_foreign_or_newly_started_deployment(self):
        self.promote();self.pods=[]
        self.apps.read_namespaced_deployment.side_effect=ApiException(status=404)
        p._bind_reviewed_runtime_image(self.agent)
        self.apps.create_namespaced_deployment.side_effect=ApiException(status=409)
        with self.assertRaises(ApiException):p._create_deployment(self.apps,'shizuha-fleet',self.agent,replicas=0)
        self.apps.replace_namespaced_deployment.assert_not_called()

    def test_daemon_executor_handoff_refuses_template_mutation(self):
        self.promote();AgentRuntimeDesiredState.objects.filter(pk=self.desired.pk).update(lifecycle_executor='daemon_v2')
        with self.assertRaises(nr.RuntimeReleaseProjectionUnavailable):p.ensure_agent_image(self.agent)
        self.apps.replace_namespaced_deployment.assert_not_called();self.stream.assert_not_called()

    def test_codex_refresh_manifest_and_pinned_successor_pass_native_writer_activation(self):
        """The separate CI apply+image-pin path must activate its own template."""
        from pathlib import Path
        import yaml
        root = Path(__file__).resolve().parents[1]
        manifest = yaml.safe_load((root / 'k8s/hive-codex-refresh-deploy.yaml').read_text())
        script = (root / '.forgejo/scripts/ring-release-fleet.sh').read_text()
        self.assertIn('apply -f k8s/hive-codex-refresh-deploy.yaml', script)
        self.assertIn('set image deploy/hive-codex-refresh', script)
        deployment = client.ApiClient()._ApiClient__deserialize(manifest, 'V1Deployment')
        deployment.metadata.uid = 'refresh-deployment'
        # This is the only container change made by the existing CI image pin;
        # env/command must come from the actual standalone source manifest.
        container = deployment.spec.template.spec.containers[0]
        original_command = deepcopy(container.command)
        container.image = 'localhost:30500/shizuha-hive:hive-new'
        rs = client.V1ReplicaSet(metadata=client.V1ObjectMeta(name='refresh-rs', uid='refresh-rs'),
            spec=client.V1ReplicaSetSpec(replicas=1, selector=deployment.spec.selector,
                                        template=deepcopy(deployment.spec.template)))
        pod = client.V1Pod(metadata=client.V1ObjectMeta(name='refresh-pod', uid='refresh-pod'),
            spec=deepcopy(deployment.spec.template.spec), status=client.V1PodStatus(phase='Running'))
        self.apps.list_namespaced_deployment.side_effect = lambda *a, **kw: SimpleNamespace(items=[deployment])
        self.apps.list_namespaced_replica_set.return_value = SimpleNamespace(items=[rs])
        self.core.list_namespaced_pod.side_effect = lambda *a, **kw: SimpleNamespace(items=[pod])
        with mock.patch('kubernetes.client.BatchV1Api') as batch:
            batch.return_value.list_namespaced_cron_job.return_value = SimpleNamespace(items=[])
            batch.return_value.list_namespaced_job.return_value = SimpleNamespace(items=[])
            inventory = nr._writer_inventory(self.core, self.apps)
        self.assertEqual({r['kind'] for r in inventory}, {'Deployment', 'ReplicaSet', 'Pod'})
        for row in inventory:
            with self.subTest(kind=row['kind']):
                self.assertTrue(row['native_enabled'])
                self.assertEqual(row['native_namespace'], 'shizuha-hive')
                self.assertEqual(row['image'], container.image)
                self.assertEqual(row['command'], original_command)

    def test_writer_inventory_includes_old_active_job_and_suspended_future_template(self):
        env=[client.V1EnvVar(name='HIVE_NATIVE_RUNTIME_RELEASE_ENABLED',value='true'),
             client.V1EnvVar(name='HIVE_NATIVE_RUNTIME_RELEASE_NAMESPACE',value='shizuha-hive')]
        spec=client.V1PodSpec(containers=[client.V1Container(name='hive',image='localhost:30500/shizuha-hive:hive-new',env=env)])
        dep=client.V1Deployment(metadata=client.V1ObjectMeta(name='hive',uid='api'),spec=client.V1DeploymentSpec(replicas=2,
            selector=client.V1LabelSelector(match_labels={'app':'hive'}),template=client.V1PodTemplateSpec(spec=spec)))
        pod=client.V1Pod(metadata=client.V1ObjectMeta(name='old-job-pod',uid='old'),spec=deepcopy(spec),status=client.V1PodStatus(phase='Running'))
        pod.spec.containers[0].image='localhost:30500/shizuha-hive:hive-old';pod.spec.containers[0].env=[]
        cron=client.V1CronJob(metadata=client.V1ObjectMeta(name='reconciler',uid='cron'),spec=client.V1CronJobSpec(schedule='*/10 * * * *',suspend=True,
             job_template=client.V1JobTemplateSpec(spec=client.V1JobSpec(template=client.V1PodTemplateSpec(spec=spec)))))
        self.apps.list_namespaced_deployment.side_effect=lambda *a,**kw:SimpleNamespace(items=[dep])
        self.core.list_namespaced_pod.side_effect=lambda *a,**kw:SimpleNamespace(items=[pod])
        self.apps.list_namespaced_replica_set.return_value=SimpleNamespace(items=[])
        job=client.V1Job(metadata=client.V1ObjectMeta(name='old-no-pod-job',uid='job'),
            spec=client.V1JobSpec(template=client.V1PodTemplateSpec(spec=deepcopy(pod.spec))),status=None)
        with mock.patch('kubernetes.client.BatchV1Api') as batch:
            batch.return_value.list_namespaced_cron_job.return_value=SimpleNamespace(items=[cron])
            batch.return_value.list_namespaced_job.return_value=SimpleNamespace(items=[job])
            inventory=nr._writer_inventory(self.core,self.apps)
        self.assertEqual(len(inventory),4)
        self.assertFalse(next(r for r in inventory if r['kind']=='Pod')['native_enabled'])
        self.assertTrue(next(r for r in inventory if r['kind']=='CronJob')['suspended'])

    def test_create_retains_original_intent_across_concurrent_stop_archive_and_handoff(self):
        self.promote();self.pods=[]
        self.apps.read_namespaced_deployment.side_effect=ApiException(status=404)
        for changed in ({'desired_lifecycle':'stopped'},{'desired_lifecycle':'archived'},
                        {'lifecycle_executor':'daemon_v2'},{'desired_generation':2}):
            with self.subTest(changed=changed):
                AgentRuntimeDesiredState.objects.filter(pk=self.desired.pk).update(
                    desired_lifecycle='active',lifecycle_executor='hive_legacy',desired_generation=1)
                p._bind_reviewed_runtime_image(self.agent)
                AgentRuntimeDesiredState.objects.filter(pk=self.desired.pk).update(**changed)
                with self.assertRaises(nr.RuntimeReleaseProjectionUnavailable):
                    p._create_deployment(self.apps,'shizuha-fleet',self.agent,replicas=1)
        self.apps.create_namespaced_deployment.assert_not_called();self.apps.replace_namespaced_deployment.assert_not_called()

    def test_create_rechecks_lease_and_row_stop_after_initial_binding(self):
        self.promote();self.pods=[];self.apps.read_namespaced_deployment.side_effect=ApiException(status=404)
        p._bind_reviewed_runtime_image(self.agent)
        self.lease.return_value=SimpleNamespace(allowed=False)
        with self.assertRaises(nr.RuntimeReleaseProjectionUnavailable):p._create_deployment(self.apps,'shizuha-fleet',self.agent,replicas=1)
        self.lease.return_value=SimpleNamespace(allowed=True)
        FleetAgent.objects.filter(pk=self.agent.pk).update(status='stopped',runtime_lifecycle_state='operator_stopped')
        with self.assertRaises(nr.RuntimeReleaseProjectionUnavailable):p._create_deployment(self.apps,'shizuha-fleet',self.agent,replicas=1)
        self.apps.create_namespaced_deployment.assert_not_called()

    def test_same_pod_restart_during_final_source_read_invalidates_final_proof(self):
        self.promote();read_cm=self.cm
        def source(*a,**kw):
            if self.stream.call_count:self.pods[0].status.container_statuses[0].container_id='restarted'
            return read_cm()
        self.core.read_namespaced_config_map.side_effect=source
        self.assertFalse(p.ensure_agent_image(self.agent));self.apps.replace_namespaced_deployment.assert_not_called()

    def test_replaced_live_drain_nonce_or_deadline_refuses_final_write(self):
        self.promote();wire=self.wire
        for field,value in [('requestId','foreign-nonce'),('leaseUntil',time.time()*1000+120000)]:
            with self.subTest(field=field):
                def changed(*a,**kw):
                    if '/health' in kw['command'][-1]:
                        return json.dumps({'runtimeRollDrain':dict(self.response,**{field:value})})
                    return wire(*a,**kw)
                self.stream.side_effect=changed
                self.assertFalse(p.ensure_agent_image(self.agent));self.apps.replace_namespaced_deployment.assert_not_called()

    def test_protocol_one_idle_boundary_can_fast_forward(self):
        # Live gen-50 Codex (Nova): POST /rollout-drain speaks protocol 1 and
        # /health leaves runtimeRollDrain null while busy is false.
        self.promote();wire=self.wire
        def unfenced(*a,**kw):
            wire(*a,**kw);self.response.update(protocol=1,pendingAcceptedTurns=0,ingressFenced=False)
            if '/health' in kw['command'][-1]:
                return json.dumps({'status':'degraded','busy':False,'queueDepth':0,'runtimeRollDrain':None})
            return json.dumps(self.response)+'\n__SHIZUHA_HTTP_STATUS__:200'
        self.stream.side_effect=unfenced
        self.assertTrue(p.ensure_agent_image(self.agent))
        self.apps.replace_namespaced_deployment.assert_called()
        self.assertEqual(self.deployment.metadata.annotations[nr.GENERATION_ANNOTATION],'28')

    def test_protocol_one_retained_inbox_is_not_an_idle_boundary(self):
        self.promote();wire=self.wire
        def retained(*a,**kw):
            wire(*a,**kw);self.response.update(protocol=1,pendingAcceptedTurns=1,ingressFenced=False)
            return json.dumps(self.response)+'\n__SHIZUHA_HTTP_STATUS__:200'
        self.stream.side_effect=retained
        self.assertFalse(p.ensure_agent_image(self.agent));self.apps.replace_namespaced_deployment.assert_not_called()

    def test_unknown_dormant_image_is_unadopted_then_materialized_without_running_old_code(self):
        self.deployment.spec.replicas=0;self.pods=[]
        self.deployment.spec.template.spec.containers[0].image='localhost:30500/shizuha-agent-runtime:old-unqualified'
        self.deployment.spec.template.spec.containers[0].command=['old-command']
        self.assertFalse(nr.runtime_snapshot()['census'][0]['adopted'])
        self.apps.replace_namespaced_deployment.assert_not_called()
        self.promote()
        self.assertFalse(p.ensure_agent_image(self.agent))
        self.apps.replace_namespaced_deployment.assert_not_called()
        with mock.patch('fleet.provisioner.ensure_agent_id_password_aligned',return_value='ok'):
            self.assertTrue(p.start_agent(self.agent))
        self.assertEqual(self.deployment.spec.replicas,0)  # fake scale call records the separately fenced wake
        self.assertNotEqual(self.deployment.spec.template.spec.containers[0].command,['old-command'])
        self.assertEqual(self.deployment.metadata.annotations[nr.GENERATION_ANNOTATION],'28')
        self.lease.assert_called();self.stream.assert_not_called()

    def test_unknown_image_with_active_replica_is_quarantined(self):
        self.deployment.spec.template.spec.containers[0].image='localhost:30500/shizuha-agent-runtime:old-unqualified'
        with self.assertRaises(nr.RuntimeReleaseProjectionUnavailable):p.ensure_agent_image(self.agent)
        self.apps.replace_namespaced_deployment.assert_not_called();self.stream.assert_not_called()

    def test_real_start_materializes_unknown_cold_template_then_scales_with_uid_resource_version(self):
        self.promote();self.deployment.spec.replicas=0;self.pods=[]
        self.deployment.spec.template.spec.containers[0].image='localhost:30500/shizuha-agent-runtime:older'
        self.agent.status='hibernated';self.agent.save(update_fields=['status'])
        with mock.patch('fleet.provisioner.ensure_agent_id_password_aligned',return_value='ok'):
            self.assertTrue(p.start_agent(self.agent))
        self.assertEqual(self.deployment.metadata.annotations[nr.GENERATION_ANNOTATION],'28')
        body=self.apps.patch_namespaced_deployment_scale.call_args.kwargs['body']
        self.assertEqual(body['metadata'],{'uid':'deployment-1','resourceVersion':'2'})
        self.assertEqual(body['spec'],{'replicas':1})
        self.assertEqual(self.stream.call_count,0)

    def test_real_start_does_not_recapture_concurrent_stopped_intent_inside_ensure(self):
        self.promote();self.deployment.spec.replicas=0;self.pods=[]
        def identity(*a,**kw):
            AgentRuntimeDesiredState.objects.filter(pk=self.desired.pk).update(desired_lifecycle='stopped',desired_generation=2)
            return 'ok'
        with mock.patch('fleet.provisioner.ensure_agent_id_password_aligned',side_effect=identity):
            with self.assertRaises(nr.RuntimeReleaseProjectionUnavailable):p.start_agent(self.agent)
        self.apps.replace_namespaced_deployment.assert_not_called();self.apps.patch_namespaced_deployment_scale.assert_not_called()

    def test_real_start_lost_lease_before_cold_materialization_makes_no_template_or_scale_write(self):
        self.promote();self.deployment.spec.replicas=0;self.pods=[]
        self.deployment.spec.template.spec.containers[0].image='localhost:30500/shizuha-agent-runtime:older'
        def identity(*a,**kw):self.lease.return_value=SimpleNamespace(allowed=False);return 'ok'
        with mock.patch('fleet.provisioner.ensure_agent_id_password_aligned',side_effect=identity):
            self.assertFalse(p.start_agent(self.agent))
        self.apps.replace_namespaced_deployment.assert_not_called();self.apps.patch_namespaced_deployment_scale.assert_not_called()

    def test_scale_rechecks_lost_lease_after_successful_cold_template_replace(self):
        self.promote();self.deployment.spec.replicas=0;self.pods=[]
        replace=self.replace
        def replaced(*a,**kw):
            replace(*a,**kw);self.lease.return_value=SimpleNamespace(allowed=False)
        self.apps.replace_namespaced_deployment.side_effect=replaced
        with mock.patch('fleet.provisioner.ensure_agent_id_password_aligned',return_value='ok'):
            self.assertFalse(p.start_agent(self.agent))
        self.assertEqual(self.deployment.spec.replicas,0);self.apps.patch_namespaced_deployment_scale.assert_not_called()

    def test_real_on_demand_release_convergence_keeps_unleased_dormant_unknown_template(self):
        from contextlib import ExitStack
        from io import StringIO
        from django.core.management import call_command
        from fleet.management.commands.reconcile_on_demand_agents import Command, ConnectLifecycleSnapshot
        from fleet.lifecycle_fleet import converge_running_lifecycle_agent
        self.promote();self.pods=[];self.deployment.spec.replicas=0
        self.deployment.spec.template.spec.containers[0].image='localhost:30500/shizuha-agent-runtime:older'
        self.agent.status='hibernated';self.agent.save(update_fields=['status'])
        row={'id':str(self.agent.pk),'username':self.agent.agent_username,'agent_username':self.agent.agent_username,
             'enabled':False,'status':'hibernated','model':self.agent.model,'email':'native-sara@shizuha.com'}
        prefix='fleet.management.commands.reconcile_on_demand_agents.'
        with ExitStack() as stack:
            for target,value in [(prefix+'list_lifecycle_agents',[row]),(prefix+'reconcile_model_lease',{'scale':{self.agent.agent_username:False},'log':[]}),
                                 (prefix+'observed_native_replicas',0),('fleet.external_agents.fresh_agent_telemetry',{}),
                                 ('fleet.model_serviceability.unserviceable_cortex_models',frozenset()),('fleet.provisioner.clear_stale_personal_isolation_blocks',0)]:
                stack.enter_context(mock.patch(target,return_value=value))
            for method,value in [('_fetch_pulse_rosters',[{'team_name':'engineering','is_active':True,'tiers':[['native-sara@shizuha.com']]}]),('_fetch_pulse_snapshot',{'workload':{},'team_workload':{}}),
                                 ('_fetch_connect_state',ConnectLifecycleSnapshot(available=True,unread_by_email={},last_dm_at_by_email={})),
                                 ('_apply_health_signals',None)]:
                stack.enter_context(mock.patch.object(Command,method,return_value=value))
            converge=stack.enter_context(mock.patch(prefix+'converge_running_lifecycle_agent',wraps=converge_running_lifecycle_agent))
            start=stack.enter_context(mock.patch(prefix+'set_lifecycle_agent_enabled'))
            stop=stack.enter_context(mock.patch(prefix+'hibernate_lifecycle_agent'))
            call_command('reconcile_on_demand_agents','--apply',stdout=StringIO())
            converge.assert_not_called();start.assert_not_called();stop.assert_not_called()
        self.apps.replace_namespaced_deployment.assert_not_called();self.apps.patch_namespaced_deployment_scale.assert_not_called()

    def test_real_on_demand_converges_running_unleased_stale_native_before_start_or_stop(self):
        from contextlib import ExitStack
        from io import StringIO
        from django.core.management import call_command
        from fleet.management.commands.reconcile_on_demand_agents import Command, ConnectLifecycleSnapshot
        from fleet.lifecycle_fleet import converge_running_lifecycle_agent
        self.promote()
        self.agent.status='running';self.agent.save(update_fields=['status'])
        self.lease.return_value=SimpleNamespace(allowed=False)
        row={'id':str(self.agent.pk),'username':self.agent.agent_username,'agent_username':self.agent.agent_username,
             'enabled':True,'status':'running','model':self.agent.model,'email':'native-sara@shizuha.com',
             'activity_state':'idle'}
        prefix='fleet.management.commands.reconcile_on_demand_agents.'
        with ExitStack() as stack:
            for target,value in [(prefix+'list_lifecycle_agents',[row]),(prefix+'reconcile_model_lease',{'scale':{},'log':[]}),
                                 (prefix+'observed_native_replicas',1),(prefix+'decide_on_demand_actions',[]),
                                 ('fleet.external_agents.fresh_agent_telemetry',{}),
                                 ('fleet.model_serviceability.unserviceable_cortex_models',frozenset()),('fleet.provisioner.clear_stale_personal_isolation_blocks',0)]:
                stack.enter_context(mock.patch(target,return_value=value))
            for method,value in [('_fetch_pulse_rosters',[{'team_name':'engineering','is_active':True,'tiers':[['native-sara@shizuha.com']]}]),('_fetch_pulse_snapshot',{'workload':{},'team_workload':{}}),
                                 ('_fetch_connect_state',ConnectLifecycleSnapshot(available=True,unread_by_email={},last_dm_at_by_email={})),
                                 ('_apply_health_signals',None)]:
                stack.enter_context(mock.patch.object(Command,method,return_value=value))
            converge=stack.enter_context(mock.patch(prefix+'converge_running_lifecycle_agent',wraps=converge_running_lifecycle_agent))
            start=stack.enter_context(mock.patch(prefix+'set_lifecycle_agent_enabled'))
            stop=stack.enter_context(mock.patch(prefix+'hibernate_lifecycle_agent'))
            call_command('reconcile_on_demand_agents','--apply',stdout=StringIO())
            converge.assert_called()
            start.assert_not_called();stop.assert_not_called()
        self.assertEqual(self.apps.replace_namespaced_deployment.call_count,1)
        self.assertEqual(self.deployment.metadata.annotations[nr.GENERATION_ANNOTATION],'28')

    def test_create_existing_zero_to_one_rechecks_lease_before_full_replace(self):
        self.promote();self.deployment.spec.replicas=0;self.pods=[]
        p._bind_reviewed_runtime_image(self.agent)
        self.lease.return_value=SimpleNamespace(allowed=False)
        with self.assertRaises(nr.RuntimeReleaseProjectionUnavailable):p._create_deployment(self.apps,'shizuha-fleet',self.agent,replicas=1)
        self.apps.replace_namespaced_deployment.assert_not_called()

    def test_agent_truth_separates_new_desired_release_from_still_old_running_pod(self):
        from fleet.serializers import FleetAgentSerializer
        self.promote()
        payload=FleetAgentSerializer(self.agent).data
        self.assertEqual(payload['desired_runtime_release']['generation'],28)
        self.assertEqual(payload['observed_runtime_image'],self.anchor)
        self.assertEqual(payload['runtime_image_tag'],'sha256:'+nr.ANCHOR_DIGEST.split(':')[1][:12])
        self.assertNotEqual(payload['effective_image'],payload['observed_runtime_image'])

    def local_workspace(self):
        self.agent.requires_longhorn = False
        self.agent.arch = 'arm64'
        self.agent.save(update_fields=['requires_longhorn', 'arch'])
        self.deployment.spec.replicas = 0
        self.pods = []
        self.core.read_namespaced_persistent_volume_claim.return_value = client.V1PersistentVolumeClaim(
            metadata=client.V1ObjectMeta(uid='workspace-1'),
            spec=client.V1PersistentVolumeClaimSpec(volume_name='volume-1'),
            status=client.V1PersistentVolumeClaimStatus(phase='Bound'),
        )
        self.volume = client.V1PersistentVolume(
            metadata=client.V1ObjectMeta(uid='volume-1', resource_version='17'),
            spec=client.V1PersistentVolumeSpec(
                local=client.V1LocalVolumeSource(path='/retained-workspace'),
                claim_ref=client.V1ObjectReference(uid='workspace-1', name=self.agent.workspace_pvc_name,
                                                 namespace='shizuha-fleet'),
                node_affinity=client.V1VolumeNodeAffinity(required=client.V1NodeSelector(node_selector_terms=[
                    client.V1NodeSelectorTerm(match_expressions=[client.V1NodeSelectorRequirement(
                        key='kubernetes.io/hostname', operator='In', values=['gx10-15'])]),
                ])),
            ),
        )
        self.core.read_persistent_volume.side_effect = lambda *args, **kwargs: deepcopy(self.volume)
        self.core.read_node.return_value = client.V1Node(metadata=client.V1ObjectMeta(
            uid='node-15', labels={'kubernetes.io/hostname': 'gx10-15', 'kubernetes.io/arch': 'arm64',
                                 'node-role.kubernetes.io/control-plane': 'true'},
        ))

    def test_native_placement_repairs_existing_template_and_repeated_reconcile_is_noop(self):
        self.local_workspace()
        self.deployment.spec.template.spec.node_selector = {
            'kubernetes.io/arch': 'amd64', 'kubernetes.io/hostname': 's1',
        }
        retained_volumes = deepcopy(self.deployment.spec.template.spec.volumes)
        self.assertTrue(p.native_agent_runtime_contract_drift(self.agent, self.anchor))
        self.assertTrue(p.ensure_agent_image(self.agent))
        self.assertEqual(self.deployment.spec.template.spec.node_selector, {'kubernetes.io/arch': 'arm64'})
        self.assertIsNone(self.deployment.spec.template.spec.affinity.node_affinity)
        self.assertEqual(self.deployment.spec.template.spec.volumes, retained_volumes)
        self.assertEqual(self.deployment.spec.replicas, 0)
        writes = self.apps.replace_namespaced_deployment.call_count
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                self.assertFalse(p.native_agent_runtime_contract_drift(self.agent, self.anchor))
                self.assertTrue(p.ensure_agent_image(self.agent))
                self.assertEqual(self.apps.replace_namespaced_deployment.call_count, writes)
        self.stream.assert_not_called()
        self.apps.patch_namespaced_deployment_scale.assert_not_called()

    def test_native_placement_refuses_canonical_selector_conflicting_with_local_data(self):
        self.local_workspace()
        self.agent.arch = 'amd64'
        self.agent.node_selector_overrides = {'kubernetes.io/hostname': 's1'}
        self.agent.save(update_fields=['arch', 'node_selector_overrides'])
        original = deepcopy(self.deployment)
        self.assertFalse(p.ensure_agent_image(self.agent))
        self.assertIn('conflicts with bound workspace', self.agent.status_message)
        self.assertEqual(self.deployment, original)
        self.apps.replace_namespaced_deployment.assert_not_called()
        self.apps.patch_namespaced_deployment_scale.assert_not_called()

    def test_native_placement_unreadable_pv_is_not_control_plane_exclusion(self):
        self.local_workspace()
        self.core.read_persistent_volume.side_effect = ApiException(status=403, reason='Forbidden')
        self.assertFalse(p.ensure_agent_image(self.agent))
        self.assertIn('stage placement', self.agent.status_message)
        self.apps.replace_namespaced_deployment.assert_not_called()

    def test_native_placement_rechecks_volume_identity_before_write(self):
        self.local_workspace()
        reads = 0

        def rebound(*args, **kwargs):
            nonlocal reads
            reads += 1
            volume = deepcopy(self.volume)
            if reads > 1:
                volume.metadata.uid = 'replacement-volume'
            return volume

        self.core.read_persistent_volume.side_effect = rebound
        self.assertFalse(p.ensure_agent_image(self.agent))
        self.assertIn('changed before runtime replacement', self.agent.status_message)
        self.apps.replace_namespaced_deployment.assert_not_called()

    def test_native_placement_rejects_foreign_claim_before_write(self):
        self.local_workspace()
        self.volume.spec.claim_ref.uid = 'foreign-claim'
        self.assertFalse(p.ensure_agent_image(self.agent))
        self.apps.replace_namespaced_deployment.assert_not_called()

    def test_native_placement_does_not_bypass_live_turn_fence(self):
        self.local_workspace()
        self.deployment.spec.replicas = 1
        self.pods = [self.pod()]
        with mock.patch.object(p, 'native_agent_runtime_roll_ready', return_value=False) as fence:
            self.assertFalse(p.ensure_agent_image(self.agent))
        fence.assert_called_once()
        self.apps.replace_namespaced_deployment.assert_not_called()

    def test_native_placement_api_patch_uses_existing_owner_without_scaling(self):
        self.local_workspace()
        self.agent.arch = 'amd64'
        self.agent.node_selector_overrides = {'kubernetes.io/hostname': 's1'}
        self.agent.save(update_fields=['arch', 'node_selector_overrides'])
        staff = get_user_model().objects.create_user(username='placement-owner', is_staff=True)
        api = APIClient()
        api.force_authenticate(staff)
        with mock.patch('fleet.lifecycle_fleet.provisioner.heal_workspace_disk'):
            response = api.patch('/api/v1/fleet/agents/' + str(self.agent.pk) + '/', {
                'arch': 'arm64', 'node_selector_overrides': {},
            }, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data['k3s_lane_apply']['applied'])
        self.assertEqual(self.deployment.spec.template.spec.node_selector, {'kubernetes.io/arch': 'arm64'})
        self.assertEqual(self.deployment.spec.replicas, 0)
        self.apps.patch_namespaced_deployment_scale.assert_not_called()

    def test_native_placement_rechecks_binding_after_turn_drain_wait(self):
        self.local_workspace()
        self.deployment.spec.replicas = 1
        self.pods = [self.pod()]

        def rebound_during_drain(*args, **kwargs):
            response = self.wire(*args, **kwargs)
            if '/health' not in kwargs['command'][-1]:
                self.volume.metadata.uid = 'volume-rebound-during-drain'
            return response

        self.stream.side_effect = rebound_during_drain
        self.assertFalse(p.ensure_agent_image(self.agent))
        self.assertIn('changed before runtime replacement', self.agent.status_message)
        self.apps.replace_namespaced_deployment.assert_not_called()

    def test_native_placement_desired_node_pin_change_invalidates_original_intent(self):
        self.local_workspace()
        p._bind_reviewed_runtime_image(self.agent)
        FleetAgent.objects.filter(pk=self.agent.pk).update(node_name='gx10-5')
        with self.assertRaises(nr.RuntimeReleaseProjectionUnavailable):
            p.ensure_agent_image(self.agent, _preserve_native_intent=True)
        self.apps.replace_namespaced_deployment.assert_not_called()

    def test_native_placement_unknown_volume_identity_is_not_an_empty_binding(self):
        self.local_workspace()
        self.volume.metadata.uid = None
        self.assertFalse(p.ensure_agent_image(self.agent))
        self.assertIn('identity is unknown', self.agent.status_message)
        self.apps.replace_namespaced_deployment.assert_not_called()

    def test_native_placement_supported_shapes_preserve_existing_affinity_and_repeat_noop(self):
        original = deepcopy(self.deployment)
        shapes = [
            ('local', 'gx10-15', ''), ('local', 'gx10-14', ''),
            ('hostpath', 'gx10-15', ''), ('hostpath', 'gx10-14', ''),
            ('rwx', '', ''), ('rwx', '', 'gx10-15'), ('rwx', '', 'gx10-14'),
            ('pending', '', ''), ('pending', '', 'gx10-15'), ('pending', '', 'gx10-14'),
            ('emptydir', '', ''), ('emptydir', '', 'gx10-15'), ('emptydir', '', 'gx10-14'),
        ]
        for storage, storage_node, explicit_node in shapes:
            with self.subTest(storage=storage, storage_node=storage_node, explicit_node=explicit_node):
                self.deployment = deepcopy(original)
                self.local_workspace()
                self.agent.node_name = explicit_node
                self.agent.save(update_fields=['node_name'])
                self.core.read_node.side_effect = lambda name, **kwargs: client.V1Node(
                    metadata=client.V1ObjectMeta(uid='uid-' + name, labels={
                        'kubernetes.io/hostname': name, 'kubernetes.io/arch': 'arm64',
                        **({'node-role.kubernetes.io/control-plane': 'true'} if name == 'gx10-15' else {}),
                    }),
                )
                if storage_node:
                    expression = self.volume.spec.node_affinity.required.node_selector_terms[0].match_expressions[0]
                    expression.values = [storage_node]
                if storage == 'hostpath':
                    self.volume.spec.local = None
                    self.volume.spec.host_path = client.V1HostPathVolumeSource(path='/retained-workspace')
                if storage == 'rwx':
                    self.volume.spec.local = None
                    self.volume.spec.node_affinity = None
                    self.volume.spec.nfs = client.V1NFSVolumeSource(server='storage', path='/workspace')
                if storage in ('pending', 'emptydir'):
                    claim = self.core.read_namespaced_persistent_volume_claim.return_value
                    claim.spec.volume_name = None
                    claim.status.phase = 'Pending'
                if storage == 'emptydir':
                    for volume in self.deployment.spec.template.spec.volumes:
                        if volume.name == 'workspace':
                            volume.persistent_volume_claim = None
                            volume.empty_dir = client.V1EmptyDirVolumeSource()
                expected_affinity = p._affinity(self.agent).to_dict()
                retained_volumes = deepcopy(self.deployment.spec.template.spec.volumes)
                self.assertTrue(p.ensure_agent_image(self.agent))
                self.assertEqual(self.deployment.spec.template.spec.affinity.to_dict(), expected_affinity)
                self.assertEqual(self.deployment.spec.template.spec.node_selector, p._node_selector(self.agent))
                self.assertEqual(self.deployment.spec.template.spec.volumes, retained_volumes)
                writes = self.apps.replace_namespaced_deployment.call_count
                for attempt in range(2):
                    self.assertFalse(p.native_agent_runtime_contract_drift(self.agent, self.anchor))
                    self.assertTrue(p.ensure_agent_image(self.agent))
                    self.assertEqual(self.apps.replace_namespaced_deployment.call_count, writes)
                self.apps.patch_namespaced_deployment_scale.assert_not_called()

    def test_native_placement_explicit_node_identity_must_be_known(self):
        self.local_workspace()
        self.volume.spec.local = None
        self.volume.spec.node_affinity = None
        self.agent.node_name = 'gx10-15'
        self.agent.save(update_fields=['node_name'])
        self.core.read_node.return_value.metadata.uid = None
        self.assertFalse(p.ensure_agent_image(self.agent))
        self.apps.replace_namespaced_deployment.assert_not_called()

    def test_native_placement_blocking_final_read_cannot_age_authorization_past_clocks(self):
        self.local_workspace()
        self.deployment.spec.replicas = 1
        self.pods = [self.pod()]
        clock = [10.0]
        self.start_patch('fleet.provisioner.time.monotonic', side_effect=lambda: clock[0])

        def slow_final_volume(*args, **kwargs):
            if self.stream.call_count:
                clock[0] = 71.0
            return deepcopy(self.volume)

        self.core.read_persistent_volume.side_effect = slow_final_volume
        self.assertFalse(p.ensure_agent_image(self.agent))
        self.apps.replace_namespaced_deployment.assert_not_called()

    @override_settings(HIVE_NATIVE_RUNTIME_RELEASE_ENABLED=False)
    def test_legacy_runtime_keeps_existing_placement_actuation_boundary(self):
        self.local_workspace()
        previous_selector = deepcopy(self.deployment.spec.template.spec.node_selector)
        previous_affinity = deepcopy(self.deployment.spec.template.spec.affinity)
        self.assertTrue(p.ensure_agent_image(self.agent))
        self.assertEqual(self.deployment.spec.template.spec.node_selector, previous_selector)
        self.assertEqual(self.deployment.spec.template.spec.affinity, previous_affinity)
        self.apps.replace_namespaced_deployment.assert_not_called()
