"""Token-coordinator models extracted from `claws` (ADR-0080 / HIVE-2018).

Moved from claws/models.py with `db_table` preserved so existing rows survive
intact (no data migration). The token-coordinator service modules
(token_coordinator, coordinator, token_revocation, coordinator_views,
quota_projection_v1) live alongside in this app.
"""
import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

class TokenPool(models.Model):
    """Owner-scoped model-token pool for the HIVE Token Coordinator (HIVE-123).

    The owner key is part of the uniqueness contract so queries can be scoped at
    the storage layer: a lookup for owner A cannot accidentally return owner B's
    pool. Internal fleet credentials use the synthetic org owner id below.
    """

    class OwnerKind(models.TextChoices):
        USER = 'user', 'User'
        ORG = 'org', 'Organization'

    class Provider(models.TextChoices):
        ANTHROPIC = 'anthropic', 'Anthropic'
        OPENAI = 'openai', 'OpenAI'
        XAI = 'xai', 'xAI Grok'
        GOOGLE = 'google', 'Google Antigravity'

    ORG_OWNER_ID = 'org'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner_kind = models.CharField(max_length=16, choices=OwnerKind.choices, db_index=True)
    owner_id = models.CharField(max_length=128, db_index=True)
    provider = models.CharField(max_length=32, choices=Provider.choices, db_index=True)
    display_name = models.CharField(max_length=160, blank=True)
    org_fallback_allowed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'claws_tokenpool'
        ordering = ['owner_kind', 'owner_id', 'provider']
        constraints = [
            models.UniqueConstraint(
                fields=['owner_kind', 'owner_id', 'provider'],
                name='uniq_token_pool_owner_provider',
            ),
        ]

    def __str__(self):
        return f'TokenPool({self.owner_kind}:{self.owner_id}, {self.provider})'


class TokenEntryQuerySet(models.QuerySet):
    def for_owner(self, owner_kind, owner_id, provider):
        return self.filter(
            pool__owner_kind=owner_kind,
            pool__owner_id=str(owner_id),
            pool__provider=provider,
            deleted_at__isnull=True,
        )

    def for_scopes(self, scopes, provider):
        """Entries across MULTIPLE owner scopes: [(owner_kind, owner_id), …].

        Personal-ownership model (operator 2026-07-11): credentials belong to
        the USER who added them; the synthetic org pool is only for genuinely
        shared credentials. Fleet-internal broker requests therefore draw from
        the platform owner's personal pool AND the org pool — this queryset is
        that union, still storage-scoped (no scope can leak another owner's
        entries that weren't explicitly listed).
        """
        q = Q(pk=None)  # empty scopes → matches nothing
        for owner_kind, owner_id in scopes:
            q |= Q(pool__owner_kind=owner_kind, pool__owner_id=str(owner_id))
        return self.filter(q, pool__provider=provider, deleted_at__isnull=True)

    def active_for_owner(self, owner_kind, owner_id, provider, now=None):
        return self.active_for_scopes([(owner_kind, owner_id)], provider, now=now)

    def active_for_scopes(self, scopes, provider, now=None):
        from django.utils import timezone

        now = now or timezone.now()
        return self.for_scopes(scopes, provider).filter(
            active=True,
            agent_leasing_enabled=True,
        ).filter(
            Q(cooldown_until__isnull=True) | Q(cooldown_until__lte=now)
        ).order_by('priority', 'last_rate_limit_at', 'created_at')


