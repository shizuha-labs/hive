"""Metering Core identity, payer authority, and immutable catalog (PLAT-4481/4483).

Design pin: HLD b131fbff v44 · ADR-0032 Accepted v15 · ADR-0022 Accepted v15.

Wave 0 foundation:
- BillingAccount + optional PostpaidFacility
- Append-only dual-consent payer authority (proposal/consent/grant/binding)
- Rebuildable CurrentResourcePayer projection
- PayerGenesisManifest with quarantine
- PayerAuthorityProof (server-issued immutable admission fact)
- Versioned MeterDefinition / PlanVersion / RateVersion / EntitlementGrant

Legacy BillingPlan / PricingRate / TenantSubscription remain for migration
parity; new authority never mutates them as the SoT.
"""
from __future__ import annotations

import hashlib
import json
import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone


def _json_dict():
    return {}


def _json_list():
    return []


def _canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), default=str)


def canonical_sha256(value) -> str:
    return hashlib.sha256(_canonical_json(value).encode('utf-8')).hexdigest()


class ImmutableQuerySet(models.QuerySet):
    """Block bulk mutation of append-only / immutable rows."""

    def update(self, **kwargs):
        raise ValidationError(f'{self.model.__name__} rows are immutable.')

    def delete(self):
        raise ValidationError(f'{self.model.__name__} rows are immutable.')


class ImmutableModel(models.Model):
    """Mixin: rows may be inserted once; never updated or deleted."""

    objects = ImmutableQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError(f'{self.__class__.__name__} rows are immutable.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(f'{self.__class__.__name__} rows are immutable.')


# ---------------------------------------------------------------------------
# BillingAccount identity (PLAT-4481)
# ---------------------------------------------------------------------------


class BillingAccount(models.Model):
    """Metering identity/payer root (HLD §2, §12.1).

    UUID is immutable. collection_status is a state machine. policy_generation
    and admission_epoch are monotonic fences bumped on policy/revoke events.
    """

    class AccountType(models.TextChoices):
        INDIVIDUAL = 'individual', 'Individual'
        ORGANIZATION = 'organization', 'Organization'

    class CollectionStatus(models.TextChoices):
        ACTIVE = 'active', 'Active'
        PAST_DUE = 'past_due', 'Past due'
        RESTRICTED = 'restricted', 'Restricted'
        BARRED = 'barred', 'Barred'
        CLOSED = 'closed', 'Closed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account_type = models.CharField(max_length=32, choices=AccountType.choices)
    currency = models.CharField(max_length=3, default='INR')
    collection_status = models.CharField(
        max_length=20,
        choices=CollectionStatus.choices,
        default=CollectionStatus.ACTIVE,
        db_index=True,
    )
    # Display snapshots only — never keys (HLD §2).
    display_name = models.CharField(max_length=200, blank=True, default='')
    owner_user_id = models.IntegerField(null=True, blank=True, db_index=True)
    owner_org_id = models.CharField(max_length=64, blank=True, default='', db_index=True)
    monthly_anchor_day = models.PositiveSmallIntegerField(
        default=1,
        help_text='Immutable day-of-month (1-28) for grant period anchors.',
    )
    monthly_anchor_timezone = models.CharField(max_length=64, default='Asia/Kolkata')
    policy_generation = models.BigIntegerField(default=1)
    admission_epoch = models.BigIntegerField(default=1)
    metadata = models.JSONField(default=_json_dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['account_type', 'collection_status']),
            models.Index(fields=['owner_user_id', 'account_type']),
            models.Index(fields=['owner_org_id', 'account_type']),
        ]
        constraints = [
            models.CheckConstraint(
                check=Q(monthly_anchor_day__gte=1) & Q(monthly_anchor_day__lte=28),
                name='billing_account_anchor_day_1_28',
            ),
            models.CheckConstraint(
                check=Q(policy_generation__gte=1) & Q(admission_epoch__gte=1),
                name='billing_account_generations_positive',
            ),
        ]

    def __str__(self):
        return f'BillingAccount({self.id} {self.account_type} {self.collection_status})'

    def save(self, *args, **kwargs):
        if not self._state.adding:
            # UUID and type are immutable after create.
            prior = BillingAccount.objects.filter(pk=self.pk).values(
                'id', 'account_type', 'currency', 'monthly_anchor_day', 'monthly_anchor_timezone'
            ).first()
            if prior is not None:
                if str(prior['id']) != str(self.id):
                    raise ValidationError('BillingAccount.id is immutable.')
                if prior['account_type'] != self.account_type:
                    raise ValidationError('BillingAccount.account_type is immutable.')
                if prior['currency'] != self.currency:
                    raise ValidationError('BillingAccount.currency is immutable.')
                if prior['monthly_anchor_day'] != self.monthly_anchor_day:
                    raise ValidationError('BillingAccount.monthly_anchor_day is immutable.')
                if prior['monthly_anchor_timezone'] != self.monthly_anchor_timezone:
                    raise ValidationError('BillingAccount.monthly_anchor_timezone is immutable.')
        return super().save(*args, **kwargs)

    def bump_admission_epoch(self, *, reason: str = '') -> int:
        """Monotonic fence on revoke/rebind (HLD §2). Returns new epoch."""
        BillingAccount.objects.filter(pk=self.pk).update(
            admission_epoch=models.F('admission_epoch') + 1,
            updated_at=timezone.now(),
        )
        self.refresh_from_db(fields=['admission_epoch', 'updated_at'])
        meta = dict(self.metadata or {})
        events = list(meta.get('admission_epoch_events') or [])
        events.append({'epoch': self.admission_epoch, 'reason': reason, 'at': timezone.now().isoformat()})
        meta['admission_epoch_events'] = events[-50:]
        BillingAccount.objects.filter(pk=self.pk).update(metadata=meta)
        self.metadata = meta
        return int(self.admission_epoch)

    def bump_policy_generation(self, *, reason: str = '') -> int:
        BillingAccount.objects.filter(pk=self.pk).update(
            policy_generation=models.F('policy_generation') + 1,
            updated_at=timezone.now(),
        )
        self.refresh_from_db(fields=['policy_generation', 'updated_at'])
        return int(self.policy_generation)

    def transition_collection_status(self, new_status: str) -> None:
        allowed = {
            self.CollectionStatus.ACTIVE: {
                self.CollectionStatus.PAST_DUE,
                self.CollectionStatus.RESTRICTED,
                self.CollectionStatus.BARRED,
                self.CollectionStatus.CLOSED,
            },
            self.CollectionStatus.PAST_DUE: {
                self.CollectionStatus.ACTIVE,
                self.CollectionStatus.RESTRICTED,
                self.CollectionStatus.BARRED,
                self.CollectionStatus.CLOSED,
            },
            self.CollectionStatus.RESTRICTED: {
                self.CollectionStatus.ACTIVE,
                self.CollectionStatus.PAST_DUE,
                self.CollectionStatus.BARRED,
                self.CollectionStatus.CLOSED,
            },
            self.CollectionStatus.BARRED: {
                self.CollectionStatus.RESTRICTED,
                self.CollectionStatus.CLOSED,
            },
            self.CollectionStatus.CLOSED: set(),
        }
        if new_status not in allowed.get(self.collection_status, set()):
            raise ValidationError(
                f'Illegal collection_status transition {self.collection_status!r} → {new_status!r}.'
            )
        self.collection_status = new_status
        if new_status == self.CollectionStatus.CLOSED:
            self.closed_at = timezone.now()
        self.save(update_fields=['collection_status', 'closed_at', 'updated_at'])


