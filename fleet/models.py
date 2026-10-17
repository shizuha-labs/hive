"""
Hive Fleet models — the **declarative** layer for the k3s-native agent runtime.

HLD PLAT-147 v0.2 (EPIC PLAT-146), §10: model ``FleetAgent`` as a clean
declarative spec *now*, consumed *imperatively* by the generalized provisioner
(``fleet/provisioner.py``). A CRD + reconcile controller (phase B) becomes an
additive loop over these same objects later — "declarative data model now,
imperative actuation first, controller later" (A→B-phased).

A ``FleetAgent`` describes ONE fleet agent that runs as a native k3s pod
(one Deployment, ``replicas=1``, ``Recreate``) — NOT a tenant claw and not a
hand-written Kubernetes manifest. It carries everything the provisioner needs
to emit the per-agent Namespace/Secret/PVC/Deployment(/Service) defined in HLD
§4. Optional capabilities such as Docker are declared in ``sidecar_set`` and
actuated by the provisioner.

Scope note (additive): provisioning a ``FleetAgent`` never touches the existing
rt-fleet host-docker runtime; native pods run side-by-side until cutover.
"""
import hashlib
import json
import uuid
import re
import unicodedata
from datetime import timezone as datetime_timezone
from urllib.parse import urlparse

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone

from .pending_config import validate_pending_config_mapping

# DNS-1123 label: lowercase alnum + '-', start/end alnum, ≤63 chars. The username
# drives k8s object/label names (agent-<username>), so an invalid value would only
# fail later at apply-time — validate it at persist instead (review #5).
DNS1123_LABEL_RE = r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$'
dns1123_validator = RegexValidator(
    regex=DNS1123_LABEL_RE,
    message='agent_username must be a DNS-1123 label: lowercase alphanumeric or "-", '
            'starting and ending alphanumeric (e.g. "ichi", "dev-agent").',
)

CAPABILITY_SLUG_RE = r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$'
capability_slug_validator = RegexValidator(
    regex=CAPABILITY_SLUG_RE,
    message='slug must be kebab-case lowercase alphanumeric/hyphen.',
)


DEFAULT_TEAM_CAPABILITY_SPECS = {
    'accounting': {
        'display_name': 'Accounting',
        'risk_tier': 'standard',
        'skill_refs': ['company-os'],
        # HIVE-331/PLAT-1274: accounting agents need books+drive; without this the
        # spec fell back to DEFAULT_TEAM_MCP_SERVER_REFS and re-creating the team
        # would clobber the capability back to ['pulse'].
        'mcp_server_refs': ['pulse', 'connect', 'wiki', 'admin', 'id', 'books', 'drive'],
    },
    'admin-ops': {
        'display_name': 'Admin Ops',
        'risk_tier': 'sensitive',
        'skill_refs': ['chief-of-staff', 'company-os', 'org-structural-execution', 'task-routing'],
        'sensitive_access_flags': ['admin_org'],
    },
    'analytics': {'display_name': 'Analytics', 'risk_tier': 'standard', 'skill_refs': ['symptom-verification', 'shizuha-wiki']},
    'architecture': {'display_name': 'Architecture', 'risk_tier': 'standard', 'skill_refs': ['adr', 'wiki-lifecycle', 'event-driven-over-sweeps']},
    'ceo-office': {
        'display_name': 'CEO Office',
        'risk_tier': 'sensitive',
        'skill_refs': ['personal-assistant', 'company-os', 'operator-request-hygiene'],
        # Operator 2026-08-16: talk seats are proper agents. Same platform
        # floor as other teams plus Hive so they can list/inspect fleet agents.
        'mcp_server_refs': ['pulse', 'connect', 'wiki', 'admin', 'id', 'hive'],
        # Operator 2026-08-15: live CEO conversation / home voice. Must stay
        # in sync with on_demand.KEEP_RESIDENT_TEAM_SLUGS.
        'keep_resident': True,
        'lean_conversational': True,
    },
    'customer-support': {'display_name': 'Customer Support', 'risk_tier': 'standard', 'skill_refs': ['connect-messaging', 'personal-assistant']},
    'design': {'display_name': 'Design', 'risk_tier': 'standard', 'skill_refs': ['wiki-lifecycle', 'design-system', 'ux-heuristics', 'design-critique']},
    'devops': {
        'display_name': 'DevOps',
        'risk_tier': 'privileged',
        'skill_refs': ['devops', 'kubernetes', 'runbook', 'safe-operations', 'live-verification', 'cluster-scheduling'],
        'sensitive_access_flags': ['host_exec', 'credential_broker', 'kubeconfig'],
        'default_credential_grant_scopes': ['kubeconfig'],
    },
    'documentation': {'display_name': 'Documentation', 'risk_tier': 'standard', 'skill_refs': ['shizuha-wiki', 'wiki-lifecycle']},
    'engineering': {
        'display_name': 'Engineering',
        'risk_tier': 'standard',
        # agents-md-shipping is an agents_md directive (shipping always-on for authors).
        'skill_refs': [
            'engineering-core', 'fix-root-cause', 'github', 'shipping-prs',
            'agents-md-shipping', 'primary-branch-workflow', 'live-verification', 'e2e-testing',
        ],
    },
    'governance': {'display_name': 'Governance', 'risk_tier': 'standard', 'skill_refs': ['company-os', 'org-structural-execution', 'chief-of-staff']},
    'human-ops': {'display_name': 'Human Ops', 'risk_tier': 'standard', 'skill_refs': ['people-ops', 'personal-assistant']},
    'it': {'display_name': 'IT', 'risk_tier': 'standard', 'skill_refs': ['safe-operations', 'ssh-tunnels', 'network-devices']},
    'legal': {'display_name': 'Legal', 'risk_tier': 'standard', 'skill_refs': ['company-os', 'shizuha-wiki']},
    'marketing': {
        'display_name': 'Marketing',
        'risk_tier': 'standard',
        # Human-browser stealth + X account safety: always-on for any seat that
        # posts or browses social surfaces (operator 2026-07-29: stop skill drift
        # from hand-patching agents — capability grant is Hive SoT).
        'skill_refs': [
            'x-twitter', 'connect-messaging',
            'human-browser-stealth', 'x-account-safety',
        ],
        'eager_skill_refs': [
            'human-browser-stealth', 'x-account-safety', 'x-twitter',
        ],
    },
    'merge': {
        'display_name': 'Merge',
        'risk_tier': 'sensitive',
        # Merge-seat only pack (not first-pass review). agents_md directives compose
        # into AGENTS.md via pulse-merge.
        'skill_refs': ['pulse-merge'],
        'sensitive_access_flags': ['repo_write'],
        'default_credential_grant_scopes': ['github:merge'],
    },
    'people': {'display_name': 'People', 'risk_tier': 'standard', 'skill_refs': ['people-ops']},
    'platform': {'display_name': 'Platform', 'risk_tier': 'standard', 'skill_refs': ['framework-maintainer', 'devops', 'kubernetes', 'cluster-scheduling']},
    'product': {'display_name': 'Product', 'risk_tier': 'standard', 'skill_refs': ['enhancement-task-authoring', 'task-routing', 'company-os', 'product-discovery', 'product-analytics', 'experimentation']},
    'program-management': {'display_name': 'Program Management', 'risk_tier': 'standard', 'skill_refs': ['chief-of-staff', 'task-routing', 'company-os']},
    'qa': {'display_name': 'QA', 'risk_tier': 'standard', 'skill_refs': ['qa-verification', 'e2e-testing', 'symptom-verification', 'live-verification']},
    'research': {'display_name': 'Research', 'risk_tier': 'standard', 'skill_refs': ['web-search', 'summarize-url', 'shizuha-wiki', 'wiki-lifecycle', 'user-research', 'research-repository']},
    'research-analytics': {
        'display_name': 'Research',
        'risk_tier': 'standard',
        'skill_refs': [
            'web-search', 'summarize-url', 'shizuha-wiki', 'wiki-lifecycle',
            'user-research', 'research-repository', 'symptom-verification',
        ],
    },
    'review': {
        'display_name': 'Review',
        'risk_tier': 'standard',
        # Review-seat only: code-reviewer + pulse-review (agents_md directives).
        # Merge is pulse-merge on the merge capability — not shared.
        # MCP floor still includes admin via BASE_PLATFORM_MCP_SERVERS.
        'skill_refs': ['code-reviewer', 'pulse-review'],
        'default_credential_grant_scopes': ['github:review'],
    },
    'sales': {'display_name': 'Sales', 'risk_tier': 'standard', 'skill_refs': ['personal-assistant', 'connect-messaging']},
    'security': {
        'display_name': 'Security',
        'risk_tier': 'sensitive',
        'skill_refs': ['pulse-security', 'defer-exposed-token-rotation', 'no-blind-eye', 'github'],
        'sensitive_access_flags': ['security_review'],
    },
    'social-media': {
        'display_name': 'Social Media',
        'risk_tier': 'standard',
        # Capability grant is the only durable skill SoT for team seats.
        # Reconcile overwrites agent skill_config from this bundle — do not
        # hand-patch FleetAgent.skill_config for Nori (it drifts on next pass).
        'skill_refs': [
            'x-twitter', 'connect-messaging',
            'human-browser-stealth', 'x-account-safety',
        ],
        'eager_skill_refs': [
            'human-browser-stealth', 'x-account-safety', 'x-twitter',
        ],
        # Browser is a private per-agent MCP rather than a platform-global
        # endpoint, but it still belongs in the effective capability allowlist.
        # Otherwise the daemon's reviewed grant excludes the browser bridge even
        # when Hive has provisioned the worker and projected its URL.
        'mcp_server_refs': ['browser'],
    },
}

# PLAT-1251 canonical contract: every enabled fleet agent always has the base
# platform MCP set. The capability system may ADD MCP servers; it must never be
# able to remove these (floor enforced in fleet.views._effective_capability_set).
# CEO Office (operator 2026-08-16) uses that same floor plus Hive.
BASE_PLATFORM_MCP_SERVERS = ('pulse', 'connect', 'wiki', 'admin', 'id')
# Pulse/Hive skill floor: heartbeat-protocol is a mandatory inline skill for
# every seat, independent of team capability. Capability grants may ADD skills;
# they must never drop this floor (compute-layer restore in
# compute_effective_capability_grant). The skill body requires
# pulse_get_my_alerts then pulse_get_my_tasks on every heartbeat.
BASE_PLATFORM_SKILLS = ('heartbeat-protocol',)
BASE_PLATFORM_EAGER_SKILLS = BASE_PLATFORM_SKILLS
LEAN_CONVERSATIONAL_TEAM_SLUGS = frozenset({'ceo-office'})
LEAN_CONVERSATIONAL_MCP_SERVERS = ('pulse', 'connect', 'wiki', 'admin', 'id', 'hive')
LEAN_TRIMMABLE_PLATFORM_MCP = frozenset()
LEAN_CONVERSATIONAL_USERNAMES = frozenset({'hina', 'aya', 'yuna', 'ena'})

DEFAULT_TEAM_MCP_SERVER_REFS = list(BASE_PLATFORM_MCP_SERVERS)


def platform_mcp_floor_for_teams(team_slugs):
    """Return the non-removable MCP floor for a grant's team set.

    Exclusive CEO Office membership uses the talk-seat floor (platform base
    plus Hive). Mixed/unknown teams keep the historical PLAT-1251 base.
    """
    slugs = [str(s).strip().lower() for s in (team_slugs or []) if str(s).strip()]
    if slugs and all(slug in LEAN_CONVERSATIONAL_TEAM_SLUGS for slug in slugs):
        return tuple(LEAN_CONVERSATIONAL_MCP_SERVERS)
    return tuple(getattr(settings, 'HIVE_BASE_PLATFORM_MCP_SERVERS', BASE_PLATFORM_MCP_SERVERS))


def trim_lean_conversational_mcp(refs):
    """Keep lean core + operator extras; drop historical floor bloat."""
    extras = []
    for ref in refs or []:
        value = str(ref or '').strip()
        if not value or value in LEAN_TRIMMABLE_PLATFORM_MCP or value in extras:
            continue
        extras.append(value)
    out = list(LEAN_CONVERSATIONAL_MCP_SERVERS)
    for ref in extras:
        if ref not in out:
            out.append(ref)
    return out


def _team_mcp_server_refs(spec, slug=None):
    """Every team baseline keeps the core platform MCP contract.

    Pulse is the queue/state path; Connect is the only outbound messaging path;
    Wiki/Admin/ID are the shared org context/identity surfaces.  A team-specific
    MCP list may add more services, but it must not drop the base set and strand
    agents with a pulse-only tool registry again (PLAT-1251).

    CEO Office keeps the platform base plus Hive.
    """
    team_key = str(slug or spec.get('slug') or '').strip().lower()
    if team_key in LEAN_CONVERSATIONAL_TEAM_SLUGS or spec.get('lean_conversational'):
        refs = list(LEAN_CONVERSATIONAL_MCP_SERVERS)
        for ref in spec.get('mcp_server_refs') or []:
            if ref not in refs:
                refs.append(ref)
        return refs
    refs = list(DEFAULT_TEAM_MCP_SERVER_REFS)
    for ref in spec.get('mcp_server_refs') or []:
        if ref not in refs:
            refs.append(ref)
    return refs


def _json_list_default():
    return []


def _json_dict_default():
    return {}


def _definition_hash(payload) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest()


def _default_platform_url():
    # Track the deployment's configured platform URL rather than a hard-coded IP
    # (review #6). Callable default so it resolves per-instance at creation.
    return getattr(settings, 'SHIZUHA_PLATFORM_URL', 'http://100.64.0.3')


def _default_grant_scopes():
    return []


def _default_node_selector():
    return {}


def _default_tolerations():
    return []


def _default_sidecar_set():
    # ADR-PLAT-002 broker sidecar is the standard companion (HLD §4-§5). It is
    # listed declaratively so phase-B reconcile / alternate sidecar sets are an
    # additive change, not a schema change.
    return ['broker']


_CREDENTIAL_HANDLE_SCHEMES = frozenset({'broker', 'vault', 'k8s-secret'})
_PLAINTEXT_SECRET_PREFIXES = (
    'ghp_', 'gho_', 'ghu_', 'ghs_', 'ghr_', 'github_pat_',
    'sk-', 'xoxb-', 'xoxp-', 'glpat-', 'pat_', 'eyJ',
)
_JWT_RE = re.compile(r'^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$')
_HIGH_ENTROPY_RE = re.compile(r'^[A-Za-z0-9_+/=-]{32,}$')
_FINGERPRINT_RE = re.compile(
    r'^(?:'
    r'[A-Za-z0-9_.-]{2,24}\.\.\.[A-Za-z0-9_-]{4,16}|'
    r'last4:[A-Za-z0-9_-]{4,16}|'
    r'sha256:[A-Fa-f0-9]{12,64}|'
    r'redacted:[A-Za-z0-9_-]{4,32}'
    r')$'
)


def _looks_like_plaintext_secret(value: str) -> bool:
    candidate = (value or '').strip()
    if not candidate:
        return False
    lowered = candidate.lower()
    if '-----begin ' in lowered or '-----end ' in lowered:
        return True
    if candidate.startswith(_PLAINTEXT_SECRET_PREFIXES):
        return True
    if _JWT_RE.match(candidate):
        return True
    return bool(_HIGH_ENTROPY_RE.match(candidate) and '://' not in candidate)


def validate_credential_secret_ref(value: str) -> str:
    """Accept only non-secret broker/vault/k8s handles, never raw credentials."""
    candidate = (value or '').strip()
    if not candidate:
        raise ValidationError('broker/vault/k8s secret_ref is required; plaintext storage is not allowed')
    if _looks_like_plaintext_secret(candidate):
        raise ValidationError('secret_ref looks like plaintext credential material; store it in the broker/vault/k8s Secret and pass only the handle')
    parsed = urlparse(candidate)
    if parsed.scheme not in _CREDENTIAL_HANDLE_SCHEMES:
        raise ValidationError('secret_ref must use an allowed handle scheme: broker://, vault://, or k8s-secret://')
    if not parsed.netloc:
        raise ValidationError('secret_ref must include a handle target')
    return candidate


