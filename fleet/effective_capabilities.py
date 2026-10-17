"""
Pure capability grant computation — no HTTP request, no live agent lookup.

Extracted from fleet/views._effective_capability_set so the verification harness
(management command + tests) can assert against the real engine without needing
an HTTP request or a real FleetAgent record. The harness imports the shared
capability dependency expansion and PLAT-1251 base-MCP floor helpers from
fleet.views so it stays aligned with the live HTTP path until HIVE-417 makes the
HTTP path delegate to this module.

Additive change vs views.py: sensitive_access_flags is surfaced as a top-level
key in the returned payload so the HIVE-369 case-2 security check can observe it.

HIVE-398 — harness for HIVE-369 QA verification.
"""
from django.utils import timezone

from django.conf import settings

from fleet.models import (
    AgentCapabilityOverride,
    BASE_PLATFORM_EAGER_SKILLS,
    BASE_PLATFORM_SKILLS,
    CapabilityDefinition,
    TeamRequiredCapability,
    platform_mcp_floor_for_teams,
)
from fleet.views import _expand_capability_requires


def _tenant_scoped_mcp_ref(ref, tenant_org_slug):
    """Attach the tenant namespace to an MCP server ref for customer-tenant grants.

    CapabilityDefinition.mcp_server_refs remain canonical bare catalog slugs
    (``pulse``, ``wiki``, ...). The effective grant is the boundary where those
    catalog slugs become runtime MCP refs. Internal/platform grants preserve the
    historical bare slug contract; customer-tenant grants carry an explicit
    ``<server>:<tenant-org-slug>`` namespace so daemon/broker resolution cannot
    accidentally bind a customer agent to the platform-global endpoint.
    """
    value = str(ref or '').strip()
    tenant = str(tenant_org_slug or '').strip()
    if not value or not tenant:
        return value
    # Already scoped refs are preserved so future migrations can store explicit
    # scoped refs without double-prefixing.
    if ':' in value or '/' in value:
        return value
    return f'{value}:{tenant}'


def tenant_scope_mcp_refs(refs, tenant_org_slug):
    """Return sorted MCP refs, scoped only when a tenant org slug is supplied."""
    return sorted({_tenant_scoped_mcp_ref(ref, tenant_org_slug) for ref in (refs or []) if str(ref or '').strip()})


def resolve_tenant_grant_allowlist(tenant_org_slug):
    """HIVE-446: resolve a customer tenant's allowed privileged flags/scopes.

    The capability→grant path is otherwise tenant-blind: sensitive_access_flags and
    default_credential_grant_scopes are attached purely by team slug, so a customer
    tenant provisioned with a `devops` (or other privileged) team would inherit the
    exact same host_exec/credential_broker/kubeconfig flags as the internal fleet.
    This gate withholds those unless the tenant's subscription plan permits them.

    Returns:
        None — when ``tenant_org_slug`` is falsy (explicit internal/platform
            agent): the caller must NOT gate; internal behaviour is preserved
            unchanged.
        (allowed_sensitive_flags: set, allowed_credential_scopes: set) — for a
            customer tenant. DEFAULT-DENY: empty sets when the tenant has no
            ACTIVE/TRIAL subscription, no active plan, or the plan lists nothing
            (so unknown/unlisted flags are always withheld — acceptance #3). The
            allowlists are sourced from the real BillingPlan bound via
            TenantSubscription (a subscription attribute, not client convention —
            acceptance #1).
    """
    slug = str(tenant_org_slug or '').strip()
    if not slug:
        return None  # internal/platform — no gating
    try:
        # Lazy import: claws.models may import fleet, so a module-level import here
        # risks an app-load cycle. Fail CLOSED (deny all) if the model is unavailable.
        from hive_platform.models import TenantSubscription
    except Exception:
        return set(), set()
    entitled_statuses = [
        TenantSubscription.Status.ACTIVE,
        TenantSubscription.Status.TRIAL,
    ]
    subscription = (
        TenantSubscription.objects.filter(tenant_org_slug=slug, status__in=entitled_statuses)
        .select_related('plan')
        .order_by('-updated_at')
        .first()
    )
    plan = subscription.plan if subscription else None
    if plan is None or not plan.active:
        return set(), set()  # no entitled subscription/active plan → default-deny
    return (
        {str(f) for f in (plan.allowed_sensitive_access_flags or []) if str(f).strip()},
        {str(s) for s in (plan.allowed_credential_grant_scopes or []) if str(s).strip()},
    )


