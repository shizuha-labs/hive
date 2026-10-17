"""
gVisor-Job code-exec sandbox substitute (HLD §11 R1 / PLAT-153).

The native per-agent pod (PLAT-147) deliberately has NO inner-docker exec. This
module is the decided substitute: an agent's untrusted code-exec workload runs as
a **K8s Job** with ``runtimeClassName: gvisor`` in a **per-agent sandbox namespace**
(``sbx-<agent>``), on the dedicated tainted sandbox node pool (PLAT-289 substrate:
runsc + RuntimeClass + ``node.shizuha/sandbox=true:NoSchedule``).

The isolation boundary is BOTH layers (decided, §11 R1):
  * gVisor syscall boundary (the ``runtimeClassName``), AND
  * the namespace/RBAC/NetworkPolicy/quota declared here.

Per-agent sandbox namespace invariants (the actual security boundary — keep tight):
  * ``automountServiceAccountToken: false`` on the runner SA — no kube API path.
  * RBAC: the runner SA may create/get/list/watch/delete **Jobs** (and read pod
    logs) **only in its own** ``sbx-<agent>`` ns. Nothing else — no Secrets, no
    cluster scope (a Role, never a ClusterRole).
  * **default-deny** NetworkPolicy (Ingress+Egress); a per-Job egress allowlist is
    added explicitly when an exec needs the network (no-network is the default).
  * ResourceQuota (caps pods/jobs/cpu/mem in the ns) + LimitRange (per-container
    defaults) so one exec can't starve the node pool.
  * Job: ``restartPolicy=Never``, ``backoffLimit=0``, ``activeDeadlineSeconds`` (no
    runaway), ``ttlSecondsAfterFinished`` (self-reap), ephemeral storage only,
    hardened securityContext (non-root, no priv-esc, drop ALL caps, RO rootfs).

The dispatcher is a thin library call (create Job → stream logs → reap), NOT a
shared service — a shared service centralizes the very blast radius being isolated
(§11 R1, REJECTED).
"""
import logging

from django.conf import settings
from kubernetes import client, config
from kubernetes.client.rest import ApiException

from .models import FleetAgent

logger = logging.getLogger(__name__)

MANAGED_BY = 'shizuha-hive'

# The runner ServiceAccount in each sandbox ns (token NOT auto-mounted).
SANDBOX_SA = 'sandbox-runner'
SANDBOX_ROLE = 'sandbox-runner'

# Sandbox node pool taint/label (PLAT-289 substrate). Jobs carry the matching
# nodeSelector + toleration; the rest of the fleet needs no node changes.
SANDBOX_NODE_LABEL = 'node.shizuha/sandbox'
SANDBOX_NODE_VALUE = 'true'

DEFAULT_ACTIVE_DEADLINE_SECONDS = 900   # 15 min hard wall on a single exec
DEFAULT_TTL_AFTER_FINISHED = 300        # self-reap finished Jobs after 5 min
DEFAULT_EXEC_CPU = '1'
DEFAULT_EXEC_MEM = '1Gi'
# Bound node-local disk for untrusted exec (review P1): per-emptyDir sizeLimit +
# a container ephemeral-storage limit, so a write-heavy run can't fill the node.
DEFAULT_EXEC_EMPTYDIR = '512Mi'         # per emptyDir (/work, /tmp)
DEFAULT_EXEC_EPHEMERAL = '1Gi'          # container ephemeral-storage limit (≥ Σ emptyDirs)
# Cap log collection memory (review P2): untrusted code can print unbounded output.
SANDBOX_LOG_LIMIT_BYTES = 1_000_000


def sandbox_runtime_class() -> str:
    """The configured gVisor runtimeClass name (settings, default 'gvisor')."""
    return getattr(settings, 'FLEET_SANDBOX_RUNTIME_CLASS', '') or 'gvisor'


def sandbox_namespace(agent: FleetAgent) -> str:
    """Per-agent sandbox namespace — distinct from the agent's run namespace so a
    sandbox escape is contained to the agent's OWN throwaway exec ns, never the
    fleet ns that holds the broker sidecar + identity Secret."""
    return f'sbx-{agent.agent_username}'


