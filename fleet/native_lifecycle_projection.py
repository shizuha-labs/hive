"""Local read projection of completed, source-fenced native lifecycle writes."""

import logging
import uuid

from django.db import transaction
from django.utils import timezone

from . import native_runtime_release as release
from .models import AgentRuntimeDesiredState, FleetAgent


logger = logging.getLogger(__name__)
SCHEMA = 'hive.native-lifecycle.v1'
INACTIVE = {'hibernated', 'stopped'}


def _superseded_operation(receipt):
    if receipt.get('state') == 'pending':
        return receipt.get('supersedes_operation_id')
    return receipt.get('operation_id')


def _scope(agent):
    if not release.row_candidate(agent):
        return None
    desired = getattr(agent, 'desired_state', None)
    binding = getattr(agent, 'daemon_binding', None)
    migration = getattr(agent, 'runtime_migration_state', None)
    if binding is not None or (migration and migration.migration_state == 'held'):
        return None
    if desired and desired.lifecycle_executor != 'hive_legacy':
        return None
    return {
        'agent_id': str(agent.pk), 'platform_agent_id': agent.agent_id,
        'username': agent.agent_username, 'namespace': release.live_namespace(agent),
        'deployment': agent.deployment_name, 'managing_daemon_id': agent.managing_daemon_id,
        'desired_generation': desired.desired_generation if desired else None,
        'executor_generation': desired.executor_generation if desired else None,
        'desired_lifecycle': desired.desired_lifecycle if desired else None,
        'operation_id': str(desired.operation_id) if desired and desired.operation_id else None,
        'expected_deployment_uid': desired.expected_deployment_uid if desired else '',
        'workload_identity_generation': desired.workload_identity_generation if desired else None,
    }


def _owned_target(agent, authority):
    from .provisioner import get_k8s_client
    _, apps, _ = get_k8s_client()
    target = apps.read_namespaced_deployment(
        agent.deployment_name, release.live_namespace(agent), _request_timeout=(3, 5),
    )
    release.assert_owned_deployment(agent, target)
    if (target.metadata.annotations or {}).get('shizuha.io/runtime-owner') != 'hive':
        raise ValueError('native lifecycle owner is not Hive')
    if not release.validate_applied_release(authority, target):
        raise ValueError('native lifecycle release is not approved')
    annotations = target.metadata.annotations or {}
    if not annotations.get(release.GENERATION_ANNOTATION) or not annotations.get(release.DIGEST_ANNOTATION):
        raise ValueError('native lifecycle applied release identity is missing')
    release.assert_still_current(authority)
    return target


def begin_inactive(agent, status):
    agent._native_lifecycle_operation = None
    if not release.enabled() or not release.row_candidate(agent):
        return None
    authority = release.get_native_runtime_release()
    target = _owned_target(agent, authority)
    source = _scope(agent)
    if source is None:
        raise ValueError('native lifecycle ownership is not current')
    if source['expected_deployment_uid'] and source['expected_deployment_uid'] != target.metadata.uid:
        raise ValueError('native lifecycle Deployment identity changed')
    receipt = {
        'schema': SCHEMA, 'state': 'pending', 'operation_id': uuid.uuid4().hex,
        'target_status': status, 'source': source,
        'deployment_uid': target.metadata.uid,
        'source_resource_version': target.metadata.resource_version,
        'authority_uid': authority.uid, 'authority_generation': authority.release.generation,
        'authority_document_sha256': authority.document_hash,
    }
    with transaction.atomic():
        current = FleetAgent.objects.select_for_update().get(pk=agent.pk)
        AgentRuntimeDesiredState.objects.select_for_update().filter(agent_id=agent.pk).first()
        if _scope(current) != source:
            raise ValueError('native lifecycle source changed before operation')
        previous = current.native_lifecycle_receipt or {}
        receipt['supersedes_operation_id'] = _superseded_operation(previous)
        current.native_lifecycle_receipt = receipt
        current.save(update_fields=['native_lifecycle_receipt'])
    agent._native_lifecycle_operation = receipt
    return receipt


def retire_before_start(agent):
    """A new native materialization must not inherit a completed inactive receipt."""
    previous = getattr(agent, 'native_lifecycle_receipt', None)
    if not getattr(agent, 'pk', None) or (
        not release.enabled() and (not isinstance(previous, dict) or not previous)
    ):
        return
    with transaction.atomic():
        current = FleetAgent.objects.select_for_update().get(pk=agent.pk)
        if current.native_lifecycle_receipt:
            previous = current.native_lifecycle_receipt
            current.native_lifecycle_receipt = {
                'schema': SCHEMA, 'state': 'pending', 'operation_id': uuid.uuid4().hex,
                'target_status': 'running', 'source': _scope(current),
                'supersedes_operation_id': _superseded_operation(previous),
            }
            current.save(update_fields=['native_lifecycle_receipt'])