def compute_effective_capability_grant(team_slugs, agent_identifiers=None, tenant_org_slug=None):
    """Compute the effective capability grant for a set of team memberships.

    Args:
        team_slugs: iterable of Pulse TeamRoster team slugs the synthetic agent
            is a member of.
        agent_identifiers: optional iterable of agent identifiers (id/username/email)
            for per-agent AgentCapabilityOverride lookup. Pass None to skip overrides.
        tenant_org_slug: optional customer-tenant organization slug. When set,
            enabled_mcp_servers are namespaced as ``<server>:<tenant>``. Omit for
            internal/platform agents to preserve historical bare MCP refs.

    Returns dict with keys:
        team_slugs, capabilities, skills, eager_skills, enabled_mcp_servers,
        sensitive_access_flags, inline_profiles, runtime_flags,
        credential_grant_scopes, agent_overrides, diagnostics,
        computed_at, catalog_version, definition_hashes.
    """
    team_slugs = sorted(team_slugs or [])
    requirements = list(
        TeamRequiredCapability.objects.select_related('capability').filter(
            team_slug__in=team_slugs,
            enabled=True,
            required=True,
            capability__activation_status=CapabilityDefinition.ActivationStatus.ACTIVE,
        )
    )
    source_by_cap = {}
    for req in requirements:
        source_by_cap.setdefault(req.capability.slug, set()).add(req.team_slug)
    base_caps, diagnostics = _expand_capability_requires([req.capability for req in requirements])

    override_rows = []
    if agent_identifiers:
        identifiers = set(agent_identifiers)
        overrides = list(
            AgentCapabilityOverride.objects.select_related('capability').filter(
                agent_identifier__in=identifiers,
            )
        )
        now = timezone.now()
        for override in overrides:
            if override.expires_at and override.expires_at <= now:
                override_rows.append({
                    'capability': override.capability.slug,
                    'action': override.action,
                    'expired': True,
                })
                continue
            override_rows.append({
                'capability': override.capability.slug,
                'action': override.action,
                'expired': False,
            })
            if override.action == AgentCapabilityOverride.Action.ADD:
                if override.capability.activation_status == CapabilityDefinition.ActivationStatus.ACTIVE:
                    base_caps[override.capability.slug] = override.capability
                    source_by_cap.setdefault(override.capability.slug, set()).add(
                        f'override:{override.id}'
                    )
                else:
                    diagnostics.append({
                        'level': 'error',
                        'message': f'Override add ignored for inactive capability {override.capability.slug}',
                    })
            elif override.capability.non_removable:
                diagnostics.append({
                    'level': 'error',
                    'message': f'Override remove denied for non-removable capability {override.capability.slug}',
                })
            else:
                base_caps.pop(override.capability.slug, None)
                source_by_cap.pop(override.capability.slug, None)

    withheld = set()
    for slug, cap in list(base_caps.items()):
        for conflict in cap.conflicts_with or []:
            if conflict in base_caps:
                withheld.update({slug, conflict})
                diagnostics.append({
                    'level': 'error',
                    'message': f'Capability conflict withheld: {slug} conflicts with {conflict}',
                })
    for slug in withheld:
        base_caps.pop(slug, None)

    capabilities = sorted(base_caps.values(), key=lambda c: c.slug)
    skills = sorted({ref for c in capabilities for ref in (c.skill_refs or [])})
    eager = sorted({ref for c in capabilities for ref in (c.eager_skill_refs or [])})
    base_skills = tuple(getattr(settings, 'HIVE_BASE_PLATFORM_SKILLS', BASE_PLATFORM_SKILLS))
    base_eager_skills = tuple(
        getattr(settings, 'HIVE_BASE_PLATFORM_EAGER_SKILLS', BASE_PLATFORM_EAGER_SKILLS)
    )
    base_skills_missing = sorted(set(base_skills) - set(skills))
    if base_skills_missing:
        skills = sorted(set(skills) | set(base_skills))
    base_eager_missing = sorted(set(base_eager_skills) - set(eager))
    if base_eager_missing:
        eager = sorted(set(eager) | set(base_eager_skills))
    mcp_catalog_refs = sorted({ref for c in capabilities for ref in (c.mcp_server_refs or [])})
    base_refs = platform_mcp_floor_for_teams(team_slugs)
    base_mcp_missing = sorted(set(base_refs) - set(mcp_catalog_refs))
    if base_mcp_missing:
        diagnostics.append({
            'level': 'error',
            'message': (
                f'PLAT-1251 base-MCP floor applied: capability union missing {base_mcp_missing}; '
                f'invariant: enabled_mcp_servers must always include {sorted(base_refs)} — '
                'fix the granting capability rows (the floor restored them on this payload)'
            ),
        })
        mcp_catalog_refs = sorted(set(mcp_catalog_refs) | set(base_refs))
    profiles = sorted({ref for c in capabilities for ref in (c.inline_profile_refs or [])})
    scopes = sorted({ref for c in capabilities for ref in (c.default_credential_grant_scopes or [])})
    sensitive_flags = sorted({flag for c in capabilities for flag in (c.sensitive_access_flags or [])})
    flags = {}
    for c in capabilities:
        flags.update(c.runtime_flags or {})

    # ── HIVE-446: tenant-tier gate on privileged grants ──────────────────────
    # For a customer tenant, withhold any sensitive_access_flags / credential
    # scopes its subscription plan does not permit (default-deny). Only explicit
    # internal/platform callers pass a falsy tenant_org_slug and skip gating.
    tenant_gate = resolve_tenant_grant_allowlist(tenant_org_slug)
    allowed_sensitive_flags = None if tenant_gate is None else tenant_gate[0]
    withheld_sensitive_flags = []
    withheld_credential_scopes = []
    if tenant_gate is not None:
        allowed_scopes = tenant_gate[1]
        withheld_sensitive_flags = sorted(f for f in sensitive_flags if f not in allowed_sensitive_flags)
        withheld_credential_scopes = sorted(s for s in scopes if s not in allowed_scopes)
        sensitive_flags = [f for f in sensitive_flags if f in allowed_sensitive_flags]
        scopes = [s for s in scopes if s in allowed_scopes]
        if withheld_sensitive_flags or withheld_credential_scopes:
            diagnostics.append({
                'level': 'warning',
                'message': (
                    f'HIVE-446 tenant-tier gate (tenant={str(tenant_org_slug)!r}): withheld '
                    f'sensitive_access_flags={withheld_sensitive_flags} '
                    f'credential_grant_scopes={withheld_credential_scopes} — not permitted by '
                    'the tenant subscription plan allowlist (default-deny for customer tenants).'
                ),
            })

    def _gate_cap_flags(cap_flags):
        """Filter a capability's declared sensitive flags to the tenant-allowed set
        so the per-capability view never advertises a flag the effective grant
        withheld. Internal agents (no gate) see the declared flags unchanged."""
        declared = cap_flags or []
        if allowed_sensitive_flags is None:
            return list(declared)
        return [f for f in declared if f in allowed_sensitive_flags]

    return {
        'team_slugs': team_slugs,
        'capabilities': [
            {
                'slug': c.slug,
                'display_name': c.display_name,
                'risk_tier': c.risk_tier,
                'version': c.version,
                'definition_hash': c.definition_hash,
                'sources': sorted(source_by_cap.get(c.slug, [])),
                'sensitive_access_flags': _gate_cap_flags(c.sensitive_access_flags),
            }
            for c in capabilities
        ],
        'skills': skills,
        'eager_skills': eager,
        'enabled_mcp_servers': tenant_scope_mcp_refs(mcp_catalog_refs, tenant_org_slug),
        'mcp_ref_scope': str(tenant_org_slug or ''),
        'base_mcp_missing_pre_floor': base_mcp_missing,
        'base_skills_missing_pre_floor': base_skills_missing,
        'sensitive_access_flags': sensitive_flags,
        'tenant_tier_gated': tenant_gate is not None,
        'withheld_sensitive_access_flags': withheld_sensitive_flags,
        'withheld_credential_grant_scopes': withheld_credential_scopes,
        'inline_profiles': profiles,
        'runtime_flags': flags,
        'credential_grant_scopes': scopes,
        'agent_overrides': override_rows,
        'diagnostics': diagnostics,
        'computed_at': timezone.now().isoformat(),
        'catalog_version': max((c.version for c in capabilities), default=0),
        'definition_hashes': {c.slug: c.definition_hash for c in capabilities},
    }


