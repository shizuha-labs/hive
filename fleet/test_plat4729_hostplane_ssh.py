"""PLAT-4729 / PLAT-6041: host-plane SSH egress class in the shared fleet namespace.

Decision (kumo on PLAT-6041, Option 1, under wiki SDR 74b498f3): restore the
privileged host-plane SSH egress class, rendered by Hive, scoped to the tailnet
node set as ``/32``s — never ``100.64.0.0/10`` — on TCP/22 only, selected by
``shizuha.io/agent-role=devops-ssh`` which Hive sets on designated DevOps seats.

These tests pin the parts of that decision a later edit could quietly undo.
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from kubernetes.client.rest import ApiException

from fleet import provisioner
from fleet.models import FleetAgent

MANIFEST = Path('k8s/fleet/30-fleet-isolation-netpol.yaml')
WORKFLOW = Path('.forgejo/workflows/deploy-fleet-networkpolicy.yml')
POLICY_NAME = 'fleet-allow-hostplane-ssh-egress-v2'
TAILNET_SUPERNET = '100.64.0.0/10'


class Plat4729HostplaneSshPolicyTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.documents = list(yaml.safe_load_all(MANIFEST.read_text()))
        cls.by_name = {item['metadata']['name']: item for item in cls.documents}

    @property
    def policy(self):
        self.assertIn(
            POLICY_NAME, self.by_name,
            f'{POLICY_NAME} is missing from {MANIFEST}; without it a DevOps seat '
            'in the shared namespace has zero tailnet reach.',
        )
        return self.by_name[POLICY_NAME]

    def test_policy_is_hive_rendered_into_the_shared_fleet_namespace(self):
        # The decision's "Hive renders … no hand-applied objects, no self-grant"
        # means this object ships in the manifest the deploy workflow applies —
        # not from provisioner code (that plane emits into fleet-<username>
        # namespaces, which have no members) and never from an agent's kubectl.
        policy = self.policy
        self.assertEqual(policy['kind'], 'NetworkPolicy')
        self.assertEqual(policy['metadata']['namespace'], 'shizuha-fleet')
        self.assertEqual(
            policy['metadata']['labels']['app.kubernetes.io/managed-by'],
            'shizuha-hive',
        )
        self.assertEqual(policy['spec']['policyTypes'], ['Egress'])

    def test_selector_is_the_devops_ssh_role_label(self):
        self.assertEqual(
            self.policy['spec']['podSelector'],
            {'matchLabels': {'shizuha.io/agent-role': 'devops-ssh'}},
        )

    def test_egress_is_tcp_22_only(self):
        ports = {
            (item.get('protocol'), int(item['port']))
            for rule in self.policy['spec']['egress']
            for item in rule['ports']
        }
        self.assertEqual(ports, {('TCP', 22)})

    def test_peers_are_a_node_set_of_slash_32_ip_blocks(self):
        peers = [peer for rule in self.policy['spec']['egress'] for peer in rule['to']]
        self.assertTrue(peers, 'an empty node set silently denies all host-plane SSH')
        cidrs = []
        for peer in peers:
            self.assertEqual(
                set(peer), {'ipBlock'},
                f'host-plane peers are ipBlock-only; got {sorted(peer)}',
            )
            self.assertEqual(
                set(peer['ipBlock']), {'cidr'},
                'a node-set entry carries no except:; narrow by removing the /32',
            )
            cidrs.append(peer['ipBlock']['cidr'])
        for cidr in cidrs:
            self.assertTrue(
                cidr.endswith('/32'),
                f'{cidr} is not a /32; the SDR requires the tailnet NODE SET, '
                f'and {TAILNET_SUPERNET} is explicitly rejected',
            )
        self.assertNotIn(TAILNET_SUPERNET, cidrs)
        # s1/Phoenix is the host plane PLAT-4729 acceptance 1 reaches.
        self.assertIn('100.64.0.3/32', cidrs)

    def test_the_management_lan_is_never_reachable_on_ssh(self):
        # The per-agent template carried an allow-hostplane-ssh-bootstrap-lan
        # companion opening 10.200.10.0/24:22 for gx10 nodes not yet on Tailscale.
        # A /24 is not a node set, so that companion must not come back. The
        # management LAN legitimately appears in this manifest for the k8s API
        # (6443) — so assert the pairing, not the prefix: no rule anywhere in the
        # shared manifest may reach 10.200.10.0/24 on TCP/22.
        offenders = [
            (policy['metadata']['name'], sorted(
                f"{item.get('protocol')}/{item['port']}" for item in rule.get('ports', [])
            ))
            for policy in self.documents
            for rule in policy['spec'].get('egress', [])
            if any(
                peer.get('ipBlock', {}).get('cidr') == '10.200.10.0/24'
                for peer in rule.get('to', [])
            )
            and any(int(item['port']) == 22 for item in rule.get('ports', []))
        ]
        self.assertEqual(offenders, [])
        # Control: the prefix is present in this manifest (on the k8s-API rule,
        # which serves 6443 and the 443 alias), so the empty result above is a
        # real absence and not a selector that matches nothing.
        lan_ports = {
            int(item['port'])
            for policy in self.documents
            for rule in policy['spec'].get('egress', [])
            if any(
                peer.get('ipBlock', {}).get('cidr') == '10.200.10.0/24'
                for peer in rule.get('to', [])
            )
            for item in rule.get('ports', [])
        }
        self.assertEqual(lan_ports, {6443, 443}, 'k8s API server ports only')

    def test_the_baseline_policies_are_untouched(self):
        # NetworkPolicy allows are additive, so the new policy must not have been
        # "made to work" by loosening the shared baseline. Assert both halves of
        # what the restored class exists to work around: the default-deny still
        # selects every agent pod, and the WAN rule still excludes the tailnet.
        deny = self.by_name['fleet-agent-default-deny']
        self.assertEqual(sorted(deny['spec']['policyTypes']), ['Egress', 'Ingress'])
        self.assertEqual(
            deny['spec']['podSelector']['matchExpressions'],
            [{'key': 'shizuha.io/agent', 'operator': 'Exists'}],
        )
        wan = [
            peer['ipBlock']
            for rule in self.by_name['fleet-agent-allow-egress']['spec']['egress']
            for peer in rule.get('to', [])
            if peer.get('ipBlock', {}).get('cidr') == '0.0.0.0/0'
        ]
        self.assertEqual(len(wan), 1)
        self.assertIn(TAILNET_SUPERNET, wan[0]['except'])

    def test_ci_asserts_the_policy_shape_on_every_reconcile(self):
        workflow = WORKFLOW.read_text()
        self.assertIn(f'SSH_POLICY={POLICY_NAME}', workflow)
        self.assertIn('podSelector must be shizuha.io/agent-role=devops-ssh', workflow)
        self.assertIn('must allow TCP/22 only', workflow)
        self.assertIn('is not a /32', workflow)
        self.assertIn('has no egress peers', workflow)
        self.assertIn('host-plane peers are ipBlock-only', workflow)
        # The manifest path is already a push trigger; the new policy rides the
        # same file, so no trigger change is needed — pin that it still does.
        self.assertIn("- 'k8s/fleet/30-fleet-isolation-netpol.yaml'", workflow)


class Plat4729BaseLabelsTests(SimpleTestCase):
    ROLE_LABEL = 'shizuha.io/agent-role'

    @staticmethod
    def _agent(**overrides):
        defaults = dict(
            agent_username='ichi',
            agent_id='agent-uuid-4729',
            display_name='Ichi',
            platform_url='http://platform',
        )
        defaults.update(overrides)
        return FleetAgent(**defaults)

    def test_plain_shared_namespace_agent_carries_no_role_label(self):
        labels = provisioner._base_labels(self._agent())
        self.assertNotIn(self.ROLE_LABEL, labels)

    def test_hostplane_ssh_designation_emits_the_role_label(self):
        labels = provisioner._base_labels(self._agent(hostplane_ssh=True))
        self.assertEqual(labels[self.ROLE_LABEL], 'devops-ssh')

    def test_isolated_agents_keep_their_existing_role_label(self):
        # PLAT-174's per-agent policy set still selects on this label; the new
        # designation must be additive, not a replacement.
        labels = provisioner._base_labels(self._agent(isolated=True))
        self.assertEqual(labels[self.ROLE_LABEL], 'devops-ssh')

    def test_the_label_reaches_the_pod_template_not_only_the_deployment(self):
        # The NetworkPolicy selects PODS. A label written only onto Deployment
        # metadata would leave the policy selecting zero pods.
        apps_v1 = _MagicMockDeployment()
        core = MagicMock()
        core.read_namespaced_secret.side_effect = ApiException(status=404)
        with patch.object(provisioner, 'get_k8s_client', return_value=(core, apps_v1, MagicMock())):
            provisioner._create_deployment(apps_v1, 'shizuha-fleet', self._agent(hostplane_ssh=True))
        deployment = apps_v1.created
        self.assertEqual(
            deployment.spec.template.metadata.labels[self.ROLE_LABEL],
            'devops-ssh',
        )
        # The immutable selector must stay the per-agent identity label only.
        self.assertNotIn(self.ROLE_LABEL, deployment.spec.selector.match_labels)
        mounts = [m.mount_path for m in deployment.spec.template.spec.containers[0].volume_mounts]
        self.assertIn('/home/agent/.ssh', mounts)
        inits = [c.name for c in (deployment.spec.template.spec.init_containers or [])]
        self.assertIn('fleet-ssh-materialize', inits)
        vol_names = [v.name for v in deployment.spec.template.spec.volumes]
        self.assertIn('fleet-ssh-secret', vol_names)
        self.assertIn('fleet-ssh-home', vol_names)

    def test_apply_hostplane_ssh_binds_reviewed_runtime_image(self):
        # apply_hostplane_ssh used to call _create_deployment without binding
        # reviewed intent, so _resolve_image fell back to settings.FLEET_AGENT_IMAGE
        # (stale harness-20260711c) and ImagePullBackOff'd ena on enable.
        reviewed = (
            'localhost:30500/shizuha-agent-runtime@'
            'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
        )
        apps = _MagicMockDeployment()
        agent = self._agent(hostplane_ssh=True)
        with patch.object(provisioner, '_live_fleet_image', return_value=reviewed), \
             patch.object(provisioner, 'get_k8s_client', return_value=(MagicMock(), apps, None)), \
             patch.object(provisioner, 'ensure_fleet_ssh_secret'), \
             patch.object(provisioner, '_live_ns', return_value='shizuha-fleet'):
            provisioner.apply_hostplane_ssh(agent)
        deployment = apps.created
        self.assertIsNotNone(deployment)
        self.assertEqual(deployment.spec.replicas, 0)
        self.assertEqual(deployment.metadata.resource_version, 'observed-version')
        images = [c.image for c in deployment.spec.template.spec.containers]
        init_images = [c.image for c in (deployment.spec.template.spec.init_containers or [])]
        self.assertIn(reviewed, images)
        self.assertIn(reviewed, init_images)
        self.assertNotIn('harness-20260711c', ' '.join(images + init_images))

    def test_ensure_fleet_ssh_secret_clones_data_without_last_applied(self):
        class _Secret:
            def __init__(self, name, data, annotations=None):
                self.data = data
                self.metadata = type('M', (), {
                    'name': name,
                    'annotations': annotations or {},
                })()

        created = {}

        class _Core:
            def read_namespaced_secret(self, name, ns):
                if name == 'ena-fleet-ssh':
                    raise ApiException(status=404)
                if name == 'ichi-fleet-ssh':
                    return _Secret(name, {'id_rsa': 'x', 'id_rsa.pub': 'y'}, {
                        'kubectl.kubernetes.io/last-applied-configuration': 'LEAK',
                    })
                raise ApiException(status=404)

            def create_namespaced_secret(self, ns, body):
                created['name'] = body.metadata.name
                created['data_keys'] = sorted(body.data)
                created['annotations'] = dict(body.metadata.annotations or {})
                return body

        agent = self._agent(agent_username='ena', hostplane_ssh=True)
        name = provisioner.ensure_fleet_ssh_secret(_Core(), 'shizuha-fleet', agent)
        self.assertEqual(name, 'ena-fleet-ssh')
        self.assertEqual(created['data_keys'], ['id_rsa', 'id_rsa.pub'])
        self.assertNotIn('kubectl.kubernetes.io/last-applied-configuration', created['annotations'])


class _MagicMockDeployment:
    """Minimal apps_v1 stand-in that captures the created Deployment."""

    created = None

    def create_namespaced_deployment(self, namespace, body, **kwargs):  # noqa: ARG002
        self.created = body
        return body

    def read_namespaced_deployment(self, name, namespace):
        existing = MagicMock()
        existing.spec.replicas = 0
        existing.metadata.resource_version = 'observed-version'
        return existing

    def replace_namespaced_deployment(self, name, namespace, body):
        self.created = body
        return body


class Plat4729DesignationIsMutableTests(TestCase):
    """`isolated` could not carry this designation — prove the new field can."""

    def _provisioned_agent(self):
        agent = FleetAgent.objects.create(
            agent_username='ichi-4729',
            agent_id='agent-uuid-4729-db',
            display_name='Ichi',
            platform_url='http://platform',
        )
        agent.provisioned_at = timezone.now()
        agent.save()
        return FleetAgent.objects.get(pk=agent.pk)

    def test_hostplane_ssh_can_be_designated_after_provisioning(self):
        agent = self._provisioned_agent()
        agent.hostplane_ssh = True
        agent.save()
        self.assertTrue(FleetAgent.objects.get(pk=agent.pk).hostplane_ssh)

    def test_isolated_stays_immutable_after_provisioning(self):
        # Control: proves the immutability check is live in this fixture, so the
        # passing test above is a real allowance and not a disabled guard.
        agent = self._provisioned_agent()
        agent.isolated = True
        with self.assertRaises(ValidationError):
            agent.save()

    def test_designation_does_not_move_the_namespace(self):
        agent = self._provisioned_agent()
        before = agent.namespace
        agent.hostplane_ssh = True
        agent.save()
        self.assertEqual(FleetAgent.objects.get(pk=agent.pk).namespace, before)