class TokenEntry(models.Model):
    """Envelope-encrypted token entry in an owner-scoped TokenPool."""

    class LastStatus(models.TextChoices):
        UNKNOWN = 'unknown', 'Unknown'
        OK = 'ok', 'OK'
        RATE_LIMITED = 'rate_limited', 'Rate limited'
        AUTH_FAILED = 'auth_failed', 'Auth failed (dead)'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    pool = models.ForeignKey(TokenPool, on_delete=models.CASCADE, related_name='entries')
    label = models.CharField(max_length=120)
    # Which provider account this token belongs to. Operator-editable; also
    # auto-filled from the provider identity endpoint when the token's scopes
    # allow it (Claude /api/oauth/profile, Codex id-token email claim).
    account_email = models.CharField(max_length=254, blank=True, default='')
    # Operator-owned policy: False reserves the credential for personal use.
    # This remains independent from `active`, which automation owns as the
    # provider-health state and may repair after transient errors.
    agent_leasing_enabled = models.BooleanField(default=True, db_index=True)
    active = models.BooleanField(default=True, db_index=True)
    cooldown_until = models.DateTimeField(null=True, blank=True, db_index=True)
    last_rate_limit_at = models.DateTimeField(null=True, blank=True)
    usage_count = models.PositiveBigIntegerField(default=0)
    tier = models.CharField(max_length=50, blank=True, default='')
    priority = models.IntegerField(default=0, db_index=True)

    token_ciphertext = models.TextField()
    wrapped_dek = models.TextField()
    dek_key_id = models.CharField(max_length=120)
    token_fingerprint = models.CharField(max_length=64, db_index=True)
    # Human-recognizable mask (first+last chars, never the middle) so an operator
    # can identify WHICH credential a row is without exposing the secret. Set at
    # set_token() time; never enough to reconstruct the token.
    token_hint = models.CharField(max_length=40, blank=True, default='')

    # HIVE-124 Coordinator service-core metrics: load + health observability used
    # by pick_token (least-loaded + LRU) and the health-check loop / HIVE UI.
    active_sessions = models.PositiveIntegerField(default=0, db_index=True)
    last_used_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_status = models.CharField(
        max_length=16, choices=LastStatus.choices, default=LastStatus.UNKNOWN,
    )
    last_checked_at = models.DateTimeField(null=True, blank=True)
    # CodexBar-style subscription quota windows (Claude OAuth usage, Codex wham/usage, …).
    # Redacted snapshot: used/left %, resets_at, plan — never stores raw secrets.
    quota_snapshot = models.JSONField(default=dict, blank=True)
    # HIVE-1747: the last report-status reason that stamped the current cooldown
    # (e.g. ``quota_exhausted`` vs ``rate_limited``). The pre-dry self-heal sweep
    # must NOT re-lease a provider-confirmed quota-exhausted credential for its
    # requested window even when the usage API is unreadable (confidence='unsupported').
    # Cleared on ``ok``. Empty string = no reason recorded (legacy rows).
    cooldown_reason = models.CharField(max_length=32, blank=True, default='')
    # PLAT-5850: per-account model-capability allowlist (e.g. ["gpt-5.6-sol"]).
    # Populated by a live account probe or by an operator. Empty/unknown means
    # "no capability constraint" — pick_token preserves the existing lease path
    # so an unprobed account is never falsely dried. When non-empty, pick_token
    # skips this entry for any model not listed, so an account whose plan
    # rejects the agent's pinned model (e.g. FREE/trading ChatGPT accounts that
    # 400 "model is not supported") is never leased to that agent.
    supported_models = models.JSONField(default=list, blank=True)
    # Auditable deletion tombstone. The encrypted secret fields are scrubbed at
    # delete time, while the row/fingerprint/leases remain for lineage and incident
    # review. Deleted entries are excluded from all serving/UI query paths.
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    deleted_by = models.CharField(max_length=254, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TokenEntryQuerySet.as_manager()

    class Meta:
        db_table = 'claws_tokenentry'
        ordering = ['pool_id', 'priority', 'created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['pool', 'label'],
                name='uniq_token_entry_pool_label',
                condition=Q(deleted_at__isnull=True),
            ),
            models.UniqueConstraint(
                fields=['pool', 'token_fingerprint'],
                name='uniq_token_entry_pool_fingerprint',
                condition=Q(deleted_at__isnull=True),
            ),
        ]

    def set_token(self, raw_token):
        from hive_platform.token_coordinator import encrypt_token, mask_token_hint

        encrypted = encrypt_token(raw_token)
        self.token_ciphertext = encrypted['ciphertext']
        self.wrapped_dek = encrypted['wrapped_dek']
        self.dek_key_id = encrypted['dek_key_id']
        self.token_fingerprint = encrypted['fingerprint']
        self.token_hint = mask_token_hint(raw_token)

    def get_token(self):
        from hive_platform.token_coordinator import decrypt_token

        if self.deleted_at:
            raise ValueError('deleted token entry has no secret material')
        return decrypt_token(self.token_ciphertext, self.wrapped_dek)

    def save(self, *args, **kwargs):
        update_fields = kwargs.get('update_fields')
        if update_fields is None or 'quota_snapshot' in update_fields:
            from hive_platform.quota_projection_v2 import project_quota_snapshot
            self.quota_snapshot = project_quota_snapshot(self.quota_snapshot)
        return super().save(*args, **kwargs)

    def __str__(self):
        return f'TokenEntry({self.pool_id}, {self.label}, active={self.active})'


