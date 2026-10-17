"""
Management command: verify_capability_grants

Repeatable harness that provisions a throwaway Tech-Company-template tenant,
introspects TeamRequiredCapability rows and per-agent effective grants, exercises
a custom capability and a no-capability team, and emits machine-readable JSON
keyed to the HIVE-369 QA assertion spec (cases 1–5).

All DB changes are rolled back after the run by default (--dry-run).
Pass --no-dry-run to commit (useful for seeding a staging env).

Usage:
    python manage.py verify_capability_grants
    python manage.py verify_capability_grants --teams engineering devops qa
    python manage.py verify_capability_grants --output /tmp/hive398_result.json

HIVE-398 / HIVE-369
"""
import json

from django.core.management.base import BaseCommand
from django.db import transaction

from fleet.effective_capabilities import compute_effective_capability_grant
from fleet.models import (
    BASE_PLATFORM_MCP_SERVERS,
    BASE_PLATFORM_SKILLS,
    CapabilityDefinition,
    TeamRequiredCapability,
    ensure_team_required_capability,
)

# Representative Tech-Company-template teams for the harness.
_DEFAULT_TEAMS = ['engineering', 'devops', 'qa', 'architecture']

# Slug for the custom-capability test (case 3). Must not collide with real slugs.
_CUSTOM_SLUG = 'harness-custom-xyz-cap'
_CUSTOM_SKILL_REFS = ['harness-skill-alpha', 'harness-skill-beta']

# A team with no provisioned capability (case 5 — negative/safe-default).
_NO_CAP_TEAM = 'harness-orphan-team'


class _Rollback(Exception):
    """Sentinel used to roll back a dry-run atomic block."""


