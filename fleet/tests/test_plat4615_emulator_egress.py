from pathlib import Path
from unittest import TestCase

import yaml


ROOT = Path(__file__).resolve().parents[2]


class FleetEmulatorEgressTests(TestCase):
    def test_fleet_agents_can_reach_only_the_bench_emulator_adb_port(self):
        documents = list(
            yaml.safe_load_all(
                (ROOT / "k8s/fleet/30-fleet-isolation-netpol.yaml").read_text()
            )
        )
        allow = next(
            item
            for item in documents
            if item["metadata"]["name"] == "fleet-agent-allow-egress"
        )

        emulator_rules = [
            rule
            for rule in allow["spec"]["egress"]
            if any(
                peer.get("namespaceSelector", {}).get("matchLabels", {}).get(
                    "kubernetes.io/metadata.name"
                )
                == "bench"
                for peer in rule.get("to", [])
            )
        ]

        self.assertEqual(
            emulator_rules,
            [
                {
                    "to": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {
                                    "kubernetes.io/metadata.name": "bench"
                                }
                            },
                            "podSelector": {
                                "matchLabels": {"app": "android-emulator"}
                            },
                        }
                    ],
                    "ports": [{"protocol": "TCP", "port": 6520}],
                }
            ],
        )
