from pathlib import Path

import yaml
from django.test import SimpleTestCase


class CortexVipProbeReconcilerTests(SimpleTestCase):
    """PLAT-4549: the fleet-agent liveness reconciler must probe per-runtime
    Cortex VIP reachability by exec-ing inside each running agent container, and
    fail the Job (-> the existing KubeJobFailed alert) when any runtime cannot
    reach the VIP. That turns a runtime-specific NetworkPolicy/CNI refusal into a
    detectable, first-responder-actionable signal even while fleet-wide/external
    probes stay green.
    """

    def test_reconciler_script_probes_vip_and_fails_job_on_refusal(self):
        manifest = (
            Path(__file__).resolve().parents[1]
            / 'k8s/fleet-agent-liveness-reconciler.yaml'
        )
        docs = list(yaml.safe_load_all(manifest.read_text()))
        config_map = next(
            doc for doc in docs
            if doc.get('kind') == 'ConfigMap'
            and doc['metadata']['name'] == 'fleet-agent-liveness-reconciler-script'
        )
        script = config_map['data']['reconcile_liveness.py']

        # The embedded control script must compile and carry the probe contract:
        # per-pod exec probe, the VIP-REFUSED signal, and a non-zero Job exit.
        compile(script, str(manifest), 'exec')
        self.assertIn('connect_get_namespaced_pod_exec', script)
        self.assertIn('VIP-REFUSED', script)
        self.assertIn('if vip_failed:', script)
        self.assertIn('raise SystemExit', script)


class VipProbeErrorClassificationTests(SimpleTestCase):
    """PLAT-9950 engineering leg: exec failures must classify as a typed
    ``vip_probe_error`` with an actionable reason — never surface as the
    NoneType.decode crash class, and never silently swallow into "no signal".

    Classes: ``transport`` (kubernetes-client/websocket crash), ``exec_denied``
    (ApiException 401/403 — the isolated-namespace RBAC class, cured only via
    PLAT-9114's scoped bootstrap), ``missing_source`` (exec ran but returned
    nothing decodable). All three stay fail-open: none increments ``vip_failed``
    (a real refusal drives that counter and the Job exit).
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        manifest = (
            Path(__file__).resolve().parents[1]
            / 'k8s/fleet-agent-liveness-reconciler.yaml'
        )
        docs = list(yaml.safe_load_all(manifest.read_text()))
        config_map = next(
            doc for doc in docs
            if doc.get('kind') == 'ConfigMap'
            and doc['metadata']['name'] == 'fleet-agent-liveness-reconciler-script'
        )
        cls.script = config_map['data']['reconcile_liveness.py']

    def test_script_compiles_with_typed_classification(self):
        compile(self.script, 'reconcile_liveness.py', 'exec')

    def test_transport_failures_classify_typed_not_as_decode_crash(self):
        # The transport branch carries the typed token and the actionable
        # exception class — the old bare transport_error string is gone.
        self.assertIn("f'vip_probe_error={cls}:{type(exc).__name__}: {exc}'", self.script)
        self.assertNotIn("f'transport_error=", self.script)

    def test_exec_denied_class_gates_on_api_exception_401_403(self):
        # The 401/403 gate lives in the shared classifier (fleet/liveness_activity.py);
        # the embedded script consumes it via classify_exec_error.
        classifier = (
            Path(__file__).resolve().parents[1] / 'fleet/liveness_activity.py'
        ).read_text()
        self.assertIn('in (401, 403)', classifier)
        self.assertIn("'exec_denied'", classifier)
        self.assertIn('classify_exec_error', self.script)

    def test_missing_source_decode_swallow_is_typed_and_counted(self):
        self.assertIn('vip_probe_error=missing_source', self.script)
        self.assertIn('FILELOG_DECODE_ERROR', self.script)
        self.assertIn("probe_errors['missing_source'] += 1", self.script)

    def test_summary_carries_typed_probe_error_breakdown(self):
        for cls in ('transport', 'exec_denied', 'missing_source'):
            self.assertIn(f'probe_errors["{cls}"]', self.script)

    def test_fail_open_contract_preserved_for_typed_errors(self):
        # The typed classification lives in the INDETERMINATE branch and the
        # source-probe error paths; the vip_failed increment (real refusal)
        # must stay outside the reachable-is-None branch.
        none_branch = (
            self.script.split('if reachable is None:', 1)[1]
            .split('if not reachable:', 1)[0]
        )
        self.assertIn('vip_transport_errors += 1', none_branch)
        self.assertNotIn('vip_failed += 1', none_branch)
        self.assertIn('raise SystemExit', self.script)

    def test_classifier_maps_real_exception_types(self):
        from kubernetes.client.rest import ApiException

        from fleet import liveness_activity

        self.assertEqual(
            liveness_activity.classify_exec_error(ApiException(status=403)),
            'exec_denied')
        self.assertEqual(
            liveness_activity.classify_exec_error(ApiException(status=401)),
            'exec_denied')
        self.assertEqual(
            liveness_activity.classify_exec_error(ApiException(status=404)),
            'transport')
        self.assertEqual(
            liveness_activity.classify_exec_error(
                AttributeError("'NoneType' object has no attribute 'decode'")),
            'transport')
        self.assertEqual(
            liveness_activity.classify_exec_error(RuntimeError('stream died')),
            'transport')
