"""PLAT-6397: the fleet runtime memory floor is a MINIMUM, not a replacement.

Regression: the provisioner's `_resolve_resources` overwrote the profile's
memory limit with the floor, so a heavy gateway agent (8Gi profile) was
silently reduced to 6Gi and OOM-killed (agent-nagi, 14x CrashLoopBackOff on
300K-token sessions). The floor must never LOWER a profile that is already at
or above it.
"""
from django.test import TestCase

from fleet import provisioner
from fleet.models import FleetAgent


class ResolveResourcesFloorTests(TestCase):
    def _agent(self, **kw):
        defaults = dict(
            agent_username='nagi',
            agent_id='nagi-runtime-id',
            display_name='Nagi',
            status=FleetAgent.Status.RUNNING,
            image='registry/shizuha-agent-runtime:current',
            model='cortex/DeepSeek-V4-Flash',
            resource_profile=FleetAgent.ResourceProfile.HEAVY,
        )
        defaults.update(kw)
        return FleetAgent.objects.create(**defaults)

    def test_heavy_gateway_agent_keeps_8gi(self):
        """PLAT-6397 regression: a heavy (8Gi) gateway agent must NOT be
        reduced to the 6Gi gateway floor — that is what OOM-killed nagi."""
        agent = self._agent()
        resources = provisioner._resolve_resources(agent)
        self.assertEqual(resources.limits['memory'], '8Gi')

    def test_stale_low_profile_is_raised_to_floor(self):
        """A stale 2Gi profile (the Sato OOM class) is still raised to the
        6Gi gateway floor."""
        agent = self._agent(resource_profile=FleetAgent.ResourceProfile.SMALL)
        resources = provisioner._resolve_resources(agent)
        self.assertEqual(resources.limits['memory'], '6Gi')

    def test_bridge_agent_gets_8gi_floor(self):
        """Non-gateway (codex-bridge) agents keep the 8Gi floor."""
        agent = self._agent(model='codex/gpt-5')
        resources = provisioner._resolve_resources(agent)
        self.assertEqual(resources.limits['memory'], '8Gi')

    def test_explicit_overrides_remain_authoritative(self):
        """A per-agent resource_overrides pin (e.g. jun's 12Gi) is never
        touched by the floor."""
        agent = self._agent(
            resource_overrides={
                'requests': {'cpu': '500m', 'memory': '2Gi'},
                'limits': {'cpu': '4', 'memory': '12Gi'},
            },
        )
        resources = provisioner._resolve_resources(agent)
        self.assertEqual(resources.limits['memory'], '12Gi')

    def test_memory_mb_parses_binary_and_decimal(self):
        self.assertEqual(provisioner._memory_mb('256Mi'), 256)
        self.assertEqual(provisioner._memory_mb('6Gi'), 6 * 1024)
        self.assertEqual(provisioner._memory_mb('8Gi'), 8 * 1024)
        self.assertEqual(provisioner._memory_mb('536870912'), 512)
        self.assertEqual(provisioner._memory_mb('1G'), int(1000 * 1000 * 1000 / (1024 * 1024)))
        self.assertEqual(provisioner._memory_mb(None), 0)
        self.assertEqual(provisioner._memory_mb(''), 0)
        self.assertEqual(provisioner._memory_mb('bogus'), 0)