def validate_credential_fingerprint(value: str) -> str:
    """Fingerprints are read back, so accept only short redacted forms."""
    candidate = (value or '').strip()
    if not candidate:
        return ''
    if _FINGERPRINT_RE.match(candidate):
        return candidate
    if _looks_like_plaintext_secret(candidate):
        raise ValidationError('fingerprint looks like plaintext credential material; use a redacted form such as ghp_...abcd or sha256:<12-64 hex>')
    if len(candidate) > 80:
        raise ValidationError('fingerprint must be redacted: <prefix>...<last4>, last4:<last4>, redacted:<id>, or sha256:<12-64 hex>')
    raise ValidationError('fingerprint must be redacted: <prefix>...<last4>, last4:<last4>, redacted:<id>, or sha256:<12-64 hex>')


class FleetAgent(models.Model):
    """
    Declarative spec for one k3s-native fleet agent (HLD §4/§10).

    The agent's ``agent_username`` is the stable, DNS-1123 handle that drives all
    k8s resource names (``agent-<username>``), so it must be unique and lowercase.
    The agent's platform identity (``agent_id``) is non-secret env; the durable
    login credential (AGENT_PASSWORD) is NEVER stored on this row — it lives only
    in the per-agent identity Secret, mounted only into the broker sidecar (§5).
    """

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'                  # row created, not yet provisioned
        PROVISIONING = 'provisioning', 'Provisioning'   # k8s resources being created
        RUNNING = 'running', 'Running'                   # Deployment scaled to 1
        HIBERNATED = 'hibernated', 'Hibernated'          # policy scale-to-zero; auto-resumable
        STOPPED = 'stopped', 'Stopped'                   # explicit operator disable; never auto-wake
        DELETING = 'deleting', 'Deleting'               # teardown in progress
        FAILED = 'failed', 'Failed'                      # provisioning/runtime error
        DELETED = 'deleted', 'Deleted'                   # fully torn down
        BLOCKED = 'blocked', 'Blocked'                   # gated (e.g. sandbox_required, R1) — not provisioned

    class ResourceProfile(models.TextChoices):
        # Named profiles → requests/limits resolved in the provisioner from
        # settings.FLEET_RESOURCE_PROFILES, so capacity policy (HLD §3, R2) is a
        # config change, not a code change. ``resource_overrides`` can pin exact
        # values for a special agent.
        SMALL = 'small', 'Small'
        STANDARD = 'standard', 'Standard'
        HEAVY = 'heavy', 'Heavy'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # ── Identity (HLD §5: identity is non-secret; the password is not here) ──
    # max_length 47 = the k8s budget (review P2): the longest derived name is
    # `agent-<username>-workspace` (6 + len + 10), which must stay ≤63.
    agent_username = models.CharField(
        max_length=47, unique=True, db_index=True, validators=[dns1123_validator],
        help_text='DNS-1123 handle, ≤47 chars; drives k8s names (agent-<username>-*, ≤63).',
    )
    agent_id = models.CharField(
        max_length=64, blank=True,
        help_text='Platform AGENT_ID (non-secret env on the agent container).',
    )
    display_name = models.CharField(max_length=150, blank=True)
    role = models.CharField(max_length=100, blank=True)
    # HIVE-620/HIVE-644: immutable Shizuha-ID owner SUBJECT. Mutable email,
    # username, and display name are never authority. NULL preserves existing
    # org/team fleet agents; a value marks the one personal Shizuha object.
    owner_id = models.BigIntegerField(
        null=True, blank=True, default=None, db_index=True,
        help_text='Immutable Shizuha-ID owner subject; NULL means an org/team fleet agent.',
    )
    owner_username = models.CharField(
        max_length=150, blank=True, default='',
        help_text='Non-authoritative owner display handle; owner_id is the stable key.',
    )
    owner_profile_id = models.BigIntegerField(
        null=True, blank=True, default=None, db_index=True,
        help_text='Exact immutable Shizuha-ID UserProfile row for owner_id.',
    )
    identity_user_id = models.BigIntegerField(
        null=True, blank=True, default=None, unique=True,
        help_text='Exact dedicated Shizuha-ID user row for this agent identity.',
    )
    identity_profile_id = models.BigIntegerField(
        null=True, blank=True, default=None, unique=True,
        help_text='Exact dedicated Shizuha-ID UserProfile row for this agent identity.',
    )
    identity_provisioned_at = models.DateTimeField(
        null=True, blank=True,
        help_text='When the dedicated @agents.shizuha.io identity was confirmed.',
    )
    # HIVE-70: MODEL env var injected into the agent container so the runtime
    # uses the configured model rather than the hardcoded DEFAULT_CORTEX_MODEL.
    model = models.CharField(
        max_length=200, blank=True,
        help_text='MODEL env var for the agent runtime (e.g. cortex/GLM-5.2). Blank → runtime default.',
    )

    # ── Image / runtime (HLD §3-§4; multi-arch shizuha-agent-runtime, PLAT-150) ──
    image = models.CharField(
        max_length=300, blank=True,
        help_text='Container image; blank → settings.FLEET_AGENT_IMAGE default.',
    )
    image_pull_policy = models.CharField(max_length=20, default='IfNotPresent')

    # ── Resource profile (HLD §3 requests/limits, R2 capacity) ──
    resource_profile = models.CharField(
        max_length=20, choices=ResourceProfile.choices, default=ResourceProfile.STANDARD,
    )
    resource_overrides = models.JSONField(
        default=dict, blank=True,
        help_text='Optional {requests:{cpu,memory}, limits:{cpu,memory}} override.',
    )

    # ── Node / arch placement policy (HLD §3) ──
    arch = models.CharField(
        max_length=16, default='arm64',
        help_text='kubernetes.io/arch nodeSelector value (v1: arm64 Longhorn pool).',
    )
    requires_longhorn = models.BooleanField(
        default=True,
        help_text='Pin to storage.longhorn.io/ready nodes (workspace durability, §5).',
    )
    node_selector_overrides = models.JSONField(default=_default_node_selector, blank=True)
    node_pool = models.CharField(
        max_length=100, blank=True,
        help_text='Optional node-pool selector value; emits shizuha.io/node-pool=<value>.',
    )
    node_name = models.CharField(
        max_length=100, blank=True,
        help_text='Optional hard node pin; emits kubernetes.io/hostname=<value> (e.g. s1).',
    )
    tolerations = models.JSONField(
        default=_default_tolerations, blank=True,
        help_text='Optional Kubernetes tolerations list for this agent (dicts with key/operator/value/effect).',
    )

    # ── Namespace model (HLD §3 Q1: shared shizuha-fleet; DevOps isolated, Q1b) ──
    isolated = models.BooleanField(
        default=False,
        help_text='DevOps SSH-bearing agents (ichi/ni) get their own ns/RBAC (Q1b/R3).',
    )
    namespace = models.CharField(
        max_length=100, blank=True,
        help_text='Resolved on save: shared fleet ns, or fleet-<username> when isolated.',
    )
    # PLAT-4729/PLAT-6041: designation for the privileged host-plane SSH egress
    # class in the SHARED namespace. Deliberately NOT `isolated`: that field
    # relocates the namespace and is immutable after provisioning (below), so it
    # cannot designate an already-running seat. This one is mutable by design —
    # it only adds the shizuha.io/agent-role=devops-ssh pod label that
    # k8s/fleet/30-fleet-isolation-netpol.yaml's
    # fleet-allow-hostplane-ssh-egress-v2 selects, and moves no k8s resource.
    # Flipping it on a provisioned seat needs `backfill_hostplane_ssh_label`,
    # because pod-template labels are written only at provision time.
    hostplane_ssh = models.BooleanField(
        default=False,
        help_text='Designated host-plane SSH seat (ichi/ni class): adds the '
                  'shizuha.io/agent-role=devops-ssh pod label selected by the '
                  'tailnet /32 TCP-22 egress policy. Reach only — Tailscale '
                  'ACL/SSH-cert auth is a separate gate.',
    )

    # PLAT-7463 (PLAT-5584 fix): cluster-plane read capability. Flag-gated
    # exception to the Q1a fail-closed invariant (automount_service_account_
    # token=False): flagged seats get the kubelet-managed SA token projection
    # and an init-materialized ~/.kube/config that REFERENCES the tokenFile
    # (live rotation; no bearer bytes are ever copied). Authorization is NOT
    # granted by this flag — the scoped RBAC (e.g. the PLAT-5583 four-Deployment
    # read) is a separate declarative grant in the deploy repo.
    clusterplane_kubeconfig = models.BooleanField(
        default=False,
        help_text='Cluster-plane read seat: projects the SA token (kubelet-'
                  'rotated) and init-materializes ~/.kube/config referencing '
                  'the tokenFile. Fail-closed default; RBAC is a separate '
                  'declarative grant.',
    )

    # ── State refs (HLD §5 state carry) ──
    pvc_size = models.CharField(max_length=20, default='10Gi')
    storage_class = models.CharField(
        max_length=100, blank=True,
        help_text='Optional per-agent PVC StorageClass; blank → settings.RUNTIME_STORAGE_CLASS.',
    )
    workspace_mount_path = models.CharField(max_length=200, default='/home/agent/.shizuha')
    shared_workspace = models.BooleanField(
        default=False,
        help_text='Mount the fleet-wide agent-shared-workspace RWX PVC at /shared '
                  '(cross-agent file handoff, e.g. Scout→Nori news catalog, PLAT-1056). '
                  'Only for agents that need cross-agent sharing — not the whole fleet.',
    )

    # ── Brokering / credentials / wiring (HLD §4-§5; broker = PLAT-149) ──
    sidecar_set = models.JSONField(
        default=_default_sidecar_set, blank=True,
        help_text="Sidecars to attach, e.g. ['broker']. Empty → agent container only.",
    )
    credential_grant_scopes = models.JSONField(
        default=_default_grant_scopes, blank=True,
        help_text="Broker grant scopes, e.g. ['github','docker','kubeconfig'].",
    )
    mcp_config = models.JSONField(default=dict, blank=True)
    skill_config = models.JSONField(default=dict, blank=True)

    # ── Behavioral fields (HIVE-133: parity with agents.json) ──
    context_prompt = models.TextField(
        blank=True,
        help_text='CONTEXT_PROMPT env injected into the agent container (maps to contextPrompt in agents.json).',
    )
    personality_traits = models.JSONField(
        default=dict, blank=True,
        help_text='Personality configuration dict (personalityTraits in agents.json); injected as PERSONALITY_TRAITS JSON env.',
    )
    model_fallbacks = models.JSONField(
        default=list, blank=True,
        help_text='Fallback chain list [{model, method, reasoningEffort}] (modelFallbacks); injected as MODEL_FALLBACKS JSON env.',
    )
    model_overrides = models.JSONField(
        default=dict, blank=True,
        help_text='Per-method model/effort override dict (modelOverrides); injected as MODEL_OVERRIDES JSON env.',
    )
    reasoning_effort = models.CharField(
        max_length=50, blank=True,
        help_text='REASONING_EFFORT env (e.g. low, medium, high, max); maps to reasoningEffort/thinkingLevel in agents.json.',
    )
    env = models.JSONField(
        default=dict, blank=True,
        help_text='Per-agent env var overrides injected directly into the agent container (string→string dict).',
    )
    failover_chain_id = models.CharField(
        max_length=100, blank=True,
        help_text='FAILOVER_CHAIN_ID env — identifies the failover chain this agent belongs to (failoverChainId in agents.json).',
    )
    pending_config = models.JSONField(
        default=dict, blank=True, validators=[validate_pending_config_mapping],
        help_text='HIVE-597: desired-config updates accepted by the API but not yet '
                  'confirmed applied by the managing daemon. Written on every config '
                  'push (instantly, even when the daemon-link is down), replayed on '
                  'daemon reconnect, protected from runtime-delta clobber, and '
                  'cleared when the daemon confirms with a config_applied delta.',
    )

    # ── Cohort / sandbox (HLD §1, §11 R1 sequencing gate) ──
    cohort = models.CharField(max_length=50, blank=True, help_text='Cutover cohort (HLD §9).')
    sandbox_required = models.BooleanField(
        default=False,
        help_text='Uses inner-docker exec → BLOCKED on R1 native sandbox substitute.',
    )

    managing_daemon = models.ForeignKey(
        'DaemonRegistry',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='managed_agents',
        help_text='The fleet daemon that adopted and now manages this agent (P4 — null = direct k8s provisioning).',
    )

    # ── Platform wiring ──
    platform_url = models.URLField(default=_default_platform_url)
    expose_http = models.BooleanField(
        default=False,
        help_text='Agents are egress-only (§7); only set for the rare dashboard agent.',
    )
    http_port = models.IntegerField(default=8080)

    # ── k8s/runtime state ──
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True,
    )
    status_message = models.TextField(blank=True)
    hibernation_reason = models.CharField(max_length=255, blank=True)
    hibernated_at = models.DateTimeField(null=True, blank=True)
    native_lifecycle_receipt = models.JSONField(default=dict, blank=True, editable=False)
    runtime_lifecycle_state = models.CharField(
        max_length=32,
        blank=True,
        default='',
        help_text=(
            'Latest typed lifecycle intent confirmed by the managing runtime: '
            'enabled, hibernated, or operator_stopped.'
        ),
    )
    pod_name = models.CharField(max_length=200, blank=True)

    # PLAT-4172: heartbeat needs-help escalation surfaced from the agent daemon
    # (DaemonLink frame). A queue-blind agent — heartbeats that stop exposing a
    # Pulse queue snapshot while ready work is assigned — flips needs_help so the
    # operator sees it on the Agents page instead of a falsely-healthy fleet.
    needs_help = models.BooleanField(default=False, db_index=True)
    needs_help_reason = models.TextField(blank=True)
    heartbeat_outcome = models.CharField(max_length=32, blank=True)

    # ── Timestamps ──
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    provisioned_at = models.DateTimeField(null=True, blank=True)
    last_active_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['agent_username']
        # PLAT-4235 (operator 2026-07-12, "users OWN fleets"): a user's per-user
        # daemon manages that user's whole FLEET, so an owner may own MANY agents.
        # The former unique `fleet_one_personal_agent_per_owner` constraint (a
        # single-personal-agent assumption from 0027) is dropped in 0030. The
        # canonical personal Shizuha is now identified by its deterministic
        # username `shizuha-<owner_id>` (see fleet.personal_agents), not by owner
        # uniqueness. owner_id keeps a plain index for owner-scoped fleet queries.
        indexes = [
            models.Index(fields=['owner_id'], name='fleet_agent_owner_idx'),
        ]

    def __str__(self):
        return f'FleetAgent({self.agent_username}, {self.status})'

    # ── Derived names (single source of truth for the provisioner) ──
    @property
    def deployment_name(self) -> str:
        return f'agent-{self.agent_username}'

    @property
    def app_label(self) -> str:
        return f'agent-{self.agent_username}'

    @property
    def identity_secret_name(self) -> str:
        return f'agent-{self.agent_username}-identity'

    @property
    def workspace_pvc_name(self) -> str:
        return f'agent-{self.agent_username}-workspace'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Snapshot identity-shaping fields to enforce immutability-after-provision
        # (review P2 + P1 root cause): once provisioned, the k8s resource names and
        # the chosen namespace are fixed, so agent_username and isolated must not
        # change underneath the live resources.
        self.__orig_username = self.agent_username
        self.__orig_isolated = self.isolated
        self.__orig_owner_id = self.owner_id
        self.__orig_owner_profile_id = self.owner_profile_id
        self.__orig_identity_user_id = self.identity_user_id
        self.__orig_identity_profile_id = self.identity_profile_id
        self.__orig_model = self.model
        self.__orig_execution_method = str(
            (self.mcp_config or {}).get('execution_method') or 'shizuha'
        ).strip()

    def resolve_namespace(self, shared_ns: str) -> str:
        """Isolated agents (DevOps) get their own ns; everyone else shares (Q1/Q1b)."""
        return f'fleet-{self.agent_username}' if self.isolated else shared_ns

    def save(self, *args, **kwargs):
        # Enforce the DNS-1123 username at persist (review #5) so an invalid
        # handle can never reach the provisioner and fail at k8s apply-time.
        dns1123_validator(self.agent_username)
        # JSONField accepts scalars and arrays, but pending_config is a durable
        # key/value outbox. Enforce the narrower contract for every model write;
        # field validators cover ModelForms/serializers and the data migration
        # repairs rows created before this invariant existed.
        validate_pending_config_mapping(self.pending_config)
        if len(self.agent_username) > 47:  # k8s name budget (review P2)
            raise ValidationError('agent_username must be ≤47 chars (k8s name budget)')
        # Immutable after provision (review P2 + P1 root): agent_username drives
        # every k8s name and `isolated` drives the namespace — changing either on a
        # provisioned agent would orphan/mis-target live resources (the P1 shared-ns
        # deletion bug stemmed from a mutable `isolated`).
        if self.provisioned_at is not None or self.identity_provisioned_at is not None:
            if self.agent_username != self.__orig_username:
                raise ValidationError('agent_username is immutable after provisioning')
            if self.isolated != self.__orig_isolated:
                raise ValidationError('isolated is immutable after provisioning')
            if self.owner_id != self.__orig_owner_id:
                # PLAT-6199: same first-bind class as identity_user_id below —
                # persist_identity_binding queryset-updates owner_id and mutates
                # the in-memory instance, but refresh_from_db() does not reset
                # __orig_owner_id, so a NULL→value first bind would poison every
                # later save() (e.g. start_agent's status write in the enable
                # path) with a false immutability error. Allow the first bind.
                if self.__orig_owner_id is not None:
                    raise ValidationError('owner_id is immutable after identity/runtime provisioning')
            if self.owner_profile_id != self.__orig_owner_profile_id:
                # PLAT-6199: same first-bind allowance — see owner_id above.
                if self.__orig_owner_profile_id is not None:
                    raise ValidationError('owner_profile_id is immutable after identity/runtime provisioning')
            # identity_* are immutable once BOUND, but a first bind (NULL → value)
            # must be allowed: rows provisioned before identity resolution exist
            # fleet-wide with identity_user_id NULL, and refusing the first bind
            # meant the resolved id could never persist — worse, the refused save
            # left the in-memory instance poisoned so EVERY later save() on it
            # (e.g. start_agent's status write in the enable path) raised too.
            if (self.__orig_identity_user_id is not None
                    and self.identity_user_id != self.__orig_identity_user_id):
                raise ValidationError('identity_user_id is immutable after identity/runtime provisioning')
            if (self.__orig_identity_profile_id is not None
                    and self.identity_profile_id != self.__orig_identity_profile_id):
                raise ValidationError('identity_profile_id is immutable after identity/runtime provisioning')
        if not self.namespace:
            # Default to the shared ns from settings; the provisioner is the
            # authoritative resolver, but storing it keeps the row self-describing
            # AND is what lifecycle ops read back (review #3).
            shared = getattr(settings, 'SHIZUHA_FLEET_NAMESPACE', 'shizuha-fleet')
            self.namespace = self.resolve_namespace(shared)
        # Keep a persisted Grok Build label and its model lane compatible. The REST editor
        # already admits runtime-lane changes atomically, but FleetAgent is also
        # written by reconcilers, imports, admin, and management commands.  A
        # pre-admission grok_build + DeepSeek row reached production through one
        # of those paths: the daemon silently downgraded it to the Shizuha
        # gateway, then the DeepSeek lease controller hibernated it.  Validate at
        # the model chokepoint whenever either lane-defining field changes so a
        # Grok-labelled agent cannot persist a non-Grok model again.  Existing
        # unrelated status writes remain safe while the repair migration fixes
        # the known legacy rows.
        execution_method = str(
            (self.mcp_config or {}).get('execution_method') or 'shizuha'
        ).strip()
        requested_fields = kwargs.get('update_fields')
        from .runtime_lane import remap_retired_fleet_model
        remapped = remap_retired_fleet_model(self.model)
        if remapped != (self.model or ''):
            self.model = remapped
            if requested_fields is not None:
                requested_fields = list(dict.fromkeys([*requested_fields, 'model']))
                kwargs['update_fields'] = requested_fields
        lane_changed = self._state.adding or (
            ({'model', 'mcp_config'} & set(requested_fields))
            if requested_fields is not None
            else (
                self.model != self.__orig_model
                or execution_method != self.__orig_execution_method
            )
        )
        if lane_changed and self.model and execution_method == 'grok_build':
            from .runtime_lane import RuntimeLaneError, resolve_runtime_lane
            try:
                resolve_runtime_lane({}, {
                    'model': self.model,
                    'execution_method': execution_method,
                })
            except RuntimeLaneError as exc:
                raise ValidationError({'model': exc.message}) from exc
        # HIVE-1937: an explicit provider prefix must be servable by the method
        # for EVERY lane, not just grok_build. `xai:grok-4.5` on the shizuha
        # gateway (no xai capability) reached a pod env that exited at startup
        # with "Provider xai is not configured/available" and crash-looped Aya.
        # This lightweight check runs for all methods whenever the model changes
        # and rejects only models with an explicit provider the method cannot
        # serve — bare models and cortex-routed models are untouched.
        if lane_changed and self.model:
            from .runtime_lane import RuntimeLaneError, validate_model_provider_servable
            try:
                validate_model_provider_servable(self.model, execution_method)
            except RuntimeLaneError as exc:
                raise ValidationError({'model': exc.message}) from exc
        # Operator exception (2026-08-01): arbitrary within-agent fallbacks remain
        # forbidden, but an xAI Grok Build primary must have the one platform-
        # managed local DeepSeek V4 Flash tail so confirmed xAI credit/auth outages
        # do not idle Grok agents. Keep the normalization at this model chokepoint
        # so admin, migrations, reconcilers, daemon sync, and REST cannot persist a
        # different hidden chain.
        from .runtime_lane import managed_model_fallbacks
        self.model_fallbacks = managed_model_fallbacks(self.model, execution_method)
        self.failover_chain_id = ''
        # Hive fleet placement is k3s-only. Never persist bare_metal/docker/host
        # placement in mcp_config — the per-user fleet daemon (e.g. hritik /
        # rt-fleet) must spawn agent Deployments in shizuha-fleet, not host
        # processes. Isolation (sandbox/isolated) is separate fields.
        from .runtime_placement import force_mcp_config_k8s
        before_rt = (self.mcp_config or {}).get('runtime_environment')
        self.mcp_config = force_mcp_config_k8s(self.mcp_config)
        if requested_fields is not None and lane_changed:
            kwargs['update_fields'] = list(dict.fromkeys([
                *requested_fields, 'model_fallbacks', 'failover_chain_id',
            ]))
        if requested_fields is not None and before_rt != 'k8s':
            kwargs['update_fields'] = list(dict.fromkeys([
                *list(kwargs.get('update_fields') or requested_fields),
                'mcp_config',
            ]))
        super().save(*args, **kwargs)
        self.__orig_model = self.model
        self.__orig_execution_method = execution_method
        # A committed first bind is the new baseline: the identity fields are
        # immutable from THIS point on, including on this same instance.
        self.__orig_identity_user_id = self.identity_user_id
        self.__orig_identity_profile_id = self.identity_profile_id


