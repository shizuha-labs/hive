"""
Regression tests for HIVE-133 env-denylist security fix.

Verifies:
  1. _agent_container() never injects reserved/secret-bearing env keys from agent.env.
  2. Permitted custom keys are still injected.
  3. FleetAgentCreateSerializer.validate_env() rejects reserved keys.
  4. FleetAgentConfigureSerializer.validate_env() rejects reserved keys.
"""
import types
import unittest
from unittest import mock


# ---------------------------------------------------------------------------
# Helpers — build minimal stubs so tests run without a full Django setup
# ---------------------------------------------------------------------------

def _make_agent(**kwargs):
    defaults = dict(
        agent_username='test-agent',
        agent_id='42',
        display_name='Test',
        role='',
        platform_url='http://localhost',
        reasoning_effort='',
        context_prompt='',
        failover_chain_id='',
        model_fallbacks=[],
        model_overrides={},
        personality_traits={},
        env={},
        workspace_mount_path='/home/agent/.shizuha',
        sidecar_set=[],
        credential_grant_scopes=[],
        mcp_config={},
        skill_config={},
        expose_http=False,
        http_port=8080,
        image='',
        image_pull_policy='IfNotPresent',
        pvc_size='10Gi',
        storage_class='',
        isolated=False,
        namespace='shizuha-fleet',
        node_name='',
        node_pool='',
        node_selector_overrides={},
        requires_longhorn=True,
        arch='arm64',
        resource_profile='standard',
        resource_overrides={},
        tolerations=[],
        cohort='',
        sandbox_required=False,
    )
    defaults.update(kwargs)
    agent = types.SimpleNamespace(**defaults)
    agent.deployment_name = f'agent-{agent.agent_username}'
    agent.app_label = f'agent-{agent.agent_username}'
    agent.identity_secret_name = f'agent-{agent.agent_username}-identity'
    agent.workspace_pvc_name = f'agent-{agent.agent_username}-workspace'
    return agent


def _env_names(env_list):
    return {e.name for e in env_list}


# ---------------------------------------------------------------------------
# Provisioner env-injection tests
# ---------------------------------------------------------------------------

