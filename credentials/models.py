"""HIVE-758 (SCLI-330): additive, flags-OFF credential-authority state schema.

First schema sub-slice for the Token/Credential Coordinator (ADR-0039 /
HLD 7052b163 §7.2). These are INERT additive state tables only — no reducer,
no enforcement, no authority flag is armed by this migration. Landing the
schema with v1 off is the HLD §7.13 rollout step; the exact-causal reducer,
use-fences, outbox/ACTIVE-tuple/receipt/event rows, replay delegation and the
status-mac-v2 crypto are separate follow-on sub-slices that build on these
tables.

Field semantics are taken verbatim from ADR-0039 (Accepted) + HLD v23 §7.2:
root facts and borrower ordinary health are separate; BorrowerCredentialSuppression
is grant-INDEPENDENT (never keyed by grant); CredentialRootState never stores
ok/rate_limited/refreshable_auth_failed (those are borrower-scope facts, not
root truth). No production credential is read/selected/mutated by this schema.
"""

import uuid

from django.db import models


class CredentialGeneration(models.Model):
    """Immutable root generation node (HLD §7.2).

    Ordinal is strictly monotonic per entry; parent must be the prior current
    generation; the chain digest commits the full immutable ancestry. Repair/
    replacement atomically creates N+1 and fences N. Ordinal alone never proves
    ancestry. Retained for the >=400-day exact-report/tombstone horizon.
    """

    generation_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    entry_id = models.CharField(max_length=255, db_index=True)
    generation_ordinal = models.BigIntegerField()
    parent_generation_id = models.UUIDField(null=True, blank=True)
    generation_chain_digest = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entry_id", "generation_ordinal"],
                name="uniq_credgen_entry_ordinal",
            ),
        ]
        indexes = [models.Index(fields=["entry_id", "generation_ordinal"])]


class CredentialRootState(models.Model):
    """Root-wide record (HLD §7.2). Never stores ordinary borrower health."""

    class RootHealth(models.TextChoices):
        UNKNOWN = "unknown"
        TERMINAL_DISABLED = "terminal_disabled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    entry_id = models.CharField(max_length=255, db_index=True)
    root_generation = models.ForeignKey(
        CredentialGeneration, on_delete=models.PROTECT, related_name="root_states"
    )
    root_health = models.CharField(
        max_length=32, choices=RootHealth.choices, default=RootHealth.UNKNOWN
    )
    terminal_reason = models.CharField(max_length=64, null=True, blank=True)
    current_artifact_id = models.CharField(max_length=255, null=True, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    # Increments on root terminal/repair, current-artifact, archive, admin
    # action and root-wide hold/lineage changes (HLD §7.2 two-level versioning).
    root_state_version = models.BigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entry_id", "root_generation"],
                name="uniq_rootstate_entry_gen",
            ),
        ]


class CredentialScopeState(models.Model):
    """Grant-versioned exact-scope ordinary health (HLD §7.2).

    Keyed by the exact causal scope. An absent row, or a row whose
    scope_artifact_id differs from the root's current artifact, composes as
    `unknown` with no cooldown; it is updated lazily by an outcome for the
    current artifact, never by fan-out.
    """

    class ScopeHealth(models.TextChoices):
        UNKNOWN = "unknown"
        OK = "ok"
        RATE_LIMITED = "rate_limited"
        REFRESHABLE_AUTH_FAILED = "refreshable_auth_failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    entry_id = models.CharField(max_length=255)
    root_generation = models.ForeignKey(
        CredentialGeneration, on_delete=models.PROTECT, related_name="scope_states"
    )
    credential_owner_id = models.CharField(max_length=255)
    borrower_owner_id = models.CharField(max_length=255)
    # Part of the exact-scope key. Empty string is the direct (no-fallback-grant)
    # scope sentinel. Must be non-null so PostgreSQL UNIQUE enforces a single
    # direct-scope row (NULL is distinct under default PG semantics; Django 4.2
    # has no NULLS NOT DISTINCT). See HIVE-758 P1 / HLD §7.2 exact causal scope.
    fallback_grant_id = models.CharField(max_length=255, blank=True, default="")
    scope_artifact_id = models.CharField(max_length=255, null=True, blank=True)
    scope_health = models.CharField(
        max_length=32, choices=ScopeHealth.choices, default=ScopeHealth.UNKNOWN
    )
    scope_cooldown_until = models.DateTimeField(null=True, blank=True)
    # Increments only for this grant-versioned exact scope's ordinary health/
    # cooldown/reconciliation/admin change (HLD §7.2). scope_observation_cursor
    # is allocated only by begin and is independent of the state version.
    scope_state_version = models.BigIntegerField(default=0)
    scope_observation_cursor = models.BigIntegerField(default=0)
    scope_last_applied_observation_seq = models.BigIntegerField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "entry_id",
                    "root_generation",
                    "credential_owner_id",
                    "borrower_owner_id",
                    "fallback_grant_id",
                ],
                name="uniq_scopestate_exact_scope",
            ),
        ]
        indexes = [
            models.Index(
                fields=["entry_id", "root_generation", "credential_owner_id", "borrower_owner_id"]
            ),
        ]


class BorrowerCredentialSuppression(models.Model):
    """Durable grant-INDEPENDENT borrowed-root suppression row/tombstone (HLD §7.2).

    Keyed ONLY by (entry, root_generation, credential_owner, borrower) — never a
    fallback grant. Absence is the canonical virtual {epoch:0, adjudicated_closed}.
    Activation and audited privileged close each CAS-increment the epoch; close
    persists the tombstone for the lifetime/retention of root N. Root N+1 makes
    N's row inapplicable but never deletes its audit/replay state. Eligibility
    denies only `active`, and active suppression applies to every current/future
    grant for that borrower+root (this is why it is grant-independent).
    """

    class State(models.TextChoices):
        ACTIVE = "active"
        ADJUDICATED_CLOSED = "adjudicated_closed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    entry_id = models.CharField(max_length=255)
    root_generation = models.ForeignKey(
        CredentialGeneration, on_delete=models.PROTECT, related_name="borrower_suppressions"
    )
    credential_owner_id = models.CharField(max_length=255)
    borrower_owner_id = models.CharField(max_length=255)
    # Immutable-at-epoch authority snapshot (a hold epoch/state, not a third
    # health-state domain). Every select/renew/report checks it (HLD §7.2).
    borrower_suppression_epoch = models.BigIntegerField(default=0)
    state = models.CharField(
        max_length=32, choices=State.choices, default=State.ADJUDICATED_CLOSED
    )
    reason = models.CharField(max_length=128, blank=True, default="")
    evidence_ref = models.CharField(max_length=255, blank=True, default="")
    activated_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    disposition_ref = models.CharField(max_length=255, blank=True, default="")
    audit_ref = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entry_id", "root_generation", "credential_owner_id", "borrower_owner_id"],
                name="uniq_borrower_suppression_key",
            ),
        ]