class PostpaidFacility(ImmutableModel):
    """Optional approved postpaid headroom on a BillingAccount (HLD §12.1).

    Prepaid/free remain universal; postpaid is additional headroom, never an
    exclusive account mode. Immutable once activated — changes create a new row.
    """

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        ACTIVE = 'active', 'Active'
        REVOKED = 'revoked', 'Revoked'
        EXPIRED = 'expired', 'Expired'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    billing_account = models.ForeignKey(
        BillingAccount, on_delete=models.PROTECT, related_name='postpaid_facilities'
    )
    seller_legal_entity_id = models.CharField(max_length=128)
    seller_gst_registration_id = models.CharField(max_length=64, blank=True, default='')
    books_organization_id = models.CharField(max_length=128)
    gateway_merchant_account = models.CharField(max_length=128, blank=True, default='')
    currency = models.CharField(max_length=3, default='INR')
    limit_inr_micros = models.BigIntegerField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    effective_start = models.DateTimeField()
    effective_end = models.DateTimeField(null=True, blank=True)
    facility_version = models.PositiveIntegerField(default=1)
    canonical_hash = models.CharField(max_length=64)
    metadata = models.JSONField(default=_json_dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['billing_account', 'status']),
            models.Index(
                fields=[
                    'billing_account',
                    'seller_legal_entity_id',
                    'seller_gst_registration_id',
                    'books_organization_id',
                    'currency',
                ],
                name='postpaid_fac_part_idx',
            ),
        ]


# ---------------------------------------------------------------------------
# Dual-consent payer authority (PLAT-4481)
# ---------------------------------------------------------------------------


class PayerLinkProposal(models.Model):
    """Two-sided purpose-bound payer-link proposal (HLD §2).

    Binds target BillingAccount, scope, budget policy, finite interval,
    expected binding/scope version, canonical hash and idempotency key.

    Semantic payload fields are immutable after create; only status may change.
    """

    class ScopeKind(models.TextChoices):
        RESOURCE = 'resource', 'Resource'
        PROJECT = 'project', 'Project'

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        ACTIVATED = 'activated', 'Activated'
        REJECTED = 'rejected', 'Rejected'
        EXPIRED = 'expired', 'Expired'
        SUPERSEDED = 'superseded', 'Superseded'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    target_billing_account = models.ForeignKey(
        BillingAccount, on_delete=models.PROTECT, related_name='payer_link_proposals'
    )
    scope_kind = models.CharField(max_length=16, choices=ScopeKind.choices)
    scope_id = models.CharField(max_length=255, db_index=True)
    allowed_resource_types = models.JSONField(default=_json_list)
    budget_policy_version = models.CharField(max_length=128)
    budget_hard_cap_inr_micros = models.BigIntegerField(null=True, blank=True)
    no_additional_cap = models.BooleanField(default=False)
    interval_start = models.DateTimeField()
    interval_end = models.DateTimeField()
    expected_binding_version = models.BigIntegerField(null=True, blank=True)
    expected_scope_version = models.BigIntegerField(null=True, blank=True)
    purpose = models.CharField(max_length=64, default='money-movement-reauth-v1')
    canonical_hash = models.CharField(max_length=64, db_index=True)
    idempotency_key = models.CharField(max_length=160, unique=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True)
    proposer_subject = models.CharField(max_length=255)
    proposer_role_generation = models.BigIntegerField(default=1)
    metadata = models.JSONField(default=_json_dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['scope_kind', 'scope_id', 'status']),
            models.Index(fields=['target_billing_account', 'status']),
        ]

    _IMMUTABLE_FIELDS = (
        'target_billing_account_id',
        'scope_kind',
        'scope_id',
        'allowed_resource_types',
        'budget_policy_version',
        'budget_hard_cap_inr_micros',
        'no_additional_cap',
        'interval_start',
        'interval_end',
        'expected_binding_version',
        'expected_scope_version',
        'purpose',
        'canonical_hash',
        'idempotency_key',
        'proposer_subject',
        'proposer_role_generation',
    )

    def payload_for_hash(self) -> dict:
        return {
            'target_billing_account_id': str(self.target_billing_account_id),
            'scope_kind': self.scope_kind,
            'scope_id': self.scope_id,
            'allowed_resource_types': list(self.allowed_resource_types or []),
            'budget_policy_version': self.budget_policy_version,
            'budget_hard_cap_inr_micros': self.budget_hard_cap_inr_micros,
            'no_additional_cap': bool(self.no_additional_cap),
            'interval_start': self.interval_start.isoformat() if self.interval_start else None,
            'interval_end': self.interval_end.isoformat() if self.interval_end else None,
            'expected_binding_version': self.expected_binding_version,
            'expected_scope_version': self.expected_scope_version,
            'purpose': self.purpose,
            'idempotency_key': self.idempotency_key,
        }

    def save(self, *args, **kwargs):
        if not self._state.adding:
            prior = PayerLinkProposal.objects.filter(pk=self.pk).values(*self._IMMUTABLE_FIELDS).first()
            if prior is not None:
                for field in self._IMMUTABLE_FIELDS:
                    current = getattr(self, field)
                    previous = prior[field]
                    if field == 'allowed_resource_types':
                        current = list(current or [])
                        previous = list(previous or [])
                    if str(current) != str(previous):
                        raise ValidationError(f'PayerLinkProposal.{field} is immutable.')
        return super().save(*args, **kwargs)


