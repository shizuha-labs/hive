"""Exercise the actual policy renderer and serialized Kubernetes contract."""
import json
import os
from types import SimpleNamespace
from unittest import mock
from django.test import SimpleTestCase
from kubernetes.client import ApiClient
from fleet import provisioner


class ManagedHostplaneEgressTests(SimpleTestCase):
    def render(self):
        api=mock.MagicMock()
        agent=SimpleNamespace(agent_username='fixture-agent',expose_http=False)
        provisioner._create_ssh_egress_network_policies(api,'fixture-namespace',agent)
        return {call.args[1].metadata.name:call.args[1] for call in api.create_namespaced_network_policy.call_args_list}

    @mock.patch.dict(os.environ, {'HIVE_HOSTPLANE_SSH_TAILNET_CIDRS':'','HIVE_HOSTPLANE_SSH_BOOTSTRAP_CIDRS':''})
    def test_unconfigured_policies_deny_ssh_without_an_empty_allow_peer_rule(self):
        policies=self.render()
        for name in ('allow-hostplane-ssh-egress','allow-hostplane-ssh-bootstrap-lan'):
            wire=ApiClient().sanitize_for_serialization(policies[name])
            self.assertTrue(wire['spec']['egress'] == [], 'unconfigured policy must serialize an empty deny egress list')
        self.assertFalse(policies['default-deny-all'].spec.egress)
        self.assertIn('Ingress',policies['default-deny-all'].spec.policy_types)
        self.assertIn('Egress',policies['default-deny-all'].spec.policy_types)
        public = policies['allow-llm-https-egress'].spec.egress[0].to[0].ip_block
        self.assertEqual(public.cidr,'0.0.0.0/0')
        self.assertEqual(public._except,['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16', '100.64.0.0/10'])

    @mock.patch.dict(os.environ, {'HIVE_HOSTPLANE_SSH_TAILNET_CIDRS':json.dumps(['100.64.1.9/32','100.64.2.9/32']),
        'HIVE_HOSTPLANE_SSH_BOOTSTRAP_CIDRS':json.dumps(['10.99.1.0/24'])})
    def test_explicit_installation_targets_remain_exact_tcp22_only(self):
        policies=self.render()
        for name,expected in [('allow-hostplane-ssh-egress',['100.64.1.9/32','100.64.2.9/32']),
                              ('allow-hostplane-ssh-bootstrap-lan',['10.99.1.0/24'])]:
            wire=ApiClient().sanitize_for_serialization(policies[name])
            self.assertEqual(len(wire['spec']['egress']),1)
            rule=wire['spec']['egress'][0]
            self.assertEqual([peer['ipBlock']['cidr'] for peer in rule['to']],expected)
            self.assertEqual(rule['ports'],[{'port':22,'protocol':'TCP'}])
            self.assertEqual(wire['spec']['podSelector'],{'matchLabels':{'shizuha.io/agent-role':'devops-ssh'}})

    def test_malformed_cidr_or_shape_refuses_before_any_policy_write(self):
        for field in ('HIVE_HOSTPLANE_SSH_TAILNET_CIDRS','HIVE_HOSTPLANE_SSH_BOOTSTRAP_CIDRS'):
            for raw in ('{}','null','[true]','["10.99.1.1/24"]','["bad"]','["0.0.0.0/0"]','["10.99.1.0/24","10.99.1.0/24"]'):
                api=mock.MagicMock()
                with self.subTest(field=field,shape=raw),mock.patch.dict(os.environ,{'HIVE_HOSTPLANE_SSH_TAILNET_CIDRS':'[]','HIVE_HOSTPLANE_SSH_BOOTSTRAP_CIDRS':'[]',field:raw}):
                    with self.assertRaises(ValueError):
                        provisioner._create_ssh_egress_network_policies(api,'fixture-namespace',SimpleNamespace(agent_username='fixture-agent',expose_http=False))
                api.create_namespaced_network_policy.assert_not_called()
                api.replace_namespaced_network_policy.assert_not_called()
                api.delete_namespaced_network_policy.assert_not_called()
