"""PLAT-9847 (rei P1): EXISTING native seats must converge to the persistent-
log contract — drift detection flags a missing agent-logs mount/init, the
reconcile path patches the live template, and a second reconcile is a no-op.

Exercises the real drift probe + real convergence on real V1 objects against
a mocked cluster, not a double ``_build_pod_spec`` render.
"""
from copy import deepcopy
from unittest import mock

from django.test import SimpleTestCase, override_settings
from kubernetes import client

from fleet import provisioner
from fleet.models import FleetAgent


@override_settings(FLEET_COORDINATOR_MODEL_TOKEN_URL='')
class NativeLogContractConvergenceTests(SimpleTestCase):
    def setUp(self):
        self.agent = FleetAgent(
            agent_username='san', agent_id='san-runtime-id',
            model='cortex/GLM-5.3-Flash',
            image='registry/shizuha-agent-runtime:reviewed',
        )
        self.agent.save = mock.Mock()
        self.agent._reviewed_runtime_image = self.agent.image
        self.apps = mock.Mock()
        for name, result in (
            ('ensure_agent_cortex_key', ''), ('ensure_agent_forgejo_token', ''),
            ('get_k8s_client', (mock.Mock(), self.apps, mock.Mock())),
            ('_log_event', None),
            # isolate the log-contract dimension: the other drift axes have
            # their own suites (broker consumer convergence, lane env, ...)
            ('_cortex_key_env_ref_drifted', False),
            ('_forgejo_env_drifted', False),
            ('_lane_env_drifted', False),
            ('_broker_contract_drifted', False),
            ('_reconnect_state_volume_drifted', False),
            ('_uses_native_runtime_image', False),
        ):
            patcher = mock.patch.object(provisioner, name, return_value=result)
            patcher.start()
            self.addCleanup(patcher.stop)
        binding = mock.patch.object(
            provisioner, '_bind_reviewed_runtime_image',
            side_effect=lambda agent: agent.image,
        )
        binding.start()
        self.addCleanup(binding.stop)
        self.apps.read_namespaced_deployment.side_effect = (
            lambda *args: deepcopy(self.live)
        )
        self.replace_calls = 0
        self.apps.replace_namespaced_deployment.side_effect = self._replace

    def _replace(self, name, namespace, body):
        self.replace_calls += 1
        self.live = deepcopy(body)
        self.live.metadata.resource_version = str(
            int(body.metadata.resource_version) + 1
        )

    def pre_contract_deployment(self, *, workspace_claim='agent-workspace-pvc'):
        """A legacy native template: runtime labels fine, agent container fine,
        but NO agent-logs subPath mount and NO materialize init. The workspace
        PVC volume itself may be absent (drift) or present (mount-only drift).
        """
        agent_container = provisioner._agent_container(
            self.agent, has_broker=False, has_dind=False,
        )
        # strip everything the fresh renderer added for the log contract
        agent_container.volume_mounts = [
            mount for mount in (agent_container.volume_mounts or [])
            if not (
                getattr(mount, 'mount_path', '')
                == provisioner.NATIVE_LOG_MOUNT_PATH
            )
        ]
        if workspace_claim:  # None simulates the volume missing entirely
            volumes = [client.V1Volume(
                name='workspace',
                persistent_volume_claim=(
                    client.V1PersistentVolumeClaimVolumeSource(
                        claim_name=workspace_claim,
                    )
                ),
            )]
        else:
            volumes = []
        return client.V1Deployment(
            metadata=client.V1ObjectMeta(
                name=self.agent.deployment_name, resource_version='11',
                labels={
                    provisioner.NATIVE_RUNTIME_LABEL:
                        provisioner.NATIVE_RUNTIME_LABEL_VALUE,
                },
                annotations={
                    provisioner.NATIVE_RUNTIME_OWNER_ANNOTATION:
                        provisioner.NATIVE_RUNTIME_OWNER,
                },
            ),
            spec=client.V1DeploymentSpec(
                replicas=0,
                selector=client.V1LabelSelector(
                    match_labels={
                        provisioner.NATIVE_RUNTIME_LABEL:
                            provisioner.NATIVE_RUNTIME_LABEL_VALUE,
                    },
                ),
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(
                        labels={
                            provisioner.NATIVE_RUNTIME_LABEL:
                                provisioner.NATIVE_RUNTIME_LABEL_VALUE,
                        },
                    ),
                    spec=client.V1PodSpec(
                        containers=[agent_container],
                        volumes=volumes,
                        init_containers=None,
                    ),
                ),
            ),
        )

    def test_drift_probe_flags_missing_mount_and_init(self):
        self.live = self.pre_contract_deployment()
        self.assertTrue(
            provisioner.native_agent_runtime_contract_drift(
                self.agent, self.agent.image,
            )
        )

    def test_drift_probe_clears_after_convergence(self):
        self.live = self.pre_contract_deployment()
        spec = self.live.spec.template.spec
        container = spec.containers[0]
        self.assertTrue(
            provisioner._converge_native_log_contract(
                spec, container, self.agent,
            )
        )
        self.assertFalse(
            provisioner.native_agent_runtime_contract_drift(
                self.agent, self.agent.image,
            )
        )

    def test_reconcile_patches_then_second_pass_is_noop(self):
        """The actual existing-deployment sequence: missing mount/init ->
        patched template (replace called once) -> no-op second reconcile."""
        self.live = self.pre_contract_deployment()
        self.assertTrue(
            provisioner.native_agent_runtime_contract_drift(
                self.agent, self.agent.image,
            )
        )
        spec = self.live.spec.template.spec
        container = spec.containers[0]
        self.assertTrue(
            provisioner._converge_native_log_contract(
                spec, container, self.agent,
            )
        )
        self._replace(self.agent.deployment_name, 'default', self.live)
        self.assertEqual(self.replace_calls, 1)
        # second reconcile: probe clear, convergence no-op
        self.assertFalse(
            provisioner.native_agent_runtime_contract_drift(
                self.agent, self.agent.image,
            )
        )
        spec2 = deepcopy(self.live.spec.template.spec)
        self.assertFalse(
            provisioner._converge_native_log_contract(
                spec2, spec2.containers[0], self.agent,
            )
        )

    def test_converged_template_matches_fresh_render_shape(self):
        self.live = self.pre_contract_deployment()
        spec = self.live.spec.template.spec
        provisioner._converge_native_log_contract(
            spec, spec.containers[0], self.agent,
        )
        init_names = [
            getattr(init, 'name', '') for init in (spec.init_containers or [])
        ]
        self.assertEqual(init_names[0], provisioner.NATIVE_LOG_INIT_NAME)
        mount = next(
            mount for mount in (spec.containers[0].volume_mounts or [])
            if getattr(mount, 'mount_path', '')
            == provisioner.NATIVE_LOG_MOUNT_PATH
        )
        self.assertEqual(getattr(mount, 'sub_path', ''), 'agent-logs')
        self.assertEqual(getattr(mount, 'name', ''), 'workspace')

    def test_custom_workspace_claim_is_bound(self):
        """Custom workspace path: the volume must bind the agent's OWN claim,
        never a global default."""
        self.live = self.pre_contract_deployment(workspace_claim=None)
        spec = self.live.spec.template.spec
        self.assertTrue(
            provisioner._converge_native_log_contract(
                spec, spec.containers[0], self.agent,
            )
        )
        volume = next(
            volume for volume in (spec.volumes or [])
            if getattr(volume, 'name', '') == 'workspace'
        )
        self.assertEqual(
            volume.persistent_volume_claim.claim_name,
            self.agent.workspace_pvc_name,
        )
