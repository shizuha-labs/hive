"""Agent chat admission consumes explicit Cortex output modality metadata."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from fleet.models import (
    AgentRuntimeDesiredState, FleetAgent, RuntimeChange, RuntimeLaneOutbox,
)
from fleet.views import FleetAgentDetailView, FleetAgentListView, FleetRuntimeOptionsView


@override_settings(CORTEX_BASE_URL='http://catalog.example.test/v1')
class ChatModelCapabilityAdmissionTests(TestCase):
    def setUp(self):
        self.agent = FleetAgent.objects.create(
            agent_username='chat-modality-test', agent_id='chat-modality-test',
            model='cortex/GLM-5.3-Flash',
            mcp_config={'execution_method': 'shizuha'},
        )
        self.desired = AgentRuntimeDesiredState.objects.create(
            agent=self.agent, desired_generation=8, desired_config_hash='old-hash',
        )
        self.staff = SimpleNamespace(
            id=3, email='operator@example.test', username='operator',
            is_staff=True, is_authenticated=True,
        )

    def request(self, model, row=None, failure=None):
        req = APIRequestFactory().patch(
            '/hive/api/v1/fleet/agents/chat-modality-test/',
            {'model': model, 'base_generation': 8}, format='json',
        )
        force_authenticate(req, user=self.staff)
        response = Mock()
        response.status_code = 200
        response.json.return_value = {'data': [row] if row else []}
        with patch('requests.get', return_value=response, side_effect=failure), \
                patch('fleet.views.apply_k3s_lane_after_patch', return_value={'applied': False}), \
                patch('fleet.views._require_staff', return_value=None):
            return FleetAgentDetailView.as_view()(req, agent_id=str(self.agent.id))

    def assert_unchanged(self):
        self.agent.refresh_from_db()
        self.desired.refresh_from_db()
        self.assertEqual(self.agent.model, 'cortex/GLM-5.3-Flash')
        self.assertEqual(self.desired.desired_generation, 8)
        self.assertEqual(self.desired.desired_config_hash, 'old-hash')
        self.assertEqual(self.agent.pending_config, {})
        self.assertFalse(RuntimeChange.objects.exists())
        self.assertFalse(RuntimeLaneOutbox.objects.exists())

    def test_actual_patch_refuses_explicit_nontext_before_all_writes(self):
        result = self.request('cortex/fixture-video', {
            'id': 'cortex/fixture-video',
            'x_cortex': {'output_modalities': ['video', 'audio']},
        })
        self.assertEqual(result.status_code, 400, result.data)
        self.assertEqual(result.data['code'], 'MODEL_NOT_CHAT_CAPABLE')
        self.assert_unchanged()

    def test_alias_match_also_refuses(self):
        result = self.request('cortex/fixture-video-alias', {
            'id': 'cortex/fixture-video', 'aliases': ['fixture-video-alias'],
            'x_cortex': {'output_modalities': ['video']},
        })
        self.assertEqual(result.status_code, 400, result.data)
        self.assert_unchanged()

    def test_explicit_text_model_keeps_normal_admission(self):
        result = self.request('cortex/fixture-chat', {
            'id': 'cortex/fixture-chat',
            'capabilities': {'tools': False},
            'x_cortex': {'output_modalities': ['text', 'audio']},
        })
        self.assertEqual(result.status_code, 202, result.data)
        self.agent.refresh_from_db()
        self.desired.refresh_from_db()
        self.assertEqual(self.agent.model, 'cortex/fixture-chat')
        self.assertEqual(self.desired.desired_generation, 9)
        self.assertEqual(RuntimeChange.objects.count(), 1)
        self.assertEqual(RuntimeLaneOutbox.objects.count(), 1)

    def test_unknown_catalog_model_preserves_legacy_admission(self):
        self.assertEqual(self.request('cortex/fixture-unknown').status_code, 202)

    def test_malformed_or_missing_metadata_does_not_become_rejection(self):
        for metadata in [None, {}, {'output_modalities': []},
                         {'output_modalities': 'video'},
                         {'output_modalities': ['video', None]},
                         {'output_modalities': ['video', '']}]:
            with self.subTest(metadata=metadata):
                # Exercise actual preflight without changing this test's generation.
                from fleet.runtime_lane import preflight_runtime_lane, resolve_runtime_lane
                lane = resolve_runtime_lane({}, {'model': 'cortex/fixture-legacy', 'execution_method': 'shizuha'})
                response = Mock(status_code=200)
                response.json.return_value = {'data': [{'id': 'cortex/fixture-legacy', 'x_cortex': metadata}]}
                with patch('requests.get', return_value=response):
                    self.assertEqual(preflight_runtime_lane(self.agent, lane)['state'], 'not_required')

    def test_transport_failure_keeps_existing_unknown_behavior(self):
        import requests
        self.assertEqual(self.request('cortex/fixture-unknown', failure=requests.RequestException('fixture')).status_code, 202)

    def test_options_omit_only_explicit_nontext(self):
        response = Mock(status_code=200)
        response.json.return_value = {'data': [
            {'id': 'fixture-video', 'x_cortex': {'output_modalities': ['video']}},
            {'id': 'fixture-chat', 'x_cortex': {'output_modalities': ['text']}},
            {'id': 'fixture-legacy'},
            {'id': 'fixture-malformed', 'x_cortex': {'output_modalities': 'video'}},
        ]}
        with patch('fleet.views.requests.get', return_value=response):
            rows, error = FleetRuntimeOptionsView()._cortex_models()
        ids = {row['id'] for row in rows}
        self.assertNotIn('cortex/fixture-video', ids)
        self.assertTrue({'cortex/fixture-chat', 'cortex/fixture-legacy', 'cortex/fixture-malformed'} <= ids)
        self.assertEqual(error, '')

    def create_request(self, row=None, failure=None):
        req = APIRequestFactory().post('/hive/api/v1/fleet/agents/', {
            'agent_username': 'new-chat-capability-test',
            'model': 'cortex/fixture-new',
            'mcp_config': {'execution_method': 'shizuha'},
        }, format='json')
        force_authenticate(req, user=self.staff)
        response = Mock(status_code=200)
        response.json.return_value = {'data': [row] if row else []}
        with patch('requests.get', return_value=response, side_effect=failure), \
                patch('fleet.views.provisioner.provision_agent', return_value=True) as provision, \
                patch('fleet.views._require_staff', return_value=None):
            result = FleetAgentListView.as_view()(req)
        return result, provision

    def test_actual_create_refuses_nontext_before_persist_or_provision(self):
        before = (FleetAgent.objects.count(), AgentRuntimeDesiredState.objects.count(),
                  RuntimeChange.objects.count(), RuntimeLaneOutbox.objects.count())
        result, provision = self.create_request({
            'id': 'cortex/fixture-new', 'x_cortex': {'output_modalities': ['video']},
        })
        after = (FleetAgent.objects.count(), AgentRuntimeDesiredState.objects.count(),
                 RuntimeChange.objects.count(), RuntimeLaneOutbox.objects.count())
        self.assertEqual(result.status_code, 400, {
            'status': result.status_code, 'before': before, 'after': after,
            'provision_called': provision.called,
        })
        self.assertEqual(after, before)
        provision.assert_not_called()

    def test_create_chat_unknown_malformed_and_transport_compatibility(self):
        import requests
        for row, failure in [
            ({'id': 'cortex/fixture-new', 'x_cortex': {'output_modalities': ['text']}}, None),
            (None, None),
            ({'id': 'cortex/fixture-new', 'x_cortex': {'output_modalities': ['video', None]}}, None),
            (None, requests.RequestException('fixture')),
        ]:
            with self.subTest(row=row, failure=type(failure).__name__):
                result, provision = self.create_request(row, failure)
                self.assertEqual(result.status_code, 201, result.data)
                provision.assert_called_once()
                FleetAgent.objects.get(agent_username='new-chat-capability-test').delete()


class ExplicitOutputMetadataTests(SimpleTestCase):
    def test_only_typed_explicit_output_exclusion_is_authoritative(self):
        from fleet.model_serviceability import explicitly_nontext_model
        for item in [
            {}, {'x_cortex': None}, {'x_cortex': []},
            {'x_cortex': {'input_modalities': ['video']}},
            {'x_cortex': {'output_modalities': []}},
            {'x_cortex': {'output_modalities': 'video'}},
            {'x_cortex': {'output_modalities': ['video', False]}},
            {'x_cortex': {'output_modalities': ['video', '']}},
            {'x_cortex': {'output_modalities': ['video', ' Text ']}},
            {'capabilities': {'tools': False}},
        ]:
            with self.subTest(item=item):
                self.assertFalse(explicitly_nontext_model(item))
        for outputs in [['video'], ['audio'], [' VIDEO ', 'Audio']]:
            with self.subTest(outputs=outputs):
                self.assertTrue(explicitly_nontext_model({'x_cortex': {'output_modalities': outputs}}))
