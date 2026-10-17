"""
Hive Claw models.

A *claw* is a per-tenant OpenClaw assistant instance running on the cluster.
Ownership is 1:N — a single owner (Shizuha-ID user) may own many claws
(Charter D2). Each claw lives in its own namespace `claw-<owner_id>-<claw_id>`
and is keyed by its own UUID, NOT by the owner id.
"""
import hashlib
import json
import re
import uuid

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone


ORG_TEMPLATE_SLUG_RE = r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$'
org_template_slug_validator = RegexValidator(
    regex=ORG_TEMPLATE_SLUG_RE,
    message='slug must be kebab-case lowercase alphanumeric/hyphen.',
)

_INTERNAL_TEMPLATE_PATTERN_RE = re.compile(
    r'(@shizuha\.com|hothritik|hritik|credential[_-]?broker|kubeconfig|host[_-]?exec|repo[_-]?write|github[:_-]?(merge|review))',
    re.IGNORECASE,
)


def _json_list_default():
    return []


def _json_dict_default():
    return {}


def _stable_hash(payload) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest()


class OrgTemplate(models.Model):
    """Versioned artifact for guided tenant-org provisioning (HIVE-368).

    The template is deliberately declarative data: it describes teams,
    capability refs, starter work, spaces/workspaces, and manager/andon shape.
    The HIVE-320 provisioning saga resolves this record into concrete service
    calls; the template itself never stores credentials, agent identities, or
    internal Shizuha doctrine.
    """

    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        ACTIVE = 'active', 'Active'
        ARCHIVED = 'archived', 'Archived'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slug = models.SlugField(max_length=80, db_index=True, validators=[org_template_slug_validator])
    version = models.PositiveIntegerField(default=1)
    name = models.CharField(max_length=160)
    description = models.TextField(blank=True, default='')
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT, db_index=True)
    category = models.CharField(max_length=80, blank=True, default='')
    icon = models.CharField(max_length=80, blank=True, default='')
    recommended_tier = models.CharField(max_length=80, blank=True, default='')
    teams = models.JSONField(default=_json_list_default, blank=True)
    custom_capabilities = models.JSONField(default=_json_list_default, blank=True)
    workflows = models.JSONField(default=_json_list_default, blank=True)
    projects = models.JSONField(default=_json_list_default, blank=True)
    wiki_seed_pages = models.JSONField(default=_json_list_default, blank=True)
    connect_workspaces = models.JSONField(default=_json_list_default, blank=True)
    drive_roots = models.JSONField(default=_json_list_default, blank=True)
    starter_tasks = models.JSONField(default=_json_list_default, blank=True)
    managers = models.JSONField(default=_json_dict_default, blank=True)
    metadata = models.JSONField(default=_json_dict_default, blank=True)
    definition_hash = models.CharField(max_length=64, blank=True, db_index=True)
    scrubbed_by = models.CharField(max_length=255, blank=True, default='')
    scrubbed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.CharField(max_length=255, blank=True, default='')
    updated_by = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['slug', '-version']
        constraints = [
            models.UniqueConstraint(fields=['slug', 'version'], name='uniq_org_template_slug_version'),
            models.UniqueConstraint(
                fields=['slug'],
                condition=Q(status='active'),
                name='uniq_active_org_template_slug',
            ),
        ]
        indexes = [
            models.Index(fields=['status', 'slug']),
            models.Index(fields=['category', 'status']),
        ]

    def __str__(self):
        return f'{self.slug}@v{self.version} ({self.status})'


    def definition_payload(self):
        return {
            'slug': self.slug,
            'version': self.version,
            'name': self.name,
            'description': self.description,
            'category': self.category,
            'icon': self.icon,
            'recommended_tier': self.recommended_tier,
            'teams': self.teams or [],
            'custom_capabilities': self.custom_capabilities or [],
            'workflows': self.workflows or [],
            'projects': self.projects or [],
            'wiki_seed_pages': self.wiki_seed_pages or [],
            'connect_workspaces': self.connect_workspaces or [],
            'drive_roots': self.drive_roots or [],
            'starter_tasks': self.starter_tasks or [],
            'managers': self.managers or {},
            'metadata': self.metadata or {},
        }

    def export(self):
        payload = self.definition_payload()
        payload.update({
            'id': str(self.id),
            'status': self.status,
            'definition_hash': self.definition_hash,
        })
        return payload

    def clean(self):
        self.slug = (self.slug or '').strip().lower().replace('_', '-').replace(' ', '-')
        org_template_slug_validator(self.slug)
        if self.version < 1:
            raise ValidationError({'version': 'version must be >= 1'})
        for field in ('teams', 'custom_capabilities', 'workflows', 'projects', 'wiki_seed_pages', 'connect_workspaces', 'drive_roots', 'starter_tasks'):
            value = getattr(self, field) or []
            if not isinstance(value, list):
                raise ValidationError({field: 'must be a JSON list'})
        if not isinstance(self.managers or {}, dict):
            raise ValidationError({'managers': 'must be a JSON object'})
        if not isinstance(self.metadata or {}, dict):
            raise ValidationError({'metadata': 'must be a JSON object'})

        for cap in self.custom_capabilities or []:
            slug = (cap.get('slug') if isinstance(cap, dict) else '') or ''
            slug = slug.strip().lower().replace('_', '-').replace(' ', '-')
            org_template_slug_validator(slug)

        for idx, team in enumerate(self.teams or []):
            if not isinstance(team, dict):
                raise ValidationError({'teams': f'entry {idx} must be an object'})
            team_slug = (team.get('slug') or '').strip().lower().replace('_', '-').replace(' ', '-')
            org_template_slug_validator(team_slug)
            capabilities = team.get('capabilities') or team.get('capability') or team_slug
            if isinstance(capabilities, str):
                capabilities = [capabilities]
            if not isinstance(capabilities, list) or not capabilities:
                raise ValidationError({'teams': f'{team_slug} must declare one or more capabilities'})
            for cap_slug in capabilities:
                cap_slug = str(cap_slug or '').strip().lower().replace('_', '-').replace(' ', '-')
                org_template_slug_validator(cap_slug)

        # A customer-facing active template must have passed the scrub gate and
        # must not embed obvious internal identities or privileged access handles.
        # Capability slugs are checked in org_templates.resolve_org_template_plan,
        # where sensitive built-in capabilities can be scrubbed at provisioning.
        if self.status == self.Status.ACTIVE:
            text = json.dumps(self.definition_payload(), sort_keys=True)
            match = _INTERNAL_TEMPLATE_PATTERN_RE.search(text)
            if match:
                raise ValidationError({'status': f'active templates must be scrubbed of internal-only marker {match.group(0)!r}'})
            if not (self.scrubbed_by and self.scrubbed_at):
                raise ValidationError({'scrubbed_by': 'active customer-facing templates require scrub gate metadata'})

    def save(self, *args, **kwargs):
        self.definition_hash = _stable_hash(self.definition_payload())
        self.full_clean()
        super().save(*args, **kwargs)


