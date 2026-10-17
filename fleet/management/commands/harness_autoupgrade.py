"""Detect external harness upgrades and dispatch an Origin runtime build.

This controller is intentionally dispatch-only.  A successful build is merely
an immutable candidate; it cannot become desired runtime state until a distinct
review appends a ``DesiredRuntimeRelease`` in the rt-fleet source repository.
"""
from __future__ import annotations

import logging
import time

from fleet.cache_resilience import (
    required_add,
    required_delete,
    required_get,
    required_set,
)
from django.core.management.base import BaseCommand

from fleet import harness_versions as hv
from fleet.runtime_release import (
    RuntimeReleaseProjectionUnavailable,
    get_authoritative_runtime_release,
)


logger = logging.getLogger(__name__)

VERSION_DETECTION_CADENCE_KEY = 'harness-autoupgrade:version-detection-cadence'
VERSION_DETECTION_CADENCE_SECONDS = 15 * 60
DISPATCH_COOLDOWN_SECONDS = 6 * 3600


def _pending_build_failed(tag: str) -> bool:
    """Return true only when every observed multi-arch build job failed."""
    if not tag:
        return False
    try:
        from kubernetes import client
        from kubernetes.config import load_incluster_config

        try:
            load_incluster_config()
        except Exception:
            return False
        batch = client.BatchV1Api()
        failed = 0
        seen = 0
        for arch in ('amd64', 'arm64'):
            name = f'ci-build-agentrt-{tag}-{arch}'
            try:
                job = batch.read_namespaced_job(name=name, namespace='build')
            except Exception:
                continue
            seen += 1
            status = job.status
            if status and (status.failed or 0) > 0:
                failed += 1
            elif not status or (status.succeeded or 0) <= 0:
                return False
        return seen > 0 and failed == seen
    except Exception as exc:  # noqa: BLE001 - best-effort dispatch retry probe
        logger.info('pending-build failure probe skipped: %s', exc)
        return False


def _reviewed_release_labels(projection) -> dict:
    """Resolve labels for exactly the reviewed release, never a sibling row."""
    from fleet.native_runtime_release import NativeRuntimeRelease
    if isinstance(projection, NativeRuntimeRelease):
        return hv.image_harness_labels(projection.release.deployment_image)
    daemon = projection.daemon
    release_image = projection.release.display_tag
    report = daemon.harness_report if isinstance(daemon.harness_report, dict) else {}
    reported_image = str(
        report.get('agent_runtime_image') or daemon.agent_runtime_image or ''
    ).strip()
    labels = report.get('image_harness')
    if reported_image == release_image and isinstance(labels, dict) and labels:
        return dict(labels)
    return hv.image_harness_labels(release_image)


class Command(BaseCommand):
    help = 'Detect newer harness versions and dispatch an agent-runtime build.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='detect only; do not dispatch')
        parser.add_argument('--harness', default='', help='limit to one harness key')
        parser.add_argument(
            '--force',
            action='store_true',
            help='dispatch a source rebuild from current master without changing desired state',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        only = str(options.get('harness') or '').strip()

        if options.get('force'):
            if dry_run:
                self.stdout.write('[dry-run] would force-dispatch a build from current master')
                return
            result = hv.dispatch_agent_runtime_build({})
            if result.get('dispatched'):
                self.stdout.write(self.style.SUCCESS(
                    f'force-dispatched build {result["image"]}; '
                    'reviewed DesiredRuntimeRelease adoption is still required'
                ))
            else:
                self.stdout.write(self.style.WARNING(
                    f'force dispatch failed: {result.get("error")}'
                ))
            return

        try:
            from fleet.native_runtime_release import enabled, get_native_runtime_release
            projection = get_native_runtime_release() if enabled() else get_authoritative_runtime_release()
        except RuntimeReleaseProjectionUnavailable as exc:
            self.stdout.write(self.style.ERROR(
                f'reviewed runtime release unavailable; skipping dispatch: {exc}'
            ))
            return

        # Do not consume a detection slot while the reviewed-release authority
        # is unavailable: the next tick must be able to act as soon as the
        # projection becomes fresh and valid again.
        if not dry_run and not only and not required_add(
            VERSION_DETECTION_CADENCE_KEY,
            time.time(),
            VERSION_DETECTION_CADENCE_SECONDS,
        ):
            self.stdout.write('external version detection cadence-gated')
            return

        labels = _reviewed_release_labels(projection)
        if not labels:
            self.stdout.write(self.style.ERROR(
                'reviewed runtime release labels unavailable; skipping dispatch'
            ))
            return

        overrides = {}
        upgrades = []
        withheld = []
        # HIVE-615: --force is the operator's deliberate escape hatch, so it
        # bypasses the policy dial the same way it bypasses version detection.
        policy = 'minor' if options.get('force') else hv.autoupgrade_policy()
        for harness, (package, _label) in hv.HARNESS_KEYS.items():
            if only and harness != only:
                continue
            if harness == 'scli':
                continue
            if harness == 'antigravity':
                latest = hv.antigravity_latest()
                current = labels.get(harness) or labels.get('gemini')
            elif package:
                latest = hv.npm_latest(package)
                current = labels.get(harness)
            else:
                continue
            if not (current and latest and hv.upgrade_available(current, latest)):
                continue
            # Detection is deliberately unconditional; only DISPATCH is gated,
            # so a withheld upgrade still surfaces instead of going invisible.
            if not hv.autoupgrade_allowed(current, latest, policy):
                withheld.append((harness, current, latest, hv.bump_level(current, latest)))
                continue
            upgrades.append((harness, current, latest))
            overrides[harness] = latest

        if withheld:
            detail = ', '.join(f'{h}: {c} -> {l} ({lvl})' for h, c, l, lvl in withheld)
            self.stdout.write(self.style.WARNING(
                f'upgrades withheld by HARNESS_AUTOUPGRADE={policy}: {detail} '
                '— detected and reported, not dispatched; promote deliberately '
                'or raise the policy'))

        if not upgrades:
            if withheld:
                self.stdout.write(
                    'no auto-promotable upgrades under the active policy '
                    f'({len(withheld)} withheld)')
            else:
                self.stdout.write('all harnesses current — no upgrades')
            return
        self.stdout.write(
            'upgrades available: '
            + ', '.join(f'{harness}: {current} -> {latest}' for harness, current, latest in upgrades)
        )
        if dry_run:
            return

        marker = 'harness-autoupgrade:dispatched:' + ','.join(
            f'{harness}={latest}' for harness, _current, latest in sorted(upgrades)
        )
        dispatched_tag = str(required_get(marker) or '').strip()
        if dispatched_tag:
            if _pending_build_failed(dispatched_tag):
                self.stdout.write(self.style.WARNING(
                    f'dispatched build {dispatched_tag} failed; retrying dispatch'
                ))
                required_delete(marker)
            else:
                artifact_state = (
                    'published and awaiting reviewed release adoption'
                    if hv.runtime_image_tag_exists(dispatched_tag)
                    else 'still pending'
                )
                self.stdout.write(
                    f'already dispatched this target set ({artifact_state})'
                )
                return

        result = hv.dispatch_agent_runtime_build(overrides)
        if result.get('dispatched'):
            required_set(marker, result['tag'], DISPATCH_COOLDOWN_SECONDS)
            self.stdout.write(self.style.SUCCESS(
                f'dispatched build {result["image"]} for {[item[0] for item in upgrades]}; '
                'reviewed DesiredRuntimeRelease adoption is required'
            ))
        else:
            self.stdout.write(self.style.WARNING(
                f'dispatch failed: {result.get("error")}'
            ))
