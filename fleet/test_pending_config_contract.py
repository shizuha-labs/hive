"""Regression coverage for the FleetAgent.pending_config object invariant."""

import importlib

from django.apps import apps
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from .models import DaemonRegistry, FleetAgent
from .pending_config import pending_config_or_empty


repair_migration = importlib.import_module(
    "fleet.migrations.0047_repair_pending_config_objects"
)


class PendingConfigValueContractTests(SimpleTestCase):
    def test_defensive_reader_rejects_every_non_object_json_shape(self):
        for value in (True, False, [], ["model"], "model", 1, 1.5, None):
            with self.subTest(value=value):
                with self.assertLogs("fleet.pending_config", level="ERROR"):
                    self.assertEqual(
                        pending_config_or_empty(
                            value,
                            agent_identifier="legacy-agent",
                            boundary="test",
                        ),
                        {},
                    )


class PendingConfigPersistenceContractTests(TestCase):
    def test_model_write_rejects_non_object(self):
        agent = FleetAgent(agent_username="invalid-pending", pending_config=True)
        with self.assertRaisesMessage(
            ValidationError,
            "pending_config must be a JSON object",
        ):
            agent.save()

    def test_runtime_serializer_treats_legacy_scalar_as_empty_and_logs(self):
        from .runtime_fleet import _fleet_agent_row_to_runtime

        agent = FleetAgent.objects.create(agent_username="legacy-serializer")
        FleetAgent.objects.filter(pk=agent.pk).update(pending_config=True)
        agent.refresh_from_db()

        with self.assertLogs("fleet.pending_config", level="ERROR") as captured:
            serialized = _fleet_agent_row_to_runtime(agent)

        self.assertEqual(serialized["pending_config_keys"], [])
        self.assertIn("boundary=runtime_serializer", "\n".join(captured.output))

    def test_runtime_writer_replaces_legacy_scalar_with_mapping(self):
        from .runtime_fleet import _configure_runtime_agent_via_daemon_link

        daemon = DaemonRegistry.objects.create(
            daemon_id="pending-writer-daemon",
            fleet_id="pending-writer-fleet",
            status=DaemonRegistry.Status.DISCONNECTED,
        )
        agent = FleetAgent.objects.create(
            agent_username="legacy-writer",
            agent_id="legacy-writer-id",
            managing_daemon=daemon,
        )
        FleetAgent.objects.filter(pk=agent.pk).update(pending_config=True)

        with self.assertLogs("fleet.pending_config", level="ERROR"):
            ok, detail = _configure_runtime_agent_via_daemon_link(
                agent.agent_id,
                {"skills": ["pulse-core"]},
            )

        self.assertTrue(ok, detail)
        agent.refresh_from_db()
        self.assertEqual(agent.pending_config, {"skills": ["pulse-core"]})

    def test_repair_migration_replaces_all_non_objects_and_preserves_objects(self):
        rows = []
        for index, value in enumerate((True, False, [], ["model"], "model", 1, 1.5)):
            row = FleetAgent.objects.create(agent_username=f"legacy-pending-{index}")
            FleetAgent.objects.filter(pk=row.pk).update(pending_config=value)
            rows.append(row)
        valid = FleetAgent.objects.create(
            agent_username="valid-pending",
            pending_config={"model": "gpt-5.6-sol"},
        )

        repair_migration.repair_pending_config_objects(apps, None)

        self.assertEqual(
            list(
                FleetAgent.objects.filter(pk__in=[row.pk for row in rows])
                .values_list("pending_config", flat=True)
            ),
            [{}] * len(rows),
        )
        valid.refresh_from_db()
        self.assertEqual(valid.pending_config, {"model": "gpt-5.6-sol"})
