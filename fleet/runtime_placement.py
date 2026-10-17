"""Hive fleet placement: k3s-only.

Hive fleet agents (and the per-user fleet daemon that manages them, e.g.
hritik / rt-fleet-s1) spawn **only** as k3s Deployments in the fleet
namespace. Host-local / bare_metal / docker-run placement is not a valid
Hive agent runtime.

Historical defaults of ``bare_metal`` in daemon-link frames and API
fallbacks pre-date the k3s cutover and must never re-open a dual-spawn
path next to the k3s fleet.
"""
from __future__ import annotations

from typing import Any, Mapping, MutableMapping

# Canonical placement for every Hive FleetAgent / daemon-link config frame.
HIVE_FLEET_RUNTIME_ENVIRONMENT = 'k8s'

# Values that mean "run on the host / not k3s agent Deployments".
# Isolation vocabulary (sandbox / restricted_container) is NOT placement —
# those are separate FleetAgent fields (sandbox_required, isolated).
HOST_OR_LEGACY_PLACEMENTS = frozenset({
    'bare_metal',
    'local',
    'host',
    'docker',
    'container',  # docker-run style on the daemon host, not a k3s pod
    'restricted_container',
    'sandbox',
    '',
})

# Host CLI `isK8sAgent` is a stub that always returns false, so `shizuha up`
# on a k3s fleet host hits LEGACY_LOCAL_DAEMON_RETIRED and reports error.
# That is not a live runtime observation and must never paint Agents Failed.
RETIRED_HOST_SPAWN_MARKERS = (
    'legacy_local_daemon_retired',
    'refusing host-local spawn',
    'local daemon retired',
    'fleet agents are k3s-only',
)


def is_retired_host_spawn_observation(row: Mapping[str, Any] | None) -> bool:
    """True when a daemon/runtime row is the retired host-local spawn refusal.

    Live k3s pods keep Running while the host rt-fleet daemon reports
    ``status=error`` with this message. Treating that as Failed made Ichi/Ni/Kai
    and peers look down on the Agents page.
    """
    blob = ' '.join(
        str((row or {}).get(key) or '')
        for key in ('status_message', 'error', 'status')
    ).casefold()
    return any(marker in blob for marker in RETIRED_HOST_SPAWN_MARKERS)


def coerce_hive_fleet_runtime_environment(value: Any = None) -> str:
    """Return the only allowed Hive fleet placement: ``k8s``.

    Accepts any legacy input and always returns ``k8s``. Callers that need
    to *reject* non-k8s writes should use :func:`validate_hive_fleet_runtime_environment`.
    """
    return HIVE_FLEET_RUNTIME_ENVIRONMENT


def validate_hive_fleet_runtime_environment(value: Any) -> str:
    """Validate a write-path runtime_environment for Hive fleet agents.

    Empty / omitted → k8s. Explicit non-k8s placement values are rejected.
    """
    if value is None:
        return HIVE_FLEET_RUNTIME_ENVIRONMENT
    raw = str(value).strip().lower()
    if not raw or raw == HIVE_FLEET_RUNTIME_ENVIRONMENT:
        return HIVE_FLEET_RUNTIME_ENVIRONMENT
    if raw in HOST_OR_LEGACY_PLACEMENTS:
        raise ValueError(
            f'Hive fleet agents must use runtime_environment=k8s '
            f'(host/bare_metal/docker placement is not allowed); got {value!r}'
        )
    # Unknown future value: still force k8s rather than open a host path.
    return HIVE_FLEET_RUNTIME_ENVIRONMENT


def force_mcp_config_k8s(mcp_config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a copy of mcp_config with runtime_environment forced to k8s."""
    out = dict(mcp_config or {})
    out['runtime_environment'] = HIVE_FLEET_RUNTIME_ENVIRONMENT
    return out


def force_config_frame_k8s(config: MutableMapping[str, Any] | None) -> dict[str, Any]:
    """Force runtime_environment on a daemon-link config_update payload."""
    out = dict(config or {})
    out['runtime_environment'] = HIVE_FLEET_RUNTIME_ENVIRONMENT
    # camelCase used by some frames
    if 'runtimeEnvironment' in out:
        out['runtimeEnvironment'] = HIVE_FLEET_RUNTIME_ENVIRONMENT
    return out
