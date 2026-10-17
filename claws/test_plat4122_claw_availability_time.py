import base64
from datetime import UTC, datetime, timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from claws.management.commands.reconcile_claw_availability import (
    MODEL_UNAVAILABLE_PREFIX,
    _kubernetes_timestamp,
)
from claws.models import Claw
from claws.provisioner import CortexModelAccess, TierSpec


class KubernetesTimestampTests(SimpleTestCase):
    def test_naive_kubernetes_timestamp_is_interpreted_as_utc(self):
        raw = datetime(2026, 7, 26, 1, 0, 0)

        normalized = _kubernetes_timestamp(raw)

        self.assertTrue(timezone.is_aware(normalized))
        self.assertEqual(normalized.tzinfo, UTC)
        self.assertEqual(normalized, datetime(2026, 7, 26, 1, 0, 0, tzinfo=UTC))

    def test_aware_or_rfc3339_timestamp_preserves_the_instant(self):
        aware = datetime(2026, 7, 26, 1, 0, 0, tzinfo=UTC)

        self.assertIs(_kubernetes_timestamp(aware), aware)
        self.assertEqual(
            _kubernetes_timestamp("2026-07-26T01:00:00Z"),
            aware,
        )

    def test_missing_timestamp_stays_missing(self):
        self.assertIsNone(_kubernetes_timestamp(None))


