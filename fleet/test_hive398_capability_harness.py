"""
HIVE-398 capability harness tests — HIVE-369 QA spec cases 1–5.

Tests the pure compute_effective_capability_grant() function directly (no HTTP
request, no real FleetAgent) and verifies the verify_capability_grants management
command produces the expected machine-readable output.
"""
import json

from django.test import TestCase
from django.db import transaction

from fleet.effective_capabilities import compute_effective_capability_grant
from fleet.models import (
    BASE_PLATFORM_MCP_SERVERS,
    BASE_PLATFORM_SKILLS,
    CapabilityDefinition,
    TeamRequiredCapability,
    ensure_team_required_capability,
)
from hive_platform.models import BillingPlan, TenantSubscription


def _mk_cap(slug, skill_refs=None, mcp_server_refs=None, sensitive_access_flags=None,
            risk_tier=None, security_reviewed=False):
    """Helper: create an ACTIVE CapabilityDefinition for tests."""
    is_privileged = bool(sensitive_access_flags) or risk_tier in {
        CapabilityDefinition.RiskTier.SENSITIVE,
        CapabilityDefinition.RiskTier.PRIVILEGED,
    }
    from django.utils import timezone
    cap = CapabilityDefinition(
        slug=slug,
        display_name=slug.replace('-', ' ').title(),
        lifecycle_status=CapabilityDefinition.LifecycleStatus.DRAFT,
        activation_status=CapabilityDefinition.ActivationStatus.ACTIVE,
        risk_tier=risk_tier or CapabilityDefinition.RiskTier.STANDARD,
        skill_refs=skill_refs or [],
        mcp_server_refs=mcp_server_refs or [],
        sensitive_access_flags=sensitive_access_flags or [],
        created_by='test',
        updated_by='test',
        approved_by='test',
        approved_at=timezone.now(),
    )
    if is_privileged:
        cap.security_reviewed_by = 'test'
        cap.security_reviewed_at = timezone.now()
    cap.save()
    return cap


def _mk_req(team_slug, cap):
    """Helper: create an enabled, required TeamRequiredCapability."""
    req, _ = TeamRequiredCapability.objects.update_or_create(
        team_slug=team_slug,
        capability=cap,
        defaults={'required': True, 'enabled': True, 'reason': 'test', 'created_by': 'test'},
    )
    return req


class Case1FreshTenantGrant(TestCase):
    """Case 1: fresh-tenant TeamRequiredCapability rows fire for a provisioned team."""

    def test_engineering_team_provisioned_skills(self):
        cap, req = ensure_team_required_capability('engineering', actor='test')
        grant = compute_effective_capability_grant(['engineering'])
        self.assertIn('engineering', [c['slug'] for c in grant['capabilities']])
        # engineering-core and shipping-prs must be present per DEFAULT_TEAM_CAPABILITY_SPECS
        self.assertIn('engineering-core', grant['skills'])
        self.assertIn('shipping-prs', grant['skills'])

    def test_engineering_team_has_pulse_mcp(self):
        ensure_team_required_capability('engineering', actor='test')
        grant = compute_effective_capability_grant(['engineering'])
        self.assertIn('pulse', grant['enabled_mcp_servers'])

    def test_team_required_capability_row_created(self):
        cap, req = ensure_team_required_capability('qa', actor='test')
        self.assertTrue(req.required)
        self.assertTrue(req.enabled)
        self.assertEqual(req.team_slug, 'qa')
        self.assertEqual(req.capability.slug, 'qa')

    def test_multi_team_union(self):
        ensure_team_required_capability('engineering', actor='test')
        ensure_team_required_capability('qa', actor='test')
        grant = compute_effective_capability_grant(['engineering', 'qa'])
        slugs = [c['slug'] for c in grant['capabilities']]
        self.assertIn('engineering', slugs)
        self.assertIn('qa', slugs)
        # Skills are the union of both teams
        self.assertIn('engineering-core', grant['skills'])
        self.assertIn('qa-verification', grant['skills'])