class FleetAgentEvent(models.Model):
    """Audit log of fleet-agent lifecycle events (mirrors ClawEvent)."""
    agent = models.ForeignKey(FleetAgent, on_delete=models.CASCADE, related_name='events')
    event_type = models.CharField(max_length=50)  # provisioned, started, stopped, error, destroyed
    message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.event_type} @ {self.created_at}'


class FleetAgentActivitySnapshot(models.Model):
    """Last observed live-activity transcript for a native fleet agent.

    Hive's activity GET execs the running pod. After hibernate or restart there
    is no pod, so the Agents drawer used to go blank even when the workspace
    PVC still had audit/telemetry. Persist the last successful native read
    here so past history remains visible from Hive.
    """

    agent = models.OneToOneField(
        FleetAgent, on_delete=models.CASCADE, related_name='activity_snapshot',
    )
    events = models.JSONField(default=list, blank=True)
    newest_event_at = models.DateTimeField(null=True, blank=True)
    captured_at = models.DateTimeField(auto_now=True)
    source = models.CharField(max_length=64, blank=True)
    resource_uid = models.CharField(max_length=128, blank=True)
    pod_name = models.CharField(max_length=200, blank=True)
    source_state_at_capture = models.CharField(max_length=32, blank=True)

    def __str__(self):
        return f'activity snapshot {self.agent_id} @ {self.captured_at}'


class AgentMcpProjectionState(models.Model):
    """Durable per-agent high-water and bounded authority lease.

    ``lease_deadline`` is renewed only by the authenticated Hive↔daemon
    heartbeat/registration stream.  A direct HTTP projection request can read
    this tuple but can never extend it, so a silently stalled/disconnected
    authority consumer is fenced within the five-second evidence bound.
    """

    agent = models.OneToOneField(
        FleetAgent, on_delete=models.CASCADE, related_name='mcp_projection_state',
    )
    generation = models.PositiveBigIntegerField(default=1)
    fingerprint = models.CharField(max_length=64)
    authority_state = models.CharField(max_length=32, default='authority_unknown')
    current_high_water = models.PositiveBigIntegerField(default=1)
    lease_deadline = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'AgentMcpProjectionState({self.agent_id}, gen={self.generation})'


class CapabilityDefinition(models.Model):
    """Hive-owned capability catalog row (HIVE-272/HIVE-273)."""

    class LifecycleStatus(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        DEPRECATED = 'deprecated', 'Deprecated'
        DISABLED = 'disabled', 'Disabled'

    class ActivationStatus(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        PENDING_SECURITY = 'pending_security', 'Pending Security'
        APPROVED = 'approved', 'Approved'
        ACTIVE = 'active', 'Active'
        DISABLED = 'disabled', 'Disabled'

    class RiskTier(models.TextChoices):
        BASE = 'base', 'Base'
        STANDARD = 'standard', 'Standard'
        SENSITIVE = 'sensitive', 'Sensitive'
        PRIVILEGED = 'privileged', 'Privileged'

    slug = models.CharField(max_length=80, unique=True, db_index=True, validators=[capability_slug_validator])
    display_name = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    lifecycle_status = models.CharField(
        max_length=20, choices=LifecycleStatus.choices, default=LifecycleStatus.DRAFT, db_index=True,
    )
    activation_status = models.CharField(
        max_length=32, choices=ActivationStatus.choices, default=ActivationStatus.DRAFT, db_index=True,
    )
    risk_tier = models.CharField(max_length=20, choices=RiskTier.choices, default=RiskTier.STANDARD)
    skill_refs = models.JSONField(default=_json_list_default, blank=True)
    eager_skill_refs = models.JSONField(default=_json_list_default, blank=True)
    inline_profile_refs = models.JSONField(default=_json_list_default, blank=True)
    mcp_server_refs = models.JSONField(default=_json_list_default, blank=True)
    runtime_flags = models.JSONField(default=_json_dict_default, blank=True)
    sensitive_access_flags = models.JSONField(default=_json_list_default, blank=True)
    default_credential_grant_scopes = models.JSONField(default=_json_list_default, blank=True)
    conflicts_with = models.JSONField(default=_json_list_default, blank=True)
    requires = models.JSONField(default=_json_list_default, blank=True)
    non_removable = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)
    definition_hash = models.CharField(max_length=64, blank=True, db_index=True)
    approval_task_key = models.CharField(max_length=50, blank=True)
    approval_wiki_page_id = models.CharField(max_length=80, blank=True)
    approved_by = models.CharField(max_length=255, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    security_review_required = models.BooleanField(default=False)
    security_reviewed_by = models.CharField(max_length=255, blank=True)
    security_reviewed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.CharField(max_length=255, blank=True)
    updated_by = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['slug']

    def __str__(self):
        return self.slug

    @property
    def is_privileged(self):
        return self.risk_tier in {self.RiskTier.SENSITIVE, self.RiskTier.PRIVILEGED} or bool(self.sensitive_access_flags)

    def hash_payload(self):
        return {
            'slug': self.slug,
            'display_name': self.display_name,
            'description': self.description,
            'lifecycle_status': self.lifecycle_status,
            'activation_status': self.activation_status,
            'risk_tier': self.risk_tier,
            'skill_refs': self.skill_refs or [],
            'eager_skill_refs': self.eager_skill_refs or [],
            'inline_profile_refs': self.inline_profile_refs or [],
            'mcp_server_refs': self.mcp_server_refs or [],
            'runtime_flags': self.runtime_flags or {},
            'sensitive_access_flags': self.sensitive_access_flags or [],
            'default_credential_grant_scopes': self.default_credential_grant_scopes or [],
            'conflicts_with': self.conflicts_with or [],
            'requires': self.requires or [],
            'non_removable': self.non_removable,
        }

    def clean(self):
        capability_slug_validator(self.slug)
        if self.activation_status == self.ActivationStatus.ACTIVE:
            if self.lifecycle_status == self.LifecycleStatus.DISABLED:
                raise ValidationError('disabled capabilities cannot be active')
            if self.is_privileged and not (self.security_reviewed_by and self.security_reviewed_at):
                raise ValidationError('sensitive/privileged active capabilities require security review metadata')

    def save(self, *args, **kwargs):
        self.slug = (self.slug or '').strip().lower()
        if not self.display_name:
            self.display_name = ' '.join(p.capitalize() for p in self.slug.split('-') if p)
        self.security_review_required = self.is_privileged
        new_hash = _definition_hash(self.hash_payload())
        if self.pk:
            old = CapabilityDefinition.objects.filter(pk=self.pk).values('definition_hash').first()
            if old and old.get('definition_hash') and old['definition_hash'] != new_hash:
                self.version = (self.version or 1) + 1
        self.definition_hash = new_hash
        self.full_clean()
        super().save(*args, **kwargs)


class TeamRequiredCapability(models.Model):
    """Required capability grant for a canonical Pulse TeamRoster team."""

    team_slug = models.CharField(max_length=80, db_index=True, validators=[capability_slug_validator])
    capability = models.ForeignKey(CapabilityDefinition, on_delete=models.CASCADE, related_name='team_requirements')
    required = models.BooleanField(default=True)
    enabled = models.BooleanField(default=True, db_index=True)
    reason = models.TextField(blank=True)
    created_by = models.CharField(max_length=255, blank=True)
    updated_by = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('team_slug', 'capability')]
        ordering = ['team_slug', 'capability__slug']

    def clean(self):
        self.team_slug = (self.team_slug or '').strip().lower().replace('_', '-').replace(' ', '-')
        capability_slug_validator(self.team_slug)
        if self.enabled and self.capability.activation_status != CapabilityDefinition.ActivationStatus.ACTIVE:
            raise ValidationError('team requirements can only grant active capabilities')


class TeamMembershipRole(models.Model):
    """Display-only role/title metadata for an agent's membership in one team.

    The membership source of truth is Pulse TeamRoster exclusively.
    This edge only carries human-readable role context for Hive UI; runtime
    grants continue to come from TeamRequiredCapability + overrides.
    """

    class MembershipRole(models.TextChoices):
        MEMBER = 'member', 'Member'
        LEAD = 'lead', 'Lead'
        REVIEWER = 'reviewer', 'Reviewer'
        OPERATOR = 'operator', 'Operator'
        OBSERVER = 'observer', 'Observer'
        CUSTOM = 'custom', 'Custom'

    agent_identifier = models.CharField(max_length=255, db_index=True)
    team_slug = models.CharField(max_length=80, db_index=True, validators=[capability_slug_validator])
    organization_slug = models.CharField(max_length=120, blank=True, default='', db_index=True)
    role_title = models.CharField(max_length=120, blank=True)
    membership_role = models.CharField(
        max_length=24,
        choices=MembershipRole.choices,
        default=MembershipRole.MEMBER,
    )
    tier = models.CharField(max_length=80, blank=True)
    enabled = models.BooleanField(default=True, db_index=True)
    source = models.CharField(max_length=80, default='hive', blank=True)
    reason = models.TextField(blank=True)
    created_by = models.CharField(max_length=255, blank=True)
    updated_by = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('agent_identifier', 'team_slug', 'organization_slug')]
        ordering = ['organization_slug', 'team_slug', 'agent_identifier']

    def _normalise_fields(self):
        self.agent_identifier = (self.agent_identifier or '').strip().lower()
        self.team_slug = (self.team_slug or '').strip().lower().replace('_', '-').replace(' ', '-')
        self.organization_slug = (self.organization_slug or '').strip().lower()
        self.role_title = (self.role_title or '').strip()
        self.membership_role = (self.membership_role or self.MembershipRole.MEMBER).strip().lower()
        self.tier = str(self.tier or '').strip()
        if self.membership_role not in {choice[0] for choice in self.MembershipRole.choices}:
            self.membership_role = self.MembershipRole.CUSTOM

    def clean(self):
        self._normalise_fields()
        capability_slug_validator(self.team_slug)

    def save(self, *args, **kwargs):
        self._normalise_fields()
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.agent_identifier} @ {self.team_slug}: {self.role_title or self.membership_role}'


