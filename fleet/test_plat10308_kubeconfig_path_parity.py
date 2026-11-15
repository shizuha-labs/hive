"""PLAT-10308 path parity: the fleet-kubeconfig-materialize init container and
the agent container MUST resolve the workspace PVC to the SAME volume path.

The hive#603 render mounted the workspace PVC at ``/home/agent`` in the init
container while the agent container mounts it at
``agent.workspace_mount_path`` (default ``/home/agent/.shizuha``). The
materialized config therefore landed at
``/home/agent/.shizuha/.kube/config`` in the agent container's view while
``KUBECONFIG=/home/agent/.kube/config`` pointed at a path that never existed
— kubectl only worked through the silent in-cluster fallback, and
``kubectl config view --minify`` failed with ``current-context must exist``.

These tests pin the contract: init mountPath == agent workspace mount path,
init KUBE_DIR == <workspace>/.kube, and the KUBECONFIG env target ==
<workspace>/.kube/config — for the default mount path AND for a seat with a
custom workspace_mount_path.
"""
from types import SimpleNamespace

from django.test import TestCase, override_settings
from kubernetes import client as k8s_client

from fleet.models import FleetAgent
from fleet.provisioner import (
    FLEET_KUBECONFIG_INIT_NAME,
    _build_pod_spec,
    _fleet_kubeconfig_init_container,
    _fleet_kubeconfig_init_script,
    fleet_kubeconfig_dir_for,
    fleet_kubeconfig_path_for,
)


@override_settings(FLEET_AGENT_IMAGE='localhost:30500/shizuha-agent-runtime:test')
class KubeconfigPathParityRenderTests(TestCase):
    def _agent(self, **kw):
        return FleetAgent.objects.create(
            agent_username='kai', agent_id='platform-kai',
            namespace='shizuha-fleet', status='running',
            image='localhost:30500/shizuha-agent-runtime:test',
            clusterplane_kubeconfig=True,
            **kw,
        )

    def _agent_container(self, spec):
        return next(c for c in spec.containers if c.name == 'agent')

    def _init_container(self, spec):
        return next(
            c for c in (spec.init_containers or [])
            if c.name == FLEET_KUBECONFIG_INIT_NAME
        )

    def _assert_parity(self, agent):
        """The core contract: init mountPath, init KUBE_DIR, and the agent's
        KUBECONFIG target all resolve to the same PVC volume path."""
        spec = _build_pod_spec(agent)
        init = self._init_container(spec)
        agent_container = self._agent_container(spec)

        # the init mounts the workspace PVC at the SAME path the agent does
        agent_workspace_mounts = [
            m.mount_path for m in agent_container.volume_mounts
            if m.name == 'workspace'
        ]
        self.assertTrue(agent_workspace_mounts, 'agent container must mount workspace')
        init_mounts = [
            m.mount_path for m in init.volume_mounts if m.name == 'workspace'
        ]
        self.assertEqual(init_mounts, agent_workspace_mounts)

        # the init writes under that same mount, not a hardcoded sibling
        script = init.args[0]
        kube_dir = fleet_kubeconfig_dir_for(agent)
        self.assertIn(f"KUBE_DIR='{kube_dir}'", script)

        # the agent's KUBECONFIG target resolves to the same volume path
        kubeconfig_env = next(
            e for e in agent_container.env if e.name == 'KUBECONFIG'
        )
        self.assertEqual(kubeconfig_env.value, fleet_kubeconfig_path_for(agent))
        self.assertEqual(kubeconfig_env.value, f'{kube_dir}/config')
        return spec

    def test_default_workspace_mount_path_parity(self):
        # kai's live shape: workspace_mount_path defaults to
        # /home/agent/.shizuha — the init must mount and write THERE, and
        # KUBECONFIG must point at /home/agent/.shizuha/.kube/config.
        agent = self._agent()
        self.assertEqual(agent.workspace_mount_path, '/home/agent/.shizuha')
        self._assert_parity(agent)

    def test_custom_workspace_mount_path_parity(self):
        # a seat with a non-default workspace mount path keeps parity too —
        # the init derives everything from agent.workspace_mount_path.
        agent = self._agent(workspace_mount_path='/home/agent/ws-custom')
        self._assert_parity(agent)

    def test_parity_holds_after_clusterplane_converge(self):
        # the read-modify-replace update path (existing Deployment) must
        # converge to the SAME parity contract the create path renders.
        from fleet.provisioner import _converge_clusterplane_pod_contract
        agent = self._agent()
        spec = _build_pod_spec(agent)
        # simulate the pre-fix drifted state: init mounted at /home/agent
        drifted = SimpleNamespace(
            automount_service_account_token=True,
            init_containers=[
                k8s_client.V1Container(
                    name=FLEET_KUBECONFIG_INIT_NAME,
                    volume_mounts=[
                        k8s_client.V1VolumeMount(name='workspace', mount_path='/home/agent'),
                    ],
                ),
            ],
        )
        self.assertTrue(_converge_clusterplane_pod_contract(drifted, agent))
        init = next(
            c for c in drifted.init_containers
            if c.name == FLEET_KUBECONFIG_INIT_NAME
        )
        agent_mounts = [
            m.mount_path for m in
            self._agent_container(spec).volume_mounts
            if m.name == 'workspace'
        ]
        self.assertIn(init.volume_mounts[0].mount_path, agent_mounts)
        self.assertIn(
            f"KUBE_DIR='{fleet_kubeconfig_dir_for(agent)}'",
            init.args[0],
        )

    def test_script_writes_and_chowns_the_derived_kube_dir(self):
        agent = self._agent()
        script = _fleet_kubeconfig_init_script(fleet_kubeconfig_dir_for(agent))
        self.assertIn('mkdir -p "$KUBE_DIR"', script)
        self.assertIn('chown -R 1000:1000 "$KUBE_DIR"', script)
        self.assertIn('chmod 600 "$KUBE_DIR/config"', script)
        # no embedded bearer bytes — tokenFile reference only (PLAT-7463)
        self.assertIn('tokenFile', script)
        self.assertNotIn('token: ey', script)

    def test_init_container_writer_matches_rendered_init(self):
        # the standalone init-builder and the full pod-spec render agree.
        agent = self._agent()
        standalone = _fleet_kubeconfig_init_container(agent)
        rendered = self._init_container(_build_pod_spec(agent))
        self.assertEqual(standalone.to_dict(), rendered.to_dict())