class MarketplaceTemplate(models.Model):
    """Catalog metadata wrapping, never duplicating, an OrgTemplate artifact."""

    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        REVIEWED = 'reviewed', 'Reviewed'
        PUBLISHED = 'published', 'Published'
        DEPRECATED = 'deprecated', 'Deprecated'

    class Visibility(models.TextChoices):
        INTERNAL = 'internal', 'Internal'
        DESIGN_PARTNER = 'design_partner', 'Design partner'
        PUBLIC = 'public_catalog', 'Public catalog'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slug = models.SlugField(max_length=80, validators=[org_template_slug_validator])
    version = models.CharField(max_length=32)
    base_template = models.OneToOneField(OrgTemplate, on_delete=models.PROTECT, related_name='marketplace_entry')
    artifact_type = models.CharField(max_length=32, default='function_pod', editable=False)
    category = models.CharField(max_length=80, blank=True, default='')
    publisher = models.CharField(max_length=255)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT, db_index=True)
    visibility = models.CharField(max_length=24, choices=Visibility.choices, default=Visibility.INTERNAL, db_index=True)
    required_inputs = models.JSONField(default=_json_list_default, blank=True)
    expected_first_artifact = models.JSONField(default=_json_dict_default, blank=True)
    risk_flags = models.JSONField(default=_json_list_default, blank=True)
    compatibility = models.JSONField(default=_json_dict_default, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    deprecated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['slug', '-version']
        constraints = [models.UniqueConstraint(fields=['slug', 'version'], name='uniq_marketplace_slug_version')]
        indexes = [models.Index(fields=['status', 'visibility', 'slug'])]

    def clean(self):
        self.slug = (self.slug or '').strip().lower()
        org_template_slug_validator(self.slug)
        if not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?', self.version or ''):
            raise ValidationError({'version': 'must be a semantic version'})
        if self.artifact_type != 'function_pod':
            raise ValidationError({'artifact_type': 'v1 supports function_pod only'})
        if not (self.publisher or '').lower().endswith('@shizuha.com'):
            raise ValidationError({'publisher': 'v1 publishers must be internal Shizuha actors'})

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).first()
            if previous and previous.status == self.Status.PUBLISHED:
                immutable = (
                    'slug', 'version', 'base_template_id', 'artifact_type', 'category',
                    'publisher', 'visibility', 'required_inputs', 'expected_first_artifact',
                    'risk_flags', 'compatibility',
                )
                changed = [field for field in immutable if getattr(previous, field) != getattr(self, field)]
                if changed:
                    raise ValidationError({'published': f'published versions are immutable: {", ".join(changed)}'})
        self.full_clean()
        return super().save(*args, **kwargs)


class MarketplaceTemplateAuditEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    template = models.ForeignKey(MarketplaceTemplate, on_delete=models.PROTECT, related_name='audit_events')
    actor = models.CharField(max_length=255)
    event_type = models.CharField(max_length=32)
    from_status = models.CharField(max_length=16)
    to_status = models.CharField(max_length=16)
    created_at = models.DateTimeField(auto_now_add=True)

class Claw(models.Model):
    """
    A tenant's OpenClaw assistant instance.

    One owner may own many claws (non-unique ``owner_id``). The claw's own
    ``id`` (UUID) is the identifier used across the HLD §5 API surface
    (``/v1/claws/<id>/...``).
    """

    class Status(models.TextChoices):
        PROVISIONING = 'provisioning', 'Provisioning'   # k8s resources being created
        RUNNING = 'running', 'Running'                   # Deployment scaled to 1
        STOPPED = 'stopped', 'Stopped'                   # Deployment scaled to 0 (PVC retained)
        SUSPENDED_OWNER_REMOVED = 'suspended_owner_removed', 'Suspended (owner removed)'
        SUSPENDED_NO_CREDITS = 'suspended_no_credits', 'Suspended (no credits)'
        DELETING = 'deleting', 'Deleting'                # In the 60s undo window / teardown
        FAILED = 'failed', 'Failed'                      # Provisioning or runtime error
        DELETED = 'deleted', 'Deleted'                   # Fully torn down

    class AgentType(models.TextChoices):
        # Near-native OpenClaw (default, current behaviour) — image from
        # settings.HIVE_CLAW_IMAGE; Cortex support via an `openclaw.json` provider.
        CLAW = 'claw', 'OpenClaw'
        # shizuha-scaffold runtime — image from settings.HIVE_SCLI_IMAGE; reads
        # Cortex from CORTEX_BASE_URL / CORTEX_API_KEY / MODEL env.
        SCLI = 'scli', 'SCLI'
        # Hermes agent (NousResearch hermes-agent, vendored at vendor/hermes-agent).
        # A dedicated runtime uses settings.HIVE_HERMES_IMAGE. Until that image
        # exists, running the OpenClaw-compatible gateway is allowed only when
        # HIVE_HERMES_OPENCLAW_COMPAT is explicitly enabled and is recorded on
        # runtime_compat_mode so API/UI surfaces label it honestly.
        HERMES = 'hermes', 'Hermes'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Which runtime backs this claw (HIVE per-owner Cortex). 'claw' = OpenClaw
    # image; 'scli' = shizuha-runtime image. Default keeps every existing claw on
    # the unchanged OpenClaw path.
    agent_type = models.CharField(
        max_length=16,
        choices=AgentType.choices,
        default=AgentType.CLAW,
        db_index=True,
    )

    # Per-owner Cortex model id (e.g. "Qwen3.6-27B"). Blank falls back to the
    # tier's default model (or the built-in HIVE_CORTEX_DEFAULT_MODEL).
    model = models.CharField(max_length=128, blank=True, default='')

    # Owner identity (from Shizuha ID). NON-unique: 1 owner : N claws (Charter D2).
    owner_id = models.IntegerField(db_index=True)
    owner_username = models.CharField(max_length=150)
    owner_email = models.EmailField()

    # Tenant org of the owner (HIVE-2233 fix): Cortex requires org_id on
    # STANDARD (customer) key mints. The reconcile path has no owner JWT, so
    # the org is persisted here. Today it is populated via the repair command's
    # --org-id flag (claws/management/commands/reconcile_claw_cortex_key.py);
    # creation-time population from the owner's membership is future work.
    # Nullable for legacy claws until backfilled.
    org_id = models.IntegerField(null=True, blank=True, db_index=True)

    # Display name (owner-renameable, HLD M4). Not used for any k8s resource name.
    name = models.CharField(max_length=100, default='My Assistant')

    # Tier id from the `hive-tiers` ConfigMap (HLD §5.1).
    tier_id = models.CharField(max_length=50, blank=True)

    platform_url = models.URLField(
        default='https://s1.tail.shizuha.com',
        help_text='Platform URL the claw connects to',
    )

    # k8s state
    namespace = models.CharField(max_length=100, blank=True)
    pod_name = models.CharField(max_length=200, blank=True)
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.PROVISIONING,
        db_index=True,
    )
    status_message = models.TextField(blank=True)

    # Runtime identity audit. For Hermes, runtime_compat_mode means the row is
    # intentionally backed by the OpenClaw-compatible gateway image rather than
    # a dedicated Hermes runtime; API/UI must label this explicitly.
    runtime_image = models.CharField(max_length=255, blank=True, default='')
    runtime_compat_mode = models.BooleanField(default=False)

    # Resource tracking
    pvc_size = models.CharField(max_length=20, default='10Gi')

    # Two-phase delete bookkeeping (HLD §4.2). Set when the claw enters the
    # 60s undo window; the reclaim path reads it.
    delete_requested_at = models.DateTimeField(null=True, blank=True)

    # HIVE-4/HIVE-7 lifecycle/backstop state. These map to controller
    # annotations with explicit precedence: legal_hold > reclaim_now > recycle_after.
    # HIVE-598: recycle_after belongs EXCLUSIVELY to the delete flow (24h
    # delayed-reclaim retention). The provision-time idle-TTL intent lives in
    # idle_recycle_after — mixing the two once selected running assistants for
    # hard-delete (2026-07-09 near-miss).
    recycle_after = models.DateTimeField(null=True, blank=True, db_index=True)
    # Free-tier idle-recycle deadline (tier.idle_ttl_minutes past last activity).
    # Informational until the idle-recycle worker exists; NEVER consumed by the
    # delete/reclaim path.
    idle_recycle_after = models.DateTimeField(null=True, blank=True, db_index=True)
    reclaim_now = models.BooleanField(default=False, db_index=True)
    reclaim_previous_status = models.CharField(max_length=32, blank=True, default="")
    legal_hold = models.BooleanField(default=False, db_index=True)
    legal_hold_reason = models.TextField(blank=True)
    legal_hold_case_id = models.CharField(max_length=128, blank=True)
    legal_hold_set_by = models.CharField(max_length=150, blank=True)
    legal_hold_set_at = models.DateTimeField(null=True, blank=True)
    recycle_after_frozen_at = models.DateTimeField(null=True, blank=True)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_active_at = models.DateTimeField(null=True, blank=True)
    provisioned_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            # An owner may run several agents on the same tier as long as they
            # differ by (agent_type, model) — e.g. an OpenClaw + an SCLI on
            # Qwen3.6-27B, or the same type on two different models. The active
            # uniqueness therefore keys on (owner, tier, agent_type, model) so it
            # still blocks a TRUE duplicate (same owner+tier+type+model) while
            # allowing distinct (type, model) combinations per owner.
            models.UniqueConstraint(
                fields=['owner_id', 'tier_id', 'agent_type', 'model'],
                condition=Q(status__in=[
                    'provisioning',
                    'running',
                    'stopped',
                    'suspended_owner_removed',
                    'suspended_no_credits',
                    'deleting',
                ]),
                name='uniq_active_claw_owner_tier_type_model',
            ),
        ]

    def __str__(self):
        return f'Claw({self.name}, owner={self.owner_id}, {self.status})'

    def save(self, *args, **kwargs):
        if not self.namespace:
            # claw-<owner_id>-<claw_id>; uuid hex keeps it DNS-1123 safe and < 63 chars.
            self.namespace = f'claw-{self.owner_id}-{self.id.hex}'
        super().save(*args, **kwargs)


