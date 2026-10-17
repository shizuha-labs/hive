import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from security.hosted_claw_wan_egress_probe import (
    DEFAULT_ALLOWED_TARGETS,
    DEFAULT_BLOCKED_TARGETS,
    WanEgressProbeConfig,
    build_node_probe_script,
    run_probe,
    select_probe_target,
)


class FakeCore:
    def __init__(self, namespaces=None, pods=None):
        self.namespaces = namespaces or []
        self.pods = pods or []

    def list_namespace(self, label_selector=None):
        self.last_namespace_selector = label_selector
        return SimpleNamespace(items=self.namespaces)

    def list_namespaced_pod(self, namespace, label_selector=None):
        self.last_pod_lookup = (namespace, label_selector)
        return SimpleNamespace(items=self.pods)

    def connect_get_namespaced_pod_exec(self, *args, **kwargs):  # pragma: no cover - patched stream uses the callable only
        raise AssertionError('stream.stream should be patched')


def ns(name):
    return SimpleNamespace(metadata=SimpleNamespace(name=name))


def pod(name, phase='Running'):
    return SimpleNamespace(metadata=SimpleNamespace(name=name), status=SimpleNamespace(phase=phase))


class HostedClawWanEgressProbeTests(unittest.TestCase):
    def test_refuses_to_auto_select_unlabelled_customer_claw(self):
        with self.assertRaisesRegex(RuntimeError, 'no hosted-claw WAN-egress smoke namespace'):
            select_probe_target(FakeCore(namespaces=[]), WanEgressProbeConfig())

    def test_selects_single_labelled_internal_namespace_and_running_claw_pod(self):
        target = select_probe_target(
            FakeCore(namespaces=[ns('claw-internal-smoke')], pods=[pod('claw-0', 'Pending'), pod('claw-1')]),
            WanEgressProbeConfig(),
        )
        self.assertEqual(target.namespace, 'claw-internal-smoke')
        self.assertEqual(target.pod, 'claw-1')

    def test_config_env_parses_public_statuses_and_blocked_targets(self):
        with patch.dict(os.environ, {
            'HIVE_WAN_EGRESS_PUBLIC_STATUSES': '200,301',
            'HIVE_WAN_EGRESS_BLOCKED_TARGETS': 'metadata=169.254.169.254:80,tailnet=100.64.0.1:443',
        }, clear=True):
            cfg = WanEgressProbeConfig.from_env()
        self.assertEqual(cfg.public_statuses, (200, 301))
        self.assertEqual(cfg.blocked_targets, ('metadata=169.254.169.254:80', 'tailnet=100.64.0.1:443'))

    def test_node_script_covers_required_blocked_ranges(self):
        script = build_node_probe_script(WanEgressProbeConfig())
        for expected in ('10.43.0.1', '10.42.0.1', '192.168.1.1', '169.254.169.254', '100.64.0.1'):
            self.assertIn(expected, script)
        self.assertEqual(len(DEFAULT_BLOCKED_TARGETS), 5)

    # ── PLAT-8489: the s1 bridge ports must be asserted as MUST-CONNECT ──

    def test_default_allowed_targets_are_the_s1_bridge_ports(self):
        self.assertEqual(
            DEFAULT_ALLOWED_TARGETS,
            (
                's1-ori-mcp=100.64.0.3:9500',
                's1-drive-mcp=100.64.0.3:9600',
                's1-fleet-control=100.64.0.3:8016',
            ),
        )

    def test_config_env_parses_allowed_targets(self):
        with patch.dict(os.environ, {
            'HIVE_WAN_EGRESS_ALLOWED_TARGETS': 's1-ori-mcp=100.64.0.3:9500',
        }, clear=True):
            cfg = WanEgressProbeConfig.from_env()
        self.assertEqual(cfg.allowed_targets, ('s1-ori-mcp=100.64.0.3:9500',))

    def test_node_script_embeds_allowed_targets_and_must_connect_semantics(self):
        script = build_node_probe_script(WanEgressProbeConfig())
        for expected in ('100.64.0.3:9500', '100.64.0.3:9600', '100.64.0.3:8016', 'checkAllowed'):
            self.assertIn(expected, script)
        # a failed allowed connect must fail the whole probe, not be skipped
        self.assertIn("for (const raw of allowedTargets) results.push(await checkAllowed(raw))", script)

    def test_run_probe_fails_when_an_allowed_s1_bridge_target_is_clipped(self):
        core = FakeCore(namespaces=[ns('claw-smoke')], pods=[pod('claw-abc')])
        payload = {
            'ok': False,
            'results': [
                {'name': 'public-https', 'ok': True, 'status': 200},
                {'name': 's1-ori-mcp', 'host': '100.64.0.3', 'port': 9500, 'ok': False, 'error': 'timeout (egress clip?)'},
            ],
        }
        with patch('security.hosted_claw_wan_egress_probe._exec_pod', return_value=json.dumps(payload)):
            ok, result = run_probe(core, WanEgressProbeConfig())
        self.assertFalse(ok)
        self.assertEqual(result['results'][1]['port'], 9500)

    def test_run_probe_execs_node_in_target_pod_and_returns_payload(self):
        core = FakeCore(namespaces=[ns('claw-smoke')], pods=[pod('claw-abc')])
        payload = {'ok': True, 'results': [{'name': 'public-https', 'ok': True, 'status': 301}]}
        with patch('security.hosted_claw_wan_egress_probe._exec_pod', return_value=json.dumps(payload)) as mocked:
            ok, result = run_probe(core, WanEgressProbeConfig())
        self.assertTrue(ok)
        self.assertEqual(result['target'], {'namespace': 'claw-smoke', 'pod': 'claw-abc'})
        args, kwargs = mocked.call_args
        self.assertEqual(args[:3], (core, 'claw-smoke', 'claw-abc'))
        self.assertEqual(kwargs['command'][:2], ['node', '-e'])

    def test_run_probe_surfaces_failed_public_or_leaky_internal_result(self):
        core = FakeCore(namespaces=[ns('claw-smoke')], pods=[pod('claw-abc')])
        payload = {'ok': False, 'results': [{'name': 'metadata', 'ok': False, 'error': 'unexpected TCP connect'}]}
        with patch('security.hosted_claw_wan_egress_probe._exec_pod', return_value=json.dumps(payload)):
            ok, result = run_probe(core, WanEgressProbeConfig())
        self.assertFalse(ok)
        self.assertEqual(result['results'][0]['error'], 'unexpected TCP connect')


    # ── HIVE-537: k8s stream returns a Python-literal dict string ──

    def test_extract_json_accepts_python_literal_dict_from_k8s_stream(self):
        from security.hosted_claw_wan_egress_probe import _extract_json
        # Exact shape observed live from stream.stream(...) on 2026-07-05
        raw = "{'ok': True, 'results': [{'name': 'public-https', 'ok': True, 'status': 200}]}"
        payload = _extract_json(raw)
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['results'][0]['name'], 'public-https')

    def test_extract_json_still_prefers_strict_json(self):
        from security.hosted_claw_wan_egress_probe import _extract_json
        payload = _extract_json('noise {"ok": false, "results": []} trailing')
        self.assertFalse(payload['ok'])

    def test_extract_json_rejects_garbage_and_non_dict_payloads(self):
        from security.hosted_claw_wan_egress_probe import _extract_json
        with self.assertRaises(RuntimeError):
            _extract_json('no braces at all')
        with self.assertRaises(RuntimeError):
            _extract_json("{'not': 'closed'")  # find/rfind slice is not parseable
        with self.assertRaises(RuntimeError):
            _extract_json('{1, 2, 3}')  # a set literal is not an object payload

    def test_run_probe_succeeds_end_to_end_with_literal_stream_output(self):
        payload = {'ok': True, 'results': [{'name': 'public-https', 'ok': True, 'status': 200}]}
        core = FakeCore(namespaces=[ns('claw-smoke')], pods=[pod('claw-abc')])
        with patch('security.hosted_claw_wan_egress_probe._exec_pod', return_value=repr(payload)):
            ok, result = run_probe(core, WanEgressProbeConfig())
        self.assertTrue(ok)
        self.assertEqual(result['results'][0]['status'], 200)


if __name__ == '__main__':
    unittest.main()
