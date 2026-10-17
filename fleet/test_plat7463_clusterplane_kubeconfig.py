"""PLAT-7463: the cluster-plane kubeconfig capability — capability-selection →
render → pod-start sequence.

The Q1a fail-closed invariant (no kube API path from a fleet pod) gains ONE
flag-gated exception: flagged seats project the kubelet-managed SA token and
init-materialize ~/.kube/config referencing the tokenFile (live rotation, no
embedded bearer bytes). Unflagged seats render neither — the default stays
fail-closed. Authorization is NOT granted by the flag (the scoped RBAC is a
separate declarative grant in the deploy repo).
"""
from django.test import TestCase, override_settings

from fleet.models import FleetAgent
from fleet.provisioner import (
    FLEET_KUBECONFIG_INIT_SCRIPT,
    _build_pod_spec,
    _fleet_kubeconfig_init_container,
)


@override_settings(FLEET_AGENT_IMAGE='localhost:30500/shizuha-agent-runtime:test')
class ClusterPlaneKubeconfigRenderTests(TestCase):
    def _agent(self, **kw):
        return FleetAgent.objects.create(
            agent_username='kai', agent_id='platform-kai',
            namespace='shizuha-fleet', status='running',
            image='localhost:30500/shizuha-agent-runtime:test',
            **kw,
        )

    def test_default_agent_renders_no_kube_api_path(self):
        # Q1a holds by default: no automount, no init container, no KUBECONFIG.
        spec = _build_pod_spec(self._agent())
        self.assertFalse(spec.automount_service_account_token)
        init_names = [c.name for c in (spec.init_containers or [])]
        self.assertNotIn('fleet-kubeconfig-materialize', init_names)
        agent_container = next(c for c in spec.containers if c.name == 'agent')
        self.assertFalse(any(e.name == 'KUBECONFIG' for e in agent_container.env))

    def test_flagged_agent_projects_token_and_materializes_config(self):
        agent = self._agent(clusterplane_kubeconfig=True)
        spec = _build_pod_spec(agent)
        # the SA token projects (kubelet-managed rotation — no copied bytes)
        self.assertTrue(spec.automount_service_account_token)
        # the init container materializes the config before the agent starts
        init_names = [c.name for c in (spec.init_containers or [])]
        self.assertIn('fleet-kubeconfig-materialize', init_names)
        init = next(c for c in spec.init_containers if c.name == 'fleet-kubeconfig-materialize')
        self.assertIn('tokenFile', FLEET_KUBECONFIG_INIT_SCRIPT)
        self.assertNotIn('token: ey', FLEET_KUBECONFIG_INIT_SCRIPT)  # no embedded bearer
        # the materialized config lands on the workspace PVC (durable)
        mounts = [m.name for m in init.volume_mounts]
        self.assertIn('workspace', mounts)
        # the agent container points kubectl at the materialized config
        agent_container = next(c for c in spec.containers if c.name == 'agent')
        kubeconfig_env = next(e for e in agent_container.env if e.name == 'KUBECONFIG')
        self.assertEqual(kubeconfig_env.value, '/home/agent/.kube/config')

    def test_init_container_runs_as_root_only_to_chown_workspace_write(self):
        # the materialize init needs root solely to write/chown the fsGroup-1000
        # PVC path; capabilities are otherwise dropped.
        agent = self._agent(clusterplane_kubeconfig=True)
        init = _fleet_kubeconfig_init_container(agent)
        self.assertEqual(init.security_context.run_as_user, 0)
        self.assertFalse(init.security_context.allow_privilege_escalation)
        dropped = set(init.security_context.capabilities.drop or [])
        added = set(init.security_context.capabilities.add or [])
        self.assertEqual(added, {'CHOWN', 'FOWNER', 'DAC_OVERRIDE'})
        self.assertTrue('ALL' in dropped)

    def test_flag_flip_is_model_level_and_defaults_off(self):
        # the migration default is False — existing seats are unaffected.
        self.assertFalse(FleetAgent._meta.get_field('clusterplane_kubeconfig').default)