class TokenEntryLeasingEvent(models.Model):
    """Durable attribution for operator-owned agent leasing policy changes."""

    class Source(models.TextChoices):
        OWNER_API = 'owner_api', 'Owner token-pools API'
        GROK_DEVICE_AUTH = 'grok_device_auth', 'Grok device auth'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    entry = models.ForeignKey(
        TokenEntry,
        on_delete=models.CASCADE,
        related_name='leasing_events',
    )
    # Snapshot the non-secret ownership/provider context so incident review does
    # not depend on a later label or pool metadata change.
    owner_kind = models.CharField(max_length=16)
    owner_id = models.CharField(max_length=128)
    provider = models.CharField(max_length=32)
    previous_enabled = models.BooleanField()
    enabled = models.BooleanField()
    actor = models.CharField(max_length=254)
    source = models.CharField(max_length=32, choices=Source.choices)
    revoked_lease_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'claws_tokenentryleasingevent'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['entry', 'created_at']),
        ]

    def __str__(self):
        return (
            f'TokenEntryLeasingEvent({self.entry_id}, '
            f'{self.previous_enabled}->{self.enabled}, {self.source})'
        )


class TokenStickyAssignment(models.Model):
    """Durable caller-to-subscription placement for cache-safe fair routing.

    The caller identity is stored only as an owner-scoped SHA-256 digest.  A
    durable row lets Hive balance distinct agents/sessions across subscription
    accounts without rotating an active conversation between provider accounts
    and paying a fresh upstream prefix-cache prefill on every turn.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.CharField(max_length=32, db_index=True)
    sticky_key_hash = models.CharField(max_length=64)
    entry = models.ForeignKey(
        TokenEntry,
        on_delete=models.CASCADE,
        related_name='sticky_assignments',
    )
    assigned_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        db_table = 'claws_tokenstickyassignment'
        constraints = [
            models.UniqueConstraint(
                fields=['provider', 'sticky_key_hash'],
                name='unique_provider_sticky_assignment',
            ),
        ]
        indexes = [
            models.Index(
                fields=['entry', 'expires_at'],
                name='claws_token_entry_i_e48245_idx',
            ),
        ]

    def __str__(self):
        return f'TokenStickyAssignment({self.provider}:{self.sticky_key_hash[:8]} -> {self.entry_id})'


class TokenLease(models.Model):
    """Short-lived lease binding a raw-token read to a single broker + entry (HIVE-127 §3).

    Issued by ``GET /model-token`` when a broker/workload identity successfully
    reads a token. ``report-status`` (cooldown/deactivate) is accepted ONLY with a
    valid, unexpired lease whose ``broker_identity`` and ``entry`` both match the
    report. This makes cross-owner token poisoning impossible without a guessed
    ``entry_id`` AND a forged broker-scoped lease: a broker that never leased A's
    entry cannot cool/deactivate it. Leases are single-issuer; an idempotent retry
    of a still-valid lease is safe (same entry, same broker).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    entry = models.ForeignKey(TokenEntry, on_delete=models.CASCADE, related_name='leases')
    # Opaque, verified workload identity the lease was issued to (e.g. the broker
    # token's subject / agent id). report-status must present the SAME identity.
    broker_identity = models.CharField(max_length=200, db_index=True)
    # Denormalized owner scope copied from the entry's pool at issue time, so a
    # lease validation never has to re-derive (and cannot drift to another owner).
    owner_kind = models.CharField(max_length=16, db_index=True)
    owner_id = models.CharField(max_length=128, db_index=True)
    provider = models.CharField(max_length=32)

    issued_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        db_table = 'claws_tokenlease'
        ordering = ['-issued_at']
        indexes = [
            models.Index(fields=['entry', 'broker_identity']),
        ]

    def is_valid(self, now=None):
        from django.utils import timezone

        now = now or timezone.now()
        return self.expires_at > now

    def __str__(self):
        return f'TokenLease({self.entry_id} -> {self.broker_identity}, exp={self.expires_at:%Y-%m-%dT%H:%M})'