class ClawEvent(models.Model):
    """Audit log of claw lifecycle events."""
    claw = models.ForeignKey(Claw, on_delete=models.CASCADE, related_name='events')
    event_type = models.CharField(max_length=50)  # provisioned, started, stopped, error, destroyed
    message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.event_type} @ {self.created_at}'


class ClawExportJob(models.Model):
    """Owner/admin export job audit row (HIVE-4 §4.1 / §6.3)."""

    class Status(models.TextChoices):
        QUEUED = 'queued', 'Queued'
        SNAPSHOTTING = 'snapshotting', 'Snapshotting'
        RUNNING = 'running', 'Running'
        UPLOADING = 'uploading', 'Uploading'
        DONE = 'done', 'Done'
        FAILED = 'failed', 'Failed'
        CANCELLED = 'cancelled', 'Cancelled'

    class RequestedBy(models.TextChoices):
        OWNER = 'owner', 'Owner'
        OPERATOR = 'operator', 'Operator'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    claw = models.ForeignKey(Claw, on_delete=models.CASCADE, related_name='export_jobs')
    owner_id = models.IntegerField(db_index=True)
    requested_by = models.CharField(max_length=16, choices=RequestedBy.choices)
    requested_by_actor = models.CharField(max_length=150, blank=True)
    include_session = models.BooleanField(default=False)
    include_credits = models.BooleanField(default=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED, db_index=True)
    snapshot_name = models.CharField(max_length=128, blank=True)
    share_url = models.URLField(max_length=2048, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    byte_count = models.BigIntegerField(null=True, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['claw', 'status']),
            models.Index(fields=['owner_id', 'created_at']),
        ]

    @property
    def terminal(self):
        return self.status in {self.Status.DONE, self.Status.FAILED, self.Status.CANCELLED}

    def __str__(self):
        return f'ClawExportJob({self.claw_id}, {self.status})'


