"""HIVE-2624 — drift-check gains a ready-replicas leg (detection-only).

Pins the detection signal for the HIVE-2595 wedge class: an agent Hive
intends ACTIVE whose Deployment sits at ``spec.replicas>=1`` with
``ready_replicas==0`` past the bounded window. During the incident three
consecutive BackoffLimitExceeded episodes produced no detection signal —
every desired-vs-actual comparison looked satisfied.

The fixture reproduces that exact state with a mocked k8s Deployment and
asserts the sweep fires (gauge stamped, event logged), stays silent inside
the bounded window, and never misfires on healthy / scaled-to-zero /
missing-Deployment workloads.
"""
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone as tz

from fleet import fleet_readiness_metrics, provisioner
from fleet.models import FleetAgent


def _deployment(*, spec_replicas, ready_replicas, available_false_since=None):
    """Build a mock k8s Deployment matching the HIVE-2595 shape."""
    dep = mock.Mock()
    dep.spec.replicas = spec_replicas
    dep.status.ready_replicas = ready_replicas
    conditions = []
    if available_false_since is not None:
        cond = mock.Mock()
        cond.type = 'Available'
        cond.status = 'False'
        cond.last_transition_time = available_false_since
        conditions.append(cond)
    dep.status.conditions = conditions
    return dep


class ActiveDesiredZeroReadyDetectionTests(TestCase):
    def setUp(self):
        self.agent = FleetAgent.objects.create(
            agent_username='wedgevictim',
            status=FleetAgent.Status.RUNNING,
        )
        fleet_readiness_metrics.record_active_zero_ready({})

    def _run(self, dep):
        with mock.patch.object(provisioner, 'get_k8s_client') as client, \
                mock.patch.object(provisioner, '_log_event') as log_event:
            client.return_value = (mock.Mock(), mock.Mock(read_namespaced_deployment=lambda n, ns: dep), mock.Mock())
            summary = provisioner.reconcile_active_agent_readiness()
        return summary, log_event

    def test_fires_on_hive2595_state_beyond_bounded_window(self):
        """spec.replicas=1, ready=0, Available=False for >15m — the incident state."""
        since = tz.now() - timedelta(seconds=1800)
        summary, log_event = self._run(_deployment(spec_replicas=1, ready_replicas=0, available_false_since=since))
        self.assertEqual(summary['fired'], 1)
        self.assertEqual(summary['unready'], 1)
        log_event.assert_called_once()
        self.assertEqual(log_event.call_args.args[1], 'active_desired_zero_ready')
        snap = fleet_readiness_metrics.snapshot()
        self.assertEqual(snap.get('wedgevictim'), 1)
        self.assertIn('hive_fleet_active_desired_zero_ready{agent="wedgevictim"} 1', fleet_readiness_metrics.to_prometheus())

    def test_silent_inside_bounded_window(self):
        """0 ready for 5 minutes is a rollout transient, not a wedge."""
        since = tz.now() - timedelta(seconds=300)
        summary, log_event = self._run(_deployment(spec_replicas=1, ready_replicas=0, available_false_since=since))
        self.assertEqual(summary['fired'], 0)
        log_event.assert_not_called()
        self.assertNotIn('wedgevictim', fleet_readiness_metrics.snapshot())

    def test_silent_when_ready(self):
        since = tz.now() - timedelta(seconds=1800)
        summary, _ = self._run(_deployment(spec_replicas=1, ready_replicas=1, available_false_since=since))
        self.assertEqual(summary['fired'], 0)

    def test_silent_when_scaled_to_zero(self):
        """Hibernated/scaled-to-zero is a different lifecycle state, not this defect."""
        summary, _ = self._run(_deployment(spec_replicas=0, ready_replicas=0))
        self.assertEqual(summary['fired'], 0)
        self.assertEqual(summary['unready'], 0)

    def test_fires_conservatively_without_available_condition(self):
        """Missing Available condition with 0 ready replicas still fires — the
        replica facts alone are the signal (fail-loud, not fail-blind)."""
        summary, _ = self._run(_deployment(spec_replicas=1, ready_replicas=0, available_false_since=None))
        self.assertEqual(summary['fired'], 1)

    def test_recovery_drives_same_series_to_zero(self):
        since = tz.now() - timedelta(seconds=1800)
        self._run(_deployment(spec_replicas=1, ready_replicas=0, available_false_since=since))
        self.assertEqual(fleet_readiness_metrics.snapshot().get('wedgevictim'), 1)
        # Next sweep observes a ready Deployment → same series to 0 in place.
        self._run(_deployment(spec_replicas=1, ready_replicas=1))
        snap = fleet_readiness_metrics.snapshot()
        self.assertEqual(snap.get('wedgevictim'), 0)
        self.assertIn('hive_fleet_active_desired_zero_ready{agent="wedgevictim"} 0', fleet_readiness_metrics.to_prometheus())

    def test_dry_run_stamps_nothing(self):
        since = tz.now() - timedelta(seconds=1800)
        dep = _deployment(spec_replicas=1, ready_replicas=0, available_false_since=since)
        with mock.patch.object(provisioner, 'get_k8s_client') as client, \
                mock.patch.object(provisioner, '_log_event') as log_event:
            client.return_value = (mock.Mock(), mock.Mock(read_namespaced_deployment=lambda n, ns: dep), mock.Mock())
            summary = provisioner.reconcile_active_agent_readiness(dry_run=True)
        self.assertEqual(summary['fired'], 1)
        log_event.assert_not_called()
        self.assertNotIn('wedgevictim', fleet_readiness_metrics.snapshot())

    def test_missing_deployment_is_not_an_error(self):
        from kubernetes.client.rest import ApiException
        with mock.patch.object(provisioner, 'get_k8s_client') as client, \
                mock.patch.object(provisioner, '_log_event'):
            api_exc = ApiException(status=404)
            apps = mock.Mock()
            apps.read_namespaced_deployment.side_effect = api_exc
            client.return_value = (mock.Mock(), apps, mock.Mock())
            summary = provisioner.reconcile_active_agent_readiness()
        self.assertEqual(summary['fired'], 0)
        self.assertEqual(summary['errors'], 0)
