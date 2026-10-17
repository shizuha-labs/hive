"""HIVE-758 schema-invariant regression tests (P2).

Encodes the accepted HLD v23 §7.2 / ADR-0039 v20 invariants for the additive
flags-off credential-authority state tables. No reducer/enforcement coverage —
that is follow-on sub-slices. These tests would have caught the P1 NULL-is-
distinct hole on uniq_scopestate_exact_scope.
"""
from django.db import IntegrityError, connection, transaction
from django.db.models import UniqueConstraint
from django.test import TestCase, TransactionTestCase

from credentials.models import (
    BorrowerCredentialSuppression,
    CredentialGeneration,
    CredentialRootState,
    CredentialScopeState,
)


def _gen(entry_id="entry-a", ordinal=1, digest="digest-a"):
    return CredentialGeneration.objects.create(
        entry_id=entry_id,
        generation_ordinal=ordinal,
        generation_chain_digest=digest,
    )


class CredentialGenerationInvariantsTests(TestCase):
    def test_entry_ordinal_unique(self):
        _gen(entry_id="e1", ordinal=1, digest="d1")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                _gen(entry_id="e1", ordinal=1, digest="d1-dup")

    def test_same_ordinal_different_entry_ok(self):
        _gen(entry_id="e1", ordinal=1, digest="d1")
        other = _gen(entry_id="e2", ordinal=1, digest="d2")
        self.assertEqual(other.generation_ordinal, 1)

    def test_constraint_name_present(self):
        names = {
            c.name
            for c in CredentialGeneration._meta.constraints
            if isinstance(c, UniqueConstraint)
        }
        self.assertIn("uniq_credgen_entry_ordinal", names)


class CredentialRootStateInvariantsTests(TestCase):
    def test_defaults(self):
        gen = _gen()
        root = CredentialRootState.objects.create(
            entry_id="e1",
            root_generation=gen,
        )
        self.assertEqual(root.root_health, CredentialRootState.RootHealth.UNKNOWN)
        self.assertEqual(root.root_state_version, 0)
        self.assertIsNone(root.terminal_reason)
        self.assertIsNone(root.current_artifact_id)
        self.assertIsNone(root.archived_at)

    def test_entry_generation_unique(self):
        gen = _gen()
        CredentialRootState.objects.create(entry_id="e1", root_generation=gen)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                CredentialRootState.objects.create(entry_id="e1", root_generation=gen)

    def test_root_health_choices_exclude_borrower_ordinary(self):
        """Root never stores ok/rate_limited/refreshable_auth_failed (HLD §7.2)."""
        values = {v for v, _ in CredentialRootState.RootHealth.choices}
        self.assertEqual(values, {"unknown", "terminal_disabled"})
        for forbidden in ("ok", "rate_limited", "refreshable_auth_failed"):
            self.assertNotIn(forbidden, values)


class CredentialScopeStateInvariantsTests(TransactionTestCase):
    """TransactionTestCase so IntegrityError does not poison the outer txn."""

    def test_fallback_grant_id_default_is_empty_string_sentinel(self):
        gen = _gen()
        scope = CredentialScopeState.objects.create(
            entry_id="e1",
            root_generation=gen,
            credential_owner_id="owner-a",
            borrower_owner_id="borrower-a",
        )
        scope.refresh_from_db()
        self.assertEqual(scope.fallback_grant_id, "")
        self.assertIsNotNone(scope.fallback_grant_id)
        field = CredentialScopeState._meta.get_field("fallback_grant_id")
        self.assertFalse(field.null)
        self.assertEqual(field.default, "")

    def test_direct_scope_uniqueness_enforced(self):
        """P1 regression: two direct-scope rows for the same exact key must fail.

        Under the old null=True design PostgreSQL admitted duplicates because
        NULL is distinct in UNIQUE. Empty-string sentinel closes the hole.
        """
        gen = _gen()
        kwargs = dict(
            entry_id="e1",
            root_generation=gen,
            credential_owner_id="owner-a",
            borrower_owner_id="borrower-a",
            fallback_grant_id="",
        )
        CredentialScopeState.objects.create(**kwargs)
        with self.assertRaises(IntegrityError):
            CredentialScopeState.objects.create(**kwargs)

    def test_direct_scope_default_omitted_also_unique(self):
        """Omitting fallback_grant_id must still collide with explicit ''."""
        gen = _gen()
        CredentialScopeState.objects.create(
            entry_id="e1",
            root_generation=gen,
            credential_owner_id="owner-a",
            borrower_owner_id="borrower-a",
        )
        with self.assertRaises(IntegrityError):
            CredentialScopeState.objects.create(
                entry_id="e1",
                root_generation=gen,
                credential_owner_id="owner-a",
                borrower_owner_id="borrower-a",
                fallback_grant_id="",
            )

    def test_distinct_fallback_grants_coexist(self):
        gen = _gen()
        base = dict(
            entry_id="e1",
            root_generation=gen,
            credential_owner_id="owner-a",
            borrower_owner_id="borrower-a",
        )
        CredentialScopeState.objects.create(**base, fallback_grant_id="")
        CredentialScopeState.objects.create(**base, fallback_grant_id="grant-1")
        CredentialScopeState.objects.create(**base, fallback_grant_id="grant-2")
        self.assertEqual(
            CredentialScopeState.objects.filter(
                entry_id="e1", root_generation=gen
            ).count(),
            3,
        )

    def test_same_fallback_grant_duplicate_rejected(self):
        gen = _gen()
        kwargs = dict(
            entry_id="e1",
            root_generation=gen,
            credential_owner_id="owner-a",
            borrower_owner_id="borrower-a",
            fallback_grant_id="grant-1",
        )
        CredentialScopeState.objects.create(**kwargs)
        with self.assertRaises(IntegrityError):
            CredentialScopeState.objects.create(**kwargs)

    def test_scope_defaults(self):
        gen = _gen()
        scope = CredentialScopeState.objects.create(
            entry_id="e1",
            root_generation=gen,
            credential_owner_id="owner-a",
            borrower_owner_id="borrower-a",
        )
        self.assertEqual(scope.scope_health, CredentialScopeState.ScopeHealth.UNKNOWN)
        self.assertEqual(scope.scope_state_version, 0)
        self.assertEqual(scope.scope_observation_cursor, 0)
        self.assertIsNone(scope.scope_cooldown_until)
        self.assertIsNone(scope.scope_artifact_id)
        self.assertIsNone(scope.scope_last_applied_observation_seq)

    def test_scope_health_includes_ordinary_borrower_states(self):
        values = {v for v, _ in CredentialScopeState.ScopeHealth.choices}
        self.assertEqual(
            values,
            {"unknown", "ok", "rate_limited", "refreshable_auth_failed"},
        )

    def test_constraint_name_and_fields(self):
        constraints = [
            c
            for c in CredentialScopeState._meta.constraints
            if isinstance(c, UniqueConstraint)
        ]
        self.assertEqual(len(constraints), 1)
        c = constraints[0]
        self.assertEqual(c.name, "uniq_scopestate_exact_scope")
        self.assertEqual(
            list(c.fields),
            [
                "entry_id",
                "root_generation",
                "credential_owner_id",
                "borrower_owner_id",
                "fallback_grant_id",
            ],
        )