class ClawHardDeleteFailure(models.Model):
    """Fail-closed hard-delete audit row (HIVE-4 §4.2.b.1 / §6.3)."""

    claw = models.ForeignKey(Claw, on_delete=models.CASCADE, related_name='hard_delete_failures')
    owner_id = models.IntegerField(db_index=True)
    requested_by = models.CharField(max_length=150)
    failure_class = models.CharField(max_length=64)
    akira_pulse_alert_id = models.CharField(max_length=128, blank=True)
    requested_at = models.DateTimeField()
    failed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-failed_at']


class ClawCancelDelete(models.Model):
    """Audit row for restorative cancel-delete (HIVE-4 §6.3)."""

    claw = models.ForeignKey(Claw, on_delete=models.CASCADE, related_name='cancel_delete_events')
    owner_id = models.IntegerField(db_index=True)
    session_id = models.CharField(max_length=128, blank=True)
    cancelled_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-cancelled_at']


class WhatsAppLink(models.Model):
    """HIVE-201 E1: binds a WhatsApp number (``phone_jid``) to a :class:`Claw`.

    The dashboard mints a short-lived single-use ``code`` against the owner's
    claw; the owner taps a ``wa.me`` deep-link to send ``LINK-<code>`` from their
    WhatsApp number; the inbound handler (E1 slice 2, via Ori/whatsmeow) binds
    the whatsmeow-authenticated ``phone_jid`` to the claw.

    Ownership proof is the whatsmeow session itself — the inbound ``phone_jid``
    is cryptographically bound to that WA account by Ori's authenticated session,
    so there is NO Meta webhook / ``X-Hub-Signature-256`` HMAC (HLD §6 is
    reconciled to the confirmed whatsmeow reality on HIVE-201; E0/HIVE-200).
    Single-use is enforced by the ``status`` transition pending -> linked.
    """

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'    # code minted, awaiting the WA message
        LINKED = 'linked', 'Linked'       # phone_jid bound to the claw and actively routed
        PAUSED = 'paused', 'Paused'       # owner paused routing without deleting the link
        RATE_LIMITED = 'rate_limited', 'Rate limited'  # routing blocked by quota/rate limit
        EXPIRED = 'expired', 'Expired'    # TTL elapsed before a WA message arrived

    # The code travels through a WhatsApp text message, so it is a plain random
    # token (not an HMAC blob) and is the primary key for O(1) inbound lookup.
    code = models.CharField(max_length=64, primary_key=True)
    claw = models.ForeignKey(Claw, on_delete=models.CASCADE, related_name='wa_links')
    # Denormalized owner scope (copied from the claw at mint time) so a bind
    # never has to re-derive ownership and cannot drift to another owner.
    owner_id = models.IntegerField(db_index=True)
    wa_phone_jid = models.CharField(max_length=64, blank=True, default='', db_index=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True,
    )
    expires_at = models.DateTimeField(db_index=True)
    linked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def is_consumable(self, now=None):
        """True iff this code can still bind a number (pending + not expired)."""
        from django.utils import timezone

        now = now or timezone.now()
        return self.status == self.Status.PENDING and self.expires_at > now

    def __str__(self):
        return f'WhatsAppLink({self.code[:6]}…, claw={self.claw_id}, {self.status})'


