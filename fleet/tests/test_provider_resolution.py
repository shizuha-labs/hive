"""HIVE-2161 S3-lite resolver tests — gate conditions (kumo, comment 696427)."""

from unittest import mock
from uuid import uuid4

from django.test import SimpleTestCase, TestCase

from fleet import provider_resolution as pr


class _Ref:
    """Minimal HiveOrgProviderReference stand-in (no DB)."""

    def __init__(self, org_id=1, status='active', default_models=None,
                 default_models_by_runtime=None, provider_type='openai',
                 ref_id=7):
        self.id = ref_id
        self.org_id = org_id
        self.status = status
        self.default_models = default_models if default_models is not None else []
        self.default_models_by_runtime = default_models_by_runtime
        self.provider_type = provider_type


class ResolveModelTests(SimpleTestCase):
    def test_non_byo_org_resolves_no_provider_configured(self):
        """No reference → typed no_provider_configured; caller keeps existing
        non-BYO behavior (gate condition: byte-for-byte unchanged)."""
        r = pr.resolve_model(organization_id=1, runtime_class='claude-bridge')
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, pr.REASON_NO_PROVIDER_CONFIGURED)
        self.assertFalse(r.lease_wiring_pending)
        self.assertIsNone(r.lease_handle)

    def test_fail_closed_hold_uses_dedicated_reason(self):
        """Active BYO reference + wiring pending → lease_wiring_pending, its
        own enum value — never no_provider_configured (kumo refinement #1)."""
        r = pr.resolve_model(
            organization_id=1, runtime_class='claude-bridge',
            reference=_Ref(default_models=['cortex/GLM-5.2']),
        )
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, pr.REASON_LEASE_WIRING_PENDING)
        self.assertTrue(r.lease_wiring_pending)
        self.assertNotEqual(r.reason, pr.REASON_NO_PROVIDER_CONFIGURED)
        # The org's choice is known and carried redacted — but no lease.
        self.assertEqual(r.model_id, 'cortex/GLM-5.2')
        self.assertIsNone(r.lease_handle)

    def test_precedence_by_runtime_beats_flat_list(self):
        ref = _Ref(default_models=['flat-head'],
                   default_models_by_runtime={'codex-bridge': 'by-runtime'})
        r = pr.resolve_model(organization_id=1, runtime_class='codex-bridge',
                             reference=ref)
        self.assertEqual(r.model_id, 'by-runtime')

    def test_precedence_flat_list_is_live_fallback(self):
        """Slice-1 rows (no by_runtime field) still resolve via the flat
        list — additive/null-safe, no backfill (kumo refinement #3)."""
        r = pr.resolve_model(organization_id=1, runtime_class='shizuha-cli',
                             reference=_Ref(default_models=['flat-head']))
        self.assertEqual(r.model_id, 'flat-head')

    def test_precedence_null_by_runtime_skipped_safely(self):
        ref = _Ref(default_models=['flat-head'], default_models_by_runtime=None)
        r = pr.resolve_model(organization_id=1, runtime_class='claude-bridge',
                             reference=ref)
        self.assertEqual(r.model_id, 'flat-head')

    def test_agent_override_beats_reference_defaults(self):
        ref = _Ref(default_models=['flat-head'],
                   default_models_by_runtime={'claude-bridge': 'by-runtime'})
        r = pr.resolve_model(organization_id=1, runtime_class='claude-bridge',
                             reference=ref, agent_override_model='override')
        self.assertEqual(r.model_id, 'override')

    def test_cross_org_reference_raises_invariant_violation(self):
        """Resolve-time same-org assertion (kumo refinement #4, binding):
        a mismatched row is an invariant violation, not a fallback miss."""
        with self.assertRaises(pr.InvariantViolation):
            pr.resolve_model(organization_id=2, runtime_class='claude-bridge',
                             reference=_Ref(org_id=1))
        with self.assertRaises(pr.InvariantViolation):
            pr.resolve_model(organization_id=1, runtime_class='claude-bridge',
                             reference=_Ref(org_id=None))

    def test_status_maps_to_typed_reasons(self):
        """F1 (kumo review 2026-09-12): lifecycle states type their own
        reasons — disabled/verifying/probation get OWN enum members (never
        overloaded onto no_provider_configured, which is the non-BYO signal
        and would wake the agent on Shizuha-owned keys)."""
        cases = {'invalid': pr.REASON_KEY_INVALID,
                 'exhausted': pr.REASON_PROVIDER_QUOTA_EXHAUSTED,
                 'disabled': pr.REASON_PROVIDER_DISABLED,
                 'mismatch': pr.REASON_KEY_INVALID,
                 'verifying': pr.REASON_PROVIDER_LIFECYCLE_PENDING,
                 'probation': pr.REASON_PROVIDER_LIFECYCLE_PENDING}
        for status, reason in cases.items():
            r = pr.resolve_model(organization_id=1, runtime_class='cli',
                                 reference=_Ref(status=status))
            self.assertEqual(r.reason, reason, status)
            self.assertNotEqual(r.reason, pr.REASON_NO_PROVIDER_CONFIGURED,
                                status)
            self.assertFalse(r.lease_wiring_pending)

    def test_no_model_configured_is_no_provider_configured(self):
        r = pr.resolve_model(organization_id=1, runtime_class='claude-bridge',
                             reference=_Ref(default_models=[]))
        self.assertEqual(r.reason, pr.REASON_NO_PROVIDER_CONFIGURED)

    def test_hold_is_loud_counters_emit_reason(self):
        """Loud hold (kumo refinement #2): every decision emits a counter
        carrying its reason — never a bare unexplained failure."""
        with mock.patch.object(pr, '_incr', wraps=pr._incr) as spy:
            pr.resolve_model(organization_id=1, runtime_class='cli',
                             reference=_Ref(default_models=['m']))
            pr.resolve_model(organization_id=1, runtime_class='cli')
            emitted = []
            for c in spy.call_args_list:
                name = c.args[0] if c.args else (c.kwargs.get('name') or '')
                labels = (c.args[1] if len(c.args) > 1 else None) or c.kwargs.get('labels') or {}
                emitted.append((name, labels))
        names = ' '.join(n for n, _ in emitted)
        label_blob = ' '.join(str(v) for _, labels in emitted for v in labels.values())
        self.assertTrue(any('unavailable_total' in n for n, _ in emitted))
        self.assertIn('lease_wiring_pending', label_blob)
        self.assertIn('no_provider_configured', label_blob)
        self.assertIn('unavailable_total', names)

    def test_resolution_never_carries_key_material(self):
        """Redaction: the Resolution dataclass has no field that could carry
        a credential ref or key — only opaque ids, type, model id, and the
        always-None lease slot."""
        r = pr.resolve_model(organization_id=1, runtime_class='cli',
                             reference=_Ref(default_models=['m'],
                                            provider_type='openai'))
        for forbidden in ('cortex_key_ref', 'credential_ref', 'key',
                          'authorization'):
            self.assertFalse(any(forbidden in f for f in vars(r)))
        self.assertIsNone(r.lease_handle)