class BorrowerCredentialSuppressionInvariantsTests(TestCase):
    def test_defaults_are_virtual_closed(self):
        gen = _gen()
        row = BorrowerCredentialSuppression.objects.create(
            entry_id="e1",
            root_generation=gen,
            credential_owner_id="owner-a",
            borrower_owner_id="borrower-a",
        )
        self.assertEqual(row.borrower_suppression_epoch, 0)
        self.assertEqual(
            row.state, BorrowerCredentialSuppression.State.ADJUDICATED_CLOSED
        )
        self.assertEqual(row.reason, "")
        self.assertEqual(row.evidence_ref, "")
        self.assertEqual(row.disposition_ref, "")
        self.assertEqual(row.audit_ref, "")

    def test_grant_independent_key_unique(self):
        """Suppression is keyed without fallback_grant_id (HLD §7.2)."""
        gen = _gen()
        kwargs = dict(
            entry_id="e1",
            root_generation=gen,
            credential_owner_id="owner-a",
            borrower_owner_id="borrower-a",
        )
        BorrowerCredentialSuppression.objects.create(**kwargs)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                BorrowerCredentialSuppression.objects.create(**kwargs)

    def test_no_fallback_grant_field(self):
        field_names = {f.name for f in BorrowerCredentialSuppression._meta.get_fields()}
        self.assertNotIn("fallback_grant_id", field_names)

    def test_constraint_name_present(self):
        names = {
            c.name
            for c in BorrowerCredentialSuppression._meta.constraints
            if isinstance(c, UniqueConstraint)
        }
        self.assertIn("uniq_borrower_suppression_key", names)


class MigrationSchemaGoldenTests(TestCase):
    """Golden fixture: applied schema matches the accepted model contract."""

    EXPECTED_TABLES = {
        "credentials_credentialgeneration",
        "credentials_credentialrootstate",
        "credentials_credentialscopestate",
        "credentials_borrowercredentialsuppression",
    }

    EXPECTED_UNIQUE_CONSTRAINTS = {
        "uniq_credgen_entry_ordinal",
        "uniq_rootstate_entry_gen",
        "uniq_scopestate_exact_scope",
        "uniq_borrower_suppression_key",
    }

    def test_tables_exist(self):
        existing = set(connection.introspection.table_names())
        missing = self.EXPECTED_TABLES - existing
        self.assertEqual(missing, set(), f"missing tables: {missing}")

    def test_unique_constraints_applied(self):
        # Collect constraint names across the four tables via introspection.
        found = set()
        with connection.cursor() as cursor:
            for table in self.EXPECTED_TABLES:
                constraints = connection.introspection.get_constraints(cursor, table)
                for name, meta in constraints.items():
                    if meta.get("unique"):
                        found.add(name)
        missing = self.EXPECTED_UNIQUE_CONSTRAINTS - found
        self.assertEqual(
            missing,
            set(),
            f"missing unique constraints: {missing}; found={sorted(found)}",
        )

    def test_fallback_grant_id_column_not_nullable(self):
        table = "credentials_credentialscopestate"
        with connection.cursor() as cursor:
            description = connection.introspection.get_table_description(cursor, table)
        cols = {col.name: col for col in description}
        self.assertIn("fallback_grant_id", cols)
        # Django's FieldInfo: null_ok is True when the column allows NULL.
        self.assertFalse(
            cols["fallback_grant_id"].null_ok,
            "fallback_grant_id must be NOT NULL (empty-string sentinel)",
        )
