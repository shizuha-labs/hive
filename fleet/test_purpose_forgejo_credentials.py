"""PLAT-9357: the native consumer must use the separately provisioned grant."""
import base64
from copy import deepcopy
from unittest import mock

from django.test import SimpleTestCase, override_settings
from kubernetes import client
from kubernetes.client.rest import ApiException

from fleet import provisioner as p
from fleet.models import FleetAgent
from fleet.management.commands.reconcile_agent_forgejo_tokens import _has_token


def secret(name, **values):
    return client.V1Secret(metadata=client.V1ObjectMeta(name=name, uid=name+'-uid', resource_version='10'),
        data={key: base64.b64encode(value.encode()).decode() for key, value in values.items()})


@override_settings(HIVE_NATIVE_RUNTIME_RELEASE_ENABLED=False, FLEET_COORDINATOR_MODEL_TOKEN_URL='')
class PurposeForgejoCallerTests(SimpleTestCase):
    def setUp(self):
        self.agent = FleetAgent(agent_username='nova', agent_id='nova-id', owner_id=3,
            namespace='shizuha-fleet', model='cortex/GLM-5.3-Flash',
            image='registry/shizuha-agent-runtime:reviewed', sidecar_set=['broker'])
        self.agent.save = mock.Mock()
        self.agent._reviewed_runtime_image = self.agent.image
        self.core, self.apps = mock.Mock(), mock.Mock()
        self.secrets = {('shizuha-fleet', 'nova-agent-creds'): secret('nova-agent-creds',
            FORGEJO_TOKEN='legacy-token', CORTEX_API_KEY='cortex-token', AGENT_PASSWORD='old-password')}
        self.core.read_namespaced_secret.side_effect = self.read_secret
        self.core.read_namespaced_persistent_volume_claim.side_effect = ApiException(status=404)
        self.apps.read_namespaced_deployment.side_effect = lambda *a, **kw: deepcopy(self.live)
        self.apps.replace_namespaced_deployment.side_effect = self.replace
        for name, value in [('get_k8s_client', (self.core, self.apps, mock.Mock())),
                            ('ensure_agent_cortex_key', ''), ('_ensure_coordinator_broker_token', True),
                            ('_ensure_projection_keyring', True), ('_log_event', None),
                            ('agent_needs_browser_mcp', False)]:
            patcher = mock.patch.object(p, name, return_value=value)
            patcher.start(); self.addCleanup(patcher.stop)
        patcher = mock.patch.object(p, '_bind_reviewed_runtime_image', side_effect=lambda a: a.image)
        patcher.start(); self.addCleanup(patcher.stop)
        self.mint_patch = mock.patch.object(p, '_mint_agent_forgejo_token')
        self.mint = self.mint_patch.start(); self.addCleanup(self.mint_patch.stop)

    def read_secret(self, name, namespace):
        value = self.secrets.get((namespace, name))
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise ApiException(status=404)
        return deepcopy(value)

    def replace(self, name, namespace, body, **kwargs):
        self.assertEqual(body.metadata.resource_version, self.live.metadata.resource_version)
        self.assertEqual(body.spec.replicas, self.live.spec.replicas)
        self.live = deepcopy(body)
        self.live.metadata.resource_version = str(int(body.metadata.resource_version) + 1)

    def materialize(self, namespace='shizuha-fleet'):
        p._create_deployment(self.apps, namespace, self.agent, replicas=0)
        self.live = deepcopy(self.apps.create_namespaced_deployment.call_args.args[1])
        self.live.metadata.resource_version = '11'
        self.live.metadata.uid = 'nova-deployment'
        return self.live

    def assert_refs(self, deploy, expected):
        agent = next(c for c in deploy.spec.template.spec.containers if c.name == 'agent')
        for name in ('FORGEJO_TOKEN', 'ORIGIN_TOKEN'):
            [entry] = [e for e in agent.env if e.name == name]
            self.assertIsNone(entry.value)
            self.assertEqual(entry.value_from.secret_key_ref.name, expected)
            self.assertEqual(entry.value_from.secret_key_ref.key, 'FORGEJO_TOKEN')
        env = {e.name: e for e in agent.env}
        self.assertEqual(env['CORTEX_API_KEY'].value_from.secret_key_ref.name, 'nova-agent-creds')
        self.assertEqual(env['AGENT_PASSWORD'].value_from.secret_key_ref.name, 'agent-nova-identity')
        broker = next(c for c in deploy.spec.template.spec.containers if c.name == 'broker')
        self.assertEqual(broker.env_from[0].secret_ref.name, 'agent-nova-identity')

    def assert_no_credential_writes(self):
        self.mint.assert_not_called()
        for operation in ('create_namespaced_secret', 'replace_namespaced_secret', 'patch_namespaced_secret'):
            getattr(self.core, operation).assert_not_called()

    def test_legacy_create_then_purpose_grant_drift_reconcile_and_idempotent_rerender(self):
        self.assert_refs(self.materialize(), 'nova-agent-creds')
        self.assertTrue(p.ensure_agent_image(self.agent))
        self.assertFalse(p.native_agent_runtime_contract_drift(self.agent, self.agent.image))
        self.secrets['shizuha-fleet', 'nova-forgejo-token'] = secret('nova-forgejo-token', FORGEJO_TOKEN='purpose-token')
        before = deepcopy(self.secrets)
        self.assertTrue(p.native_agent_runtime_contract_drift(self.agent, self.agent.image))
        self.assertTrue(p.ensure_agent_image(self.agent))
        self.assert_refs(self.live, 'nova-forgejo-token')
        self.assertFalse(p.native_agent_runtime_contract_drift(self.agent, self.agent.image))
        writes = self.apps.replace_namespaced_deployment.call_count
        self.assertTrue(p.ensure_agent_image(self.agent))
        self.assertEqual(self.apps.replace_namespaced_deployment.call_count, writes)
        self.assertEqual({k:v.to_dict() for k,v in self.secrets.items()}, {k:v.to_dict() for k,v in before.items()})
        self.assertEqual(self.live.spec.replicas, 0)
        self.apps.patch_namespaced_deployment_scale.assert_not_called()
        self.assert_no_credential_writes()

    def test_initial_purpose_materialization_preserves_separate_identity_and_cortex_sources(self):
        self.secrets['shizuha-fleet', 'nova-forgejo-token'] = secret('nova-forgejo-token', FORGEJO_TOKEN='purpose-token')
        self.assert_refs(self.materialize(), 'nova-forgejo-token')
        self.assertTrue(_has_token(self.core, self.agent))
        self.assertTrue(p.ensure_agent_forgejo_token(self.agent))
        self.assert_no_credential_writes()
        self.assertNotIn('purpose-token', str(self.live.to_dict()))

    def test_only_confirmed_absence_uses_legacy_and_unknown_refuses_create_and_convergence(self):
        self.materialize()
        for status in (401, 403, 500, 503):
            with self.subTest(status=status):
                self.secrets['shizuha-fleet', 'nova-forgejo-token'] = ApiException(status=status)
                before = self.live.to_dict()
                self.apps.reset_mock()
                self.assertIsNone(_has_token(self.core, self.agent))
                self.assertEqual(p.ensure_agent_forgejo_token(self.agent), '')
                self.assertTrue(p.native_agent_runtime_contract_drift(self.agent, self.agent.image))
                self.assertFalse(p.ensure_agent_image(self.agent))
                with self.assertRaises(ApiException):
                    p._create_deployment(self.apps, 'shizuha-fleet', self.agent, replicas=0)
                self.apps.create_namespaced_deployment.assert_not_called()
                self.apps.replace_namespaced_deployment.assert_not_called()
                self.assertEqual(self.live.to_dict(), before)
        self.assert_no_credential_writes()

    def test_empty_purpose_grant_never_reads_or_repairs_the_legacy_identity(self):
        self.secrets['shizuha-fleet', 'nova-forgejo-token'] = secret('nova-forgejo-token')
        self.assert_refs(self.materialize(), 'nova-forgejo-token')
        self.assertFalse(_has_token(self.core, self.agent))
        self.assertEqual(p.ensure_agent_forgejo_token(self.agent), '')
        self.assertFalse(any(c.args[0] == 'nova-agent-creds' for c in self.core.read_namespaced_secret.call_args_list))
        self.assert_no_credential_writes()

    def test_selection_uses_consumer_namespace_not_shared_grant_or_human_owner(self):
        self.secrets['shizuha-fleet', 'nova-forgejo-token'] = secret('nova-forgejo-token', FORGEJO_TOKEN='shared-grant')
        self.secrets['private-nova', 'nova-agent-creds'] = secret('nova-agent-creds', FORGEJO_TOKEN='private-legacy')
        self.assert_refs(self.materialize('private-nova'), 'nova-agent-creds')
        self.assertEqual(self.core.read_namespaced_secret.call_args_list,
                         [mock.call('nova-forgejo-token', 'private-nova'), mock.call('nova-agent-creds', 'private-nova')])
        self.assert_no_credential_writes()

    def test_origin_alias_is_not_a_secret_owner(self):
        self.agent.agent_username = 'shizuha'
        self.secrets['shizuha-fleet', 'shizuha-forgejo-token'] = secret('shizuha-forgejo-token', FORGEJO_TOKEN='seat-token')
        self.assertEqual(p._forgejo_identity_username(self.agent), 'shizuha-agent')
        self.assertEqual(p._forgejo_secret(self.agent, self.core, 'shizuha-fleet')[0], 'shizuha-forgejo-token')
        self.assertTrue(p.ensure_agent_forgejo_token(self.agent))
        self.assert_no_credential_writes()