def get_sandbox_clients():
    """k8s clients used by the dispatcher. In-cluster config when running in k8s."""
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return (
        client.CoreV1Api(),
        client.BatchV1Api(),
        client.RbacAuthorizationV1Api(),
        client.NetworkingV1Api(),
    )


def _job_label(name: str) -> str:
    """A k8s label VALUE for a job name. Object names can exceed 63 chars but label
    values are capped at 63 (review P2) — truncate so the Job-template label, the
    egress podSelector, and the get_job_logs selector all agree on the same value."""
    return (name or '')[:63]


def _labels(agent: FleetAgent, **extra) -> dict:
    base = {
        'app.kubernetes.io/managed-by': MANAGED_BY,
        'shizuha.io/agent': agent.agent_username,
        'shizuha.io/sandbox': 'true',
    }
    base.update(extra)
    return base


def _create_or_ignore(create_fn, *args, **kwargs):
    """Run a k8s create; swallow 409 AlreadyExists (idempotent ensure)."""
    try:
        create_fn(*args, **kwargs)
        return True
    except ApiException as e:
        if e.status != 409:
            raise
        return False


def _apply(create_fn, patch_fn, name, ns, body):
    """Create the object; on 409 RECONCILE it (patch to the desired spec) rather than
    assuming the pre-existing one is correct (review P2) — so tightened quotas/RBAC/
    NetworkPolicy from a later rollout actually take effect on an existing ns."""
    try:
        create_fn(ns, body)
    except ApiException as e:
        if e.status != 409:
            raise
        try:
            patch_fn(name, ns, body)
        except ApiException as pe:
            logger.warning('sandbox: reconcile-patch failed for %s/%s: %s', ns, name, pe.reason)