class PayerLinkConsent(ImmutableModel):
    """One purpose-bound AAL2 consent over an identical proposal hash."""

    class Role(models.TextChoices):
        RESOURCE_OWNER = 'resource_owner', 'Resource owner'
        BILLING_ADMIN = 'billing_admin', 'Billing admin'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    proposal = models.ForeignKey(
        PayerLinkProposal, on_delete=models.PROTECT, related_name='consents'
    )
    role = models.CharField(max_length=32, choices=Role.choices)
    subject = models.CharField(max_length=255)
    role_generation = models.BigIntegerField()
    proposal_canonical_hash = models.CharField(max_length=64)
    aal = models.CharField(max_length=16, default='aal2')
    auth_time = models.DateTimeField()
    action_grant_id = models.CharField(max_length=255)
    purpose = models.CharField(max_length=64, default='money-movement-reauth-v1')
    metadata = models.JSONField(default=_json_dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['proposal', 'role', 'action_grant_id'],
                name='uniq_payer_link_consent_action_grant',
            ),
        ]
        indexes = [
            models.Index(fields=['proposal', 'role']),
        ]


class PayerLinkGrantVersion(models.Model):
    """Append-only activated payer-link grant (HLD §2).

    Semantic payload is immutable; status/revoked_at may transition on revoke.
    """

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        REVOKED = 'revoked', 'Revoked'
        SUPERSEDED = 'superseded', 'Superseded'
        EXPIRED = 'expired', 'Expired'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    grant_id = models.UUIDField(db_index=True, help_text='Stable grant identity across versions.')
    version = models.PositiveIntegerField()
    proposal = models.ForeignKey(
        PayerLinkProposal, on_delete=models.PROTECT, related_name='grant_versions'
    )
    billing_account = models.ForeignKey(
        BillingAccount, on_delete=models.PROTECT, related_name='payer_link_grants'
    )
    scope_kind = models.CharField(max_length=16)
    scope_id = models.CharField(max_length=255, db_index=True)
    allowed_resource_types = models.JSONField(default=_json_list)
    budget_policy_version = models.CharField(max_length=128)
    budget_hard_cap_inr_micros = models.BigIntegerField(null=True, blank=True)
    no_additional_cap = models.BooleanField(default=False)
    interval_start = models.DateTimeField()
    interval_end = models.DateTimeField()
    canonical_hash = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE, db_index=True)
    resource_owner_consent = models.ForeignKey(
        PayerLinkConsent,
        on_delete=models.PROTECT,
        related_name='+',
    )
    billing_admin_consent = models.ForeignKey(
        PayerLinkConsent,
        on_delete=models.PROTECT,
        related_name='+',
    )
    admission_epoch_at_activation = models.BigIntegerField()
    activated_at = models.DateTimeField(default=timezone.now)
    revoked_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=_json_dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-activated_at', '-version']
        constraints = [
            models.UniqueConstraint(
                fields=['grant_id', 'version'],
                name='uniq_payer_link_grant_version',
            ),
        ]
        indexes = [
            models.Index(fields=['scope_kind', 'scope_id', 'status']),
            models.Index(fields=['billing_account', 'status']),
        ]

    _IMMUTABLE_FIELDS = (
        'grant_id',
        'version',
        'proposal_id',
        'billing_account_id',
        'scope_kind',
        'scope_id',
        'allowed_resource_types',
        'budget_policy_version',
        'budget_hard_cap_inr_micros',
        'no_additional_cap',
        'interval_start',
        'interval_end',
        'canonical_hash',
        'resource_owner_consent_id',
        'billing_admin_consent_id',
        'admission_epoch_at_activation',
        'activated_at',
    )

    def save(self, *args, **kwargs):
        if not self._state.adding:
            prior = PayerLinkGrantVersion.objects.filter(pk=self.pk).values(*self._IMMUTABLE_FIELDS).first()
            if prior is not None:
                for field in self._IMMUTABLE_FIELDS:
                    current = getattr(self, field)
                    previous = prior[field]
                    if field == 'allowed_resource_types':
                        current = list(current or [])
                        previous = list(previous or [])
                    if str(current) != str(previous):
                        raise ValidationError(f'PayerLinkGrantVersion.{field} is immutable.')
        return super().save(*args, **kwargs)


