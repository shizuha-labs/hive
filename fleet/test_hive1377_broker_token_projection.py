"""HIVE-1377 — project the coordinator broker token Secret per-agent namespace.

The broker sidecar's `MCP_AUTH_PROXY_COORDINATOR_TOKEN` env is a
namespace-scoped secretKeyRef with optional=False. Provisioning rendered the
namespace, identity Secret, PVC and Deployment but NEVER projected the shared
`hive-coordinator-broker-token` Secret into per-agent namespaces, so
agent-shizuha-116 sat in `CreateContainerConfigError` / `CrashLoopBackOff`
(kubelet could not resolve the referenced Secret) — the fleet's only
`devops-ssh` host-plane SSH vantage down for 64 minutes.

These tests pin the contract of `_ensure_coordinator_broker_token`: a faithful,
Hive-labelled COPY in the agent namespace, idempotent when already present, and
fail-closed (no silent success) when the shared source Secret exists nowhere.
"""
from unittest import mock
from unittest.mock import MagicMock

from django.test import TestCase, override_settings

from fleet import provisioner


def _agent(**overrides):
    defaults = dict(
        agent_username='hive1377',
        agent_id='agent-uuid-1377',
        display_name='HIVE-1377 seat',
        platform_url='http://platform',
        isolated=True,
    )
    defaults.update(overrides)
    agent = provisioner.FleetAgent(**defaults)
    agent._reviewed_runtime_image = 'registry/shizuha-agent-runtime:test-reviewed'
    return agent


def _source_secret(data=None):
    """A shared-namespace V1Secret the provisioner copies FROM."""
    data = data if data is not None else {'token': 'b3Atc2VjcmV0LXRva2Vu'}  # b64
    return provisioner.client.V1Secret(
        metadata=provisioner.client.V1ObjectMeta(name='hive-coordinator-broker-token',
                                                 namespace='shizuha-fleet'),
        type='Opaque',
        data=data,
    )


@override_settings(
    FLEET_COORDINATOR_MODEL_TOKEN_URL='http://hive.shizuha-hive.svc.cluster.local:8030/hive/api/v1/coordinator/model-token',
    FLEET_COORDINATOR_TOKEN_SECRET_NAME='hive-coordinator-broker-token',
    FLEET_COORDINATOR_TOKEN_SECRET_KEY='token',
    SHIZUHA_FLEET_NAMESPACE='shizuha-fleet',
)
class BrokerTokenProjectionTests(TestCase):
    def _core(self, *, already_in_agent_ns=False, source_present=True, fail_read=False):
        from kubernetes.client.rest import ApiException
        core = MagicMock()
        if already_in_agent_ns:
            core.read_namespaced_secret.return_value = _source_secret()
        else:
            def _read(name, ns, **_):
                if ns == 'shizuha-fleet' and source_present:
                    return _source_secret()
                if fail_read:
                    raise ApiException(status=403, reason='Forbidden')
                raise ApiException(status=404, reason='Not Found')
            core.read_namespaced_secret.side_effect = _read
        return core

    def test_disabled_coordinator_is_a_noop(self):
        agent = _agent()
        core = MagicMock()
        with override_settings(
            FLEET_COORDINATOR_MODEL_TOKEN_URL='',
            FLEET_COORDINATOR_TOKEN_SECRET_NAME='',
        ):
            self.assertTrue(provisioner._ensure_coordinator_broker_token(core, 'fleet-shizuha-1377', agent))
        core.read_namespaced_secret.assert_not_called()
        core.create_namespaced_secret.assert_not_called()

    def test_shared_namespace_target_skips_copy(self):
        """A shared-ns agent already lives beside the shared Secret — no copy."""
        core = MagicMock()
        self.assertTrue(provisioner._ensure_coordinator_broker_token(core, 'shizuha-fleet', _agent()))
        core.read_namespaced_secret.assert_not_called()
        core.create_namespaced_secret.assert_not_called()

    def test_already_projected_short_circuits_without_touching_source(self):
        core = self._core(already_in_agent_ns=True)
        core.read_namespaced_secret.side_effect = None
        core.read_namespaced_secret.return_value = _source_secret()
        result = provisioner._ensure_coordinator_broker_token(core, 'fleet-shizuha-1377', _agent())
        self.assertTrue(result)
        # The target-namespace existence check happens; a create never does.
        self.assertEqual(core.read_namespaced_secret.call_count, 1)
        core.create_namespaced_secret.assert_not_called()

    def test_missing_source_fails_closed_without_creating(self):
        core = self._core(source_present=False)
        result = provisioner._ensure_coordinator_broker_token(core, 'fleet-shizuha-1377', _agent())
        self.assertFalse(result)
        core.create_namespaced_secret.assert_not_called()

    def test_non_404_read_error_propagates(self):
        """A 403/500 is NOT treated as 'missing' — that would mint/copy against
        an outage and churn state."""
        from kubernetes.client.rest import ApiException
        core = MagicMock()
        core.read_namespaced_secret.side_effect = ApiException(status=403, reason='Forbidden')
        with self.assertRaises(ApiException):
            provisioner._ensure_coordinator_broker_token(core, 'fleet-shizuha-1377', _agent())
        core.create_namespaced_secret.assert_not_called()

    def test_projects_faithful_hive_labelled_copy_from_shared_namespace(self):
        core = self._core()  # source present in shizuha-fleet, not in agent ns
        result = provisioner._ensure_coordinator_broker_token(core, 'fleet-shizuha-1377', _agent())
        self.assertTrue(result)
        core.create_namespaced_secret.assert_called_once()
        _ns, body = core.create_namespaced_secret.call_args[0]
        self.assertEqual(_ns, 'fleet-shizuha-1377')
        self.assertEqual(body.metadata.name, 'hive-coordinator-broker-token')
        self.assertEqual(body.metadata.namespace, 'fleet-shizuha-1377')
        # Acceptance: Hive-managed label present alongside the other rendered objects.
        self.assertEqual(body.metadata.labels['app.kubernetes.io/managed-by'], 'shizuha-hive')
        # Faithful byte copy — identical base64 data, never re-encoded.
        self.assertEqual(body.data, {'token': 'b3Atc2VjcmV0LXRva2Vu'})

    def test_409_create_means_concurrent_projection_is_fine(self):
        from kubernetes.client.rest import ApiException
        core = self._core()
        core.create_namespaced_secret.side_effect = ApiException(status=409, reason='AlreadyExists')
        # The source-read path must still resolve (read in target raises 404 first).
        self.assertTrue(provisioner._ensure_coordinator_broker_token(core, 'fleet-shizuha-1377', _agent()))