class CardFailureNoteTests(SimpleTestCase):
    def test_ok_resolution_has_no_note(self):
        ref = _Ref(default_models=['m'])
        with mock.patch.object(pr, 'LEASE_WIRING_COMPLETE', True):
            r = pr.resolve_model(organization_id=1, runtime_class='cli',
                                 reference=ref)
        self.assertEqual(pr.card_failure_note(r), '')

    def test_hold_note_names_model_and_org_scope(self):
        r = pr.resolve_model(organization_id=1, runtime_class='cli',
                             reference=_Ref(default_models=['cortex/GLM-5.2']))
        note = pr.card_failure_note(r)
        self.assertIn('lease wiring pending', note)
        self.assertIn('cortex/GLM-5.2', note)
        self.assertIn('Org-scoped', note)
        self.assertIn('will not run on Shizuha-owned keys', note)

    def test_note_never_echoes_secrets(self):
        r = pr.resolve_model(organization_id=1, runtime_class='cli',
                             reference=_Ref(default_models=['m']))
        note = pr.card_failure_note(r)
        for forbidden in ('cortex_key_ref', 'sk-', 'Bearer', 'credential_ref'):
            self.assertNotIn(forbidden, note)


class IdentityBridgeTests(SimpleTestCase):
    """Kumo refinement #5 (binding, 09-10 comment): the slug↔org_id bridge
    must fail LOUD — shape 2 (lookup failed/ambiguous) holds with its own
    reason code ``identity_unresolved`` and never falls through to the
    non-BYO path."""

    def test_shape1_no_rows_maps_to_non_byo(self):
        """Lookup succeeded, zero active rows → None → genuinely non-BYO,
        byte-for-byte unchanged path."""
        self.assertIsNone(pr.resolve_org_id_from_slug('acme', lambda s: None))
        self.assertIsNone(pr.resolve_org_id_from_slug('acme', lambda s: []))

    def test_shape1_unique_hit_returns_int(self):
        self.assertEqual(
            pr.resolve_org_id_from_slug('acme', lambda s: 42), 42)
        self.assertEqual(
            pr.resolve_org_id_from_slug('acme', lambda s: '42'), 42)
        self.assertEqual(
            pr.resolve_org_id_from_slug('acme', lambda s: [17]), 17)

    def test_shape2_lookup_failure_holds_loud(self):
        """The lookup itself failed → IdentityUnresolved, never None, never
        no_provider_configured."""
        def broken(_slug):
            raise RuntimeError('join exploded')
        with self.assertRaises(pr.IdentityUnresolved) as ctx:
            pr.resolve_org_id_from_slug('acme', broken)
        self.assertEqual(ctx.exception.reason, pr.REASON_IDENTITY_UNRESOLVED)

    def test_shape2_ambiguous_match_holds_loud(self):
        """Multiple candidates → IdentityUnresolved, never silent non-BYO."""
        with self.assertRaises(pr.IdentityUnresolved) as ctx:
            pr.resolve_org_id_from_slug('acme', lambda s: [3, 9])
        self.assertEqual(ctx.exception.reason, pr.REASON_IDENTITY_UNRESOLVED)

    def test_shape2_empty_slug_holds_loud(self):
        with self.assertRaises(pr.IdentityUnresolved):
            pr.resolve_org_id_from_slug('', lambda s: 1)
        with self.assertRaises(pr.IdentityUnresolved):
            pr.resolve_org_id_from_slug(None, lambda s: 1)

    def test_shape2_non_numeric_id_holds_loud(self):
        with self.assertRaises(pr.IdentityUnresolved):
            pr.resolve_org_id_from_slug('acme', lambda s: 'org-acme')

    def test_identity_unresolved_reason_is_distinct(self):
        """The shape-2 reason must be its own enum member — distinct from
        both existing hold reasons (kumo refinement #5 test requirement)."""
        self.assertNotEqual(pr.REASON_IDENTITY_UNRESOLVED,
                            pr.REASON_LEASE_WIRING_PENDING)
        self.assertNotEqual(pr.REASON_IDENTITY_UNRESOLVED,
                            pr.REASON_NO_PROVIDER_CONFIGURED)
        self.assertNotEqual(pr.REASON_IDENTITY_UNRESOLVED,
                            pr.REASON_INVARIANT_VIOLATION)

    def test_bridge_is_loud_counters_emit_outcomes(self):
        """Every bridge outcome emits a counter (loud, observable)."""
        seen = []
        with mock.patch.object(pr, '_incr', lambda n, l=None: seen.append((n, l))):
            pr.resolve_org_id_from_slug('acme', lambda s: 5)
            pr.resolve_org_id_from_slug('acme', lambda s: None)
            with self.assertRaises(pr.IdentityUnresolved):
                pr.resolve_org_id_from_slug('acme', lambda s: [1, 2])
        names = [n for n, _ in seen]
        self.assertIn('identity_bridge_total', names)
        outcomes = [l['outcome'] for _, l in seen if l]
        self.assertIn('resolved', outcomes)
        self.assertIn('no_rows', outcomes)
        self.assertIn('identity_unresolved', outcomes)

    def test_identity_note_is_loud_and_scoped(self):
        """The card note for identity_unresolved names the failure and the
        no-Shizuha-key guarantee, and never echoes secrets."""
        note = pr.card_failure_note(pr.Resolution(
            ok=False, reason=pr.REASON_IDENTITY_UNRESOLVED))
        self.assertIn('identity could not be resolved', note)
        self.assertIn('Shizuha-owned keys', note)
        self.assertIn('org-scoped', note)
        for forbidden in ('cortex_key_ref', 'sk-', 'Bearer', 'credential_ref'):
            self.assertNotIn(forbidden, note)


