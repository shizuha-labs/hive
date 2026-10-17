"""Read-only projection of rt-fleet's reviewed runtime release.

Hive is a consumer of ``DesiredRuntimeRelease``.  It must never synthesize,
cache, or advance that desired state: the exact shared rt-fleet daemon reports
the reviewed source record and Hive fails closed when that projection is stale
or malformed.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import timedelta

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import DaemonRegistry


_DIGEST_RE = re.compile(r'^sha256:[0-9a-f]{64}$')
_COMMIT_RE = re.compile(r'^[0-9a-f]{40}$')
_VALID_INTENTS = frozenset({'promote', 'rollback'})


class RuntimeReleaseProjectionUnavailable(RuntimeError):
    """The authoritative reviewed-release projection cannot safely be used."""


@dataclass(frozen=True)
class RuntimeReleaseProjection:
    generation: int
    image_digest: str
    display_tag: str
    source_commit: str
    approved_at: str
    intent: str
    rollback_of_generation: int | None

    @property
    def deployment_image(self) -> str:
        """Canonical immutable OCI reference used by every actuation path."""
        repository, _tag = self.display_tag.rsplit(':', 1)
        return f'{repository}@{self.image_digest}'

    def matches_observed_image(self, image_ref: str) -> bool:
        """True when a daemon-reported image is this reviewed release.

        rt-fleet reports the immutable ``repository@sha256:…`` form. Hive's
        display tag is ``repository:harness-…``. String inequality of those
        two is not a rollout: they name the same digest (Agents page 2026-08-26:
        banner stuck on "runtime rolling" while the fleet already ran
        ``@sha256:8878023b…`` = ``harness-202608181052-02c595e``).
        """
        ref = str(image_ref or '').strip()
        if not ref:
            return False
        digest = str(self.image_digest or '').strip()
        if digest and (ref == digest or ref.endswith('@' + digest) or ref.endswith(':' + digest)):
            return True
        return ref in (self.display_tag, self.deployment_image)

    def as_api_dict(self) -> dict:
        return {
            'image': self.display_tag,
            'image_digest': self.image_digest,
            'generation': self.generation,
            'intent': self.intent,
            'source_commit': self.source_commit,
            'approved_at': self.approved_at,
        }


@dataclass(frozen=True)
class AuthoritativeRuntimeRelease:
    daemon: DaemonRegistry
    release: RuntimeReleaseProjection


def _is_tagged_runtime_image(image: str) -> bool:
    if not image or '@' in image:
        return False
    leaf = image.rsplit('/', 1)[-1]
    if ':' not in leaf:
        return False
    repository, tag = leaf.rsplit(':', 1)
    return repository == 'shizuha-agent-runtime' and bool(tag)


def is_managed_runtime_image_ref(image: str) -> bool:
    """Whether an image reference targets Hive's reviewed runtime repository."""
    value = str(image or '').strip()
    if not value:
        return False
    repository_ref = value.split('@', 1)[0]
    leaf = repository_ref.rsplit('/', 1)[-1]
    repository = leaf.split(':', 1)[0]
    return repository == 'shizuha-agent-runtime'


def _positive_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _validated_projection(value: object) -> RuntimeReleaseProjection:
    if not isinstance(value, dict):
        raise RuntimeReleaseProjectionUnavailable(
            'rt-fleet has not reported a reviewed DesiredRuntimeRelease'
        )

    generation = value.get('generation')
    digest = str(value.get('image_digest') or '').strip()
    image = str(value.get('display_tag') or '').strip()
    source_commit = str(value.get('source_commit') or '').strip().lower()
    approved_at = str(value.get('approved_at') or '').strip()
    intent = str(value.get('intent') or '').strip()
    rollback_of_generation = value.get('rollback_of_generation')
    approved = parse_datetime(approved_at) if approved_at else None

    rollback_valid = (
        intent == 'promote' and rollback_of_generation is None
    ) or (
        intent == 'rollback'
        and _positive_int(generation)
        and _positive_int(rollback_of_generation)
        and rollback_of_generation < generation
    )
    if (
        not _positive_int(generation)
        or not _DIGEST_RE.fullmatch(digest)
        or not _is_tagged_runtime_image(image)
        or not _COMMIT_RE.fullmatch(source_commit)
        or approved is None
        or timezone.is_naive(approved)
        or intent not in _VALID_INTENTS
        or not rollback_valid
    ):
        raise RuntimeReleaseProjectionUnavailable(
            'rt-fleet reported an invalid DesiredRuntimeRelease projection'
        )

    return RuntimeReleaseProjection(
        generation=generation,
        image_digest=digest,
        display_tag=image,
        source_commit=source_commit,
        approved_at=approved_at,
        intent=intent,
        rollback_of_generation=rollback_of_generation,
    )


def get_authoritative_runtime_release(
    *, now=None,
) -> AuthoritativeRuntimeRelease:
    """Return the exact, connected, fresh and validated rt-fleet projection."""
    daemon_id = os.environ.get('SHIZUHA_RUNTIME_RELEASE_DAEMON_ID', 'rt-fleet-s1')
    fleet_id = os.environ.get('SHIZUHA_RUNTIME_RELEASE_FLEET_ID', 'shizuha-fleet')
    observed_at = now or timezone.now()
    daemon = DaemonRegistry.objects.filter(
        daemon_id=daemon_id,
        fleet_id=fleet_id,
        runtime='k8s',
        status=DaemonRegistry.Status.CONNECTED,
        last_seen__gte=observed_at - timedelta(seconds=30),
    ).first()
    if daemon is None:
        raise RuntimeReleaseProjectionUnavailable(
            'authoritative rt-fleet daemon projection is disconnected, stale, or unavailable'
        )

    report = daemon.harness_report if isinstance(daemon.harness_report, dict) else {}
    return AuthoritativeRuntimeRelease(
        daemon=daemon,
        release=_validated_projection(report.get('desired_runtime_release')),
    )
