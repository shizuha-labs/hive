"""HIVE-615: transient Redis cache faults must not kill harness autoupgrade ticks.

The autoupgrade controller is dispatch-only: a successful build is an immutable
candidate and cannot become desired runtime state until a distinct review
appends a ``DesiredRuntimeRelease`` (see ``fleet/runtime_release.py``).  This
suite proves the HIVE-615 Redis-resilience layer (``fleet/cache_resilience.py``)
keeps that dispatch flow alive across transient cache timeouts:

- authoritative state (cadence gate, dispatch cooldown marker) uses the
  ``required_*`` helpers: bounded retry, then fail loud;
- acceleration-only state (OCI label / npm-latest caches) uses the ``optional_*``
  helpers: a cache fault is a miss and the authoritative source is consulted.
"""
from io import StringIO
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from redis.exceptions import TimeoutError as RedisTimeoutError

from fleet import cache_resilience
from fleet import harness_versions as hv
from fleet.management.commands.harness_autoupgrade import (
    VERSION_DETECTION_CADENCE_KEY,
)
from fleet.models import DaemonRegistry


class HarnessAutoupgradeCacheResilienceTests(TestCase):
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

    def _daemon(self, *, labels=None):
        return DaemonRegistry.objects.create(
            daemon_id='rt-fleet-s1',
            fleet_id='shizuha-fleet',
            runtime='k8s',
            status=DaemonRegistry.Status.CONNECTED,
            last_seen=timezone.now(),
            agent_runtime_image=self.reviewed_image,
            harness_report={
                'agent_runtime_image': self.reviewed_image,
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
    def test_two_ticks_survive_one_authoritative_cooldown_timeout(
        self, _latest, dispatch, _exists, _failed,
    ):
        """A transient required_get fault on the dispatch marker is retried.

        The first tick dispatches and writes the cooldown marker; the second
        tick reads it through a one-shot Redis timeout.  required_get retries
        the idempotent read, so the tick reports 'already dispatched' instead
        of aborting.
        """
        self._daemon()
        first = StringIO()
        call_command('harness_autoupgrade', '--harness', 'codex', stdout=first)
        self.assertIn('reviewed DesiredRuntimeRelease adoption is required', first.getvalue())

        marker = 'harness-autoupgrade:dispatched:codex=0.2.0'
        self.assertIsNotNone(cache.get(marker))
        real_get = cache.get
        fault = {'remaining': 1}

        def flaky_get(key, default=None, *args, **kwargs):
            if key == marker and fault['remaining']:
                fault['remaining'] -= 1
                raise RedisTimeoutError('transient cooldown read timeout')
            return real_get(key, default, *args, **kwargs)

        second = StringIO()
        with mock.patch('fleet.cache_resilience.cache.get', side_effect=flaky_get):
            call_command('harness_autoupgrade', '--harness', 'codex', stdout=second)

        self.assertEqual(fault['remaining'], 0)
        self.assertIn('already dispatched this target set', second.getvalue())
        dispatch.assert_called_once()

    @mock.patch('fleet.harness_versions.requests.Session')
    def test_transient_label_cache_timeout_falls_back_to_registry_then_warms(
        self, session,
    ):
        """An optional_get fault on the label cache is a miss; registry wins.

        ``image_harness_labels`` reads the acceleration-only label cache through
        ``optional_get``.  A transient Redis timeout is treated as a miss and
        the authoritative OCI registry is consulted; the result is then warmed.
        """
        image = 'localhost:30500/shizuha-agent-runtime:harness-cache-fallback'
        manifest = mock.Mock()
        manifest.raise_for_status.return_value = None
        manifest.json.return_value = {'config': {'digest': 'sha256:config'}}
        config = mock.Mock()
        config.raise_for_status.return_value = None
        config.json.return_value = {
            'config': {'Labels': {'org.shizuha.harness.codex': '0.146.0'}},
        }
        session.return_value.get.side_effect = [manifest, config]
        real_get = cache.get
        fault = {'remaining': 1}

        def flaky_get(key, default=None, *args, **kwargs):
            if key.startswith('harness:img-labels:') and fault['remaining']:
                fault['remaining'] -= 1
                raise RedisTimeoutError('transient label-cache read timeout')
            return real_get(key, default, *args, **kwargs)

        with mock.patch('fleet.cache_resilience.cache.get', side_effect=flaky_get):
            first = hv.image_harness_labels(image)
            second = hv.image_harness_labels(image)

        self.assertEqual(fault['remaining'], 0)
        self.assertEqual(first, {'codex': '0.146.0'})
        self.assertEqual(second, first)
        self.assertEqual(session.return_value.get.call_count, 2)

    @mock.patch('fleet.harness_versions.npm_latest', return_value='0.2.0')
    @mock.patch(
        'fleet.harness_versions.dispatch_agent_runtime_build',
        return_value={
            'dispatched': True,
            'tag': 'harness-202608121200',
            'image': 'localhost:30500/shizuha-agent-runtime:harness-202608121200',
        },
    )
    def test_persistent_authoritative_cache_failure_retries_then_fails_loud(
        self, dispatch, _latest,
    ):
        """A persistent required_get fault retries 3x then raises (no silent skip)."""
        self._daemon()
        marker = 'harness-autoupgrade:dispatched:codex=0.2.0'
        cache.set(marker, 'harness-202608121200', 6 * 3600)

        real_get = cache.get
        calls = {'n': 0}

        def always_fault(key, default=None, *args, **kwargs):
            if key == marker:
                calls['n'] += 1
                raise RedisTimeoutError('persistent cooldown read timeout')
            return real_get(key, default, *args, **kwargs)

        with mock.patch('fleet.cache_resilience.cache.get', side_effect=always_fault):
            with self.assertRaises(RedisTimeoutError):
                call_command('harness_autoupgrade', '--harness', 'codex', stdout=StringIO())

        self.assertEqual(calls['n'], cache_resilience.REQUIRED_CACHE_ATTEMPTS)
        dispatch.assert_not_called()

    def test_optional_get_returns_default_on_cache_fault(self):
        """optional_get never raises; a fault is a miss."""
        with mock.patch(
            'fleet.cache_resilience.cache.get',
            side_effect=RedisTimeoutError('down'),
        ):
            self.assertIsNone(cache_resilience.optional_get('harness:img-labels:any'))
            self.assertEqual(
                cache_resilience.optional_get('harness:img-labels:any', 'fallback'),
                'fallback',
            )
