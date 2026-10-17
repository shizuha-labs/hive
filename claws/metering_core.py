"""Metering Core services — BillingAccount, payer-link authority, genesis, catalog.

Implements PLAT-4481 (identity/payer) and PLAT-4483 (versioned catalog) against
HLD b131fbff v44 / ADR-0032 v15 / ADR-0022 v15.

Invariants enforced here (HLD §10):
- No client-selected payer: resource registry + grants derive BillingAccount.
- Payer-link activation is two-sided, purpose-bound AAL2, role-generation checked.
- Genesis creates only proven self-payer rows; ambiguity is quarantined.
- Revocation bumps admission_epoch; old proofs remain historically valid.
- Catalog versions are immutable once written; free-v1 is configuration.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
import hashlib
import json
import uuid
from typing import Any, Iterable, Mapping, Optional, Sequence

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, Max
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .metering_catalog import (
    DEFAULT_SELLER,
    FREE_V1_METERS,
    FREE_V1_MONTHLY_GRANTS,
    FREE_V1_PLAN,
    STARTER_999_V1_RC1,
    catalog_snapshot,
)
from .metering_events import (
    authority_snapshot_sha256,
    register_admission_authority_snapshot,
)
from .models import (
    BillingAccount,
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
    RateVersion,
    ResourcePayerBindingVersion,
)


PURPOSE_MONEY_MOVEMENT = 'money-movement-reauth-v1'
DEFAULT_BUDGET_POLICY = 'budget-policy-v1'
SELF_PAYER_BUDGET_POLICY = 'self-payer-genesis-v1'


class MeteringCoreError(ValueError):
    code = 'metering_core_error'

    def __init__(self, detail: str, *, code: str | None = None):
        super().__init__(detail)
        self.detail = detail
        if code:
            self.code = code

    def as_dict(self):
        return {'error': self.code, 'detail': self.detail}


class ConsentError(MeteringCoreError):
    code = 'consent_error'


class GenesisError(MeteringCoreError):
    code = 'genesis_error'


class CatalogError(MeteringCoreError):
    code = 'catalog_error'


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), default=str).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _require_aal2(aal: str) -> str:
    value = (aal or '').strip().lower()
    if value not in {'aal2', 'aal3'}:
        raise ConsentError('Payer-link consent requires AAL2 or higher.')
    return value


def _parse_ts(value):
    if value is None:
        return None
    if hasattr(value, 'isoformat') and not isinstance(value, str):
        parsed = value
    else:
        parsed = parse_datetime(str(value))
        if parsed is None:
            raise MeteringCoreError(f'Invalid timestamp: {value!r}')
    now = timezone.now()
    if timezone.is_naive(parsed) and timezone.is_aware(now):
        try:
            parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
        except Exception:
            parsed = timezone.make_aware(parsed)
    elif timezone.is_aware(parsed) and timezone.is_naive(now):
        parsed = timezone.make_naive(parsed, timezone.utc)
    return parsed


def _jsonable(value):
    """Recursively convert Decimals for JSONField storage."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# BillingAccount
# ---------------------------------------------------------------------------


@transaction.atomic
def create_billing_account(
    *,
    account_type: str,
    display_name: str = '',
    owner_user_id: Optional[int] = None,
    owner_org_id: str = '',
    owner_subject: str = '',  # convenience alias → stored in metadata + derived fields
    organization_id: str = '',  # alias for owner_org_id
    currency: str = 'INR',
    monthly_anchor_day: int = 1,
    metadata: Optional[Mapping[str, Any]] = None,
) -> BillingAccount:
    if account_type not in BillingAccount.AccountType.values:
        raise MeteringCoreError(f'Unknown account_type={account_type!r}')
    if currency != 'INR':
        raise MeteringCoreError('R1A BillingAccount currency must be INR.')

    org_id = owner_org_id or organization_id or ''
    user_id = owner_user_id
    meta = dict(metadata or {})
    if owner_subject:
        meta.setdefault('owner_subject', owner_subject)
        if user_id is None and owner_subject.startswith('user:'):
            # optional numeric suffix: user:42
            tail = owner_subject.split(':', 1)[-1]
            if tail.isdigit():
                user_id = int(tail)
        if not org_id and owner_subject.startswith('org:'):
            org_id = owner_subject.split(':', 1)[-1]

    if account_type == BillingAccount.AccountType.INDIVIDUAL:
        if user_id is None and not meta.get('owner_subject'):
            raise MeteringCoreError('individual BillingAccount requires owner_user_id or owner_subject.')
    if account_type == BillingAccount.AccountType.ORGANIZATION:
        if not org_id:
            raise MeteringCoreError('organization BillingAccount requires owner_org_id.')

    return BillingAccount.objects.create(
        account_type=account_type,
        display_name=display_name or meta.get('owner_subject') or org_id or str(user_id or ''),
        owner_user_id=user_id,
        owner_org_id=org_id,
        currency=currency,
        monthly_anchor_day=monthly_anchor_day,
        metadata=meta,
    )


def get_billing_account(account_id: uuid.UUID | str) -> BillingAccount:
    return BillingAccount.objects.get(id=account_id)