class Case2SensitiveFlagGating(TestCase):
    """Case 2 (HIVE-446): sensitive_access_flags are inherited by INTERNAL agents but
    tenant-tier-GATED for customer tenants (default-deny unless the plan allows)."""

    def test_devops_sensitive_flags_present(self):
        """Internal/platform agent (no tenant scope) still inherits the full team spec."""
        ensure_team_required_capability('devops', actor='test')
        grant = compute_effective_capability_grant(['devops'])
        for flag in ('host_exec', 'credential_broker', 'kubeconfig'):
            self.assertIn(flag, grant['sensitive_access_flags'],
                          f'Expected internal devops grant to carry {flag!r} — '
                          'internal behaviour must be unchanged by the tenant gate.')
        self.assertFalse(grant['tenant_tier_gated'])

    def test_sensitive_flags_surfaced_per_capability(self):
        """sensitive_access_flags are exposed at per-capability granularity too."""
        ensure_team_required_capability('devops', actor='test')
        grant = compute_effective_capability_grant(['devops'])
        devops_cap = next(c for c in grant['capabilities'] if c['slug'] == 'devops')
        self.assertIn('host_exec', devops_cap['sensitive_access_flags'])

    def test_standard_team_no_sensitive_flags(self):
        ensure_team_required_capability('engineering', actor='test')
        grant = compute_effective_capability_grant(['engineering'])
        self.assertEqual(grant['sensitive_access_flags'], [],
                         'Standard-risk engineering team must carry no sensitive flags.')

    # ── HIVE-446 tenant-tier gate ────────────────────────────────────────────
    def _customer_with_plan(self, slug, *, flags=None, scopes=None):
        plan = BillingPlan.objects.create(
            plan_id=f'plan-{slug}',
            name=f'Plan {slug}',
            allowed_sensitive_access_flags=flags or [],
            allowed_credential_grant_scopes=scopes or [],
        )
        TenantSubscription.objects.create(
            tenant_org_id=f'org-{slug}',
            tenant_org_slug=slug,
            plan=plan,
            status=TenantSubscription.Status.ACTIVE,
        )
        return plan

    def test_customer_tenant_devops_flags_withheld(self):
        """A customer tenant on a default-deny plan inherits NONE of the privileged flags/scopes."""
        ensure_team_required_capability('devops', actor='test')
        self._customer_with_plan('cust-deny')  # empty allowlists
        grant = compute_effective_capability_grant(['devops'], tenant_org_slug='cust-deny')
        self.assertTrue(grant['tenant_tier_gated'])
        for flag in ('host_exec', 'credential_broker', 'kubeconfig'):
            self.assertNotIn(flag, grant['sensitive_access_flags'],
                             f'{flag!r} must be withheld from a customer tenant with no plan entitlement.')
        self.assertEqual(grant['sensitive_access_flags'], [])
        self.assertEqual(grant['credential_grant_scopes'], [],
                         'kubeconfig credential scope must also be withheld (default-deny).')
        self.assertIn('host_exec', grant['withheld_sensitive_access_flags'])
        self.assertIn('kubeconfig', grant['withheld_credential_grant_scopes'])
        # Per-capability view must not advertise a withheld flag either.
        devops_cap = next(c for c in grant['capabilities'] if c['slug'] == 'devops')
        self.assertEqual(devops_cap['sensitive_access_flags'], [])

    def test_customer_tenant_no_subscription_defaults_deny(self):
        """Unknown tenant (no subscription/plan) → default-deny, not full inheritance."""
        ensure_team_required_capability('devops', actor='test')
        grant = compute_effective_capability_grant(['devops'], tenant_org_slug='ghost-tenant')
        self.assertTrue(grant['tenant_tier_gated'])
        self.assertEqual(grant['sensitive_access_flags'], [])
        self.assertEqual(grant['credential_grant_scopes'], [])

    def test_customer_tenant_plan_allows_specific_flag(self):
        """Only the flags/scopes the plan lists pass the gate; the rest are withheld."""
        ensure_team_required_capability('devops', actor='test')
        self._customer_with_plan('cust-partial', flags=['host_exec'], scopes=['kubeconfig'])
        grant = compute_effective_capability_grant(['devops'], tenant_org_slug='cust-partial')
        self.assertEqual(grant['sensitive_access_flags'], ['host_exec'])
        self.assertNotIn('credential_broker', grant['sensitive_access_flags'])
        self.assertNotIn('kubeconfig', grant['sensitive_access_flags'])
        self.assertEqual(grant['credential_grant_scopes'], ['kubeconfig'])
        self.assertIn('credential_broker', grant['withheld_sensitive_access_flags'])

    def test_cancelled_subscription_defaults_deny(self):
        """A cancelled subscription confers no entitlement → default-deny."""
        ensure_team_required_capability('devops', actor='test')
        plan = BillingPlan.objects.create(
            plan_id='plan-cancelled', name='Cancelled plan',
            allowed_sensitive_access_flags=['host_exec', 'credential_broker', 'kubeconfig'],
            allowed_credential_grant_scopes=['kubeconfig'],
        )
        TenantSubscription.objects.create(
            tenant_org_id='org-cancelled', tenant_org_slug='cust-cancelled',
            plan=plan, status=TenantSubscription.Status.CANCELLED,
        )
        grant = compute_effective_capability_grant(['devops'], tenant_org_slug='cust-cancelled')
        self.assertEqual(grant['sensitive_access_flags'], [])
        self.assertEqual(grant['credential_grant_scopes'], [])

    def test_suspended_and_past_due_subscriptions_default_deny(self):
        """Suspended/past-due tenants confer no privileged entitlement."""
        ensure_team_required_capability('devops', actor='test')
        for status in (TenantSubscription.Status.SUSPENDED, TenantSubscription.Status.PAST_DUE):
            with self.subTest(status=status):
                slug = f'cust-{status}'
                plan = BillingPlan.objects.create(
                    plan_id=f'plan-{status}', name=f'Plan {status}',
                    allowed_sensitive_access_flags=['host_exec', 'credential_broker', 'kubeconfig'],
                    allowed_credential_grant_scopes=['kubeconfig'],
                )
                TenantSubscription.objects.create(
                    tenant_org_id=f'org-{status}', tenant_org_slug=slug,
                    plan=plan, status=status,
                )

                grant = compute_effective_capability_grant(['devops'], tenant_org_slug=slug)

                self.assertEqual(grant['sensitive_access_flags'], [])
                self.assertEqual(grant['credential_grant_scopes'], [])

    def test_inactive_plan_defaults_deny(self):
        """An inactive plan confers no privileged entitlement even on an active subscription."""
        ensure_team_required_capability('devops', actor='test')
        plan = BillingPlan.objects.create(
            plan_id='plan-inactive', name='Inactive plan', active=False,
            allowed_sensitive_access_flags=['host_exec', 'credential_broker', 'kubeconfig'],
            allowed_credential_grant_scopes=['kubeconfig'],
        )
        TenantSubscription.objects.create(
            tenant_org_id='org-inactive', tenant_org_slug='cust-inactive',
            plan=plan, status=TenantSubscription.Status.ACTIVE,
        )

        grant = compute_effective_capability_grant(['devops'], tenant_org_slug='cust-inactive')

        self.assertEqual(grant['sensitive_access_flags'], [])
        self.assertEqual(grant['credential_grant_scopes'], [])


