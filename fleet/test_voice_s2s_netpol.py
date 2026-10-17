"""Voice → grok-voice SCLI ingress is a declared Hive policy, not a hand object."""
from pathlib import Path

import yaml
from django.test import SimpleTestCase

MANIFEST = Path('k8s/fleet/30-fleet-isolation-netpol.yaml')
POLICY_NAME = 'fleet-agent-allow-voice-s2s-ingress'


class VoiceS2SIngressPolicyTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        documents = list(yaml.safe_load_all(MANIFEST.read_text()))
        cls.by_name = {item['metadata']['name']: item for item in documents}

    def test_policy_is_in_the_shared_fleet_manifest(self):
        self.assertIn(POLICY_NAME, self.by_name)
        policy = self.by_name[POLICY_NAME]
        self.assertEqual(policy['metadata']['namespace'], 'shizuha-fleet')
        self.assertEqual(policy['spec']['policyTypes'], ['Ingress'])
        self.assertEqual(
            policy['spec']['podSelector'],
            {'matchLabels': {'shizuha.io/voice-s2s': 'true'}},
        )

    def test_only_the_voice_proxy_may_reach_scli_http(self):
        [rule] = self.by_name[POLICY_NAME]['spec']['ingress']
        self.assertEqual(
            [int(port['port']) for port in rule['ports']],
            [8080],
        )
        [peer] = rule['from']
        self.assertEqual(
            peer['namespaceSelector']['matchLabels'],
            {'kubernetes.io/metadata.name': 'voice'},
        )
        self.assertEqual(peer['podSelector']['matchLabels'], {'app': 'shizuha-voice'})
