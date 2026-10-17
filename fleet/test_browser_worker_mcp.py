"""Private per-agent browser MCP worker wiring (operator 2026-07-29)."""
from django.test import SimpleTestCase, override_settings
from types import SimpleNamespace
from unittest.mock import MagicMock

from kubernetes.client.rest import ApiException

from fleet import provisioner


class BrowserWorkerMcpHelpersTests(SimpleTestCase):
    def test_needs_browser_from_stealth_skill(self):
        agent = SimpleNamespace(
            skill_config={'skills': ['human-browser-stealth'], 'eager_skills': []},
            sidecar_set=[],
            env={},
            role='Engineer',
        )
        self.assertTrue(provisioner.agent_needs_browser_mcp(agent))

    def test_needs_browser_from_social_media_role(self):
        agent = SimpleNamespace(
            skill_config={},
            sidecar_set=[],
            env={},
            role='Social Media Manager',
        )
        self.assertTrue(provisioner.agent_needs_browser_mcp(agent))

    def test_does_not_need_browser_for_generic_agent(self):
        agent = SimpleNamespace(
            skill_config={'skills': ['engineering-core'], 'eager_skills': []},
            sidecar_set=['broker'],
            env={},
            role='Engineer',
        )
        self.assertFalse(provisioner.agent_needs_browser_mcp(agent))

    def test_service_url_is_private_per_agent(self):
        agent = SimpleNamespace(agent_username='nori')
        url = provisioner.browser_worker_service_url(agent)
        self.assertIn('browser-nori.', url)
        self.assertTrue(url.endswith('/mcp'))
        self.assertNotIn('shizuha-browser.shizuha-browser', url)

    def test_browser_worker_pod_pins_amd64(self):
        agent = SimpleNamespace(agent_username='nori', arch='arm64')
        spec = provisioner._browser_worker_pod_spec(agent, 'img:tag')
        self.assertEqual(spec.node_selector, {'kubernetes.io/arch': 'amd64'})
        keys = {t.key for t in (spec.tolerations or [])}
        self.assertIn('node.shizuha/workstation', keys)
        # s1 carries the operator=unschedulable maintenance taint (PLAT-5936);
        # browser-worker PVCs are local-path pinned to s1, so the worker must
        # tolerate it or it stays Unschedulable and the agent's browser MCP
        # never comes up (ShizuhaAgentMcpReconnectWedge).
        self.assertIn('operator', keys)