class Case3CustomCapability(TestCase):
    """Case 3: a novel slug + skill_refs is inherited by an agent on the team."""

    def test_custom_capability_skills_inherited(self):
        cap = _mk_cap('custom-hive398-xyz', skill_refs=['custom-skill-a', 'custom-skill-b'])
        _mk_req('engineering', cap)
        ensure_team_required_capability('engineering', actor='test')

        grant = compute_effective_capability_grant(['engineering'])
        self.assertIn('custom-skill-a', grant['skills'])
        self.assertIn('custom-skill-b', grant['skills'])

    def test_custom_capability_slug_in_capabilities(self):
        cap = _mk_cap('custom-hive398-abc', skill_refs=['skill-x'])
        _mk_req('qa', cap)
        ensure_team_required_capability('qa', actor='test')

        grant = compute_effective_capability_grant(['qa'])
        slugs = [c['slug'] for c in grant['capabilities']]
        self.assertIn('custom-hive398-abc', slugs)

    def test_teardown_is_clean(self):
        """Creating then deleting a capability leaves no grant artifacts."""
        cap = _mk_cap('custom-hive398-teardown', skill_refs=['teardown-skill'])
        _mk_req('engineering', cap)
        ensure_team_required_capability('engineering', actor='test')

        grant_before = compute_effective_capability_grant(['engineering'])
        self.assertIn('teardown-skill', grant_before['skills'])

        # Simulate teardown by disabling the capability
        cap.activation_status = CapabilityDefinition.ActivationStatus.DISABLED
        cap.save()

        grant_after = compute_effective_capability_grant(['engineering'])
        self.assertNotIn('teardown-skill', grant_after['skills'])


