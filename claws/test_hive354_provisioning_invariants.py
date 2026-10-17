"""HIVE-354 adversarial tests: tenant-provisioning isolation invariant gate.

Covers the acceptance matrix: malicious/cross-org refs, missing namespace
isolation, shared/internal credential grant attempts, wrong-org IDs, and
unverified/missing refs — all must fail closed. A full set of verified, same-org,
isolated refs is the only way a non-dry-run run may reach READY.
"""
from django.test import TestCase

from .models import TenantProvisioningRun
from .provisioning_invariants import (
    TenantIsolationInvariantError,
    assert_ready_invariants,
    verify_resource_ref,
    verify_step_result,
)
from .tenant_provisioning import advance_preview, create_or_get_run, initial_step_state

ORG = 'acme'


def _good_refs(org=ORG):
    """A full set of verified, same-org, isolated resource refs."""
    return {
        'organization': {'verified': True, 'external_org_id': 'org_123', 'org_slug': org},
        'pulse': {'verified': True, 'project_id': 'PRJ1', 'org_slug': org},
        'wiki': {'verified': True, 'space_id': 'sp_1', 'org_slug': org},
        'connect': {'verified': True, 'workspace_id': 'ws_1', 'org_slug': org},
        'drive': {'verified': True, 'root_id': 'root_1', 'org_slug': org},
        'kubernetes': {'verified': True, 'namespace': f'tenant-{org}', 'org_slug': org,
                       'network_policy_isolated': True},
        'fleet': {'verified': True, 'agent_identities': ['ag_1', 'ag_2'], 'org_slug': org},
        'cortex': {'verified': True, 'tenant_key_id': 'ck_1', 'org_slug': org},
        'seed_task': {'verified': True, 'task_id': 'T-1', 'org_slug': org},
    }


