"""HIVE-368 org-template catalog and dry-run resolver.

Templates are declarative records.  This module owns the built-in starter
library and translates a template into the service calls the HIVE-320 saga will
execute once concrete adapters are enabled.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError
from django.utils import timezone

from fleet.models import DEFAULT_TEAM_CAPABILITY_SPECS

from .models import OrgTemplate

BUILTIN_TEMPLATE_ACTOR = 'hive:hive-368-starter-library'
DEFAULT_WORKFLOWS = ['autonomous-dev', 'autonomous-bug', 'simple']


@dataclass(frozen=True)
class TemplatePlanContext:
    org_name: str
    org_slug: str
    plan_slug: str = ''
    tenant_kind: str = 'customer'


def _team(slug, name, *, capability=None, agent_count=1, model_tier='standard', tier='core', roster_shape='member'):
    return {
        'slug': slug,
        'name': name,
        'capabilities': [capability or slug],
        'agent_count': agent_count,
        'agent_model_tier': model_tier,
        'tier': tier,
        'roster_shape': roster_shape,
    }


def _project(slug, name, *, workflows=None, description=''):
    return {
        'slug': slug,
        'key': slug.upper()[:10],
        'name': name,
        'description': description,
        'workflows': workflows or DEFAULT_WORKFLOWS,
    }


def _starter_task(title, *, project_slug='launch', assignment_group='product', workflow='autonomous-dev', description=''):
    return {
        'title': title,
        'description': description,
        'project_slug': project_slug,
        'assignment_group': assignment_group,
        'workflow': workflow,
        'priority': 'normal',
    }


def _base_template(slug, name, description, teams, starter_tasks, *, category, icon, recommended_tier, projects=None, wiki_pages=None, managers=None, metadata=None):
    return {
        'slug': slug,
        'version': 1,
        'name': name,
        'description': description,
        'status': OrgTemplate.Status.ACTIVE,
        'category': category,
        'icon': icon,
        'recommended_tier': recommended_tier,
        'teams': teams,
        'custom_capabilities': [],
        'workflows': [{'slug': slug, 'default': slug == 'autonomous-dev'} for slug in DEFAULT_WORKFLOWS],
        'projects': projects or [_project('launch', 'Launch Board', description='Default launch project for first-week work.')],
        'wiki_seed_pages': wiki_pages or [
            {
                'space_slug': 'handbook',
                'title': 'Welcome: how this org works',
                'content': 'Use this handbook for durable decisions, runbooks, and onboarding notes.',
            }
        ],
        'connect_workspaces': [{'slug': 'company', 'name': 'Company'}],
        'drive_roots': [{'slug': 'company', 'name': 'Company Drive'}],
        'starter_tasks': starter_tasks,
        'managers': managers or {
            'cluster_manager_team_slug': 'program-management',
            'andon_routes': {
                'product': 'product',
                'engineering': 'architecture',
                'infra-ops': 'devops',
                'business': 'program-management',
            },
        },
        'metadata': {
            'source': 'HIVE-368 starter library',
            'customer_facing': True,
            'sensitive_capability_policy': 'strip_sensitive_access_flags_at_customer_provisioning',
            **(metadata or {}),
        },
    }


def builtin_template_definitions() -> list[dict[str, Any]]:
    """Return the HIVE-368 starter library definitions.

    The Tech Company template is intentionally patterned after Shizuha's proven
    team topology, but contains no internal identities, internal URLs, or
    doctrine.  It only references public/canonical capability slugs.
    """

    return [
        _base_template(
            'tech-company',
            'Tech Company',
            'A full product-engineering organization with product, delivery, review, QA, operations, and documentation lanes.',
            [
                _team('product', 'Product', agent_count=1, roster_shape='lead'),
                _team('design', 'Design', agent_count=1),
                _team('architecture', 'Architecture', agent_count=1, roster_shape='lead'),
                _team('engineering', 'Engineering', agent_count=2, model_tier='advanced'),
                _team('review', 'Code Review', agent_count=1),
                _team('merge', 'Merge', agent_count=1, tier='controlled'),
                _team('qa', 'Quality Assurance', agent_count=1),
                _team('devops', 'DevOps', agent_count=1, tier='controlled'),
                _team('documentation', 'Documentation', agent_count=1),
                _team('program-management', 'Program Management', agent_count=1),
            ],
            [
                _starter_task('Define the first customer-visible product increment', assignment_group='product'),
                _starter_task('Set up the delivery project skeleton and acceptance checklist', assignment_group='engineering'),
                _starter_task('Create the release-readiness smoke checklist', assignment_group='qa', workflow='simple'),
            ],
            category='company',
            icon='building-2',
            recommended_tier='team',
            metadata={'reference_shape': 'top-tech-company'},
        ),
        _base_template(
            'content-marketing-org',
            'Content / Marketing Org',
            'A publishing and growth team with research, writing, design, social, analytics, and campaign operations.',
            [
                _team('product', 'Content Strategy', capability='product', agent_count=1, roster_shape='lead'),
                _team('research-analytics', 'Research', agent_count=2),
                _team('documentation', 'Writing', agent_count=2),
                _team('design', 'Design', agent_count=1),
                _team('social-media', 'Social Media', agent_count=1),
                _team('marketing', 'Marketing Ops', agent_count=1),
            ],
            [
                _starter_task('Draft the first 30-day editorial calendar', assignment_group='product'),
                _starter_task('Research three customer pain themes for the first campaign', assignment_group='research-analytics'),
                _starter_task('Publish the first weekly performance report shell', assignment_group='research-analytics', workflow='simple'),
            ],
            category='go-to-market',
            icon='megaphone',
            recommended_tier='team',
        ),
        _base_template(
            'back-office-ops-org',
            'Back-office / Ops Org',
            'A business-operations team for finance, legal, people, support, governance, and administrative execution.',
            [
                _team('program-management', 'Operations Lead', agent_count=1, roster_shape='lead'),
                _team('accounting', 'Accounting', agent_count=1),
                _team('legal', 'Legal', agent_count=1),
                _team('people', 'People Ops', agent_count=1),
                _team('customer-support', 'Customer Support', agent_count=1),
                _team('admin-ops', 'Admin Ops', agent_count=1, tier='controlled'),
                _team('documentation', 'Process Documentation', agent_count=1),
            ],
            [
                _starter_task('Create the weekly operations review checklist', assignment_group='program-management', workflow='simple'),
                _starter_task('Draft the customer support intake and escalation path', assignment_group='customer-support'),
                _starter_task('Set up the first finance close task list', assignment_group='accounting'),
            ],
            category='operations',
            icon='briefcase-business',
            recommended_tier='team',
        ),
        _base_template(
            'solo-founder',
            'Solo Founder',
            'A tiny autonomous-org shape: one product/generalist lead plus one engineering/generalist lane and optional operations support.',
            [
                _team('product', 'Founder PM', agent_count=1, model_tier='advanced', roster_shape='lead'),
                _team('engineering', 'Builder', agent_count=1, model_tier='advanced'),
                _team('program-management', 'Ops Assistant', agent_count=1, tier='optional'),
            ],
            [
                _starter_task('Turn the idea into a one-page launch brief', assignment_group='product'),
                _starter_task('Create the smallest shippable prototype plan', assignment_group='engineering'),
            ],
            category='starter',
            icon='rocket',
            recommended_tier='solo',
        ),
    ]


def get_builtin_template_definition(slug: str) -> dict[str, Any] | None:
    needle = (slug or '').strip().lower()
    for definition in builtin_template_definitions():
        if definition['slug'] == needle:
            return deepcopy(definition)
    return None


def seed_builtin_org_templates(*, actor=BUILTIN_TEMPLATE_ACTOR) -> list[OrgTemplate]:
    """Upsert built-in templates into versioned OrgTemplate records."""

    seeded = []
    now = timezone.now()
    for definition in builtin_template_definitions():
        fields = dict(definition)
        slug = fields.pop('slug')
        version = fields.pop('version')
        fields.update({
            'scrubbed_by': actor,
            'scrubbed_at': now,
            'updated_by': actor,
        })
        template, created = OrgTemplate.objects.update_or_create(
            slug=slug,
            version=version,
            defaults=fields,
        )
        if created and not template.created_by:
            template.created_by = actor
            template.save(update_fields=['created_by'])
        seeded.append(template)
    return seeded


def load_org_template(slug: str, *, version: int | None = None) -> OrgTemplate:
    qs = OrgTemplate.objects.filter(slug=(slug or '').strip().lower())
    if version is not None:
        return qs.get(version=version)
    try:
        return qs.get(status=OrgTemplate.Status.ACTIVE)
    except OrgTemplate.DoesNotExist:
        definition = get_builtin_template_definition(slug)
        if not definition:
            # Backward-compatible preview path for older callers/tests that used
            # arbitrary template slugs before HIVE-368 introduced a catalog.  It
            # preserves HIVE-320 dry-run behavior while making catalog-backed
            # slugs richer.
            slug_value = (slug or 'custom-template').strip().lower().replace('_', '-').replace(' ', '-')
            return OrgTemplate(
                slug=slug_value,
                version=1,
                name=slug_value.replace('-', ' ').title(),
                status=OrgTemplate.Status.DRAFT,
                projects=[_project('launch', 'Launch Board')],
                workflows=[{'slug': workflow, 'default': workflow == 'autonomous-dev'} for workflow in DEFAULT_WORKFLOWS],
                metadata={'source': 'compatibility-fallback'},
            )
        # Transient fallback keeps dry-run preview usable before the seed command
        # has run in a fresh environment. The seed command remains the durable
        # storage path.
        return OrgTemplate(**definition, scrubbed_by=BUILTIN_TEMPLATE_ACTOR, scrubbed_at=timezone.now())


def _normalize_slug(value: str) -> str:
    return str(value or '').strip().lower().replace('_', '-').replace(' ', '-')


def _capability_payload(slug: str, custom_by_slug: dict[str, dict[str, Any]]) -> dict[str, Any]:
    slug = _normalize_slug(slug)
    custom = custom_by_slug.get(slug)
    if custom:
        return {
            'slug': slug,
            'display_name': custom.get('display_name') or custom.get('name') or slug.replace('-', ' ').title(),
            'kind': 'custom',
            'skill_refs': custom.get('skill_refs') or [],
            'mcp_server_refs': custom.get('mcp_refs') or custom.get('mcp_server_refs') or [],
            'sensitive_access_flags': custom.get('sensitive_access_flags') or [],
            'credential_grant_scopes': custom.get('default_credential_grant_scopes') or [],
        }
    spec = DEFAULT_TEAM_CAPABILITY_SPECS.get(slug)
    if not spec:
        raise ValidationError(f'Unknown capability slug {slug!r}; add it to custom_capabilities or DEFAULT_TEAM_CAPABILITY_SPECS')
    return {
        'slug': slug,
        'display_name': spec.get('display_name') or slug.replace('-', ' ').title(),
        'kind': 'builtin',
        'skill_refs': spec.get('skill_refs') or [],
        'mcp_server_refs': spec.get('mcp_server_refs') or [],
        'sensitive_access_flags': spec.get('sensitive_access_flags') or [],
        'credential_grant_scopes': spec.get('default_credential_grant_scopes') or [],
    }


def resolve_org_template_plan(template: OrgTemplate | str, context: TemplatePlanContext | dict[str, Any]) -> dict[str, Any]:
    """Resolve a template into deterministic create-calls without side effects."""

    if isinstance(template, str):
        template = load_org_template(template)
    if isinstance(context, dict):
        context = TemplatePlanContext(**context)

    custom_by_slug = {
        _normalize_slug(cap.get('slug')): cap
        for cap in (template.custom_capabilities or [])
        if isinstance(cap, dict) and cap.get('slug')
    }
    tenant_kind = _normalize_slug(context.tenant_kind)
    tenant_is_customer = tenant_kind != 'internal'
    create_calls: list[dict[str, Any]] = []
    team_plans: list[dict[str, Any]] = []

    create_calls.append({
        'service': 'admin',
        'action': 'create_organization',
        'args': {'name': context.org_name, 'slug': context.org_slug, 'template_slug': template.slug},
    })

    for team in template.teams or []:
        team_slug = _normalize_slug(team.get('slug'))
        capabilities = team.get('capabilities') or team.get('capability') or team_slug
        if isinstance(capabilities, str):
            capabilities = [capabilities]
        capability_plans = []
        stripped_sensitive_flags = []
        stripped_credential_scopes = []
        effective_sensitive_flags = []
        for cap_slug in capabilities:
            cap = _capability_payload(cap_slug, custom_by_slug)
            flags = list(cap.get('sensitive_access_flags') or [])
            credential_scopes = list(cap.get('credential_grant_scopes') or [])
            if tenant_is_customer and (flags or credential_scopes):
                stripped_sensitive_flags.extend(flags)
                stripped_credential_scopes.extend(credential_scopes)
                cap = {
                    **cap,
                    'sensitive_access_flags': [],
                    'credential_grant_scopes': [],
                    'stripped_credential_grant_scopes': sorted(set(credential_scopes)),
                }
            else:
                effective_sensitive_flags.extend(flags)
            capability_plans.append(cap)

        team_plan = {
            'slug': team_slug,
            'name': team.get('name') or team_slug.replace('-', ' ').title(),
            'agent_count': int(team.get('agent_count') or 0),
            'agent_model_tier': team.get('agent_model_tier') or 'standard',
            'tier': team.get('tier') or 'core',
            'roster_shape': team.get('roster_shape') or 'member',
            'capabilities': capability_plans,
            'sensitive_access_flags': sorted(set(effective_sensitive_flags)),
            'stripped_sensitive_access_flags': sorted(set(stripped_sensitive_flags)),
            'stripped_credential_grant_scopes': sorted(set(stripped_credential_scopes)),
        }
        team_plans.append(team_plan)
        create_calls.append({
            'service': 'admin',
            'action': 'create_team',
            'args': {'org_slug': context.org_slug, 'team_slug': team_slug, 'name': team_plan['name']},
        })
        for cap in capability_plans:
            create_calls.append({
                'service': 'hive',
                'action': 'ensure_team_required_capability',
                'args': {
                    'org_slug': context.org_slug,
                    'team_slug': team_slug,
                    'capability_slug': cap['slug'],
                    'sensitive_access_flags': cap.get('sensitive_access_flags') or [],
                },
            })
        for index in range(team_plan['agent_count']):
            create_calls.append({
                'service': 'hive',
                'action': 'provision_agent',
                'args': {
                    'org_slug': context.org_slug,
                    'team_slug': team_slug,
                    'ordinal': index + 1,
                    'model_tier': team_plan['agent_model_tier'],
                },
            })

    for project in template.projects or []:
        project_slug = _normalize_slug(project.get('slug') or 'launch')
        create_calls.append({
            'service': 'pulse',
            'action': 'create_project',
            'args': {
                'org_slug': context.org_slug,
                'project_slug': project_slug,
                'key': project.get('key') or project_slug.upper()[:10],
                'name': project.get('name') or project_slug.title(),
                'workflows': project.get('workflows') or DEFAULT_WORKFLOWS,
            },
        })

    for workspace in template.connect_workspaces or []:
        create_calls.append({'service': 'connect', 'action': 'create_workspace', 'args': {'org_slug': context.org_slug, **workspace}})
    for root in template.drive_roots or []:
        create_calls.append({'service': 'drive', 'action': 'create_root', 'args': {'org_slug': context.org_slug, **root}})
    for page in template.wiki_seed_pages or []:
        create_calls.append({'service': 'wiki', 'action': 'create_seed_page', 'args': {'org_slug': context.org_slug, **page}})
    for task in template.starter_tasks or []:
        create_calls.append({'service': 'pulse', 'action': 'create_task', 'args': {'org_slug': context.org_slug, **task}})
    if template.managers:
        create_calls.append({'service': 'hive', 'action': 'configure_manager_andon_routes', 'args': {'org_slug': context.org_slug, **template.managers}})

    return {
        'template': {
            'slug': template.slug,
            'version': template.version,
            'name': template.name,
            'definition_hash': template.definition_hash,
            'status': template.status,
        },
        'context': {
            'org_name': context.org_name,
            'org_slug': context.org_slug,
            'plan_slug': context.plan_slug,
            'tenant_kind': tenant_kind,
        },
        'teams': team_plans,
        'workflows': template.workflows or [],
        'projects': template.projects or [],
        'starter_tasks': template.starter_tasks or [],
        'managers': template.managers or {},
        'create_calls': create_calls,
    }