class Case4TenantScopedMcp(TestCase):
    """Case 4: customer-tenant MCP refs are explicitly tenant-scoped."""

    def test_internal_mcp_refs_remain_global_slugs(self):
        """Internal/platform grants preserve the historical bare-slug contract."""
        ensure_team_required_capability('engineering', actor='test')
        grant = compute_effective_capability_grant(['engineering'])
        self.assertEqual(grant['mcp_ref_scope'], '')
        self.assertTrue(grant['enabled_mcp_servers'])
        for ref in grant['enabled_mcp_servers']:
            self.assertNotIn(':', ref)
            self.assertNotIn('/', ref)

    def test_customer_tenant_mcp_refs_are_namespaced(self):
        """Customer-tenant grants carry a namespace so daemon/broker cannot resolve platform-global endpoints."""
        tenant = 'hive398-tenant'
        ensure_team_required_capability('engineering', actor='test')
        grant = compute_effective_capability_grant(['engineering'], tenant_org_slug=tenant)
        self.assertEqual(grant['mcp_ref_scope'], tenant)
        self.assertTrue(grant['enabled_mcp_servers'])
        self.assertTrue(
            all(ref.endswith(f':{tenant}') for ref in grant['enabled_mcp_servers']),
            f'Expected every MCP ref to be scoped to {tenant}: {grant["enabled_mcp_servers"]!r}',
        )
        self.assertIn(f'pulse:{tenant}', grant['enabled_mcp_servers'])