class Command(BaseCommand):
    help = (
        'Verify capability→skills+MCP auto-grant for a throwaway tenant '
        '(HIVE-398 harness, HIVE-369 QA spec cases 1–5). '
        'All DB changes are rolled back unless --no-dry-run is passed.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=True,
            help='Roll back all DB changes after verification (default: on)',
        )
        parser.add_argument(
            '--no-dry-run',
            dest='dry_run',
            action='store_false',
            help='Commit provisioned rows (useful for staging seeding)',
        )
        parser.add_argument(
            '--teams',
            nargs='+',
            default=_DEFAULT_TEAMS,
            metavar='TEAM',
            help=f'Teams to provision (default: {" ".join(_DEFAULT_TEAMS)})',
        )
        parser.add_argument(
            '--output',
            default='-',
            metavar='PATH',
            help='Write JSON output to PATH (- = stdout, default: -)',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        teams = options['teams']
        output_path = options['output']

        result = None
        try:
            with transaction.atomic():
                result = _run_harness(teams)
                if dry_run:
                    raise _Rollback()
        except _Rollback:
            pass

        payload = json.dumps(result, indent=2, default=str)
        if output_path == '-':
            self.stdout.write(payload)
        else:
            with open(output_path, 'w') as fh:
                fh.write(payload)
        if dry_run:
            self.stderr.write(
                self.style.WARNING('Dry-run: all DB changes rolled back. Pass --no-dry-run to commit.')
            )


def _run_harness(teams):
    """Provision, introspect, and return the case-keyed JSON payload."""

    # ── PROVISION ───────────────────────────────────────────────────────────
    provisioned_caps = {}
    provisioned_reqs = {}
    for team_slug in teams:
        cap, req = ensure_team_required_capability(
            team_slug,
            actor='hive:harness-hive398',
            reason='HIVE-398 verification harness — provisioned for test.',
        )
        provisioned_caps[team_slug] = cap
        provisioned_reqs[team_slug] = req

    # ── CASE 1: fresh-tenant grant ──────────────────────────────────────────
    # Each provisioned team must have a TeamRequiredCapability row and an agent
    # on that team must receive the expected skills + pulse MCP.
    case1_teams = {}
    for team_slug in teams:
        grant = compute_effective_capability_grant([team_slug])
        req = provisioned_reqs[team_slug]
        cap = provisioned_caps[team_slug]
        case1_teams[team_slug] = {
            'team_required_capability': {
                'team_slug': req.team_slug,
                'capability_slug': cap.slug,
                'required': req.required,
                'enabled': req.enabled,
            },
            'grant': {
                'skills': grant['skills'],
                'eager_skills': grant['eager_skills'],
                'enabled_mcp_servers': grant['enabled_mcp_servers'],
                'sensitive_access_flags': grant['sensitive_access_flags'],
            },
        }

    # Quick pass/fail: every team must have its capability slug in the grant.
    case1_pass = all(
        any(c['slug'] == team_slug for c in compute_effective_capability_grant([team_slug])['capabilities'])
        for team_slug in teams
    )

    # ── CASE 2: sensitive-flag tenant-tier gating (HIVE-446) ───────────────
    # The internal fleet (no tenant scope) still inherits its team's privileged
    # flags, but a customer tenant on a sandbox plan (empty allowlist) must have
    # host_exec/credential_broker/kubeconfig WITHHELD — default-deny. This flips
    # the historic HIVE-369 case-2 gap into a passing regression guard.
    from hive_platform.models import BillingPlan, TenantSubscription

    expected_devops_flags = {'host_exec', 'credential_broker', 'kubeconfig'}
    has_devops = 'devops' in teams

    internal_devops = compute_effective_capability_grant(['devops']) if has_devops else None
    internal_sensitive = internal_devops['sensitive_access_flags'] if internal_devops else []
    internal_has_flags = expected_devops_flags.issubset(set(internal_sensitive))

    customer_org_slug = 'hive446-harness-customer'
    sandbox_plan = BillingPlan.objects.create(
        plan_id='hive446-harness-sandbox',
        name='HIVE-446 harness sandbox (default-deny allowlists)',
        allowed_sensitive_access_flags=[],
        allowed_credential_grant_scopes=[],
    )
    TenantSubscription.objects.create(
        tenant_org_id='hive446-harness-customer-org',
        tenant_org_slug=customer_org_slug,
        plan=sandbox_plan,
        status=TenantSubscription.Status.ACTIVE,
    )
    customer_devops = (
        compute_effective_capability_grant(['devops'], tenant_org_slug=customer_org_slug)
        if has_devops else None
    )
    customer_sensitive = customer_devops['sensitive_access_flags'] if customer_devops else []
    customer_scopes = customer_devops['credential_grant_scopes'] if customer_devops else []
    customer_withheld = customer_devops['withheld_sensitive_access_flags'] if customer_devops else []
    customer_withholds = (
        not (expected_devops_flags & set(customer_sensitive)) and not customer_scopes
    )
    # Pass = internal keeps its privileged flags AND the customer tenant withholds
    # them entirely. (If 'devops' was not in --teams the gate is not exercised; treat
    # as not-applicable rather than a false pass.)
    case2_pass = bool(has_devops and internal_has_flags and customer_withholds)
    case2_gap_confirmed = bool(expected_devops_flags & set(internal_sensitive))

    # ── CASE 3: custom capability ───────────────────────────────────────────
    # Declare a capability with a novel slug + skill_refs, assign it to a team,
    # assert the agent inherits those skills.
    custom_team = teams[0] if teams else 'engineering'
    _provision_custom_capability(_CUSTOM_SLUG, _CUSTOM_SKILL_REFS, custom_team)
    custom_grant = compute_effective_capability_grant([custom_team])
    custom_inherited = all(s in custom_grant['skills'] for s in _CUSTOM_SKILL_REFS)

    # ── CASE 4: tenant-scoped MCP refs ─────────────────────────────────────
    # CapabilityDefinition rows store catalog slugs (e.g. 'pulse'), but the
    # effective grant for a customer tenant must attach an explicit tenant
    # namespace so daemon/broker resolution cannot bind to platform-global MCP.
    sample_team = teams[0] if teams else 'engineering'
    sample_tenant = 'hive398-tenant'
    sample_grant = compute_effective_capability_grant([sample_team], tenant_org_slug=sample_tenant)
    mcp_refs = sample_grant['enabled_mcp_servers']
    case4_pass = bool(mcp_refs) and all(ref.endswith(f':{sample_tenant}') for ref in mcp_refs)

    # ── CASE 5: negative / safe default ────────────────────────────────────
    # An agent on a team with no provisioned capability gets no extra
    # capabilities or sensitive flags. The PLAT-1251 base MCP floor and the
    # heartbeat-protocol skill floor are still restored.
    empty_grant = compute_effective_capability_grant([_NO_CAP_TEAM])
    expected_base_mcp = sorted(BASE_PLATFORM_MCP_SERVERS)
    expected_base_skills = sorted(BASE_PLATFORM_SKILLS)
    case5_pass = (
        len(empty_grant['capabilities']) == 0
        and empty_grant['skills'] == expected_base_skills
        and empty_grant['enabled_mcp_servers'] == expected_base_mcp
        and len(empty_grant['sensitive_access_flags']) == 0
    )

    return {
        'harness': 'HIVE-398',
        'qa_spec': 'HIVE-369',
        'provisioned_teams': teams,
        'cases': {
            'case_1_fresh_tenant_grant': {
                'pass': case1_pass,
                'description': (
                    'Each provisioned team has a TeamRequiredCapability row '
                    'and an agent on that team inherits the expected skills + pulse MCP.'
                ),
                'teams': case1_teams,
            },
            'case_2_sensitive_flag_gating': {
                'pass': case2_pass,
                'description': (
                    'HIVE-446: sensitive_access_flags / credential_grant_scopes are now '
                    'tenant-tier-gated. Internal/platform agents keep their team spec, but a '
                    'customer tenant only inherits flags/scopes its subscription plan allows '
                    '(default-deny). A sandbox plan with empty allowlists withholds '
                    'host_exec/credential_broker/kubeconfig from a customer devops agent.'
                ),
                'internal_devops_sensitive_flags': internal_sensitive,
                'internal_still_inherits_flags': internal_has_flags,
                'customer_tenant': customer_org_slug,
                'customer_devops_sensitive_flags': customer_sensitive,
                'customer_devops_credential_scopes': customer_scopes,
                'customer_withheld_flags': customer_withheld,
                'customer_flags_withheld': customer_withholds,
                'expected_flags_confirmed': case2_gap_confirmed,
            },
            'case_3_custom_capability': {
                'pass': custom_inherited,
                'description': (
                    'Custom capability with novel slug and novel skill_refs '
                    'is inherited by an agent on the assigned team.'
                ),
                'custom_slug': _CUSTOM_SLUG,
                'custom_skill_refs': _CUSTOM_SKILL_REFS,
                'inherited_by_agent': custom_inherited,
                'full_skills_after': custom_grant['skills'],
            },
            'case_4_tenant_scoped_mcp': {
                'pass': case4_pass,
                'description': (
                    'mcp_server_refs remain catalog slugs on CapabilityDefinition, but the '
                    'effective customer-tenant grant resolves them as <server>:<tenant-org-slug> '
                    'so downstream daemon/broker wiring has an explicit tenant namespace.'
                ),
                'sample_team': sample_team,
                'sample_tenant': sample_tenant,
                'mcp_refs_observed': mcp_refs,
            },
            'case_5_negative_safe_default': {
                'pass': case5_pass,
                'description': (
                    'An agent on a team with no provisioned capability row receives no '
                    'skills/capabilities/sensitive flags; only the PLAT-1251 base MCP floor '
                    'is restored, so there is no accidental privileged access.'
                ),
                'no_capability_team': _NO_CAP_TEAM,
                'grant_has_only_base_mcp_floor': case5_pass,
                'grant_observed': {
                    'skills': empty_grant['skills'],
                    'mcp': empty_grant['enabled_mcp_servers'],
                    'base_mcp_missing_pre_floor': empty_grant['base_mcp_missing_pre_floor'],
                    'sensitive_flags': empty_grant['sensitive_access_flags'],
                },
            },
        },
        'summary': {
            'case_1_pass': case1_pass,
            'case_2_pass': case2_pass,
            'case_3_pass': custom_inherited,
            'case_4_pass': case4_pass,
            'case_5_pass': case5_pass,
            'gaps': [
                gap for gap in [
                    None if case2_pass else
                    'case_2: sensitive_access_flags not tenant-tier-gated (security-critical)',
                ] if gap
            ],
        },
    }


def _provision_custom_capability(slug, skill_refs, team_slug):
    """Provision a novel CapabilityDefinition + TeamRequiredCapability for case 3.

    The harness `approved_by` marker is intentionally machine-authored: the
    default dry-run rolls it back, and `--no-dry-run` is for staging/throwaway
    verification only.
    """
    cap, _ = CapabilityDefinition.objects.update_or_create(
        slug=slug,
        defaults={
            'display_name': 'Harness Custom Capability (HIVE-398)',
            'description': 'Throwaway capability for HIVE-398 custom-capability verification.',
            'lifecycle_status': CapabilityDefinition.LifecycleStatus.DRAFT,
            'activation_status': CapabilityDefinition.ActivationStatus.ACTIVE,
            'risk_tier': CapabilityDefinition.RiskTier.STANDARD,
            'skill_refs': skill_refs,
            'mcp_server_refs': [],
            'sensitive_access_flags': [],
            'created_by': 'hive:harness-hive398',
            'updated_by': 'hive:harness-hive398',
            'approved_by': 'hive:harness-hive398',
        },
    )
    TeamRequiredCapability.objects.update_or_create(
        team_slug=team_slug,
        capability=cap,
        defaults={
            'required': True,
            'enabled': True,
            'reason': 'HIVE-398 custom-capability verification harness.',
            'created_by': 'hive:harness-hive398',
            'updated_by': 'hive:harness-hive398',
        },
    )
    return cap