# ── Wake-decision + card-grid pipelines (DB-backed) ─────────────────────────


class _RosterBase(TestCase):
    """Seeds roster adjacency + mirror rows for the pipeline tests."""

    @staticmethod
    def _lookup(org_id):
        return lambda slug: org_id

    def _roster(self, agent_username, org_slug, enabled=True):
        from fleet.models import TeamMembershipRole
        return TeamMembershipRole.objects.create(
            agent_identifier=agent_username.lower(),
            team_slug='build-pipeline',
            organization_slug=org_slug,
            enabled=enabled,
        )


class ResolveAgentOrgProviderTests(_RosterBase):
    def test_agent_without_roster_adjacency_is_non_byo(self):
        """No roster edge → genuinely non-BYO → typed unavailable, no raise
        (gate condition: byte-for-byte unchanged caller behavior)."""
        from fleet import provider_resolution as pr
        r = pr.resolve_agent_org_provider({'agent_username': 'ghost'})
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, pr.REASON_NO_PROVIDER_CONFIGURED)

    def test_byo_agent_holds_lease_wiring_pending_with_model(self):
        """Roster edge + wired lookup + active ref → fail-closed hold carrying
        the org's model choice (never Shizuha-key resolution)."""
        from fleet.models import HiveOrgProviderReference
        from fleet import provider_resolution as pr
        self._roster('gin', 'acme')
        HiveOrgProviderReference.objects.create(
            org_id=7, cortex_provider_id=uuid4(),
            default_models=['cortex/GLM-5.2'], status='active',
        )
        r = pr.resolve_agent_org_provider(
            {'agent_username': 'gin'}, runtime_class='claude-bridge',
            slug_lookup=self._lookup(7),
        )
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, pr.REASON_LEASE_WIRING_PENDING)
        self.assertEqual(r.model_id, 'cortex/GLM-5.2')
        self.assertTrue(r.lease_wiring_pending)
        self.assertIsNone(r.lease_handle)

    def test_unwired_lookup_holds_loud_never_non_byo(self):
        """Roster edge + an active reference somewhere + registry unwired →
        IdentityUnresolved raised — NEVER a silent non-BYO fall-through
        (kumo refinement #5: the silent path puts a BYO org on Shizuha keys)."""
        from fleet.models import HiveOrgProviderReference
        from fleet import provider_resolution as pr
        self._roster('kane', 'byo-org')
        HiveOrgProviderReference.objects.create(
            org_id=9, cortex_provider_id=uuid4(),
            default_models=['cortex/Qwen3.6'], status='active',
        )
        # Deterministically simulate the unwired registry: CI runs in-cluster
        # where the default hook is LIVE (a real Admin 404 is a contract-
        # correct shape-1 miss → non-BYO), so "unwired" must be patched,
        # never assumed from the environment.
        with mock.patch.object(pr, 'ORG_SLUG_LOOKUP', None):
            with self.assertRaises(pr.IdentityUnresolved):
                pr.resolve_agent_org_provider({'agent_username': 'kane'})

    def test_lookup_no_rows_is_non_byo(self):
        """Successful lookup, zero active org rows → non-BYO unchanged path."""
        from fleet import provider_resolution as pr
        self._roster('sato', 'plain-org')
        r = pr.resolve_agent_org_provider(
            {'agent_username': 'sato'}, slug_lookup=lambda s: None,
        )
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, pr.REASON_NO_PROVIDER_CONFIGURED)

    def test_ambiguous_roster_adjacency_raises_loud(self):
        """Agent mapped to two orgs → IdentityUnresolved (ambiguous adjacency)."""
        from fleet import provider_resolution as pr
        self._roster('tora', 'acme')
        self._roster('tora', 'globex')
        with self.assertRaises(pr.IdentityUnresolved):
            pr.resolve_agent_org_provider(
                {'agent_username': 'tora'}, slug_lookup=self._lookup(7),
            )

    def test_disabled_ref_is_never_served(self):
        """F1 (kumo review 2026-09-12): a disabled credential is resolution-
        ineligible with its OWN typed reason (provider_disabled) — never ok,
        never silently mapped to no_provider_configured (which would wake the
        agent on Shizuha-owned keys), never a lease-wiring hold."""
        from fleet.models import HiveOrgProviderReference
        from fleet import provider_resolution as pr
        self._roster('gin', 'acme')
        HiveOrgProviderReference.objects.create(
            org_id=7, cortex_provider_id=uuid4(),
            default_models=['cortex/GLM-5.2'],
            status=HiveOrgProviderReference.Status.DISABLED,
        )
        r = pr.resolve_agent_org_provider(
            {'agent_username': 'gin'}, slug_lookup=self._lookup(7),
        )
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, pr.REASON_PROVIDER_DISABLED)
        self.assertNotEqual(r.reason, pr.REASON_NO_PROVIDER_CONFIGURED)
        self.assertFalse(r.lease_wiring_pending)

    def test_lifecycle_states_hold_with_typed_reasons(self):
        """F1 point 1+2 (kumo review 2026-09-12, blocking): every lifecycle
        state reaches the resolver's typed mapping — the pipeline must NOT
        pre-filter non-active refs into a silent non-BYO wake. VERIFYING/
        PROBATION wait (provider_lifecycle_pending); INVALID/MISMATCH are
        key_invalid; EXHAUSTED is provider_quota_exhausted; DISABLED is
        provider_disabled. None resolve no_provider_configured."""
        from fleet.models import HiveOrgProviderReference
        from fleet import provider_resolution as pr
        expected = {
            HiveOrgProviderReference.Status.VERIFYING: pr.REASON_PROVIDER_LIFECYCLE_PENDING,
            HiveOrgProviderReference.Status.PROBATION: pr.REASON_PROVIDER_LIFECYCLE_PENDING,
            HiveOrgProviderReference.Status.INVALID: pr.REASON_KEY_INVALID,
            HiveOrgProviderReference.Status.MISMATCH: pr.REASON_KEY_INVALID,
            HiveOrgProviderReference.Status.EXHAUSTED: pr.REASON_PROVIDER_QUOTA_EXHAUSTED,
            HiveOrgProviderReference.Status.DISABLED: pr.REASON_PROVIDER_DISABLED,
        }
        self._roster('gin', 'acme')
        for status, want_reason in expected.items():
            with self.subTest(status=status):
                HiveOrgProviderReference.objects.create(
                    org_id=7, cortex_provider_id=uuid4(),
                    default_models=['cortex/GLM-5.2'], status=status,
                )
                r = pr.resolve_agent_org_provider(
                    {'agent_username': 'gin'}, runtime_class='claude-bridge',
                    slug_lookup=self._lookup(7),
                )
                self.assertFalse(r.ok)
                self.assertEqual(r.reason, want_reason)
                self.assertNotEqual(r.reason, pr.REASON_NO_PROVIDER_CONFIGURED)

    def test_latest_reference_wins_over_stale_lifecycle_row(self):
        """F1: mid-rotate orgs carry a disabled predecessor plus the active
        successor — the ACTIVE reference serves; the stale row never types
        the resolution."""
        from fleet.models import HiveOrgProviderReference
        from fleet import provider_resolution as pr
        self._roster('gin', 'acme')
        HiveOrgProviderReference.objects.create(
            org_id=7, cortex_provider_id=uuid4(),
            default_models=['cortex/OLD'], status='disabled',
        )
        HiveOrgProviderReference.objects.create(
            org_id=7, cortex_provider_id=uuid4(),
            default_models=['cortex/NEW'], status='active',
        )
        r = pr.resolve_agent_org_provider(
            {'agent_username': 'gin'}, runtime_class='claude-bridge',
            slug_lookup=self._lookup(7),
        )
        self.assertEqual(r.reason, pr.REASON_LEASE_WIRING_PENDING)
        self.assertEqual(r.model_id, 'cortex/NEW')

    def test_multiple_active_refs_is_invariant_violation(self):
        """Two active providers for one org → fail-loud InvariantViolation."""
        from fleet.models import HiveOrgProviderReference
        from fleet import provider_resolution as pr
        self._roster('gin', 'acme')
        for _ in range(2):
            HiveOrgProviderReference.objects.create(
                org_id=7, cortex_provider_id=uuid4(),
                default_models=['cortex/GLM-5.2'], status='active',
            )
        with self.assertRaises(pr.InvariantViolation):
            pr.resolve_agent_org_provider(
                {'agent_username': 'gin'}, slug_lookup=self._lookup(7),
            )


