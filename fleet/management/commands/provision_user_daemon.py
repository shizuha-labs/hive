"""PLAT-4235 P2: provision a per-user fleet daemon.

Operator 2026-07-12: "each user's Hive must have exactly one independent k3s
fleet daemon that manages that user's entire fleet."

Creates, for a Shizuha-ID user subject:
  1. A Hive ``DaemonCredential`` for ``rt-fleet-u<user_id>`` (token minted here,
     stored hashed; plaintext goes ONLY into the k8s Secret below).
  2. Namespace ``fleet-u<user_id>`` — the user's isolated agent namespace — plus
     a Role/RoleBinding letting the daemon ServiceAccount manage agent objects
     there (mirrors the org daemon's shizuha-fleet RBAC).
  3. Secret ``user-daemon-u<user_id>-link`` (rt-fleet ns) carrying the link token.
  4. Secret ``user-daemon-u<user_id>-agents`` (rt-fleet ns): the roster SEED —
     ``agents.json`` + ``enabled-agents.json`` built from THIS user's FleetAgent
     rows (``owner_id=<user_id>``). Mounted at ``/fleet-config``; the daemon copies
     it into its state dir before ``up`` so it spawns the user's fleet instead of
     an empty roster (the old emptyDir "only saw a POC agent" failure mode).
  5. Deployment ``fleet-daemon-u<user_id>`` (rt-fleet ns): k8s-backend daemon with
     an ISOLATED state dir (its own roster — SEEDED from #4; it must never read the
     org daemon's host ~/.shizuha roster or the two daemons would fight over the
     same agent Deployments). Carries the spawn-side env the k8s backend needs to
     render agent pods (agent-runtime/broker images, node selector, coordinator
     model-token URL, provisioner token, id/search/JWT config). Registers over the
     daemon-link with ``owner_subject=<user_id>`` so Hive scopes binding to that
     user's agents (INV-OWNER-SCOPE, hive b81d8df + cli cad14548).

The daemon binary is the same host-dist bind-mount the org daemon uses (pinned to
s1) — identical trust domain and code path; packaging it as a standalone OCI image
is the SCLI-331 "agent-runtime OCI CI" follow-up.

Dry-run by default; ``--apply`` mints + applies. Idempotent: re-running rotates
nothing unless ``--rotate-token``; the Deployment is re-applied in place.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets

from django.core.management.base import BaseCommand, CommandError

RT_FLEET_NS = 'rt-fleet'
DAEMON_SA = 'shizuha-runtime-fleet'  # existing SA the org daemon runs as
# The legacy host-dist lane needs an explicit host checkout path. Container
# layout is stable and independent of the operator account or source location.
HOST_CLI_MOUNT = '/opt/shizuha-runtime'


def runtime_host_cli():
    value = os.environ.get('HIVE_RUNTIME_HOST_CLI', '')
    if (not value.startswith('/') or value == '/' or '\n' in value or '\r' in value
            or any(part in ('.', '..', '') for part in value[1:].split('/'))):
        raise CommandError('HIVE_RUNTIME_HOST_CLI must name the explicit absolute host runtime checkout.')
    return value


def runtime_infrastructure():
    """Advanced host-dist provisioning requires an installation-owned contract."""
    images = {}
    for kind in ('BASE', 'BROKER'):
        name = 'HIVE_RUNTIME_' + kind + '_IMAGE'
        value = os.environ.get(name, '')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@+-]*', value):
            raise CommandError(name + ' must name an explicit container image reference.')
        images[kind.lower()] = value
    selectors = {}
    for kind in ('AGENT', 'DAEMON'):
        name = 'HIVE_RUNTIME_' + kind + '_NODE_SELECTOR'
        value = os.environ.get(name, '')
        items = {}
        for item in value.split(','):
            if item.count('=') != 1:
                raise CommandError(name + ' must contain explicit key=value node labels.')
            key, label = item.split('=')
            if (not re.fullmatch(r'(?:[a-z0-9][a-z0-9.-]*/)?[A-Za-z0-9][A-Za-z0-9_.-]{0,62}', key)
                    or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,62}', label)
                    or key in items):
                raise CommandError(name + ' contains an invalid or duplicate node label.')
            items[key] = label
        selectors[kind.lower()] = items
    return images, selectors

DAEMON_LINK_URL = 'ws://hive-direct.shizuha-hive.svc.cluster.local:8030/v1/fleet/daemon-link'
PLATFORM_URL = 'http://shizuha-nginx.shizuha.svc.cluster.local'

# ── Spawn-side config the k8s backend needs to render agent pods (GAP 1) ──
# The per-user daemon runs SHIZUHA_RUNTIME_BACKEND=k8s, so src/daemon/k8s-backend.ts
# reads these to render each per-agent Deployment. Values mirror the live rt-fleet
# manifest (deploy/k3s/manifests/shizuha-runtime-fleet.yaml) and the k8s-backend
# reference daemon (deploy/k3s/rt-fleet/k8s-fleet-daemon.yaml). Reviewed
# DesiredRuntimeRelease state owns live runtime adoption and supplies the
# per-user daemon's bootstrap image at apply time.
COORDINATOR_MODEL_TOKEN_URL = (
    'http://hive-direct.shizuha-hive.svc.cluster.local:8030/hive/api/v1/coordinator/model-token')
# Agent pods need a Ready, non-control-plane Longhorn node (k8s-fleet-daemon.yaml).
# Do NOT pin to a control-plane hostname or broad Longhorn labels while NotReady
# nodes are tolerated, or rollouts wedge at Pending/ProgressDeadlineExceeded.
# Daemon-owned platform config (mirrors shizuha-runtime-fleet.yaml env). The
# daemon uses ID_INTERNAL_URL + FLEET_PROVISIONER_TOKEN to mint per-agent
# @agents.shizuha.io identities (cli src/daemon/agent-accounts.ts).
ID_INTERNAL_URL = 'http://shizuha-id.shizuha.svc.cluster.local:8001'
SEARCH_BASE_URL = 'http://shizuha-search.search.svc.cluster.local'
# Provisioned privately in the daemon namespace and verified read-only by CI. No signing
# material belongs in generated Deployment env values or public source.
DEFAULT_JWT_SECRET_NAME = 'runtime-platform-jwt-v2'
DEFAULT_JWT_SECRET_KEY = 'jwt-secret'


def runtime_jwt_secret_ref():
    """Return a required Secret selector; Kubernetes fails closed if absent."""
    from kubernetes import client as k8s
    name = os.environ.get('USER_DAEMON_JWT_SECRET_NAME', DEFAULT_JWT_SECRET_NAME).strip()
    key = os.environ.get('USER_DAEMON_JWT_SECRET_KEY', DEFAULT_JWT_SECRET_KEY).strip()
    if not name or len(name) > 253 or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', name):
        raise CommandError('USER_DAEMON_JWT_SECRET_NAME must name a configured Kubernetes Secret')
    if not key or len(key) > 253 or not re.fullmatch(r'[A-Za-z0-9._-]+', key):
        raise CommandError('USER_DAEMON_JWT_SECRET_KEY must name a configured Secret key')
    return k8s.V1SecretKeySelector(name=name, key=key, optional=False)


# secretKeyRef target that already lives in the rt-fleet namespace (the org daemon
# Deployment in shizuha-runtime-fleet.yaml references this same Secret/key).
FLEET_PROVISIONER_TOKEN_SECRET = 'fleet-provisioner-token'
FLEET_PROVISIONER_TOKEN_KEY = 'token'
# GAP 2: the roster-seed Secret the daemon copies into its state dir before `up`.
# Statuses that mean "the daemon should spawn this agent" (mirrors
# fleet.runtime_fleet.RUNNING_STATUSES).
ROSTER_ENABLED_STATUSES = {'running', 'starting', 'provisioning', 'pending'}


def roster_secret_for(user_id: int) -> str:
    return f'user-daemon-u{user_id}-agents'


def daemon_id_for(user_id: int) -> str:
    return f'rt-fleet-u{user_id}'


def fleet_ns_for(user_id: int) -> str:
    return f'fleet-u{user_id}'


def _valid_seat_key(value: str) -> bool:
    """PLAT-8597 leg 1: a seat key must be DNS-safe so it can also name a Secret.

    Case is preserved verbatim — the seat's client polls with os.hostname()
    exactly as returned, and DaemonCredential.daemon_id lookups are
    case-sensitive.
    """
    if not value or len(value) > 253:
        return False
    return all(ch.isalnum() or ch in '.-' for ch in value)


class Command(BaseCommand):
    help = 'Provision the per-user fleet daemon for a Shizuha-ID user (PLAT-4235 P2). Dry-run unless --apply.'

    def add_arguments(self, parser):
        parser.add_argument('--user-id', type=int, required=True, help='Shizuha-ID user subject (owner).')
        parser.add_argument('--username', default='', help='Display handle for the owner (non-authoritative).')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--rotate-token', action='store_true', help='Mint a new link token even if one exists.')
        parser.add_argument('--daemon-id', default='', help='Override the DaemonCredential lookup key (PLAT-8597 leg 1: local seats send os.hostname() verbatim — pass the seat hostname so the minted credential matches what the client polls with).')
        parser.add_argument('--link-only', action='store_true', help='Mint ONLY the DaemonCredential + link Secret (PLAT-8597 leg 1: local dev machines — no agent namespace, RBAC, netpol, roster seed, or Deployment; those are the per-user daemon legs).')

    def handle(self, *args, **opts):
        user_id = opts['user_id']
        username = (opts['username'] or '').strip()
        apply = bool(opts['apply'])
        if apply and not opts['link_only']:
            runtime_host_cli()  # Refuse before any credential or cluster mutation.
            runtime_infrastructure()
        daemon_id_override = (opts['daemon_id'] or '').strip()
        if daemon_id_override and not _valid_seat_key(daemon_id_override):
            raise CommandError(
                f'--daemon-id {daemon_id_override!r} is not a valid seat key '
                '(expected DNS-safe [A-Za-z0-9.-], ≤253 chars)')
        did = daemon_id_override or daemon_id_for(user_id)
        ns = fleet_ns_for(user_id)
        secret_name = f'user-daemon-u{user_id}-link'
        deploy_name = f'fleet-daemon-u{user_id}'

        self.stdout.write(f'per-user daemon plan: daemon_id={did} owner_subject={user_id} '
                          f'agent-ns={ns} deploy={RT_FLEET_NS}/{deploy_name} '
                          f'roster-secret={RT_FLEET_NS}/{roster_secret_for(user_id)}')
        if not apply:
            if opts['link_only']:
                self.stdout.write(
                    f'[dry-run] link-only plan: DaemonCredential(daemon_id={did}) '
                    f'+ Secret {RT_FLEET_NS}/{secret_name} (token retrieved out-of-band)')
                self.stdout.write('[dry-run] pass --apply to provision')
                return
            try:
                agents, enabled_ids = self._build_roster(user_id)
                self.stdout.write(
                    f'[dry-run] roster seed: {len(agents)} agent(s) for owner {user_id}, '
                    f'{len(enabled_ids)} enabled ({", ".join(a["username"] for a in agents[:12])}'
                    f'{" …" if len(agents) > 12 else ""})')
            except Exception as e:  # pragma: no cover - dry-run preview is best-effort
                self.stdout.write(f'[dry-run] roster preview unavailable: {e}')
            self.stdout.write('[dry-run] pass --apply to provision')
            return

        from fleet.models import DaemonCredential
        from fleet.provisioner import get_k8s_client
        from fleet.runtime_release import (
            RuntimeReleaseProjectionUnavailable,
            get_authoritative_runtime_release,
        )
        from kubernetes import client as k8s
        from kubernetes.client.exceptions import ApiException

        # Resolve reviewed intent before minting credentials or creating any
        # cluster resource. Provisioning fails closed on stale/malformed state.
        try:
            runtime_image = get_authoritative_runtime_release().release.deployment_image
        except RuntimeReleaseProjectionUnavailable as exc:
            raise CommandError(f'reviewed runtime release unavailable: {exc}') from exc

        core_v1, apps_v1, _ = get_k8s_client()
        rbac_v1 = k8s.RbacAuthorizationV1Api(core_v1.api_client)
        networking_v1 = k8s.NetworkingV1Api(core_v1.api_client)

        # 1. Daemon-link credential (mint once; --rotate-token revokes + re-mints).
        existing = DaemonCredential.objects.filter(daemon_id=did, revoked=False).first()
        token = None
        if existing and not opts['rotate_token']:
            # Keep the existing credential; reuse the token already in the Secret.
            try:
                sec = core_v1.read_namespaced_secret(secret_name, RT_FLEET_NS)
                import base64 as _b64
                token = _b64.b64decode((sec.data or {}).get('token', '')).decode() or None
            except ApiException:
                token = None
            if token is None:
                raise CommandError(
                    f'active DaemonCredential exists for {did} but Secret {secret_name} is missing; '
                    'pass --rotate-token to mint a fresh pair')
            self.stdout.write(f'  credential: reusing active DaemonCredential for {did}')
        else:
            if existing:
                from django.utils import timezone
                existing.revoked = True
                existing.revoked_at = timezone.now()
                existing.save(update_fields=['revoked', 'revoked_at'])
            token = secrets.token_urlsafe(32)
            DaemonCredential.objects.create(daemon_id=did, token_hash=hashlib.sha256(token.encode()).hexdigest())
            self.stdout.write(f'  credential: minted DaemonCredential for {did}')

        if opts['link_only']:
            # PLAT-8597 leg 1: link-only seats (local dev machines) get ONLY the
            # DaemonCredential + link Secret — no agent namespace, RBAC, netpol,
            # roster seed, or Deployment (those are the per-user daemon legs).
            # The seat's client polls with os.hostname() as daemon_id, so pass
            # --daemon-id <hostname> to match; the operator retrieves the token
            # from the Secret and delivers it to the seat config out-of-band.
            self._apply_secret(core_v1, secret_name, token)
            self.stdout.write(self.style.SUCCESS(
                f'link-only seat {did} provisioned — token in Secret {RT_FLEET_NS}/{secret_name}; '
                'deliver it to the seat config out-of-band'))
            return

        # 2. User agent namespace + RBAC for the daemon SA + tenant-isolation NetworkPolicy.
        self._ensure_ns(core_v1, ns, user_id)
        self._ensure_rbac(rbac_v1, ns)
        self._ensure_netpol(networking_v1, ns)

        # 3. Link-token Secret.
        self._apply_secret(core_v1, secret_name, token)

        # 4. Roster-seed Secret (agents.json + enabled-agents.json for THIS user).
        self._apply_roster_secret(core_v1, roster_secret_for(user_id), user_id)

        # 5. Daemon Deployment.
        self._apply_deployment(
            apps_v1,
            deploy_name,
            did,
            ns,
            secret_name,
            user_id,
            username,
            runtime_image,
        )

        self.stdout.write(self.style.SUCCESS(
            f'per-user daemon {did} provisioned — watch DaemonRegistry for owner_subject={user_id} connected'))

    # ── k8s helpers ──────────────────────────────────────────────────────
    def _ensure_ns(self, core_v1, ns, user_id):
        from kubernetes import client as k8s
        from kubernetes.client.exceptions import ApiException
        try:
            core_v1.read_namespace(ns)
        except ApiException as e:
            if e.status != 404:
                raise
            core_v1.create_namespace(k8s.V1Namespace(metadata=k8s.V1ObjectMeta(
                name=ns, labels={'shizuha.io/user-fleet': str(user_id)})))
            self.stdout.write(f'  namespace {ns} created')

    def _ensure_rbac(self, rbac_v1, ns):
        from kubernetes import client as k8s
        from kubernetes.client.exceptions import ApiException
        role = k8s.V1Role(
            metadata=k8s.V1ObjectMeta(name='user-fleet-daemon', namespace=ns),
            rules=[
                k8s.V1PolicyRule(
                    api_groups=['', 'apps'],
                    resources=['deployments', 'pods', 'pods/log', 'pods/exec', 'secrets',
                               'persistentvolumeclaims', 'services'],
                    verbs=['get', 'list', 'watch', 'create', 'update', 'patch', 'delete'],
                ),
                # Tenant-isolation NetworkPolicies (reconcile/verify from the daemon SA).
                k8s.V1PolicyRule(
                    api_groups=['networking.k8s.io'],
                    resources=['networkpolicies'],
                    verbs=['get', 'list', 'watch', 'create', 'update', 'patch'],
                ),
            ],
        )
        binding = k8s.V1RoleBinding(
            metadata=k8s.V1ObjectMeta(name='user-fleet-daemon', namespace=ns),
            role_ref=k8s.V1RoleRef(api_group='rbac.authorization.k8s.io', kind='Role', name='user-fleet-daemon'),
            subjects=[k8s.RbacV1Subject(kind='ServiceAccount', name=DAEMON_SA, namespace=RT_FLEET_NS)],
        )
        for create, replace, obj, kind in (
            (rbac_v1.create_namespaced_role, rbac_v1.replace_namespaced_role, role, 'Role'),
            (rbac_v1.create_namespaced_role_binding, rbac_v1.replace_namespaced_role_binding, binding, 'RoleBinding'),
        ):
            try:
                create(ns, obj)
                self.stdout.write(f'  {kind} user-fleet-daemon created in {ns}')
            except ApiException as e:
                if e.status != 409:
                    raise
                replace('user-fleet-daemon', ns, obj)

    def _ensure_netpol(self, networking_v1, ns):
        """PLAT-4235 hardening (operator 2026-07-13): tenant-isolation NetworkPolicy
        set for a per-user agent namespace. Personal-agent egress = platform gateway
        + direct ID + Origin + package caches + hive coordinator + Cortex + search + DNS + public
        internet ONLY; NO lateral pod-to-pod reach and NO arbitrary internal services. Mirrors the live
        shizuha-fleet policy (k8s/fleet/30-fleet-isolation-netpol.yaml), minus the
        k8s-API allow (personal agents do no infra). Verified allowlist via the
        recon/ichi canaries. Idempotent (409 → replace)."""
        from kubernetes import client as k8s
        from kubernetes.client.exceptions import ApiException
        labels = {'app.kubernetes.io/managed-by': 'shizuha-hive', 'shizuha.io/policy': 'fleet-isolation'}
        agent_sel = k8s.V1LabelSelector(
            match_expressions=[k8s.V1LabelSelectorRequirement(key='shizuha.io/agent', operator='Exists')])

        def ns_peer(name, port):
            return k8s.V1NetworkPolicyEgressRule(
                to=[k8s.V1NetworkPolicyPeer(namespace_selector=k8s.V1LabelSelector(
                    match_labels={'kubernetes.io/metadata.name': name}))],
                ports=[k8s.V1NetworkPolicyPort(protocol='TCP', port=port)])

        def ns_pod_peer(name, match_labels, ports):
            return k8s.V1NetworkPolicyEgressRule(
                to=[k8s.V1NetworkPolicyPeer(
                    namespace_selector=k8s.V1LabelSelector(
                        match_labels={'kubernetes.io/metadata.name': name}),
                    pod_selector=k8s.V1LabelSelector(match_labels=match_labels),
                )],
                ports=[k8s.V1NetworkPolicyPort(protocol='TCP', port=port) for port in ports],
            )

        default_deny = k8s.V1NetworkPolicy(
            metadata=k8s.V1ObjectMeta(name='fleet-agent-default-deny', namespace=ns, labels=labels),
            spec=k8s.V1NetworkPolicySpec(pod_selector=agent_sel, policy_types=['Ingress', 'Egress']))
        allow_egress = k8s.V1NetworkPolicy(
            metadata=k8s.V1ObjectMeta(name='fleet-agent-allow-egress', namespace=ns, labels=labels),
            spec=k8s.V1NetworkPolicySpec(
                pod_selector=agent_sel, policy_types=['Egress'],
                egress=[
                    k8s.V1NetworkPolicyEgressRule(
                        to=[k8s.V1NetworkPolicyPeer(
                            namespace_selector=k8s.V1LabelSelector(match_labels={'kubernetes.io/metadata.name': 'kube-system'}),
                            pod_selector=k8s.V1LabelSelector(match_labels={'k8s-app': 'kube-dns'}))],
                        ports=[k8s.V1NetworkPolicyPort(protocol='UDP', port=53),
                               k8s.V1NetworkPolicyPort(protocol='TCP', port=53)]),
                    ns_peer('shizuha', 80),
                    ns_pod_peer('shizuha', {
                        'app.kubernetes.io/name': 'shizuha-id',
                        'app.kubernetes.io/component': 'backend',
                    }, [8001, 18102]),
                    ns_pod_peer('origin', {'app': 'forgejo'}, [3000, 22, 8080]),
                    ns_pod_peer('registry', {
                        'app.kubernetes.io/name': 'shizuha-registry',
                        'app.kubernetes.io/component': 'mirror-dockerhub',
                    }, [5000]),
                    ns_pod_peer('registry', {
                        'app.kubernetes.io/name': 'shizuha-registry',
                        'app.kubernetes.io/component': 'registry',
                    }, [5000]),
                    ns_pod_peer('registry', {'app': 'pip-cache'}, [80]),
                    ns_pod_peer('registry', {'app': 'apt-cache'}, [3142]),
                    # PLAT-5022: npm-cache was the one package cache never
                    # allowlisted, so every npm install/ci from an agent pod hit
                    # an instant ECONNREFUSED indistinguishable from the cache
                    # being down. Must stay in lockstep with the shizuha-fleet
                    # manifest (k8s/fleet/30-fleet-isolation-netpol.yaml).
                    ns_pod_peer('registry', {'app': 'npm-cache'}, [4873]),
                    # PLAT-4692: Alpine and raw-source build paths use the same
                    # package-cache boundary and must stay in lockstep with the
                    # shizuha-fleet policy. Exact app selectors avoid granting
                    # port 80 to the whole registry namespace.
                    ns_pod_peer('registry', {'app': 'apk-cache'}, [80]),
                    ns_pod_peer('registry', {'app': 'git-raw-cache'}, [80]),
                    ns_pod_peer('shizuha', {
                        'app.kubernetes.io/name': 'shizuha-edge',
                    }, [443]),
                    k8s.V1NetworkPolicyEgressRule(
                        to=[k8s.V1NetworkPolicyPeer(ip_block=k8s.V1IPBlock(
                            cidr='192.168.0.250/32'))],
                        ports=[k8s.V1NetworkPolicyPort(protocol='TCP', port=443)]),
                    ns_peer('shizuha-hive', 8030),
                    ns_peer('shizuha-cortex', 8040),
                    # Shizuha Search. NetworkPolicy is enforced AFTER kube-proxy
                    # DNAT, so the rule must name the pod's TARGET port, not the
                    # Service port. The Service is 80 -> targetPort 8090; the old
                    # `ns_peer('search', 80)` therefore never matched a single
                    # real packet — every agent's web_search got an instant
                    # connection-refused for as long as this policy has existed
                    # (verified from a live agent pod 2026-08-05: svc:80 and
                    # pod:8090 both rejected in <2ms while the same query
                    # returned results from the host). Port 80 stays for the
                    # day the Service targets 80 directly; 8090 is the one that
                    # matters.
                    ns_peer('search', 80),
                    ns_peer('search', 8090),
                    k8s.V1NetworkPolicyEgressRule(
                        to=[k8s.V1NetworkPolicyPeer(ip_block=k8s.V1IPBlock(
                            cidr='0.0.0.0/0',
                            _except=['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16', '100.64.0.0/10']))],
                        ports=[k8s.V1NetworkPolicyPort(protocol='TCP', port=443),
                               k8s.V1NetworkPolicyPort(protocol='TCP', port=80)]),
                ]))
        allow_ingress = k8s.V1NetworkPolicy(
            metadata=k8s.V1ObjectMeta(name='fleet-agent-allow-platform-ingress', namespace=ns, labels=labels),
            spec=k8s.V1NetworkPolicySpec(
                pod_selector=agent_sel, policy_types=['Ingress'],
                ingress=[
                    k8s.V1NetworkPolicyIngressRule(_from=[
                        k8s.V1NetworkPolicyPeer(namespace_selector=k8s.V1LabelSelector(match_labels={'kubernetes.io/metadata.name': 'shizuha'})),
                        k8s.V1NetworkPolicyPeer(namespace_selector=k8s.V1LabelSelector(match_labels={'kubernetes.io/metadata.name': 'shizuha-hive'})),
                    ]),
                    # CON-339/PLAT-6006: kube-prometheus Prometheus lives in
                    # `monitoring`, so every NetworkPolicy-admitted agent pod was
                    # unscrapeable on its runtime metrics port (PodMonitor
                    # monitoring/shizuha-agent-runtime-metrics). Lockstep with
                    # k8s/fleet/30-fleet-isolation-netpol.yaml. TCP/9103 only,
                    # scoped to the Prometheus workload; every other namespace,
                    # port and direction stays default-deny.
                    k8s.V1NetworkPolicyIngressRule(
                        _from=[k8s.V1NetworkPolicyPeer(
                            namespace_selector=k8s.V1LabelSelector(match_labels={'kubernetes.io/metadata.name': 'monitoring'}),
                            pod_selector=k8s.V1LabelSelector(match_labels={'app.kubernetes.io/name': 'prometheus'}),
                        )],
                        ports=[k8s.V1NetworkPolicyPort(protocol='TCP', port=9103)]),
                ]))
        for pol in (default_deny, allow_egress, allow_ingress):
            name = pol.metadata.name
            try:
                networking_v1.create_namespaced_network_policy(ns, pol)
                self.stdout.write(f'  NetworkPolicy {name} created in {ns}')
            except ApiException as e:
                if e.status != 409:
                    raise
                networking_v1.replace_namespaced_network_policy(name, ns, pol)

    def _apply_secret(self, core_v1, name, token):
        from kubernetes import client as k8s
        from kubernetes.client.exceptions import ApiException
        import base64 as _b64
        sec = k8s.V1Secret(
            metadata=k8s.V1ObjectMeta(name=name, namespace=RT_FLEET_NS,
                                      labels={'shizuha.io/component': 'user-daemon-link'}),
            data={'token': _b64.b64encode(token.encode()).decode()},
        )
        try:
            core_v1.create_namespaced_secret(RT_FLEET_NS, sec)
        except ApiException as e:
            if e.status != 409:
                raise
            core_v1.replace_namespaced_secret(name, RT_FLEET_NS, sec)

    # ── Roster seed (GAP 2) ─────────────────────────────────────────────
    def _build_roster(self, user_id):
        """Build (agents.json list, enabled-agents.json id list) from this user's
        FleetAgent rows. The camelCase entry shape mirrors the live host
        ~/.shizuha/agents.json roster AND fleet.runtime_fleet._fleet_agent_row_to_runtime
        (the canonical FleetAgent→runtime projection): the daemon's fleet loader keys
        on ``id``/``username``/``enabled`` and reads executionMethod, mcpServers,
        skills, model, modelOverrides, personalityTraits, contextPrompt,
        reasoningEffort, runtimeEnvironment and env. Best-effort per the task:
        unknown/absent fields fall back to safe defaults.
        """
        from fleet.models import FleetAgent
        from fleet.installation_config import effective_agent_env

        agents = []
        enabled_ids = []
        rows = (FleetAgent.objects
                .filter(owner_id=user_id)
                .exclude(status=FleetAgent.Status.DELETED)
                .order_by('agent_username'))
        for agent in rows:
            mcp_config = dict(agent.mcp_config or {})
            skill_config = dict(agent.skill_config or {})
            # mcpServers: prefer the fully-shaped list; else synthesize from the
            # enabled slug set (mirrors _fleet_agent_row_to_runtime), normalized to
            # the live agents.json server shape.
            raw_servers = mcp_config.get('mcp_servers')
            if not raw_servers:
                raw_servers = [{'slug': s, 'name': s}
                               for s in (mcp_config.get('enabled_mcp_server_ids') or []) if s]
            mcp_servers = [self._normalize_mcp_server(s) for s in raw_servers
                           if self._normalize_mcp_server(s) is not None]
            enabled = agent.status in ROSTER_ENABLED_STATUSES
            agent_ext_id = agent.agent_id or str(agent.id)
            entry = {
                'id': agent_ext_id,
                'platformUserId': agent.agent_id or '',
                'username': agent.agent_username,
                'name': agent.display_name or agent.agent_username,
                'email': (f'{agent.agent_username}@shizuha.com' if agent.agent_username else ''),
                'role': agent.role or 'Agent',
                'team': mcp_config.get('team') or '',
                'status': 'active' if enabled else 'inactive',
                'enabled': enabled,
                'model': agent.model or '',
                # Operator policy: NO per-agent model fallback anywhere (see FleetAgent.save).
                'modelFallbacks': [],
                'modelOverrides': agent.model_overrides or {},
                'executionMethod': mcp_config.get('execution_method') or 'shizuha',
                # This is a k8s-backend daemon, so agents render as k3s pods.
                'runtimeEnvironment': mcp_config.get('runtime_environment') or 'k8s',
                'reasoningEffort': agent.reasoning_effort or '',
                'skills': skill_config.get('skills') or [],
                'eagerSkills': skill_config.get('eager_skills') or [],
                'mcpServers': mcp_servers,
                'personalityTraits': agent.personality_traits or {},
                'contextPrompt': agent.context_prompt or '',
                'env': effective_agent_env(agent.env),
                'credentialGrantScopes': agent.credential_grant_scopes or [],
                'resourceLimits': agent.resource_overrides or {},
                'failoverChainId': '',
                'isLocal': False,
            }
            agents.append(entry)
            if enabled:
                enabled_ids.append(agent_ext_id)
        return agents, enabled_ids

    @staticmethod
    def _normalize_mcp_server(srv):
        """Coerce a stored MCP server into the live agents.json shape
        ({name, slug, command, args, env, transportType}). Accepts a dict or a
        bare slug string; returns None for anything without a name/slug."""
        if isinstance(srv, str):
            srv = {'slug': srv, 'name': srv}
        if not isinstance(srv, dict):
            return None
        slug = srv.get('slug') or srv.get('name')
        name = srv.get('name') or srv.get('slug')
        if not (slug or name):
            return None
        return {
            'name': name or slug,
            'slug': slug or name,
            'command': srv.get('command') or '',
            'args': srv.get('args') or [],
            'env': srv.get('env') or {},
            'transportType': srv.get('transportType') or srv.get('transport') or 'stdio',
        }

    def _apply_roster_secret(self, core_v1, name, user_id):
        from kubernetes import client as k8s
        from kubernetes.client.exceptions import ApiException
        agents, enabled_ids = self._build_roster(user_id)
        data = {
            'agents.json': base64.b64encode(
                json.dumps(agents, indent=2).encode()).decode(),
            'enabled-agents.json': base64.b64encode(
                json.dumps(enabled_ids, indent=2).encode()).decode(),
        }
        sec = k8s.V1Secret(
            metadata=k8s.V1ObjectMeta(name=name, namespace=RT_FLEET_NS,
                                      labels={'shizuha.io/component': 'user-daemon-roster',
                                              'shizuha.io/owner-subject': str(user_id)}),
            data=data,
        )
        try:
            core_v1.create_namespaced_secret(RT_FLEET_NS, sec)
            self.stdout.write(
                f'  roster secret {name} created ({len(agents)} agents, {len(enabled_ids)} enabled)')
        except ApiException as e:
            if e.status != 409:
                raise
            core_v1.replace_namespaced_secret(name, RT_FLEET_NS, sec)
            self.stdout.write(
                f'  roster secret {name} re-applied ({len(agents)} agents, {len(enabled_ids)} enabled)')

    def _apply_deployment(
        self,
        apps_v1,
        name,
        did,
        agent_ns,
        secret_name,
        user_id,
        username,
        runtime_image,
    ):
        from kubernetes import client as k8s
        from kubernetes.client.exceptions import ApiException
        host_cli = runtime_host_cli()
        images, selectors = runtime_infrastructure()
        env = [
            k8s.V1EnvVar(name='SHIZUHA_DAEMON_ID', value=did),
            k8s.V1EnvVar(name='SHIZUHA_FLEET_ID', value=f'user-{user_id}'),
            k8s.V1EnvVar(name='SHIZUHA_DAEMON_RUNTIME', value='k8s'),
            k8s.V1EnvVar(name='SHIZUHA_RUNTIME_BACKEND', value='k8s'),
            k8s.V1EnvVar(name='SHIZUHA_DAEMON_OWNER_SUBJECT', value=str(user_id)),
            *( [k8s.V1EnvVar(name='SHIZUHA_DAEMON_OWNER_USERNAME', value=username)] if username else [] ),
            k8s.V1EnvVar(name='SHIZUHA_FLEET_NAMESPACE', value=agent_ns),
            k8s.V1EnvVar(name='SHIZUHA_DAEMON_LINK_URL', value=DAEMON_LINK_URL),
            k8s.V1EnvVar(name='SHIZUHA_DAEMON_LINK_TOKEN', value_from=k8s.V1EnvVarSource(
                secret_key_ref=k8s.V1SecretKeySelector(name=secret_name, key='token'))),
            k8s.V1EnvVar(name='SHIZUHA_PLATFORM_SVC_URL', value=PLATFORM_URL),
            # ── GAP 1: spawn-side config the k8s backend needs to render agent pods ──
            # (src/daemon/k8s-backend.ts reads these). Mirrors the live rt-fleet
            # manifest + k8s-fleet-daemon.yaml. Plain values for images/URLs;
            # secretKeyRef for the provisioner token (Secret lives in rt-fleet ns).
            k8s.V1EnvVar(name='SHIZUHA_AGENT_RUNTIME_IMAGE', value=runtime_image),
            k8s.V1EnvVar(name='SHIZUHA_BROKER_IMAGE', value=images['broker']),
            k8s.V1EnvVar(name='SHIZUHA_COORDINATOR_MODEL_TOKEN_URL', value=COORDINATOR_MODEL_TOKEN_URL),
            k8s.V1EnvVar(name='SHIZUHA_FLEET_NODE_SELECTOR', value=','.join(key+'='+value for key,value in selectors['agent'].items())),
            k8s.V1EnvVar(name='SHIZUHA_ID_INTERNAL_URL', value=ID_INTERNAL_URL),
            k8s.V1EnvVar(name='SEARCH_BASE_URL', value=SEARCH_BASE_URL),
            k8s.V1EnvVar(name='JWT_SECRET_KEY', value_from=k8s.V1EnvVarSource(
                secret_key_ref=runtime_jwt_secret_ref())),
            k8s.V1EnvVar(name='FLEET_PROVISIONER_TOKEN', value_from=k8s.V1EnvVarSource(
                secret_key_ref=k8s.V1SecretKeySelector(
                    name=FLEET_PROVISIONER_TOKEN_SECRET, key=FLEET_PROVISIONER_TOKEN_KEY))),
            k8s.V1EnvVar(name='HOME', value='/home/agent'),
            k8s.V1EnvVar(name='PATH', value='/kb:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'),
        ]
        pod = k8s.V1PodSpec(
            service_account_name=DAEMON_SA,
            node_selector=selectors['daemon'],  # Explicit placement for the host-dist bind mount.
            tolerations=[k8s.V1Toleration(
                key='node.shizuha/workstation', operator='Equal', value='true', effect='NoSchedule')],
            init_containers=[k8s.V1Container(
                name='kubectl-cli', image='alpine/k8s:1.30.0',
                command=['sh', '-c', 'cp $(command -v kubectl) /kb/kubectl && chmod 0755 /kb/kubectl'],
                volume_mounts=[k8s.V1VolumeMount(name='kubectl-bin', mount_path='/kb')],
            )],
            containers=[k8s.V1Container(
                name='daemon', image=images['base'],
                # GAP 2: seed the ISOLATED state dir from the roster Secret mounted at
                # /fleet-config BEFORE `up` (mirrors k8s-fleet-daemon.yaml), so the
                # daemon spawns THIS user's fleet instead of an empty roster.
                command=['sh', '-c'],
                args=[
                    'set -e\n'
                    'export PATH=/kb:$PATH\n'
                    'mkdir -p /home/agent/.shizuha\n'
                    '[ -f /fleet-config/agents.json ] && cp /fleet-config/*.json /home/agent/.shizuha/ || true\n'
                    f'exec node {HOST_CLI_MOUNT}/dist/shizuha.js up --foreground --no-service '
                    f'--platform {PLATFORM_URL}\n'
                ],
                env=env,
                volume_mounts=[
                    k8s.V1VolumeMount(name='host-cli', mount_path=HOST_CLI_MOUNT, read_only=True),
                    # ISOLATED per-user state, SEEDED from the roster Secret on boot.
                    # Never mount the host ~/.shizuha here — two daemons over one
                    # roster would fight over the same agent Deployments.
                    k8s.V1VolumeMount(name='state', mount_path='/home/agent/.shizuha'),
                    # Read-only roster seed (agents.json + enabled-agents.json).
                    k8s.V1VolumeMount(name='fleet-config', mount_path='/fleet-config', read_only=True),
                    k8s.V1VolumeMount(name='kubectl-bin', mount_path='/kb'),
                ],
                resources=k8s.V1ResourceRequirements(
                    requests={'cpu': '100m', 'memory': '256Mi'},
                    limits={'cpu': '1', 'memory': '1Gi'}),
            )],
            volumes=[
                k8s.V1Volume(name='host-cli', host_path=k8s.V1HostPathVolumeSource(
                    path=host_cli, type='Directory')),
                k8s.V1Volume(name='state', empty_dir=k8s.V1EmptyDirVolumeSource()),
                # Roster seed built by _apply_roster_secret; optional so the daemon
                # still boots (empty roster) if the Secret is briefly absent.
                k8s.V1Volume(name='fleet-config', secret=k8s.V1SecretVolumeSource(
                    secret_name=roster_secret_for(user_id), optional=True)),
                k8s.V1Volume(name='kubectl-bin', empty_dir=k8s.V1EmptyDirVolumeSource()),
            ],
        )
        dep = k8s.V1Deployment(
            metadata=k8s.V1ObjectMeta(
                name=name, namespace=RT_FLEET_NS,
                labels={'app': name, 'shizuha.io/component': 'user-fleet-daemon',
                        'shizuha.io/owner-subject': str(user_id)}),
            spec=k8s.V1DeploymentSpec(
                replicas=1,
                strategy=k8s.V1DeploymentStrategy(type='Recreate'),
                selector=k8s.V1LabelSelector(match_labels={'app': name}),
                template=k8s.V1PodTemplateSpec(
                    metadata=k8s.V1ObjectMeta(labels={'app': name,
                                                      'shizuha.io/component': 'user-fleet-daemon'}),
                    spec=pod),
            ),
        )
        try:
            apps_v1.create_namespaced_deployment(RT_FLEET_NS, dep)
            self.stdout.write(f'  Deployment {name} created')
        except ApiException as e:
            if e.status != 409:
                raise
            apps_v1.replace_namespaced_deployment(name, RT_FLEET_NS, dep)
            self.stdout.write(f'  Deployment {name} re-applied')
