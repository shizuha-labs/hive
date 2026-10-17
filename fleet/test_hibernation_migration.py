"""Regression coverage for the 0037 hibernation rollout-race repair."""

import importlib

from django.apps import apps
from django.test import TestCase
from django.utils import timezone

from .models import FleetAgent


repair_migration = importlib.import_module(
    "fleet.migrations.0038_repair_hibernation_rollout_race"
)


class HibernationRolloutRepairTests(TestCase):
    def test_repairs_only_exact_legacy_rollout_rows(self):
        legacy = FleetAgent.objects.create(
            agent_username="legacy-sleeper",
            status=FleetAgent.Status.STOPPED,
            hibernation_reason=repair_migration.LEGACY_HIBERNATION_REASON,
            hibernated_at=timezone.now(),
        )
        explicit = FleetAgent.objects.create(
            agent_username="explicit-stop",
            status=FleetAgent.Status.STOPPED,
            hibernation_reason="",
            hibernated_at=None,
        )
        unrelated = FleetAgent.objects.create(
            agent_username="other-marker",
            status=FleetAgent.Status.STOPPED,
            hibernation_reason="other automatic state",
            hibernated_at=timezone.now(),
        )

        repair_migration.repair_legacy_hibernation_rollout_race(apps, None)

        legacy.refresh_from_db()
        explicit.refresh_from_db()
        unrelated.refresh_from_db()
        self.assertEqual(legacy.status, FleetAgent.Status.HIBERNATED)
        self.assertEqual(explicit.status, FleetAgent.Status.STOPPED)
        self.assertEqual(unrelated.status, FleetAgent.Status.STOPPED)