# --- Billing (ADR-0080 / HIVE-2018) ---
class BillingPlan(models.Model):
    """Unit-agnostic plan catalog for Autonomous-Org-as-a-Service billing (HIVE-319).

    HIVE-317 owns pricing/packaging; this model deliberately stores quotas and
    included units as data so Product/operator decisions do not require schema
    changes.
    """

    plan_id = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=120)
    currency = models.CharField(max_length=3, default='INR')
    monthly_base_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    included_usage = models.JSONField(default=dict, blank=True)
    soft_quota = models.JSONField(default=dict, blank=True)
    hard_quota = models.JSONField(default=dict, blank=True)
    # HIVE-446: tenant-tier gate for privileged capability grants. These are the
    # ONLY sensitive_access_flags / credential_grant_scopes an agent spawned onto a
    # customer tenant on this plan may inherit from its team capability specs.
    # Empty (the default) means DEFAULT-DENY: a customer-tenant agent inherits none
    # of the privileged flags/scopes (e.g. host_exec, credential_broker, kubeconfig)
    # regardless of team slug. Internal/platform agents (no tenant scope) are not
    # gated by this — they keep the full team spec. Stored as data so Product can
    # entitle a tier without a schema change (mirrors the quota fields above).
    allowed_sensitive_access_flags = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            'HIVE-446 allowlist: sensitive_access_flags a customer tenant on this '
            'plan may inherit (e.g. ["host_exec"]). Empty = default-deny.'
        ),
    )
    allowed_credential_grant_scopes = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            'HIVE-446 allowlist: credential_grant_scopes a customer tenant on this '
            'plan may inherit (e.g. ["kubeconfig"]). Empty = default-deny.'
        ),
    )
    active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'claws_billingplan'
        ordering = ['plan_id']

    def __str__(self):
        return f'{self.plan_id} ({self.currency} {self.monthly_base_price})'