class TenantProvisioningRun(models.Model):
    """Idempotent saga for provisioning a tenant org from a Hive template.

    HIVE-320 deliberately stores the orchestration state in Hive before it
    calls out to Admin/ID, Pulse, Wiki, Connect, Drive, Cortex, or the fleet
    daemon.  Every external resource is recorded as a ref on this row so a
    retry, operator resume, teardown, or audit can start from the database
    instead of re-discovering partial side effects.
    """

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        RUNNING = 'running', 'Running'
        READY = 'ready', 'Ready'
        FAILED = 'failed', 'Failed'
        MANUAL_REVIEW = 'manual_review', 'Manual review'
        TEARING_DOWN = 'tearing_down', 'Tearing down'
        TORN_DOWN = 'torn_down', 'Torn down'

    class StepStatus(models.TextChoices):
        PENDING = 'pending', 'Pending'
        RUNNING = 'running', 'Running'
        DONE = 'done', 'Done'
        FAILED = 'failed', 'Failed'
        ROLLED_BACK = 'rolled_back', 'Rolled back'
        MANUAL_REVIEW = 'manual_review', 'Manual review'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    idempotency_key = models.CharField(
        max_length=128,
        unique=True,
        help_text='Stable caller/run key; repeated calls return/resume the same saga.',
    )
    requester_user_id = models.IntegerField(db_index=True)
    requester_email = models.EmailField(blank=True, default='')
    org_name = models.CharField(max_length=160)
    org_slug = models.SlugField(max_length=80, db_index=True)
    template_slug = models.SlugField(max_length=80, db_index=True)
    plan_slug = models.SlugField(max_length=80, blank=True, default='')
    dry_run = models.BooleanField(default=False, db_index=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    current_step = models.CharField(max_length=80, blank=True, default='')
    step_state = models.JSONField(default=dict, blank=True)
    resource_refs = models.JSONField(default=dict, blank=True)
    audit_events = models.JSONField(default=list, blank=True)
    # HIVE-356 (VEN-61): design-partner pilot-prep glue. Carries the captured
    # HIVE-324/Connect lead intake (RFC §2.1 fields), the RFC §3 launch-gate
    # state, the pilot mode, and template-scoped tool grants so the audited
    # intake→preview handoff and the fail-closed external-apply gate live on the
    # same provisioning row. Empty for non-pilot provisioning runs.
    pilot_prep = models.JSONField(default=dict, blank=True)
    status_message = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['org_slug', 'template_slug']),
            models.Index(fields=['requester_user_id', 'created_at']),
            models.Index(fields=['status', 'updated_at']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['org_slug'],
                condition=Q(dry_run=False, status__in=['pending', 'running', 'ready', 'manual_review']),
                name='uniq_active_tenant_provisioning_org_slug',
            ),
        ]

    def __str__(self):
        return f'TenantProvisioningRun({self.org_slug}, {self.template_slug}, {self.status})'

    @property
    def terminal(self):
        return self.status in {self.Status.READY, self.Status.FAILED, self.Status.TORN_DOWN}


