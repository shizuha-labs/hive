from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from fleet.compute_billing import (
    bill_hour,
    completed_hours,
    micros_for,
    parse_cpu_cores,
    parse_memory_gib,
    payer_user_id,
    resources_for,
)
from fleet.models import FleetAgent


class ResourceParseTests(SimpleTestCase):
    def test_cpu_and_memory_units(self):
        self.assertEqual(parse_cpu_cores('250m'), Decimal('0.25'))
        self.assertEqual(parse_cpu_cores('2'), Decimal('2'))
        self.assertEqual(parse_memory_gib('512Mi'), Decimal('512') / Decimal('1024'))
        self.assertEqual(parse_memory_gib('1Gi'), Decimal('1'))

    def test_standard_profile_is_half_hane_per_hour(self):
        agent = FleetAgent(agent_username='san', resource_profile='standard')
        cpu, ram = resources_for(agent)
        self.assertEqual(cpu, Decimal('0.25'))
        self.assertEqual(ram, Decimal('0.5'))
        self.assertEqual(micros_for(cpu, ram), 500_000)

    def test_personal_agent_bills_owner_org_fleet_bills_platform(self):
        personal = FleetAgent(agent_username='shizuha-9', owner_id=9)
        org = FleetAgent(agent_username='san', owner_id=None)
        self.assertEqual(payer_user_id(personal), 9)
        self.assertEqual(payer_user_id(org), 3)

    def test_completed_hours_are_prior_clock_hours(self):
        now = datetime(2026, 8, 17, 16, 12, tzinfo=timezone.utc)
        hours = completed_hours(now, count=2)
        self.assertEqual(hours[0], datetime(2026, 8, 17, 14, 0, tzinfo=timezone.utc))
        self.assertEqual(hours[1], datetime(2026, 8, 17, 15, 0, tzinfo=timezone.utc))

    def test_naive_now_is_treated_as_utc_not_django_local(self):
        # Production Hive is USE_TZ=False TIME_ZONE=America/Chicago. A naive
        # 07:13 Chicago clock must not become the 07:00 UTC meter hour.
        naive = datetime(2026, 8, 17, 12, 13)
        hours = completed_hours(naive, count=1)
        self.assertEqual(hours[0], datetime(2026, 8, 17, 11, 0, tzinfo=timezone.utc))

    @patch('fleet.compute_billing.datetime')
    def test_default_now_uses_utc_wall_clock(self, dt_mod):
        dt_mod.now.return_value = datetime(2026, 8, 17, 12, 13, tzinfo=timezone.utc)
        hours = completed_hours(count=1)
        dt_mod.now.assert_called_with(timezone.utc)
        self.assertEqual(hours[0], datetime(2026, 8, 17, 11, 0, tzinfo=timezone.utc))


class BillHourTests(TestCase):
    def test_dry_run_counts_running_agents_only(self):
        FleetAgent.objects.create(
            agent_username='san', status=FleetAgent.Status.RUNNING, resource_profile='standard',
        )
        FleetAgent.objects.create(
            agent_username='nagi', status=FleetAgent.Status.HIBERNATED, resource_profile='standard',
        )
        hour = datetime(2026, 8, 17, 15, 0, tzinfo=timezone.utc)
        summary = bill_hour(hour, dry_run=True)
        self.assertEqual(summary['agents'], 1)
        self.assertEqual(summary['charged_micros'], 500_000)
        self.assertEqual(summary['errors'], 0)

    @patch('fleet.compute_billing._cortex_spend', return_value={'charged_micros': 500000, 'shortfall_micros': 0})
    def test_live_path_posts_once(self, spend):
        FleetAgent.objects.create(
            agent_username='kai', status=FleetAgent.Status.RUNNING, resource_profile='standard',
        )
        hour = datetime(2026, 8, 17, 15, 0, tzinfo=timezone.utc)
        summary = bill_hour(hour, dry_run=False)
        self.assertEqual(summary['charged_micros'], 500_000)
        self.assertEqual(spend.call_count, 1)
        kwargs = spend.call_args.kwargs
        self.assertEqual(kwargs['subject_user_id'], 3)
        self.assertTrue(kwargs['idempotency_key'].startswith('hive-compute:'))
