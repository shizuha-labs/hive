"""PLAT-4902 (CTX-496 item 7): declarative scale fallback when control plane is down.

The runtime-fleet daemon + proxies are single replicas node-bound to s1; during
the 2026-07-18 outage every lifecycle POST failed with connection refused for
~5.5h and demanded on-demand starts stalled. `set_runtime_agent_enabled` must
fall back to scaling the agent's existing Deployment ONLY on connection-level
control failure — never on HTTP auth/contract errors, and never by
creating/provisioning anything.
"""
from types import SimpleNamespace
from unittest.mock import patch

import contextlib

from django.conf import settings
from django.test import SimpleTestCase

from .runtime_fleet import _control_unreachable, set_runtime_agent_enabled


if not settings.configured:
    settings.configure(DEFAULT_CHARSET='utf-8')


def _agent_row(username='merge'):
    return SimpleNamespace(
        agent_username=username,
        deployment_name=f'agent-{username}',
    )


@contextlib.contextmanager
def _lease_gate_passing():
    """Post-PLAT-9052 contract: the enable path gates on model-lease authority
    BEFORE the control POST (fail-closed, control-plane-independent). These
    tests establish authority cleanly, then exercise the PLAT-4902 fallback
    composition itself."""
    with patch('fleet.runtime_fleet._find_runtime_agent_row',
               return_value=SimpleNamespace(agent_username='merge')), \
         patch('fleet.runtime_fleet._fleet_agent_row_to_runtime',
               return_value={'effective_model': 'cortex/glm-5.3-flash',
                             'execution_method': 'shizuha'}), \
         patch('fleet.model_lease.lease_wake_available',
               return_value=(True, 'lease confirmed')):
        yield


class ControlUnreachableClassifierTests(SimpleTestCase):
    def test_connection_failure_is_unreachable(self):
        self.assertTrue(_control_unreachable('runtime-fleet unreachable: ConnectionError(...)'))

    def test_http_and_config_failures_are_not_unreachable(self):
        self.assertFalse(_control_unreachable('control enable returned 401: bad secret'))
        self.assertFalse(_control_unreachable('control enable returned 500: boom'))
        self.assertFalse(_control_unreachable('RUNTIME_FLEET_CONTROL_URL unset'))
        self.assertFalse(_control_unreachable('runtime-fleet control secret not configured'))
        self.assertFalse(_control_unreachable({'status': 'enable'}))


class ScaleFallbackTests(SimpleTestCase):
    def test_connection_failure_scales_existing_deployment(self):
        calls = []
        with _lease_gate_passing(), \
             patch('fleet.runtime_fleet._post_control',
                   return_value=(False, 'runtime-fleet unreachable: ConnectionError')), \
             patch('fleet.runtime_fleet._fleet_agent_for_fallback',
                   return_value=_agent_row('merge')), \
             patch('fleet.provisioner.emergency_scale',
                   side_effect=lambda agent, replicas: calls.append((agent.deployment_name, replicas)) or True):
            ok, detail = set_runtime_agent_enabled('merge-uuid', True)

        self.assertTrue(ok)
        self.assertEqual(calls, [('agent-merge', 1)])
        self.assertEqual(detail['plane'], 'k8s-scale-fallback')
        self.assertIn('control_error', detail)

    def test_disable_scales_to_zero(self):
        calls = []
        with _lease_gate_passing(), \
             patch('fleet.runtime_fleet._post_control',
                   return_value=(False, 'runtime-fleet unreachable: ConnectionError')), \
             patch('fleet.runtime_fleet._fleet_agent_for_fallback',
                   return_value=_agent_row('devops')), \
             patch('fleet.provisioner.emergency_scale',
                   side_effect=lambda agent, replicas: calls.append((agent.deployment_name, replicas)) or True):
            ok, _detail = set_runtime_agent_enabled('devops-uuid', False)

        self.assertTrue(ok)
        self.assertEqual(calls, [('agent-devops', 0)])

    def test_http_error_does_not_trigger_fallback(self):
        with _lease_gate_passing(), \
             patch('fleet.runtime_fleet._post_control',
                   return_value=(False, 'control enable returned 401: bad secret')), \
             patch('fleet.runtime_fleet._fleet_agent_for_fallback') as lookup:
            ok, detail = set_runtime_agent_enabled('merge-uuid', True)

        self.assertFalse(ok)
        lookup.assert_not_called()
        self.assertIn('401', detail)

    def test_success_does_not_trigger_fallback(self):
        with _lease_gate_passing(), \
             patch('fleet.runtime_fleet._post_control',
                   return_value=(True, {'status': 'enable'})), \
             patch('fleet.runtime_fleet._fleet_agent_for_fallback') as lookup:
            ok, detail = set_runtime_agent_enabled('merge-uuid', True)

        self.assertTrue(ok)
        lookup.assert_not_called()
        self.assertEqual(detail, {'status': 'enable'})

    def test_unknown_agent_fails_loud_without_provisioning(self):
        with _lease_gate_passing(), \
             patch('fleet.runtime_fleet._post_control',
                   return_value=(False, 'runtime-fleet unreachable: ConnectionError')), \
             patch('fleet.runtime_fleet._fleet_agent_for_fallback', return_value=None), \
             patch('fleet.provisioner.emergency_scale') as scale:
            ok, detail = set_runtime_agent_enabled('ghost-uuid', True)

        self.assertFalse(ok)
        scale.assert_not_called()
        self.assertIn('runtime-fleet unreachable', detail)
        self.assertIn('daemon-only', detail)

    def test_scale_failure_reports_both_errors(self):
        with _lease_gate_passing(), \
             patch('fleet.runtime_fleet._post_control',
                   return_value=(False, 'runtime-fleet unreachable: ConnectionError')), \
             patch('fleet.runtime_fleet._fleet_agent_for_fallback',
                   return_value=_agent_row('merge')), \
             patch('fleet.provisioner.emergency_scale', return_value=False):
            ok, detail = set_runtime_agent_enabled('merge-uuid', True)

        self.assertFalse(ok)
        self.assertIn('runtime-fleet unreachable', detail)
        self.assertIn('agent-merge', detail)