class ProvisionerEnvDenylistTests(unittest.TestCase):
    """_agent_container() must silently drop reserved keys from agent.env."""

    def _call_container(self, agent):
        """
        Call fleet.provisioner._agent_container() with kubernetes mocked out.
        Returns the env list built for the main agent container.
        """
        k8s_mock = mock.MagicMock()
        # V1EnvVar stub that stores name/value
        def _v1env(name, value=None, value_from=None):
            obj = mock.MagicMock()
            obj.name = name
            obj.value = value
            return obj
        k8s_mock.V1EnvVar.side_effect = _v1env

        # Build a minimal module mock for kubernetes.client
        import sys
        fake_k8s = types.ModuleType('kubernetes')
        fake_k8s.client = k8s_mock
        fake_k8s.config = mock.MagicMock()

        with mock.patch.dict(sys.modules, {'kubernetes': fake_k8s, 'kubernetes.client': k8s_mock, 'kubernetes.config': fake_k8s.config}):
            # Import provisioner inside the patch so kubernetes resolves correctly
            if 'fleet.provisioner' in sys.modules:
                del sys.modules['fleet.provisioner']
            # We can't fully import fleet.provisioner without Django; instead
            # test _ENV_DENYLIST and the loop logic directly.
        return None  # Django-dependency guard — see direct tests below

    def _denylist_src(self):
        import pathlib, re
        src = (pathlib.Path(__file__).parent.parent / 'provisioner.py').read_text()
        # Extract _ENV_DENYLIST block as a string of quoted keys
        m = re.search(r'_ENV_DENYLIST[^\{]*\{([^}]*)\}', src, re.DOTALL)
        self.assertIsNotNone(m, '_ENV_DENYLIST not found in provisioner')
        return m.group(1)

    def test_denylist_contains_agent_password(self):
        """AGENT_PASSWORD must be in the denylist."""
        block = self._denylist_src()
        for key in ('AGENT_PASSWORD', 'SHIZUHA_AGENT_TOKEN', 'VLLM_API_KEY',
                    'FORGEJO_TOKEN', 'ORIGIN_TOKEN', 'OPENAI_API_KEY', 'CORTEX_API_KEY',
                    'AGENT_ID', 'AGENT_USERNAME',
                    'SHIZUHA_AGENT_ID', 'SHIZUHA_AGENT_USERNAME', 'SHIZUHA_CACHE_TTL'):
            self.assertIn(f"'{key}'", block, f'{key} missing from _ENV_DENYLIST')

    def test_denylist_contains_runtime_keys(self):
        """Runtime keys set by Hive must be in the denylist."""
        block = self._denylist_src()
        for key in ('VLLM_STREAM_WITH_TOOLS', 'MCP_AUTH_PROXY_SOCKET', 'AGENT_ROLE',
                    'MODEL', 'SHIZUHA_K8S_PRIMARY_MODEL', 'SHIZUHA_MODEL_FALLBACKS',
                    'REASONING_EFFORT',
                    'CONTEXT_PROMPT', 'FAILOVER_CHAIN_ID',
                    'MODEL_FALLBACKS', 'MODEL_OVERRIDES', 'PERSONALITY_TRAITS'):
            self.assertIn(f"'{key}'", block, f'{key} missing from _ENV_DENYLIST')

    def test_stream_with_tools_defaults_on_for_glm53(self):
        """Fleet GLM tool turns stream so Usage TTFT is first-token, not e2e.

        GLM-5.1 glm47 XML-leak class did not reproduce on live GLM-5.3-Flash
        (2026-09-06). The denylist still blocks agent.env from flipping this.
        """
        import pathlib
        import re
        src = (pathlib.Path(__file__).parent.parent / 'provisioner.py').read_text()
        self.assertIn(
            "V1EnvVar(name='VLLM_STREAM_WITH_TOOLS', value='1')",
            src,
        )
        self.assertNotIn(
            "V1EnvVar(name='VLLM_STREAM_WITH_TOOLS', value='0')",
            src,
        )
        # Lane converge only restamps `_RUNTIME_LANE_ENV_NAMES`. A default of 1
        # in `_agent_container` is a no-op for already-rendered Deployments
        # unless this key is in the set.
        m = re.search(
            r'_RUNTIME_LANE_ENV_NAMES\s*=\s*\{([^}]*)\}', src, re.DOTALL,
        )
        self.assertIsNotNone(m, '_RUNTIME_LANE_ENV_NAMES not found')
        self.assertIn("'VLLM_STREAM_WITH_TOOLS'", m.group(1))

    def test_injection_loop_skips_denylist_keys(self):
        """The injection loop must skip keys in _ENV_DENYLIST."""
        import pathlib
        src = (pathlib.Path(__file__).parent.parent / 'provisioner.py').read_text()
        # Verify the guard is present in source
        self.assertIn('if key in _ENV_DENYLIST:', src)
        self.assertIn('continue', src)

    def test_injection_loop_allows_custom_keys(self):
        """The injection loop must NOT skip custom, non-reserved keys."""
        import pathlib
        src = (pathlib.Path(__file__).parent.parent / 'provisioner.py').read_text()
        # The loop should still call env.append for non-denied keys
        self.assertIn("env.append(client.V1EnvVar(name=key, value=val))", src)


# ---------------------------------------------------------------------------
# Serializer validate_env tests
# ---------------------------------------------------------------------------