class Case5NegativeSafeDefault(TestCase):
    """Case 5: team with no capability row gets no extra grant beyond the platform MCP + skill floor."""

    def test_no_capability_team_empty_grant(self):
        grant = compute_effective_capability_grant(['orphan-team-no-cap'])
        self.assertEqual(grant['capabilities'], [])
        self.assertEqual(grant['skills'], sorted(BASE_PLATFORM_SKILLS))
        self.assertEqual(grant['eager_skills'], sorted(BASE_PLATFORM_SKILLS))
        self.assertEqual(grant['enabled_mcp_servers'], sorted(BASE_PLATFORM_MCP_SERVERS))
        self.assertEqual(grant['base_mcp_missing_pre_floor'], sorted(BASE_PLATFORM_MCP_SERVERS))
        self.assertEqual(grant['base_skills_missing_pre_floor'], sorted(BASE_PLATFORM_SKILLS))
        self.assertEqual(grant['sensitive_access_flags'], [])

    def test_empty_team_list_empty_grant(self):
        grant = compute_effective_capability_grant([])
        self.assertEqual(grant['capabilities'], [])
        self.assertEqual(grant['skills'], sorted(BASE_PLATFORM_SKILLS))
        self.assertEqual(grant['eager_skills'], sorted(BASE_PLATFORM_SKILLS))
        self.assertEqual(grant['enabled_mcp_servers'], sorted(BASE_PLATFORM_MCP_SERVERS))

    def test_merge_capability_still_includes_heartbeat_floor(self):
        ensure_team_required_capability('merge', actor='test')
        grant = compute_effective_capability_grant(['merge'])
        self.assertIn('pulse-merge', grant['skills'])
        self.assertIn('heartbeat-protocol', grant['skills'])
        self.assertIn('heartbeat-protocol', grant['eager_skills'])
        self.assertEqual(grant['base_skills_missing_pre_floor'], ['heartbeat-protocol'])

    def test_disabled_requirement_excluded(self):
        cap = _mk_cap('disabled-req-cap', skill_refs=['secret-skill'])
        TeamRequiredCapability.objects.create(
            team_slug='test-disabled-team',
            capability=cap,
            required=True,
            enabled=False,
            reason='disabled for test',
            created_by='test',
        )
        grant = compute_effective_capability_grant(['test-disabled-team'])
        self.assertNotIn('secret-skill', grant['skills'],
                         'Disabled TeamRequiredCapability must not contribute to the grant.')

    def test_inactive_capability_excluded(self):
        cap = _mk_cap('inactive-cap', skill_refs=['inactive-skill'])
        _mk_req('test-inactive-team', cap)
        cap.activation_status = CapabilityDefinition.ActivationStatus.DISABLED
        cap.save()
        grant = compute_effective_capability_grant(['test-inactive-team'])
        self.assertNotIn('inactive-skill', grant['skills'],
                         'Inactive CapabilityDefinition must not contribute to the grant.')


class ManagementCommandHarness(TestCase):
    """Verify the management command runs atomically and produces valid JSON."""

    def test_command_dry_run_produces_valid_json(self):
        from io import StringIO
        from django.core.management import call_command

        out = StringIO()
        call_command('verify_capability_grants', dry_run=True, stdout=out, stderr=StringIO())
        result = json.loads(out.getvalue())

        self.assertEqual(result['harness'], 'HIVE-398')
        self.assertIn('cases', result)
        cases = result['cases']
        for key in ('case_1_fresh_tenant_grant', 'case_2_sensitive_flag_gating',
                    'case_3_custom_capability', 'case_4_tenant_scoped_mcp',
                    'case_5_negative_safe_default'):
            self.assertIn(key, cases, f'Expected case key {key!r} in harness output')

    def test_command_dry_run_rolls_back(self):
        from io import StringIO
        from django.core.management import call_command

        before_count = CapabilityDefinition.objects.count()
        call_command('verify_capability_grants', dry_run=True, stdout=StringIO(), stderr=StringIO())
        after_count = CapabilityDefinition.objects.count()
        self.assertEqual(before_count, after_count,
                         'Dry-run must not leave permanent CapabilityDefinition rows.')

    def test_command_summary_all_cases_pass(self):
        from io import StringIO
        from django.core.management import call_command

        out = StringIO()
        call_command('verify_capability_grants', dry_run=True, stdout=out, stderr=StringIO())
        result = json.loads(out.getvalue())

        summary = result['summary']
        self.assertTrue(summary['case_2_pass'],
                        'Case 2 (HIVE-446 tenant-tier gating) must now pass — customer flags withheld.')
        self.assertTrue(summary['case_4_pass'], 'Case 4 (tenant-scoped MCP) must pass after HIVE-447')
        self.assertTrue(summary['case_1_pass'], 'Case 1 (fresh-tenant grant) must pass')
        self.assertTrue(summary['case_3_pass'], 'Case 3 (custom capability) must pass')
        self.assertTrue(summary['case_5_pass'], 'Case 5 (safe default) must pass')
        self.assertEqual(summary['gaps'], [],
                         'No open security gaps once case_2 tenant-tier gating passes.')
        # Case 2 detail must show the customer tenant withheld the privileged flags.
        case2 = result['cases']['case_2_sensitive_flag_gating']
        self.assertTrue(case2['customer_flags_withheld'])
        self.assertEqual(case2['customer_devops_sensitive_flags'], [])
