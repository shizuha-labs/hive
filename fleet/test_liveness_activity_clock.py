"""Execute the deployed ConfigMap caller and its real in-agent file probe."""
import contextlib
import datetime
import http.server
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml
from django.test import SimpleTestCase, TestCase
from kubernetes.client import ApiClient
from kubernetes.client.rest import ApiException

from fleet import liveness_activity
from fleet.models import FleetAgent


class LivenessActivityClockTests(TestCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.now = datetime.datetime.now(datetime.timezone.utc)
        self.old = (self.now - datetime.timedelta(days=7)).replace(microsecond=0)
        self.agent = FleetAgent.objects.create(
            agent_username='probe', namespace='test-fleet',
            status=FleetAgent.Status.RUNNING, last_active_at=self.old.replace(tzinfo=None))
        self.paths = {kind: tuple(str(self.directory / (kind + str(i))) for i, _ in enumerate(names))
                      for kind, names in liveness_activity.LOG_PATHS.items()}
        self.real_paths = dict(liveness_activity.LOG_PATHS)
        # PLAT-4924 (aoi ruling, option a): the fake stream runs the embedded
        # script's VIP probe LOCALLY (subprocess inherits os.environ), so the
        # prod default (in-cluster cortex Service :8040) would need the
        # shizuha-cortex Service to exist in the test cluster — it does not.
        # Point VIP_PROBE_URL at an ephemeral local
        # HTTP server instead: the probe is `curl -skS` (no -f), so any HTTP
        # response exercises the full healthy path with zero egress.
        class _OK(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.vip = http.server.HTTPServer(('127.0.0.1', 0), _OK)
        self.vip_port = self.vip.server_address[1]
        self.vip_thread = threading.Thread(target=self.vip.serve_forever, daemon=True)
        self.vip_thread.start()
        self.addCleanup(self.vip.server_close)
        self.addCleanup(self.vip.shutdown)

    def write(self, kind, record, touched=None):
        path = Path(self.paths[kind][0])
        path.write_text(json.dumps(record) + '\n')
        stamp = touched if touched is not None else self.now.timestamp()
        os.utime(path, (stamp, stamp))
        return path

    def pino(self, timestamp, message='Empty-queue heartbeat skipped'):
        return {'level': 30, 'time': int(timestamp.timestamp() * 1000), 'pid': 10,
                'hostname': 'agent-probe-pod', 'msg': message}

    def reconcile(self, stdout=None, unavailable=False, dry_run=True, during_probe=None, wire=None):
        path = Path(os.environ.get('LIVENESS_TEST_MANIFEST', 'k8s/fleet-agent-liveness-reconciler.yaml'))
        manifest = next(doc for doc in yaml.safe_load_all(path.read_text()) if doc.get('kind') == 'ConfigMap')
        core, apps = Mock(), Mock()
        pod = SimpleNamespace(metadata=SimpleNamespace(name='agent-probe-pod'),
                              status=SimpleNamespace(phase='Running', start_time=self.old))
        core.list_namespaced_pod.return_value.items = [pod]
        if unavailable:
            core.read_namespaced_pod_log.side_effect = ApiException(status=502)
        else:
            core.read_namespaced_pod_log.return_value = stdout if stdout is not None else (
                (self.now - datetime.timedelta(seconds=2)).isoformat() + ' waiting-for-broker-readyz\n')
        apps.read_namespaced_deployment.return_value.metadata.annotations = {}

        def stream(*args, **kwargs):
            if unavailable:
                raise RuntimeError('kubelet unavailable')
            self.assertEqual(kwargs['container'], 'agent')
            if kwargs['command'][:2] == ['sh', '-lc']:
                # PLAT-4924 (aoi ruling, option a): the VIP probe is a shell
                # command (curl "$1"). Simulate the pod by running it locally —
                # the subprocess inherits the patched env, so VIP_PROBE_URL
                # points at the setUp dummy server (zero egress).
                vip = subprocess.run(kwargs['command'], text=True, capture_output=True, timeout=10)
                return vip.stdout + vip.stderr
            self.assertEqual(kwargs['command'][:2], ['python3', '-c'])
            if during_probe is not None:
                during_probe()  # Fires on the activity probe only, not the VIP probe.
            code = kwargs['command'][2]
            # The baseline manifest's mtime-only probe uses the fixed old paths.
            # The candidate transmits the same parser exercised by these files.
            for kind, names in self.real_paths.items():
                for original, isolated in zip(names, self.paths[kind]):
                    code = code.replace(original, isolated)
            result = subprocess.run([sys.executable, '-c', code], text=True, capture_output=True, timeout=10)
            if not os.environ.get('LIVENESS_TEST_MANIFEST'):
                self.assertEqual(result.returncode, 0, result.stderr)
            self.probe_output = result.stdout + result.stderr
            if wire is not None:
                return wire(self.probe_output)
            # The real Kubernetes client JSON-decodes first, then str(dict)
            # produces single quotes/None. Returning raw JSON hid this boundary.
            with ApiClient() as api:
                return api.deserialize(SimpleNamespace(data=self.probe_output), 'str')

        output = io.StringIO()
        self.system_exit = None
        with patch('fleet.provisioner.get_k8s_client', return_value=(core, apps, None)), \
             patch('fleet.provisioner.reconcile_stopped_agent_drift', return_value={
                 'checked': 0, 'drifted': 0, 'scaled': 0, 'errors': []}), \
             patch('kubernetes.stream.stream', side_effect=stream), \
             patch.dict(os.environ, {'DRY_RUN': 'true' if dry_run else 'false',
                                     'VIP_PROBE_URL': f'http://127.0.0.1:{self.vip_port}/v1/models'}), \
             contextlib.redirect_stdout(output):
            try:
                exec(compile(manifest['data']['reconcile_liveness.py'], str(path), 'exec'), {})
            except SystemExit as exc:
                # PLAT-4924: the detector's fail-loud exit is part of the
                # contract; capture it so tests can assert both the exit and
                # the per-agent output printed before it.
                self.system_exit = exc
        self.agent.refresh_from_db()
        return output.getvalue(), apps

    def test_fresh_runtime_file_prevents_restart_across_real_exec_wire_shapes(self):
        event = (self.now - datetime.timedelta(minutes=5)).replace(microsecond=123000)
        stale_stdout = (self.now - datetime.timedelta(minutes=46)).isoformat() + ' boot complete\n'
        for label, wire in [('api-client', None), ('strict-json', lambda value: value),
                            ('native-dict', json.loads), ('bytes', lambda value: value.encode()),
                            ('bytes-repr', lambda value: repr(value.encode()))]:
            with self.subTest(wire=label):
                FleetAgent.objects.filter(pk=self.agent.pk).update(last_active_at=self.old.replace(tzinfo=None))
                self.write('pino', self.pino(event))
                output, apps = self.reconcile(stdout=stale_stdout, dry_run=False, wire=wire)
                apps.patch_namespaced_deployment.assert_not_called()
                self.assertIn('healthy=1', output)
                self.assertIn('restarted=0', output)
                self.assertEqual(self.agent.last_active_at, event.replace(tzinfo=None))
                # A touched old log remains a silence signal, not a new event.
                self.write('pino', self.pino(self.old), touched=self.now.timestamp())
                output, apps = self.reconcile(stdout=stale_stdout, dry_run=False, wire=wire)
                apps.patch_namespaced_deployment.assert_not_called()
                self.assertEqual(self.agent.last_active_at, event.replace(tzinfo=None))

    def test_malformed_exec_values_cannot_inject_activity_or_execute_code(self):
        marker = self.directory / 'must-not-exist'
        expression = "__import__('pathlib').Path(" + repr(str(marker)) + ").touch()"
        invalid = ['{partial', '[]', 'null', expression,
                   "{'file_mtime': " + expression + ", 'activity_epoch': None}",
                   {'file_mtime': None, 'activity_epoch': expression}]
        for label, value in enumerate(invalid):
            with self.subTest(case=label):
                output, apps = self.reconcile(stdout='', dry_run=False, wire=lambda _, value=value: value)
                apps.patch_namespaced_deployment.assert_not_called()
                self.assertIn('fail-open', output)
                self.assertEqual(self.agent.last_active_at, self.old.replace(tzinfo=None))
                self.assertFalse(marker.exists())
                self.assertNotIn(expression, output)

    def test_invalid_timestamp_types_do_not_fabricate_health_or_activity(self):
        for value in [True, False, '123', [], {}, float('nan'), float('inf'), -1, 0]:
            with self.subTest(value=repr(value)):
                output, apps = self.reconcile(stdout='', dry_run=False, wire=lambda _, value=value: {
                    'file_mtime': value, 'activity_epoch': value})
                apps.patch_namespaced_deployment.assert_not_called()
                self.assertIn('fail-open', output)
                self.assertNotIn('healthy=1', output)
                self.assertEqual(self.agent.last_active_at, self.old.replace(tzinfo=None))

    def test_bootstrap_stdout_without_runtime_does_not_advance_activity(self):
        output, apps = self.reconcile()
        self.assertEqual(self.agent.last_active_at, self.old.replace(tzinfo=None))
        apps.patch_namespaced_deployment.assert_not_called()
        self.assertIn('healthy=1', output)  # Existing silence policy is independent.

    def test_copy_touch_and_repeated_reconcile_use_event_time_not_mtime(self):
        self.write('pino', self.pino(self.old), touched=self.now.timestamp())
        self.reconcile()
        self.assertEqual(self.agent.last_active_at, self.old.replace(tzinfo=None))
        event = (self.now - datetime.timedelta(seconds=12)).replace(microsecond=123000)
        log = self.write('pino', self.pino(event))
        self.reconcile()
        self.assertEqual(self.agent.last_active_at, event.replace(tzinfo=None))
        os.utime(log, (self.now.timestamp() + 100, self.now.timestamp() + 100))
        self.reconcile()
        self.assertEqual(self.agent.last_active_at, event.replace(tzinfo=None))  # Same event cannot re-arm the clock.

    def test_copied_mtime_alone_retains_silence_signal_without_activity(self):
        self.write('pino', self.pino(self.old), touched=self.now.timestamp())
        output, apps = self.reconcile(stdout='', dry_run=False)
        self.assertIn('healthy=1', output)
        self.assertEqual(self.agent.last_active_at, self.old.replace(tzinfo=None))
        apps.patch_namespaced_deployment.assert_not_called()

    def test_audit_and_telemetry_advance_without_exposing_content(self):
        audit_at = self.now - datetime.timedelta(minutes=3)
        self.write('audit', {'id': 'tool1', 'phase': 'after', 'tool': 'read_file',
                            'timestamp': audit_at.isoformat(), 'resultSummary': 'PRIVATE_TOOL_OUTPUT'})
        self.reconcile(stdout='')
        self.assertEqual(self.agent.last_active_at, audit_at.replace(tzinfo=None))
        self.assertNotIn('PRIVATE_TOOL_OUTPUT', self.probe_output)
        ended = self.now - datetime.timedelta(seconds=20)
        self.write('telemetry', {'name': 'llm-turn', 'startTime': audit_at.isoformat(),
                                'endTime': ended.isoformat(), 'result': {'toolCalls': 0}})
        self.reconcile(stdout='')
        self.assertEqual(self.agent.last_active_at, ended.replace(tzinfo=None))

    def test_stdout_uses_inner_runtime_time_not_current_transport_prefix(self):
        event = (self.now - datetime.timedelta(minutes=5)).replace(microsecond=456000)
        line = self.now.isoformat() + ' ' + json.dumps(self.pino(event)) + '\n'
        self.reconcile(stdout=repr(line.encode()))
        self.assertEqual(self.agent.last_active_at, event.replace(tzinfo=None))

    def test_unrecognized_partial_and_future_records_do_not_fabricate_activity(self):
        self.write('pino', {'time': self.now.timestamp() * 1000, 'msg': 'bootstrap JSON'})
        self.write('audit', {'id': 'a', 'phase': 'after', 'tool': 'read', 'timestamp': '2026-09-02T01:02:03'})
        self.write('telemetry', {'name': 'llm-turn', 'endTime': (self.now + datetime.timedelta(days=2)).isoformat()})
        self.reconcile()
        self.assertEqual(self.agent.last_active_at, self.old.replace(tzinfo=None))
        self.write('pino', self.pino(self.now + datetime.timedelta(days=2)))
        self.reconcile()
        self.assertEqual(self.agent.last_active_at, self.old.replace(tzinfo=None))

    def test_unreadable_signals_preserve_existing_fail_open_safety(self):
        # PLAT-4924: with the kubelet unavailable the VIP probe cannot exec, so
        # the run ends with the detector's fail-loud SystemExit (line-251
        # contract) — but the per-agent flow must still have been fail-open:
        # no restart, the DB clock untouched, and the fail-open summary printed
        # before the exit.
        output, apps = self.reconcile(unavailable=True, dry_run=False)
        self.assertIsNotNone(self.system_exit)
        self.assertIn('cannot reach', str(self.system_exit))
        self.assertEqual(self.agent.last_active_at, self.old.replace(tzinfo=None))
        apps.patch_namespaced_deployment.assert_not_called()
        self.assertIn('fail-open', output)

    def test_real_silence_still_uses_existing_restart_policy(self):
        self.write('pino', self.pino(self.old), touched=self.old.timestamp())
        output, apps = self.reconcile(stdout='', dry_run=False)
        apps.patch_namespaced_deployment.assert_called_once()
        self.assertIn('restarted=1', output)
        self.assertEqual(self.agent.last_active_at, self.old.replace(tzinfo=None))

    def test_existing_newer_activity_is_not_overwritten_by_a_bounded_old_tail(self):
        newer = self.now - datetime.timedelta(seconds=1)
        FleetAgent.objects.filter(pk=self.agent.pk).update(last_active_at=newer.replace(tzinfo=None))
        self.write('pino', self.pino(self.old))
        self.reconcile()
        self.assertEqual(self.agent.last_active_at, newer.replace(tzinfo=None))

    def test_other_producer_during_pod_read_cannot_be_overwritten_and_null_can_initialize(self):
        measured = (self.now - datetime.timedelta(minutes=5)).replace(microsecond=456000)
        other_producer = (self.now - datetime.timedelta(seconds=1)).replace(tzinfo=None)
        self.write('pino', self.pino(measured))
        writes = []

        def interleave():
            writes.append(FleetAgent.objects.filter(pk=self.agent.pk).update(last_active_at=other_producer))

        self.reconcile(during_probe=interleave)
        self.assertEqual(writes, [1])
        self.assertEqual(self.agent.last_active_at, other_producer)
        self.reconcile()  # Repeated stale evidence still cannot move the DB clock backwards.
        self.assertEqual(self.agent.last_active_at, other_producer)
        FleetAgent.objects.filter(pk=self.agent.pk).update(last_active_at=None)
        self.reconcile()
        self.assertEqual(self.agent.last_active_at, measured.replace(tzinfo=None))
        newer_event = (self.now - datetime.timedelta(seconds=2)).replace(microsecond=789000)
        self.write('pino', self.pino(newer_event))
        self.reconcile()
        self.assertEqual(self.agent.last_active_at, newer_event.replace(tzinfo=None))


class RuntimeActivityRecordTests(SimpleTestCase):
    def test_offset_timestamp_is_normalized_without_host_timezone(self):
        record = {'id': 'a', 'phase': 'before', 'tool': 'read',
                  'timestamp': '2026-09-02T15:18:59.926+05:30'}
        expected = datetime.datetime(2026, 9, 2, 9, 48, 59, 926000, tzinfo=datetime.timezone.utc)
        self.assertEqual(liveness_activity.runtime_event_epoch(record, 'audit', expected.timestamp()), expected.timestamp())

    def test_byte_bound_discards_clipped_record_but_keeps_valid_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'pino'
            now = datetime.datetime.now(datetime.timezone.utc)
            record = {'level': 30, 'time': int((now.timestamp() - 10) * 1000), 'pid': 10,
                      'hostname': 'agent-pod', 'msg': 'runtime event'}
            path.write_text('x' * (liveness_activity.MAX_TAIL_BYTES + 100) + '\n' + json.dumps(record) + '\n{partial')
            signal = liveness_activity.read_file_signals({'pino': [str(path)]})
            self.assertEqual(signal['activity_epoch'], record['time'] / 1000)
            self.assertEqual(set(signal), {'file_mtime', 'activity_epoch', 'activity_source'})
