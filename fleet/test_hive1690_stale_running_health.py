"""HIVE-1690 — Deployment readiness must refute an asserted `running` state.

Binding v1.1 (aoi/sora on this task): do NOT threshold `last_active_at`
(written on provision/start, not a heartbeat). Use one observed readiness
source — the Claw Deployment's ready_replicas — producing typed
`runtime_unavailable` / `runtime_health_unknown` issues on service=runtime.

Reported fixture (@mika, owner vantage): status=running, health_summary.ok=true
with month-stale last_active_at while the card said "about 1 month ago".
"""
from django.test import SimpleTestCase
from unittest.mock import patch

from fleet.views import (
    _compute_agent_health_summary,
    _observe_claw_runtime_readiness,
)


def _iso_seconds_ago(seconds: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


class StaleRunningAgentHealthTests(SimpleTestCase):

    def _agent(self, **kw):
        base = {
            'kind': 'claw',
            'agent_username': 'claw:1d176064-4499-4ee6-a107-5c6e03a3fb35',
            'status': 'running',
            'health_ok': True,
            'needs_help': False,
            'namespace': 'claw-16-1d176064',
        }
        base.update(kw)
        return base

    # ---- the reported class of defect ---------------------------------------

    def test_running_with_unavailable_deployment_is_not_ok(self):
        """Claimed running + no ready replicas must not paint ALIVE."""
        summary = _compute_agent_health_summary(self._agent(
            runtime_readiness='unavailable',
            last_active_at=_iso_seconds_ago(30 * 24 * 3600),
        ))
        self.assertFalse(summary['ok'])
        self.assertEqual(summary['highest_severity'], 'error')
        self.assertIn('runtime_unavailable', summary['issue_types'])
        issue = next(i for i in summary['issues'] if i['type'] == 'runtime_unavailable')
        self.assertEqual(issue['service'], 'runtime')
        self.assertEqual(issue['severity'], 'error')

    def test_running_with_unknown_readiness_is_not_ok(self):
        summary = _compute_agent_health_summary(self._agent(
            runtime_readiness='unknown',
        ))
        self.assertFalse(summary['ok'])
        self.assertIn('runtime_health_unknown', summary['issue_types'])

    def test_last_active_at_age_alone_does_not_fault(self):
        """Binding v1.1: never threshold last_active_at. A ready Deployment
        with old last_active_at (idle but healthy) stays ok."""
        summary = _compute_agent_health_summary(self._agent(
            runtime_readiness='ready',
            last_active_at=_iso_seconds_ago(30 * 24 * 3600),
        ))
        self.assertTrue(summary['ok'])
        self.assertNotIn('runtime_unavailable', summary['issue_types'])
        self.assertNotIn('runtime_health_unknown', summary['issue_types'])
        # age is still published for the card clock
        self.assertIsNotNone(summary['last_snapshot_age_s'])
        self.assertGreater(summary['last_snapshot_age_s'], 24 * 3600)

    def test_ready_running_stays_ok(self):
        summary = _compute_agent_health_summary(self._agent(
            runtime_readiness='ready',
            last_active_at=_iso_seconds_ago(30),
        ))
        self.assertTrue(summary['ok'])

    def test_absent_readiness_does_not_manufacture_a_fault(self):
        """No observation yet → do not invent unavailable. Callers must attach
        readiness for claw rows; native fleet agents without the field stay
        on their existing health signals."""
        summary = _compute_agent_health_summary(self._agent())
        self.assertNotIn('runtime_unavailable', summary['issue_types'])
        self.assertNotIn('runtime_health_unknown', summary['issue_types'])

    def test_non_running_statuses_are_not_faulted_for_readiness(self):
        for status in ('stopped', 'hibernated', 'offline', 'pending', 'failed'):
            with self.subTest(status=status):
                summary = _compute_agent_health_summary(self._agent(
                    status=status, runtime_readiness='unavailable',
                ))
                self.assertNotIn('runtime_unavailable', summary['issue_types'])

    def test_unavailable_does_not_mask_provider_issue(self):
        summary = _compute_agent_health_summary(self._agent(
            provider_unavailable=True,
            runtime_readiness='unavailable',
        ))
        self.assertIn('provider_unavailable_or_quota', summary['issue_types'])
        self.assertIn('runtime_unavailable', summary['issue_types'])
        self.assertFalse(summary['ok'])

    def test_issue_types_registered_as_errors(self):
        from fleet.views import _HEALTH_ERROR_TYPES, _HEALTH_ISSUE_LABELS
        self.assertIn('runtime_unavailable', _HEALTH_ERROR_TYPES)
        self.assertIn('runtime_health_unknown', _HEALTH_ERROR_TYPES)
        self.assertIn('runtime_unavailable', _HEALTH_ISSUE_LABELS)
        self.assertIn('runtime_health_unknown', _HEALTH_ISSUE_LABELS)


class ObserveClawRuntimeReadinessTests(SimpleTestCase):

    def test_empty_namespace_is_unknown(self):
        self.assertEqual(_observe_claw_runtime_readiness(''), 'unknown')

    def test_ready_and_unavailable_from_deployment_status(self):
        from types import SimpleNamespace
        from kubernetes.client.rest import ApiException

        apps = type('A', (), {})()
        apps.read_namespaced_deployment = lambda *a, **k: SimpleNamespace(
            status=SimpleNamespace(ready_replicas=1),
        )
        with patch('claws.provisioner.get_k8s_client', return_value=(None, apps)):
            self.assertEqual(_observe_claw_runtime_readiness('claw-1-abc'), 'ready')

        apps.read_namespaced_deployment = lambda *a, **k: SimpleNamespace(
            status=SimpleNamespace(ready_replicas=0),
        )
        with patch('claws.provisioner.get_k8s_client', return_value=(None, apps)):
            self.assertEqual(_observe_claw_runtime_readiness('claw-1-abc'), 'unavailable')

        def _raise(*a, **k):
            raise ApiException(status=404)

        apps.read_namespaced_deployment = _raise
        with patch('claws.provisioner.get_k8s_client', return_value=(None, apps)):
            self.assertEqual(_observe_claw_runtime_readiness('claw-1-abc'), 'unavailable')
