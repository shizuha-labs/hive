"""Host-daemon LEGACY_LOCAL_DAEMON_RETIRED errors are not fleet failures."""
from django.test import SimpleTestCase

from fleet.runtime_placement import is_retired_host_spawn_observation


class RetiredHostSpawnObservationTests(SimpleTestCase):
    def test_detects_live_ichi_message(self):
        self.assertTrue(is_retired_host_spawn_observation({
            'status': 'failed',
            'status_message': (
                'refusing host-local spawn for Ichi (runtime=k8s): '
                'local daemon retired; fleet agents are k3s-only. '
                'See /home/phoenix/.shizuha/LEGACY_LOCAL_DAEMON_RETIRED'
            ),
        }))

    def test_ignores_real_runtime_failures(self):
        self.assertFalse(is_retired_host_spawn_observation({
            'status': 'failed',
            'status_message': 'ImagePullBackOff: shizuha-agent-runtime',
        }))
        self.assertFalse(is_retired_host_spawn_observation({
            'status': 'running',
            'status_message': '',
        }))
