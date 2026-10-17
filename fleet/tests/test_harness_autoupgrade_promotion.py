"""Runtime builds are dispatch-only; Hive cannot author release intent."""
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from fleet import harness_versions as hv
from fleet.models import DaemonRegistry


LEGACY_KEYS = (
    'fleet:desired-runtime-image',
    'fleet:queued-runtime-image',
    'fleet:deferred-native-runtime-image',
    'harness-autoupgrade:pending',
)


class HarnessAutoupgradeDispatchOnlyTests(TestCase):
    reviewed_image = 'localhost:30500/shizuha-agent-runtime:harness-reviewed'

    def setUp(self):
        cache.clear()
        self.release = {
            'generation': 8,
            'image_digest': 'sha256:' + ('a' * 64),
            'display_tag': self.reviewed_image,
            'source_commit': 'b' * 40,
            'approved_at': '2026-08-12T10:00:00Z',
            'intent': 'promote',
            'rollback_of_generation': None,
        }

    def tearDown(self):
        cache.clear()
        super().tearDown()

    def _daemon(self, *, last_seen=None, image=None, labels=None):
        return DaemonRegistry.objects.create(
            daemon_id='rt-fleet-s1',
            fleet_id='shizuha-fleet',
            runtime='k8s',
            status=DaemonRegistry.Status.CONNECTED,
            last_seen=last_seen or timezone.now(),
            agent_runtime_image=image or self.reviewed_image,
            harness_report={
                'agent_runtime_image': image or self.reviewed_image,
                'image_harness': labels or {'codex': '0.1.0'},
                'desired_runtime_release': self.release,
            },
        )

    @mock.patch(
        'fleet.management.commands.harness_autoupgrade._pending_build_failed',
        return_value=False,
    )
    @mock.patch('fleet.harness_versions.runtime_image_tag_exists', return_value=False)
    @mock.patch(
        'fleet.harness_versions.dispatch_agent_runtime_build',
        return_value={
            'dispatched': True,
            'tag': 'harness-202608121200',
            'image': 'localhost:30500/shizuha-agent-runtime:harness-202608121200',
        },
    )
    @mock.patch('fleet.harness_versions.npm_latest', return_value='0.2.0')
    def test_two_ticks_dispatch_once_and_never_touch_legacy_desired_state(
        self, _latest, dispatch, _exists, _failed,
    ):
        self._daemon()
        for key in LEGACY_KEYS:
            cache.set(key, {'sentinel': key}, None)

        first = StringIO()
        second = StringIO()
        call_command('harness_autoupgrade', '--harness', 'codex', stdout=first)
        call_command('harness_autoupgrade', '--harness', 'codex', stdout=second)

        dispatch.assert_called_once_with({'codex': '0.2.0'})
        self.assertIn('reviewed DesiredRuntimeRelease adoption is required', first.getvalue())
        self.assertIn('already dispatched this target set', second.getvalue())
        for key in LEGACY_KEYS:
            self.assertEqual(cache.get(key), {'sentinel': key})

    @mock.patch('fleet.harness_versions.dispatch_agent_runtime_build')
    def test_stale_or_missing_reviewed_projection_fails_closed(self, dispatch):
        self._daemon(last_seen=timezone.now() - timedelta(seconds=31))

        out = StringIO()
        call_command('harness_autoupgrade', '--harness', 'codex', stdout=out)

        dispatch.assert_not_called()
        self.assertIn('reviewed runtime release unavailable', out.getvalue())

    @mock.patch('fleet.harness_versions.dispatch_agent_runtime_build')
    def test_unavailable_projection_does_not_consume_detection_cadence(self, dispatch):
        self._daemon(last_seen=timezone.now() - timedelta(seconds=31))

        call_command('harness_autoupgrade', stdout=StringIO())

        dispatch.assert_not_called()
        self.assertIsNone(cache.get('harness-autoupgrade:version-detection-cadence'))

    @mock.patch('fleet.harness_versions.image_harness_labels', return_value={})
    @mock.patch('fleet.harness_versions.dispatch_agent_runtime_build')
    def test_mismatched_observation_without_reviewed_image_labels_fails_closed(
        self, dispatch, _labels,
    ):
        self._daemon(
            image='localhost:30500/shizuha-agent-runtime:harness-observed-old',
            labels={'codex': '0.1.0'},
        )

        out = StringIO()
        call_command('harness_autoupgrade', '--harness', 'codex', stdout=out)

        dispatch.assert_not_called()
        self.assertIn('reviewed runtime release labels unavailable', out.getvalue())

    @mock.patch('fleet.harness_versions.dispatch_agent_runtime_build')
    def test_force_is_dispatch_only_and_dry_run_is_mutation_free(self, dispatch):
        for key in LEGACY_KEYS:
            cache.set(key, key, None)
        out = StringIO()

        call_command('harness_autoupgrade', '--force', '--dry-run', stdout=out)

        dispatch.assert_not_called()
        for key in LEGACY_KEYS:
            self.assertEqual(cache.get(key), key)

    def test_release_keeps_dispatch_controller_on_one_minute_cadence(self):
        release = Path('.forgejo/scripts/ring-release-fleet.sh').read_text()
        block = release.split('Installing harness auto-upgrade CronJob', 1)[1].split(
            'LIVE_HIVE_IMAGE=', 1,
        )[0]
        self.assertIn('"schedule": "* * * * *"', block)


