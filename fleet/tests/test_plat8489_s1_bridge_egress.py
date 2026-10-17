"""PLAT-8489 regression guard: the fleet-agent egress allowlist must keep the
s1 bridge ports (ori MCP :9500, google-drive MCP :9600, fleet-control :8016).

The 2026-09-10 fleet-wide MCP wedge was a silent clip: ``fleet-agent-allow-egress``
allowed 22/80/443 to the tailnet/LAN but had no rule for the s1 bridge ports, and
the WAN rule's ``except`` list excludes 100.64.0.0/10 — so cross-node agent pods
could not reach ori/drive/fleet-control on s1 while every s1-side listener was
healthy (tcpdump-verified sender-side drop). This test pins the dedicated s1
bridge rule so the ports cannot be clipped again, and pins the WAN carve-out so
the fix cannot be "simplified" into widening WAN egress instead.
"""
from pathlib import Path

import yaml

from django.test import SimpleTestCase

S1_TAILNET = '100.64.0.3/32'
S1_LAN_LEGACY = '192.168.0.136/32'
S1_LAN = '192.168.88.136/32'
S1_NODE_CIDRS = frozenset({S1_TAILNET, S1_LAN_LEGACY, S1_LAN})
S1_BRIDGE_PORTS = {9500, 9600, 8016}


class Plat8489S1BridgeEgressTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        documents = list(yaml.safe_load_all(
            Path('k8s/fleet/30-fleet-isolation-netpol.yaml').read_text()
        ))
        cls.policy = next(
            item for item in documents
            if item['metadata']['name'] == 'fleet-agent-allow-egress'
        )

    def _s1_bridge_rule(self):
        for rule in self.policy['spec']['egress']:
            cidrs = {
                peer.get('ipBlock', {}).get('cidr')
                for peer in rule.get('to', [])
            }
            if S1_TAILNET in cidrs and cidrs <= S1_NODE_CIDRS:
                return rule
        self.fail(
            f'fleet-agent-allow-egress is missing the s1 bridge egress rule '
            f'(ipBlocks {S1_TAILNET} + LAN {S1_LAN}); agent pods cannot reach '
            f'ori/drive/fleet-control on s1 without it (PLAT-8489)'
        )

    def test_s1_bridge_rule_allows_all_three_bridge_ports(self):
        rule = self._s1_bridge_rule()
        ports = {int(item['port']) for item in rule['ports']}
        self.assertEqual(ports, S1_BRIDGE_PORTS)
        self.assertEqual({item['protocol'] for item in rule['ports']}, {'TCP'})

    def test_s1_bridge_rule_is_scoped_to_node_set_cidrs(self):
        # /32 node-set per the PLAT-6041 constraints: the rule must not widen
        # to a whole tailnet/LAN supernet.
        rule = self._s1_bridge_rule()
        for peer in rule['to']:
            cidr = peer['ipBlock']['cidr']
            self.assertIn(cidr, S1_NODE_CIDRS)
            self.assertNotIn('except', peer['ipBlock'])

    def test_wan_rule_still_carves_out_the_tailnet(self):
        # The s1 reachability must come from the dedicated bridge rule, not
        # from a widened WAN rule.
        wan_rules = [
            rule for rule in self.policy['spec']['egress']
            if any(
                peer.get('ipBlock', {}).get('cidr') == '0.0.0.0/0'
                for peer in rule.get('to', [])
            )
        ]
        self.assertTrue(wan_rules, 'expected the WAN egress rule to exist')
        for rule in wan_rules:
            for peer in rule['to']:
                ip_block = peer.get('ipBlock', {})
                if ip_block.get('cidr') == '0.0.0.0/0':
                    self.assertIn(
                        '100.64.0.0/10', ip_block.get('except', []),
                        'WAN egress must keep excluding the tailnet (PLAT-8489)',
                    )
