"""PLAT-9202 leg 1: FleetRecoveryActionMetricsView accepts the internal bearer.

The hive_wake_deferred_* families (hive#323) and the HIVE-277 liveness
metrics are served by this view; it was staff-gated, so the Prometheus
scrape (PodMonitor bearer from hive-secrets/internal-auth-token) got 403
and the paging rules saw no series. House pattern: HIVE-614
(claws ConnectDeliveryMetricsView) — Bearer/X-Hive-Internal-Token against
HIVE_INTERNAL_AUTH_TOKEN, hmac.compare_digest, fail closed when unset.
Staff access is unchanged.
"""
from django.test import TestCase, override_settings
from django.urls import reverse

from fleet.models import FleetAgent


@override_settings(HIVE_INTERNAL_AUTH_TOKEN='recovery-metrics-test-token')
class RecoveryActionMetricsBearerTests(TestCase):
    def test_internal_bearer_is_admitted(self):
        response = self.client.get(
            reverse('fleet-recovery-action-metrics'),
            HTTP_AUTHORIZATION='Bearer recovery-metrics-test-token',
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        # The families main's view serves today (HIVE-277 liveness). The
        # hive_wake_deferred_* families join the same view when hive#323
        # lands — this leg's contract is that the AUTHENTICATED scrape
        # reaches the exposition at all (it 403'd before).
        self.assertIn('hive_recovery_actions_total', body)
        self.assertIn('hive_runtime_lane_outbox_pending', body)
        self.assertIn('hive_harness_upgrade_failure', body)

    def test_x_hive_internal_token_header_is_admitted(self):
        response = self.client.get(
            reverse('fleet-recovery-action-metrics'),
            HTTP_X_HIVE_INTERNAL_TOKEN='recovery-metrics-test-token',
        )
        self.assertEqual(response.status_code, 200)

    def test_wrong_bearer_is_rejected(self):
        response = self.client.get(
            reverse('fleet-recovery-action-metrics'),
            HTTP_AUTHORIZATION='Bearer wrong-token',
        )
        self.assertEqual(response.status_code, 403)

    def test_missing_bearer_is_rejected(self):
        response = self.client.get(reverse('fleet-recovery-action-metrics'))
        self.assertEqual(response.status_code, 403)

    def test_fails_closed_when_token_unconfigured(self):
        from django.test import Client, override_settings as _ovr
        with _ovr(HIVE_INTERNAL_AUTH_TOKEN=''):
            response = Client().get(
                reverse('fleet-recovery-action-metrics'),
                HTTP_AUTHORIZATION='Bearer recovery-metrics-test-token',
            )
        self.assertEqual(response.status_code, 403)

    def test_staff_access_unchanged(self):
        # Production staff access is federated-JWT-claim based (the DRF
        # default authenticator populates request.user from JWT claims —
        # there is no Django-session auth on /hive/api/). Unit-test the
        # view's staff path by stubbing the manual federated-JWT validation
        # the view performs.
        from unittest.mock import patch

        from django.contrib.auth.models import User

        staff = User.objects.create_user('staff-viewer', password='x', is_staff=True)
        with patch(
            'claws.authentication.FederatedJWTAuthentication.authenticate',
            return_value=(staff, None),
        ):
            response = self.client.get(reverse('fleet-recovery-action-metrics'))
        self.assertEqual(response.status_code, 200)
        self.assertIn('hive_recovery_actions_total', response.content.decode())

    def test_non_staff_federated_user_is_rejected(self):
        from unittest.mock import patch

        from django.contrib.auth.models import User

        viewer = User.objects.create_user('plain-viewer', password='x', is_staff=False)
        with patch(
            'claws.authentication.FederatedJWTAuthentication.authenticate',
            return_value=(viewer, None),
        ):
            response = self.client.get(reverse('fleet-recovery-action-metrics'))
        self.assertEqual(response.status_code, 403)

    def test_fleet_agent_liveness_series_still_served(self):
        # HIVE-277's recovery-action liveness metrics ride the same view and
        # are equally unblinded by this change.
        FleetAgent.objects.create(
            agent_username='bearer-probe', agent_id='bearer-probe-runtime',
            identity_user_id=901, display_name='Bearer Probe',
        )
        response = self.client.get(
            reverse('fleet-recovery-action-metrics'),
            HTTP_AUTHORIZATION='Bearer recovery-metrics-test-token',
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('hive_', response.content.decode())
