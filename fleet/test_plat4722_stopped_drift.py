"""PLAT-4722: reconcile k8s-running-but-Hive-stopped orphan drift.

A FleetAgent Hive already thinks is stopped/hibernated (intended) but whose k8s
Deployment is still scaled to 1 is an orphan neither the on-demand reconciler
nor the harness roller corrects. This pins `reconcile_stopped_agent_drift()`
converges the k8s observation TO Hive's authoritative intent (scale to 0).
"""
from unittest.mock import MagicMock, patch

from django.test import TestCase

from fleet import provisioner
from fleet.models import FleetAgent


def _agent(**kw):
    defaults = dict(
        agent_username='driftagent',
        agent_id='agent-drift',
        display_name='Drift',
        identity_user_id=4242,
        status=FleetAgent.Status.STOPPED,
    )
    defaults.update(kw)
    return FleetAgent(**defaults)


def _deployment(replicas):
    dep = MagicMock()
    dep.spec.replicas = replicas
    return dep


class ReconcileStoppedAgentDriftTests(TestCase):
    def setUp(self):
        self.apps = MagicMock()
        patcher = patch.object(
            provisioner, 'get_k8s_client',
            return_value=(MagicMock(), self.apps, MagicMock()),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher2 = patch.object(provisioner, '_log_event')
        patcher2.start()
        self.addCleanup(patcher2.stop)

    def test_scales_drifted_stopped_agent_to_zero(self):
        agent = _agent()
        agent.save()
        self.apps.read_namespaced_deployment.return_value = _deployment(1)

        summary = provisioner.reconcile_stopped_agent_drift()

        self.assertEqual(summary['checked'], 1)
        self.assertEqual(summary['drifted'], 1)
        self.assertEqual(summary['scaled'], 1)
        self.apps.patch_namespaced_deployment_scale.assert_called_once()
        call = self.apps.patch_namespaced_deployment_scale.call_args
        self.assertEqual(call.kwargs['body'], {'spec': {'replicas': 0}})

    def test_leaves_already_scaled_zero_alone(self):
        agent = _agent()
        agent.save()
        self.apps.read_namespaced_deployment.return_value = _deployment(0)

        summary = provisioner.reconcile_stopped_agent_drift()

        self.assertEqual(summary['checked'], 1)
        self.assertEqual(summary['drifted'], 0)
        self.assertEqual(summary['scaled'], 0)
        self.apps.patch_namespaced_deployment_scale.assert_not_called()

    def test_hibernated_agent_drift_also_converged(self):
        agent = _agent(status=FleetAgent.Status.HIBERNATED)
        agent.save()
        self.apps.read_namespaced_deployment.return_value = _deployment(1)

        summary = provisioner.reconcile_stopped_agent_drift()

        self.assertEqual(summary['scaled'], 1)

    def test_dry_run_does_not_scale(self):
        agent = _agent()
        agent.save()
        self.apps.read_namespaced_deployment.return_value = _deployment(1)

        summary = provisioner.reconcile_stopped_agent_drift(dry_run=True)

        self.assertEqual(summary['drifted'], 1)
        self.assertEqual(summary['scaled'], 0)
        self.apps.patch_namespaced_deployment_scale.assert_not_called()

    def test_missing_deployment_is_not_an_error(self):
        from kubernetes.client.rest import ApiException

        agent = _agent()
        agent.save()
        self.apps.read_namespaced_deployment.side_effect = ApiException(status=404)

        summary = provisioner.reconcile_stopped_agent_drift()

        self.assertEqual(summary['checked'], 1)
        self.assertEqual(summary['drifted'], 0)
        self.assertEqual(summary['errors'], 0)
