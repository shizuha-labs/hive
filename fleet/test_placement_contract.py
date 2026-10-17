from unittest import mock

from django.test import SimpleTestCase

from fleet.views import _merge_native_and_runtime


class DesiredPlacementProjectionTests(SimpleTestCase):
    def test_runtime_adapter_defaults_cannot_overwrite_hive_placement_intent(self):
        native = {
            'agent_username': 'fumi', 'arch': 'amd64',
            'node_selector_overrides': {'kubernetes.io/hostname': 's1'},
            'requires_longhorn': False, 'node_name': '', 'node_pool': '', 'tolerations': [],
        }
        runtime = {'agent_username': 'fumi', 'arch': 'arm64', 'node_selector_overrides': {},
                   'node_name': 'observed-node', 'status': 'running', 'pod_name': 'observed-pod'}
        with mock.patch('fleet.views._native_row_is_live', return_value=False):
            [merged] = _merge_native_and_runtime([native], {'fumi': runtime})
        for field in ('arch', 'requires_longhorn', 'node_selector_overrides', 'node_pool', 'tolerations'):
            self.assertEqual(merged[field], native[field])
        self.assertEqual(merged['node_name'], 'observed-node')
        self.assertEqual(merged['status'], 'running')
        self.assertEqual(merged['pod_name'], 'observed-pod')