@override_settings(
    FLEET_COORDINATOR_MODEL_TOKEN_URL='http://hive.shizuha-hive.svc.cluster.local:8030/hive/api/v1/coordinator/model-token',
    FLEET_COORDINATOR_TOKEN_SECRET_NAME='hive-coordinator-broker-token',
    FLEET_COORDINATOR_TOKEN_SECRET_KEY='token',
    SHIZUHA_FLEET_NAMESPACE='shizuha-fleet',
)
class ProvisionFailClosedTests(TestCase):
    """Provisioning/roll must NOT report success while the broker token Secret
    is missing — that is exactly the crash-loop state HIVE-1377 was filed for."""

    def test_provision_agent_refuses_when_broker_token_source_missing(self):
        from kubernetes.client.rest import ApiException
        agent = provisioner.FleetAgent.objects.create(
            agent_username='hive1377-prov', agent_id='agent-uuid-p1',
            display_name='prov', platform_url='http://platform', isolated=True,
        )
        agent._reviewed_runtime_image = 'registry/shizuha-agent-runtime:test-reviewed'
        core = MagicMock()
        core.read_namespaced_secret.side_effect = ApiException(status=404, reason='Not Found')
        with mock.patch.object(provisioner, '_personal_runtime_isolation_gate', return_value=False), \
                mock.patch.object(provisioner, '_daemon_routing_guard'), \
                mock.patch.object(provisioner, '_ensure_namespace'), \
                mock.patch.object(provisioner, '_create_identity_secret'), \
                mock.patch.object(provisioner, '_create_workspace_pvc'), \
                mock.patch.object(provisioner, '_create_ssh_egress_network_policies'), \
                mock.patch.object(provisioner, 'get_k8s_client',
                                  return_value=(core, MagicMock(), MagicMock())), \
                mock.patch.object(provisioner, '_log_event'):
            result = provisioner.provision_agent(agent, agent_password='x')

        self.assertFalse(result)
        core.create_namespaced_secret.assert_not_called()
        agent.refresh_from_db()
        self.assertEqual(agent.status, provisioner.FleetAgent.Status.FAILED)
        self.assertIn('HIVE-1377', agent.status_message)
        self.assertIn('coordinator broker token Secret', agent.status_message)

    def test_ensure_agent_image_refuses_when_broker_token_source_missing(self):
        from kubernetes.client.rest import ApiException
        agent = provisioner.FleetAgent.objects.create(
            agent_username='hive1377-roll', agent_id='agent-uuid-r1',
            display_name='roll', platform_url='http://platform', isolated=True,
        )
        agent._reviewed_runtime_image = 'registry/shizuha-agent-runtime:test-reviewed'
        core = MagicMock()
        core.read_namespaced_secret.side_effect = ApiException(status=404, reason='Not Found')
        with mock.patch.object(provisioner, 'ensure_agent_cortex_key', return_value='fp'), \
                mock.patch.object(provisioner, 'ensure_agent_forgejo_token', return_value=''), \
                mock.patch.object(provisioner, 'get_k8s_client',
                                  return_value=(core, MagicMock(), MagicMock())), \
                mock.patch.object(provisioner, '_log_event'):
            result = provisioner.ensure_agent_image(agent)

        self.assertFalse(result)
        agent.refresh_from_db()
        self.assertIn('HIVE-1377', agent.status_message)
        self.assertIn('coordinator broker token Secret', agent.status_message)
