import importlib

from django.apps import apps
from django.core.exceptions import ValidationError
from django.test import TestCase

from .models import FleetAgent


class GrokRuntimeLaneModelGuardTests(TestCase):
    def test_grok_build_rejects_non_grok_model_on_create(self):
        agent = FleetAgent(
            agent_username='invalid-grok-create',
            model='cortex/DeepSeek-V4-Flash',
            mcp_config={'execution_method': 'grok_build'},
        )

        with self.assertRaisesMessage(ValidationError, 'cannot run model'):
            agent.save()

    def test_grok_build_rejects_non_grok_model_on_update(self):
        agent = FleetAgent.objects.create(
            agent_username='valid-grok-update',
            model='cortex/grok-4.5',
            mcp_config={'execution_method': 'grok_build'},
        )

        agent.model = 'cortex/DeepSeek-V4-Flash'
        with self.assertRaisesMessage(ValidationError, 'cannot run model'):
            agent.save(update_fields=['model', 'updated_at'])

    def test_unrelated_status_write_does_not_revalidate_legacy_lane(self):
        agent = FleetAgent.objects.create(
            agent_username='legacy-lane',
            model='cortex/DeepSeek-V4-Flash',
            mcp_config={'execution_method': 'shizuha'},
        )
        FleetAgent.objects.filter(pk=agent.pk).update(
            mcp_config={'execution_method': 'grok_build'},
        )
        agent.refresh_from_db()

        agent.status = FleetAgent.Status.HIBERNATED
        agent.save(update_fields=['status', 'updated_at'])

        self.assertEqual(agent.status, FleetAgent.Status.HIBERNATED)

    def test_valid_grok_lane_is_persisted(self):
        agent = FleetAgent.objects.create(
            agent_username='valid-grok-create',
            model='cortex/grok-4.5',
            mcp_config={'execution_method': 'grok_build'},
        )

        self.assertEqual(agent.model, 'cortex/grok-4.5')

    def test_repair_migration_normalizes_only_invalid_grok_build_rows(self):
        invalid = FleetAgent.objects.create(
            agent_username='legacy-invalid-grok',
            model='cortex/DeepSeek-V4-Flash',
            mcp_config={'execution_method': 'shizuha'},
        )
        FleetAgent.objects.filter(pk=invalid.pk).update(
            mcp_config={'execution_method': 'grok_build'},
            pending_config={'model': 'cortex/DeepSeek-V4-Flash'},
        )
        deepseek = FleetAgent.objects.create(
            agent_username='valid-deepseek',
            model='cortex/DeepSeek-V4-Flash',
            mcp_config={'execution_method': 'shizuha'},
        )
        grok = FleetAgent.objects.create(
            agent_username='already-valid-grok',
            model='grok-4.5',
            mcp_config={'execution_method': 'grok_build'},
        )

        migration = importlib.import_module(
            'fleet.migrations.0039_repair_grok_build_model_lanes'
        )
        migration.repair_grok_build_model_lanes(apps, None)

        invalid.refresh_from_db()
        deepseek.refresh_from_db()
        grok.refresh_from_db()
        self.assertEqual(invalid.model, 'cortex/grok-4.5')
        self.assertEqual(invalid.pending_config['model'], 'cortex/grok-4.5')
        self.assertEqual(deepseek.model, 'cortex/DeepSeek-V4-Flash')
        self.assertEqual(grok.model, 'grok-4.5')