def ensure_sandbox_namespace(agent: FleetAgent) -> str:
    """Idempotently create the per-agent sandbox ns + its security objects.

    Returns the namespace name. Safe to call before every dispatch.
    """
    core_v1, _batch_v1, rbac_v1, net_v1 = get_sandbox_clients()
    ns = sandbox_namespace(agent)
    labels = _labels(agent)

    # 1. Namespace.
    _create_or_ignore(core_v1.create_namespace, body=client.V1Namespace(
        metadata=client.V1ObjectMeta(name=ns, labels=labels)))

    # 2. Runner ServiceAccount — token NOT auto-mounted (no kube API path from the
    #    exec pod even though it runs under this SA). create-or-reconcile (P2).
    _apply(core_v1.create_namespaced_service_account, core_v1.patch_namespaced_service_account,
           SANDBOX_SA, ns, client.V1ServiceAccount(
        metadata=client.V1ObjectMeta(name=SANDBOX_SA, namespace=ns, labels=labels),
        automount_service_account_token=False))

    # 3. Role — Jobs (+ pod read for log streaming) ONLY, in THIS ns only. No
    #    Secrets, no ClusterRole, no other verbs/resources. create-or-reconcile so a
    #    tightened Role from a later rollout actually replaces an older one (P2).
    _apply(rbac_v1.create_namespaced_role, rbac_v1.patch_namespaced_role,
           SANDBOX_ROLE, ns, client.V1Role(
        metadata=client.V1ObjectMeta(name=SANDBOX_ROLE, namespace=ns, labels=labels),
        rules=[
            client.V1PolicyRule(
                api_groups=['batch'], resources=['jobs'],
                verbs=['create', 'get', 'list', 'watch', 'delete']),
            client.V1PolicyRule(
                api_groups=[''], resources=['pods'], verbs=['get', 'list', 'watch']),
            client.V1PolicyRule(
                api_groups=[''], resources=['pods/log'], verbs=['get']),
        ]))

    # 4. RoleBinding: runner SA -> Role (namespaced; cannot grant cluster scope).
    _apply(rbac_v1.create_namespaced_role_binding, rbac_v1.patch_namespaced_role_binding,
           SANDBOX_ROLE, ns, client.V1RoleBinding(
        metadata=client.V1ObjectMeta(name=SANDBOX_ROLE, namespace=ns, labels=labels),
        role_ref=client.V1RoleRef(
            api_group='rbac.authorization.k8s.io', kind='Role', name=SANDBOX_ROLE),
        subjects=[client.RbacV1Subject(kind='ServiceAccount', name=SANDBOX_SA, namespace=ns)]))

    # 5. Default-deny NetworkPolicy (all pods, both directions, no rules). A
    #    per-Job egress allowlist is layered on explicitly when an exec needs it.
    _apply(net_v1.create_namespaced_network_policy, net_v1.patch_namespaced_network_policy,
           'default-deny', ns, client.V1NetworkPolicy(
        metadata=client.V1ObjectMeta(name='default-deny', namespace=ns, labels=labels),
        spec=client.V1NetworkPolicySpec(
            pod_selector=client.V1LabelSelector(),  # {} = all pods
            policy_types=['Ingress', 'Egress'])))    # no ingress/egress rules = deny

    # 6. ResourceQuota — cap the ns so one agent's exec can't exhaust the pool.
    #    create-or-reconcile so tightened quotas take effect on an existing ns (P2).
    _apply(core_v1.create_namespaced_resource_quota, core_v1.patch_namespaced_resource_quota,
           'sandbox-quota', ns, client.V1ResourceQuota(
        metadata=client.V1ObjectMeta(name='sandbox-quota', namespace=ns, labels=labels),
        spec=client.V1ResourceQuotaSpec(hard={
            'pods': getattr(settings, 'FLEET_SANDBOX_MAX_PODS', '4'),
            'count/jobs.batch': getattr(settings, 'FLEET_SANDBOX_MAX_JOBS', '8'),
            'requests.cpu': getattr(settings, 'FLEET_SANDBOX_QUOTA_CPU', '4'),
            'requests.memory': getattr(settings, 'FLEET_SANDBOX_QUOTA_MEM', '8Gi'),
            'limits.cpu': getattr(settings, 'FLEET_SANDBOX_QUOTA_CPU', '4'),
            'limits.memory': getattr(settings, 'FLEET_SANDBOX_QUOTA_MEM', '8Gi'),
        })))

    # 7. LimitRange — per-container defaults so a Job without explicit resources
    #    still gets bounded (and the quota's requests.* is always satisfiable).
    _apply(core_v1.create_namespaced_limit_range, core_v1.patch_namespaced_limit_range,
           'sandbox-limits', ns, client.V1LimitRange(
        metadata=client.V1ObjectMeta(name='sandbox-limits', namespace=ns, labels=labels),
        spec=client.V1LimitRangeSpec(limits=[client.V1LimitRangeItem(
            type='Container',
            default={'cpu': DEFAULT_EXEC_CPU, 'memory': DEFAULT_EXEC_MEM},
            default_request={'cpu': '100m', 'memory': '128Mi'})])))

    logger.info('sandbox: ensured namespace %s for agent %s', ns, agent.agent_username)
    return ns


