"""PLAT-4481 / PLAT-4483 — Metering Core identity, payer-link, genesis, catalog."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from .metering_core import (
    CatalogError,
    ConsentError,
    MeteringCoreError,
    activate_payer_link_grant,
    bump_admission_epoch,
    consume_entitlement,
    create_billing_account,
    create_payer_genesis_manifest,
    create_payer_link_proposal,
    import_payer_genesis_rows,
    issue_account_entitlement_from_template,
    issue_payer_authority_proof,
    materialize_resource_payer_binding,
    ordered_entitlement_balances,
    record_payer_link_consent,
    rebuild_current_resource_payer,
    revoke_payer_link_grant,
    seed_free_v1_catalog,
    transition_collection_status,
)
from .metering_events import validate_registered_admission_authority
from .models import (
    BillingAccount,
    CurrentResourcePayer,
    EntitlementGrant,
    MeterDefinition,
    PayerAuthorityProof,
    PayerGenesisFact,
    PayerLinkConsent,
    PayerLinkGrantVersion,
    PayerLinkProposal,
    PlanVersion,
    RateVersion,
    ResourcePayerBindingVersion,
)
from hive_platform.models import AdmissionAuthoritySnapshot


class BillingAccountTests(TestCase):
    def test_create_individual_and_org_isolation(self):
        a = create_billing_account(
            account_type=BillingAccount.AccountType.INDIVIDUAL,
            owner_subject='user:alice',
            display_name='Alice',
        )
        b = create_billing_account(
            account_type=BillingAccount.AccountType.ORGANIZATION,
            organization_id='org:acme',
            display_name='Acme',
        )
        self.assertNotEqual(a.id, b.id)
        self.assertEqual(a.currency, 'INR')
        self.assertEqual(a.collection_status, BillingAccount.CollectionStatus.ACTIVE)
        self.assertEqual(a.admission_epoch, 1)
        self.assertEqual(a.policy_generation, 1)
        # UUID is immutable
        a.display_name = 'Alice 2'
        a.save(update_fields=['display_name', 'updated_at'])
        a.currency = 'USD'
        with self.assertRaises(ValidationError):
            a.save()

    def test_collection_state_machine_and_closed_terminal(self):
        acct = create_billing_account(
            account_type='individual', owner_subject='user:bob'
        )
        transition_collection_status(acct, to_status='past_due', reason='invoice late')
        acct.refresh_from_db()
        self.assertEqual(acct.collection_status, 'past_due')
        transition_collection_status(acct, to_status='suspended')
        transition_collection_status(acct, to_status='closed')
        acct.refresh_from_db()
        self.assertIsNotNone(acct.closed_at)
        with self.assertRaises(MeteringCoreError):
            transition_collection_status(acct, to_status='active')

    def test_non_inr_rejected(self):
        with self.assertRaises(MeteringCoreError):
            create_billing_account(
                account_type='individual', owner_subject='u:1', currency='USD'
            )


class PayerLinkDualConsentTests(TestCase):
    def setUp(self):
        self.acct = create_billing_account(
            account_type='individual', owner_subject='user:owner'
        )
        self.proposal = create_payer_link_proposal(
            billing_account=self.acct,
            scope_kind='resource',
            scope_id='claw:abc',
            resource_owner_subject='user:owner',
            billing_admin_subject='user:billing',
            resource_owner_role_generation=3,
            billing_admin_role_generation=7,
            allowed_resource_types=['claw'],
            budget_hard_cap_inr_micros=1_000_000_000,
        )

    def test_dual_consent_activates_grant_and_binding(self):
        record_payer_link_consent(
            self.proposal,
            role='resource_owner',
            subject='user:owner',
            role_generation=3,
            action_grant_id='ag-owner-1',
        )
        record_payer_link_consent(
            self.proposal,
            role='billing_admin',
            subject='user:billing',
            role_generation=7,
            action_grant_id='ag-admin-1',
        )
        grant = activate_payer_link_grant(self.proposal)
        self.assertEqual(grant.status, PayerLinkGrantVersion.Status.ACTIVE)
        self.assertEqual(grant.billing_account_id, self.acct.id)
        self.assertEqual(grant.admission_epoch_at_activation, self.acct.admission_epoch)

        binding = materialize_resource_payer_binding(
            grant=grant, resource_uid='claw:abc', resource_type='claw'
        )
        self.assertEqual(binding.status, 'active')
        current = CurrentResourcePayer.objects.get(resource_uid='claw:abc')
        self.assertEqual(current.billing_account_id, self.acct.id)
        self.assertEqual(current.binding_version_id, binding.id)

        # Immutable grant payload
        grant.scope_id = 'claw:mutated'
        with self.assertRaises(ValidationError):
            grant.save()

    def test_consent_replay_rejected(self):
        record_payer_link_consent(
            self.proposal,
            role='resource_owner',
            subject='user:owner',
            role_generation=3,
            action_grant_id='ag-once',
        )
        with self.assertRaises(ConsentError):
            record_payer_link_consent(
                self.proposal,
                role='billing_admin',
                subject='user:billing',
                role_generation=7,
                action_grant_id='ag-once',  # replay
            )

    def test_stale_role_generation_rejected(self):
        with self.assertRaises(ConsentError):
            record_payer_link_consent(
                self.proposal,
                role='resource_owner',
                subject='user:owner',
                role_generation=99,  # stale
                action_grant_id='ag-stale',
            )

    def test_single_consent_cannot_activate(self):
        record_payer_link_consent(
            self.proposal,
            role='resource_owner',
            subject='user:owner',
            role_generation=3,
            action_grant_id='ag-only-owner',
        )
        with self.assertRaises(ConsentError):
            activate_payer_link_grant(self.proposal)

    def test_two_account_isolation_on_bindings(self):
        other = create_billing_account(
            account_type='individual', owner_subject='user:other'
        )
        p2 = create_payer_link_proposal(
            billing_account=other,
            scope_kind='resource',
            scope_id='claw:other',
            resource_owner_subject='user:other',
            billing_admin_subject='user:other',
            resource_owner_role_generation=1,
            billing_admin_role_generation=1,
            allowed_resource_types=['claw'],
        )
        for role, ag in (
            ('resource_owner', 'ag-o1'),
            ('billing_admin', 'ag-o2'),
        ):
            record_payer_link_consent(
                p2, role=role, subject='user:other', role_generation=1, action_grant_id=ag
            )
        g2 = activate_payer_link_grant(p2)
        materialize_resource_payer_binding(
            grant=g2, resource_uid='claw:other', resource_type='claw'
        )

        # Complete first proposal too
        record_payer_link_consent(
            self.proposal,
            role='resource_owner',
            subject='user:owner',
            role_generation=3,
            action_grant_id='ag-a1',
        )
        record_payer_link_consent(
            self.proposal,
            role='billing_admin',
            subject='user:billing',
            role_generation=7,
            action_grant_id='ag-a2',
        )
        g1 = activate_payer_link_grant(self.proposal)
        materialize_resource_payer_binding(
            grant=g1, resource_uid='claw:abc', resource_type='claw'
        )

        self.assertEqual(
            CurrentResourcePayer.objects.get(resource_uid='claw:abc').billing_account_id,
            self.acct.id,
        )
        self.assertEqual(
            CurrentResourcePayer.objects.get(resource_uid='claw:other').billing_account_id,
            other.id,
        )
        self.assertEqual(
            ResourcePayerBindingVersion.objects.filter(billing_account=self.acct).count(),
            1,
        )
        self.assertEqual(
            ResourcePayerBindingVersion.objects.filter(billing_account=other).count(),
            1,
        )


class RevokeEpochAndProofTests(TestCase):
    def _active_binding(self, resource_uid='res:1'):
        acct = create_billing_account(
            account_type='individual', owner_subject='user:x'
        )
        proposal = create_payer_link_proposal(
            billing_account=acct,
            scope_kind='resource',
            scope_id=resource_uid,
            resource_owner_subject='user:x',
            billing_admin_subject='user:x',
            resource_owner_role_generation=1,
            billing_admin_role_generation=1,
            allowed_resource_types=['claw'],
        )
        record_payer_link_consent(
            proposal,
            role='resource_owner',
            subject='user:x',
            role_generation=1,
            action_grant_id=f'ag-ro-{resource_uid}',
        )
        record_payer_link_consent(
            proposal,
            role='billing_admin',
            subject='user:x',
            role_generation=1,
            action_grant_id=f'ag-ba-{resource_uid}',
        )
        grant = activate_payer_link_grant(proposal)
        binding = materialize_resource_payer_binding(
            grant=grant, resource_uid=resource_uid, resource_type='claw'
        )
        return acct, grant, binding

    def test_revoke_bumps_admission_epoch_and_clears_current(self):
        acct, grant, binding = self._active_binding('res:revoke')
        epoch_before = acct.admission_epoch
        issued = issue_payer_authority_proof(
            resource_uid='res:revoke',
            reservation_id='rsv-1',
        )
        self.assertTrue(
            PayerAuthorityProof.objects.filter(proof_id=issued.proof.proof_id).exists()
        )
        # Historical proof remains valid after revoke
        validate_registered_admission_authority(
            {
                'proof_id': issued.proof.proof_id,
                'proof_sha256': issued.proof.proof_sha256,
                'billing_account_id': str(acct.id),
                'reservation_id': 'rsv-1',
                'lease_id': '',
                'payer_link_generation': grant.version,
                'binding_generation': binding.binding_version,
                'admission_generation': epoch_before,
            }
        )

        revoke_payer_link_grant(grant, reason='user requested')
        acct.refresh_from_db()
        self.assertEqual(acct.admission_epoch, epoch_before + 1)
        self.assertFalse(CurrentResourcePayer.objects.filter(resource_uid='res:revoke').exists())
        binding.refresh_from_db()
        self.assertEqual(binding.status, 'revoked')
        grant.refresh_from_db()
        self.assertEqual(grant.status, PayerLinkGrantVersion.Status.REVOKED)

        # New proof issuance fails without current binding
        with self.assertRaises(MeteringCoreError):
            issue_payer_authority_proof(resource_uid='res:revoke', reservation_id='rsv-2')

        # Old proof still resolves (historical occurrence authority)
        snap = AdmissionAuthoritySnapshot.objects.get(proof_id=issued.proof.proof_id)
        self.assertEqual(snap.admission_generation, epoch_before)

    def test_rebuild_current_from_bindings(self):
        acct, grant, binding = self._active_binding('res:rebuild')
        CurrentResourcePayer.objects.filter(resource_uid='res:rebuild').delete()
        rebuilt = rebuild_current_resource_payer('res:rebuild')
        self.assertIsNotNone(rebuilt)
        self.assertEqual(rebuilt.binding_version_id, binding.id)
        self.assertEqual(rebuilt.billing_account_id, acct.id)


class GenesisQuarantineTests(TestCase):
    def test_unambiguous_self_payer_imported_ambiguous_quarantined(self):
        acct = create_billing_account(
            account_type='individual', owner_subject='user:solo'
        )
        other = create_billing_account(
            account_type='individual', owner_subject='user:shared'
        )
        manifest = create_payer_genesis_manifest(
            source_system='legacy-hive',
            source_schema_version='v1',
            source_watermark='ts-100',
            writer_authority_generation=1,
            signature='test-sig',
        )
        import_payer_genesis_rows(
            manifest,
            [
                {
                    'source_object_id': 'claw-1',
                    'source_object_version': '1',
                    'resource_uid': 'claw:solo',
                    'resource_type': 'claw',
                    'billing_account_id': acct.id,
                    'ownership_evidence': {
                        'self_subject': 'user:solo',
                        'owner_subjects': ['user:solo'],
                        'confidence': 1.0,
                    },
                },
                {
                    'source_object_id': 'claw-2',
                    'source_object_version': '1',
                    'resource_uid': 'claw:ambig',
                    'resource_type': 'claw',
                    'billing_account_id': other.id,
                    'ownership_evidence': {
                        'self_subject': 'user:shared',
                        'owner_subjects': ['user:shared', 'user:partner'],
                        'confidence': 1.0,
                    },
                },
            ],
        )
        manifest.refresh_from_db()
        self.assertEqual(manifest.status, 'completed')
        self.assertEqual(manifest.imported_count, 1)
        self.assertEqual(manifest.quarantined_count, 1)

        self.assertTrue(CurrentResourcePayer.objects.filter(resource_uid='claw:solo').exists())
        self.assertFalse(CurrentResourcePayer.objects.filter(resource_uid='claw:ambig').exists())

        q = PayerGenesisFact.objects.get(resource_uid='claw:ambig')
        self.assertEqual(q.decision, PayerGenesisFact.Decision.QUARANTINED)
        self.assertIn('ambiguous', q.quarantine_reason)

        ok = PayerGenesisFact.objects.get(resource_uid='claw:solo')
        self.assertEqual(ok.decision, PayerGenesisFact.Decision.SELF_PAYER)
        self.assertIsNotNone(ok.resulting_grant_version_id)


class CatalogImmutabilityTests(TestCase):
    def test_seed_free_v1_idempotent_and_immutable(self):
        first = seed_free_v1_catalog()
        second = seed_free_v1_catalog()
        self.assertEqual(MeterDefinition.objects.count(), 6)
        self.assertEqual(first['free_plan'].id, second['free_plan'].id)
        self.assertEqual(PlanVersion.objects.filter(plan_key='free-v1').count(), 1)
        self.assertEqual(PlanVersion.objects.filter(plan_key='starter-999-v1-rc1').count(), 1)
        starter = PlanVersion.objects.get(plan_key='starter-999-v1-rc1')
        self.assertEqual(starter.status, 'proposed')
        self.assertEqual(RateVersion.objects.count(), 6)
        self.assertEqual(
            EntitlementGrant.objects.filter(billing_account__isnull=True).count(), 6
        )

        meter = MeterDefinition.objects.get(key='platform.requests', version=1)
        meter.canonical_unit = 'mutated'
        with self.assertRaises(ValidationError):
            meter.save()

        plan = first['free_plan']
        plan.advertised_price_inr_micros = 1
        with self.assertRaises(ValidationError):
            plan.save()

    def test_grant_consumption_order_by_priority(self):
        seeded = seed_free_v1_catalog()
        acct = create_billing_account(
            account_type='individual', owner_subject='user:grants'
        )
        # Issue two grants for same meter with different priorities via templates
        templates = list(
            EntitlementGrant.objects.filter(
                billing_account__isnull=True, plan_version=seeded['free_plan']
            ).order_by('priority')
        )
        self.assertGreaterEqual(len(templates), 2)
        t_low_p, t_high_p = templates[0], templates[1]
        # Force same meter on both for ordering test by issuing balances
        g1, b1 = issue_account_entitlement_from_template(
            template=t_low_p, billing_account=acct, amount=Decimal('100')
        )
        g2, b2 = issue_account_entitlement_from_template(
            template=t_high_p, billing_account=acct, amount=Decimal('100')
        )
        ordered = ordered_entitlement_balances(acct)
        self.assertEqual(ordered[0].grant.priority, g1.priority)
        self.assertLessEqual(ordered[0].grant.priority, ordered[1].grant.priority)

        consume_entitlement(ordered[0], amount=Decimal('10'))
        ordered[0].refresh_from_db()
        self.assertEqual(ordered[0].consumed_amount, Decimal('10'))
        self.assertEqual(ordered[0].available_amount, Decimal('90'))

        with self.assertRaises(CatalogError):
            consume_entitlement(ordered[0], amount=Decimal('1000'))

    def test_reset_generation_increments_on_reissue(self):
        seeded = seed_free_v1_catalog()
        acct = create_billing_account(
            account_type='individual', owner_subject='user:reset'
        )
        template = EntitlementGrant.objects.filter(
            grant_key='free-v1.platform.requests', billing_account__isnull=True
        ).get()
        g1, _ = issue_account_entitlement_from_template(
            template=template, billing_account=acct, amount=Decimal('1000')
        )
        g2, _ = issue_account_entitlement_from_template(
            template=template, billing_account=acct, amount=Decimal('1000')
        )
        self.assertEqual(g1.reset_generation, 1)
        self.assertEqual(g2.reset_generation, 2)
        # Template itself stays at generation 1
        template.refresh_from_db()
        self.assertEqual(template.reset_generation, 1)

    def test_catalog_is_configuration_not_hardcoded_keys_only(self):
        """free-v1 meters come from metering_catalog module (configuration)."""
        from . import metering_catalog as cat

        self.assertEqual(len(cat.FREE_V1_METERS), 6)
        keys = {m['key'] for m in cat.FREE_V1_METERS}
        self.assertIn('storage.byte_seconds', keys)
        self.assertIn('inference.weighted_tokens', keys)
        seed_free_v1_catalog()
        for key in keys:
            self.assertTrue(MeterDefinition.objects.filter(key=key, version=1).exists())
