from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from hive_platform.models import TenantSubscription
from fleet.company_state import HEALTH_FAMILIES
from fleet.views import FleetCompanyStateView


class FleetCompanyStateViewTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.url = "/api/internal/company-state/fleet/v1/?org_slug=shizuha"
        self.headers = {"HTTP_AUTHORIZATION": "Bearer dedicated-read-token"}

    def _get(self, url=None, **headers):
        request = self.factory.get(url or self.url, **(headers or self.headers))
        with patch.dict(
            "os.environ",
            {
                "COMPANY_STATE_READ_TOKEN": "dedicated-read-token",
                "COMPANY_STATE_INFRA_ORG_SLUG": "shizuha",
            },
        ):
            return FleetCompanyStateView.as_view()(request)

    def test_dedicated_token_allows_and_missing_or_wrong_token_denies(self):
        with patch("fleet.lifecycle_fleet.list_lifecycle_agents", return_value=[]), patch(
            "fleet.external_agents.agent_telemetry", return_value={}
        ):
            self.assertEqual(self._get().status_code, 200)
        self.assertEqual(self._get(HTTP_AUTHORIZATION="Bearer wrong").status_code, 403)
        self.assertEqual(self._get(HTTP_AUTHORIZATION="").status_code, 403)

    def test_wrong_and_second_org_return_not_configured_without_fleet_data(self):
        for slug in ("", "customer-two"):
            response = self._get(
                f"/api/internal/company-state/fleet/v1/?org_slug={slug}"
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data["status"], "not_configured")
            self.assertNotIn("agents", response.data)
            self.assertNotIn("fleet", response.data)
            self.assertNotIn("tenants", response.data)

    def test_successful_empty_tenant_source_is_real_zero(self):
        with patch("fleet.lifecycle_fleet.list_lifecycle_agents", return_value=[]), patch(
            "fleet.external_agents.agent_telemetry", return_value={}
        ):
            response = self._get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["tenants"]["total"], 0)
        self.assertEqual(response.data["tenants"]["by_status"]["active"], 0)

    def test_tenant_counts_are_distinct_anonymized_and_grouped_by_status(self):
        TenantSubscription.objects.create(
            tenant_org_id="1", tenant_org_slug="alpha", status="active"
        )
        TenantSubscription.objects.create(
            tenant_org_id="2", tenant_org_slug="alpha", status="trial"
        )
        TenantSubscription.objects.create(
            tenant_org_id="3", tenant_org_slug="beta", status="suspended"
        )
        with patch("fleet.lifecycle_fleet.list_lifecycle_agents", return_value=[]), patch(
            "fleet.external_agents.agent_telemetry", return_value={}
        ):
            response = self._get()
        tenants = response.data["tenants"]
        self.assertEqual(tenants["total"], 2)
        self.assertEqual(tenants["active_or_trial"], 1)
        self.assertEqual(tenants["by_status"]["active"], 1)
        self.assertEqual(tenants["by_status"]["trial"], 1)
        self.assertNotContains(response, "alpha")
        self.assertNotContains(response, "beta")

    def test_tenant_source_failure_is_not_silent_zero(self):
        with patch("fleet.lifecycle_fleet.list_lifecycle_agents", return_value=[]), patch(
            "fleet.external_agents.agent_telemetry", return_value={}
        ), patch(
            "hive_platform.models.TenantSubscription.objects.exclude",
            side_effect=RuntimeError("db unavailable"),
        ):
            response = self._get()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["status"], "source_unavailable")
        self.assertEqual(response.data["source"], "tenant_subscriptions")
        self.assertNotIn("tenants", response.data)

    def test_every_agent_has_every_family_and_absent_or_stale_values_are_unknown(self):
        old = (timezone.now() - timedelta(hours=1)).timestamp()
        roster = [
            {"agent_username": "native", "enabled": True, "status": "running", "lifecycle_plane": "native"},
            {"agent_username": "host", "enabled": False, "status": "stopped", "lifecycle_plane": "runtime"},
        ]
        with patch("fleet.lifecycle_fleet.list_lifecycle_agents", return_value=roster), patch(
            "fleet.external_agents.agent_telemetry",
            return_value={"native": {"received_at": old, "health": {"provider_healthy": True}}},
        ):
            response = self._get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["fleet"], {"enabled": 1, "running": 1, "stopped": 1})
        self.assertEqual(len(response.data["agents"]), 2)
        by_name = {agent["username"]: agent for agent in response.data["agents"]}
        self.assertEqual(by_name["native"]["lifecycle_status"], "running")
        self.assertTrue(by_name["native"]["enabled"])
        self.assertEqual(by_name["host"]["lifecycle_status"], "stopped")
        self.assertFalse(by_name["host"]["enabled"])
        for agent in response.data["agents"]:
            self.assertTrue(agent["email"].endswith("@shizuha.com"))
            for family in HEALTH_FAMILIES:
                self.assertIn(family, agent)
                self.assertFalse(agent[family]["known"])
                self.assertIsNone(agent[family]["value"])

    def test_fresh_connect_sample_wins_and_readiness_never_implies_health(self):
        now = timezone.now().timestamp()
        roster = [
            {
                "agent_username": "native",
                "enabled": True,
                "status": "running",
                "lifecycle_plane": "native",
                "k3s_ready_replicas": 1,
            }
        ]
        telemetry = {
            "native": {
                "received_at": now,
                "health": {
                    "provider_healthy": False,
                    "auth_failed": True,
                    "bridge_auth_unavailable": False,
                    "mcp_errors": 2,
                    "wedge_errors": 3,
                    "empty_turn_wedge": True,
                    "pulse_wedge": False,
                    "capacity_unavailable": True,
                },
            }
        }
        with patch("fleet.lifecycle_fleet.list_lifecycle_agents", return_value=roster), patch(
            "fleet.external_agents.agent_telemetry", return_value=telemetry
        ):
            response = self._get()
        agent = response.data["agents"][0]
        self.assertFalse(agent["provider_healthy"]["value"])
        self.assertTrue(agent["auth_failed"]["value"])
        self.assertEqual(agent["mcp_errors"]["value"], 2)
        self.assertEqual(agent["wedge_errors"]["value"], 3)
        for family in HEALTH_FAMILIES:
            self.assertTrue(agent[family]["known"])
            self.assertEqual(agent[family]["source"], f"connect.telemetry.health.{family}")

        with patch("fleet.lifecycle_fleet.list_lifecycle_agents", return_value=roster), patch(
            "fleet.external_agents.agent_telemetry", return_value={}
        ):
            no_telemetry = self._get().data["agents"][0]
        for family in HEALTH_FAMILIES:
            self.assertFalse(no_telemetry[family]["known"])
            self.assertIsNone(no_telemetry[family]["value"])

    def test_newer_connect_sample_wins_over_older_host_exporter_sample(self):
        now = timezone.now()
        roster = [
            {
                "agent_username": "host",
                "enabled": True,
                "status": "running",
                "lifecycle_plane": "runtime",
            }
        ]
        old_host = SimpleNamespace(
            received_at=now - timedelta(minutes=1),
            provider_health={"available": True, "quota_ok": True, "in_backoff": False},
        )
        telemetry = {
            "host": {
                "received_at": now.timestamp(),
                "health": {"provider_healthy": False},
            }
        }
        with patch("fleet.lifecycle_fleet.list_lifecycle_agents", return_value=roster), patch(
            "fleet.external_agents.agent_telemetry", return_value=telemetry
        ), patch("fleet.company_state._latest_host_snapshots", return_value={"host": old_host}):
            response = self._get()
        provider = response.data["agents"][0]["provider_healthy"]
        self.assertTrue(provider["known"])
        self.assertFalse(provider["value"])
        self.assertEqual(provider["source"], "connect.telemetry.health.provider_healthy")
