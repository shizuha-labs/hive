"""HIVE-609: the fleet agents-index must not let a dead legacy rt-fleet (host-
docker) entry shadow a migrated agent's LIVE k3s-native row.

The temporary, explicit HIVE609 containment flag uses activity freshness so a
migrated agent (k8s pod active now, rt-fleet transcript days old) resolves to its
live k3s identity. Native activity must read the selected live pod and must never
route a FleetAgent UUID to the legacy daemon proxy."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from fleet.views import (
    _activity_epoch,
    _get_native_agent_activity,
    _merge_native_and_runtime,
    _native_row_is_live,
    _normalise_native_audit_event,
    _pick_live_pod,
)
from fleet.runtime_fleet import enrich_k8s_placement


def _iso(dt):
    return dt.isoformat().replace('+00:00', 'Z')


NOW = datetime(2026, 7, 10, 21, 0, tzinfo=timezone.utc)
FRESH = _iso(NOW - timedelta(minutes=2))
STALE = _iso(NOW - timedelta(days=3))


class ActivityEpochTests(SimpleTestCase):
    def test_parses_iso_and_datetime_and_handles_missing(self):
        self.assertIsNone(_activity_epoch(None))
        self.assertIsNone(_activity_epoch(''))
        self.assertIsNone(_activity_epoch('not-a-date'))
        self.assertEqual(_activity_epoch(_iso(NOW)), NOW.timestamp())
        self.assertEqual(_activity_epoch(NOW), NOW.timestamp())


@override_settings(HIVE609_TEMP_FRESHNESS_PRECEDENCE=True)
class NativeRowIsLiveTests(SimpleTestCase):
    def test_migrated_agent_fresh_native_beats_stale_rtfleet(self):
        # aoi: live k8s pod (minutes-fresh) vs 3-day-old rt-fleet docker entry.
        native = {'agent_username': 'aoi', 'status': 'running', 'last_active_at': FRESH}
        runtime = {'agent_username': 'aoi', 'status': 'running', 'last_active_at': STALE,
                   'runtime_source': 'rt-fleet'}
        self.assertTrue(_native_row_is_live(native, runtime))

    def test_retired_native_yields_to_live_host_agent(self):
        # Original concern preserved: a stale/decommissioned k3s row must NOT
        # shadow a genuinely live host-runtime agent.
        native = {'agent_username': 'glm-old', 'status': 'stopped', 'last_active_at': STALE}
        runtime = {'agent_username': 'glm-old', 'status': 'running', 'last_active_at': FRESH}
        self.assertFalse(_native_row_is_live(native, runtime))

    def test_only_native_has_activity_signal_wins(self):
        native = {'agent_username': 'x', 'status': 'running', 'last_active_at': FRESH}
        runtime = {'agent_username': 'x', 'status': 'running', 'last_active_at': None}
        self.assertTrue(_native_row_is_live(native, runtime))

    def test_only_runtime_has_activity_signal_wins(self):
        native = {'agent_username': 'x', 'status': 'running', 'last_active_at': None}
        runtime = {'agent_username': 'x', 'status': 'running', 'last_active_at': FRESH}
        self.assertFalse(_native_row_is_live(native, runtime))

    def test_tie_prefers_running_native(self):
        native = {'agent_username': 'x', 'status': 'running', 'last_active_at': None}
        runtime = {'agent_username': 'x', 'status': 'running', 'last_active_at': None}
        self.assertTrue(_native_row_is_live(native, runtime))

    def test_both_missing_and_native_not_running_keeps_runtime(self):
        native = {'agent_username': 'x', 'status': 'pending', 'last_active_at': None}
        runtime = {'agent_username': 'x', 'status': 'running', 'last_active_at': None}
        self.assertFalse(_native_row_is_live(native, runtime))


@override_settings(HIVE609_TEMP_FRESHNESS_PRECEDENCE=True)
class MergeNativeAndRuntimeTests(SimpleTestCase):
    def test_migrated_agent_resolves_to_native_identity(self):
        native = [{'agent_username': 'aoi', 'id': 'uuid-aoi', 'status': 'running',
                   'last_active_at': FRESH, 'runtime_source': 'k3s-native'}]
        runtime_by_username = {'aoi': {'agent_username': 'aoi', 'id': 'rtfleet-1',
                                       'status': 'running', 'last_active_at': STALE,
                                       'runtime_source': 'rt-fleet'}}
        merged = _merge_native_and_runtime(native, runtime_by_username)
        self.assertEqual(len(merged), 1)
        # The live k3s row wins → detail/activity now resolve to the real pod, not
        # the dead daemon id.
        self.assertEqual(merged[0]['id'], 'uuid-aoi')
        self.assertEqual(merged[0]['runtime_source'], 'k3s-native')

    def test_runtime_only_agent_still_listed(self):
        native = []
        runtime_by_username = {'host1': {'agent_username': 'host1', 'id': 'rt-1',
                                         'status': 'running', 'last_active_at': FRESH}}
        merged = _merge_native_and_runtime(native, runtime_by_username)
        self.assertEqual([a['id'] for a in merged], ['rt-1'])

    def test_native_only_agent_kept(self):
        native = [{'agent_username': 'solo', 'id': 'uuid-solo', 'status': 'running',
                   'last_active_at': FRESH}]
        merged = _merge_native_and_runtime(native, {})
        self.assertEqual([a['id'] for a in merged], ['uuid-solo'])

    def test_stale_native_yields_but_agent_not_dropped(self):
        native = [{'agent_username': 'glm-old', 'id': 'uuid-old', 'status': 'stopped',
                   'last_active_at': STALE}]
        runtime_by_username = {'glm-old': {'agent_username': 'glm-old', 'id': 'rt-old',
                                           'status': 'running', 'last_active_at': FRESH}}
        merged = _merge_native_and_runtime(native, runtime_by_username)
        self.assertEqual([a['id'] for a in merged], ['rt-old'])  # live host wins, still present

    @override_settings(HIVE609_TEMP_FRESHNESS_PRECEDENCE=False)
    def test_retired_host_spawn_error_never_shadows_native_k3s_row(self):
        native = [{
            'agent_username': 'ichi',
            'id': 'uuid-ichi',
            'status': 'running',
            'enabled': True,
            'last_active_at': FRESH,
            'runtime_source': 'k3s-native',
            'pod_name': 'agent-ichi-66f58f679d-bshz4',
        }]
        runtime = {'ichi': {
            'agent_username': 'ichi',
            'id': 'rt-ichi',
            'status': 'failed',
            'enabled': False,
            'last_active_at': FRESH,
            'runtime_source': 'rt-fleet',
            'status_message': (
                'refusing host-local spawn for Ichi (runtime=k8s): '
                'local daemon retired; fleet agents are k3s-only. '
                'See /home/phoenix/.shizuha/LEGACY_LOCAL_DAEMON_RETIRED'
            ),
            'pod_name': 'shizuha-runtime-fleet',
        }}
        row = _merge_native_and_runtime(native, runtime)[0]
        self.assertEqual(row['id'], 'uuid-ichi')
        self.assertEqual(row['status'], 'running')
        self.assertTrue(row['enabled'])
        self.assertEqual(row['runtime_source'], 'k3s-native')
        self.assertEqual(row['pod_name'], 'agent-ichi-66f58f679d-bshz4')
        self.assertFalse(row.get('status_message'))

    @override_settings(HIVE609_TEMP_FRESHNESS_PRECEDENCE=False)
    def test_poisoned_native_failed_paint_is_cleared_when_runtime_is_retired_spawn(self):
        native = [{
            'agent_username': 'ni',
            'id': 'uuid-ni',
            'status': 'failed',
            'enabled': False,
            'lifecycle_state': 'enabled',
            'status_message': 'refusing host-local spawn for Ni (runtime=k8s): local daemon retired',
            'runtime_source': 'k3s-native',
        }]
        runtime = {'ni': {
            'agent_username': 'ni',
            'id': 'rt-ni',
            'status': 'failed',
            'status_message': 'refusing host-local spawn for Ni (runtime=k8s): local daemon retired',
            'runtime_source': 'rt-fleet',
        }}
        row = _merge_native_and_runtime(native, runtime)[0]
        self.assertEqual(row['id'], 'uuid-ni')
        self.assertEqual(row['status'], 'running')
        self.assertTrue(row['enabled'])
        self.assertEqual(row.get('status_message'), '')

    @override_settings(HIVE609_TEMP_FRESHNESS_PRECEDENCE=False)
    def test_running_native_beats_stale_operator_stopped_runtime(self):
        """2026-09-01: GLM native pods were Running while rt-fleet still
        reported operator_stopped, so the Agents page painted Stopped."""
        native = [{
            'agent_username': 'akira',
            'id': 'uuid-akira',
            'status': 'running',
            'enabled': True,
            'last_active_at': FRESH,
            'runtime_source': 'k3s-native',
            'model': 'cortex/GLM-5.3-Flash',
        }]
        runtime = {'akira': {
            'agent_username': 'akira',
            'id': 'rt-akira',
            'status': 'stopped',
            'enabled': False,
            'lifecycle_state': 'operator_stopped',
            'last_active_at': STALE,
            'runtime_source': 'rt-fleet',
            'model': 'cortex/GLM-5.3-Flash',
        }}
        row = _merge_native_and_runtime(native, runtime)[0]
        self.assertEqual(row['id'], 'uuid-akira')
        self.assertEqual(row['status'], 'running')
        self.assertTrue(row['enabled'])
        self.assertEqual(row['runtime_source'], 'k3s-native')

    @override_settings(HIVE609_TEMP_FRESHNESS_PRECEDENCE=False)
    def test_temporary_selector_is_fail_closed_without_explicit_flag(self):
        native = [{'agent_username': 'aoi', 'id': 'uuid-aoi', 'status': 'running',
                   'last_active_at': FRESH, 'model': 'cortex/DeepSeek-V4-Flash'}]
        runtime = {'aoi': {'agent_username': 'aoi', 'id': 'rt-aoi', 'status': 'running',
                           'last_active_at': STALE,
                           'model': 'cortex/GLM-5.2-NVFP4-AQLM-380K'}}
        row = _merge_native_and_runtime(native, runtime)[0]
        self.assertEqual(row['id'], 'rt-aoi')
        self.assertEqual(row['model'], 'cortex/DeepSeek-V4-Flash')
        self.assertEqual(
            row['observed_runtime_model'],
            'cortex/GLM-5.2-NVFP4-AQLM-380K',
        )

    @override_settings(HIVE609_TEMP_FRESHNESS_PRECEDENCE=False)
    def test_stopped_runtime_keeps_lifecycle_but_never_reverts_hive_model(self):
        native = [{
            'agent_username': 'nova',
            'id': 'uuid-nova',
            'status': 'stopped',
            'model': 'cortex/DeepSeek-V4-Flash',
            'effective_model': 'cortex/DeepSeek-V4-Flash',
            'context_prompt': 'DeepSeek lane',
        }]
        runtime = {'nova': {
            'agent_username': 'nova',
            'id': 'rt-nova',
            'status': 'stopped',
            'enabled': False,
            'model': 'cortex/GLM-5.2-NVFP4-AQLM-380K',
            'effective_model': 'cortex/GLM-5.2-NVFP4-AQLM-380K',
            'context_prompt': 'GLM lane',
        }}

        row = _merge_native_and_runtime(native, runtime)[0]

        self.assertEqual(row['id'], 'rt-nova')
        self.assertEqual(row['status'], 'stopped')
        self.assertFalse(row['enabled'])
        self.assertEqual(row['model'], 'cortex/DeepSeek-V4-Flash')
        self.assertEqual(row['effective_model'], 'cortex/DeepSeek-V4-Flash')
        self.assertEqual(row['context_prompt'], 'DeepSeek lane')
        self.assertEqual(row['desired_config_source'], 'hive')

    def test_runtime_merge_preserves_avatar_url_and_display_title(self):
        """HIVE-1242 regression: runtime dicts never carry avatar_url/display_title,
        so the merge must copy them from the native row. Before this fix every
        runtime-merged agent lost its portrait and the Agents UI showed initials
        for everyone, even agents with a published shizuha.com avatar."""
        native = [{
            'agent_username': 'kai',
            'id': 'uuid-kai',
            'status': 'stopped',
            'last_active_at': STALE,
            'display_name': 'Kai',
            'display_title': 'Engineer',
            'avatar_url': 'https://shizuha.com/avatars/kai.png',
            'model': 'cortex/DeepSeek-V4-Flash',
        }]
        runtime = {'kai': {
            'agent_username': 'kai',
            'id': 'rt-kai',
            'status': 'running',
            'last_active_at': FRESH,
            'display_name': 'Kai',
            'model': 'cortex/GLM-5.2-NVFP4-AQLM-380K',
            # NOTE: no avatar_url, no display_title — mirrors the real runtime dict.
        }}

        row = _merge_native_and_runtime(native, runtime)[0]

        self.assertEqual(row['id'], 'rt-kai')
        self.assertEqual(row['avatar_url'], 'https://shizuha.com/avatars/kai.png')
        self.assertEqual(row['display_title'], 'Engineer')
        self.assertEqual(row['desired_config_source'], 'hive')


def _make_pod(name, phase='Running', ready=True, created_ago=0, terminating=False):
    """Build a minimal fake V1Pod for _pick_live_pod tests."""
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.creation_timestamp = NOW - timedelta(seconds=created_ago)
    pod.metadata.deletion_timestamp = NOW if terminating else None
    pod.status.phase = phase
    if ready:
        ready_cond = MagicMock()
        ready_cond.type = 'Ready'
        ready_cond.status = 'True'
        not_ready_cond = MagicMock()
        not_ready_cond.type = 'ContainersReady'
        not_ready_cond.status = 'True'
        pod.status.conditions = [ready_cond, not_ready_cond]
    else:
        ready_cond = MagicMock()
        ready_cond.type = 'Ready'
        ready_cond.status = 'False'
        pod.status.conditions = [ready_cond]
    return pod


class PickLivePodTests(SimpleTestCase):
    """Deterministic pod selection during rollouts (review P1)."""

    def test_empty_list_returns_none(self):
        self.assertIsNone(_pick_live_pod([]))

    def test_single_running_ready_pod(self):
        pod = _make_pod('agent-aoi-abc', created_ago=60)
        self.assertEqual(_pick_live_pod([pod]).metadata.name, 'agent-aoi-abc')

    def test_newer_ready_pod_wins_during_rollout(self):
        old = _make_pod('agent-aoi-old', created_ago=300)
        new = _make_pod('agent-aoi-new', created_ago=10)
        self.assertEqual(_pick_live_pod([old, new]).metadata.name, 'agent-aoi-new')

    def test_ready_pod_wins_over_not_ready(self):
        ready = _make_pod('agent-aoi-ready', created_ago=60)
        not_ready = _make_pod('agent-aoi-notready', ready=False, created_ago=30)
        self.assertEqual(_pick_live_pod([not_ready, ready]).metadata.name, 'agent-aoi-ready')

    def test_terminating_ready_pod_never_beats_live_replacement(self):
        retiring = _make_pod('agent-aoi-old', created_ago=5, terminating=True)
        replacement = _make_pod('agent-aoi-new', created_ago=60)
        self.assertEqual(_pick_live_pod([retiring, replacement]).metadata.name,
                         'agent-aoi-new')

    def test_running_not_ready_falls_back_to_newest_running(self):
        not_ready = _make_pod('agent-aoi-nr', ready=False, created_ago=30)
        running = _make_pod('agent-aoi-run', ready=False, created_ago=60)
        self.assertEqual(_pick_live_pod([running, not_ready]).metadata.name, 'agent-aoi-nr')

    def test_no_running_pods_returns_newest_any_phase(self):
        pending = _make_pod('agent-aoi-pend', phase='Pending', created_ago=30)
        old = _make_pod('agent-aoi-old', phase='Pending', created_ago=300)
        self.assertEqual(_pick_live_pod([old, pending]).metadata.name, 'agent-aoi-pend')

    def test_pods_without_timestamp_sort_last(self):
        no_ts = _make_pod('agent-aoi-nots', created_ago=0)
        no_ts.metadata.creation_timestamp = None
        with_ts = _make_pod('agent-aoi-ts', created_ago=60)
        self.assertEqual(_pick_live_pod([no_ts, with_ts]).metadata.name, 'agent-aoi-ts')


class NativeAgentActivityTests(SimpleTestCase):
    """_get_native_agent_activity: pod selection, exec, parse, fail-soft."""

    @patch('kubernetes.stream.stream')
    def test_native_audit_exec_targets_agent_container(self, stream):
        from fleet.views import _exec_native_audit_tail

        stream.return_value.read_all.return_value = ''
        _exec_native_audit_tail(MagicMock(), 'agent-rui-abc', 'shizuha-fleet', 10)

        self.assertEqual(stream.call_args.kwargs['container'], 'agent')
        self.assertEqual(stream.call_args.kwargs['_request_timeout'], (3, 8))
        self.assertIs(stream.call_args.kwargs['_preload_content'], False)

    @patch('kubernetes.stream.stream')
    def test_native_audit_exec_reads_unpreloaded_stdout(self, stream):
        from fleet.views import _exec_native_audit_tail

        stream.return_value.read_all.return_value = (
            '__TELEMETRY__\n'
            '{"name":"llm-turn","endTime":"2026-09-24T05:38:31.029Z",'
            '"metadata":{"model":"Qwen3.8-27B"},"result":{"toolCalls":1,'
            '"inputTokens":1,"outputTokens":1}}\n'
        )
        raw = _exec_native_audit_tail(MagicMock(), 'agent-reika-abc', 'shizuha-fleet', 10)
        self.assertIn('Qwen3.8-27B', raw)
        stream.return_value.run_forever.assert_called_once()

    @patch('kubernetes.stream.stream')
    def test_native_audit_exec_decode_miss_returns_empty(self, stream):
        from fleet.views import _exec_native_audit_tail

        stream.side_effect = AttributeError("'NoneType' object has no attribute 'decode'")
        self.assertIsNone(
            _exec_native_audit_tail(MagicMock(), 'agent-reo-abc', 'shizuha-fleet', 10),
        )
        self.assertEqual(stream.call_count, 2)

    @patch('kubernetes.stream.stream')
    def test_native_audit_exec_retries_then_reads(self, stream):
        from fleet.views import _exec_native_audit_tail

        failed = MagicMock()
        failed.run_forever.side_effect = TimeoutError('activity tail did not finish')
        ok = MagicMock()
        ok.returncode = 0
        ok.read_all.return_value = '__TELEMETRY__\n{"name":"llm-turn"}\n'
        stream.side_effect = [failed, ok]
        raw = _exec_native_audit_tail(MagicMock(), 'agent-reika-abc', 'shizuha-fleet', 10)
        self.assertIn('llm-turn', raw)
        self.assertEqual(stream.call_count, 2)
        failed.close.assert_called_once()

    def test_exec_read_failure_is_unavailable_not_a_fresh_session(self):
        agent = MagicMock()
        agent.agent_username = 'reika'
        pod = _make_pod('agent-reika-abc')
        with (
            patch('fleet.views._pick_live_pod', return_value=pod),
            patch('fleet.views.provisioner.get_k8s_client') as mock_client,
            patch('fleet.views._exec_native_audit_tail', return_value=None),
        ):
            mock_client.return_value = (MagicMock(), None, None)
            result = _get_native_agent_activity(agent)
        self.assertEqual(result['source_state'], 'unavailable')
        self.assertEqual(result['events'], [])
        self.assertEqual(result['pod_name'], 'agent-reika-abc')

    def test_list_pods_decode_miss_returns_unavailable(self):
        """list_namespaced_pod NoneType.decode must not look like a generic fail."""
        agent = MagicMock()
        agent.agent_username = 'aoi'
        core_v1 = MagicMock()
        core_v1.list_namespaced_pod.side_effect = AttributeError(
            "'NoneType' object has no attribute 'decode'",
        )
        with patch('fleet.views.provisioner.get_k8s_client', return_value=(core_v1, None, None)):
            result = _get_native_agent_activity(agent)
        self.assertEqual(result['source_state'], 'unavailable')
        self.assertEqual(result['events'], [])
        self.assertEqual(result['total'], 0)

    def test_no_pods_returns_empty(self):
        agent = MagicMock()
        agent.agent_username = 'aoi'
        core_v1 = MagicMock()
        core_v1.list_namespaced_pod.return_value.items = []
        with patch('fleet.views.provisioner.get_k8s_client', return_value=(core_v1, None, None)):
            result = _get_native_agent_activity(agent)
        # HIVE-1552: no live pod is an explicit unavailable receipt, not {}.
        self.assertEqual(result['source_state'], 'unavailable')
        self.assertEqual(result['events'], [])
        self.assertEqual(result['total'], 0)

    def test_exec_timeout_returns_unavailable(self):
        agent = MagicMock()
        agent.agent_username = 'san'
        pod = _make_pod('agent-san-abc')
        with (
            patch('fleet.views._pick_live_pod', return_value=pod),
            patch('fleet.views.provisioner.get_k8s_client') as mock_client,
            patch('fleet.views._exec_native_audit_tail', side_effect=Exception('timed out')),
        ):
            mock_client.return_value = (MagicMock(), None, None)
            result = _get_native_agent_activity(agent)
        self.assertEqual(result['source_state'], 'unavailable')
        self.assertEqual(result['events'], [])

    def test_exec_failure_returns_empty(self):
        agent = MagicMock()
        agent.agent_username = 'aoi'
        pod = _make_pod('agent-aoi-abc')
        with (
            patch('fleet.views._pick_live_pod', return_value=pod),
            patch('fleet.views.provisioner.get_k8s_client', side_effect=Exception('k8s down')),
        ):
            result = _get_native_agent_activity(agent)
        # HIVE-1552: lookup/exec failure is an explicit unavailable receipt.
        self.assertEqual(result['source_state'], 'unavailable')
        self.assertEqual(result['events'], [])
        self.assertEqual(result['total'], 0)

    def test_parses_audit_lines_into_events(self):
        agent = MagicMock()
        agent.agent_username = 'aoi'
        pod = _make_pod('agent-aoi-abc')
        fake_log = (
            '{"id":"call-1","timestamp":"2026-07-22T01:02:03Z","agent":"aoi",'
            '"tool":"mcp__pulse__get","inputSummary":"{\\"key\\":\\"HIVE-609\\"}",'
            '"phase":"before"}\n'
            '{"id":"call-1","timestamp":"2026-07-22T01:02:04Z","agent":"aoi",'
            '"tool":"mcp__pulse__get","resultSummary":"ok","phase":"after"}\n'
            'not-json-line\n'
            '{"phase":"unknown","timestamp":"2026-07-22T01:02:05Z"}\n'
        )
        with (
            patch('fleet.views._pick_live_pod', return_value=pod),
            patch('fleet.views.provisioner.get_k8s_client') as mock_client,
            patch('fleet.views._exec_native_audit_tail') as mock_stream,
        ):
            core_v1 = MagicMock()
            mock_client.return_value = (core_v1, None, None)
            mock_stream.return_value = fake_log
            result = _get_native_agent_activity(agent)
        self.assertEqual(result['total'], 2)
        self.assertEqual(result['source'], 'k8s-native-audit+telemetry')
        self.assertEqual(result['pod_name'], 'agent-aoi-abc')
        self.assertEqual([e['type'] for e in result['events']],
                         ['tool_result', 'tool_call'])
        self.assertEqual(result['events'][1]['ts'], '2026-07-22T01:02:03Z')
        self.assertEqual(result['events'][1]['input'], '{"key":"HIVE-609"}')
        self.assertEqual(result['events'][0]['output'], 'ok')

    def test_parses_telemetry_after_marker_into_turn_summaries(self):
        agent = MagicMock()
        agent.agent_username = 'saki'
        pod = _make_pod('agent-saki-abc')
        fake_log = (
            '{"id":"call-1","timestamp":"2026-08-14T21:28:17Z","agent":"saki",'
            '"tool":"bash","phase":"before","inputSummary":"ls"}\n'
            '__TELEMETRY__\n'
            '{"name":"llm-turn","endTime":"2026-08-14T21:28:40Z","durationMs":8000,'
            '"status":"ok","metadata":{"model":"DeepSeek-V4-Flash"},'
            '"result":{"toolCalls":0,"inputTokens":294089,"outputTokens":101,'
            '"toolNames":[],"assistantExcerpt":""}}\n'
        )
        with (
            patch('fleet.views._pick_live_pod', return_value=pod),
            patch('fleet.views.provisioner.get_k8s_client') as mock_client,
            patch('fleet.views._exec_native_audit_tail', return_value=fake_log),
        ):
            mock_client.return_value = (MagicMock(), None, None)
            result = _get_native_agent_activity(agent)
        self.assertEqual(result['total'], 2)
        types = [e['type'] for e in result['events']]
        self.assertIn('turn_summary', types)
        self.assertIn('tool_call', types)
        summary = next(e for e in result['events'] if e['type'] == 'turn_summary')
        self.assertIn('no tools', summary['text'])
        self.assertEqual(summary['input_tokens'], 294089)

    def test_skips_non_dict_lines(self):
        agent = MagicMock()
        agent.agent_username = 'aoi'
        pod = _make_pod('agent-aoi-abc')
        with (
            patch('fleet.views._pick_live_pod', return_value=pod),
            patch('fleet.views.provisioner.get_k8s_client') as mock_client,
            patch('fleet.views._exec_native_audit_tail', return_value='["array"]\n"string"\n42\n'),
        ):
            core_v1 = MagicMock()
            mock_client.return_value = (core_v1, None, None)
            result = _get_native_agent_activity(agent)
        self.assertEqual(result['total'], 0)
        # A readable empty tail (reset/new session) is live-with-zero,
        # not "source degraded" (Kumo 2026-09-15).
        self.assertEqual(result['source_state'], 'live')
        self.assertEqual(result['events'], [])

    def test_live_native_receipt_carries_source_state_and_uid(self):
        """HIVE-1552: a healthy native read is a live receipt with resource_uid."""
        agent = MagicMock()
        agent.agent_username = 'aoi'
        pod = _make_pod('agent-aoi-abc')
        pod.metadata.uid = 'pod-uid-123'
        fake_log = (
            '{"id":"call-1","timestamp":"2026-07-22T01:02:03Z","agent":"aoi",'
            '"tool":"mcp__pulse__get","inputSummary":"x","phase":"before"}\n'
        )
        with (
            patch('fleet.views._pick_live_pod', return_value=pod),
            patch('fleet.views.provisioner.get_k8s_client') as mock_client,
            patch('fleet.views._exec_native_audit_tail', return_value=fake_log),
        ):
            core_v1 = MagicMock()
            mock_client.return_value = (core_v1, None, None)
            result = _get_native_agent_activity(agent)
        self.assertEqual(result['source_state'], 'live')
        self.assertEqual(result['source'], 'k8s-native-audit+telemetry')
        self.assertEqual(result['resource_uid'], 'pod-uid-123')
        self.assertIsNotNone(result['observed_at'])
        self.assertEqual(result['newest_event_at'], '2026-07-22T01:02:03Z')
        self.assertEqual(result['history_events'], [])
        self.assertEqual(result['total'], 1)


class NativeAuditNormalisationTests(SimpleTestCase):
    def test_real_bridge_before_after_schema_maps_to_drawer_contract(self):
        before = _normalise_native_audit_event({
            'id': 'call-1', 'timestamp': FRESH, 'agent': 'aoi',
            'tool': 'mcp__pulse__get', 'inputSummary': 'HIVE-609', 'phase': 'before',
        })
        after = _normalise_native_audit_event({
            'id': 'call-1', 'timestamp': FRESH, 'agent': 'aoi',
            'tool': 'mcp__pulse__get', 'resultSummary': 'ok', 'phase': 'after',
        })
        self.assertEqual(before, {
            'id': 'call-1', 'ts': FRESH, 'type': 'tool_call',
            'tool': 'mcp__pulse__get', 'input': 'HIVE-609', 'agent': 'aoi',
        })
        self.assertEqual(after['type'], 'tool_result')
        self.assertEqual(after['output'], 'ok')


class NativePlacementProjectionTests(SimpleTestCase):
    def setUp(self):
        from fleet import runtime_fleet
        runtime_fleet._PLACEMENT_CACHE.clear()

    def test_projects_same_selected_live_pod_node_arch_and_workspace_claim(self):
        retiring = _make_pod('agent-aoi-old', created_ago=5, terminating=True)
        replacement = _make_pod('agent-aoi-new', created_ago=60)
        replacement.spec.node_name = 'gx10-11'
        replacement.spec.volumes = []
        agent_container = MagicMock(name='agent-container')
        agent_container.name = 'agent'
        agent_container.image = 'registry.local/runtime:sha-new'
        replacement.spec.containers = [agent_container]

        deployment = MagicMock()
        deployment.spec.template.spec.containers = [agent_container]
        volume = MagicMock()
        volume.persistent_volume_claim.claim_name = 'agent-aoi-workspace'
        deployment.spec.template.spec.volumes = [volume]

        core = MagicMock()
        core.list_namespaced_pod.return_value.items = [retiring, replacement]
        node = MagicMock()
        node.metadata.labels = {'kubernetes.io/arch': 'arm64'}
        core.read_node.return_value = node
        apps = MagicMock()
        apps.read_namespaced_deployment.return_value = deployment

        with patch('fleet.provisioner.get_k8s_client', return_value=(core, apps, None)):
            projected = enrich_k8s_placement({
                'agent_username': 'aoi',
                'runtime_environment': 'container',
                'runtime_source': 'k3s-native',
                'namespace': 'shizuha-fleet',
            }, force=True)

        self.assertEqual(projected['pod_name'], 'agent-aoi-new')
        self.assertEqual(projected['node_name'], 'gx10-11')
        self.assertEqual(projected['arch'], 'arm64')
        self.assertEqual(projected['workspace_pvc'], 'agent-aoi-workspace')
        self.assertEqual(projected['runtime_source'], 'k3s-native')
        core.list_namespaced_pod.assert_called_once_with(
            'shizuha-fleet', label_selector='shizuha.io/agent=aoi', limit=3,
        )

    def test_shape_b_missing_agent_label_falls_back_to_deployment_selector(self):
        """HIVE-1326: a migrated Shape-B deployment labels pods `app: agent-<name>`
        without the canonical `shizuha.io/agent` label. The canonical pod query
        must fall back to the deployment's own selector so the drawer still
        resolves the exact live pod/node/arch/workspace instead of showing the
        generic daemon placeholder for an otherwise-healthy live agent."""
        live = _make_pod('agent-revi-6c69544f5-sm98z', created_ago=60)
        live.spec.node_name = 'gx10-13'
        agent_container = MagicMock(name='agent-container')
        agent_container.name = 'agent'
        agent_container.image = 'localhost:30500/shizuha-hive:harness-202607221533-2f2389e'
        live.spec.containers = [agent_container]
        live.spec.volumes = []

        deployment = MagicMock()
        deployment.spec.selector.match_labels = {'app': 'agent-revi'}
        deployment.spec.template.spec.containers = [agent_container]
        volume = MagicMock()
        volume.persistent_volume_claim.claim_name = 'revi-workspace'
        deployment.spec.template.spec.volumes = [volume]

        core = MagicMock()
        # Canonical query: no pods match shizuha.io/agent=revi (Shape B label gap).
        # Fallback query: matches the deployment selector {@app: agent-revi}.
        core.list_namespaced_pod.side_effect = [
            type('R', (), {'items': []})(),
            type('R', (), {'items': [live]})(),
        ]
        node = MagicMock()
        node.metadata.labels = {'kubernetes.io/arch': 'arm64'}
        core.read_node.return_value = node
        apps = MagicMock()
        apps.read_namespaced_deployment.return_value = deployment

        with patch('fleet.provisioner.get_k8s_client', return_value=(core, apps, None)):
            projected = enrich_k8s_placement({
                'agent_username': 'revi',
                'runtime_environment': 'container',
                'runtime_source': 'k3s-native',
                'namespace': 'shizuha-fleet',
            }, force=True)

        self.assertEqual(projected['pod_name'], 'agent-revi-6c69544f5-sm98z')
        self.assertEqual(projected['node_name'], 'gx10-13')
        self.assertEqual(projected['arch'], 'arm64')
        self.assertEqual(projected['workspace_pvc'], 'revi-workspace')
        self.assertEqual(projected['runtime_source'], 'k3s-native')
        # Canonical selector queried first, then the deployment-selector fallback.
        self.assertEqual(core.list_namespaced_pod.call_args_list[0].kwargs, {
            'label_selector': 'shizuha.io/agent=revi', 'limit': 3,
        })
        self.assertEqual(core.list_namespaced_pod.call_args_list[1].kwargs, {
            'label_selector': 'app=agent-revi', 'limit': 3,
        })

    def test_shape_b_fallback_prefers_live_replacement_over_terminating(self):
        """HIVE-1326: the deployment-selector fallback keeps the same rollout/
        terminating precedence — a retiring pod never beats the live replacement."""
        retiring = _make_pod('agent-revi-old', created_ago=5, terminating=True)
        replacement = _make_pod('agent-revi-new', created_ago=60)
        replacement.spec.node_name = 'gx10-13'

        deployment = MagicMock()
        deployment.spec.selector.match_labels = {'app': 'agent-revi'}

        core = MagicMock()
        core.list_namespaced_pod.side_effect = [
            type('R', (), {'items': []})(),
            type('R', (), {'items': [retiring, replacement]})(),
        ]
        node = MagicMock()
        node.metadata.labels = {'kubernetes.io/arch': 'arm64'}
        core.read_node.return_value = node
        apps = MagicMock()
        apps.read_namespaced_deployment.return_value = deployment

        with patch('fleet.provisioner.get_k8s_client', return_value=(core, apps, None)):
            projected = enrich_k8s_placement({
                'agent_username': 'revi',
                'runtime_environment': 'container',
                'runtime_source': 'k3s-native',
                'namespace': 'shizuha-fleet',
            }, force=True)

        self.assertEqual(projected['pod_name'], 'agent-revi-new')
        self.assertEqual(projected['node_name'], 'gx10-13')
        self.assertEqual(core.list_namespaced_pod.call_args_list[1].kwargs, {
            'label_selector': 'app=agent-revi', 'limit': 3,
        })

    @override_settings(HIVE609_TEMP_FRESHNESS_PRECEDENCE=True)
    def test_index_native_uuid_detail_path_forces_live_placement_projection(self):
        from fleet.views import FleetAgentDetailView
        native = [{
            'agent_username': 'aoi', 'id': 'uuid-aoi', 'status': 'running',
            'last_active_at': FRESH, 'runtime_source': 'k3s-native',
        }]
        runtime = {'aoi': {
            'agent_username': 'aoi', 'id': 'rt-aoi', 'status': 'running',
            'last_active_at': STALE, 'runtime_source': 'rt-fleet',
        }}
        selected_id = _merge_native_and_runtime(native, runtime)[0]['id']
        self.assertEqual(selected_id, 'uuid-aoi')

        agent = MagicMock(agent_username='aoi', owner_id=42)
        request = MagicMock()
        request.user.is_authenticated = True
        request.user.is_staff = False
        request.user.id = 42
        observed = {
            'id': selected_id, 'agent_username': 'aoi',
            'pod_name': 'agent-aoi-new', 'node_name': 'gx10-11',
            'arch': 'arm64', 'workspace_pvc': 'agent-aoi-workspace',
        }

        with (
            patch('fleet.views._get_agent', return_value=agent) as get_agent,
            patch('fleet.views.FleetAgentSerializer') as serializer,
            patch('fleet.runtime_fleet.enrich_k8s_placement', return_value=observed) as enrich,
            patch('fleet.views._with_telemetry', side_effect=lambda payload: payload),
        ):
            serializer.return_value.data = {
                'id': selected_id, 'agent_username': 'aoi',
                'runtime_environment': 'container', 'runtime_source': 'k3s-native',
            }
            response = FleetAgentDetailView().get(request, selected_id)

        get_agent.assert_called_once_with('uuid-aoi')
        enrich.assert_called_once_with(serializer.return_value.data, force=True)
        self.assertEqual(response.data['pod_name'], 'agent-aoi-new')
        self.assertEqual(response.data['workspace_pvc'], 'agent-aoi-workspace')


class FleetAgentActivityViewRoutingTests(SimpleTestCase):
    """FleetAgentActivityView dispatches to native or legacy path correctly."""

    def _make_request(self, user=None):
        request = MagicMock()
        if user is None:
            user = MagicMock()
            user.is_authenticated = True
            user.is_staff = True
        request.user = user
        request.query_params = {}
        return request

    def test_native_agent_uses_native_activity(self):
        """A FleetAgent DB row routes to _get_native_agent_activity."""
        from fleet.views import FleetAgentActivityView
        view = FleetAgentActivityView()
        request = self._make_request()
        with (
            patch('fleet.views._get_agent', return_value=MagicMock(agent_username='aoi')),
            patch('fleet.views._get_native_agent_activity',
                  return_value={'events': [{'event': 'test'}], 'total': 1}) as mock_native,
            patch('fleet.views.get_runtime_agent_activity') as mock_legacy,
        ):
            response = view.get(request, 'some-uuid')
        mock_native.assert_called_once()
        mock_legacy.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['total'], 1)

    def test_runtime_only_agent_uses_legacy_activity(self):
        """A non-DB agent (404 from _get_agent) routes to get_runtime_agent_activity."""
        from fleet.views import FleetAgentActivityView, Response
        view = FleetAgentActivityView()
        request = self._make_request()
        with (
            patch('fleet.views._get_agent',
                  return_value=Response({'error': 'not found'}, status=404)),
            patch('fleet.views._get_native_agent_activity') as mock_native,
            patch('fleet.views.get_runtime_agent_activity',
                  return_value={'events': [{'event': 'legacy'}], 'total': 1}) as mock_legacy,
        ):
            response = view.get(request, 'rt-daemon-id')
        mock_native.assert_not_called()
        mock_legacy.assert_called_once()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['total'], 1)

    def test_non_staff_without_owned_agent_returns_403(self):
        """Non-staff user with no owned native or personal agent gets 403."""
        from fleet.views import FleetAgentActivityView
        view = FleetAgentActivityView()
        user = MagicMock()
        user.is_authenticated = True
        user.is_staff = False
        request = self._make_request(user)
        with (
            patch('fleet.views._owned_native_agent', return_value=None),
            patch('fleet.views._owned_personal_agent', return_value=None),
        ):
            response = view.get(request, 'some-uuid')
        self.assertEqual(response.status_code, 403)

    def test_degraded_runtime_receipt_is_passed_through_not_relabelled(self):
        """HIVE-1552: a degraded runtime receipt is never relabelled as live."""
        from fleet.views import FleetAgentActivityView, Response
        view = FleetAgentActivityView()
        request = self._make_request()
        degraded = {
            'source_state': 'degraded',
            'source': 'legacy-history',
            'resource_uid': 'deploy-uid-1',
            'events': [],
            'history_events': [{'ts': '2026-07-04T08:57:50Z', 'type': 'message'}],
            'total': 0,
        }
        with (
            patch('fleet.views._get_agent',
                  return_value=Response({'error': 'not found'}, status=404)),
            patch('fleet.views._get_native_agent_activity') as mock_native,
            patch('fleet.views.get_runtime_agent_activity',
                  return_value=degraded) as mock_legacy,
        ):
            response = view.get(request, 'rt-daemon-id')
        mock_native.assert_not_called()
        mock_legacy.assert_called_once()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['source_state'], 'degraded')
        self.assertEqual(response.data['events'], [])
        self.assertEqual(len(response.data['history_events']), 1)

    def test_missing_receipt_is_rendered_unavailable(self):
        """HIVE-1552: a runtime response without a source_state is unavailable."""
        from fleet.views import FleetAgentActivityView, Response
        view = FleetAgentActivityView()
        request = self._make_request()
        with (
            patch('fleet.views._get_agent',
                  return_value=Response({'error': 'not found'}, status=404)),
            patch('fleet.views._get_native_agent_activity') as mock_native,
            patch('fleet.views.get_runtime_agent_activity',
                  return_value={'events': [{'event': 'x'}], 'total': 1}) as mock_legacy,
        ):
            response = view.get(request, 'rt-daemon-id')
        mock_native.assert_not_called()
        mock_legacy.assert_called_once()
        self.assertEqual(response.status_code, 200)
        # No receipt → explicitly unavailable, never silently labelled live.
        self.assertEqual(response.data['source_state'], 'unavailable')

    def test_empty_runtime_response_is_unavailable(self):
        """HIVE-1552: an empty runtime response is an explicit unavailable feed."""
        from fleet.views import FleetAgentActivityView, Response
        view = FleetAgentActivityView()
        request = self._make_request()
        with (
            patch('fleet.views._get_agent',
                  return_value=Response({'error': 'not found'}, status=404)),
            patch('fleet.views._get_native_agent_activity') as mock_native,
            patch('fleet.views.get_runtime_agent_activity', return_value={}) as mock_legacy,
        ):
            response = view.get(request, 'rt-daemon-id')
        mock_native.assert_not_called()
        mock_legacy.assert_called_once()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['source_state'], 'unavailable')
        self.assertEqual(response.data['events'], [])
