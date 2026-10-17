from pathlib import Path

import yaml
from django.test import SimpleTestCase

from claws.provisioner import _FREE_TIER_DEFAULT


class DeepSeekOnlyReleaseTests(SimpleTestCase):
    def test_every_claw_default_source_is_deepseek_v4_flash(self):
        self.assertEqual(_FREE_TIER_DEFAULT.llm_default_model, 'DeepSeek-V4-Flash')

        tiers = yaml.safe_load(Path('k8s/hive-tiers.yaml').read_text())
        tier_config = yaml.safe_load(tiers['data']['tiers.yaml'])
        self.assertEqual(
            tier_config['tiers']['free']['llm']['default_model'],
            'DeepSeek-V4-Flash',
        )

        backend = yaml.safe_load(Path('k8s/hive-backend-live.yaml').read_text())
        hive = next(
            container
            for container in backend['spec']['template']['spec']['containers']
            if container['name'] == 'hive'
        )
        env = {item['name']: item.get('value') for item in hive['env']}
        self.assertEqual(env['HIVE_CORTEX_DEFAULT_MODEL'], 'DeepSeek-V4-Flash')

    def test_fleet_release_applies_and_verifies_deepseek_only_defaults(self):
        release = Path('.forgejo/scripts/ring-release-fleet.sh').read_text()

        self.assertIn('kubectl apply -f k8s/hive-tiers.yaml', release)
        self.assertIn(
            'ensure_env(container, "HIVE_CORTEX_DEFAULT_MODEL", "DeepSeek-V4-Flash")',
            release,
        )
        self.assertIn('Hive Cortex default is not DeepSeek-only', release)