class ResourcePayerBindingVersion(models.Model):
    """Exact resource→payer materialization from an active grant.

    Semantic payload immutable; status may close on revoke/rebind.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    resource_uid = models.CharField(max_length=255, db_index=True)
    resource_type = models.CharField(max_length=128)
    binding_version = models.BigIntegerField()
    grant_version = models.ForeignKey(
        PayerLinkGrantVersion, on_delete=models.PROTECT, related_name='bindings'
    )
    billing_account = models.ForeignKey(
        BillingAccount, on_delete=models.PROTECT, related_name='resource_payer_bindings'
    )
    admission_epoch = models.BigIntegerField()
    budget_policy_version = models.CharField(max_length=128)
    interval_start = models.DateTimeField()
    interval_end = models.DateTimeField()
    canonical_hash = models.CharField(max_length=64)
    status = models.CharField(max_length=16, default='active', db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-binding_version']
        constraints = [
            models.UniqueConstraint(
                fields=['resource_uid', 'binding_version'],
                name='uniq_resource_payer_binding_version',
            ),
        ]
        indexes = [
            models.Index(fields=['resource_uid', 'status']),
            models.Index(fields=['billing_account', 'status']),
        ]

    _IMMUTABLE_FIELDS = (
        'resource_uid',
        'resource_type',
        'binding_version',
        'grant_version_id',
        'billing_account_id',
        'admission_epoch',
        'budget_policy_version',
        'interval_start',
        'interval_end',
        'canonical_hash',
    )

    def save(self, *args, **kwargs):
        if not self._state.adding:
            prior = ResourcePayerBindingVersion.objects.filter(pk=self.pk).values(
                *self._IMMUTABLE_FIELDS
            ).first()
            if prior is not None:
                for field in self._IMMUTABLE_FIELDS:
                    if str(getattr(self, field)) != str(prior[field]):
                        raise ValidationError(f'ResourcePayerBindingVersion.{field} is immutable.')
        return super().save(*args, **kwargs)


class CurrentResourcePayer(models.Model):
    """Rebuildable projection of the active resource→payer binding.

    Never authoritative — always derived from ResourcePayerBindingVersion.
    """

    resource_uid = models.CharField(max_length=255, primary_key=True)
    resource_type = models.CharField(max_length=128)
    billing_account = models.ForeignKey(
        BillingAccount, on_delete=models.PROTECT, related_name='current_resource_payers'
    )
    grant_version = models.ForeignKey(
        PayerLinkGrantVersion, on_delete=models.PROTECT, related_name='+'
    )
    binding_version = models.ForeignKey(
        ResourcePayerBindingVersion, on_delete=models.PROTECT, related_name='+'
    )
    admission_epoch = models.BigIntegerField()
    rebuilt_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['resource_uid']


class PayerGenesisManifest(models.Model):
    """Signed source-versioned self-payer migration (HLD §2).

    Source identity/hash immutable; status and counters update during import.
    """

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        IMPORTING = 'importing', 'Importing'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_system = models.CharField(max_length=128)
    source_schema_version = models.CharField(max_length=64)
    source_watermark = models.CharField(max_length=255)
    writer_authority_generation = models.BigIntegerField()
    signature = models.TextField(blank=True, default='')
    canonical_hash = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    imported_count = models.PositiveIntegerField(default=0)
    quarantined_count = models.PositiveIntegerField(default=0)
    metadata = models.JSONField(default=_json_dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    _IMMUTABLE_FIELDS = (
        'source_system',
        'source_schema_version',
        'source_watermark',
        'writer_authority_generation',
        'signature',
        'canonical_hash',
    )

    def save(self, *args, **kwargs):
        if not self._state.adding:
            prior = PayerGenesisManifest.objects.filter(pk=self.pk).values(*self._IMMUTABLE_FIELDS).first()
            if prior is not None:
                for field in self._IMMUTABLE_FIELDS:
                    if str(getattr(self, field)) != str(prior[field]):
                        raise ValidationError(f'PayerGenesisManifest.{field} is immutable.')
        return super().save(*args, **kwargs)


class PayerGenesisFact(ImmutableModel):
    """One genesis row: accepted self-payer grant or quarantined ambiguity."""

    class Decision(models.TextChoices):
        SELF_PAYER = 'self_payer', 'Self payer'
        REQUIRES_CONSENT = 'requires_consent', 'Requires dual consent'
        QUARANTINED = 'quarantined', 'Quarantined'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    manifest = models.ForeignKey(
        PayerGenesisManifest, on_delete=models.PROTECT, related_name='facts'
    )
    source_object_id = models.CharField(max_length=255)
    source_object_version = models.CharField(max_length=128)
    source_digest = models.CharField(max_length=64)
    resource_uid = models.CharField(max_length=255, db_index=True)
    resource_type = models.CharField(max_length=128)
    target_billing_account = models.ForeignKey(
        BillingAccount,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='genesis_facts',
    )
    decision = models.CharField(max_length=32, choices=Decision.choices, db_index=True)
    quarantine_reason = models.TextField(blank=True, default='')
    ownership_evidence = models.JSONField(default=_json_dict)
    resulting_grant_version = models.ForeignKey(
        PayerLinkGrantVersion,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['manifest', 'source_object_id', 'source_object_version'],
                name='uniq_payer_genesis_fact_source',
            ),
        ]


class PayerAuthorityProof(ImmutableModel):
    """Server-issued immutable payer proof for one reservation/lease/allocation.

    Occurrence ingestion resolves this historical proof; body account / current
    binding lookup is forbidden (HLD §2, §12.1 AdmissionAuthoritySnapshot).
    """

    proof_id = models.CharField(max_length=255, primary_key=True)
    proof_sha256 = models.CharField(max_length=64, unique=True)
    resource_uid = models.CharField(max_length=255, db_index=True)
    billing_account = models.ForeignKey(
        BillingAccount, on_delete=models.PROTECT, related_name='authority_proofs'
    )
    grant_version = models.ForeignKey(
        PayerLinkGrantVersion, on_delete=models.PROTECT, related_name='authority_proofs'
    )
    binding_version = models.ForeignKey(
        ResourcePayerBindingVersion, on_delete=models.PROTECT, related_name='authority_proofs'
    )
    budget_policy_version = models.CharField(max_length=128)
    admission_epoch = models.BigIntegerField()
    reservation_id = models.CharField(max_length=255, blank=True, default='')
    lease_id = models.CharField(max_length=255, blank=True, default='')
    seller_legal_entity_id = models.CharField(max_length=128, blank=True, default='')
    seller_gst_registration_id = models.CharField(max_length=64, blank=True, default='')
    books_organization_id = models.CharField(max_length=128, blank=True, default='')
    gateway_merchant_account = models.CharField(max_length=128, blank=True, default='')
    currency = models.CharField(max_length=3, default='INR')
    authorized_max_inr_micros = models.BigIntegerField(null=True, blank=True)
    operation_hash = models.CharField(max_length=64)
    valid_from = models.DateTimeField()
    valid_until = models.DateTimeField()
    snapshot = models.JSONField(default=_json_dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['billing_account', 'created_at']),
            models.Index(fields=['reservation_id']),
            models.Index(fields=['lease_id']),
        ]


# ---------------------------------------------------------------------------
# Immutable versioned catalog (PLAT-4483)
# ---------------------------------------------------------------------------


class MeterDefinition(ImmutableModel):
    """Immutable once effective meter definition (HLD §4)."""

    class Aggregation(models.TextChoices):
        SUM = 'sum', 'Sum'
        MAX = 'max', 'Max'
        LAST = 'last', 'Last'
        DISTINCT_COUNT = 'distinct_count', 'Distinct count'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=128, db_index=True)
    version = models.PositiveIntegerField()
    display_name = models.CharField(max_length=200)
    event_type = models.CharField(max_length=255)
    value_selector = models.CharField(max_length=255)
    aggregation = models.CharField(max_length=32, choices=Aggregation.choices, default=Aggregation.SUM)
    canonical_unit = models.CharField(max_length=64)
    dimension_schema = models.JSONField(default=_json_dict, blank=True)
    window_semantics = models.JSONField(default=_json_dict, blank=True)
    reset_semantics = models.JSONField(default=_json_dict, blank=True)
    late_arrival_semantics = models.JSONField(default=_json_dict, blank=True)
    effective_start = models.DateTimeField()
    effective_end = models.DateTimeField(null=True, blank=True)
    deprecated_by_key = models.CharField(max_length=128, blank=True, default='')
    deprecated_by_version = models.PositiveIntegerField(null=True, blank=True)
    canonical_hash = models.CharField(max_length=64)
    metadata = models.JSONField(default=_json_dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['key', 'version']
        constraints = [
            models.UniqueConstraint(fields=['key', 'version'], name='uniq_meter_definition_key_version'),
        ]
        indexes = [
            models.Index(fields=['event_type', 'key']),
            models.Index(fields=['effective_start', 'effective_end']),
        ]

    def __str__(self):
        return f'{self.key}@v{self.version}'


class PlanVersion(ImmutableModel):
    """Immutable commercial plan version (HLD §6, §12.2)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plan_key = models.CharField(max_length=128, db_index=True)
    version = models.PositiveIntegerField()
    display_name = models.CharField(max_length=200)
    currency = models.CharField(max_length=3, default='INR')
    tax_inclusive = models.BooleanField(default=True)
    advertised_price_inr_micros = models.BigIntegerField(default=0)
    cadence = models.CharField(max_length=32, default='monthly')
    seller_legal_entity_id = models.CharField(max_length=128, blank=True, default='')
    seller_gst_registration_id = models.CharField(max_length=64, blank=True, default='')
    books_organization_id = models.CharField(max_length=128, blank=True, default='')
    product_sku = models.CharField(max_length=128, blank=True, default='')
    tax_policy_ref = models.CharField(max_length=128, blank=True, default='')
    components = models.JSONField(default=_json_list, blank=True)
    caps = models.JSONField(default=_json_dict, blank=True)
    effective_start = models.DateTimeField()
    effective_end = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=32, default='active', db_index=True)
    canonical_hash = models.CharField(max_length=64)
    metadata = models.JSONField(default=_json_dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['plan_key', 'version']
        constraints = [
            models.UniqueConstraint(fields=['plan_key', 'version'], name='uniq_plan_version_key_version'),
        ]

    def __str__(self):
        return f'{self.plan_key}@v{self.version}'


class SubscriptionTierVersion(ImmutableModel):
    """Immutable subscription tier under a PlanVersion."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plan_version = models.ForeignKey(
        PlanVersion, on_delete=models.PROTECT, related_name='tier_versions'
    )
    tier_key = models.CharField(max_length=128)
    version = models.PositiveIntegerField()
    display_name = models.CharField(max_length=200)
    factor = models.DecimalField(max_digits=10, decimal_places=4, default=1)
    caps = models.JSONField(default=_json_dict, blank=True)
    effective_start = models.DateTimeField()
    effective_end = models.DateTimeField(null=True, blank=True)
    canonical_hash = models.CharField(max_length=64)
    metadata = models.JSONField(default=_json_dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['tier_key', 'version']
        constraints = [
            models.UniqueConstraint(
                fields=['plan_version', 'tier_key', 'version'],
                name='uniq_subscription_tier_version',
            ),
        ]


class RateVersion(ImmutableModel):
    """Immutable per-meter rate (HLD §12.2). No billable meter without one."""

    class Rounding(models.TextChoices):
        HALF_UP = 'half_up', 'Half up'
        CEILING = 'ceiling', 'Ceiling'
        FLOOR = 'floor', 'Floor'
        BANKERS = 'bankers', 'Bankers'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    rate_key = models.CharField(max_length=128, db_index=True)
    version = models.PositiveIntegerField()
    meter_definition = models.ForeignKey(
        MeterDefinition, on_delete=models.PROTECT, related_name='rate_versions'
    )
    plan_version = models.ForeignKey(
        PlanVersion,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='rate_versions',
    )
    currency = models.CharField(max_length=3, default='INR')
    unit = models.CharField(max_length=64)
    inr_micros_per_unit = models.BigIntegerField(default=0)
    tier_formula = models.JSONField(default=_json_dict, blank=True)
    minimum_quantum = models.DecimalField(max_digits=24, decimal_places=6, default=0)
    rounding = models.CharField(max_length=16, choices=Rounding.choices, default=Rounding.HALF_UP)
    seller_legal_entity_id = models.CharField(max_length=128, blank=True, default='')
    seller_gst_registration_id = models.CharField(max_length=64, blank=True, default='')
    books_organization_id = models.CharField(max_length=128, blank=True, default='')
    gateway_merchant_account = models.CharField(max_length=128, blank=True, default='')
    collection_partition = models.CharField(max_length=128, blank=True, default='')
    margin_policy_ref = models.CharField(max_length=128, blank=True, default='')
    effective_start = models.DateTimeField()
    effective_end = models.DateTimeField(null=True, blank=True)
    canonical_hash = models.CharField(max_length=64)
    metadata = models.JSONField(default=_json_dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['rate_key', 'version']
        constraints = [
            models.UniqueConstraint(fields=['rate_key', 'version'], name='uniq_rate_version_key_version'),
        ]
        indexes = [
            models.Index(fields=['meter_definition', 'effective_start']),
            models.Index(
                fields=[
                    'seller_legal_entity_id',
                    'seller_gst_registration_id',
                    'books_organization_id',
                    'currency',
                ],
                name='rate_version_partition_idx',
            ),
        ]


class EntitlementGrant(ImmutableModel):
    """Immutable entitlement grant (HLD §6, §12.2). Reset is generation-based."""

    class Behavior(models.TextChoices):
        HARD = 'hard', 'Hard'
        SOFT = 'soft', 'Soft'
        OVERAGE = 'overage', 'Overage'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    grant_key = models.CharField(max_length=160, db_index=True)
    version = models.PositiveIntegerField(default=1)
    billing_account = models.ForeignKey(
        BillingAccount,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='entitlement_grants',
        help_text='Null = catalog template grant (e.g. free-v1); set for account-scoped issuance.',
    )
    plan_version = models.ForeignKey(
        PlanVersion,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='entitlement_grants',
    )
    meter_definition = models.ForeignKey(
        MeterDefinition,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='entitlement_grants',
    )
    cap_key = models.CharField(max_length=128, blank=True, default='', db_index=True)
    amount = models.DecimalField(max_digits=28, decimal_places=6)
    unit = models.CharField(max_length=64)
    period = models.CharField(max_length=32, default='monthly')
    reset_anchor = models.CharField(max_length=64, default='billing_account_monthly')
    reset_timezone = models.CharField(max_length=64, default='Asia/Kolkata')
    priority = models.PositiveIntegerField(default=100, help_text='Lower = consumed first.')
    rollover = models.BooleanField(default=False)
    behavior = models.CharField(max_length=16, choices=Behavior.choices, default=Behavior.HARD)
    start_at_utc = models.DateTimeField(null=True, blank=True)
    end_at_utc = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    reset_generation = models.BigIntegerField(default=1)
    canonical_hash = models.CharField(max_length=64)
    metadata = models.JSONField(default=_json_dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['priority', 'grant_key', 'version']
        constraints = [
            models.UniqueConstraint(
                fields=['grant_key', 'version', 'reset_generation', 'billing_account'],
                name='uniq_entitlement_grant_key_ver_gen_acct',
            ),
        ]
        indexes = [
            models.Index(fields=['billing_account', 'priority']),
            models.Index(fields=['plan_version', 'grant_key']),
        ]


class EntitlementBalance(models.Model):
    """Rebuildable balance projection for one grant generation.

    Never mutates the grant; consumption updates this projection only.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    grant = models.ForeignKey(
        EntitlementGrant, on_delete=models.PROTECT, related_name='balances'
    )
    billing_account = models.ForeignKey(
        BillingAccount, on_delete=models.PROTECT, related_name='entitlement_balances'
    )
    reset_generation = models.BigIntegerField()
    granted_amount = models.DecimalField(max_digits=28, decimal_places=6)
    reserved_amount = models.DecimalField(max_digits=28, decimal_places=6, default=0)
    consumed_amount = models.DecimalField(max_digits=28, decimal_places=6, default=0)
    available_amount = models.DecimalField(max_digits=28, decimal_places=6)
    period_start_utc = models.DateTimeField()
    period_end_utc = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-updated_at']
        constraints = [
            models.UniqueConstraint(
                fields=['grant', 'billing_account', 'reset_generation'],
                name='uniq_entitlement_balance_grant_acct_gen',
            ),
        ]
        indexes = [
            models.Index(fields=['billing_account', 'period_end_utc']),
        ]

    @property
    def remaining(self):
        return self.granted_amount - self.reserved_amount - self.consumed_amount


# ---------------------------------------------------------------------------
# Wave 1 — atomic reservations, spend leases, proofs, writer authority (PLAT-4484)
# ---------------------------------------------------------------------------


class QuotaReservation(models.Model):
    """Atomic entitlement+spend reservation (HLD §6, §12.3).

    Lifecycle: reserved → started → committed|released|expired_unstarted|orphaned_hold
    Commit split: committed_actual + released_unused + held_unknown = original_reserved
    """

    class Status(models.TextChoices):
        RESERVED = 'reserved', 'Reserved'
        STARTED = 'started', 'Started'
        COMMITTED = 'committed', 'Committed'
        RELEASED = 'released', 'Released'
        EXPIRED_UNSTARTED = 'expired_unstarted', 'Expired unstarted'
        ORPHANED_HOLD = 'orphaned_hold', 'Orphaned hold'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    decision_id = models.CharField(max_length=64, unique=True, db_index=True)
    # Scoped idempotency: reserve / start / commit / release each have own key+hash.
    reserve_idempotency_key = models.CharField(max_length=255, db_index=True)
    reserve_payload_sha256 = models.CharField(max_length=64)
    start_idempotency_key = models.CharField(max_length=255, blank=True, default='')
    start_payload_sha256 = models.CharField(max_length=64, blank=True, default='')
    terminal_idempotency_key = models.CharField(max_length=255, blank=True, default='')
    terminal_payload_sha256 = models.CharField(max_length=64, blank=True, default='')

    billing_account = models.ForeignKey(
        BillingAccount, on_delete=models.PROTECT, related_name='quota_reservations'
    )
    entitlement_balance = models.ForeignKey(
        EntitlementBalance,
        on_delete=models.PROTECT,
        related_name='reservations',
        null=True,
        blank=True,
    )
    meter_key = models.CharField(max_length=128, db_index=True)
    meter_version = models.PositiveIntegerField(default=1)
    unit = models.CharField(max_length=64)
    resource_uid = models.CharField(max_length=255, db_index=True)
    producer_principal = models.CharField(max_length=255, blank=True, default='')
    dimensions = models.JSONField(default=_json_dict, blank=True)

    original_reserved = models.DecimalField(max_digits=28, decimal_places=6)
    committed_actual = models.DecimalField(max_digits=28, decimal_places=6, default=0)
    released_unused = models.DecimalField(max_digits=28, decimal_places=6, default=0)
    held_unknown = models.DecimalField(max_digits=28, decimal_places=6, default=0)
    server_rated_spend_reserved_inr_micros = models.BigIntegerField(default=0)

    status = models.CharField(
        max_length=32, choices=Status.choices, default=Status.RESERVED, db_index=True
    )
    allowed = models.BooleanField(default=True)
    deny_reason = models.CharField(max_length=255, blank=True, default='')

    entitlement_generation = models.BigIntegerField(default=0)
    balance_generation = models.BigIntegerField(default=0)
    rate_version_id = models.CharField(max_length=128, blank=True, default='')
    admission_epoch = models.BigIntegerField()
    authority_proof = models.ForeignKey(
        PayerAuthorityProof,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='reservations',
    )

    occurrence_ref = models.CharField(max_length=255, blank=True, default='')
    outbox_ref = models.CharField(max_length=255, blank=True, default='')
    no_start_proof_sha256 = models.CharField(max_length=64, blank=True, default='')
    terminal_proof_sha256 = models.CharField(max_length=64, blank=True, default='')

    expires_at = models.DateTimeField(db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    terminal_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    response_snapshot = models.JSONField(default=_json_dict, blank=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['reserve_idempotency_key', 'producer_principal'],
                name='uniq_quota_reservation_reserve_key_principal',
            ),
        ]
        indexes = [
            # Names pinned to 0037_plat4484_quota_reservations — keep in sync so
            # `makemigrations --check` does not emit rename-only drift (CI gate).
            models.Index(
                fields=['billing_account', 'status'],
                name='claws_quota_billing_0c0a0a_idx',
            ),
            models.Index(
                fields=['resource_uid', 'status'],
                name='claws_quota_resourc_1b1b1b_idx',
            ),
            models.Index(
                fields=['status', 'expires_at'],
                name='claws_quota_status_2c2c2c_idx',
            ),
        ]

    def split_ok(self) -> bool:
        total = self.committed_actual + self.released_unused + self.held_unknown
        return total == self.original_reserved


class SpendLease(models.Model):
    """Exclusive bounded producer chunk (≤60s) with parent-plus-child debit chain."""

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        CLOSED = 'closed', 'Closed'
        EXPIRED = 'expired', 'Expired'
        FENCED = 'fenced', 'Fenced by holder epoch'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lease_id = models.CharField(max_length=64, unique=True, db_index=True)
    parent_reservation = models.ForeignKey(
        QuotaReservation,
        on_delete=models.PROTECT,
        related_name='spend_leases',
        null=True,
        blank=True,
    )
    billing_account = models.ForeignKey(
        BillingAccount, on_delete=models.PROTECT, related_name='spend_leases'
    )
    resource_uid = models.CharField(max_length=255, db_index=True)
    producer_principal = models.CharField(max_length=255)
    producer_holder_epoch = models.BigIntegerField()
    meter_key = models.CharField(max_length=128)
    authorized_quantity = models.DecimalField(max_digits=28, decimal_places=6)
    debited_quantity = models.DecimalField(max_digits=28, decimal_places=6, default=0)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.ACTIVE, db_index=True
    )
    # EdDSA envelope metadata (signatures stored separately; never log private keys).
    lease_kid = models.CharField(max_length=128, blank=True, default='')
    lease_jws = models.TextField(blank=True, default='')
    close_kid = models.CharField(max_length=128, blank=True, default='')
    close_jws = models.TextField(blank=True, default='')
    issue_idempotency_key = models.CharField(max_length=255, db_index=True)
    issue_payload_sha256 = models.CharField(max_length=64)
    close_idempotency_key = models.CharField(max_length=255, blank=True, default='')
    close_payload_sha256 = models.CharField(max_length=64, blank=True, default='')
    expires_at = models.DateTimeField(db_index=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    response_snapshot = models.JSONField(default=_json_dict, blank=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['issue_idempotency_key', 'producer_principal'],
                name='uniq_spend_lease_issue_key_principal',
            ),
        ]
        indexes = [
            models.Index(
                fields=['billing_account', 'status'],
                name='claws_spend_billing_3d3d3d_idx',
            ),
            models.Index(
                fields=['resource_uid', 'status'],
                name='claws_spend_resourc_4e4e4e_idx',
            ),
        ]