class ClawCortexKeyBinding(models.Model):
    """HIVE-2233 — typed Claw↔Cortex key binding/cutover state.

    One row per Claw recording the current Cortex API-key generation bound to
    the claw's ``claw-config`` Secret. The row is the Hive-side cutover ledger
    for the HIVE-1400 cross-service saga: it references the canonical
    HIVE-758 ``credentials.CredentialGeneration`` lineage and envelope-encrypts
    the raw current/prior artifacts with the existing token-coordinator
    primitive (per-entry Fernet DEK wrapped by the KEK — never plaintext, never
    logged/returned).

    State machine (HIVE-1397 table):
      prepared        — a new compliant generation is persisted, not yet bound
      bound           — Secret CAS-patched + Deployment pod-template stamped
      verifying       — rollout observed, tiny attributed Cortex proof pending
      active          — new key proven live; old key revoked (or none existed)
      retire_pending  — new key active but old-key revoke failed (durable retry)
      revoked         — terminal: binding retired (claw teardown / key retired)

    Never select by name/prefix/latest alone; never manually patch a live
    Secret; never revoke before new-runtime proof (HIVE-2233 §4).
    """

    class State(models.TextChoices):
        PREPARED = 'prepared', 'Prepared'
        BOUND = 'bound', 'Bound'
        VERIFYING = 'verifying', 'Verifying'
        ACTIVE = 'active', 'Active'
        RETIRE_PENDING = 'retire_pending', 'Retire pending'
        REVOKED = 'revoked', 'Revoked'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    claw = models.OneToOneField(
        Claw, on_delete=models.CASCADE, related_name='cortex_key_binding'
    )
    # Canonical HIVE-758 credential generation lineage this binding belongs to.
    generation = models.ForeignKey(
        'credentials.CredentialGeneration',
        on_delete=models.PROTECT,
        related_name='claw_cortex_bindings',
        null=True,
        blank=True,
    )
    state = models.CharField(
        max_length=20, choices=State.choices, default=State.PREPARED, db_index=True
    )
    cortex_key_uuid = models.UUIDField(null=True, blank=True)
    key_fingerprint = models.CharField(max_length=64, blank=True, default='')
    desired_tier = models.CharField(max_length=32, blank=True, default='')
    operation_id = models.UUIDField(null=True, blank=True)
    # Envelope-encrypted raw artifacts (token-coordinator primitive): dicts of
    # {ciphertext, wrapped_dek, dek_key_id, fingerprint}. Never plaintext.
    current_artifact = models.JSONField(default=dict, blank=True)
    prior_artifact = models.JSONField(default=dict, blank=True)
    # Cortex key UUID of the prior (to-be-revoked) artifact, for exact revoke.
    prior_key_id = models.UUIDField(null=True, blank=True)
    # CAS fence on the live Secret + Deployment pod-template generation stamp.
    secret_resource_version = models.CharField(max_length=64, blank=True, default='')
    deployment_generation = models.BigIntegerField(null=True, blank=True)
    last_reconcile_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['state', 'updated_at']),
            models.Index(fields=['cortex_key_uuid']),
        ]


# ---------------------------------------------------------------------------
# R1A Metering Core (PLAT-4481 / PLAT-4483) — models live in metering_models.py
# so the legacy UsageEvent surface stays readable. Re-export for Django app
# loading and for `from claws.models import BillingAccount`.
# ---------------------------------------------------------------------------
from .metering_models import (  # noqa: E402,F401
    AdmissionProofReceipt,
    AdmissionWriterAuthority,
    BillingAccount,
    BillingStatement,
    CurrentResourcePayer,
    EntitlementBalance,
    EntitlementGrant,
    MeterDefinition,
    PayerAuthorityProof,
    PayerGenesisFact,
    PayerGenesisManifest,
    PayerLinkConsent,
    PayerLinkGrantVersion,
    PayerLinkProposal,
    PlanVersion,
    PostpaidFacility,
    ProducerHolder,
    QuotaReservation,
    RateVersion,
    RatedUsageLine,
    ResourcePayerBindingVersion,
    SpendAllocation,
    SpendLease,
    SubscriptionTierVersion,
    UsageRollup,
)