def ensure_team_required_capability(team_slug, *, actor='hive:auto-capability', reason='Baseline capability for team routing.'):
    """Ensure a team has a same-slug required capability.

    Team rosters are the source for membership; capability rows are the source
    for skills/MCP/profile bundles. New teams should not start with an empty
    capability set, otherwise agents on that team receive no effective bundle.
    """
    team_key = (team_slug or '').strip().lower().replace('_', '-').replace(' ', '-')
    capability_slug_validator(team_key)
    spec = dict(DEFAULT_TEAM_CAPABILITY_SPECS.get(team_key) or {})
    spec.setdefault('display_name', ' '.join(part.capitalize() for part in team_key.split('-') if part))
    spec.setdefault('risk_tier', CapabilityDefinition.RiskTier.STANDARD)
    spec.setdefault('skill_refs', [])
    payload = {
        'slug': team_key,
        'display_name': spec['display_name'],
        'description': spec.get('description') or f'Baseline {spec["display_name"]} capability for team skill/MCP routing.',
        'lifecycle_status': CapabilityDefinition.LifecycleStatus.DRAFT,
        'activation_status': CapabilityDefinition.ActivationStatus.ACTIVE,
        'risk_tier': spec['risk_tier'],
        'skill_refs': spec.get('skill_refs') or [],
        'eager_skill_refs': spec.get('eager_skill_refs') or [],
        'inline_profile_refs': spec.get('inline_profile_refs') or [],
        'mcp_server_refs': _team_mcp_server_refs(spec, team_key),
        'runtime_flags': spec.get('runtime_flags') or {},
        'sensitive_access_flags': spec.get('sensitive_access_flags') or [],
        'default_credential_grant_scopes': spec.get('default_credential_grant_scopes') or [],
        'conflicts_with': spec.get('conflicts_with') or [],
        'requires': spec.get('requires') or [],
        'non_removable': spec.get('non_removable', False),
    }
    # HIVE-331: never silently DROP mcp_server_refs from an existing capability row.
    # update_or_create applies full defaults, so a re-created team would clobber a
    # hand-tuned bundle back to the spec (PLAT-1274: accounting lost books+drive).
    # Union spec refs into the existing list instead — monotone, order-preserving.
    existing = (
        CapabilityDefinition.objects.filter(slug=team_key)
        .values('mcp_server_refs', 'skill_refs', 'eager_skill_refs')
        .first()
        or {}
    )
    existing_refs = list(existing.get('mcp_server_refs') or [])
    if existing_refs:
        payload['mcp_server_refs'] = existing_refs + [
            ref for ref in payload['mcp_server_refs'] if ref not in existing_refs
        ]
    if team_key in LEAN_CONVERSATIONAL_TEAM_SLUGS:
        # Ensure the CEO Office floor (platform base + Hive) is present; do
        # not strip admin/id/hive. Operator 2026-08-16: talk seats do work.
        payload['mcp_server_refs'] = trim_lean_conversational_mcp(payload['mcp_server_refs'])
    # Skill / eager skill bundles: union existing row with baseline so
    # (1) operator hand-tuned extras on the capability definition are kept, and
    # (2) new baseline skills (e.g. social-media human-browser-stealth) always land.
    # Agent skill_config is still *replaced* by capability reconcile from this
    # definition — agents must not be hand-patched; the team capability is SoT.
    existing_skills = list(existing.get('skill_refs') or [])
    baseline_skills = list(payload.get('skill_refs') or [])
    merged_skills = list(existing_skills)
    for ref in baseline_skills:
        if ref not in merged_skills:
            merged_skills.append(ref)
    payload['skill_refs'] = merged_skills

    existing_eager = list(existing.get('eager_skill_refs') or [])
    baseline_eager = list(payload.get('eager_skill_refs') or [])
    merged_eager = list(existing_eager)
    for ref in baseline_eager:
        if ref not in merged_eager:
            merged_eager.append(ref)
    payload['eager_skill_refs'] = merged_eager
    is_privileged = payload['risk_tier'] in {
        CapabilityDefinition.RiskTier.SENSITIVE,
        CapabilityDefinition.RiskTier.PRIVILEGED,
    } or bool(payload['sensitive_access_flags'])
    now = timezone.now()
    cap, _ = CapabilityDefinition.objects.update_or_create(
        slug=team_key,
        defaults={
            **payload,
            'security_review_required': is_privileged,
            'security_reviewed_by': actor if is_privileged else '',
            'security_reviewed_at': now if is_privileged else None,
            'approved_by': actor,
            'approved_at': now,
            'updated_by': actor,
        },
    )
    if not cap.created_by:
        cap.created_by = actor
        cap.save(update_fields=['created_by'])
    req, _ = TeamRequiredCapability.objects.update_or_create(
        team_slug=team_key,
        capability=cap,
        defaults={
            'required': True,
            'enabled': True,
            'reason': reason,
            'updated_by': actor,
        },
    )
    if not req.created_by:
        req.created_by = actor
        req.save(update_fields=['created_by'])
    return cap, req


class AgentCapabilityOverride(models.Model):
    """Audited agent-specific capability add/remove override."""

    class Action(models.TextChoices):
        ADD = 'add', 'Add'
        REMOVE = 'remove', 'Remove'

    agent_identifier = models.CharField(max_length=255, db_index=True)
    capability = models.ForeignKey(CapabilityDefinition, on_delete=models.CASCADE, related_name='agent_overrides')
    action = models.CharField(max_length=12, choices=Action.choices)
    reason = models.TextField()
    expires_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.CharField(max_length=255, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    created_by = models.CharField(max_length=255, blank=True)
    updated_by = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('agent_identifier', 'capability')]
        ordering = ['agent_identifier', 'capability__slug']

    @property
    def is_expired(self):
        return bool(self.expires_at and self.expires_at <= timezone.now())

    def clean(self):
        self.agent_identifier = (self.agent_identifier or '').strip().lower()
        if self.action == self.Action.ADD and self.capability.activation_status != CapabilityDefinition.ActivationStatus.ACTIVE:
            raise ValidationError('agent overrides can only add active capabilities')
        if self.capability.is_privileged and self.action == self.Action.ADD and not (self.approved_by and self.approved_at):
            raise ValidationError('privileged add overrides require approval metadata')


class CapabilityAuditEvent(models.Model):
    """Append-only capability audit ledger (redacted before/after JSON)."""

    actor = models.CharField(max_length=255, blank=True)
    action = models.CharField(max_length=80, db_index=True)
    target_type = models.CharField(max_length=80)
    target_key = models.CharField(max_length=255, db_index=True)
    reason = models.TextField(blank=True)
    before = models.JSONField(default=_json_dict_default, blank=True)
    after = models.JSONField(default=_json_dict_default, blank=True)
    before_hash = models.CharField(max_length=64, blank=True)
    after_hash = models.CharField(max_length=64, blank=True)
    approval_ref = models.CharField(max_length=255, blank=True)
    runtime_reconcile_outcome = models.CharField(max_length=40, blank=True)
    diagnostic = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        self.before_hash = _definition_hash(self.before or {})
        self.after_hash = _definition_hash(self.after or {})
        super().save(*args, **kwargs)


class CapabilityEvidenceProducerGeneration(models.Model):
    """Immutable build/key provenance for the active V2 evidence producer.

    The evidence endpoint is an authorization boundary, so a source revision is
    not current merely because it is deployed.  Origin CI creates this record
    only after it has built and verified the exact multi-arch image index.  All
    bound fields and the resulting digest are immutable; rotation may only
    retire the current row before inserting its successor.
    """

    generation_id = models.CharField(max_length=96, primary_key=True)
    generation_digest = models.CharField(max_length=71, unique=True, editable=False)
    source_sha = models.CharField(max_length=40)
    source_tree = models.CharField(max_length=40)
    image_index_digest = models.CharField(max_length=71)
    schema_digest = models.CharField(max_length=71)
    materializer_version = models.CharField(max_length=96)
    materializer_digest = models.CharField(max_length=71)
    config_digest = models.CharField(max_length=71)
    service_account = models.CharField(max_length=128)
    signing_kid = models.CharField(max_length=128)
    signing_key_thumbprint = models.CharField(max_length=71)
    activated_at = models.DateTimeField()
    active = models.BooleanField(default=True, db_index=True)
    retired_at = models.DateTimeField(null=True, blank=True)

    _BOUND_FIELDS = (
        'generation_id', 'source_sha', 'source_tree', 'image_index_digest',
        'schema_digest', 'materializer_version', 'materializer_digest',
        'config_digest', 'service_account', 'signing_kid',
        'signing_key_thumbprint', 'activated_at',
    )

    class Meta:
        ordering = ['-activated_at']
        constraints = [
            models.UniqueConstraint(
                fields=['active'], condition=models.Q(active=True),
                name='fleet_one_active_cap_evidence_producer',
            ),
        ]

    def digest_payload(self):
        activated = self.activated_at
        if activated and timezone.is_naive(activated):
            activated = timezone.make_aware(activated, datetime_timezone.utc)
        return {
            'generation_id': self.generation_id,
            'source_sha': self.source_sha,
            'source_tree': self.source_tree,
            'image_index_digest': self.image_index_digest,
            'schema_digest': self.schema_digest,
            'materializer_version': self.materializer_version,
            'materializer_digest': self.materializer_digest,
            'config_digest': self.config_digest,
            'service_account': self.service_account,
            'signing_kid': self.signing_kid,
            'signing_key_thumbprint': self.signing_key_thumbprint,
            'activated_at': activated.isoformat().replace('+00:00', 'Z') if activated else '',
        }

    def expected_digest(self):
        canonical = json.dumps(
            self.digest_payload(), sort_keys=True, separators=(',', ':'),
            ensure_ascii=False,
        ).encode('utf-8')
        return f'sha256:{hashlib.sha256(canonical).hexdigest()}'

    def clean(self):
        super().clean()
        for field in ('source_sha', 'source_tree'):
            value = str(getattr(self, field) or '')
            if not re.fullmatch(r'[0-9a-f]{40}', value):
                raise ValidationError({field: 'must be a full lowercase Git SHA-1'})
        for field in (
            'image_index_digest', 'schema_digest', 'materializer_digest',
            'config_digest', 'signing_key_thumbprint',
        ):
            value = str(getattr(self, field) or '')
            if not re.fullmatch(r'sha256:[0-9a-f]{64}', value):
                raise ValidationError({field: 'must be sha256:<64 lowercase hex>'})
        if self.active and self.retired_at is not None:
            raise ValidationError({'retired_at': 'an active generation cannot be retired'})
        if not self.active and self.retired_at is None:
            raise ValidationError({'retired_at': 'a retired generation requires retired_at'})
        expected = self.expected_digest()
        if self.generation_digest and self.generation_digest != expected:
            raise ValidationError({'generation_digest': 'does not match immutable bound fields'})

    def save(self, *args, **kwargs):
        if self.pk:
            prior = type(self).objects.filter(pk=self.pk).first()
            if prior:
                changed = [
                    field for field in self._BOUND_FIELDS
                    if getattr(prior, field) != getattr(self, field)
                ]
                if changed:
                    raise ValidationError(
                        f'producer generation bound fields are immutable: {", ".join(changed)}'
                    )
                if prior.generation_digest != self.generation_digest:
                    raise ValidationError('producer generation digest is immutable')
                if not prior.active and self.active:
                    raise ValidationError('a retired producer generation cannot be reactivated')
        self.generation_digest = self.expected_digest()
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('producer generation history is immutable and cannot be deleted')

    def __str__(self):
        return f'{self.generation_id} ({"ACTIVE" if self.active else "retired"})'


# ---------------------------------------------------------------------------
# HIVE-235: Daemon-link WebSocket bridge
# ---------------------------------------------------------------------------

class DaemonCredential(models.Model):
    """Hive-issued per-daemon auth token (stored as SHA-256 hash, never plaintext)."""
    id = models.BigAutoField(auto_created=True, primary_key=True, serialize=False)
    daemon_id = models.CharField(max_length=128, db_index=True)
    token_hash = models.CharField(max_length=64, unique=True)
    revoked = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'DaemonCredential({self.daemon_id}, revoked={self.revoked})'