class OrgProviderNotesTests(_RosterBase):
    def _ref(self, org_id, status='active'):
        from fleet.models import HiveOrgProviderReference
        return HiveOrgProviderReference.objects.create(
            org_id=org_id, cortex_provider_id=uuid4(),
            default_models=['cortex/GLM-5.2'], status=status,
        )

    def test_notes_hold_for_byo_org_and_skip_non_byo(self):
        """BYO org's agent gets the loud scoped note; the non-BYO agent is
        absent from the mapping (byte-for-byte unchanged)."""
        from fleet import provider_resolution as pr
        self._ref(7)
        self._roster('gin', 'acme')
        self._roster('ghost', 'plain-org')
        notes = pr.org_provider_notes(
            [{'agent_username': 'gin'}, {'agent_username': 'ghost'}],
            slug_lookup={'acme': 7, 'plain-org': None}.get,
        )
        self.assertIn('gin', notes)
        self.assertIn('lease wiring pending', notes['gin'])
        self.assertIn('org-scoped', notes['gin'].lower())
        self.assertNotIn('ghost', notes)

    def test_notes_short_circuit_with_zero_refs(self):
        """No references at all → {} without touching the roster. (F1: the
        guard is status-blind — lifecycle rows annotate now.)"""
        from fleet import provider_resolution as pr
        self._roster('gin', 'acme')
        self.assertEqual(pr.org_provider_notes([{'agent_username': 'gin'}]), {})

    def test_notes_name_the_real_lifecycle_state(self):
        """F1 point 3 (kumo review 2026-09-12): the grid note names the real
        state (disabled / verifying) — never the false "No BYO provider
        configured" when a reference row exists."""
        from fleet.models import HiveOrgProviderReference
        from fleet import provider_resolution as pr
        self._roster('gin', 'acme')
        self._ref(7, status=HiveOrgProviderReference.Status.DISABLED)
        notes = pr.org_provider_notes(
            [{'agent_username': 'gin'}],
            slug_lookup={'acme': 7}.get,
        )
        self.assertIn('gin', notes)
        self.assertIn('disabled', notes['gin'].lower())
        self.assertNotIn('no byo provider configured', notes['gin'].lower())

        HiveOrgProviderReference.objects.all().delete()
        self._ref(7, status=HiveOrgProviderReference.Status.VERIFYING)
        notes = pr.org_provider_notes(
            [{'agent_username': 'gin'}],
            slug_lookup={'acme': 7}.get,
        )
        self.assertIn('gin', notes)
        self.assertIn('verifying', notes['gin'].lower())
        self.assertNotIn('no byo provider configured', notes['gin'].lower())

    def test_notes_omit_model_id_batched(self):
        """N1 (kumo review 2026-09-12): the batched grid path resolves with
        runtime_class='' and may name a different model than the wake hold —
        the batched note omits the model id entirely."""
        from fleet import provider_resolution as pr
        self._ref(7)
        self._roster('gin', 'acme')
        notes = pr.org_provider_notes(
            [{'agent_username': 'gin'}],
            slug_lookup={'acme': 7}.get,
        )
        self.assertIn('gin', notes)
        self.assertIn('lease wiring pending', notes['gin'])
        self.assertNotIn('cortex/GLM-5.2', notes['gin'])

    def test_notes_surface_identity_unresolved(self):
        """Registry unwired + a live reference → the operator-attention note."""
        from fleet import provider_resolution as pr
        self._ref(9)
        self._roster('kane', 'byo-org')
        # Patch the hook to None (deterministic unwired simulation — CI runs
        # in-cluster where the default hook is live).
        with mock.patch.object(pr, 'ORG_SLUG_LOOKUP', None):
            notes = pr.org_provider_notes([{'agent_username': 'kane'}])
        self.assertIn('kane', notes)
        self.assertIn('identity could not be resolved', notes['kane'])

    def test_notes_never_echo_secret_material(self):
        """Notes carry state, never key material or opaque refs."""
        from fleet import provider_resolution as pr
        self._ref(7)
        self._roster('gin', 'acme')
        notes = pr.org_provider_notes([{'agent_username': 'gin'}])
        blob = ' '.join(notes.values())
        for forbidden in ('sk-', 'Bearer ', 'cred-', 'cortex_key_ref'):
            self.assertNotIn(forbidden, blob)