@override_settings(
    SHIZUHA_FLEET_NAMESPACE='shizuha-fleet',
    SHIZUHA_BROWSER_NAMESPACE='shizuha-browser',
    SHIZUHA_PLATFORM_NAMESPACE='shizuha',
)
class BrowserMcpJwtContractTests(SimpleTestCase):
    """HIVE-1922: signer and verifier consume byte-identical namespace copies."""

    def setUp(self):
        self.agent = SimpleNamespace(
            agent_username='nori', namespace='shizuha-fleet', arch='arm64',
            skill_config={'skills': ['human-browser-stealth'], 'eager_skills': []},
            sidecar_set=[], env={}, role='Engineer', platform_url='http://platform',
            agent_id='agent-nori', display_name='Nori', model='', image='',
            resource_overrides={}, resource_profile='standard', reasoning_effort='',
            context_prompt='', model_fallbacks=[], model_overrides={},
            personality_traits={}, image_pull_policy='IfNotPresent',
            workspace_mount_path='/home/agent/.shizuha', shared_workspace=False,
            requires_longhorn=False, node_pool='', node_name='',
            node_selector_overrides={}, http_port=8080, metrics_port=9103, mcp_config={},
            hostplane_ssh=False,
            # PLAT-7463: _agent_container reads this flag-gated field; falsy
            # default keeps the cluster-plane projection legs off, which is
            # what this contract test's assertions assume.
            clusterplane_kubeconfig='',
        )

    @staticmethod
    def _secret(name, namespace, encoded, labels=None):
        return provisioner.client.V1Secret(
            metadata=provisioner.client.V1ObjectMeta(
                name=name, namespace=namespace, labels=labels or {},
            ),
            data={provisioner.BROWSER_MCP_JWT_SECRET_KEY: encoded},
            type='Opaque',
        )

    def _core(self, targets=None, source='cGxhdGZvcm0tanV0'):
        targets = dict(targets or {})
        core = MagicMock()

        def read(name, namespace, **_kwargs):
            if (namespace, name) == ('shizuha', 'shizuha-secrets'):
                return self._secret(name, namespace, source)
            target = targets.get(namespace)
            if name == provisioner.BROWSER_MCP_JWT_SECRET_NAME and target is not None:
                return self._secret(
                    name, namespace, target, labels={
                        'app.kubernetes.io/managed-by': provisioner.MANAGED_BY,
                        'shizuha.io/component': 'browser-mcp-auth',
                    },
                )
            raise ApiException(status=404, reason='Not Found')

        core.read_namespaced_secret.side_effect = read
        return core

    def test_renderer_refs_one_required_secret_contract(self):
        runtime = provisioner._agent_container(self.agent)
        runtime_refs = {
            item.name: item.value_from.secret_key_ref
            for item in runtime.env
            if item.name in {'SHIZUHA_BROWSER_MCP_JWT_SECRET', 'JWT_SECRET_KEY'}
        }
        worker = provisioner._browser_worker_pod_spec(self.agent, 'browser:test')
        worker_ref = next(
            item.value_from.secret_key_ref
            for item in worker.containers[0].env
            if item.name == 'JWT_SECRET_KEY'
        )
        self.assertEqual(set(runtime_refs), {
            'SHIZUHA_BROWSER_MCP_JWT_SECRET', 'JWT_SECRET_KEY',
        })
        for ref in [*runtime_refs.values(), worker_ref]:
            self.assertEqual(ref.name, provisioner.BROWSER_MCP_JWT_SECRET_NAME)
            self.assertEqual(ref.key, provisioner.BROWSER_MCP_JWT_SECRET_KEY)
            self.assertFalse(ref.optional)

    def test_missing_target_copies_identical_source_to_both_namespaces(self):
        core = self._core()
        self.assertTrue(provisioner._ensure_browser_mcp_jwt_secrets(core, self.agent))
        calls = core.create_namespaced_secret.call_args_list
        self.assertEqual([call.args[0] for call in calls], [
            'shizuha-fleet', 'shizuha-browser',
        ])
        bodies = [call.args[1] for call in calls]
        self.assertEqual(
            {body.data[provisioner.BROWSER_MCP_JWT_SECRET_KEY] for body in bodies},
            {'cGxhdGZvcm0tanV0'},
        )

    def test_matching_targets_are_a_stable_noop(self):
        core = self._core(targets={
            'shizuha-fleet': 'cGxhdGZvcm0tanV0',
            'shizuha-browser': 'cGxhdGZvcm0tanV0',
        })
        self.assertTrue(provisioner._ensure_browser_mcp_jwt_secrets(core, self.agent))
        core.create_namespaced_secret.assert_not_called()
        core.patch_namespaced_secret.assert_not_called()

    def test_existing_divergent_target_is_reconciled_not_preserved(self):
        core = self._core(targets={
            'shizuha-fleet': 'c3RhbGUtc2lnbmVy',
            'shizuha-browser': 'cGxhdGZvcm0tanV0',
        })
        self.assertTrue(provisioner._ensure_browser_mcp_jwt_secrets(core, self.agent))
        core.create_namespaced_secret.assert_not_called()
        core.patch_namespaced_secret.assert_called_once()
        name, namespace, body = core.patch_namespaced_secret.call_args.args
        self.assertEqual(name, provisioner.BROWSER_MCP_JWT_SECRET_NAME)
        self.assertEqual(namespace, 'shizuha-fleet')
        self.assertEqual(
            body['data'][provisioner.BROWSER_MCP_JWT_SECRET_KEY],
            'cGxhdGZvcm0tanV0',
        )

    def test_missing_source_fails_closed_without_target_writes(self):
        core = MagicMock()
        core.read_namespaced_secret.side_effect = ApiException(status=404, reason='Not Found')
        self.assertFalse(provisioner._ensure_browser_mcp_jwt_secrets(core, self.agent))
        core.create_namespaced_secret.assert_not_called()
        core.patch_namespaced_secret.assert_not_called()
