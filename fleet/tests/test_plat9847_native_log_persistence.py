"""PLAT-9847: native seats' structured agent log must be PVC-persistent.

PLAT-8958 regression on native Hive-owned seats: ``_agent_container`` mounted
the workspace PVC only at ``/home/agent/.shizuha``, leaving the agent log
(``~/.config/shizuha/logs/shizuha.log``, SCLI-410 file-only sink) on the
container overlay — every Recreate rotation erased the failure window. The
fix mirrors the reviewed legacy controller lane: mount the workspace PVC at
the log dir via the ``agent-logs`` subPath, with an unconditional root init
container that materializes the subPath directory with agent-owned
permissions (kubelet-created subPath dirs are root-owned and fsGroup does
not chown subPaths).
"""
from types import SimpleNamespace

from django.test import SimpleTestCase, override_settings

from fleet import provisioner


@override_settings(
    SHIZUHA_FLEET_NAMESPACE='shizuha-fleet',
    SHIZUHA_BROWSER_NAMESPACE='shizuha-browser',
    SHIZUHA_PLATFORM_NAMESPACE='shizuha',
)
class NativeLogPersistenceTests(SimpleTestCase):
    def setUp(self):
        self.agent = SimpleNamespace(
            agent_username='mika', namespace='shizuha-fleet', arch='arm64',
            skill_config={'skills': [], 'eager_skills': []},
            sidecar_set=[], env={}, role='Engineer', platform_url='http://platform',
            agent_id='agent-mika', display_name='Mika', model='', image='',
            resource_overrides={}, resource_profile='standard', reasoning_effort='',
            context_prompt='', model_fallbacks=[], model_overrides={},
            personality_traits={}, image_pull_policy='IfNotPresent',
            workspace_mount_path='/home/agent/.shizuha', shared_workspace=False,
            requires_longhorn=False, node_pool='', node_name='',
            node_selector_overrides={}, http_port=8080, metrics_port=9103,
            mcp_config={}, hostplane_ssh=False, clusterplane_kubeconfig='',
            sandbox_required=False, tolerations=[],
            workspace_pvc_name='agent-mika-workspace', deployment_name='agent-mika',
            storage_class='', pvc_size='',
        )

    def test_agent_container_mounts_log_dir_from_pvc_subpath(self):
        container = provisioner._agent_container(self.agent)
        log_mounts = [
            m for m in (container.volume_mounts or [])
            if m.mount_path == '/home/agent/.config/shizuha/logs'
        ]
        self.assertEqual(len(log_mounts), 1)
        mount = log_mounts[0]
        self.assertEqual(mount.name, 'workspace')
        self.assertEqual(mount.sub_path, 'agent-logs')
        self.assertFalse(mount.read_only)

    def test_agent_logs_init_container_is_safe_and_agent_owned(self):
        init = provisioner._agent_logs_init_container(self.agent)
        self.assertEqual(init.name, 'agent-logs-materialize')
        # Root is required to chown; privilege escalation stays fenced and
        # capabilities are explicitly bounded to the ownership change.
        self.assertEqual(init.security_context.run_as_user, 0)
        self.assertFalse(init.security_context.allow_privilege_escalation)
        dropped = set(init.security_context.capabilities.drop or [])
        added = set(init.security_context.capabilities.add or [])
        self.assertEqual(dropped, {'ALL'})
        self.assertEqual(added, {'CHOWN', 'FOWNER', 'DAC_OVERRIDE'})
        # It only touches the log dir on the workspace PVC.
        self.assertEqual(
            [(m.name, m.mount_path) for m in init.volume_mounts],
            [('workspace', '/home/agent/.shizuha')],
        )
        self.assertIn('mkdir -p /home/agent/.shizuha/agent-logs', init.args[0])
        self.assertIn('chown 1000:1000 /home/agent/.shizuha/agent-logs', init.args[0])

    def test_pod_spec_includes_init_container_reconcile_idempotent(self):
        spec1 = provisioner._build_pod_spec(self.agent)
        spec2 = provisioner._build_pod_spec(self.agent)
        for spec in (spec1, spec2):
            names = [c.name for c in (spec.init_containers or [])]
            # Unconditional: present even with no dind/ssh/clusterplane legs.
            self.assertIn('agent-logs-materialize', names)
            # Exactly once, and first — materialize before any other consumer.
            self.assertEqual(names.count('agent-logs-materialize'), 1)
            self.assertEqual(names[0], 'agent-logs-materialize')
        # Reconcile-safe: re-rendering produces byte-identical init shape.
        self.assertEqual(
            [(c.name, c.args) for c in spec1.init_containers],
            [(c.name, c.args) for c in spec2.init_containers],
        )