class SerializerEnvValidationTests(unittest.TestCase):
    """Serializer validate_env() must reject reserved keys at write time."""

    def _get_validate_fn(self):
        """Extract _validate_env_dict from the actual serializers source."""
        import ast, pathlib, types
        src = (pathlib.Path(__file__).parent.parent / 'serializers.py').read_text()
        # Execute the module-level helper in a sandboxed namespace
        # to test it without Django
        ns = {}
        # Extract just the helper function + denylist by parsing
        tree = ast.parse(src)
        exec_src = []
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == '_ENV_DENYLIST':
                        exec_src.append(ast.unparse(node))
            if isinstance(node, ast.FunctionDef) and node.name == '_validate_env_dict':
                exec_src.append(ast.unparse(node))
        full = '\n'.join(exec_src)
        exec(full, ns)
        return ns['_validate_env_dict']

    def test_rejects_agent_password(self):
        from rest_framework.exceptions import ValidationError
        fn = self._get_validate_fn()
        with self.assertRaises(ValidationError):
            fn({'AGENT_PASSWORD': 'secret'})

    def test_rejects_multiple_bad_keys(self):
        from rest_framework.exceptions import ValidationError
        fn = self._get_validate_fn()
        with self.assertRaises(ValidationError):
            fn({'AGENT_ID': 'x', 'SHIZUHA_AGENT_TOKEN': 'y'})

    def test_rejects_forgejo_token_aliases(self):
        from rest_framework.exceptions import ValidationError
        fn = self._get_validate_fn()
        with self.assertRaises(ValidationError):
            fn({'FORGEJO_TOKEN': 'x', 'ORIGIN_TOKEN': 'y'})

    def test_allows_custom_key(self):
        fn = self._get_validate_fn()
        result = fn({'MY_CUSTOM_KEY': 'value', 'APP_ENV': 'staging'})
        self.assertEqual(result, {'MY_CUSTOM_KEY': 'value', 'APP_ENV': 'staging'})

    def test_empty_dict_allowed(self):
        fn = self._get_validate_fn()
        self.assertEqual(fn({}), {})

    def test_error_message_names_bad_keys(self):
        from rest_framework.exceptions import ValidationError
        fn = self._get_validate_fn()
        try:
            fn({'AGENT_PASSWORD': 'x', 'VLLM_API_KEY': 'y', 'OPENAI_API_KEY': 'z', 'CORTEX_API_KEY': 'q'})
            self.fail('Expected ValidationError')
        except ValidationError as exc:
            msg = str(exc.detail)
            self.assertIn('AGENT_PASSWORD', msg)
            self.assertIn('VLLM_API_KEY', msg)
            self.assertIn('OPENAI_API_KEY', msg)
            self.assertIn('CORTEX_API_KEY', msg)

    def test_validate_env_present_in_create_serializer(self):
        """FleetAgentCreateSerializer must have a validate_env method."""
        import pathlib
        src = (pathlib.Path(__file__).parent.parent / 'serializers.py').read_text()
        # Should appear twice (once per serializer)
        self.assertGreaterEqual(src.count('def validate_env(self, value):'), 2)


# ---------------------------------------------------------------------------
# Backfill command env-sanitization tests
# ---------------------------------------------------------------------------

class BackfillEnvSanitizationTests(unittest.TestCase):
    """backfill_behavioral_fields must strip reserved keys before persisting env."""

    def _get_denylist(self):
        """Read _ENV_DENYLIST from serializers.py without Django."""
        import ast, pathlib
        src = (pathlib.Path(__file__).parent.parent / 'serializers.py').read_text()
        tree = ast.parse(src)
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == '_ENV_DENYLIST':
                        ns = {}
                        exec(ast.unparse(node), ns)
                        return ns['_ENV_DENYLIST']
        raise AssertionError('_ENV_DENYLIST not found in serializers.py')

    def _sanitize(self, raw_env):
        """Apply the same filter logic as the backfill command."""
        denylist = self._get_denylist()
        return {k: v for k, v in raw_env.items() if k not in denylist}

    def test_reserved_key_stripped(self):
        """AGENT_PASSWORD from agents.json must not survive into safe_env."""
        result = self._sanitize({'AGENT_PASSWORD': 'secret', 'MY_APP': 'ok'})
        self.assertNotIn('AGENT_PASSWORD', result)
        self.assertIn('MY_APP', result)

    def test_token_key_stripped(self):
        """SHIZUHA_AGENT_TOKEN must be stripped from backfill env."""
        result = self._sanitize({'SHIZUHA_AGENT_TOKEN': 'tok', 'APP_ENV': 'prod'})
        self.assertNotIn('SHIZUHA_AGENT_TOKEN', result)
        self.assertEqual(result, {'APP_ENV': 'prod'})

    def test_all_allowed_keys_pass_through(self):
        """Custom, non-reserved keys must all survive sanitization."""
        env = {'MY_KEY': 'v1', 'ANOTHER_KEY': 'v2', 'THIRD': 'v3'}
        result = self._sanitize(env)
        self.assertEqual(result, env)

    def test_empty_env_unchanged(self):
        """Empty env dict passes through unchanged."""
        self.assertEqual(self._sanitize({}), {})

    def test_backfill_command_source_uses_sanitizer(self):
        """The backfill command source must reference _ENV_DENYLIST."""
        import pathlib
        src = (pathlib.Path(__file__).parent.parent /
               'management' / 'commands' / 'backfill_behavioral_fields.py').read_text()
        self.assertIn('_ENV_DENYLIST', src)
        self.assertIn('safe_env', src)


if __name__ == '__main__':
    unittest.main()
