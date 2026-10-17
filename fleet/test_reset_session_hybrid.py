"""Hybrid FleetAgent + rt-fleet seats must reach daemon reset-session.

A native FleetAgent row used to hide the runtime plane and 400 the documented
recovery (Saki 2026-08-14: 1115-message / 294k-token PVC session, Hive MCP
reset was a no-op).
"""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework import status

from fleet.models import FleetAgent
from fleet.views import FleetAgentResetSessionView, _get_agent


class FleetAgentResetSessionHybridTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.staff = SimpleNamespace(
            id=3, email='test-owner@example.com', username='hritik',
            is_staff=True, is_authenticated=True,
        )

    def _post(self, agent_id):
        req = self.factory.post(f'/v1/fleet/agents/{agent_id}/reset-session/', {}, format='json')
        force_authenticate(req, user=self.staff, token={'email': self.staff.email, 'is_staff': True})
        return req

    def test_hybrid_row_uses_runtime_control_not_native_400(self):
        req = self._post('4b7af2d5-0af5-5a58-b074-c3fca115b6db')
        runtime = {
            'id': '4b7af2d5-0af5-5a58-b074-c3fca115b6db',
            'agent_username': 'saki',
            'runtime_environment': 'k8s',
        }
        control = Response(runtime, status=status.HTTP_200_OK)
        with patch('fleet.views._require_staff', return_value=None), \
                patch('fleet.views.get_runtime_agent', return_value=runtime) as get_rt, \
                patch('fleet.views._rt_fleet_control', return_value=control) as control_fn, \
                patch('fleet.views._get_agent') as get_native:
            resp = FleetAgentResetSessionView.as_view()(
                req, agent_id='4b7af2d5-0af5-5a58-b074-c3fca115b6db',
            )
        self.assertEqual(resp.status_code, 200)
        get_rt.assert_called_once_with('4b7af2d5-0af5-5a58-b074-c3fca115b6db')
        self.assertEqual(control_fn.call_count, 1)
        self.assertEqual(control_fn.call_args.args[1], '4b7af2d5-0af5-5a58-b074-c3fca115b6db')
        self.assertEqual(control_fn.call_args.args[2], 'reset-session')
        get_native.assert_not_called()

    def test_daemon_404_falls_through_to_native_pvc_wipe(self):
        """k3s-native seats appear in daemon-link cache as rt-fleet.

        Hive MCP then POSTs the daemon, which 404s, and used to return 502
        without wiping the PVC (Nova 2026-09-09).
        """
        req = self._post('dd062ecf-2a12-5ee6-87f8-83963b9f6f6b')
        runtime = {
            'id': 'dd062ecf-2a12-5ee6-87f8-83963b9f6f6b',
            'agent_username': 'nova',
            'runtime_source': 'rt-fleet',
        }
        native = SimpleNamespace(
            id='cd07025c-ce9d-470a-ab30-0e48dcc87b76',
            agent_username='nova',
            agent_id='dd062ecf-2a12-5ee6-87f8-83963b9f6f6b',
        )
        reset = {
            'status': 'session_reset',
            'plane': 'k8s-native-pvc',
            'session_id': 'agent-session-dd062ecf-2a12-5ee6-87f8-83963b9f6f6b',
        }
        daemon_miss = Response(
            {'error': 'control reset-session returned 404: {"error":"Agent not found"}'},
            status=status.HTTP_502_BAD_GATEWAY,
        )
        with patch('fleet.views._require_staff', return_value=None), \
                patch('fleet.views.get_runtime_agent', return_value=runtime), \
                patch('fleet.views._rt_fleet_control', return_value=daemon_miss) as control_fn, \
                patch('fleet.views._get_agent', return_value=native), \
                patch('fleet.provisioner.reset_k8s_native_runtime_session', return_value=reset) as wipe:
            resp = FleetAgentResetSessionView.as_view()(
                req, agent_id='dd062ecf-2a12-5ee6-87f8-83963b9f6f6b',
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['plane'], 'k8s-native-pvc')
        control_fn.assert_called_once()
        wipe.assert_called_once_with(native)

    def test_native_only_wipes_pvc_session(self):
        req = self._post('native-only')
        native = SimpleNamespace(id='native-only', agent_username='hina', agent_id='runtime-1')
        reset = {
            'status': 'session_reset',
            'plane': 'k8s-native-pvc',
            'session_id': 'agent-session-runtime-1',
        }
        with patch('fleet.views._require_staff', return_value=None), \
                patch('fleet.views.get_runtime_agent', return_value=None), \
                patch('fleet.views._get_agent', return_value=native), \
                patch('fleet.views._rt_fleet_control') as control_fn, \
                patch('fleet.provisioner.reset_k8s_native_runtime_session', return_value=reset) as wipe:
            resp = FleetAgentResetSessionView.as_view()(req, agent_id='native-only')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['plane'], 'k8s-native-pvc')
        wipe.assert_called_once_with(native)
        control_fn.assert_not_called()


class NativeTelemetryTurnNormaliseTests(SimpleTestCase):
    def test_empty_high_prefill_turn_says_no_tools(self):
        from fleet.views import _normalise_native_telemetry_turn
        event = _normalise_native_telemetry_turn({
            'name': 'llm-turn',
            'endTime': '2026-08-14T20:44:42.427Z',
            'durationMs': 8300,
            'status': 'ok',
            'metadata': {'model': 'DeepSeek-V4-Flash'},
            'result': {
                'toolCalls': 0,
                'inputTokens': 294089,
                'outputTokens': 101,
                'toolNames': [],
                'assistantExcerpt': '',
            },
        })
        self.assertEqual(event['type'], 'turn_summary')
        self.assertIn('no tools', event['text'])
        self.assertEqual(event['input_tokens'], 294089)
        self.assertEqual(event['tool_calls'], 0)

    def test_working_turn_carries_excerpt_and_tools(self):
        from fleet.views import _normalise_native_telemetry_turn
        event = _normalise_native_telemetry_turn({
            'name': 'llm-turn',
            'endTime': '2026-08-14T21:25:40.065Z',
            'durationMs': 49451,
            'status': 'ok',
            'metadata': {'model': 'DeepSeek-V4-Flash'},
            'result': {
                'toolCalls': 1,
                'inputTokens': 157076,
                'outputTokens': 836,
                'toolNames': ['bash'],
                'assistantExcerpt': 'Checking Pulse for SCLI-376',
            },
        })
        self.assertIn('Checking Pulse for SCLI-376', event['text'])
        self.assertIn('tools: bash', event['text'])
        self.assertEqual(event['tool_names'], ['bash'])
        self.assertEqual(event['assistant_excerpt'], 'Checking Pulse for SCLI-376')


class GetAgentRuntimeIdTests(TestCase):
    def test_resolves_daemon_uuid_stored_on_agent_id(self):
        row = FleetAgent.objects.create(
            agent_username='saki',
            agent_id='4b7af2d5-0af5-5a58-b074-c3fca115b6db',
        )
        found = _get_agent('4b7af2d5-0af5-5a58-b074-c3fca115b6db')
        self.assertEqual(found.pk, row.pk)
        self.assertEqual(found.agent_username, 'saki')
