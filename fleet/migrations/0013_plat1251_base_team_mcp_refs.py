# Generated for PLAT-1251 on 2026-07-02

from django.db import migrations
import fleet.models


BASE_PLATFORM_MCP_REFS = ['pulse', 'connect', 'wiki', 'admin', 'id']

TEAM_CAPABILITY_SLUGS = [
    'accounting',
    'admin-ops',
    'analytics',
    'architecture',
    'customer-support',
    'design',
    'devops',
    'documentation',
    'engineering',
    'governance',
    'human-ops',
    'it',
    'legal',
    'marketing',
    'merge',
    'people',
    'platform',
    'product',
    'program-management',
    'qa',
    'research',
    'review',
    'sales',
    'security',
    'social-media',
]


def _with_base_mcp(existing):
    refs = list(BASE_PLATFORM_MCP_REFS)
    for ref in existing or []:
        if ref not in refs:
            refs.append(ref)
    return refs


def _payload(cap, mcp_refs):
    return {
        'slug': cap.slug,
        'display_name': cap.display_name,
        'description': cap.description,
        'lifecycle_status': cap.lifecycle_status,
        'activation_status': cap.activation_status,
        'risk_tier': cap.risk_tier,
        'skill_refs': cap.skill_refs or [],
        'eager_skill_refs': cap.eager_skill_refs or [],
        'inline_profile_refs': cap.inline_profile_refs or [],
        'mcp_server_refs': mcp_refs,
        'runtime_flags': cap.runtime_flags or {},
        'sensitive_access_flags': cap.sensitive_access_flags or [],
        'default_credential_grant_scopes': cap.default_credential_grant_scopes or [],
        'conflicts_with': cap.conflicts_with or [],
        'requires': cap.requires or [],
        'non_removable': cap.non_removable,
    }


def add_base_mcp_refs_to_team_capabilities(apps, schema_editor):
    CapabilityDefinition = apps.get_model('fleet', 'CapabilityDefinition')
    actor = 'migration:PLAT-1251-base-team-mcp'

    for cap in CapabilityDefinition.objects.filter(slug__in=TEAM_CAPABILITY_SLUGS):
        next_mcp = _with_base_mcp(cap.mcp_server_refs or [])
        if next_mcp == (cap.mcp_server_refs or []):
            continue
        payload = _payload(cap, next_mcp)
        cap.mcp_server_refs = next_mcp
        cap.definition_hash = fleet.models._definition_hash(payload)
        cap.version = (cap.version or 1) + 1
        cap.updated_by = actor
        cap.save(update_fields=['mcp_server_refs', 'definition_hash', 'version', 'updated_by', 'updated_at'])


def noop_reverse(apps, schema_editor):
    # Do not remove base MCP refs on rollback; doing so would recreate the
    # pulse-only runtime contract that PLAT-1251 fixed.
    return None


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0012_merge_20260702_0855'),
    ]

    operations = [
        migrations.RunPython(add_base_mcp_refs_to_team_capabilities, noop_reverse),
    ]
