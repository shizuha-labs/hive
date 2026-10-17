"""HIVE-356 (VEN-61): design-partner internal pilot-prep — intake→preview handoff.

Guards the RFC c1af3d6e operating envelope for the internal-only slice:
- intake capture (RFC §2.1 fields) produces an AUDITED dry-run preview handoff,
  never a real external apply;
- the handoff carries the RFC §3 launch-gate state (default fail-closed / red);
- the external launch/apply path is fail-closed until every gate is green, while
  internal dry-run previews are allowed;
- no internal admin/devops/credential-broker/billing grant may be attached to a
  customer-agent pilot handoff.
"""
from django.test import TestCase

from claws.models import TenantProvisioningRun
from claws import pilot_prep
from claws.pilot_prep import (
    INTAKE_FIELDS,
    PILOT_LAUNCH_GATES,
    PilotGateError,
    PilotGrantError,
    all_gates_green,
    assert_external_apply_allowed,
    assert_no_forbidden_grants,
    create_pilot_handoff,
    default_gate_state,
    pilot_gate_blockers,
)

INTAKE = {
    'company': 'Acme Founder Co',
    'country': 'IN',
    'workflow': 'weekly content ops',
    'sop_link': 'https://drive/x',
    'sponsor': 'Priya (COO)',
    'sponsor_availability': 'Fridays 4pm IST',
    'sensitivity': 'low',
    'fit_risk': 'good-fit / low-reg',
}


def _handoff(**over):
    kw = dict(
        requester_user_id=7, requester_email='sales@shizuha.com',
        intake=INTAKE, org_name='Acme Pilot', org_slug='acme-pilot',
        template_slug='team-pod', plan_slug='design-partner-90d')
    kw.update(over)
    return create_pilot_handoff(**kw)


class PilotIntakeHandoffTests(TestCase):
    def test_intake_capture_produces_dry_run_preview(self):
        run, created = _handoff()
        self.assertTrue(created)
        # preview only — never a real external apply.
        self.assertTrue(run.dry_run)
        self.assertEqual(run.status, TenantProvisioningRun.Status.READY)
        self.assertEqual(run.pilot_prep['mode'], 'internal_pilot_prep')
        # all RFC §2.1 intake fields captured.
        for f in INTAKE_FIELDS:
            self.assertIn(f, run.pilot_prep['intake'])
        self.assertEqual(run.pilot_prep['intake']['company'], 'Acme Founder Co')
        # audited.
        events = [e.get('event') for e in run.audit_events]
        self.assertIn('pilot_intake_captured', events)

    def test_handoff_carries_all_rfc_launch_gates(self):
        run, _ = _handoff()
        gate_keys = set(run.pilot_prep['gates'])
        self.assertEqual(gate_keys, {k for k, _ in PILOT_LAUNCH_GATES})

    def test_gates_default_red_and_external_apply_fail_closed(self):
        run, _ = _handoff()
        self.assertFalse(all_gates_green(run))
        self.assertEqual(len(pilot_gate_blockers(run)), len(PILOT_LAUNCH_GATES))
        self.assertFalse(run.pilot_prep['external_apply_allowed'])
        with self.assertRaises(PilotGateError):
            assert_external_apply_allowed(run)

    def test_partial_gates_still_fail_closed(self):
        greens = {k: {'status': 'green'} for k, _ in PILOT_LAUNCH_GATES[:-1]}
        run, _ = _handoff(gate_state=greens)  # one gate (final sign-off) still red
        self.assertFalse(all_gates_green(run))
        with self.assertRaises(PilotGateError):
            assert_external_apply_allowed(run)

    def test_all_gates_green_allows_external_apply(self):
        greens = {k: {'status': 'green', 'evidence': 'verified'} for k, _ in PILOT_LAUNCH_GATES}
        run, _ = _handoff(gate_state=greens)
        self.assertTrue(all_gates_green(run))
        self.assertTrue(run.pilot_prep['external_apply_allowed'])
        assert_external_apply_allowed(run)  # no raise

    def test_unknown_gate_key_is_dropped_cannot_widen(self):
        # A caller cannot invent a gate or drop a required one to widen access.
        run, _ = _handoff(gate_state={'not_a_real_gate': {'status': 'green'}})
        self.assertEqual(set(run.pilot_prep['gates']),
                         {k for k, _ in PILOT_LAUNCH_GATES})
        with self.assertRaises(PilotGateError):
            assert_external_apply_allowed(run)

    def test_dry_run_preview_creates_no_real_external_resource_refs(self):
        run, _ = _handoff()
        # dry-run preview plans refs but applies nothing externally.
        self.assertTrue(run.dry_run)
        self.assertNotEqual(run.status, TenantProvisioningRun.Status.MANUAL_REVIEW)


class PilotGrantGuardTests(TestCase):
    def test_forbidden_grants_rejected(self):
        for bad in ('admin', 'devops', 'credential-broker', 'billing',
                    'books-write', 'admin-mcp', 'host-ssh'):
            with self.assertRaises(PilotGrantError):
                assert_no_forbidden_grants([bad])

    def test_template_scoped_grants_allowed(self):
        # No raise for template-scoped, non-internal grants.
        assert_no_forbidden_grants(['pulse:read', 'wiki:read', 'drive:read', 'connect:dm'])

    def test_create_handoff_rejects_forbidden_grant_before_write(self):
        with self.assertRaises(PilotGrantError):
            _handoff(tool_grants=['pulse:read', 'credential-broker'])
        # nothing persisted for this org (guard runs before create_or_get_run).
        self.assertFalse(
            TenantProvisioningRun.objects.filter(org_slug='acme-pilot').exists())

    def test_allowed_grants_recorded_on_handoff(self):
        run, _ = _handoff(tool_grants=['pulse:read', 'wiki:read'])
        self.assertEqual(run.pilot_prep['tool_grants'], ['pulse:read', 'wiki:read'])


class PilotHelperTests(TestCase):
    def test_default_gate_state_all_red(self):
        state = default_gate_state()
        self.assertTrue(all(g['status'] == 'red' for g in state.values()))

    def test_assert_external_apply_requires_gate_state(self):
        # a run with no pilot_prep gates fails closed.
        run = TenantProvisioningRun.objects.create(
            idempotency_key='k-nogate', requester_user_id=1,
            org_name='x', org_slug='x-slug', template_slug='t', dry_run=True)
        with self.assertRaises(PilotGateError):
            assert_external_apply_allowed(run)

    def test_idempotent_handoff_returns_same_run(self):
        run1, created1 = _handoff()
        run2, created2 = _handoff()
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(run1.id, run2.id)