class Hive354InvariantTests(TestCase):
    def _run(self, *, dry_run=False, org=ORG, refs=None, key='k1'):
        return TenantProvisioningRun.objects.create(
            idempotency_key=key, requester_user_id=1, org_name=org.title(),
            org_slug=org, template_slug='startup', dry_run=dry_run,
            step_state=initial_step_state(), resource_refs=refs or {},
        )

    # -- READY gate ----------------------------------------------------------
    def test_ready_passes_with_full_verified_same_org_refs(self):
        run = self._run(refs=_good_refs())
        assert_ready_invariants(run)  # must not raise

    def test_dry_run_is_exempt_from_invariant_gate(self):
        run = self._run(dry_run=True, refs={})
        assert_ready_invariants(run)  # no real resources expected

    def test_missing_refs_fail_closed(self):
        run = self._run(refs={})
        with self.assertRaises(TenantIsolationInvariantError) as ctx:
            assert_ready_invariants(run)
        self.assertTrue(ctx.exception.violations)
        # Fail-loud audit signal recorded.
        self.assertTrue(any(e.get('event') == 'ready_invariant_violation' and e.get('security_sensitive')
                            for e in run.audit_events))

    def test_unverified_preview_refs_fail_closed(self):
        # Synthesized preview refs (no verified=True) must not satisfy a real run.
        refs = {k: {kk: vv for kk, vv in v.items() if kk != 'verified'} for k, v in _good_refs().items()}
        run = self._run(refs=refs)
        with self.assertRaises(TenantIsolationInvariantError):
            assert_ready_invariants(run)

    # -- Per-ref adversarial cases -------------------------------------------
    def test_cross_org_ref_rejected(self):
        run = self._run()
        bad = {'verified': True, 'space_id': 'sp_9', 'org_slug': 'evilcorp'}
        v = verify_resource_ref(run, 'wiki', 'space_id', bad)
        self.assertTrue(any('cross-org' in x for x in v))

    def test_shared_internal_credential_marker_rejected(self):
        run = self._run()
        bad = {'verified': True, 'tenant_key_id': 'ck', 'org_slug': ORG, 'scope': 'internal-shared'}
        v = verify_resource_ref(run, 'cortex', 'tenant_key_id', bad)
        self.assertTrue(any('forbidden shared/internal scope' in x for x in v))

    def test_hyphenated_marker_is_matched(self):
        # HIVE-354 review: hyphenated multi-word markers must match despite the
        # loose tokenizer splitting on '-'. Both cross-org and cluster-admin.
        run = self._run()
        for marker in ('cross-org', 'cluster-admin'):
            bad = {'verified': True, 'tenant_key_id': 'k', 'org_slug': ORG, 'scope': marker}
            v = verify_resource_ref(run, 'cortex', 'tenant_key_id', bad)
            self.assertTrue(any('forbidden shared/internal scope' in x for x in v), marker)

    def test_wrong_namespace_rejected(self):
        run = self._run()
        bad = {'verified': True, 'namespace': 'default', 'org_slug': ORG, 'network_policy_isolated': True}
        v = verify_resource_ref(run, 'kubernetes', 'namespace', bad)
        self.assertTrue(any('isolated tenant namespace' in x for x in v))

    def test_missing_network_isolation_rejected(self):
        run = self._run()
        bad = {'verified': True, 'namespace': f'tenant-{ORG}', 'org_slug': ORG}
        v = verify_resource_ref(run, 'kubernetes', 'namespace', bad)
        self.assertTrue(any('network-policy isolated' in x for x in v))

    def test_missing_external_id_rejected(self):
        run = self._run()
        bad = {'verified': True, 'org_slug': ORG}  # no project_id
        v = verify_resource_ref(run, 'pulse', 'project_id', bad)
        self.assertTrue(any('missing real project_id' in x for x in v))

    # -- Drive isolation (review P1) ----------------------------------------
    def test_missing_drive_ref_fails_closed(self):
        refs = _good_refs()
        del refs['drive']
        run = self._run(refs=refs)
        with self.assertRaises(TenantIsolationInvariantError) as ctx:
            assert_ready_invariants(run)
        self.assertTrue(any('drive' in v for v in ctx.exception.violations))

    def test_wrong_org_drive_rejected(self):
        run = self._run()
        bad = {'verified': True, 'root_id': 'r9', 'org_slug': 'evilcorp'}
        v = verify_resource_ref(run, 'drive', 'root_id', bad)
        self.assertTrue(any('cross-org' in x for x in v))

    def test_shared_internal_drive_marker_rejected(self):
        run = self._run()
        bad = {'verified': True, 'root_id': 'r', 'org_slug': ORG, 'acl': 'platform-shared'}
        v = verify_resource_ref(run, 'drive', 'root_id', bad)
        self.assertTrue(any('forbidden shared/internal scope' in x for x in v))

    def test_connect_drive_step_requires_both_resources(self):
        run = self._run()
        out = verify_step_result(run, 'connect_drive', {
            'connect': {'workspace_id': 'ws', 'org_slug': ORG},
            'drive': {'root_id': 'r', 'org_slug': ORG},
        })
        self.assertTrue(out['connect']['verified'] and out['drive']['verified'])
        # Missing Drive in the connect_drive result fails closed.
        with self.assertRaises(TenantIsolationInvariantError):
            verify_step_result(run, 'connect_drive', {'connect': {'workspace_id': 'ws', 'org_slug': ORG}})

    # -- Fail-loud durability (review P2) -----------------------------------
    def test_step_violation_audit_is_persisted(self):
        run = self._run(key='persist1')
        with self.assertRaises(TenantIsolationInvariantError):
            verify_step_result(run, 'id_org', {'external_org_id': 'x', 'org_slug': 'other'})
        # The security-sensitive signal must survive the exception path durably.
        run.refresh_from_db()
        self.assertTrue(any(
            e.get('event') == 'step_invariant_violation' and e.get('security_sensitive')
            for e in run.audit_events
        ))

    # -- Adapter entrypoint --------------------------------------------------
    def test_verify_step_result_stamps_and_accepts_good_ref(self):
        run = self._run()
        out = verify_step_result(run, 'wiki', {'space_id': 'sp_1', 'org_slug': ORG})
        self.assertTrue(out['verified'])

    def test_verify_step_result_raises_on_cross_org(self):
        run = self._run()
        with self.assertRaises(TenantIsolationInvariantError):
            verify_step_result(run, 'id_org', {'external_org_id': 'x', 'org_slug': 'other'})
        self.assertTrue(any(e.get('event') == 'step_invariant_violation' for e in run.audit_events))

    def test_verify_step_result_passthrough_for_non_resource_step(self):
        run = self._run()
        self.assertEqual(verify_step_result(run, 'validate_request', {'ok': True}), {'ok': True})

    # -- Saga wiring ---------------------------------------------------------
    def test_non_dry_run_cannot_reach_ready_via_saga(self):
        # The saga still stops non-dry-run at manual_review (no adapters), never READY.
        run, _ = create_or_get_run(requester_user_id=1, org_name='Zeta', org_slug='zeta',
                                   template_slug='startup', dry_run=False, idempotency_key='z1')
        advanced = advance_preview(run, allow_side_effects=True)
        self.assertNotEqual(advanced.status, TenantProvisioningRun.Status.READY)

    def test_dry_run_still_reaches_ready(self):
        run, _ = create_or_get_run(requester_user_id=1, org_name='Zeta', org_slug='zeta',
                                   template_slug='startup', dry_run=True, idempotency_key='z2')
        advanced = advance_preview(run)
        self.assertEqual(advanced.status, TenantProvisioningRun.Status.READY)