class LeaseStartDecisionLifecycleTests(_RosterBase):
    """F1 point 2 (kumo review 2026-09-12, blocking): the WAKE gate holds for
    every typed unavailable reason — lifecycle states never silently start the
    agent on Shizuha-owned keys."""

    def _decision(self, username='gin', org_id=7):
        from fleet.models import FleetAgent
        from fleet import provisioner
        from fleet import provider_resolution as pr
        agent = FleetAgent(agent_username=username)
        # The registry hook is unwired until the registry slice lands; wire it
        # exactly the way production will (module global).
        with mock.patch.object(pr, 'ORG_SLUG_LOOKUP', lambda slug: org_id):
            decision = provisioner._lease_start_decision(agent)
        return decision

    def test_wake_holds_for_every_lifecycle_state(self):
        from fleet.models import HiveOrgProviderReference
        from fleet import provider_resolution as pr
        expected = {
            HiveOrgProviderReference.Status.VERIFYING: pr.REASON_PROVIDER_LIFECYCLE_PENDING,
            HiveOrgProviderReference.Status.PROBATION: pr.REASON_PROVIDER_LIFECYCLE_PENDING,
            HiveOrgProviderReference.Status.INVALID: pr.REASON_KEY_INVALID,
            HiveOrgProviderReference.Status.MISMATCH: pr.REASON_KEY_INVALID,
            HiveOrgProviderReference.Status.EXHAUSTED: pr.REASON_PROVIDER_QUOTA_EXHAUSTED,
            HiveOrgProviderReference.Status.DISABLED: pr.REASON_PROVIDER_DISABLED,
        }
        self._roster('gin', 'acme')
        for status, want_reason in expected.items():
            with self.subTest(status=status):
                HiveOrgProviderReference.objects.create(
                    org_id=7, cortex_provider_id=uuid4(),
                    default_models=['cortex/GLM-5.2'], status=status,
                )
                decision = self._decision()
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, want_reason)
                # Fault-class reasons are operator attention, not lease-waiting.
                self.assertFalse(decision.pending)

    def test_wake_holds_typed_on_invariant_violation(self):
        """N3 (kumo review 2026-09-12): a cross-org/ambiguous reference is
        fail-closed with the TYPED invariant_violation reason at the wake
        gate — not an untyped gate_error_fail_closed."""
        from fleet.models import HiveOrgProviderReference
        from fleet import provider_resolution as pr
        self._roster('gin', 'acme')
        for _ in range(2):
            HiveOrgProviderReference.objects.create(
                org_id=7, cortex_provider_id=uuid4(),
                default_models=['cortex/GLM-5.2'], status='active',
            )
        decision = self._decision()
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, pr.REASON_INVARIANT_VIOLATION)
        self.assertFalse(decision.pending)

    def test_non_byo_wake_path_unchanged(self):
        """Gate condition: a genuinely non-BYO agent (no roster edge) still
        proceeds past the provider gate byte-for-byte — the gate returns the
        downstream lease decision, not a provider hold."""
        from fleet import provider_resolution as pr
        decision = self._decision(username='ghost')
        self.assertNotEqual(decision.reason, pr.REASON_NO_PROVIDER_CONFIGURED)
        self.assertNotEqual(decision.reason, pr.REASON_LEASE_WIRING_PENDING)