class MutablePromotionTombstoneTests(TestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()
        super().tearDown()

    def test_management_command_cannot_promote(self):
        cache.set('fleet:desired-runtime-image', 'sentinel', None)

        with self.assertRaisesMessage(CommandError, 'mutable Hive runtime promotion is disabled'):
            call_command(
                'promote_runtime_image',
                '--image',
                'localhost:30500/shizuha-agent-runtime:harness-candidate',
            )

        self.assertEqual(cache.get('fleet:desired-runtime-image'), 'sentinel')

    def test_legacy_mutable_helpers_no_longer_exist(self):
        names = (
            'sync_native_runtime_image',
            'reconcile_deferred_native_runtime_image',
            'record_managed_k8s_runtime_image',
            'stage_stopped_native_runtime_image',
            'set_desired_runtime_image',
            'set_managed_runtime_roll_target',
            'promote_runtime_image_automatically',
            'promote_queued_runtime_image_if_converged',
            'refresh_daemon_report_after_k8s_convergence',
        )
        for name in names:
            with self.subTest(name=name):
                self.assertFalse(hasattr(hv, name))


class RuntimeBuildDispatchTests(TestCase):
    @mock.patch.dict('os.environ', {'HIVE_FORGEJO_TOKEN': 'test-token'}, clear=False)
    # PLAT-9683 leg 4: this test's subject is the post-dispatch projection
    # (no pending promotion), not guard behavior — stub the dispatch-time
    # workflow-YAML guard so the success path is exercised deterministically
    # (its fetch lives in fleet.workflow_dispatch_guard, outside this patch
    # scope).
    @mock.patch(
        'fleet.harness_versions.workflow_dispatch_guard.validate_workflow_dispatch_yaml',
        return_value={'ok': True},
    )
    @mock.patch('fleet.harness_versions.requests.post')
    def test_successful_dispatch_does_not_create_pending_promotion(self, post, _guard):
        post.return_value.status_code = 204
        cache.delete('harness-autoupgrade:pending')
        versions = {
            'codex': '0.2.0',
            'claude_code': '2.0.0',
            'antigravity': '1.0.0',
            'openclaw': '3.0.0',
        }

        result = hv.dispatch_agent_runtime_build(versions)

        self.assertTrue(result['dispatched'])
        self.assertIsNone(cache.get('harness-autoupgrade:pending'))
        self.assertNotIn('fleet:desired-runtime-image', result)