@transaction.atomic
def resolve_default_account(
    *,
    account_type: str,
    owner_user_id: Optional[int] = None,
    owner_org_id: str = '',
    display_name: str = '',
) -> BillingAccount:
    """Server-derived get-or-create of an owner's DEFAULT BillingAccount.

    R1B (PLAT-7006): producers must never derive or choose a payer themselves
    (HLD §2/§2.1) — they name the owner subject and the core resolves the
    canonical default account.  The default is the OLDEST existing account for
    that owner (deterministic when legacy accounts exist); a new default is
    created only when none exists.

    Race safety uses a transaction-scoped Postgres advisory lock keyed on the
    owner identity, so two concurrent resolves cannot create two accounts for
    one owner (a split-payer correctness bug).  No schema change required.
    """
    if account_type not in BillingAccount.AccountType.values:
        raise MeteringCoreError(f'Unknown account_type={account_type!r}')
    if account_type == BillingAccount.AccountType.INDIVIDUAL:
        if owner_user_id is None:
            raise MeteringCoreError('individual account resolve requires owner_user_id.')
        if owner_org_id:
            raise MeteringCoreError('individual account resolve must not carry owner_org_id.')
        owner_key = f'individual:user:{int(owner_user_id)}'
    else:
        if not owner_org_id:
            raise MeteringCoreError('organization account resolve requires owner_org_id.')
        if owner_user_id is not None:
            raise MeteringCoreError('organization account resolve must not carry owner_user_id.')
        owner_key = f'organization:org:{str(owner_org_id)}'

    with transaction.get_connection().cursor() as cursor:
        cursor.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (owner_key,))

    existing = (
        BillingAccount.objects.filter(
            account_type=account_type,
            **(
                {'owner_user_id': int(owner_user_id)}
                if account_type == BillingAccount.AccountType.INDIVIDUAL
                else {'owner_org_id': str(owner_org_id)}
            ),
        )
        .order_by('created_at', 'id')
        .first()
    )
    if existing is not None:
        return existing
    return create_billing_account(
        account_type=account_type,
        owner_user_id=owner_user_id,
        owner_org_id=owner_org_id,
        display_name=display_name,
    )


def _account_subject(account: BillingAccount) -> str:
    meta = account.metadata or {}
    if meta.get('owner_subject'):
        return str(meta['owner_subject'])
    if account.owner_user_id is not None:
        return f'user:{account.owner_user_id}'
    if account.owner_org_id:
        return f'org:{account.owner_org_id}'
    return str(account.id)


@transaction.atomic
def transition_collection_status(
    account: BillingAccount,
    *,
    to_status: str,
    reason: str = '',
) -> BillingAccount:
    # Accept legacy aliases used in early drafts.
    aliases = {
        'ok': BillingAccount.CollectionStatus.ACTIVE,
        'suspended': BillingAccount.CollectionStatus.BARRED,
    }
    to_status = aliases.get(to_status, to_status)
    try:
        account.transition_collection_status(to_status)
    except ValidationError as exc:
        raise MeteringCoreError(str(exc.message if hasattr(exc, 'message') else exc)) from exc
    if reason:
        meta = dict(account.metadata or {})
        history = list(meta.get('collection_history') or [])
        history.append({'to': to_status, 'reason': reason, 'at': timezone.now().isoformat()})
        meta['collection_history'] = history[-50:]
        BillingAccount.objects.filter(pk=account.pk).update(metadata=meta)
        account.metadata = meta
    return account


@transaction.atomic
def bump_admission_epoch(account: BillingAccount, *, reason: str = '') -> BillingAccount:
    account.bump_admission_epoch(reason=reason)
    account.refresh_from_db()
    return account


@transaction.atomic
def bump_policy_generation(account: BillingAccount, *, reason: str = '') -> BillingAccount:
    account.bump_policy_generation(reason=reason)
    account.refresh_from_db()
    return account


@transaction.atomic
def open_postpaid_facility(
    account: BillingAccount,
    *,
    credit_limit_inr_micros: int,
    seller_legal_entity_id: str = '',
    books_organization_id: str = '',
    seller_gst_registration_id: str = '',
    gateway_merchant_account: str = '',
    terms_ref: str = '',
    metadata: Optional[Mapping[str, Any]] = None,
) -> PostpaidFacility:
    if credit_limit_inr_micros < 0:
        raise MeteringCoreError('credit_limit_inr_micros must be >= 0')
    now = timezone.now()
    seller = {
        'seller_legal_entity_id': seller_legal_entity_id or DEFAULT_SELLER['seller_legal_entity_id'],
        'seller_gst_registration_id': seller_gst_registration_id
        or DEFAULT_SELLER['seller_gst_registration_id'],
        'books_organization_id': books_organization_id or DEFAULT_SELLER['books_organization_id'],
        'gateway_merchant_account': gateway_merchant_account
        or DEFAULT_SELLER['gateway_merchant_account'],
    }
    canonical = {
        'billing_account_id': str(account.id),
        'limit_inr_micros': credit_limit_inr_micros,
        **seller,
        'currency': 'INR',
        'facility_version': 1,
        'effective_start': now.isoformat(),
    }
    return PostpaidFacility.objects.create(
        billing_account=account,
        limit_inr_micros=credit_limit_inr_micros,
        seller_legal_entity_id=seller['seller_legal_entity_id'],
        seller_gst_registration_id=seller['seller_gst_registration_id'],
        books_organization_id=seller['books_organization_id'],
        gateway_merchant_account=seller['gateway_merchant_account'],
        currency='INR',
        status=PostpaidFacility.Status.ACTIVE,
        effective_start=now,
        facility_version=1,
        canonical_hash=_canonical_sha256(canonical),
        metadata={**(metadata or {}), 'terms_ref': terms_ref},
    )


# ---------------------------------------------------------------------------
# Payer link: proposal → dual consent → grant → binding
# ---------------------------------------------------------------------------