def compute_effective_capability_grant_for_memberships(
        memberships, agent_identifiers=None, organization_scope=None,
        internal_org_slugs=('shizuha',)):
    """Compute one safe runtime grant from organization-qualified memberships.

    Team slugs are only unique *inside* an organization.  Flattening an agent's
    memberships to ``{"accounting", "devops"}`` before capability resolution
    lets a team held in one organization influence the MCP grant emitted for
    another.  Resolve each organization independently, tenant-scope its MCP
    references, then union the results for the agent's single runtime config.

    ``organization_scope`` is used by an explicitly selected-org API read.  The
    aggregate runtime reconcile leaves it unset so a multi-org agent retains all
    of its independently scoped grants instead of whichever org happened to
    trigger the latest roster mutation.
    """
    internal_orgs = {
        str(slug or '').strip().lower()
        for slug in (internal_org_slugs or ())
        if str(slug or '').strip()
    }
    explicit_org = str(organization_scope or '').strip().lower()
    grouped = {}
    for membership in memberships or []:
        if not isinstance(membership, dict):
            continue
        org_slug = str(
            membership.get('organization_slug')
            or membership.get('org_slug')
            or 'shizuha'
        ).strip().lower()
        if explicit_org and org_slug != explicit_org:
            continue
        team_slug = str(
            membership.get('team_slug')
            or membership.get('team_name')
            or membership.get('slug')
            or ''
        ).strip().lower().replace('_', '-').replace(' ', '-')
        if team_slug:
            grouped.setdefault(org_slug, set()).add(team_slug)

    if explicit_org and explicit_org not in grouped:
        grouped[explicit_org] = set()
    elif not grouped:
        grouped[''] = set()

    grants = []
    for org_slug in sorted(grouped):
        # Explicit selected-org reads always remain org-qualified, including
        # ``shizuha``.  Aggregate runtime config keeps the platform-internal
        # organization's historical bare refs while namespacing every tenant.
        tenant_scope = (
            org_slug
            if explicit_org or org_slug not in internal_orgs
            else ''
        )
        grants.append((
            org_slug,
            compute_effective_capability_grant(
                sorted(grouped[org_slug]),
                # AgentCapabilityOverride has no organization key. Applying one
                # to a tenant membership would silently turn a platform-global
                # exception into a cross-org grant. Until overrides themselves
                # are org-scoped, they are valid only for the internal org.
                agent_identifiers if org_slug in internal_orgs else None,
                tenant_org_slug=tenant_scope,
            ),
        ))

    capabilities = {}
    diagnostics = []
    agent_overrides = {}
    definition_hashes = {}
    runtime_flags = {}
    union_fields = {
        'team_slugs': set(),
        'skills': set(),
        'eager_skills': set(),
        'enabled_mcp_servers': set(),
        'base_mcp_missing_pre_floor': set(),
        'sensitive_access_flags': set(),
        'withheld_sensitive_access_flags': set(),
        'withheld_credential_grant_scopes': set(),
        'inline_profiles': set(),
        'credential_grant_scopes': set(),
    }
    catalog_version = 0
    tenant_tier_gated = False
    mcp_ref_scopes = set()
    for org_slug, grant in grants:
        for field, values in union_fields.items():
            union_fields[field].update(grant.get(field) or [])
        for capability in grant.get('capabilities') or []:
            slug = capability['slug']
            existing = capabilities.setdefault(slug, dict(capability))
            existing['sources'] = sorted(
                set(existing.get('sources') or [])
                | set(capability.get('sources') or [])
            )
        for diagnostic in grant.get('diagnostics') or []:
            diagnostics.append({**diagnostic, 'organization_slug': org_slug})
        for override in grant.get('agent_overrides') or []:
            key = (
                override.get('capability'),
                override.get('action'),
                bool(override.get('expired')),
            )
            agent_overrides[key] = override
        definition_hashes.update(grant.get('definition_hashes') or {})
        runtime_flags.update(grant.get('runtime_flags') or {})
        catalog_version = max(catalog_version, int(grant.get('catalog_version') or 0))
        tenant_tier_gated = tenant_tier_gated or bool(grant.get('tenant_tier_gated'))
        if grant.get('mcp_ref_scope'):
            mcp_ref_scopes.add(grant['mcp_ref_scope'])

    return {
        **{field: sorted(values) for field, values in union_fields.items()},
        'capabilities': sorted(capabilities.values(), key=lambda capability: capability['slug']),
        'agent_overrides': [
            agent_overrides[key]
            for key in sorted(agent_overrides)
        ],
        'runtime_flags': runtime_flags,
        'diagnostics': diagnostics,
        'computed_at': timezone.now().isoformat(),
        'catalog_version': catalog_version,
        'definition_hashes': definition_hashes,
        'tenant_tier_gated': tenant_tier_gated,
        'mcp_ref_scope': explicit_org or ','.join(sorted(mcp_ref_scopes)),
        'organization_scopes': sorted(org for org in grouped if org),
    }