class AdmissionProofReceipt(ImmutableModel):
    """Proof v4 receipts (Prepared / StartAuthorization / NoSideEffect / Terminal)."""

    class Profile(models.TextChoices):
        PREPARED = 'prepared_operation', 'PreparedOperationReceipt'
        START_AUTH = 'start_authorization', 'StartAuthorizationReceipt'
        NO_SIDE_EFFECT = 'no_side_effect', 'NoSideEffectProof'
        TERMINAL = 'terminal_operation', 'TerminalOperationReceipt'
        LEASE_ISSUE = 'spend_lease_issue', 'SpendLeaseIssue'
        LEASE_CLOSE = 'spend_lease_close', 'SpendLeaseClose'

    receipt_id = models.CharField(max_length=64, primary_key=True)
    profile = models.CharField(max_length=32, choices=Profile.choices, db_index=True)
    reservation = models.ForeignKey(
        QuotaReservation,
        on_delete=models.PROTECT,
        related_name='proof_receipts',
        null=True,
        blank=True,
    )
    lease = models.ForeignKey(
        SpendLease,
        on_delete=models.PROTECT,
        related_name='proof_receipts',
        null=True,
        blank=True,
    )
    payload_sha256 = models.CharField(max_length=64)
    # RFC 8785 compact JWS (EdDSA). Public verification only in this service.
    jws_compact = models.TextField(blank=True, default='')
    kid = models.CharField(max_length=128, blank=True, default='')
    algorithm = models.CharField(max_length=16, default='EdDSA')
    audience = models.CharField(max_length=128, default='metering:admission')
    payload = models.JSONField(default=_json_dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(
                fields=['profile', 'created_at'],
                name='claws_admis_profile_5f5f5f_idx',
            ),
            models.Index(
                fields=['payload_sha256'],
                name='claws_admis_payload_6a6a6a_idx',
            ),
        ]


