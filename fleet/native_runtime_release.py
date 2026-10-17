"""Source-owned executable releases for Hive's native lifecycle owner.

The legacy daemon release remains independent. Native authority is explicitly
activated after its generation-27 bootstrap; once active there is no fallback
to a daemon report, row image, or static setting when this document is missing.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from copy import copy
import hashlib
import json
import re

from django.conf import settings

from .runtime_release import (
    RuntimeReleaseProjection,
    RuntimeReleaseProjectionUnavailable,
    _validated_projection,
    is_managed_runtime_image_ref,
)

SCOPE = 'hive-native'
CONFIGMAP = 'hive-native-runtime-release'
DOCUMENT_KEY = 'desired.json'
HASH_ANNOTATION = 'shizuha.io/native-runtime-document-sha256'
SOURCE_ANNOTATION = 'shizuha.io/native-runtime-source-commit'
GENERATION_ANNOTATION = 'shizuha.io/native-runtime-release-generation'
DIGEST_ANNOTATION = 'shizuha.io/native-runtime-release-digest'
MANAGED_BY = 'shizuha-hive'
ANCHOR_DIGEST = 'sha256:e5b4003d52ce41bfa27ddc8a067e55ad1e1f5f13b36650e43713ad0801889d2b'
ANCHOR_SOURCE = '5ac703dd86ed390c03a487832e4f4284ce6d65ca'
_SHA = re.compile(r'^[0-9a-f]{40}$')
_HASH = re.compile(r'^[0-9a-f]{64}$')


def enabled() -> bool:
    return bool(getattr(settings, 'HIVE_NATIVE_RUNTIME_RELEASE_ENABLED', False))


@dataclass(frozen=True)
class NativeRuntimeRelease:
    release: RuntimeReleaseProjection
    document_hash: str
    source_commit: str
    uid: str
    resource_version: str
    history: tuple[RuntimeReleaseProjection, ...]

    def as_api_dict(self) -> dict:
        return {
            **self.release.as_api_dict(), 'scope': SCOPE,
            'document_sha256': self.document_hash,
            'authority_source_commit': self.source_commit,
            'rollback_of_generation': self.release.rollback_of_generation,
        }


def parse_document(raw: str) -> tuple[RuntimeReleaseProjection, ...]:
    try:
        doc = json.loads(raw)
        if not isinstance(doc, dict) or set(doc) != {
            'schema_version', 'scope', 'desired_generation', 'releases',
        } or doc['schema_version'] != 1 or doc['scope'] != SCOPE:
            raise ValueError('native release scope/schema is invalid')
        releases = doc['releases']
        if not isinstance(releases, list) or not releases:
            raise ValueError('native release history is empty')
        keys = {'generation', 'image_digest', 'display_tag', 'source_commit',
                'approved_at', 'intent', 'rollback_of_generation'}
        if any(not isinstance(r, dict) or set(r) != keys for r in releases):
            raise ValueError('native release record fields are invalid')
        history = tuple(_validated_projection(r) for r in releases)
        generations = [r.generation for r in history]
        if generations != sorted(set(generations)) or doc['desired_generation'] != generations[-1]:
            raise ValueError('native release history is not monotonic')
        by_generation = {r.generation: r for r in history}
        # First record is an explicit imported baseline. Its previous global
        # rollback lineage remains in the legacy source document; native
        # promotions/rollbacks may only reference this native history.
        if (history[0].generation != 27 or history[0].intent != 'promote'
                or history[0].image_digest != ANCHOR_DIGEST
                or history[0].source_commit != ANCHOR_SOURCE):
            raise ValueError('native history must begin with its reviewed generation-27 anchor')
        digests: set[str] = set()
        for release in history:
            if release.intent == 'promote':
                if release.image_digest in digests:
                    raise ValueError('a prior native digest requires explicit rollback intent')
            else:
                prior = by_generation.get(release.rollback_of_generation)
                if prior is None or prior.image_digest != release.image_digest:
                    raise ValueError('native rollback does not match its named earlier digest')
            digests.add(release.image_digest)
        return history
    except (KeyError, TypeError, ValueError, RuntimeReleaseProjectionUnavailable) as exc:
        raise RuntimeReleaseProjectionUnavailable(f'Invalid native runtime release: {exc}') from exc


def get_native_runtime_release(*, bootstrap: bool = False) -> NativeRuntimeRelease:
    if not enabled() and not bootstrap:
        raise RuntimeReleaseProjectionUnavailable('Native runtime release authority is not activated')
    # No shared cache: a stale release may not authorize even one image write.
    from .provisioner import get_k8s_client
    try:
        core, _, _ = get_k8s_client()
        cm = core.read_namespaced_config_map(
            CONFIGMAP, getattr(settings, 'HIVE_NATIVE_RUNTIME_RELEASE_NAMESPACE', 'shizuha-hive'),
            _request_timeout=(3, 5),
        )
        if cm.metadata.deletion_timestamp is not None:
            raise ValueError('native authority object is being deleted')
        raw = (cm.data or {}).get(DOCUMENT_KEY)
        annotations = cm.metadata.annotations or {}
        expected_hash = annotations.get(HASH_ANNOTATION, '')
        source = annotations.get(SOURCE_ANNOTATION, '')
        if not isinstance(raw, str) or not _HASH.fullmatch(expected_hash) or not _SHA.fullmatch(source):
            raise ValueError('native document/source receipt is missing')
        digest = hashlib.sha256(raw.encode()).hexdigest()
        if digest != expected_hash:
            raise ValueError('native document checksum disagrees with its source receipt')
        if not cm.metadata.uid or not cm.metadata.resource_version:
            raise ValueError('native authority object identity is missing')
        history = parse_document(raw)
        return NativeRuntimeRelease(history[-1], digest, source,
                                    cm.metadata.uid, cm.metadata.resource_version, history)
    except RuntimeReleaseProjectionUnavailable:
        raise
    except Exception as exc:
        raise RuntimeReleaseProjectionUnavailable(
            f'Native runtime authority unavailable ({type(exc).__name__})',
        ) from exc


def row_candidate(agent) -> bool:
    """Desired native row candidate; existing object ownership is checked too."""
    return (
        str(getattr(agent, 'status', '')) not in {'deleted', 'deleting'}
        and bool(str(getattr(agent, 'deployment_name', '') or '').strip())
        and bool(str(getattr(agent, 'agent_id', '') or '').strip())
        and (not agent.image or is_managed_runtime_image_ref(agent.image))
    )


def assert_owned_deployment(agent, deployment) -> None:
    labels = deployment.metadata.labels or {}
    if not row_candidate(agent) or (
        labels.get('app.kubernetes.io/managed-by') != MANAGED_BY
        or labels.get('shizuha.io/agent') != agent.agent_username
        or deployment.metadata.name != agent.deployment_name
        or deployment.metadata.namespace != live_namespace(agent)
        or not deployment.metadata.uid or not deployment.metadata.resource_version
    ):
        raise RuntimeReleaseProjectionUnavailable('Native release refused an unowned or ambiguous Deployment')


def validate_applied_release(authority: NativeRuntimeRelease, deployment, *, allow_cold: bool = False) -> bool:
    """A stale/unknown projection cannot move an already-adopted seat backward."""
    known = {str(r.generation): r.image_digest for r in authority.history}
    pairs = []
    for metadata in (deployment.metadata, deployment.spec.template.metadata):
        annotations = metadata.annotations or {}
        generation, digest = annotations.get(GENERATION_ANNOTATION), annotations.get(DIGEST_ANNOTATION)
        if bool(generation) != bool(digest):
            raise RuntimeReleaseProjectionUnavailable('Native applied release authority is incomplete')
        if generation and known.get(generation) != digest:
            raise RuntimeReleaseProjectionUnavailable('Native applied release is absent from selected history')
        pairs.append((generation, digest))
    if pairs[0] != pairs[1]:
        raise RuntimeReleaseProjectionUnavailable('Native Deployment/template release authority disagrees')
    containers = [c for c in deployment.spec.template.spec.containers
                  if c.name in {'agent', agent_name(deployment)}]
    if len(containers) != 1:
        raise RuntimeReleaseProjectionUnavailable('Native agent container is ambiguous')
    # Mutable tag equality alone is not bootstrap evidence. Existing pods are
    # adopted only when the Deployment already carries a reviewed digest.
    image = containers[0].image
    applied = pairs[0][1]
    allowed = {r.deployment_image for r in authority.history}
    if not applied and image != authority.history[0].deployment_image:
        if allow_cold and deployment.spec.replicas == 0:
            # This image is NOT adopted or qualified. A future authorized
            # start may materialize a fresh current template only after the
            # final zero-replica/no-Pod identity and lifecycle fences.
            return False
        raise RuntimeReleaseProjectionUnavailable('Native active image has no reviewed adoption authority')
    if image not in allowed or (applied and not image.endswith('@' + applied)):
        raise RuntimeReleaseProjectionUnavailable('Native observed image is outside its reviewed release history')
    return True


def agent_name(deployment) -> str:
    return (deployment.metadata.labels or {}).get('shizuha.io/agent', '')


def live_namespace(agent) -> str:
    from .provisioner import _live_ns
    return _live_ns(agent)


_ROW_FIELDS = ('agent_id', 'agent_username', 'namespace', 'isolated', 'image',
               'model', 'mcp_config', 'env', 'context_prompt', 'managing_daemon_id',
               'node_selector_overrides', 'resource_overrides', 'resource_profile',
               'hostplane_ssh', 'skill_config', 'sandbox_required', 'display_name', 'role',
               'owner_id', 'image_pull_policy', 'arch', 'requires_longhorn', 'node_pool', 'node_name',
               'tolerations', 'pvc_size', 'storage_class', 'workspace_mount_path', 'shared_workspace',
               'sidecar_set', 'credential_grant_scopes', 'personality_traits', 'model_fallbacks',
               'model_overrides', 'reasoning_effort', 'failover_chain_id', 'platform_url',
               'expose_http', 'http_port')
_DESIRED_FIELDS = ('desired_generation', 'desired_config_hash', 'desired_lifecycle',
                   'operation_id', 'operation_kind', 'lifecycle_executor',
                   'executor_generation', 'expected_deployment_uid',
                   'workload_identity_generation', 'fleet_control_revision')


def _intent(row, desired) -> tuple:
    # Compare the existing renderer's canonical form. Its in-memory heartbeat
    # normalization must not make a second call look like an external edit.
    from .provisioner import heal_stale_work_agent_heartbeat_env
    row = copy(row)
    heal_stale_work_agent_heartbeat_env(row)
    row.sidecar_set = ['broker'] + [s for s in (row.sidecar_set or []) if s and s != 'broker']
    return (tuple(live_namespace(row) if key == 'namespace' else getattr(row, key) for key in _ROW_FIELDS),
            tuple(getattr(desired, key) for key in _DESIRED_FIELDS) if desired else None,
            row.status in {'stopped', 'deleted', 'deleting'} or row.runtime_lifecycle_state == 'operator_stopped')


def bind_intent(agent) -> None:
    """Capture source intent, excluding activity timestamps and status reports."""
    from .models import FleetAgent, AgentRuntimeDesiredState
    row = FleetAgent.objects.get(pk=agent.pk)
    desired = AgentRuntimeDesiredState.objects.filter(agent_id=agent.pk).first()
    if not row_candidate(row) or _intent(row, desired)[0] != _intent(agent, desired)[0]:
        raise RuntimeReleaseProjectionUnavailable('Native caller has stale desired configuration')
    agent._native_runtime_intent = _intent(row, desired)


@contextmanager
def intent_guard(agent):
    """Hold the desired row through the final Kubernetes CAS, never across waits."""
    from django.db import transaction
    from .models import FleetAgent, AgentRuntimeDesiredState
    with transaction.atomic():
        row = FleetAgent.objects.select_for_update().get(pk=agent.pk)
        desired = AgentRuntimeDesiredState.objects.select_for_update().filter(agent_id=agent.pk).first()
        if not row_candidate(row) or _intent(row, desired) != getattr(agent, '_native_runtime_intent', None):
            raise RuntimeReleaseProjectionUnavailable('Native desired configuration changed before actuation')
        if desired and desired.lifecycle_executor != 'hive_legacy':
            raise RuntimeReleaseProjectionUnavailable('Native lifecycle belongs to a different executor')
        yield desired


def assert_still_current(authority: NativeRuntimeRelease) -> None:
    current = get_native_runtime_release()
    if (current.uid, current.document_hash) != (authority.uid, authority.document_hash):
        raise RuntimeReleaseProjectionUnavailable('Native runtime authority changed before actuation')


def stamp_release(authority: NativeRuntimeRelease, deployment) -> None:
    for metadata in (deployment.metadata, deployment.spec.template.metadata):
        annotations = dict(metadata.annotations or {})
        annotations[GENERATION_ANNOTATION] = str(authority.release.generation)
        annotations[DIGEST_ANNOTATION] = authority.release.image_digest
        metadata.annotations = annotations


def runtime_snapshot(*, bootstrap: bool = False, include_pods: bool = True, include_writers: bool = False) -> dict:
    try:
        return _runtime_snapshot(bootstrap=bootstrap, include_pods=include_pods, include_writers=include_writers)
    except RuntimeReleaseProjectionUnavailable:
        raise
    except Exception as exc:
        raise RuntimeReleaseProjectionUnavailable(f'Native census unavailable ({type(exc).__name__})') from exc


def _runtime_snapshot(*, bootstrap: bool, include_pods: bool, include_writers: bool) -> dict:
    """Read-only authority and actual owner census; no lease, drain or actuation."""
    from .models import FleetAgent
    from .provisioner import get_k8s_client, fleet_namespace
    authority = get_native_runtime_release(bootstrap=bootstrap)
    core, apps, _ = get_k8s_client()
    all_rows = list(FleetAgent.objects.all())
    rows = [r for r in all_rows if r.status not in {'deleted', 'deleting'}]
    retired = {(live_namespace(r), r.deployment_name) for r in all_rows if r.status in {'deleted', 'deleting'}}
    candidates = {(live_namespace(row), row.deployment_name): row for row in rows if row_candidate(row)}
    namespaces = sorted({fleet_namespace()} | {ns for ns, _ in candidates})
    census, excluded = [], []
    found = set()
    for ns in namespaces:
        deployments = apps.list_namespaced_deployment(ns, _request_timeout=(3, 5)).items
        pods = core.list_namespaced_pod(ns, _request_timeout=(3, 5)).items if include_pods else []
        replica_sets = {r.metadata.uid: r for r in apps.list_namespaced_replica_set(
            ns, _request_timeout=(3, 5)).items} if include_pods else {}
        for deployment in deployments:
            if not deployment.metadata.name.startswith('agent-'):
                continue
            labels = deployment.metadata.labels or {}
            key = (ns, deployment.metadata.name)
            row = candidates.get(key)
            digest = hashlib.sha256(json.dumps(deployment.spec.to_dict(), sort_keys=True,
                                               separators=(',', ':'), default=str).encode()).hexdigest()
            if labels.get('app.kubernetes.io/managed-by') != MANAGED_BY or key in retired:
                excluded.append({'namespace': ns, 'deployment': deployment.metadata.name,
                                 'uid': deployment.metadata.uid, 'spec_sha256': digest,
                                 'reason': 'retired_hive_row' if key in retired else 'different_lifecycle_owner'})
                continue
            if row is None:
                raise RuntimeReleaseProjectionUnavailable('Native census found a Hive-owned Deployment without a managed current row')
            assert_owned_deployment(row, deployment)
            adopted = validate_applied_release(authority, deployment, allow_cold=True)
            found.add(key)
            owned_pods = []
            for pod in pods:
                owners = [o for o in (pod.metadata.owner_references or []) if o.controller and o.kind == 'ReplicaSet']
                if len(owners) != 1:
                    continue
                rs = replica_sets.get(owners[0].uid)
                if rs is None or not any(o.controller and o.kind == 'Deployment' and o.uid == deployment.metadata.uid
                                         for o in (rs.metadata.owner_references or [])):
                    continue
                images = [c.image for c in pod.spec.containers if c.name in {'agent', row.agent_username}]
                if len(images) != 1:
                    raise RuntimeReleaseProjectionUnavailable('Native census has an ambiguous Pod runtime')
                owned_pods.append({'name': pod.metadata.name, 'uid': pod.metadata.uid,
                                   'image': images[0], 'deleting': bool(pod.metadata.deletion_timestamp),
                                   'image_id': next((s.image_id for s in (pod.status.container_statuses or [])
                                                     if s.name in {'agent', row.agent_username}), ''),
                                   'ready': pod.status.phase == 'Running' and any(
                                       c.type == 'Ready' and c.status == 'True' for c in (pod.status.conditions or []))})
            image = next(c.image for c in deployment.spec.template.spec.containers
                         if c.name in {'agent', row.agent_username})
            census.append({'agent_id': str(row.pk), 'username': row.agent_username, 'namespace': ns,
                           'deployment': deployment.metadata.name, 'uid': deployment.metadata.uid,
                           'resource_version': deployment.metadata.resource_version, 'spec_sha256': digest,
                           'replicas': deployment.spec.replicas, 'image': image,
                           'applied_generation': (deployment.metadata.annotations or {}).get(GENERATION_ANNOTATION),
                           'ready_replicas': deployment.status.ready_replicas or 0,
                           'adopted': adopted,
                           'deferred_until_start': deployment.spec.replicas == 0 and image != authority.release.deployment_image,
                           'pods': sorted(owned_pods, key=lambda p: p['name'])})
    # Missing workloads may legitimately be pending initial provisioning. Their
    # source row must not be misreported as a deployed or successfully rolled seat.
    missing = [{'agent_id': str(row.pk), 'username': row.agent_username,
                'namespace': ns, 'deployment': name} for (ns, name), row in candidates.items()
               if (ns, name) not in found and not any(x['namespace'] == ns and x['deployment'] == name for x in excluded)]
    writers = _writer_inventory(core, apps) if include_writers else None
    current = get_native_runtime_release(bootstrap=bootstrap)
    if (current.uid, current.document_hash) != (authority.uid, authority.document_hash):
        raise RuntimeReleaseProjectionUnavailable('Native authority changed during census')
    return {'schema_version': 1, 'scope': SCOPE,
            'writers': writers,
            'authority': {**authority.as_api_dict(), 'uid': authority.uid, 'resource_version': authority.resource_version},
            'census': sorted(census, key=lambda r: (r['namespace'], r['deployment'])),
            'excluded': sorted(excluded, key=lambda r: (r['namespace'], r['deployment'])),
            'not_materialized': sorted(missing, key=lambda r: (r['namespace'], r['deployment']))}


def _writer_inventory(core, apps) -> list[dict]:
    from kubernetes import client
    namespace = getattr(settings, 'HIVE_NATIVE_RUNTIME_RELEASE_NAMESPACE', 'shizuha-hive')
    output = []
    def add(kind, obj, spec, **state):
        for container in spec.containers or []:
            repository = str(container.image or '').split('@', 1)[0].rsplit('/', 1)[-1].split(':', 1)[0]
            if repository != 'shizuha-hive':
                continue
            env = {e.name: e.value for e in (container.env or [])}
            output.append({'kind': kind, 'namespace': namespace, 'name': obj.metadata.name,
                           'uid': obj.metadata.uid, 'container': container.name, 'image': container.image,
                           'command': list(container.command or []) + list(container.args or []),
                           'native_enabled': env.get('HIVE_NATIVE_RUNTIME_RELEASE_ENABLED') == 'true',
                           'native_namespace': env.get('HIVE_NATIVE_RUNTIME_RELEASE_NAMESPACE', ''),
                           'replicas': None, 'suspended': None, 'phase': None, **state})
    for dep in apps.list_namespaced_deployment(namespace, _request_timeout=(3, 5)).items:
        add('Deployment', dep, dep.spec.template.spec, replicas=dep.spec.replicas)
    batch = client.BatchV1Api()
    for cron in batch.list_namespaced_cron_job(namespace, _request_timeout=(3, 5)).items:
        add('CronJob', cron, cron.spec.job_template.spec.template.spec, suspended=bool(cron.spec.suspend))
    for job in batch.list_namespaced_job(namespace, _request_timeout=(3, 5)).items:
        if not any(c.type in {'Complete', 'Failed'} and c.status == 'True' for c in (getattr(job.status, 'conditions', None) or [])):
            add('Job', job, job.spec.template.spec, suspended=bool(job.spec.suspend), replicas=job.spec.parallelism)
    # A delayed ReplicaSet controller can still create an old-image Pod after
    # the Deployment template changed. Zero-Pod is not zero-writer intent.
    for rs in apps.list_namespaced_replica_set(namespace, _request_timeout=(3, 5)).items:
        if (rs.spec.replicas or 0) > 0:
            add('ReplicaSet', rs, rs.spec.template.spec, replicas=rs.spec.replicas)
    for pod in core.list_namespaced_pod(namespace, _request_timeout=(3, 5)).items:
        if pod.status.phase not in {'Succeeded', 'Failed'}:
            add('Pod', pod, pod.spec, phase=pod.status.phase)
    if not output:
        raise RuntimeReleaseProjectionUnavailable('Native writer inventory was empty')
    return sorted(output, key=lambda r: (r['kind'], r['name'], r['container']))