class HiveCredential(models.Model):
    """Hive source-of-truth metadata for agent/team credential grants (HIVE-263).

    Stores only broker/encrypted handles and redacted fingerprints; plaintext
    secret values are write-only to the future broker/materializer path and must
    never be returned from Hive APIs.
    """

    class Scope(models.TextChoices):
        AGENT = 'agent', 'Agent'
        TEAM = 'team', 'Team'

    class Provider(models.TextChoices):
        GITHUB = 'github', 'GitHub'
        DOCKER = 'docker', 'Docker'
        KUBECONFIG = 'kubeconfig', 'Kubeconfig'
        GENERIC_ENV = 'generic-env', 'Generic env'

    class Health(models.TextChoices):
        UNKNOWN = 'unknown', 'Unknown'
        OK = 'ok', 'OK'
        FAILING = 'failing', 'Failing'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scope = models.CharField(max_length=16, choices=Scope.choices, db_index=True)
    agent = models.ForeignKey(
        FleetAgent, null=True, blank=True, on_delete=models.CASCADE, related_name='credential_grants'
    )
    organization_slug = models.CharField(
        max_length=100,
        default='shizuha',
        db_index=True,
        help_text='Organization owning a team-scoped grant; ignored for agent-scoped grants.',
    )
    team_slug = models.CharField(max_length=100, blank=True, db_index=True)
    provider = models.CharField(max_length=32, choices=Provider.choices, db_index=True)
    purpose = models.CharField(max_length=100, db_index=True)
    secret_ref = models.CharField(
        max_length=300,
        help_text='Broker/envelope handle only; never store plaintext secret material here.',
    )
    fingerprint = models.CharField(max_length=100, blank=True)
    requires_team_credential_eligible = models.BooleanField(
        default=True,
        help_text='Team grants fan out only to members explicitly eligible for team credentials.',
    )
    health = models.CharField(max_length=16, choices=Health.choices, default=Health.UNKNOWN)
    health_message = models.TextField(blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_applied_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_by_user_id = models.CharField(max_length=64, blank=True)
    updated_by_user_id = models.CharField(max_length=64, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['scope', 'organization_slug', 'team_slug', 'provider', 'purpose']
        constraints = [
            models.UniqueConstraint(
                fields=['scope', 'organization_slug', 'team_slug', 'provider', 'purpose'],
                condition=models.Q(scope='team', is_active=True),
                name='fleet_hivecred_active_team_unique',
            ),
            models.UniqueConstraint(
                fields=['scope', 'agent', 'provider', 'purpose'],
                condition=models.Q(scope='agent', is_active=True),
                name='fleet_hivecred_active_agent_unique',
            ),
        ]

    def clean(self):
        super().clean()
        if self.scope == self.Scope.TEAM:
            self.organization_slug = _canon_team_slug(self.organization_slug)
            if not self.organization_slug:
                raise ValidationError({
                    'organization_slug': 'organization_slug is required for team-scoped credentials',
                })
            if not self.team_slug:
                raise ValidationError({'team_slug': 'team_slug is required for team-scoped credentials'})
            if self.agent_id:
                raise ValidationError({'agent': 'agent must be empty for team-scoped credentials'})
        elif self.scope == self.Scope.AGENT:
            if not self.agent_id:
                raise ValidationError({'agent': 'agent is required for agent-scoped credentials'})
            if self.team_slug:
                raise ValidationError({'team_slug': 'team_slug must be empty for agent-scoped credentials'})
        try:
            self.secret_ref = validate_credential_secret_ref(self.secret_ref)
        except ValidationError as exc:
            raise ValidationError({'secret_ref': exc.messages}) from exc
        try:
            self.fingerprint = validate_credential_fingerprint(self.fingerprint)
        except ValidationError as exc:
            raise ValidationError({'fingerprint': exc.messages}) from exc

    def save(self, *args, **kwargs):
        if self.organization_slug:
            self.organization_slug = _canon_team_slug(self.organization_slug)
        if self.team_slug:
            self.team_slug = _canon_team_slug(self.team_slug)
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        target = self.team_slug if self.scope == self.Scope.TEAM else getattr(self.agent, 'agent_username', self.agent_id)
        if self.scope == self.Scope.TEAM:
            target = f'{self.organization_slug}/{target}'
        return f'HiveCredential({self.scope}:{target}:{self.provider}:{self.purpose})'


class FleetAgentTeamCredentialEligibility(models.Model):
    """Exact-team opt-in required before a team credential may materialize.

    Pulse TeamRoster membership and this row are independent gates: neither one
    alone grants anything. Rows are toggled rather than deleted so their audit
    ledger and rollback provenance remain durable.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agent = models.ForeignKey(
        FleetAgent,
        on_delete=models.CASCADE,
        related_name='team_credential_eligibilities',
    )
    organization_slug = models.CharField(max_length=100, default='shizuha', db_index=True)
    team_slug = models.CharField(max_length=100, db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)
    actor_user_id = models.CharField(max_length=100, blank=True)
    reason = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['organization_slug', 'team_slug', 'agent__agent_username']
        constraints = [
            models.UniqueConstraint(
                fields=['agent', 'organization_slug', 'team_slug'],
                name='fleet_agent_team_credential_eligibility_unique',
            ),
        ]
        indexes = [models.Index(
            fields=['organization_slug', 'team_slug', 'is_active'],
            name='fleet_elig_org_team_active_idx',
        )]

    def clean(self):
        super().clean()
        self.organization_slug = _canon_team_slug(self.organization_slug)
        self.team_slug = _canon_team_slug(self.team_slug)
        if not self.organization_slug:
            raise ValidationError({'organization_slug': 'organization_slug is required'})
        if not self.team_slug:
            raise ValidationError({'team_slug': 'team_slug is required'})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return (
            f'FleetAgentTeamCredentialEligibility('
            f'{self.agent.agent_username}:{self.organization_slug}/{self.team_slug}, '
            f'active={self.is_active})'
        )


class FleetAgentTeamCredentialEligibilityAudit(models.Model):
    """Append-only redacted ledger for exact-team eligibility changes."""

    class Action(models.TextChoices):
        GRANT = 'grant', 'Grant'
        REVOKE = 'revoke', 'Revoke'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    eligibility = models.ForeignKey(
        FleetAgentTeamCredentialEligibility,
        on_delete=models.PROTECT,
        related_name='audit_events',
    )
    action = models.CharField(max_length=16, choices=Action.choices)
    actor_user_id = models.CharField(max_length=100)
    agent_username = models.CharField(max_length=100)
    organization_slug = models.CharField(max_length=100, default='shizuha')
    team_slug = models.CharField(max_length=100)
    redacted_before = models.JSONField(default=dict, blank=True)
    redacted_after = models.JSONField(default=dict, blank=True)
    message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return (
            f'FleetAgentTeamCredentialEligibilityAudit('
            f'{self.agent_username}:{self.organization_slug}/{self.team_slug}, {self.action})'
        )


HIVE_CREDENTIAL_REQUEST_SCOPES = (
    ('fleet-ssh', 'Fleet SSH'),
    ('kubeconfig', 'Kubeconfig'),
    ('vault-token', 'Vault token'),
    ('shizuha-id', 'Shizuha ID'),
    ('github', 'GitHub'),
    ('gitlab', 'GitLab'),
    ('aws', 'AWS'),
    ('npm', 'npm'),
    ('docker', 'Docker'),
    ('custom', 'Custom'),
)


def normalize_credential_request_reason(value):
    reason = unicodedata.normalize('NFKC', str(value or ''))
    reason = re.sub(r'[\u200b-\u200f\u202a-\u202e\u2066-\u2069]', '', reason)
    reason = re.sub(r'\s+', ' ', reason).strip()
    if not reason:
        raise ValidationError('reason is required')
    if len(reason) > 256:
        raise ValidationError('reason must be at most 256 characters')
    return reason


class HiveCredentialRequest(models.Model):
    """Durable agent credential request queue.

    This is the Hive-owned replacement for cron/daemon broker
    request_credential. The requester is transport/auth derived by API callers;
    the reason is normalized and rendered as untrusted text to grantors.
    """

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        FULFILLED = 'fulfilled', 'Fulfilled'
        DENIED = 'denied', 'Denied'
        EXPIRED = 'expired', 'Expired'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    requester = models.ForeignKey(
        FleetAgent, on_delete=models.CASCADE, related_name='credential_requests'
    )
    requester_username = models.CharField(max_length=100, db_index=True)
    requester_role = models.CharField(max_length=100, blank=True)
    scope = models.CharField(max_length=32, choices=HIVE_CREDENTIAL_REQUEST_SCOPES, db_index=True)
    reason = models.TextField()
    requested_at = models.DateTimeField(auto_now_add=True, db_index=True)
    expiry = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True)
    fulfilled_by_credential = models.ForeignKey(
        HiveCredential, null=True, blank=True, on_delete=models.SET_NULL, related_name='fulfilled_requests'
    )
    resolved_by_user_id = models.CharField(max_length=64, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-requested_at']
        indexes = [
            models.Index(fields=['requester', 'scope', 'status']),
            models.Index(fields=['scope', 'status']),
        ]

    def clean(self):
        super().clean()
        self.reason = normalize_credential_request_reason(self.reason)
        if self.requester:
            self.requester_username = self.requester_username or self.requester.agent_username
            self.requester_role = self.requester_role or self.requester.role

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f'HiveCredentialRequest({self.requester_username}:{self.scope}:{self.status})'


def _canon_team_slug(value):
    return (value or '').strip().lower().replace('_', '-').replace(' ', '-')


class HiveCredentialAudit(models.Model):
    """Append-only redacted audit ledger for credential grant actions."""

    class Action(models.TextChoices):
        CREATE = 'create', 'Create'
        UPDATE = 'update', 'Update'
        REVOKE = 'revoke', 'Revoke'
        HEALTH_TEST = 'health-test', 'Health test'
        RECONCILE_APPLIED = 'reconcile-applied', 'Reconcile applied'
        RECONCILE_FAILED = 'reconcile-failed', 'Reconcile failed'

    credential = models.ForeignKey(HiveCredential, on_delete=models.CASCADE, related_name='audit_events')
    actor_user_id = models.CharField(max_length=64, blank=True)
    action = models.CharField(max_length=32, choices=Action.choices)
    target_scope = models.CharField(max_length=16)
    target_agent = models.CharField(max_length=100, blank=True)
    target_team = models.CharField(max_length=100, blank=True)
    redacted_before = models.JSONField(default=dict, blank=True)
    redacted_after = models.JSONField(default=dict, blank=True)
    result = models.CharField(max_length=32, default='ok')
    message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'HiveCredentialAudit({self.credential_id}, {self.action}, {self.result})'


class DaemonRegistry(models.Model):
    """Live registry of fleet daemons connected via the Hive↔daemon WebSocket link.

    The resume cursor (last_acked_seq) is persisted here so any Hive replica
    can return the correct resume_from_seq on reconnect (shared-cursor stickiness
    per HIVE-235 §hard-constraints).
    """

    id = models.BigAutoField(auto_created=True, primary_key=True, serialize=False)

    class RuntimeType(models.TextChoices):
        DOCKER = 'docker', 'Docker'
        K3S = 'k3s', 'k3s'

    class Status(models.TextChoices):
        CONNECTED = 'connected', 'Connected'
        DISCONNECTED = 'disconnected', 'Disconnected'
        DEGRADED = 'degraded', 'Degraded'
        DOWN = 'down', 'Down'

    daemon_id = models.CharField(max_length=128, unique=True, db_index=True)
    fleet_id = models.CharField(max_length=128, db_index=True)
    # PLAT-4235 P1 (operator 2026-07-12): per-user fleet daemons. Each user's Hive
    # has EXACTLY ONE independent k3s fleet daemon that manages that user's ENTIRE
    # fleet. `owner_subject` = the Shizuha-ID user subject this daemon serves; a
    # per-user daemon manages only that user's owned agents (INV-OWNER-SCOPE). NULL =
    # the shared org/team fleet daemon (backward-compatible: manages owner_id=NULL
    # org/team agents), so the existing single daemon keeps working unchanged.
    owner_subject = models.BigIntegerField(
        null=True, blank=True, db_index=True,
        help_text='Shizuha-ID user subject this daemon serves; NULL = shared org/team fleet daemon.',
    )
    owner_username = models.CharField(
        max_length=150, blank=True, default='',
        help_text='Non-authoritative owner display handle; owner_subject is the stable key.',
    )
    runtime = models.CharField(
        max_length=16, choices=RuntimeType.choices, default=RuntimeType.DOCKER
    )
    cred_ref = models.CharField(max_length=256, blank=True)
    last_seen = models.DateTimeField(null=True, blank=True)
    # INV-SEQ resume cursor: last seq for which Hive sent an ack to the daemon.
    # Persisted so a reconnect to ANY replica returns the correct resume_from_seq.
    last_acked_seq = models.PositiveBigIntegerField(default=0)
    # HIVE-277 §10 v16 protocol-v2 state. V1 keeps using last_acked_seq;
    # v2 advances the epoch-scoped committed cursor only after the full frame
    # transaction commits and never reuses a cursor under another epoch.
    selected_protocol_version = models.PositiveSmallIntegerField(default=1)
    active_runtime_epoch = models.UUIDField(null=True, blank=True)
    last_committed_frame_seq = models.PositiveBigIntegerField(default=0)
    protocol_capabilities = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.DISCONNECTED
    )
    # True once state_snapshot_complete has been received; reset on reconnect.
    snapshot_complete = models.BooleanField(default=False)
    # Set after the one permitted bottom-up config seed has completed. After this,
    # Hive is the desired-config authority; daemon snapshots may update runtime
    # observations but must not clobber Hive-owned config.
    config_seeded_at = models.DateTimeField(null=True, blank=True)
    # Discriminated-disconnect guard (HIVE-235 P1): stores the channel_name of the
    # connection that last registered. On disconnect, we only write DISCONNECTED when
    # ws_channel still matches our own channel_name — prevents a superseded connection's
    # disconnect() from clobbering the new connection's CONNECTED status.
    ws_channel = models.CharField(
        max_length=256,
        blank=True,
        default='',
        help_text='Channel name of the current active WS connection (for disconnect guard).',
    )
    # HIVE-600: the agent-runtime image this daemon spawns fleet agents from,
    # plus a best-effort snapshot of the daemon host's own CLI versions. Hive
    # resolves the authoritative fleet harness versions from the image's OCI
    # labels (org.shizuha.harness.*); this ref tells it which image to read.
    agent_runtime_image = models.CharField(max_length=512, blank=True, default='')
    harness_report = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        verbose_name_plural = 'Daemon registries'

    def __str__(self):
        return f'DaemonRegistry({self.daemon_id}, {self.status})'


class DaemonIdentityEvent(models.Model):
    """Persisted identity_event frames from the fleet daemon (HIVE-254 P5).

    An unresolved REJECTED event for a given child_id means the daemon detected
    an INV-ID violation for that child.  A subsequent ADMITTED or REPAIRED event
    marks the prior rejected rows resolved (resolved_at set).

    The identity-health endpoint surfaces all daemons with unresolved rejections
    so operators can see live INV-ID violations without parsing daemon logs.
    """

    class Verdict(models.TextChoices):
        ADMITTED = 'admitted', 'Admitted'
        REPAIRED = 'repaired', 'Repaired'
        REJECTED = 'rejected', 'Rejected'

    daemon = models.ForeignKey(
        DaemonRegistry,
        on_delete=models.CASCADE,
        related_name='identity_events',
    )
    child_id = models.CharField(max_length=256, db_index=True)
    username = models.CharField(max_length=150, blank=True)
    verdict = models.CharField(max_length=16, choices=Verdict.choices)
    reason = models.TextField(blank=True)
    seen_at = models.DateTimeField()
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-seen_at']
        indexes = [
            models.Index(fields=['daemon', 'resolved_at'], name='fleet_daemo_daemon_i_idx'),
        ]

    def __str__(self):
        return f'DaemonIdentityEvent({self.daemon_id}, {self.child_id}, {self.verdict})'


# ── HIVE-296 P1: desired-state + health snapshot + issue persistence ──────────

class AgentRuntimeDesiredState(models.Model):
    """Hive's desired-state specification for one fleet agent (HIVE-277 §3.1).

    Monotonically versioned: each capability-chain recompute or team-roster sync
    bumps desired_generation and recomputes desired_config_hash.  The daemon
    reports applied_generation/observed_generation back via AgentHealthSnapshot.
    """

    class Source(models.TextChoices):
        HIVE_UI = 'hive_ui', 'Hive UI'
        TEAM_ROSTER_SYNC = 'team_roster_sync', 'Team Roster Sync'
        CAPABILITY_RECOMPUTE = 'capability_recompute', 'Capability Recompute'
        ADMIN_OVERRIDE = 'admin_override', 'Admin Override'

    class DesiredLifecycle(models.TextChoices):
        ACTIVE = 'active', 'Active'
        STOPPED = 'stopped', 'Stopped'
        BLOCKED = 'blocked', 'Blocked'
        ARCHIVED = 'archived', 'Archived'
        DELETED = 'deleted', 'Deleted'

    class LifecycleExecutor(models.TextChoices):
        HIVE_LEGACY = 'hive_legacy', 'Hive legacy provisioner'
        DAEMON_V2 = 'daemon_v2', 'Fleet daemon v2'

    class OperationKind(models.TextChoices):
        ADOPT = 'adopt', 'Adopt'
        HANDOFF = 'handoff', 'Handoff'
        APPLY_CONFIG = 'apply_config', 'Apply config'
        START = 'start', 'Start'
        PROVISION = 'provision', 'Provision'
        STOP = 'stop', 'Stop'
        DELETE = 'delete', 'Delete'

    agent = models.OneToOneField(
        'FleetAgent',
        on_delete=models.CASCADE,
        related_name='desired_state',
    )
    # Canonical identity snapshot at generation write time
    platform_user_id = models.CharField(max_length=64, blank=True)
    canonical_username = models.CharField(max_length=150, blank=True)
    canonical_email = models.CharField(max_length=254, blank=True)
    # Desired runtime config
    team_memberships = models.JSONField(default=list, blank=True)
    effective_capabilities = models.JSONField(default=list, blank=True)
    skills = models.JSONField(default=dict, blank=True)
    mcp_access = models.JSONField(default=list, blank=True)
    runtime_profile = models.JSONField(default=dict, blank=True)
    credential_grant_expectations = models.JSONField(default=list, blank=True)
    # HIVE-277 §10 v16: desired lifecycle is an independent, generation-bound
    # Hive-owned facet. Runtime observations/results must never infer or mutate it.
    desired_lifecycle = models.CharField(
        max_length=16,
        choices=DesiredLifecycle.choices,
        default=DesiredLifecycle.ACTIVE,
        db_index=True,
    )
    operation_id = models.UUIDField(null=True, blank=True)
    operation_kind = models.CharField(
        max_length=16, choices=OperationKind.choices, blank=True, default='',
    )
    operation_started_at = models.DateTimeField(null=True, blank=True)
    operation_deadline = models.DateTimeField(null=True, blank=True)
    lifecycle_executor = models.CharField(
        max_length=16,
        choices=LifecycleExecutor.choices,
        default=LifecycleExecutor.HIVE_LEGACY,
    )
    executor_generation = models.PositiveBigIntegerField(default=1)
    expected_deployment_uid = models.CharField(max_length=64, blank=True, default='')
    workload_identity_generation = models.PositiveBigIntegerField(default=1)
    # Transitional PLAT-4163 hard-off. No v2 final reducer/protocol depends on
    # this field after the reviewed migration removes the legacy daemon bit.
    operator_disabled_transition = models.BooleanField(default=False)
    # Monotonic H-manifest fence. Every stable reducer-input mutation advances
    # this value in the same transaction as the row change.
    fleet_control_revision = models.PositiveBigIntegerField(default=1)
    # Generation / hash
    desired_generation = models.PositiveIntegerField(default=1)
    desired_config_hash = models.CharField(max_length=64, blank=True)
    previous_generation = models.PositiveIntegerField(null=True, blank=True)
    source = models.CharField(
        max_length=32, choices=Source.choices, default=Source.HIVE_UI,
    )
    source_actor = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Agent runtime desired state'

    def __str__(self):
        return f'AgentRuntimeDesiredState({self.agent_id}, gen={self.desired_generation})'


class RuntimeChange(models.Model):
    """Auditable, generation-fenced RuntimeLane rollout state."""

    class State(models.TextChoices):
        ACCEPTED = 'ACCEPTED', 'Accepted'
        ACTUATOR_PENDING = 'ACTUATOR_PENDING', 'Actuator pending'
        ROLLING = 'ROLLING', 'Rolling'
        HEALTHY = 'HEALTHY', 'Healthy'
        SUPERSEDED = 'SUPERSEDED', 'Superseded'
        FAILED = 'FAILED', 'Failed'
        ROLLBACK_PENDING = 'ROLLBACK_PENDING', 'Rollback pending'
        ROLLED_BACK = 'ROLLED_BACK', 'Rolled back'
        ROLLBACK_FAILED = 'ROLLBACK_FAILED', 'Rollback failed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agent = models.ForeignKey(
        'FleetAgent', on_delete=models.CASCADE, related_name='runtime_changes',
    )
    base_generation = models.PositiveIntegerField()
    desired_generation = models.PositiveIntegerField()
    requested_patch = models.JSONField(default=dict, blank=True)
    resolved_lane = models.JSONField(default=dict)
    previous_lane = models.JSONField(default=dict, blank=True)
    registry_version = models.CharField(max_length=64)
    runtime_release_id = models.CharField(max_length=160)
    lane_digest = models.CharField(max_length=64)
    state = models.CharField(
        max_length=32, choices=State.choices, default=State.ACCEPTED, db_index=True,
    )
    failure_code = models.CharField(max_length=80, blank=True)
    failure_evidence = models.TextField(blank=True)
    rollback_of = models.OneToOneField(
        'self', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='rollback_change',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-desired_generation']
        constraints = [
            models.UniqueConstraint(
                fields=['agent', 'desired_generation'],
                name='uniq_runtime_change_agent_generation',
            ),
        ]
        indexes = [
            models.Index(fields=['agent', 'state'], name='fleet_rtchg_agent_state_idx'),
        ]


class RuntimeLaneOutbox(models.Model):
    """Durable full-lane actuator event, retained until exact fence ack."""

    change = models.OneToOneField(
        RuntimeChange, on_delete=models.CASCADE, related_name='outbox',
    )
    payload = models.JSONField(default=dict)
    attempt_count = models.PositiveIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class AgentRuntimeMigrationState(models.Model):
    """Explicit fail-closed disposition for ambiguous legacy lifecycle rows.

    A held row stays observable in H but grants no start, recovery or routing
    authority. Only the audited ``resolve_migration_hold`` operation may release
    it; runtime evidence, reconnects and timer execution cannot.
    """

    class State(models.TextChoices):
        NOT_REQUIRED = 'not_required', 'Not required'
        HELD = 'held', 'Held'
        RELEASED = 'released', 'Released'

    agent = models.OneToOneField(
        'FleetAgent', on_delete=models.CASCADE, related_name='runtime_migration_state',
    )
    migration_state = models.CharField(
        max_length=16, choices=State.choices, default=State.NOT_REQUIRED, db_index=True,
    )
    migration_generation = models.PositiveBigIntegerField(default=1)
    legacy_source_status = models.CharField(max_length=32, blank=True, default='')
    source_row_version = models.CharField(max_length=128, blank=True, default='')
    source_row_hash = models.CharField(max_length=64, blank=True, default='')
    source_timestamp = models.DateTimeField(null=True, blank=True)
    hold_reason = models.CharField(max_length=64, blank=True, default='')
    evidence = models.JSONField(default=dict, blank=True)
    held_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)
    released_by = models.CharField(max_length=150, blank=True, default='')
    release_reason = models.CharField(max_length=300, blank=True, default='')
    release_operation_id = models.UUIDField(null=True, blank=True)
    target_lifecycle = models.CharField(
        max_length=16,
        choices=AgentRuntimeDesiredState.DesiredLifecycle.choices,
        blank=True,
        default='',
    )
    released_desired_generation = models.PositiveBigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class AgentDaemonBinding(models.Model):
    """Hive-owned daemon binding; observation frames can never claim ownership."""

    class State(models.TextChoices):
        UNBOUND = 'unbound', 'Unbound'
        ADOPTING = 'adopting', 'Adopting'
        BOUND = 'bound', 'Bound'
        HANDING_OFF = 'handing_off', 'Handing off'
        REVOKED = 'revoked', 'Revoked'

    agent = models.OneToOneField(
        'FleetAgent', on_delete=models.CASCADE, related_name='daemon_binding',
    )
    active_owner_daemon = models.ForeignKey(
        DaemonRegistry,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='active_agent_bindings',
    )
    owner_generation = models.PositiveBigIntegerField(default=0)
    active_runtime_epoch = models.UUIDField(null=True, blank=True)
    binding_state = models.CharField(
        max_length=16, choices=State.choices, default=State.UNBOUND, db_index=True,
    )
    candidate_owner_daemon = models.ForeignKey(
        DaemonRegistry,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='candidate_agent_bindings',
    )
    candidate_owner_generation = models.PositiveBigIntegerField(null=True, blank=True)
    candidate_runtime_epoch = models.UUIDField(null=True, blank=True)
    previous_owner_daemon = models.ForeignKey(
        DaemonRegistry,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='previous_agent_bindings',
    )
    binding_operation_id = models.UUIDField(null=True, blank=True)
    owner_lease_expires_at = models.DateTimeField(null=True, blank=True)
    actor = models.CharField(max_length=150, blank=True, default='')
    reason = models.CharField(max_length=300, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class AgentRuntimeObservation(models.Model):
    """Current activated owner-fenced runtime evidence for one agent.

    Candidate snapshot rows are held separately in ``RuntimeSnapshotCandidate``;
    only a verified H∪R∪L barrier activation replaces this row.
    """

    class Adapter(models.TextChoices):
        K3S = 'k3s', 'k3s'
        DOCKER = 'docker', 'Docker'

    agent = models.OneToOneField(
        'FleetAgent', on_delete=models.CASCADE, related_name='runtime_observation',
    )
    owner_daemon = models.ForeignKey(
        DaemonRegistry, on_delete=models.CASCADE, related_name='runtime_observations',
    )
    owner_generation = models.PositiveBigIntegerField()
    runtime_epoch = models.UUIDField()
    frame_seq = models.PositiveBigIntegerField()
    observation_seq = models.PositiveBigIntegerField()
    source_adapter = models.CharField(max_length=16, choices=Adapter.choices)
    source_ok = models.BooleanField(default=False)
    source_error = models.CharField(max_length=128, blank=True, default='')
    snapshot_complete = models.BooleanField(default=False)
    observed_at = models.DateTimeField()
    received_at = models.DateTimeField(auto_now_add=True)
    valid_until = models.DateTimeField()
    desired_generation = models.PositiveBigIntegerField()
    deployment_uid = models.CharField(max_length=64, blank=True, default='')
    deployment_generation = models.PositiveBigIntegerField(null=True, blank=True)
    deployment_observed_generation = models.PositiveBigIntegerField(null=True, blank=True)
    pod_uids = models.JSONField(default=list, blank=True)
    phase = models.CharField(max_length=32, blank=True, default='')
    required_replicas = models.PositiveIntegerField(default=1)
    ready_replicas = models.PositiveIntegerField(default=0)
    required_containers = models.PositiveIntegerField(default=0)
    ready_containers = models.PositiveIntegerField(default=0)
    restart_count_5m = models.PositiveIntegerField(default=0)
    unready_since = models.DateTimeField(null=True, blank=True)
    progress_deadline_exceeded = models.BooleanField(default=False)
    runtime_present = models.BooleanField(default=False)
    daemon_local_present = models.BooleanField(default=False)
    resource_count = models.PositiveIntegerField(default=0)
    classification = models.CharField(max_length=64, blank=True, default='')
    evidence = models.JSONField(default=dict, blank=True)
    active_cut_id = models.UUIDField()
    final_u_digest = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['agent', 'owner_generation', 'runtime_epoch', 'observation_seq'],
                name='fleet_runtime_observation_order_uq',
            ),
        ]
        indexes = [
            models.Index(fields=['owner_daemon', 'runtime_epoch', 'frame_seq'], name='fleet_obs_owner_frame_idx'),
            models.Index(fields=['valid_until'], name='fleet_obs_valid_until_idx'),
        ]


class AgentRuntimeProjection(models.Model):
    """Authoritative, versioned runtime-intent facet shared by all consumers."""

    agent = models.OneToOneField(
        'FleetAgent', on_delete=models.CASCADE, related_name='runtime_projection',
    )
    observation = models.ForeignKey(
        AgentRuntimeObservation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='projections',
    )
    effective_status = models.CharField(max_length=16, default='unknown', db_index=True)
    runtime_new_assignment_eligible = models.BooleanField(default=False)
    runtime_owner_drain_required = models.BooleanField(default=False)
    runtime_hard_off = models.BooleanField(default=False)
    reason_codes = models.JSONField(default=list, blank=True)
    action_code = models.CharField(max_length=64, blank=True, default='')
    evidence_valid_until = models.DateTimeField(null=True, blank=True)
    next_transition_at = models.DateTimeField(null=True, blank=True)
    reducer_version = models.PositiveIntegerField(default=5)
    desired_generation = models.PositiveBigIntegerField(default=0)
    owner_generation = models.PositiveBigIntegerField(default=0)
    runtime_epoch = models.UUIDField(null=True, blank=True)
    observation_seq = models.PositiveBigIntegerField(default=0)
    decision_version = models.PositiveBigIntegerField(default=1)
    decision_hash = models.CharField(max_length=64)
    computed_at = models.DateTimeField(auto_now=True)


class RuntimeSnapshotCandidate(models.Model):
    """Durable candidate R∪L cut awaiting Hive-owned H composition."""

    class State(models.TextChoices):
        STAGING = 'staging', 'Staging'
        READY = 'ready', 'Ready'
        ACTIVATED = 'activated', 'Activated'
        REJECTED = 'rejected', 'Rejected'
        EXPIRED = 'expired', 'Expired'

    snapshot_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    daemon = models.ForeignKey(
        DaemonRegistry, on_delete=models.CASCADE, related_name='runtime_snapshot_candidates',
    )
    owner_generation = models.PositiveBigIntegerField()
    executor_generation = models.PositiveBigIntegerField()
    runtime_epoch = models.UUIDField()
    h_manifest = models.JSONField(default=dict)
    h_revision = models.PositiveBigIntegerField()
    h_member_count = models.PositiveIntegerField()
    h_members_digest = models.CharField(max_length=64)
    r_collection_fences = models.JSONField(default=list)
    l_snapshot_seq = models.PositiveBigIntegerField()
    l_barrier_seq = models.PositiveBigIntegerField()
    barrier_end_frame_seq = models.PositiveBigIntegerField()
    rl_entries = models.JSONField(default=list)
    rl_count = models.PositiveIntegerField()
    rl_digest = models.CharField(max_length=64)
    final_u_digest = models.CharField(max_length=64, blank=True, default='')
    state = models.CharField(max_length=16, choices=State.choices, default=State.STAGING)
    failure_code = models.CharField(max_length=64, blank=True, default='')
    candidate_deadline = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    activated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['daemon', 'runtime_epoch', 'state'], name='fleet_snapshot_epoch_idx'),
            models.Index(fields=['candidate_deadline', 'state'], name='fleet_snapshot_deadline_idx'),
        ]


class RuntimeBoundaryTimer(models.Model):
    """Leased, idempotent timer for evidence/operation boundary transitions."""

    class State(models.TextChoices):
        PENDING = 'pending', 'Pending'
        CLAIMED = 'claimed', 'Claimed'
        COMPLETED = 'completed', 'Completed'
        SUPERSEDED = 'superseded', 'Superseded'

    timer_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agent = models.ForeignKey('FleetAgent', on_delete=models.CASCADE, related_name='runtime_boundary_timers')
    boundary_kind = models.CharField(max_length=32)
    boundary_fingerprint = models.CharField(max_length=64, unique=True)
    due_at = models.DateTimeField(db_index=True)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING, db_index=True)
    claim_owner = models.CharField(max_length=128, blank=True, default='')
    claim_lease_until = models.DateTimeField(null=True, blank=True)
    attempt_count = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=512, blank=True, default='')
    failure_latched = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)


class RuntimeAvailabilityOutbox(models.Model):
    """At-least-once invalidation delivery; reducer projection remains truth."""

    class State(models.TextChoices):
        PENDING = 'pending', 'Pending'
        CLAIMED = 'claimed', 'Claimed'
        DELIVERED = 'delivered', 'Delivered'
        SUPERSEDED = 'superseded', 'Superseded'

    outbox_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    schema_version = models.PositiveSmallIntegerField(default=1)
    idempotency_key = models.CharField(max_length=256, unique=True)
    agent = models.ForeignKey('FleetAgent', on_delete=models.CASCADE, related_name='runtime_availability_events')
    owner_generation = models.PositiveBigIntegerField(default=0)
    runtime_epoch = models.UUIDField(null=True, blank=True)
    observation_seq = models.PositiveBigIntegerField(default=0)
    reducer_version = models.PositiveIntegerField(default=5)
    decision_version = models.PositiveBigIntegerField()
    payload = models.JSONField(default=dict)
    payload_hash = models.CharField(max_length=64)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING, db_index=True)
    claim_owner = models.CharField(max_length=128, blank=True, default='')
    # Short-lived internal CAS nonce. It is never serialized by API/admin/log code.
    claim_token = models.CharField(max_length=128, blank=True, default='')
    claim_lease_until = models.DateTimeField(null=True, blank=True)
    attempt_count = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=512, blank=True, default='')
    failure_latched = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    delivery_receipt = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['state', 'next_attempt_at', 'created_at'], name='fleet_avail_outbox_fifo_idx'),
            models.Index(fields=['agent', 'decision_version'], name='fleet_avail_agent_ver_idx'),
        ]


class AgentLifecycleCommand(models.Model):
    """Generation-bound command and durable delivery ledger."""

    class State(models.TextChoices):
        PENDING = 'pending', 'Pending'
        ACKED = 'acked', 'ACKed'
        SUCCEEDED = 'succeeded', 'Succeeded'
        REJECTED = 'rejected', 'Rejected'
        FAILED = 'failed', 'Failed'
        SUPERSEDED = 'superseded', 'Superseded'
        EXPIRED = 'expired', 'Expired'

    command_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agent = models.ForeignKey('FleetAgent', on_delete=models.CASCADE, related_name='lifecycle_commands')
    payload = models.JSONField(default=dict)
    payload_hash = models.CharField(max_length=64)
    command_type = models.CharField(max_length=32)
    desired_generation = models.PositiveBigIntegerField()
    owner_generation = models.PositiveBigIntegerField()
    runtime_epoch = models.UUIDField(null=True, blank=True)
    lifecycle_executor = models.CharField(
        max_length=16, choices=AgentRuntimeDesiredState.LifecycleExecutor.choices,
    )
    executor_generation = models.PositiveBigIntegerField()
    expected_resource_uid = models.CharField(max_length=64, blank=True, default='')
    operation_id = models.UUIDField()
    deadline = models.DateTimeField()
    actor = models.CharField(max_length=150, blank=True, default='')
    reason = models.CharField(max_length=300, blank=True, default='')
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING, db_index=True)
    acked_at = models.DateTimeField(null=True, blank=True)
    result_payload = models.JSONField(default=dict, blank=True)
    result_reason = models.CharField(max_length=300, blank=True, default='')
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['state', 'created_at'], name='fleet_lifecycle_cmd_fifo_idx'),
            models.Index(fields=['agent', 'desired_generation', 'executor_generation'], name='fleet_lifecycle_cmd_gen_idx'),
        ]


class AgentWorkloadIdentityTransition(models.Model):
    """Bounded, command-authorized Deployment UID replacement transition."""

    class State(models.TextChoices):
        PENDING = 'pending', 'Pending'
        CANDIDATE_SEEN = 'candidate_seen', 'Candidate seen'
        PROMOTED = 'promoted', 'Promoted'
        FAILED = 'failed', 'Failed'
        EXPIRED = 'expired', 'Expired'
        SUPERSEDED = 'superseded', 'Superseded'

    transition_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    operation_id = models.UUIDField()
    command = models.OneToOneField(AgentLifecycleCommand, on_delete=models.CASCADE, related_name='workload_transition')
    agent = models.ForeignKey('FleetAgent', on_delete=models.CASCADE, related_name='workload_identity_transitions')
    desired_generation = models.PositiveBigIntegerField()
    owner_generation = models.PositiveBigIntegerField()
    executor_generation = models.PositiveBigIntegerField()
    from_deployment_uid = models.CharField(max_length=64, blank=True, default='')
    candidate_deployment_uid = models.CharField(max_length=64, blank=True, default='')
    state = models.CharField(max_length=20, choices=State.choices, default=State.PENDING, db_index=True)
    deadline = models.DateTimeField()
    evidence = models.JSONField(default=dict, blank=True)
    terminal_reason = models.CharField(max_length=128, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)


class AgentRecoveryEpisode(models.Model):
    """Non-renewable repair budget across UID/classifier/epoch changes."""

    class State(models.TextChoices):
        OPEN = 'open', 'Open'
        EXHAUSTED = 'exhausted', 'Exhausted'
        RESOLVED_HEALTHY = 'resolved_healthy', 'Resolved healthy'
        SUPERSEDED_GENERATION = 'superseded_generation', 'Superseded generation'
        MANUALLY_RESET = 'manually_reset', 'Manually reset'

    episode_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agent = models.ForeignKey('FleetAgent', on_delete=models.CASCADE, related_name='recovery_episodes')
    desired_generation = models.PositiveBigIntegerField()
    owner_generation = models.PositiveBigIntegerField()
    state = models.CharField(max_length=24, choices=State.choices, default=State.OPEN, db_index=True)
    opening_reason = models.CharField(max_length=64)
    opening_evidence = models.JSONField(default=dict)
    issue_classes = models.JSONField(default=list, blank=True)
    resource_uids = models.JSONField(default=list, blank=True)
    budget_deadline = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    healthy_cut_count = models.PositiveSmallIntegerField(default=0)
    reset_actor = models.CharField(max_length=150, blank=True, default='')
    reset_reason = models.CharField(max_length=300, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['agent', 'desired_generation', 'owner_generation'],
                condition=models.Q(state__in=['open', 'exhausted']),
                name='fleet_one_unresolved_recovery_episode',
            ),
        ]


class AgentHealthSnapshot(models.Model):
    """Latest observed runtime health snapshot for one agent/daemon pair (HIVE-277 §3.2/§3.3).

    Populated from daemon `health_snapshot` frames; Hive replaces the row on
    each full snapshot so that the table represents latest-known state per
    (agent, daemon) pair, not a time-series.  AgentHealthIssue rows persist
    the normalized open/resolved issue log.
    """

    class SnapshotType(models.TextChoices):
        FULL = 'full', 'Full'
        DELTA = 'delta', 'Delta'

    class ApplyStatus(models.TextChoices):
        OK = 'ok', 'OK'
        PENDING = 'pending', 'Pending'
        FAILED = 'failed', 'Failed'
        UNKNOWN = 'unknown', 'Unknown'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agent = models.ForeignKey(
        'FleetAgent', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='health_snapshots',
    )
    daemon = models.ForeignKey(
        'DaemonRegistry', on_delete=models.CASCADE,
        related_name='health_snapshots',
    )

    # Runtime identity
    runtime_id = models.CharField(max_length=256, blank=True)
    container_pod_id = models.CharField(max_length=256, blank=True)
    runtime_username = models.CharField(max_length=150, blank=True)
    runtime_email = models.CharField(max_length=254, blank=True)
    last_heartbeat_at = models.DateTimeField(null=True, blank=True)
    last_heartbeat_identity_email = models.CharField(max_length=254, blank=True)

    # Platform identity (cross-validated against Shizuha-ID)
    platform_user_id = models.CharField(max_length=64, blank=True)
    canonical_email = models.CharField(max_length=254, blank=True)
    account_type = models.CharField(max_length=50, blank=True)
    account_active = models.BooleanField(null=True, blank=True)
    alias_resolution_evidence = models.JSONField(default=dict, blank=True)

    # Desired/apply generation tracking
    desired_generation_seen = models.PositiveIntegerField(null=True, blank=True)
    applied_generation = models.PositiveIntegerField(null=True, blank=True)
    observed_generation = models.PositiveIntegerField(null=True, blank=True)
    observed_config_hash = models.CharField(max_length=64, blank=True)
    apply_status = models.CharField(
        max_length=16, choices=ApplyStatus.choices, default=ApplyStatus.UNKNOWN,
    )
    last_apply_error = models.TextField(blank=True)

    # Capabilities / grants
    effective_capabilities = models.JSONField(default=list, blank=True)
    skills = models.JSONField(default=dict, blank=True)
    mcp_access = models.JSONField(default=list, blank=True)
    credential_grant_health = models.JSONField(default=dict, blank=True)
    missing_grants = models.JSONField(default=list, blank=True)
    extra_grants = models.JSONField(default=list, blank=True)

    # Routing
    pulse_assignable = models.BooleanField(null=True, blank=True)
    roster_assignable_reason = models.TextField(blank=True)
    team_roster_memberships = models.JSONField(default=list, blank=True)

    # Provider health (no secret values — availability/quota/backoff only)
    provider_health = models.JSONField(default=dict, blank=True)

    snapshot_type = models.CharField(
        max_length=8, choices=SnapshotType.choices, default=SnapshotType.FULL,
    )
    source_error = models.TextField(blank=True)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-received_at']
        indexes = [
            models.Index(fields=['agent', '-received_at'], name='fleet_snap_agent_recv_idx'),
            models.Index(fields=['daemon', '-received_at'], name='fleet_snap_daemon_recv_idx'),
        ]

    def __str__(self):
        return f'AgentHealthSnapshot({self.agent_id}, {self.daemon_id}, {self.snapshot_type})'


class AgentHealthIssue(models.Model):
    """Normalized persisted issue from health snapshot classifiers (HIVE-277 §3.3).

    Open issues are surfaced on /hive/agents and /hive/teams badges.  Resolved
    issues are retained for audit.  A snapshot upsert that no longer triggers a
    classifier resolves the corresponding open issue row.
    """

    class IssueType(models.TextChoices):
        IDENTITY_ALIAS_DRIFT = 'identity_alias_drift', 'Identity alias drift'
        IDENTITY_UNRESOLVED = 'identity_unresolved', 'Identity unresolved'
        PLATFORM_USER_MISMATCH = 'platform_user_mismatch', 'Platform user mismatch'
        INACTIVE_OR_WRONG_ACCOUNT_TYPE = 'inactive_or_wrong_account_type', 'Inactive or wrong account type'
        CREDENTIAL_GRANT_MISSING = 'credential_grant_missing', 'Credential grant missing'
        PROVIDER_UNAVAILABLE_OR_QUOTA = 'provider_unavailable_or_quota', 'Provider unavailable or quota'
        ROSTER_UNASSIGNABLE = 'roster_unassignable', 'Roster unassignable'
        HEARTBEAT_IDENTITY_MISMATCH = 'heartbeat_identity_mismatch', 'Heartbeat identity mismatch'
        DESIRED_GENERATION_UNSEEN = 'desired_generation_unseen', 'Desired generation unseen'
        APPLIED_GENERATION_STALE = 'applied_generation_stale', 'Applied generation stale'
        OBSERVED_CONFIG_DRIFT = 'observed_config_drift', 'Observed config drift'
        CAPABILITY_GRANT_MISSING = 'capability_grant_missing', 'Capability grant missing'
        UNEXPECTED_CAPABILITY_GRANT = 'unexpected_capability_grant', 'Unexpected capability grant'
        MCP_ACCESS_DRIFT = 'mcp_access_drift', 'MCP access drift'
        SKILL_PROFILE_DRIFT = 'skill_profile_drift', 'Skill/profile drift'

    class Severity(models.TextChoices):
        CRITICAL = 'critical', 'Critical'
        HIGH = 'high', 'High'
        MEDIUM = 'medium', 'Medium'
        LOW = 'low', 'Low'
        INFO = 'info', 'Info'

    class Status(models.TextChoices):
        OPEN = 'open', 'Open'
        RESOLVED = 'resolved', 'Resolved'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    snapshot = models.ForeignKey(
        AgentHealthSnapshot, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='issues',
    )
    agent = models.ForeignKey(
        'FleetAgent', on_delete=models.CASCADE, related_name='health_issues',
    )

    issue_type = models.CharField(
        max_length=64, choices=IssueType.choices, db_index=True,
    )
    severity = models.CharField(
        max_length=16, choices=Severity.choices, default=Severity.MEDIUM,
    )
    service = models.CharField(max_length=64, blank=True)
    field_path = models.CharField(max_length=256, blank=True)
    expected = models.JSONField(null=True, blank=True)
    actual = models.JSONField(null=True, blank=True)
    detail = models.TextField(blank=True)

    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.OPEN, db_index=True,
    )
    first_seen = models.DateTimeField()
    last_seen = models.DateTimeField()
    resolved_at = models.DateTimeField(null=True, blank=True)

    recovery_policy = models.CharField(max_length=100, blank=True)
    recovery_action_id = models.UUIDField(null=True, blank=True)

    class Meta:
        ordering = ['-last_seen']
        indexes = [
            models.Index(fields=['agent', 'status'], name='fleet_issue_agent_status_idx'),
            models.Index(fields=['issue_type', 'status'], name='fleet_issue_type_status_idx'),
        ]

    def __str__(self):
        return f'AgentHealthIssue({self.agent_id}, {self.issue_type}, {self.status})'


# ── HIVE-332 P3: bidirectional recovery-action protocol ───────────────────────

# Safe, daemon-owned, dry-run-capable actions (HLD 0d8a2a74 §5 automatic v1 set).
SAFE_RECOVERY_ACTIONS = frozenset({
    'pull_desired_state_now',
    'reconcile_identity_alias',
    'refresh_credential_grants',
    'refresh_team_roster_cache',
    'apply_effective_runtime_config',
    'restart_agent',
})

# Unsafe/high-impact or privilege-expanding operations. NEVER valid action
# types in v1 — creation is rejected in clean() and the daemon independently
# denies them (defense in depth). A HIVE-277 P4 task with Security/CoS-approved
# auto-apply policy may relax specific entries; until then this is a hard
# code-level gate (HLD §5/§8).
GATED_RECOVERY_ACTIONS = frozenset({
    'cross_org_identity_change',
    'create_user',
    'delete_user',
    'grant_external_credential',
    'grant_billing_credential',
    'mutate_team_roster',
    'disable_agent',
    'delete_agent',
    'restart_fleet',
    'restart_daemon',
    'expand_privileges',
})


def _setting_action_set(name, default=()):
    """Return a normalized recovery-action setting set.

    Settings may be a Python iterable (tests) or comma-separated env string
    (production). Empty means deny all. Wildcards are intentionally not
    supported: HIVE-348 approved per-action rollout, so production must name
    every auto-apply action deliberately.
    """
    value = getattr(settings, name, default)
    if value is None:
        return set(default or ())
    if isinstance(value, str):
        return {item.strip() for item in value.split(',') if item.strip()}
    return {str(item).strip() for item in value if str(item).strip()}


def recovery_auto_apply_allowed(action_type: str) -> tuple[bool, str]:
    """P4 bounded auto-apply policy for safe recovery actions.

    Auto-apply remains default-off and requires BOTH the global feature flag and
    a per-action allowlist entry. The hard deny-list overrides the allowlist so
    rollback can immediately force an action back to observe/dry-run mode.
    """
    action = (action_type or '').strip()
    if action in GATED_RECOVERY_ACTIONS:
        return False, 'hard-deny-list'
    if action not in SAFE_RECOVERY_ACTIONS:
        return False, 'unknown-action'
    if not getattr(settings, 'HIVE_RECOVERY_AUTO_APPLY_ENABLED', False):
        return False, 'feature-flag-disabled'
    denylist = _setting_action_set('HIVE_RECOVERY_AUTO_APPLY_DENY_ACTIONS')
    if action in denylist:
        return False, 'per-action-denylist'
    allowlist = _setting_action_set('HIVE_RECOVERY_AUTO_APPLY_ACTIONS')
    if action in allowlist:
        return True, 'allowed'
    return False, 'not-in-allowlist'

_HEX_DIGEST_RE = re.compile(r'^[0-9a-f]{32,64}$')
_RECOVERY_SECRET_REJECTION_COUNT = 0

# Token material embedded inside a longer string (e.g. a daemon error message
# that interpolated a credential): known token prefixes or JWT-shaped runs.
_EMBEDDED_SECRET_RE = re.compile(
    r'(?:ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|sk-|xoxb-|xoxp-|glpat-)[A-Za-z0-9_-]{8,}'
    r'|[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{8,}'
)


def scan_recovery_payload_for_secrets(value, path='payload'):
    """Reject recovery params/summaries containing plaintext credential material.

    Pure-hex digests (config hashes) are allowed — they are how summaries
    reference config without carrying it. Everything else runs through the
    same heuristics as credential secret_refs.
    """
    if isinstance(value, str):
        candidate = value.strip()
        if _HEX_DIGEST_RE.match(candidate.lower()):
            return
        if _looks_like_plaintext_secret(candidate) or _EMBEDDED_SECRET_RE.search(candidate):
            record_recovery_secret_rejection()
            raise ValidationError(
                f'{path} contains plaintext credential material; recovery payloads '
                'must carry names/hashes only, never secret values'
            )
    elif isinstance(value, dict):
        for key, item in value.items():
            scan_recovery_payload_for_secrets(item, f'{path}.{key}')
    elif isinstance(value, (list, tuple)):
        for idx, item in enumerate(value):
            scan_recovery_payload_for_secrets(item, f'{path}[{idx}]')


def record_recovery_secret_rejection():
    """Process-local counter for recovery payloads rejected by secret hygiene."""
    global _RECOVERY_SECRET_REJECTION_COUNT
    _RECOVERY_SECRET_REJECTION_COUNT += 1


def recovery_secret_rejection_count():
    """Return recovery payload secret-rejection count for metrics exposition."""
    return _RECOVERY_SECRET_REJECTION_COUNT


def validate_apply_effective_runtime_config_provenance(agent, params):
    """Require apply-mode runtime config to target Hive's current desired state.

    The daemon cannot be the source of truth for config provenance.  Before an
    apply-mode ``apply_effective_runtime_config`` leaves Hive, its generation
    and hash must match the current AgentRuntimeDesiredState row for the same
    agent; dry-runs may still carry partial parameters for observation.
    """
    params = params or {}
    generation = params.get('desired_generation')
    config_hash = str(params.get('desired_config_hash') or '').strip().lower()
    if generation is None or not config_hash:
        raise ValidationError({
            'params': 'apply_effective_runtime_config apply mode requires '
                      'desired_generation and desired_config_hash from Hive desired state',
        })
    try:
        generation = int(generation)
    except (TypeError, ValueError):
        raise ValidationError({'params': 'desired_generation must be an integer'})
    if not _HEX_DIGEST_RE.match(config_hash):
        raise ValidationError({'params': 'desired_config_hash must be a 32-64 char hex digest'})
    try:
        desired_state = agent.desired_state
    except AgentRuntimeDesiredState.DoesNotExist:
        desired_state = None
    if desired_state is None:
        raise ValidationError({
            'params': 'apply_effective_runtime_config apply mode requires a current '
                      'Hive AgentRuntimeDesiredState row for this agent',
        })
    if (
        desired_state.desired_generation != generation
        or (desired_state.desired_config_hash or '').lower() != config_hash
    ):
        raise ValidationError({
            'params': 'apply_effective_runtime_config provenance does not match the '
                      'current Hive desired-state generation/hash',
        })


class AgentRecoveryAction(models.Model):
    """One Hive-requested recovery action with its full audit trail (HIVE-277 §3.3/§5).

    State machine: PENDING → SENT → {WOULD_APPLY, APPLIED, SKIPPED, DENIED,
    FAILED} (terminal daemon results), with EXPIRED reachable from PENDING/SENT
    when expires_at passes before a result lands. Rows are append-only audit:
    they are never deleted when issues resolve.

    v1 gating lives in clean(): only SAFE_RECOVERY_ACTIONS are creatable,
    apply mode requires the (default-off) HIVE_RECOVERY_AUTO_APPLY_ENABLED
    setting reserved for the P4 Security/CoS-approved policy, and no payload
    field may carry secret material.
    """

    class ActionType(models.TextChoices):
        PULL_DESIRED_STATE_NOW = 'pull_desired_state_now', 'Pull desired state now'
        RECONCILE_IDENTITY_ALIAS = 'reconcile_identity_alias', 'Reconcile identity alias'
        REFRESH_CREDENTIAL_GRANTS = 'refresh_credential_grants', 'Refresh credential grants'
        REFRESH_TEAM_ROSTER_CACHE = 'refresh_team_roster_cache', 'Refresh team roster cache'
        APPLY_EFFECTIVE_RUNTIME_CONFIG = 'apply_effective_runtime_config', 'Apply effective runtime config'
        RESTART_AGENT = 'restart_agent', 'Restart agent'

    class Mode(models.TextChoices):
        DRY_RUN = 'dry_run', 'Dry run'
        APPLY = 'apply', 'Apply'

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        SENT = 'sent', 'Sent'
        WOULD_APPLY = 'would_apply', 'Would apply'
        APPLIED = 'applied', 'Applied'
        SKIPPED = 'skipped', 'Skipped'
        DENIED = 'denied', 'Denied'
        FAILED = 'failed', 'Failed'
        EXPIRED = 'expired', 'Expired'

    # Result states a daemon may report, mapped 1:1 onto Status values.
    DAEMON_RESULT_STATES = frozenset({
        'would_apply', 'applied', 'skipped', 'denied', 'failed',
    })

    TERMINAL_STATUSES = frozenset({
        'would_apply', 'applied', 'skipped', 'denied', 'failed', 'expired',
    })

    _VALID_TRANSITIONS = {
        'pending': frozenset({'sent', 'denied', 'expired'}),
        'sent': frozenset({'would_apply', 'applied', 'skipped', 'denied', 'failed', 'expired'}),
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agent = models.ForeignKey(
        'FleetAgent', on_delete=models.CASCADE, related_name='recovery_actions',
    )
    daemon = models.ForeignKey(
        'DaemonRegistry', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='recovery_actions',
    )
    issue_ids = models.JSONField(
        default=list, blank=True,
        help_text='AgentHealthIssue UUIDs this action targets.',
    )
    action_type = models.CharField(max_length=48, choices=ActionType.choices, db_index=True)
    mode = models.CharField(max_length=16, choices=Mode.choices, default=Mode.DRY_RUN)
    target_runtime_id = models.CharField(max_length=256, blank=True)
    params = models.JSONField(
        default=dict, blank=True,
        help_text='Action parameters (names/hashes only — secret material is rejected).',
    )
    expires_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True,
    )
    before_summary = models.JSONField(default=dict, blank=True)
    after_summary = models.JSONField(default=dict, blank=True)
    daemon_result = models.JSONField(default=dict, blank=True)
    result_reason = models.TextField(blank=True)
    correlation_id = models.UUIDField(default=uuid.uuid4, unique=True, db_index=True)
    actor = models.CharField(max_length=150, blank=True)
    source = models.CharField(
        max_length=32, default='hive_api',
        help_text="Origin of the request (hive_ui, hive_api, auto_policy, …).",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['agent', 'status'], name='fleet_recov_agent_status_idx'),
            models.Index(fields=['action_type', 'status'], name='fleet_recov_type_status_idx'),
        ]

    def __str__(self):
        return f'AgentRecoveryAction({self.agent_id}, {self.action_type}, {self.mode}, {self.status})'

    @property
    def is_expired(self):
        return bool(self.expires_at and self.expires_at <= timezone.now())

    def clean(self):
        super().clean()
        action = (self.action_type or '').strip()
        if action in GATED_RECOVERY_ACTIONS:
            raise ValidationError({
                'action_type': f'action {action!r} is unsafe/privilege-expanding and gated in v1 '
                               '(HIVE-277 P4 Security/CoS-approved policy required)',
            })
        if action not in SAFE_RECOVERY_ACTIONS:
            raise ValidationError({'action_type': f'unknown recovery action {action!r} — default-deny'})
        if self.mode == self.Mode.APPLY:
            allowed, reason = recovery_auto_apply_allowed(action)
            if not allowed:
                raise ValidationError({
                    'mode': 'apply mode denied by HIVE-277 P4 auto-apply policy '
                            f'({reason}); leave action in dry_run or update the feature flag + '
                            'per-action allowlist after Security/CoS sign-off',
                })
            if action == self.ActionType.APPLY_EFFECTIVE_RUNTIME_CONFIG:
                validate_apply_effective_runtime_config_provenance(self.agent, self.params)
        for field in ('params', 'before_summary', 'after_summary', 'daemon_result'):
            scan_recovery_payload_for_secrets(getattr(self, field) or {}, field)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def _transition(self, new_status):
        allowed = self._VALID_TRANSITIONS.get(self.status, frozenset())
        if new_status not in allowed:
            raise ValidationError(
                f'invalid recovery-action transition {self.status!r} → {new_status!r}'
            )
        self.status = new_status

    def mark_sent(self, daemon):
        """PENDING → SENT once the frame is handed to the daemon-link group."""
        self._transition(self.Status.SENT)
        self.daemon = daemon
        self.sent_at = timezone.now()
        self.save(update_fields=['status', 'daemon', 'sent_at'])

    def apply_daemon_result(self, result_state, *, daemon_result=None, before=None,
                            after=None, reason=''):
        """Record the daemon's recovery_result (SENT → terminal state).

        A result arriving after expires_at resolves the row as EXPIRED but
        still keeps the daemon's payload for audit.
        """
        if result_state not in self.DAEMON_RESULT_STATES:
            raise ValidationError(f'unknown daemon result state {result_state!r}')
        target = self.Status.EXPIRED if self.is_expired else result_state
        self._transition(target)
        self.daemon_result = daemon_result or {}
        self.before_summary = before or {}
        self.after_summary = after or {}
        self.result_reason = reason or (
            f'result {result_state!r} received after expiry' if target == self.Status.EXPIRED else ''
        )
        self.resolved_at = timezone.now()
        self.save(update_fields=[
            'status', 'daemon_result', 'before_summary', 'after_summary',
            'result_reason', 'resolved_at',
        ])


class FleetAgentArchive(models.Model):
    """Archive marker for an agent (operator 2026-07-10).

    Archiving is the durable alternative to deletion: the agent is stopped and
    EXCLUDED from every automatic starter (on-demand reconciler, daemon boot
    via enabled=false, recovery sweeps) until explicitly unarchived. Keyed by
    username — the one stable handle shared by k3s-native FleetAgent rows and
    rt-fleet daemon agents — so either kind can be archived even when no
    FleetAgent row exists. Unarchive deletes the row; prior lifecycle state is
    untouched (the agent stays stopped until someone starts it).
    """

    agent_username = models.CharField(max_length=150, unique=True, db_index=True)
    archived_at = models.DateTimeField(auto_now_add=True)
    archived_by = models.CharField(max_length=150, blank=True)
    reason = models.CharField(max_length=300, blank=True)

    class Meta:
        verbose_name = 'fleet agent archive marker'

    def __str__(self):
        return f'archived:{self.agent_username}'


def archived_usernames() -> set:
    """Lowercased usernames of all archived agents (for sweep exclusions).

    Raises on DB failure — auto-START paths must treat that as "abort the
    sweep", never as "nothing is archived"; display paths may catch and
    default to an empty set.
    """
    return {u.lower() for u in FleetAgentArchive.objects.values_list('agent_username', flat=True)}


class HiveOrgProviderReference(models.Model):
    """HIVE-1862 §2 — thin READ-ONLY mirror of a Cortex org-scoped provider.

    Hive org Settings references Cortex-managed providers/keys. This row is a
    cache/mirror of the LIVE Cortex contract (source of truth = Cortex): it
    stores only opaque identifiers and display state — NEVER raw key material,
    base_url+auth tuples, or bearer tokens. All mutation (validate, activate,
    rotate) is delegated to Cortex via the named endpoints in HIVE-1862 §4; the
    mirror is refreshed by a reconcile loop and never authorizes routing on its
    own (a stale mirror never routes).

    Resolution chain (agent → org → Cortex key) mirrors the coordinator
    TokenPool flow: at request time the runtime resolves which org-scoped
    Cortex credential applies, then calls Cortex with a Hive-issued consumer
    token; Cortex's router enforces the BYO tenant-lock server-side.
    """

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        VERIFYING = 'verifying', 'Verifying'
        PROBATION = 'probation', 'Probation'
        DISABLED = 'disabled', 'Disabled'
        MISMATCH = 'mismatch', 'Mismatch'
        # HIVE-2161 — HLD HIVE-336 failure states surfaced by the lifecycle
        # (org-scoped failure contract: the resolver returns ProviderUnavailable
        # with a typed reason; the mirror records the credential state).
        INVALID = 'invalid', 'Invalid'
        EXHAUSTED = 'exhausted', 'Exhausted'

    class ProviderType(models.TextChoices):
        OPENAI = 'openai', 'OpenAI'
        ANTHROPIC = 'anthropic', 'Anthropic'
        OPENAI_COMPATIBLE = 'openai_compatible', 'OpenAI-compatible'

    org_id = models.IntegerField(db_index=True)
    cortex_provider_id = models.UUIDField(db_index=True)
    cortex_key_ref = models.CharField(max_length=200, blank=True, default='')
    default_models = models.JSONField(default=list, blank=True)
    # HIVE-2161 S3-lite (kumo refinement #3, binding): optional per-runtime-class
    # default model ids, keyed by runtime class (e.g. "claude-bridge",
    # "codex-bridge", "gateway"). Additive/null-safe: slice-1 rows may be NULL
    # and are skipped without error; the flat `default_models` list stays live
    # in the precedence chain (per_agent_overrides → default_models_by_runtime
    # → default_models[0] → agent.model). No backfill, no destructive change.
    default_models_by_runtime = models.JSONField(
        default=dict, blank=True, null=True,
        help_text='Optional {runtime_class: model_id} defaults; NULL/absent is '
                  'skipped without error (flat default_models stays live).',
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.ACTIVE,
    )
    per_agent_overrides = models.JSONField(default=dict, blank=True)
    # HIVE-2161 — lifecycle + audit fields (HLD HIVE-336 OrgProviderCredential).
    # provider_type/display_name are org-facing metadata; the Cortex listing is
    # created by the provision flow and the mirror row persists the opaque refs.
    provider_type = models.CharField(
        max_length=20, choices=ProviderType.choices,
        default=ProviderType.OPENAI_COMPATIBLE,
    )
    display_name = models.CharField(max_length=200, blank=True, default='')
    last_validation_result = models.CharField(
        max_length=40, blank=True, default='',
        help_text='Redacted outcome code: ok | auth_failed | rate_limited | '
                  'endpoint_unreachable | schema_mismatch. Never a raw body.',
    )
    created_by_principal = models.CharField(max_length=200, blank=True, default='')
    updated_by_principal = models.CharField(max_length=200, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'hive org provider reference'
        ordering = ['org_id', 'cortex_provider_id']
        constraints = [
            models.UniqueConstraint(
                fields=['org_id', 'cortex_provider_id'],
                name='uniq_hive_org_provider_ref',
            ),
        ]

    def __str__(self):
        return f'org:{self.org_id} provider:{self.cortex_provider_id} ({self.status})'


# PLAT-7523 (R2 Slice 2): the runtime-metering outbox model lives in the
# fleet.metering sub-package; imported here so the app registry sees it.
from .metering.models import FleetRuntimeOutbox  # noqa: E402,F401


class HiveOrgCredentialLease(models.Model):
    """HIVE-2622 S4 — durable org-scoped credential-lease record (BYO grant slice).

    Extends the encrypted TokenPool / credential-grant machinery to the
    org-scoped BYO chain: when a customer org's runtime consumes the org's own
    Cortex-managed credential, this row is the append-only accounting event of
    that leasing transition (the TokenPool runbook invariant — audit events on
    leasing transitions — composed with the mirror's opaque-ref contract).

    Provenance without secrets: the row pins the same-org reference, the
    verified agent identity, the runtime class, and a FINGERPRINT of the
    Hive-issued consumer token — never the token itself and never any org key
    material (the org's real credential is server-side in Cortex; Hive stores
    opaque refs only, per ``HiveOrgProviderReference``).
    """

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        EXPIRED = 'expired', 'Expired'
        REVOKED = 'revoked', 'Revoked'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    grant_id = models.UUIDField(unique=True, editable=False)
    reference = models.ForeignKey(
        HiveOrgProviderReference, on_delete=models.PROTECT,
        related_name='credential_leases',
    )
    organization_id = models.IntegerField(db_index=True)
    provider = models.CharField(max_length=32)
    runtime_class = models.CharField(max_length=80, blank=True, default='')
    agent_identity = models.CharField(max_length=200, blank=True, default='', db_index=True)
    consumer_token_fingerprint = models.CharField(max_length=71, blank=True, default='')
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    issued_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        verbose_name = 'hive org credential lease'
        ordering = ['-issued_at']
        indexes = [
            models.Index(fields=['reference', 'agent_identity']),
        ]

    def is_valid(self, now=None):
        from django.utils import timezone

        now = now or timezone.now()
        return (
            self.status == self.Status.ACTIVE
            and self.expires_at > now
        )

    def __str__(self):
        return f'HiveOrgCredentialLease(org:{self.organization_id}, {self.provider}, exp={self.expires_at:%Y-%m-%dT%H:%M})'


class LeaseCriticalTeamPolicy(models.Model):
    """Operator-editable critical-team lease floor (HIVE-2841).

    Hive's lease scheduler guarantees at least ``min_leases`` lease-holder
    seats to each enabled critical team, fairly across critical teams when
    lease supply is short, without starving the rest of the pool beyond that
    floor. This is the POLICY SURFACE, not a code constant: adding, disabling,
    or editing a row changes who the scheduler reserves on the next lease tick
    (cached for 60s) with no deploy.

    ``team_slug`` is the unqualified Pulse team slug as the lease planner sees
    it after ``fleet.on_demand.canon_team`` normalization (e.g. ``merge``).
    Teams with no worthy agent (no assigned work / DM / alert / team queue)
    are not granted idle seats — the floor arbitrates DEMAND, it does not
    manufacture wake demand.
    """

    team_slug = models.CharField(max_length=128, unique=True)
    min_leases = models.PositiveSmallIntegerField(default=1)
    enabled = models.BooleanField(default=True)
    note = models.CharField(max_length=280, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'lease critical team policy'
        verbose_name_plural = 'lease critical team policies'
        ordering = ['team_slug']

    def __str__(self):
        state = 'enabled' if self.enabled else 'disabled'
        return f'LeaseCriticalTeamPolicy({self.team_slug}, min={self.min_leases}, {state})'