class ProducerHolder(models.Model):
    """Exclusive durable-store incarnation with monotonic epoch (HLD §12.3)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    holder_key = models.CharField(max_length=255, unique=True)
    epoch = models.BigIntegerField(default=1)
    incarnation_id = models.CharField(max_length=64)
    last_heartbeat_at = models.DateTimeField(auto_now=True)
    metadata = models.JSONField(default=_json_dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['holder_key']


class AdmissionWriterAuthority(models.Model):
    """One active product/scope writer generation (HLD §6 cutover)."""

    class State(models.TextChoices):
        ACTIVE = 'active', 'Active'
        FREEZING = 'freezing', 'Freezing'
        IMPORTING = 'importing', 'Importing'
        RETIRED = 'retired', 'Retired'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product = models.CharField(max_length=64, db_index=True)
    generation = models.BigIntegerField()
    state = models.CharField(
        max_length=16, choices=State.choices, default=State.ACTIVE, db_index=True
    )
    source_watermark = models.CharField(max_length=255, blank=True, default='')
    cutover_manifest_sha256 = models.CharField(max_length=64, blank=True, default='')
    activated_at = models.DateTimeField(null=True, blank=True)
    retired_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    metadata = models.JSONField(default=_json_dict, blank=True)

    class Meta:
        ordering = ['-generation']
        constraints = [
            models.UniqueConstraint(
                fields=['product', 'generation'],
                name='uniq_admission_writer_product_generation',
            ),
            models.UniqueConstraint(
                fields=['product'],
                condition=Q(state='active'),
                name='uniq_admission_writer_one_active_per_product',
            ),
        ]


# ---------------------------------------------------------------------------
# Wave 2 — rollup / rating / statements (PLAT-4485)
# ---------------------------------------------------------------------------


class UsageRollup(models.Model):
    """Rebuildable usage projection for one account/meter/period slice (HLD §5).

    Generation + source_manifest_hash + event_count + producer_watermarks make
    the projection auditable and rebuildable from raw UsageEvent rows.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    billing_account = models.ForeignKey(
        BillingAccount, on_delete=models.PROTECT, related_name='usage_rollups'
    )
    period_start = models.DateTimeField(db_index=True)
    period_end = models.DateTimeField(db_index=True)
    meter_key = models.CharField(max_length=128, db_index=True)
    meter_version = models.PositiveIntegerField(default=1)
    unit = models.CharField(max_length=64)
    dimensions_hash = models.CharField(max_length=64, blank=True, default='', db_index=True)
    dimensions = models.JSONField(default=_json_dict, blank=True)
    gross_quantity = models.DecimalField(max_digits=28, decimal_places=6, default=0)
    generation = models.BigIntegerField(default=1)
    source_manifest_hash = models.CharField(max_length=64, blank=True, default='')
    event_count = models.PositiveIntegerField(default=0)
    producer_watermarks = models.JSONField(default=_json_dict, blank=True)
    rebuilt_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)
    metadata = models.JSONField(default=_json_dict, blank=True)

    class Meta:
        ordering = ['-period_end', 'meter_key']
        constraints = [
            models.UniqueConstraint(
                fields=[
                    'billing_account',
                    'period_start',
                    'period_end',
                    'meter_key',
                    'meter_version',
                    'unit',
                    'dimensions_hash',
                    'generation',
                ],
                name='uniq_usage_rollup_slice_generation',
            ),
        ]
        indexes = [
            models.Index(fields=['billing_account', 'period_start', 'period_end']),
            models.Index(fields=['source_manifest_hash']),
        ]

    def __str__(self):
        return (
            f'UsageRollup({self.billing_account_id} {self.meter_key} '
            f'{self.period_start}..{self.period_end} gen={self.generation})'
        )