class PricingRate(models.Model):
    """Per-plan rate card row for one billable usage metric (HIVE-346)."""

    class OveragePolicy(models.TextChoices):
        INCLUDED = 'included', 'Included only'
        PASS_THROUGH = 'pass_through', 'Pass-through'
        OVERAGE = 'overage', 'Overage'
        MANUAL_REVIEW = 'manual_review', 'Manual review'

    plan = models.ForeignKey(BillingPlan, on_delete=models.CASCADE, related_name='rates')
    metric = models.CharField(max_length=64, db_index=True)
    unit = models.CharField(max_length=32)
    model = models.CharField(
        max_length=128,
        blank=True,
        default='',
        help_text='Optional model/provider-specific rate override; blank is the default rate for the metric.',
    )
    currency = models.CharField(max_length=3, default='INR')
    unit_price = models.DecimalField(max_digits=14, decimal_places=6, default=0)
    included_quantity = models.DecimalField(max_digits=24, decimal_places=6, default=0)
    overage_policy = models.CharField(
        max_length=20,
        choices=OveragePolicy.choices,
        default=OveragePolicy.OVERAGE,
    )
    active = models.BooleanField(default=True, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'claws_pricingrate'
        ordering = ['plan__plan_id', 'metric', 'model', 'unit']
        constraints = [
            models.UniqueConstraint(
                fields=['plan', 'metric', 'unit', 'model'],
                name='uniq_pricing_rate_plan_metric_unit_model',
            ),
        ]
        indexes = [
            models.Index(fields=['metric', 'unit']),
        ]

    def __str__(self):
        suffix = f'/{self.model}' if self.model else ''
        return f'{self.plan.plan_id}:{self.metric}{suffix} {self.unit_price} {self.currency}/{self.unit}'


class TenantSubscription(models.Model):
    """Tenant -> plan binding used by quota checks and invoice generation."""

    class Status(models.TextChoices):
        TRIAL = 'trial', 'Trial'
        ACTIVE = 'active', 'Active'
        PAST_DUE = 'past_due', 'Past due'
        SUSPENDED = 'suspended', 'Suspended'
        CANCELLED = 'cancelled', 'Cancelled'

    tenant_org_id = models.CharField(max_length=64, unique=True, db_index=True)
    tenant_org_slug = models.CharField(max_length=120, blank=True, db_index=True)
    customer_name = models.CharField(max_length=200, blank=True)
    customer_gstin = models.CharField(max_length=32, blank=True)
    billing_email = models.EmailField(blank=True)
    plan = models.ForeignKey(BillingPlan, null=True, blank=True, on_delete=models.SET_NULL, related_name='subscriptions')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.TRIAL, db_index=True)
    current_period_start = models.DateField(null=True, blank=True)
    current_period_end = models.DateField(null=True, blank=True)
    manual_collection = models.BooleanField(default=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'claws_tenantsubscription'
        ordering = ['tenant_org_slug', 'tenant_org_id']

    def __str__(self):
        plan_id = self.plan.plan_id if self.plan else 'unassigned'
        return f'{self.tenant_org_slug or self.tenant_org_id} -> {plan_id}'




# --- Metering (ADR-0080 / HIVE-2018) ---
class UsageEventQuerySet(models.QuerySet):
    """Block application-level mutation of canonical raw usage facts."""

    def update(self, **kwargs):
        raise ValidationError('UsageEvent rows are append-only; write a correction event instead.')

    def delete(self):
        raise ValidationError('UsageEvent rows are append-only and cannot be deleted.')


class AdmissionAuthoritySnapshotQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError('AdmissionAuthoritySnapshot rows are immutable.')

    def delete(self):
        raise ValidationError('AdmissionAuthoritySnapshot rows are immutable.')


class AdmissionAuthoritySnapshot(models.Model):
    """Server-issued immutable payer/admission fact referenced by occurrences."""

    proof_id = models.CharField(max_length=255, primary_key=True)
    proof_sha256 = models.CharField(max_length=64, unique=True)
    billing_account_id = models.CharField(max_length=128, db_index=True)
    reservation_id = models.CharField(max_length=255, blank=True)
    lease_id = models.CharField(max_length=255, blank=True)
    payer_link_generation = models.BigIntegerField()
    binding_generation = models.BigIntegerField()
    admission_generation = models.BigIntegerField()
    snapshot = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AdmissionAuthoritySnapshotQuerySet.as_manager()

    class Meta:
        db_table = 'claws_admissionauthoritysnapshot'
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('AdmissionAuthoritySnapshot rows are immutable.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('AdmissionAuthoritySnapshot rows are immutable.')


class UsageEvent(models.Model):
    """Immutable raw tenant usage event.

    Producers can be Hive, Cortex, runtime/fleet, storage accounting, or future
    services. `metric` + `unit` remain unit-agnostic so pricing can evolve after
    HIVE-317 without losing raw evidence.
    """

    class Source(models.TextChoices):
        HIVE = 'hive', 'Hive'
        CORTEX = 'cortex', 'Cortex'
        RUNTIME = 'runtime', 'Runtime'
        FLEET = 'fleet', 'Fleet'
        STORAGE = 'storage', 'Storage'
        MANUAL = 'manual', 'Manual'

    class Metric(models.TextChoices):
        AGENT_TURN = 'agent_turn', 'Agent turn'
        AGENT_HOUR = 'agent_hour', 'Agent hour'
        CORTEX_INPUT_TOKEN = 'cortex_input_token', 'Cortex input token'
        CORTEX_OUTPUT_TOKEN = 'cortex_output_token', 'Cortex output token'
        CLAW_UPTIME_SECOND = 'claw_uptime_second', 'Claw uptime second'
        STORAGE_GIB_HOUR = 'storage_gib_hour', 'Storage GiB-hour'
        CUSTOM = 'custom', 'Custom'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant_org_id = models.CharField(max_length=64, blank=True, db_index=True)
    tenant_org_slug = models.CharField(max_length=120, blank=True, db_index=True)
    owner_id = models.IntegerField(null=True, blank=True, db_index=True)
    claw = models.ForeignKey('claws.Claw', null=True, blank=True, on_delete=models.SET_NULL, related_name='usage_events')
    # CloudEvents identifies one immutable occurrence by (source, id). Source is
    # a URI-reference rather than Hive's historical six-value enum; the Source
    # constants above remain useful to trusted in-process producers.
    source = models.CharField(max_length=512, db_index=True)
    event_id = models.CharField(max_length=255, null=True, blank=True)
    specversion = models.CharField(max_length=16, default='1.0')
    event_type = models.CharField(max_length=255, blank=True, db_index=True)
    subject = models.CharField(max_length=512, blank=True, db_index=True)
    dataschema = models.CharField(max_length=512, blank=True)
    datacontenttype = models.CharField(max_length=255, blank=True, default='application/json')
    event_payload = models.JSONField(default=dict, blank=True)
    payload_sha256 = models.CharField(max_length=64, blank=True, db_index=True)
    billing_account_id = models.CharField(max_length=128, blank=True, db_index=True)
    authority_snapshot = models.JSONField(default=dict, blank=True)
    authority_proof_id = models.CharField(max_length=255, blank=True)
    authority_proof_sha256 = models.CharField(max_length=64, blank=True)
    reservation_id = models.CharField(max_length=255, blank=True)
    lease_id = models.CharField(max_length=255, blank=True)
    payer_link_generation = models.BigIntegerField(null=True, blank=True)
    binding_generation = models.BigIntegerField(null=True, blank=True)
    admission_generation = models.BigIntegerField(null=True, blank=True)
    correction_of_source = models.CharField(max_length=512, blank=True)
    correction_of_event_id = models.CharField(max_length=255, blank=True)
    metric = models.CharField(max_length=64, choices=Metric.choices, db_index=True)
    unit = models.CharField(max_length=32)
    quantity = models.DecimalField(max_digits=24, decimal_places=6)
    model = models.CharField(max_length=128, blank=True, db_index=True)
    resource_id = models.CharField(max_length=160, blank=True, db_index=True)
    idempotency_key = models.CharField(max_length=160, unique=True, null=True, blank=True)
    occurred_at = models.DateTimeField(db_index=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = UsageEventQuerySet.as_manager()

    class Meta:
        db_table = 'claws_usageevent'
        ordering = ['-occurred_at', '-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['source', 'event_id'],
                condition=Q(event_id__isnull=False),
                name='uniq_usage_event_cloudevent_source_id',
            ),
        ]
        indexes = [
            models.Index(fields=['tenant_org_id', 'occurred_at']),
            models.Index(fields=['tenant_org_slug', 'occurred_at']),
            models.Index(fields=['owner_id', 'occurred_at']),
            models.Index(fields=['metric', 'occurred_at']),
            models.Index(fields=['billing_account_id', 'occurred_at'], name='usage_event_billing_occur_idx'),
            models.Index(fields=['correction_of_source', 'correction_of_event_id'], name='usage_event_correction_idx'),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError(
                'UsageEvent rows are append-only; write a reversal or adjustment event instead.'
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('UsageEvent rows are append-only and cannot be deleted.')

    def __str__(self):
        tenant = self.tenant_org_slug or self.tenant_org_id or f'user:{self.owner_id}'
        return f'{tenant} {self.metric}={self.quantity} {self.unit} @ {self.occurred_at:%Y-%m-%d}'


class UsageEventOutbox(models.Model):
    """Transactional producer outbox for at-least-once CloudEvent delivery.

    Producers create this row in the same database transaction as the resource
    mutation. Delivery retries retain the original envelope and (source, id), so
    Metering Core can return a stable replay result after any ambiguous timeout.
    """

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        DELIVERED = 'delivered', 'Delivered'
        CONFLICT = 'conflict', 'Idempotency conflict'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.CharField(max_length=512)
    event_id = models.CharField(max_length=255)
    envelope = models.JSONField()
    payload_sha256 = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    last_error = models.TextField(blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'claws_usageeventoutbox'
        ordering = ['next_attempt_at', 'created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['source', 'event_id'],
                name='uniq_usage_outbox_cloudevent_source_id',
            ),
        ]
        indexes = [
            models.Index(fields=['status', 'next_attempt_at'], name='usage_outbox_status_due_idx'),
        ]


class UsageDailyRollup(models.Model):
    """Per-tenant daily aggregate rebuilt from immutable UsageEvent rows."""

    day = models.DateField(db_index=True)
    tenant_org_id = models.CharField(max_length=64, blank=True, db_index=True)
    tenant_org_slug = models.CharField(max_length=120, blank=True, db_index=True)
    owner_id = models.IntegerField(null=True, blank=True, db_index=True)
    source = models.CharField(max_length=32, db_index=True)
    metric = models.CharField(max_length=64, db_index=True)
    unit = models.CharField(max_length=32)
    model = models.CharField(max_length=128, blank=True, db_index=True)
    quantity = models.DecimalField(max_digits=24, decimal_places=6, default=0)
    event_count = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'claws_usagedailyrollup'
        ordering = ['-day', 'tenant_org_slug', 'tenant_org_id', 'metric', 'model']
        constraints = [
            models.UniqueConstraint(
                fields=['day', 'tenant_org_id', 'tenant_org_slug', 'owner_id', 'source', 'metric', 'unit', 'model'],
                name='uniq_usage_daily_rollup_dimension',
            ),
        ]
        indexes = [
            models.Index(fields=['tenant_org_id', 'day']),
            models.Index(fields=['tenant_org_slug', 'day']),
            models.Index(fields=['metric', 'day']),
        ]

    def __str__(self):
        tenant = self.tenant_org_slug or self.tenant_org_id or f'user:{self.owner_id}'
        return f'{tenant} {self.day} {self.metric}={self.quantity} {self.unit}'


class UsageInvoiceDraft(models.Model):
    """Hive-side record of the Books draft invoice payload for one billing period."""

    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        POSTED_TO_BOOKS = 'posted_to_books', 'Posted to Books'
        FAILED = 'failed', 'Failed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant_org_id = models.CharField(max_length=64, db_index=True)
    tenant_org_slug = models.CharField(max_length=120, blank=True, db_index=True)
    period_start = models.DateField(db_index=True)
    period_end = models.DateField(db_index=True)
    currency = models.CharField(max_length=3, default='INR')
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    tax_rate_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    books_invoice_id = models.CharField(max_length=128, blank=True)
    books_payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'claws_usageinvoicedraft'
        ordering = ['-period_end', 'tenant_org_slug', 'tenant_org_id']
        constraints = [
            models.UniqueConstraint(
                fields=['tenant_org_id', 'period_start', 'period_end'],
                name='uniq_usage_invoice_draft_period',
            ),
        ]

    def __str__(self):
        return f'{self.tenant_org_slug or self.tenant_org_id} {self.period_start}..{self.period_end} {self.total} {self.currency}'