class ClawModelAvailabilityReconcileTests(TestCase):
    def test_running_claw_surfaces_credential_model_denial(self):
        claw = Claw.objects.create(
            owner_id=42,
            owner_username='owner',
            owner_email='owner@example.com',
            name='Denied Claw',
            namespace='claw-42-denied',
            status=Claw.Status.RUNNING,
            tier_id='free',
            model='cortex/DeepSeek-V4-Flash',
        )
        core = MagicMock()
        core.read_namespaced_secret.return_value = SimpleNamespace(data={
            'cortex-api-key': base64.b64encode(b'sk-resold').decode(),
            'cortex-model': base64.b64encode(b'cortex/DeepSeek-V4-Flash').decode(),
        })
        apps = MagicMock()
        resources = SimpleNamespace(
            requests={'cpu': '100m', 'memory': '256Mi'},
            limits={'cpu': '1', 'memory': '2Gi'},
        )
        apps.read_namespaced_deployment.return_value = SimpleNamespace(
            spec=SimpleNamespace(
                replicas=1,
                template=SimpleNamespace(
                    metadata=SimpleNamespace(annotations={}),
                    spec=SimpleNamespace(containers=[
                        SimpleNamespace(name='claw', resources=resources),
                    ]),
                ),
            ),
            status=SimpleNamespace(ready_replicas=1, conditions=[]),
        )
        tier = TierSpec('5Gi', '100m', '1', '256Mi', '2Gi', 30, 100, 'hive-llm', 'DeepSeek-V4-Flash', 1)
        out = StringIO()
        with patch(
            'claws.management.commands.reconcile_claw_availability.get_k8s_client',
            return_value=(core, apps),
        ), patch(
            'claws.management.commands.reconcile_claw_availability.resolve_tier',
            return_value=tier,
        ), patch(
            'claws.management.commands.reconcile_claw_availability.probe_cortex_model_access',
            return_value=CortexModelAccess('unavailable', 'catalog authorization HTTP 403'),
        ):
            call_command('reconcile_claw_availability', stdout=out)

        claw.refresh_from_db()
        self.assertTrue(claw.status_message.startswith(MODEL_UNAVAILABLE_PREFIX))
        self.assertTrue(claw.events.filter(event_type='model_unavailable').exists())
        self.assertIn('model_unavailable=1', out.getvalue())

    def test_entitled_desired_model_repairs_stale_secret_and_rolls_once(self):
        claw = Claw.objects.create(
            owner_id=43,
            owner_username='internal-owner',
            owner_email='internal@example.com',
            name='Stale Model Claw',
            namespace='claw-43-stale',
            status=Claw.Status.RUNNING,
            tier_id='free',
            model='cortex/DeepSeek-V4-Flash',
            status_message=f'{MODEL_UNAVAILABLE_PREFIX} old denial',
        )
        core = MagicMock()
        core.read_namespaced_secret.return_value = SimpleNamespace(data={
            'cortex-api-key': base64.b64encode(b'sk-internal').decode(),
            'cortex-model': base64.b64encode(b'Qwen3.6-35B-A3B').decode(),
        })
        apps = MagicMock()
        resources = SimpleNamespace(
            requests={'cpu': '100m', 'memory': '256Mi'},
            limits={'cpu': '1', 'memory': '2Gi'},
        )
        apps.read_namespaced_deployment.return_value = SimpleNamespace(
            spec=SimpleNamespace(
                replicas=1,
                template=SimpleNamespace(
                    metadata=SimpleNamespace(annotations={}),
                    spec=SimpleNamespace(containers=[
                        SimpleNamespace(name='claw', resources=resources),
                    ]),
                ),
            ),
            status=SimpleNamespace(ready_replicas=1, conditions=[]),
        )
        tier = TierSpec('5Gi', '100m', '1', '256Mi', '2Gi', 30, 100, 'hive-llm', 'DeepSeek-V4-Flash', 1)
        out = StringIO()
        with patch(
            'claws.management.commands.reconcile_claw_availability.get_k8s_client',
            return_value=(core, apps),
        ), patch(
            'claws.management.commands.reconcile_claw_availability.resolve_tier',
            return_value=tier,
        ), patch(
            'claws.management.commands.reconcile_claw_availability.probe_cortex_model_access',
            return_value=CortexModelAccess('available'),
        ) as probe:
            call_command('reconcile_claw_availability', stdout=out)

        probe.assert_called_once_with('sk-internal', 'cortex/DeepSeek-V4-Flash')
        patched = core.patch_namespaced_secret.call_args.args[2]
        self.assertEqual(
            base64.b64decode(patched['data']['cortex-model']).decode(),
            'cortex/DeepSeek-V4-Flash',
        )
        apps.patch_namespaced_deployment.assert_called_once()
        claw.refresh_from_db()
        self.assertEqual(claw.status_message, '')
        self.assertTrue(claw.events.filter(event_type='model_reconciled').exists())
        self.assertIn('model_reconciled=1', out.getvalue())

    def test_unentitled_desired_model_never_mutates_stale_secret(self):
        claw = Claw.objects.create(
            owner_id=44,
            owner_username='customer-owner',
            owner_email='customer@example.com',
            name='Unentitled Claw',
            namespace='claw-44-unentitled',
            status=Claw.Status.RUNNING,
            tier_id='free',
            model='cortex/DeepSeek-V4-Flash',
        )
        core = MagicMock()
        core.read_namespaced_secret.return_value = SimpleNamespace(data={
            'cortex-api-key': base64.b64encode(b'sk-customer').decode(),
            'cortex-model': base64.b64encode(b'Qwen3.6-35B-A3B').decode(),
        })
        apps = MagicMock()
        resources = SimpleNamespace(
            requests={'cpu': '100m', 'memory': '256Mi'},
            limits={'cpu': '1', 'memory': '2Gi'},
        )
        apps.read_namespaced_deployment.return_value = SimpleNamespace(
            spec=SimpleNamespace(
                replicas=1,
                template=SimpleNamespace(spec=SimpleNamespace(containers=[
                    SimpleNamespace(name='claw', resources=resources),
                ])),
            ),
            status=SimpleNamespace(ready_replicas=1, conditions=[]),
        )
        tier = TierSpec('5Gi', '100m', '1', '256Mi', '2Gi', 30, 100, 'hive-llm', 'DeepSeek-V4-Flash', 1)
        with patch(
            'claws.management.commands.reconcile_claw_availability.get_k8s_client',
            return_value=(core, apps),
        ), patch(
            'claws.management.commands.reconcile_claw_availability.resolve_tier',
            return_value=tier,
        ), patch(
            'claws.management.commands.reconcile_claw_availability.probe_cortex_model_access',
            return_value=CortexModelAccess('unavailable', 'absent from credential catalog'),
        ) as probe:
            call_command('reconcile_claw_availability')
            claw.events.filter(event_type='model_unavailable').update(
                created_at=timezone.now() - timedelta(hours=1),
            )
            call_command('reconcile_claw_availability')

        self.assertEqual(probe.call_count, 2)  # exactly once per controller cycle
        probe.assert_called_with('sk-customer', 'cortex/DeepSeek-V4-Flash')
        core.patch_namespaced_secret.assert_not_called()
        apps.patch_namespaced_deployment.assert_not_called()
        claw.refresh_from_db()
        self.assertIn('DeepSeek-V4-Flash', claw.status_message)
        self.assertNotIn('Qwen3.6', claw.status_message)
        self.assertEqual(claw.events.filter(event_type='model_unavailable').count(), 1)