def build_exec_job(
    agent: FleetAgent, *, name: str, image: str, command=None, args=None,
    env=None, cpu: str = DEFAULT_EXEC_CPU, memory: str = DEFAULT_EXEC_MEM,
    active_deadline_seconds: int = DEFAULT_ACTIVE_DEADLINE_SECONDS,
) -> client.V1Job:
    """Build (do not create) the hardened gVisor exec Job spec — pure, testable.

    Security posture (HLD §11 R1): gVisor runtimeClass + non-root, no priv-esc,
    drop ALL caps, read-only rootfs (writable work via an ephemeral emptyDir),
    ephemeral storage only, hard deadline, no SA token, sandbox-node placement.
    """
    job_labels = _labels(agent, **{'shizuha.io/sandbox-job': _job_label(name)})
    env_vars = [client.V1EnvVar(name=k, value=str(v)) for k, v in (env or {}).items()]

    container = client.V1Container(
        name='exec',
        image=image,
        command=command,
        args=args,
        env=env_vars or None,
        resources=client.V1ResourceRequirements(
            requests={'cpu': '100m', 'memory': '128Mi', 'ephemeral-storage': '64Mi'},
            # ephemeral-storage limit (review P1): caps node-local disk for untrusted
            # exec so a write-heavy run can't drive the sandbox node to disk pressure.
            limits={'cpu': cpu, 'memory': memory,
                    'ephemeral-storage': getattr(settings, 'FLEET_SANDBOX_EPHEMERAL', DEFAULT_EXEC_EPHEMERAL)}),
        security_context=client.V1SecurityContext(
            allow_privilege_escalation=False,
            read_only_root_filesystem=True,
            run_as_non_root=True,
            run_as_user=1000,
            capabilities=client.V1Capabilities(drop=['ALL'])),
        volume_mounts=[
            client.V1VolumeMount(name='work', mount_path='/work'),
            client.V1VolumeMount(name='tmp', mount_path='/tmp'),
        ],
    )

    pod_spec = client.V1PodSpec(
        restart_policy='Never',
        runtime_class_name=sandbox_runtime_class(),
        service_account_name=SANDBOX_SA,
        automount_service_account_token=False,
        node_selector={SANDBOX_NODE_LABEL: SANDBOX_NODE_VALUE},
        tolerations=[client.V1Toleration(
            key=SANDBOX_NODE_LABEL, operator='Equal', value=SANDBOX_NODE_VALUE,
            effect='NoSchedule')],
        security_context=client.V1PodSecurityContext(
            run_as_non_root=True, run_as_user=1000, fs_group=1000,
            seccomp_profile=client.V1SeccompProfile(type='RuntimeDefault')),
        containers=[container],
        # sizeLimit on each emptyDir (review P1): bounds the writable scratch so
        # untrusted code can't fill node disk; counts against ephemeral-storage.
        volumes=[
            client.V1Volume(name='work', empty_dir=client.V1EmptyDirVolumeSource(
                size_limit=getattr(settings, 'FLEET_SANDBOX_EMPTYDIR', DEFAULT_EXEC_EMPTYDIR))),
            client.V1Volume(name='tmp', empty_dir=client.V1EmptyDirVolumeSource(
                size_limit=getattr(settings, 'FLEET_SANDBOX_EMPTYDIR', DEFAULT_EXEC_EMPTYDIR))),
        ],
    )

    return client.V1Job(
        metadata=client.V1ObjectMeta(
            name=name, namespace=sandbox_namespace(agent), labels=job_labels),
        spec=client.V1JobSpec(
            backoff_limit=0,
            active_deadline_seconds=active_deadline_seconds,
            ttl_seconds_after_finished=DEFAULT_TTL_AFTER_FINISHED,
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels=job_labels),
                spec=pod_spec)))


def _egress_policy(agent: FleetAgent, job_name: str, cidrs) -> client.V1NetworkPolicy:
    """Per-Job egress allowlist layered over the ns default-deny. Always includes
    DNS (UDP/TCP 53) so the allowed CIDRs are resolvable."""
    lbl = _job_label(job_name)
    job_labels = {'shizuha.io/sandbox-job': lbl}
    # DNS egress restricted to the cluster resolver (review P1): a port-53 rule with
    # no `to:` selector would permit DNS to ANY host, an exfil/tunneling channel for
    # untrusted code even when the allowlist is narrow. Scope it to kube-dns in
    # kube-system so only cluster DNS resolution is reachable.
    egress = [client.V1NetworkPolicyEgressRule(
        to=[client.V1NetworkPolicyPeer(
            namespace_selector=client.V1LabelSelector(
                match_labels={'kubernetes.io/metadata.name': 'kube-system'}),
            pod_selector=client.V1LabelSelector(
                match_labels={'k8s-app': 'kube-dns'}))],
        ports=[client.V1NetworkPolicyPort(protocol=p, port=53) for p in ('UDP', 'TCP')])]
    for cidr in cidrs:
        egress.append(client.V1NetworkPolicyEgressRule(
            to=[client.V1NetworkPolicyPeer(
                ip_block=client.V1IPBlock(cidr=cidr))]))
    return client.V1NetworkPolicy(
        metadata=client.V1ObjectMeta(
            name=f'egress-{job_name}', namespace=sandbox_namespace(agent),
            labels=_labels(agent, **{'shizuha.io/sandbox-job': lbl})),
        spec=client.V1NetworkPolicySpec(
            pod_selector=client.V1LabelSelector(match_labels=job_labels),
            policy_types=['Egress'],
            egress=egress))


