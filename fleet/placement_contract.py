"""Hive-owned placement convergence without rewriting workspace locality."""
from copy import deepcopy


class PlacementUnavailable(ValueError):
    pass


PLACEMENT_FIELDS = frozenset({
    'arch', 'requires_longhorn', 'node_selector_overrides', 'node_pool',
    'node_name', 'tolerations',
})


def desired_placement(agent, spec, core):
    from . import provisioner

    selector = provisioner._node_selector(agent)
    binding = None
    bound_control_plane = False
    volume_hostname = ''
    observed_nodes = {}
    claims = [
        volume.persistent_volume_claim.claim_name
        for volume in spec.volumes or []
        if volume.name == 'workspace' and volume.persistent_volume_claim is not None
    ]
    if claims:
        if claims != [agent.workspace_pvc_name]:
            raise PlacementUnavailable('Workspace claim differs from Hive identity')
        claim = core.read_namespaced_persistent_volume_claim(
            claims[0], provisioner._live_ns(agent), _request_timeout=(3, 5),
        )
        if not claim.metadata.uid:
            raise PlacementUnavailable('Workspace claim identity is unknown')
        if claim.spec.volume_name:
            volume = core.read_persistent_volume(claim.spec.volume_name, _request_timeout=(3, 5))
            if not volume.metadata.uid or not volume.metadata.resource_version:
                raise PlacementUnavailable('Workspace volume identity is unknown')
            reference = volume.spec.claim_ref
            if reference is None or (reference.uid, reference.name, reference.namespace) != (
                    claim.metadata.uid, claims[0], provisioner._live_ns(agent)):
                raise PlacementUnavailable('Workspace volume binding identity changed')
            binding = (claim.metadata.uid, volume.metadata.uid, volume.metadata.resource_version)
            volume_hostname = provisioner._bound_volume_hostname(volume)
            if volume.spec.local is not None:
                required = getattr(volume.spec.node_affinity, 'required', None)
                terms = getattr(required, 'node_selector_terms', None) or []
                hostnames = set()
                for term in terms:
                    expressions = term.match_expressions or []
                    if (term.match_fields or len(expressions) != 1
                            or expressions[0].key != 'kubernetes.io/hostname'
                            or expressions[0].operator != 'In'
                            or len(expressions[0].values or []) != 1):
                        raise PlacementUnavailable('Local workspace node affinity is not a known singleton')
                    hostnames.add(expressions[0].values[0])
                if len(hostnames) != 1:
                    raise PlacementUnavailable('Local workspace hostname is unknown')
                hostname = next(iter(hostnames))
                node = core.read_node(hostname, _request_timeout=(3, 5))
                if not node.metadata.uid:
                    raise PlacementUnavailable('Workspace node identity is unknown')
                observed_nodes[hostname] = node
                labels = node.metadata.labels or {}
                conflicts = sorted(key for key, value in selector.items() if labels.get(key) != value)
                if conflicts:
                    raise PlacementUnavailable('Hive placement conflicts with bound workspace: ' + ', '.join(conflicts))
                local_control_plane = any(key in labels for key in (
                    'node-role.kubernetes.io/control-plane', 'node-role.kubernetes.io/etcd',
                ))
                binding += (node.metadata.uid, tuple(sorted((key, labels.get(key)) for key in selector)),
                            local_control_plane)
        elif getattr(claim.status, 'phase', None) == 'Bound':
            raise PlacementUnavailable('Bound workspace has no volume identity')
    hostname = (agent.node_name or '').strip() or volume_hostname
    if hostname:
        node = observed_nodes.get(hostname)
        if node is None:
            node = core.read_node(hostname, _request_timeout=(3, 5))
        if not node.metadata.uid:
            raise PlacementUnavailable('Placement node identity is unknown')
        bound_control_plane = any(key in (node.metadata.labels or {}) for key in (
            'node-role.kubernetes.io/control-plane', 'node-role.kubernetes.io/etcd',
        ))
        binding = (binding, hostname, node.metadata.uid, bound_control_plane)
    affinity = provisioner._affinity(agent, bound_control_plane=bound_control_plane)
    return {
        'node_selector': selector,
        'affinity': affinity,
        'tolerations': provisioner._pod_tolerations(agent),
        'binding': binding,
    }


def converge_placement(spec, desired):
    changed = False
    for field in ('node_selector', 'affinity', 'tolerations'):
        current = getattr(spec, field, None)
        wanted = desired[field]
        if field == 'affinity':
            same = (current.to_dict() if current else None) == (wanted.to_dict() if wanted else None)
        elif field == 'tolerations':
            same = [value.to_dict() for value in current or []] == [value.to_dict() for value in wanted or []]
        else:
            same = dict(current or {}) == dict(wanted or {})
        if not same:
            setattr(spec, field, deepcopy(wanted))
            changed = True
    return changed


def placement_changed(spec, desired):
    return converge_placement(deepcopy(spec), desired)