def complete_inactive(agent):
    pending = getattr(agent, '_native_lifecycle_operation', None)
    if not pending:
        return
    try:
        authority = release.get_native_runtime_release()
        target = _owned_target(agent, authority)
        if (authority.uid != pending['authority_uid']
                or authority.document_hash != pending['authority_document_sha256']
                or target.metadata.uid != pending['deployment_uid']
                or target.spec.replicas != 0):
            raise ValueError('native lifecycle target changed after operation')
        with transaction.atomic():
            current = FleetAgent.objects.select_for_update().get(pk=agent.pk)
            AgentRuntimeDesiredState.objects.select_for_update().filter(agent_id=agent.pk).first()
            if (current.native_lifecycle_receipt != pending or _scope(current) != pending['source']
                    or current.status != pending['target_status'] or current.status not in INACTIVE):
                raise ValueError('native lifecycle completion no longer matches source intent')
            annotations = target.metadata.annotations or {}
            current.native_lifecycle_receipt = {
                **pending, 'state': 'completed', 'completed_at': timezone.now().isoformat(),
                'applied_generation': annotations.get(release.GENERATION_ANNOTATION),
                'applied_digest': annotations.get(release.DIGEST_ANNOTATION),
                'resource_version': target.metadata.resource_version,
            }
            current.save(update_fields=['native_lifecycle_receipt'])
    except Exception:
        logger.exception('Native lifecycle completion receipt unavailable for %s', agent.agent_username)


def overlay_completed_native_lifecycle(agents):
    """Join local durable intent only; never perform runtime/network reads here."""
    if not release.enabled():
        return agents
    usernames = {agent.get('agent_username') for agent in agents if agent.get('agent_username')}
    current = FleetAgent.objects.filter(agent_username__in=usernames).exclude(native_lifecycle_receipt={}).select_related(
        'desired_state', 'daemon_binding', 'runtime_migration_state',
    )
    owners = {}
    for row in current:
        receipt = row.native_lifecycle_receipt
        source = _scope(row)
        if not isinstance(receipt, dict) or receipt.get('schema') != SCHEMA:
            continue
        if (receipt.get('state') == 'pending' and receipt.get('source') == source and source is not None
                and receipt.get('supersedes_operation_id')):
            owners[row.agent_username] = row
            continue
        if (receipt.get('state') != 'completed' or receipt.get('target_status') != row.status
                or row.status not in INACTIVE
                or source is None
                or source['desired_generation'] is None or source['executor_generation'] is None
                or not receipt.get('operation_id') or not receipt.get('completed_at')
                or not receipt.get('authority_uid') or not receipt.get('authority_document_sha256')
                or not receipt.get('deployment_uid') or not receipt.get('applied_digest')
                or not receipt.get('applied_generation') or receipt.get('source') != source):
            continue
        owners[row.agent_username] = row
    output = []
    for agent in agents:
        row = owners.get(agent.get('agent_username'))
        if row is None or str(agent.get('id')) not in {str(row.pk), str(row.agent_id)}:
            output.append(agent)
            continue
        operation_id = row.native_lifecycle_receipt['operation_id']
        pending = row.native_lifecycle_receipt['state'] == 'pending'
        if pending and agent.get('lifecycle_receipt_id') != row.native_lifecycle_receipt['supersedes_operation_id']:
            output.append(agent)
            continue
        projected = {
            **agent, 'status': 'unknown' if pending else row.status,
            'enabled': None if pending else False, 'lifecycle_plane': 'native',
            'lifecycle_receipt_id': operation_id, 'lease_idle_waste': False,
            'lifecycle_state': None if pending else row.runtime_lifecycle_state,
            'hibernation_reason': '' if pending else row.hibernation_reason,
            'hibernated_at': row.hibernated_at.isoformat() if row.hibernated_at and not pending else None,
        }
        if pending:
            projected['previous_lifecycle_observation'] = {
                key: agent.get(key) for key in ('lifecycle_receipt_id', 'status', 'enabled', 'hibernated_at')
            }
        if agent.get('lifecycle_receipt_id') != operation_id:
            lease = agent.get('model_lease')
            if isinstance(lease, dict) and lease.get('managed'):
                projected['model_lease'] = {
                    'managed': True, 'model': lease.get('model'), 'principal': lease.get('principal'),
                    'state': 'unknown', 'is_holder': None, 'holder': None,
                    'reason': 'Cached lease observation predates native lifecycle completion',
                    'observation_stale': True, 'previous_observation': lease,
                }
            activity_fields = ('activity_state', 'activityState', 'activity_phase',
                               'activity_tool_name', 'activity_phase_for_ms')
            previous = {key: agent[key] for key in activity_fields if key in agent}
            if previous:
                projected['previous_activity_observation'] = previous
                projected.update({key: None for key in previous})
        output.append(projected)
    return output