class BillingStatement(models.Model):
    """Period statement with deterministic business-key identity (HLD §8).

    State machine: open → closing → rated → exported_draft → finalized.
    Finalized-period corrections use statement_kind=adjustment with an
    immutable original reference + monotonic adjustment_sequence.
    """

    class Kind(models.TextChoices):
        ORDINARY = 'ordinary', 'Ordinary'
        ADJUSTMENT = 'adjustment', 'Adjustment'

    class Status(models.TextChoices):
        OPEN = 'open', 'Open'
        CLOSING = 'closing', 'Closing'
        RATED = 'rated', 'Rated'
        EXPORTED_DRAFT = 'exported_draft', 'Exported draft'
        FINALIZED = 'finalized', 'Finalized'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Deterministic from business key — also the external statement_id.
    statement_id = models.CharField(max_length=64, unique=True, db_index=True)
    billing_account = models.ForeignKey(
        BillingAccount, on_delete=models.PROTECT, related_name='billing_statements'
    )
    seller_legal_entity_id = models.CharField(max_length=128, blank=True, default='')
    seller_gst_registration_id = models.CharField(max_length=64, blank=True, default='')
    books_organization_id = models.CharField(max_length=128, blank=True, default='')
    currency = models.CharField(max_length=3, default='INR')
    period_start = models.DateTimeField(db_index=True)
    period_end = models.DateTimeField(db_index=True)
    statement_kind = models.CharField(
        max_length=16, choices=Kind.choices, default=Kind.ORDINARY, db_index=True
    )
    adjustment_sequence = models.PositiveIntegerField(null=True, blank=True)
    original_statement_id = models.CharField(max_length=64, blank=True, default='', db_index=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.OPEN, db_index=True
    )
    revision = models.PositiveIntegerField(default=1)
    generation = models.BigIntegerField(default=1)
    source_manifest_hash = models.CharField(max_length=64, blank=True, default='')
    rollup_ids = models.JSONField(default=_json_list, blank=True)
    books_draft_etag = models.CharField(max_length=128, blank=True, default='')
    books_document_ref = models.CharField(max_length=255, blank=True, default='')
    closing_grace_until = models.DateTimeField(null=True, blank=True)
    lag_blocked = models.BooleanField(default=False)
    lag_sources = models.JSONField(default=_json_list, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    rated_at = models.DateTimeField(null=True, blank=True)
    exported_at = models.DateTimeField(null=True, blank=True)
    finalized_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    metadata = models.JSONField(default=_json_dict, blank=True)

    class Meta:
        ordering = ['-period_end', 'statement_id']
        constraints = [
            # Exactly one exporter / ordinary statement per business key.
            models.UniqueConstraint(
                fields=[
                    'billing_account',
                    'seller_legal_entity_id',
                    'seller_gst_registration_id',
                    'books_organization_id',
                    'currency',
                    'period_start',
                    'period_end',
                    'statement_kind',
                    'adjustment_sequence',
                    'original_statement_id',
                ],
                name='uniq_billing_statement_business_key',
            ),
        ]
        indexes = [
            models.Index(fields=['billing_account', 'status']),
            models.Index(fields=['period_start', 'period_end']),
        ]

    def __str__(self):
        return f'BillingStatement({self.statement_id} {self.status})'


class RatedUsageLine(models.Model):
    """Rated line: gross → included/waived → billable (HLD §5, §8).

    Metering owns measured/rated quantity and customer gross rated debit.
    Never supplies SAC, taxable value, or GST split (Books owns those).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    statement = models.ForeignKey(
        BillingStatement,
        on_delete=models.PROTECT,
        related_name='rated_lines',
    )
    rollup = models.ForeignKey(
        UsageRollup,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='rated_lines',
    )
    billing_account = models.ForeignKey(
        BillingAccount, on_delete=models.PROTECT, related_name='rated_usage_lines'
    )
    line_key = models.CharField(max_length=160, db_index=True)
    product_sku = models.CharField(max_length=128, blank=True, default='')
    meter_key = models.CharField(max_length=128, db_index=True)
    meter_version = models.PositiveIntegerField(default=1)
    unit = models.CharField(max_length=64)
    dimensions = models.JSONField(default=_json_dict, blank=True)
    dimensions_hash = models.CharField(max_length=64, blank=True, default='')
    gross_quantity = models.DecimalField(max_digits=28, decimal_places=6, default=0)
    included_quantity = models.DecimalField(max_digits=28, decimal_places=6, default=0)
    billable_quantity = models.DecimalField(max_digits=28, decimal_places=6, default=0)
    rate_version = models.ForeignKey(
        RateVersion,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='rated_lines',
    )
    unit_rate_inr_micros = models.BigIntegerField(default=0)
    customer_gross_rated_debit_inr_micros = models.BigIntegerField(default=0)
    promotion_facts = models.JSONField(default=_json_dict, blank=True)
    allocation_facts = models.JSONField(default=_json_dict, blank=True)
    generation = models.BigIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    metadata = models.JSONField(default=_json_dict, blank=True)

    class Meta:
        ordering = ['line_key']
        constraints = [
            models.UniqueConstraint(
                fields=['statement', 'line_key', 'generation'],
                name='uniq_rated_usage_line_statement_key_gen',
            ),
        ]
        indexes = [
            models.Index(fields=['billing_account', 'meter_key']),
        ]

    def __str__(self):
        return (
            f'RatedUsageLine({self.line_key} gross={self.gross_quantity} '
            f'billable={self.billable_quantity})'
        )


class SpendAllocation(ImmutableModel):
    """Immutable spend-allocation identity for Books funding applications (HLD §8).

    Unique (source_books_posting_or_lot_slice, spend_allocation_id). Cumulative
    applied micros cannot exceed the source slice (enforced in application logic).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    spend_allocation_id = models.CharField(max_length=128, db_index=True)
    source_books_posting_or_lot_slice = models.CharField(max_length=255, db_index=True)
    statement = models.ForeignKey(
        BillingStatement,
        on_delete=models.PROTECT,
        related_name='spend_allocations',
    )
    statement_revision = models.PositiveIntegerField(default=1)
    line_key = models.CharField(max_length=160, blank=True, default='')
    amount_inr_micros = models.BigIntegerField(default=0)
    applied_inr_micros = models.BigIntegerField(default=0)
    currency = models.CharField(max_length=3, default='INR')
    gateway_merchant_account = models.CharField(max_length=128, blank=True, default='')
    canonical_hash = models.CharField(max_length=64, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    metadata = models.JSONField(default=_json_dict, blank=True)

    class Meta:
        ordering = ['created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['source_books_posting_or_lot_slice', 'spend_allocation_id'],
                name='uniq_spend_allocation_source_slice_id',
            ),
        ]

    def __str__(self):
        return f'SpendAllocation({self.spend_allocation_id} {self.amount_inr_micros})'
