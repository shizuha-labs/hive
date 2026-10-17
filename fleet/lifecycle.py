"""Fleet-agent retirement primitives.

Hive owns desired lifecycle state. Retirement therefore persists the Hive
tombstone before asking either runtime actuator to remove materialized state.
The tombstone remains until normal reconciliation observes absence.
"""
from __future__ import annotations

import logging

from . import agent_index_cache, provisioner
from .models import FleetAgent, FleetAgentArchive, FleetAgentEvent
from .runtime_fleet import delete_runtime_agent

logger = logging.getLogger(__name__)


def retire_fleet_agent(agent: FleetAgent, *, actor: str, reason: str) -> dict:
    """Persist a durable delete and converge all known runtime actuators."""
    agent.status = FleetAgent.Status.DELETED
    agent.status_message = ''
    agent.save(update_fields=['status', 'status_message'])

    runtime_ok = True
    runtime_detail = 'not daemon-managed'
    if agent.managing_daemon_id:
        runtime_ok, runtime_detail = delete_runtime_agent(agent)
        if not runtime_ok:
            logger.error(
                'daemon-managed retirement could not be queued for %s: %s',
                agent.agent_username,
                runtime_detail,
            )

    k8s_ok = provisioner.deprovision_agent(agent)
    agent.status = FleetAgent.Status.DELETED
    agent.status_message = '' if k8s_ok else 'runtime delete queued; k8s cleanup will converge'
    agent.save(update_fields=['status', 'status_message'])

    FleetAgentEvent.objects.create(
        agent=agent,
        event_type='deleted',
        message=(
            f'{reason} by {actor}; Hive tombstone persisted; '
            f'runtime={runtime_detail or "applied"}; '
            f'k8s_cleanup={"applied" if k8s_ok else "pending"}'
        ),
    )
    FleetAgentArchive.objects.filter(
        agent_username__iexact=agent.agent_username,
    ).delete()
    agent_index_cache.invalidate(f'fleet retirement: {reason}')
    return {
        'runtime_ok': runtime_ok,
        'runtime_detail': runtime_detail,
        'k8s_ok': k8s_ok,
    }