def dispatch_exec_job(
    agent: FleetAgent, *, name: str, image: str, command=None, args=None,
    env=None, egress_cidrs=None, cpu: str = DEFAULT_EXEC_CPU,
    memory: str = DEFAULT_EXEC_MEM,
    active_deadline_seconds: int = DEFAULT_ACTIVE_DEADLINE_SECONDS,
) -> str:
    """Ensure the sandbox ns, then create the gVisor exec Job. Returns the Job name.

    ``egress_cidrs`` (optional) layers a per-Job egress allowlist over the ns
    default-deny; omit it for a no-network exec (the safe default).
    """
    if agent.sandbox_required is False:
        # Defensive: callers should only dispatch for sandbox-using workloads.
        logger.warning('sandbox: dispatching exec for agent %s not marked sandbox_required',
                       agent.agent_username)
    ns = ensure_sandbox_namespace(agent)
    core_v1, batch_v1, _rbac_v1, net_v1 = get_sandbox_clients()

    if egress_cidrs:
        _create_or_ignore(net_v1.create_namespaced_network_policy, ns,
                          _egress_policy(agent, name, egress_cidrs))

    job = build_exec_job(
        agent, name=name, image=image, command=command, args=args, env=env,
        cpu=cpu, memory=memory, active_deadline_seconds=active_deadline_seconds)
    batch_v1.create_namespaced_job(ns, job)
    logger.info('sandbox: dispatched exec Job %s/%s (runtimeClass=%s)',
                ns, name, sandbox_runtime_class())
    return name


def get_job_logs(agent: FleetAgent, job_name: str) -> str:
    """Read the exec Job's pod logs (best-effort; '' if no pod yet). Bounded by
    limit_bytes (review P2) so untrusted code printing huge output can't blow up the
    Django process's memory — activeDeadlineSeconds caps runtime, not output volume."""
    core_v1, _b, _r, _n = get_sandbox_clients()
    ns = sandbox_namespace(agent)
    pods = core_v1.list_namespaced_pod(
        ns, label_selector=f'shizuha.io/sandbox-job={_job_label(job_name)}')
    if not pods.items:
        return ''
    return core_v1.read_namespaced_pod_log(
        pods.items[0].metadata.name, ns,
        limit_bytes=getattr(settings, 'FLEET_SANDBOX_LOG_LIMIT_BYTES', SANDBOX_LOG_LIMIT_BYTES)) or ''


def reap_job(agent: FleetAgent, job_name: str) -> None:
    """Delete the Job and its pods (Background propagation), AND any per-Job egress
    NetworkPolicy (review P2) — leaving a stale egress-<job> policy would keep
    selecting (and over-permitting) a later same-named exec that wanted no network or
    a narrower allowlist. ttlSecondsAfterFinished also auto-reaps the Job; this is the
    explicit/early path."""
    _c, batch_v1, _r, net_v1 = get_sandbox_clients()
    ns = sandbox_namespace(agent)
    try:
        batch_v1.delete_namespaced_job(
            job_name, ns, propagation_policy='Background')
    except ApiException as e:
        if e.status != 404:
            raise
    try:
        net_v1.delete_namespaced_network_policy(f'egress-{job_name}', ns)
    except ApiException as e:
        if e.status != 404:
            raise


def teardown_sandbox_namespace(agent: FleetAgent) -> None:
    """Delete the whole per-agent sandbox ns (deprovision path). Reaps every Job,
    NetworkPolicy, SA, Role, quota — the entire blast-radius boundary at once."""
    core_v1, _b, _r, _n = get_sandbox_clients()
    ns = sandbox_namespace(agent)
    try:
        core_v1.delete_namespace(ns)
        logger.info('sandbox: deleted namespace %s', ns)
    except ApiException as e:
        if e.status != 404:
            raise
