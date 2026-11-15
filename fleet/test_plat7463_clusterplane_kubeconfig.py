"""PLAT-7463: the cluster-plane kubeconfig capability — capability-selection →
render → pod-start sequence.

The Q1a fail-closed invariant (no kube API path from a fleet pod) gains ONE
flag-gated exception: flagged seats project the kubelet-managed SA token and
init-materialize ~/.kube/config referencing the tokenFile (live rotation, no
embedded bearer bytes). Unflagged seats render neither — the default stays
fail-closed. Authorization is NOT granted by the flag (the scoped RBAC is a
separate declarative grant in the deploy repo).
"""
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase, override_settings

from kubernetes import client as k8s_client

from fleet.models import FleetAgent
from fleet.provisioner import (
    FLEET_KUBECONFIG_INIT_NAME,
    _build_pod_spec,
    _converge_clusterplane_pod_contract,
    _converge_kubeconfig_env,
    _fleet_kubeconfig_init_container,
    _fleet_kubeconfig_init_script,
    fleet_kubeconfig_path_for,
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
        script = init.args[0]
        self.assertIn('tokenFile', script)
        self.assertNotIn('token: ey', script)  # no embedded bearer
        # the materialized config lands on the workspace PVC (durable)
        mounts = [m.name for m in init.volume_mounts]
        self.assertIn('workspace', mounts)
        # the agent container points kubectl at the materialized config
        agent_container = next(c for c in spec.containers if c.name == 'agent')
        kubeconfig_env = next(e for e in agent_container.env if e.name == 'KUBECONFIG')
        self.assertEqual(kubeconfig_env.value, fleet_kubeconfig_path_for(agent))

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


class ClusterPlaneExistingDeploymentConvergeTests(SimpleTestCase):
    """PLAT-10308: the plane flag landing on a seat whose Deployment already
    exists — and the hand kubectl-patch ownership problem.

    agent-kai rendered automountServiceAccountToken: false with
    clusterplane_kubeconfig=True because a 2026-09-20 hand kubectl-patch owned
    the field in managedFields and the read-modify-replace update path only
    converged image/resources/lane env. The update path must reconcile the
    provisioner-owned pod contract (automount + initContainers) and the
    KUBECONFIG env from the same render the create path uses — in BOTH flag
    directions.
    """

    def _spec(self, *, automount=False, init_containers=None):
        return SimpleNamespace(
            automount_service_account_token=automount,
            init_containers=init_containers,
        )

    def _agent(self, **kw):
        defaults = dict(
            agent_username='kai',
            clusterplane_kubeconfig=True,
            hostplane_ssh=False,
            image='localhost:30500/shizuha-agent-runtime:test',
            image_pull_policy='IfNotPresent',
        )
        defaults.update(kw)
        return SimpleNamespace(**defaults)

    def test_stale_hand_patch_automount_is_reconciled(self):
        # the exact agent-kai live shape: flag on, automount forced false by a
        # hand patch, no init container.
        spec = self._spec(automount=False, init_containers=None)
        self.assertTrue(_converge_clusterplane_pod_contract(spec, self._agent()))
        self.assertTrue(spec.automount_service_account_token)
        self.assertEqual(
            [c.name for c in spec.init_containers],
            [FLEET_KUBECONFIG_INIT_NAME],
        )

    def test_converge_is_idempotent(self):
        # a converged template must not churn on every reconcile pass.
        spec = self._spec(automount=False, init_containers=None)
        agent = self._agent()
        self.assertTrue(_converge_clusterplane_pod_contract(spec, agent))
        self.assertFalse(_converge_clusterplane_pod_contract(spec, agent))

    def test_unflag_removes_the_contract(self):
        # the flag was cleared on a seat provisioned while flagged: the stale
        # kubeconfig init and the token projection must go (fail-closed Q1a).
        spec = self._spec(
            automount=True,
            init_containers=[k8s_client.V1Container(name=FLEET_KUBECONFIG_INIT_NAME)],
        )
        self.assertTrue(_converge_clusterplane_pod_contract(
            spec, self._agent(clusterplane_kubeconfig=False),
        ))
        self.assertFalse(spec.automount_service_account_token)
        self.assertIsNone(spec.init_containers)

    def test_foreign_inits_are_never_touched(self):
        # native-runtime release seats carry their own runtime init (and dind/
        # ssh have their own flows) — the kubeconfig converge must not delete
        # or reorder them.
        foreign = [k8s_client.V1Container(name='runtime-init'),
                   k8s_client.V1Container(name='docker-cli-init')]
        spec = self._spec(automount=False, init_containers=list(foreign))
        self.assertTrue(_converge_clusterplane_pod_contract(spec, self._agent()))
        names = [c.name for c in spec.init_containers]
        self.assertEqual(names, ['runtime-init', 'docker-cli-init', FLEET_KUBECONFIG_INIT_NAME])
        # unflag removes only the kubeconfig init, not the foreign ones.
        spec2 = self._spec(automount=True, init_containers=list(foreign) + [
            k8s_client.V1Container(name=FLEET_KUBECONFIG_INIT_NAME)])
        self.assertTrue(_converge_clusterplane_pod_contract(
            spec2, self._agent(clusterplane_kubeconfig=False)))
        self.assertEqual([c.name for c in spec2.init_containers],
                         ['runtime-init', 'docker-cli-init'])

    def _container(self, env):
        return SimpleNamespace(env=list(env))

    def test_kubeconfig_env_added_on_flagged_seat(self):
        container = self._container([SimpleNamespace(name='MODEL', value='cortex/grok-4.5')])
        self.assertTrue(_converge_kubeconfig_env(container, self._agent()))
        names = [e.name for e in container.env]
        self.assertIn('MODEL', names)
        kc = next(e for e in container.env if e.name == 'KUBECONFIG')
        self.assertEqual(kc.value, fleet_kubeconfig_path_for(self._agent()))

    def test_kubeconfig_env_converge_is_idempotent(self):
        container = self._container([
            SimpleNamespace(name='KUBECONFIG', value=fleet_kubeconfig_path_for(self._agent())),
        ])
        self.assertFalse(_converge_kubeconfig_env(container, self._agent()))

    def test_stale_kubeconfig_value_is_rewritten(self):
        container = self._container([
            SimpleNamespace(name='KUBECONFIG', value='/legacy/kube/config'),
        ])
        self.assertTrue(_converge_kubeconfig_env(container, self._agent()))
        kc = next(e for e in container.env if e.name == 'KUBECONFIG')
        self.assertEqual(kc.value, fleet_kubeconfig_path_for(self._agent()))

    def test_kubeconfig_env_dropped_on_unflag(self):
        # absence must be obeyed — a stale KUBECONFIG on an unflagged seat
        # points kubectl at a config the init container no longer writes.
        container = self._container([
            SimpleNamespace(name='KUBECONFIG', value='/home/agent/.kube/config'),
            SimpleNamespace(name='MODEL', value='cortex/grok-4.5'),
        ])
        self.assertTrue(_converge_kubeconfig_env(
            container, self._agent(clusterplane_kubeconfig=False),
        ))
        self.assertFalse(any(e.name == 'KUBECONFIG' for e in container.env))