@transaction.atomic
def create_payer_link_proposal(
    *,
    billing_account: BillingAccount,
    scope_kind: str,
    scope_id: str,
    proposer_subject: str = '',
    resource_owner_subject: str = '',
    billing_admin_subject: str = '',
    resource_owner_role_generation: int = 1,
    billing_admin_role_generation: int = 1,
    proposer_role_generation: int = 1,
    allowed_resource_types: Optional[Sequence[str]] = None,
    budget_policy_version: str = DEFAULT_BUDGET_POLICY,
    budget_hard_cap_inr_micros: Optional[int] = None,
    no_additional_cap: bool = False,
    interval_start=None,
    interval_end=None,
    ttl_seconds: int = 900,
    purpose: str = PURPOSE_MONEY_MOVEMENT,
    idempotency_key: str = '',
    expected_binding_version: Optional[int] = None,
    expected_scope_version: Optional[int] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> PayerLinkProposal:
    if scope_kind not in PayerLinkProposal.ScopeKind.values:
        raise MeteringCoreError(f'Unknown scope_kind={scope_kind!r}')
    if purpose != PURPOSE_MONEY_MOVEMENT:
        raise ConsentError(f'Unsupported purpose={purpose!r}; required {PURPOSE_MONEY_MOVEMENT}')
    if billing_account.collection_status == BillingAccount.CollectionStatus.CLOSED:
        raise MeteringCoreError('Cannot propose payer link on a closed BillingAccount.')

    now = timezone.now()
    interval_start = interval_start or now
    interval_end = interval_end or (now + timedelta(days=365))
    if interval_end <= interval_start:
        raise MeteringCoreError('interval_end must be after interval_start')

    owner_subject = resource_owner_subject or proposer_subject or _account_subject(billing_account)
    admin_subject = billing_admin_subject or owner_subject
    proposer = proposer_subject or owner_subject
    key = idempotency_key or f'plp:{billing_account.id}:{scope_kind}:{scope_id}:{uuid.uuid4().hex[:12]}'

    existing = PayerLinkProposal.objects.filter(idempotency_key=key).first()
    if existing is not None:
        return existing

    meta = dict(metadata or {})
    meta['resource_owner_subject'] = owner_subject
    meta['billing_admin_subject'] = admin_subject
    meta['resource_owner_role_generation'] = int(resource_owner_role_generation)
    meta['billing_admin_role_generation'] = int(billing_admin_role_generation)
    meta['expires_at'] = (now + timedelta(seconds=ttl_seconds)).isoformat()

    proposal = PayerLinkProposal(
        target_billing_account=billing_account,
        scope_kind=scope_kind,
        scope_id=scope_id,
        allowed_resource_types=list(allowed_resource_types or []),
        budget_policy_version=budget_policy_version,
        budget_hard_cap_inr_micros=budget_hard_cap_inr_micros,
        no_additional_cap=no_additional_cap,
        interval_start=interval_start,
        interval_end=interval_end,
        expected_binding_version=expected_binding_version,
        expected_scope_version=expected_scope_version,
        purpose=purpose,
        idempotency_key=key,
        proposer_subject=proposer,
        proposer_role_generation=int(proposer_role_generation),
        metadata=meta,
        canonical_hash='pending',
    )
    proposal.canonical_hash = _canonical_sha256(proposal.payload_for_hash())
    proposal.save()
    return proposal


def _proposal_expired(proposal: PayerLinkProposal) -> bool:
    expires = (proposal.metadata or {}).get('expires_at')
    if not expires:
        return False
    ts = _parse_ts(expires)
    return ts is not None and ts <= timezone.now()


@transaction.atomic
def record_payer_link_consent(
    proposal: PayerLinkProposal,
    *,
    role: str,
    subject: str,
    role_generation: int,
    action_grant_id: str,
    auth_time=None,
    aal: str = 'aal2',
    metadata: Optional[Mapping[str, Any]] = None,
) -> PayerLinkConsent:
    proposal = PayerLinkProposal.objects.select_for_update().get(pk=proposal.pk)
    if proposal.status != PayerLinkProposal.Status.PENDING:
        raise ConsentError(f'Proposal is {proposal.status}, not pending.')
    if _proposal_expired(proposal):
        proposal.status = PayerLinkProposal.Status.EXPIRED
        proposal.save(update_fields=['status'])
        raise ConsentError('Proposal has expired.')
    if role not in PayerLinkConsent.Role.values:
        raise ConsentError(f'Unknown consent role={role!r}')
    aal = _require_aal2(aal)
    if not action_grant_id or not str(action_grant_id).strip():
        raise ConsentError('action_grant_id is required (one-time, non-replayable).')

    meta = proposal.metadata or {}
    if role == PayerLinkConsent.Role.RESOURCE_OWNER:
        expected_subject = meta.get('resource_owner_subject') or proposal.proposer_subject
        expected_gen = int(meta.get('resource_owner_role_generation') or proposal.proposer_role_generation)
    else:
        expected_subject = meta.get('billing_admin_subject') or proposal.proposer_subject
        expected_gen = int(meta.get('billing_admin_role_generation') or proposal.proposer_role_generation)

    if subject != expected_subject:
        raise ConsentError(f'{role} subject does not match proposal expectation.')
    if int(role_generation) != expected_gen:
        raise ConsentError(f'{role} role_generation mismatch (stale authority).')

    # Global replay protection on action_grant_id (one-time credential).
    if PayerLinkConsent.objects.filter(action_grant_id=str(action_grant_id).strip()).exists():
        raise ConsentError('action_grant_id has already been consumed (consent replay).')

    try:
        consent = PayerLinkConsent.objects.create(
            proposal=proposal,
            role=role,
            subject=subject,
            role_generation=int(role_generation),
            proposal_canonical_hash=proposal.canonical_hash,
            aal=aal,
            auth_time=auth_time or timezone.now(),
            action_grant_id=str(action_grant_id).strip(),
            purpose=proposal.purpose,
            metadata=dict(metadata or {}),
        )
    except IntegrityError as exc:
        raise ConsentError('Consent conflict (duplicate action_grant or role).') from exc
    return consent


def _consents_ready(proposal: PayerLinkProposal) -> tuple[PayerLinkConsent, PayerLinkConsent]:
    owner = (
        proposal.consents.filter(role=PayerLinkConsent.Role.RESOURCE_OWNER)
        .order_by('-created_at')
        .first()
    )
    admin = (
        proposal.consents.filter(role=PayerLinkConsent.Role.BILLING_ADMIN)
        .order_by('-created_at')
        .first()
    )
    if owner is None or admin is None:
        raise ConsentError('Both resource_owner and billing_admin consents are required.')
    if owner.proposal_canonical_hash != proposal.canonical_hash:
        raise ConsentError('resource_owner consent hash mismatch.')
    if admin.proposal_canonical_hash != proposal.canonical_hash:
        raise ConsentError('billing_admin consent hash mismatch.')
    if owner.purpose != PURPOSE_MONEY_MOVEMENT or admin.purpose != PURPOSE_MONEY_MOVEMENT:
        raise ConsentError('Consent purpose must be money-movement-reauth-v1.')
    return owner, admin


@transaction.atomic
def activate_payer_link_grant(
    proposal: PayerLinkProposal,
    *,
    grant_id: Optional[uuid.UUID] = None,
) -> PayerLinkGrantVersion:
    proposal = PayerLinkProposal.objects.select_for_update().get(pk=proposal.pk)
    if proposal.status == PayerLinkProposal.Status.ACTIVATED:
        existing = proposal.grant_versions.order_by('-version').first()
        if existing is not None:
            return existing
    if proposal.status != PayerLinkProposal.Status.PENDING:
        raise ConsentError(f'Cannot activate proposal in status={proposal.status}')
    if _proposal_expired(proposal):
        proposal.status = PayerLinkProposal.Status.EXPIRED
        proposal.save(update_fields=['status'])
        raise ConsentError('Proposal expired before activation.')

    owner, admin = _consents_ready(proposal)
    account = BillingAccount.objects.select_for_update().get(pk=proposal.target_billing_account_id)
    grant_id = grant_id or uuid.uuid4()
    prior = (
        PayerLinkGrantVersion.objects.filter(grant_id=grant_id)
        .order_by('-version')
        .first()
    )
    version = (prior.version + 1) if prior else 1
    if prior and prior.status == PayerLinkGrantVersion.Status.ACTIVE:
        prior.status = PayerLinkGrantVersion.Status.SUPERSEDED
        prior.save(update_fields=['status'])

    canonical = {
        'grant_id': str(grant_id),
        'version': version,
        'proposal_id': str(proposal.id),
        'billing_account_id': str(account.id),
        'scope_kind': proposal.scope_kind,
        'scope_id': proposal.scope_id,
        'allowed_resource_types': list(proposal.allowed_resource_types or []),
        'budget_policy_version': proposal.budget_policy_version,
        'budget_hard_cap_inr_micros': proposal.budget_hard_cap_inr_micros,
        'no_additional_cap': proposal.no_additional_cap,
        'interval_start': proposal.interval_start.isoformat(),
        'interval_end': proposal.interval_end.isoformat(),
        'resource_owner_consent_id': str(owner.id),
        'billing_admin_consent_id': str(admin.id),
    }
    grant = PayerLinkGrantVersion.objects.create(
        grant_id=grant_id,
        version=version,
        proposal=proposal,
        billing_account=account,
        scope_kind=proposal.scope_kind,
        scope_id=proposal.scope_id,
        allowed_resource_types=list(proposal.allowed_resource_types or []),
        budget_policy_version=proposal.budget_policy_version,
        budget_hard_cap_inr_micros=proposal.budget_hard_cap_inr_micros,
        no_additional_cap=proposal.no_additional_cap,
        interval_start=proposal.interval_start,
        interval_end=proposal.interval_end,
        canonical_hash=_canonical_sha256(canonical),
        status=PayerLinkGrantVersion.Status.ACTIVE,
        resource_owner_consent=owner,
        billing_admin_consent=admin,
        admission_epoch_at_activation=account.admission_epoch,
    )
    proposal.status = PayerLinkProposal.Status.ACTIVATED
    proposal.save(update_fields=['status'])
    return grant


@transaction.atomic
def materialize_resource_payer_binding(
    *,
    grant: PayerLinkGrantVersion,
    resource_uid: str,
    resource_type: str,
) -> ResourcePayerBindingVersion:
    if grant.status != PayerLinkGrantVersion.Status.ACTIVE:
        raise MeteringCoreError('Cannot bind from a non-active grant.')
    allowed = list(grant.allowed_resource_types or [])
    if allowed and resource_type not in allowed:
        raise MeteringCoreError(
            f'resource_type={resource_type!r} not in grant allowed_resource_types={allowed}'
        )
    now = timezone.now()
    if not (grant.interval_start <= now < grant.interval_end):
        raise MeteringCoreError('Grant interval does not cover now; cannot bind.')

    account = BillingAccount.objects.select_for_update().get(pk=grant.billing_account_id)
    max_ver = (
        ResourcePayerBindingVersion.objects.filter(resource_uid=resource_uid)
        .aggregate(m=Max('binding_version'))
        .get('m')
        or 0
    )
    binding_version = max_ver + 1
    ResourcePayerBindingVersion.objects.filter(
        resource_uid=resource_uid, status='active'
    ).update(status='superseded')

    canonical = {
        'resource_uid': resource_uid,
        'resource_type': resource_type,
        'binding_version': binding_version,
        'grant_version_id': str(grant.id),
        'billing_account_id': str(account.id),
        'admission_epoch': account.admission_epoch,
        'budget_policy_version': grant.budget_policy_version,
        'interval_start': grant.interval_start.isoformat(),
        'interval_end': grant.interval_end.isoformat(),
    }
    binding = ResourcePayerBindingVersion.objects.create(
        resource_uid=resource_uid,
        resource_type=resource_type,
        binding_version=binding_version,
        grant_version=grant,
        billing_account=account,
        admission_epoch=account.admission_epoch,
        budget_policy_version=grant.budget_policy_version,
        interval_start=grant.interval_start,
        interval_end=grant.interval_end,
        canonical_hash=_canonical_sha256(canonical),
        status='active',
    )
    CurrentResourcePayer.objects.update_or_create(
        resource_uid=resource_uid,
        defaults={
            'resource_type': resource_type,
            'billing_account': account,
            'grant_version': grant,
            'binding_version': binding,
            'admission_epoch': account.admission_epoch,
        },
    )
    return binding


@transaction.atomic
def revoke_payer_link_grant(
    grant: PayerLinkGrantVersion,
    *,
    reason: str = '',
) -> PayerLinkGrantVersion:
    grant = PayerLinkGrantVersion.objects.select_for_update().get(pk=grant.pk)
    if grant.status != PayerLinkGrantVersion.Status.ACTIVE:
        raise MeteringCoreError(f'Grant is {grant.status}, not active.')
    now = timezone.now()
    grant.status = PayerLinkGrantVersion.Status.REVOKED
    grant.revoked_at = now
    meta = dict(grant.metadata or {})
    meta['revoke_reason'] = reason
    grant.metadata = meta
    grant.save(update_fields=['status', 'revoked_at', 'metadata'])

    ResourcePayerBindingVersion.objects.filter(
        grant_version=grant, status='active'
    ).update(status='revoked')
    CurrentResourcePayer.objects.filter(grant_version=grant).delete()

    bump_admission_epoch(grant.billing_account, reason=f'revoke_grant:{grant.id}:{reason}')
    return grant


def rebuild_current_resource_payer(resource_uid: str) -> Optional[CurrentResourcePayer]:
    binding = (
        ResourcePayerBindingVersion.objects.filter(resource_uid=resource_uid, status='active')
        .select_related('grant_version', 'billing_account')
        .order_by('-binding_version')
        .first()
    )
    if binding is None:
        CurrentResourcePayer.objects.filter(resource_uid=resource_uid).delete()
        return None
    if binding.grant_version.status != PayerLinkGrantVersion.Status.ACTIVE:
        CurrentResourcePayer.objects.filter(resource_uid=resource_uid).delete()
        return None
    obj, _ = CurrentResourcePayer.objects.update_or_create(
        resource_uid=resource_uid,
        defaults={
            'resource_type': binding.resource_type,
            'billing_account': binding.billing_account,
            'grant_version': binding.grant_version,
            'binding_version': binding,
            'admission_epoch': binding.admission_epoch,
        },
    )
    return obj


# ---------------------------------------------------------------------------
# PayerAuthorityProof issuance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IssuedAuthority:
    proof: PayerAuthorityProof
    admission_snapshot: object


@transaction.atomic
def issue_payer_authority_proof(
    *,
    resource_uid: str,
    reservation_id: str = '',
    lease_id: str = '',
    operation_hash: str = '',
    authorized_max_inr_micros: Optional[int] = None,
    valid_for_seconds: int = 3600,
    seller_legal_entity_id: str = '',
    seller_gst_registration_id: str = '',
    books_organization_id: str = '',
    gateway_merchant_account: str = '',
) -> IssuedAuthority:
    current = (
        CurrentResourcePayer.objects.select_related(
            'billing_account', 'grant_version', 'binding_version'
        )
        .filter(resource_uid=resource_uid)
        .first()
    )
    if current is None:
        raise MeteringCoreError(
            f'No current payer binding for resource_uid={resource_uid!r}',
            code='no_current_payer',
        )
    grant = current.grant_version
    binding = current.binding_version
    account = current.billing_account
    if grant.status != PayerLinkGrantVersion.Status.ACTIVE:
        raise MeteringCoreError('Current grant is not active.')
    if binding.status != 'active':
        raise MeteringCoreError('Current binding is not active.')
    if account.admission_epoch != binding.admission_epoch:
        raise MeteringCoreError(
            'Binding admission_epoch is stale relative to BillingAccount; rebind required.',
            code='stale_binding_epoch',
        )
    if account.collection_status in {
        BillingAccount.CollectionStatus.BARRED,
        BillingAccount.CollectionStatus.CLOSED,
    }:
        raise MeteringCoreError(
            f'BillingAccount collection_status={account.collection_status} blocks admission.'
        )

    now = timezone.now()
    if not (grant.interval_start <= now < grant.interval_end):
        raise MeteringCoreError('Grant interval does not cover proof issuance time.')

    op_hash = operation_hash or _canonical_sha256(
        {
            'resource_uid': resource_uid,
            'reservation_id': reservation_id,
            'lease_id': lease_id,
            'at': now.isoformat(),
        }
    )
    proof_id = 'pap:' + hashlib.sha256(
        f'{resource_uid}\0{reservation_id}\0{lease_id}\0{op_hash}\0{binding.binding_version}'.encode()
    ).hexdigest()[:40]

    seller = {
        'seller_legal_entity_id': seller_legal_entity_id or DEFAULT_SELLER['seller_legal_entity_id'],
        'seller_gst_registration_id': seller_gst_registration_id
        or DEFAULT_SELLER['seller_gst_registration_id'],
        'books_organization_id': books_organization_id or DEFAULT_SELLER['books_organization_id'],
        'gateway_merchant_account': gateway_merchant_account
        or DEFAULT_SELLER['gateway_merchant_account'],
    }
    claims = {
        'billing_account_id': str(account.id),
        'reservation_id': reservation_id or '',
        'lease_id': lease_id or '',
        'payer_link_generation': int(grant.version),
        'binding_generation': int(binding.binding_version),
        'admission_generation': int(account.admission_epoch),
    }
    proof_sha = authority_snapshot_sha256(claims)
    snapshot_body = {
        **claims,
        'proof_id': proof_id,
        'proof_sha256': proof_sha,
        'resource_uid': resource_uid,
        'grant_version_id': str(grant.id),
        'binding_version_id': str(binding.id),
        'budget_policy_version': grant.budget_policy_version,
        'operation_hash': op_hash,
        **seller,
    }

    admission = register_admission_authority_snapshot(
        {
            **claims,
            'proof_id': proof_id,
            'proof_sha256': proof_sha,
        }
    )

    try:
        proof = PayerAuthorityProof.objects.create(
            proof_id=proof_id,
            proof_sha256=proof_sha,
            resource_uid=resource_uid,
            billing_account=account,
            grant_version=grant,
            binding_version=binding,
            budget_policy_version=grant.budget_policy_version,
            admission_epoch=account.admission_epoch,
            reservation_id=reservation_id or '',
            lease_id=lease_id or '',
            seller_legal_entity_id=seller['seller_legal_entity_id'],
            seller_gst_registration_id=seller['seller_gst_registration_id'],
            books_organization_id=seller['books_organization_id'],
            gateway_merchant_account=seller['gateway_merchant_account'],
            currency='INR',
            authorized_max_inr_micros=authorized_max_inr_micros,
            operation_hash=op_hash,
            valid_from=now,
            valid_until=now + timedelta(seconds=valid_for_seconds),
            snapshot=snapshot_body,
        )
    except IntegrityError:
        proof = PayerAuthorityProof.objects.get(proof_id=proof_id)
        if proof.proof_sha256 != proof_sha:
            raise MeteringCoreError('proof_id collision with different claims.') from None

    return IssuedAuthority(proof=proof, admission_snapshot=admission)


# ---------------------------------------------------------------------------
# Payer genesis
# ---------------------------------------------------------------------------


def _ownership_is_unambiguous(evidence: Mapping[str, Any]) -> tuple[bool, str]:
    owners = evidence.get('owner_subjects') or evidence.get('owners') or []
    if isinstance(owners, str):
        owners = [owners]
    owners = [str(o).strip() for o in owners if str(o).strip()]
    self_subject = str(evidence.get('self_subject') or evidence.get('account_owner_subject') or '').strip()
    confidence = evidence.get('confidence')
    if not self_subject:
        return False, 'missing self_subject'
    if not owners:
        return False, 'missing owner_subjects'
    unique_owners = sorted(set(owners))
    if len(unique_owners) != 1:
        return False, f'ambiguous owners={unique_owners}'
    if unique_owners[0] != self_subject:
        return False, f'owner {unique_owners[0]!r} != self_subject {self_subject!r}'
    if confidence is not None and float(confidence) < 1.0:
        return False, f'confidence {confidence} < 1.0'
    if evidence.get('shared') or evidence.get('delegated') or evidence.get('org_shared'):
        return False, 'shared/delegated ownership requires dual consent'
    return True, ''


@transaction.atomic
def create_payer_genesis_manifest(
    *,
    source_system: str,
    source_schema_version: str,
    source_watermark: str,
    writer_authority_generation: int,
    signature: str = '',
    metadata: Optional[Mapping[str, Any]] = None,
) -> PayerGenesisManifest:
    canonical = {
        'source_system': source_system,
        'source_schema_version': source_schema_version,
        'source_watermark': source_watermark,
        'writer_authority_generation': int(writer_authority_generation),
        'signature': signature or '',
    }
    return PayerGenesisManifest.objects.create(
        source_system=source_system,
        source_schema_version=source_schema_version,
        source_watermark=source_watermark,
        writer_authority_generation=int(writer_authority_generation),
        signature=signature or '',
        canonical_hash=_canonical_sha256(canonical),
        metadata=dict(metadata or {}),
    )


@transaction.atomic
def import_payer_genesis_rows(
    manifest: PayerGenesisManifest,
    rows: Iterable[Mapping[str, Any]],
) -> PayerGenesisManifest:
    manifest = PayerGenesisManifest.objects.select_for_update().get(pk=manifest.pk)
    if manifest.status not in {
        PayerGenesisManifest.Status.PENDING,
        PayerGenesisManifest.Status.IMPORTING,
    }:
        raise GenesisError(f'Manifest status={manifest.status} is not importable.')
    manifest.status = PayerGenesisManifest.Status.IMPORTING
    manifest.save(update_fields=['status'])

    imported = 0
    quarantined = 0
    for raw in rows:
        source_object_id = str(raw['source_object_id'])
        source_object_version = str(raw.get('source_object_version') or '1')
        resource_uid = str(raw['resource_uid'])
        resource_type = str(raw['resource_type'])
        evidence = dict(raw.get('ownership_evidence') or {})
        source_digest = _canonical_sha256(
            {
                'source_object_id': source_object_id,
                'source_object_version': source_object_version,
                'resource_uid': resource_uid,
                'evidence': evidence,
            }
        )
        if PayerGenesisFact.objects.filter(
            manifest=manifest,
            source_object_id=source_object_id,
            source_object_version=source_object_version,
        ).exists():
            continue

        ok, reason = _ownership_is_unambiguous(evidence)
        account = None
        account_id = raw.get('billing_account_id')
        if account_id:
            try:
                account = BillingAccount.objects.get(pk=account_id)
            except (BillingAccount.DoesNotExist, ValidationError, ValueError):
                ok = False
                reason = f'unknown billing_account_id={account_id}'

        if not ok or account is None:
            PayerGenesisFact.objects.create(
                manifest=manifest,
                source_object_id=source_object_id,
                source_object_version=source_object_version,
                source_digest=source_digest,
                resource_uid=resource_uid,
                resource_type=resource_type,
                target_billing_account=account,
                decision=PayerGenesisFact.Decision.QUARANTINED,
                quarantine_reason=reason or 'unproven ownership',
                ownership_evidence=evidence,
            )
            quarantined += 1
            continue

        grant = _activate_self_payer_genesis_grant(
            account=account,
            resource_uid=resource_uid,
            resource_type=resource_type,
            manifest=manifest,
            source_digest=source_digest,
        )
        materialize_resource_payer_binding(
            grant=grant,
            resource_uid=resource_uid,
            resource_type=resource_type,
        )
        PayerGenesisFact.objects.create(
            manifest=manifest,
            source_object_id=source_object_id,
            source_object_version=source_object_version,
            source_digest=source_digest,
            resource_uid=resource_uid,
            resource_type=resource_type,
            target_billing_account=account,
            decision=PayerGenesisFact.Decision.SELF_PAYER,
            ownership_evidence=evidence,
            resulting_grant_version=grant,
        )
        imported += 1

    # Use plain ints — F() expressions + refresh is awkward after loop.
    manifest.imported_count = int(manifest.imported_count or 0) + imported
    manifest.quarantined_count = int(manifest.quarantined_count or 0) + quarantined
    manifest.status = PayerGenesisManifest.Status.COMPLETED
    manifest.completed_at = timezone.now()
    manifest.save(
        update_fields=['imported_count', 'quarantined_count', 'status', 'completed_at']
    )
    manifest.refresh_from_db()
    return manifest


def _activate_self_payer_genesis_grant(
    *,
    account: BillingAccount,
    resource_uid: str,
    resource_type: str,
    manifest: PayerGenesisManifest,
    source_digest: str,
) -> PayerLinkGrantVersion:
    now = timezone.now()
    subject = _account_subject(account)
    proposal = create_payer_link_proposal(
        billing_account=account,
        scope_kind=PayerLinkProposal.ScopeKind.RESOURCE,
        scope_id=resource_uid,
        resource_owner_subject=subject,
        billing_admin_subject=subject,
        resource_owner_role_generation=int(manifest.writer_authority_generation),
        billing_admin_role_generation=int(manifest.writer_authority_generation),
        allowed_resource_types=[resource_type],
        budget_policy_version=SELF_PAYER_BUDGET_POLICY,
        no_additional_cap=True,
        interval_start=now,
        interval_end=now + timedelta(days=3650),
        ttl_seconds=3600,
        idempotency_key=f'genesis:{manifest.id}:{resource_uid}:{source_digest[:16]}',
        metadata={
            'genesis_manifest_id': str(manifest.id),
            'source_digest': source_digest,
            'kind': 'self_payer_genesis',
        },
    )
    for role in (PayerLinkConsent.Role.RESOURCE_OWNER, PayerLinkConsent.Role.BILLING_ADMIN):
        record_payer_link_consent(
            proposal,
            role=role,
            subject=subject,
            role_generation=int(manifest.writer_authority_generation),
            action_grant_id=f'genesis:{manifest.id}:{resource_uid}:{role}:{source_digest[:16]}',
            aal='aal2',
            metadata={'genesis': True, 'manifest_id': str(manifest.id)},
        )
    return activate_payer_link_grant(proposal)


# ---------------------------------------------------------------------------
# Catalog seeding (PLAT-4483)
# ---------------------------------------------------------------------------


def _meter_canonical(spec: Mapping[str, Any], version: int) -> str:
    return _canonical_sha256(
        {
            'key': spec['key'],
            'version': version,
            'event_type': spec['event_type'],
            'value_selector': spec['value_selector'],
            'aggregation': spec['aggregation'],
            'canonical_unit': spec['canonical_unit'],
            'dimension_schema': spec.get('dimension_schema') or {},
            'window_semantics': spec.get('window_semantics') or {},
            'reset_semantics': spec.get('reset_semantics') or {},
            'late_arrival_semantics': spec.get('late_arrival_semantics') or {},
        }
    )


@transaction.atomic
def seed_meter_definition(spec: Mapping[str, Any], *, version: int = 1, effective_start=None) -> MeterDefinition:
    existing = MeterDefinition.objects.filter(key=spec['key'], version=version).first()
    if existing is not None:
        return existing
    now = effective_start or timezone.now()
    return MeterDefinition.objects.create(
        key=spec['key'],
        version=version,
        display_name=spec.get('display_name') or spec['key'],
        event_type=spec['event_type'],
        value_selector=spec['value_selector'],
        aggregation=spec.get('aggregation') or MeterDefinition.Aggregation.SUM,
        canonical_unit=spec['canonical_unit'],
        dimension_schema=dict(spec.get('dimension_schema') or {}),
        window_semantics=dict(spec.get('window_semantics') or {}),
        reset_semantics=dict(spec.get('reset_semantics') or {}),
        late_arrival_semantics=dict(spec.get('late_arrival_semantics') or {}),
        effective_start=now,
        canonical_hash=_meter_canonical(spec, version),
        metadata={'source': 'metering_catalog'},
    )


@transaction.atomic
def seed_plan_version(spec: Mapping[str, Any], *, version: int = 1, effective_start=None) -> PlanVersion:
    existing = PlanVersion.objects.filter(plan_key=spec['plan_key'], version=version).first()
    if existing is not None:
        return existing
    now = effective_start or timezone.now()
    seller = DEFAULT_SELLER
    canonical = {
        'plan_key': spec['plan_key'],
        'version': version,
        'advertised_price_inr_micros': int(spec.get('advertised_price_inr_micros') or 0),
        'components': _jsonable(list(spec.get('components') or [])),
        'caps': _jsonable(dict(spec.get('caps') or {})),
        'cadence': spec.get('cadence') or 'monthly',
        'currency': 'INR',
    }
    return PlanVersion.objects.create(
        plan_key=spec['plan_key'],
        version=version,
        display_name=spec.get('display_name') or spec['plan_key'],
        currency='INR',
        tax_inclusive=bool(spec.get('tax_inclusive', True)),
        advertised_price_inr_micros=int(spec.get('advertised_price_inr_micros') or 0),
        cadence=spec.get('cadence') or 'monthly',
        seller_legal_entity_id=seller['seller_legal_entity_id'],
        seller_gst_registration_id=seller['seller_gst_registration_id'],
        books_organization_id=seller['books_organization_id'],
        components=_jsonable(list(spec.get('components') or [])),
        caps=_jsonable(dict(spec.get('caps') or {})),
        effective_start=now,
        status=spec.get('status') or 'active',
        canonical_hash=_canonical_sha256(canonical),
        metadata={'source': 'metering_catalog', 'design_pin': catalog_snapshot()['design_pin']},
    )


@transaction.atomic
def seed_rate_version(
    *,
    meter: MeterDefinition,
    plan: Optional[PlanVersion] = None,
    rate_key: str,
    version: int = 1,
    inr_micros_per_unit: int = 0,
    unit: str = '',
    effective_start=None,
) -> RateVersion:
    existing = RateVersion.objects.filter(rate_key=rate_key, version=version).first()
    if existing is not None:
        return existing
    now = effective_start or timezone.now()
    seller = DEFAULT_SELLER
    unit = unit or meter.canonical_unit
    canonical = {
        'rate_key': rate_key,
        'version': version,
        'meter_key': meter.key,
        'meter_version': meter.version,
        'inr_micros_per_unit': inr_micros_per_unit,
        'unit': unit,
        'currency': 'INR',
    }
    return RateVersion.objects.create(
        rate_key=rate_key,
        version=version,
        meter_definition=meter,
        plan_version=plan,
        currency='INR',
        unit=unit,
        inr_micros_per_unit=inr_micros_per_unit,
        seller_legal_entity_id=seller['seller_legal_entity_id'],
        seller_gst_registration_id=seller['seller_gst_registration_id'],
        books_organization_id=seller['books_organization_id'],
        gateway_merchant_account=seller['gateway_merchant_account'],
        effective_start=now,
        canonical_hash=_canonical_sha256(canonical),
        metadata={'source': 'metering_catalog'},
    )


@transaction.atomic
def seed_entitlement_grant_template(
    *,
    plan: PlanVersion,
    meter: Optional[MeterDefinition],
    grant_spec: Mapping[str, Any],
    version: int = 1,
) -> EntitlementGrant:
    grant_key = grant_spec['grant_key']
    existing = EntitlementGrant.objects.filter(
        grant_key=grant_key,
        version=version,
        reset_generation=1,
        billing_account__isnull=True,
    ).first()
    if existing is not None:
        return existing
    amount = grant_spec['amount']
    if not isinstance(amount, Decimal):
        amount = Decimal(str(amount))
    canonical = {
        'grant_key': grant_key,
        'version': version,
        'plan_key': plan.plan_key,
        'plan_version': plan.version,
        'meter_key': meter.key if meter else '',
        'amount': str(amount),
        'unit': grant_spec['unit'],
        'priority': int(grant_spec.get('priority') or 100),
        'reset_generation': 1,
    }
    return EntitlementGrant.objects.create(
        grant_key=grant_key,
        version=version,
        billing_account=None,
        plan_version=plan,
        meter_definition=meter,
        amount=amount,
        unit=grant_spec['unit'],
        period='monthly',
        priority=int(grant_spec.get('priority') or 100),
        behavior=grant_spec.get('behavior') or EntitlementGrant.Behavior.HARD,
        reset_generation=1,
        canonical_hash=_canonical_sha256(canonical),
        metadata={
            'issuance_mode': grant_spec.get('issuance_mode') or 'fixed',
            'issued_unit': grant_spec.get('issued_unit') or grant_spec['unit'],
            'amount_notes': grant_spec.get('amount_notes') or '',
            'source': 'metering_catalog',
        },
    )


@transaction.atomic
def seed_free_v1_catalog(*, effective_start=None) -> dict:
    now = effective_start or timezone.now()
    meters = {}
    for spec in FREE_V1_METERS:
        meters[spec['key']] = seed_meter_definition(spec, version=1, effective_start=now)

    free_plan = seed_plan_version(FREE_V1_PLAN, version=1, effective_start=now)
    starter = seed_plan_version(STARTER_999_V1_RC1, version=1, effective_start=now)

    rates = []
    for key, meter in meters.items():
        rates.append(
            seed_rate_version(
                meter=meter,
                plan=free_plan,
                rate_key=f'free-v1.{key}',
                version=1,
                inr_micros_per_unit=0,
                unit=meter.canonical_unit,
                effective_start=now,
            )
        )

    grants = []
    for g in FREE_V1_MONTHLY_GRANTS:
        meter = meters[g['meter_key']]
        grants.append(
            seed_entitlement_grant_template(
                plan=free_plan, meter=meter, grant_spec=g, version=1
            )
        )

    return {
        'meters': meters,
        'free_plan': free_plan,
        'starter_plan': starter,
        'rates': rates,
        'grants': grants,
        'snapshot': catalog_snapshot()['design_pin'],
    }


@transaction.atomic
def issue_account_entitlement_from_template(
    *,
    template: EntitlementGrant,
    billing_account: BillingAccount,
    period_start=None,
    period_end=None,
    amount: Optional[Decimal] = None,
) -> tuple[EntitlementGrant, EntitlementBalance]:
    if template.billing_account_id is not None:
        raise CatalogError('Template grant must have null billing_account.')
    now = timezone.now()
    period_start = period_start or now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if period_end is None:
        if period_start.month == 12:
            period_end = period_start.replace(year=period_start.year + 1, month=1)
        else:
            period_end = period_start.replace(month=period_start.month + 1)

    issued_amount = amount if amount is not None else template.amount
    meta = dict(template.metadata or {})
    if meta.get('issuance_mode') == 'bytes_times_period_seconds' and amount is None:
        seconds = Decimal(str((period_end - period_start).total_seconds()))
        issued_amount = (template.amount * seconds).quantize(Decimal('0.000001'))
        unit = meta.get('issued_unit') or template.unit
    else:
        unit = meta.get('issued_unit') or template.unit

    prior = (
        EntitlementGrant.objects.filter(
            grant_key=template.grant_key,
            billing_account=billing_account,
        )
        .order_by('-reset_generation')
        .first()
    )
    reset_generation = (prior.reset_generation + 1) if prior else 1
    canonical = {
        'grant_key': template.grant_key,
        'version': template.version,
        'billing_account_id': str(billing_account.id),
        'reset_generation': reset_generation,
        'amount': str(issued_amount),
        'unit': unit,
    }
    grant = EntitlementGrant.objects.create(
        grant_key=template.grant_key,
        version=template.version,
        billing_account=billing_account,
        plan_version=template.plan_version,
        meter_definition=template.meter_definition,
        cap_key=template.cap_key,
        amount=issued_amount,
        unit=unit,
        period=template.period,
        reset_anchor=template.reset_anchor,
        reset_timezone=template.reset_timezone,
        priority=template.priority,
        rollover=template.rollover,
        behavior=template.behavior,
        start_at_utc=period_start,
        end_at_utc=period_end,
        reset_generation=reset_generation,
        canonical_hash=_canonical_sha256(canonical),
        metadata={**meta, 'template_id': str(template.id)},
    )
    balance = EntitlementBalance.objects.create(
        grant=grant,
        billing_account=billing_account,
        reset_generation=reset_generation,
        granted_amount=issued_amount,
        reserved_amount=Decimal('0'),
        consumed_amount=Decimal('0'),
        available_amount=issued_amount,
        period_start_utc=period_start,
        period_end_utc=period_end,
    )
    return grant, balance


@transaction.atomic
def consume_entitlement(
    balance: EntitlementBalance,
    *,
    amount: Decimal,
) -> EntitlementBalance:
    if amount <= 0:
        raise CatalogError('consume amount must be positive')
    balance = EntitlementBalance.objects.select_for_update().get(pk=balance.pk)
    remaining = balance.granted_amount - balance.reserved_amount - balance.consumed_amount
    if amount > remaining:
        raise CatalogError(f'insufficient entitlement remaining={remaining} need={amount}')
    balance.consumed_amount = balance.consumed_amount + amount
    balance.available_amount = (
        balance.granted_amount - balance.reserved_amount - balance.consumed_amount
    )
    balance.save(update_fields=['consumed_amount', 'available_amount', 'updated_at'])
    return balance


def ordered_entitlement_balances(billing_account: BillingAccount, *, meter_key: str = ''):
    qs = EntitlementBalance.objects.filter(billing_account=billing_account).select_related(
        'grant', 'grant__meter_definition'
    )
    if meter_key:
        qs = qs.filter(grant__meter_definition__key=meter_key)
    return sorted(qs, key=lambda b: (b.grant.priority, b.created_at))
