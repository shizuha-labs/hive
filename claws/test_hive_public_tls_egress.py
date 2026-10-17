"""Public hostname -> private VIP -> edge endpoint, through the reviewed policy."""
import ipaddress
import json
from pathlib import Path
import subprocess
import sys

import yaml
from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[1]


class HivePublicTlsEgressTests(SimpleTestCase):
    def setUp(self):
        self.policies = {p['metadata']['name']: p for p in yaml.safe_load_all(
            (ROOT / 'k8s/shizuha-hive-netpol.yaml').read_text()) if p}
        self.policy = self.policies['allow-platform-egress']

    def permits(self, ip, namespace=None, pod_labels=None, port=443):
        # NetworkPolicy peers use AND between the two selectors in ONE peer;
        # separate peers/rules are OR. Exercise both pre- and post-DNAT shapes.
        for rule in self.policy['spec']['egress']:
            if not any(p.get('protocol', 'TCP') == 'TCP' and p['port'] == port for p in rule['ports']):
                continue
            for peer in rule['to']:
                if 'ipBlock' in peer:
                    address = ipaddress.ip_address(ip)
                    block = peer['ipBlock']
                    if address in ipaddress.ip_network(block['cidr']) and not any(
                            address in ipaddress.ip_network(cidr) for cidr in block.get('except', [])):
                        return True
                    continue
                wanted_namespace = peer.get('namespaceSelector', {}).get('matchLabels', {}).get(
                    'kubernetes.io/metadata.name', 'shizuha-hive')
                if namespace != wanted_namespace:
                    continue
                wanted_labels = peer.get('podSelector', {}).get('matchLabels', {})
                if all((pod_labels or {}).get(key) == value for key, value in wanted_labels.items()):
                    return True
        return False

    def test_public_hostname_vip_then_real_post_dnat_endpoint_are_allowed(self):
        # Live 2026-09-08 capture: shizuha.com -> VIP -> shizuha edge pod.
        self.assertTrue(self.permits('192.168.0.250'))
        self.assertTrue(self.permits('10.42.15.241', 'shizuha', {
            'app.kubernetes.io/name': 'shizuha-edge'}))

    def test_scope_excludes_other_private_targets_workloads_namespaces_and_ports(self):
        for ip, namespace, labels, port in [
            ('192.168.0.249', None, {}, 443),
            ('192.168.0.251', None, {}, 443),
            ('100.64.0.3', None, {}, 443),
            ('10.200.10.15', None, {}, 443),
            ('10.42.15.241', 'shizuha', {'app.kubernetes.io/name':'unrelated'}, 443),
            ('10.42.15.241', 'other', {'app.kubernetes.io/name':'shizuha-edge'}, 443),
            ('10.42.15.241', 'shizuha', {'app.kubernetes.io/name':'shizuha-edge'}, 444),
            ('192.168.0.250', None, {}, 444),
        ]:
            with self.subTest(ip=ip, namespace=namespace, labels=labels, port=port):
                self.assertFalse(self.permits(ip, namespace, labels, port))
        self.assertTrue(self.permits('10.42.15.241', 'shizuha', {'app.kubernetes.io/name':'shizuha-edge'}, 80))
        self.assertEqual(self.policies['allow-external-https-egress']['spec']['egress'], [{
            'to':[{'ipBlock':{'cidr':'0.0.0.0/0','except':[
                '10.0.0.0/8','172.16.0.0/12','192.168.0.0/16','169.254.0.0/16','100.64.0.0/10']}}],
            'ports':[{'protocol':'TCP','port':443}]}])

    def test_real_renderer_outputs_only_canonical_existing_policy(self):
        process = subprocess.run([sys.executable, str(ROOT / '.forgejo/scripts/render-hive-public-tls-egress.py')],
                                 capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(process.stdout), self.policy)
        self.assertEqual(self.policy['spec']['podSelector'], {})
        self.assertEqual(self.policy['spec']['policyTypes'], ['Egress'])

    def test_ring_reconciles_only_scoped_slice_before_natural_golden_path(self):
        release = (ROOT / '.forgejo/scripts/ring-release-fleet.sh').read_text()
        renderer = release.index('python3 .forgejo/scripts/render-hive-public-tls-egress.py')
        apply = release.index('.forgejo/scripts/kubectl apply -f /tmp/hive-public-tls-egress.json')
        golden = release.index('GOLDEN_PATH_JOB=')
        self.assertLess(renderer, apply)
        self.assertLess(apply, golden)
        self.assertNotIn('-f k8s/shizuha-hive-netpol.yaml', release)
