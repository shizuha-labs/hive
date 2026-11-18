"""HIVE-2624 follow-up — liveness reconciler contract hardening.

Two defects from the fleet-agent-liveness-reconciler-29915960 KubeJobFailed
(waka/ichi receipt chain on PLAT-13371, cmts 1296776 → 1296791 → 1296795):

1. The PLAT-9950 fail-open contract was violated in control flow: an
   INDETERMINATE probe (``vip_reachable`` returning ``None`` — transport
   crash, exec denial, or an exec against a dissolved pod) flowed into
   ``if not reachable:`` and incremented ``vip_failed``, so any exec
   failure SystemExit-ed the whole Job. Only a positively-observed refusal
   (``False``) is the page-class signal; None is counted separately and
   stays page-free.

2. The seat ledger pinned dissolved pods: ``FleetAgent.pod_name`` written
   by the runtime consumers persisted after the seat's Deployment dissolved
   (the starved-pool family: desired 0/0, deployment gen hundreds past the
   pinned pod), re-failing tick over tick. A DB-pinned pod_name absent from
   a successfully listed live pod set must be pruned, typed, never on a
   list failure (indeterminate is not evidence of dissolution).
"""
from pathlib import Path

import yaml
from django.test import SimpleTestCase

MANIFEST = Path(__file__).resolve().parents[1] / 'k8s/fleet-agent-liveness-reconciler.yaml'


def _script():
    docs = list(yaml.safe_load_all(MANIFEST.read_text()))
    config_map = next(
        doc for doc in docs
        if doc.get('kind') == 'ConfigMap'
        and doc['metadata']['name'] == 'fleet-agent-liveness-reconciler-script'
    )
    return config_map['data']['reconcile_liveness.py']


class VipFailOpenContractTests(SimpleTestCase):
    """PLAT-9950's "fail-open" promise must hold in control flow, not just
    in the classifier: None (indeterminate) never reaches vip_failed."""

    def test_script_compiles(self):
        compile(_script(), str(MANIFEST), 'exec')

    def test_refusal_guard_is_identity_false_not_falsiness(self):
        script = _script()
        self.assertIn('if reachable is False:', script)
        # The falsiness form would also match None (indeterminate) — banned.
        self.assertNotIn('if not reachable:', script)

    def test_indeterminate_stays_separate_from_refusal(self):
        script = _script()
        self.assertIn('vip_transport_errors += 1', script)
        self.assertIn('VIP-TRANSPORT-ERROR', script)
        self.assertIn('VIP-REFUSED', script)

    def test_fail_blindness_guard_still_exits_on_total_transport_blindness(self):
        """PLAT-4924: total transport blindness (no positive probe at all)
        must still SystemExit — the fail-open fix must not create silence."""
        script = _script()
        self.assertIn('if vip_transport_errors and not (healthy + wedged + restarted + vip_failed):', script)


class SeatLedgerPruneContractTests(SimpleTestCase):
    """Dissolved-seat references must be pruned from the seat ledger,
    typed, and only on a positively successful pod list."""

    def test_prune_is_typed_and_guarded_on_list_success(self):
        script = _script()
        self.assertIn('LEDGER-PRUNE', script)
        self.assertIn("listed_ok and a.pod_name", script)
        self.assertIn('dissolved-seat reference pruned', script)

    def test_prune_clears_the_pinned_reference(self):
        script = _script()
        self.assertIn(
            "FleetAgent.objects.filter(pk=a.pk).exclude(pod_name='').update(pod_name='')",
            script)
