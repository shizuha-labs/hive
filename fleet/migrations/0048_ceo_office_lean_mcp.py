"""Slim CEO Office MCP + inject long-idle conversational runtime env.

Operator 2026-08-15: talkable CEO Office seats (Hina/Aya) must keep a tiny
stable SuperGrok prefix. The PLAT-1251 admin/id floor bloated their tool head
and 5-minute heartbeats busted prompt cache after every live conversation.
"""
import hashlib
import json

from django.db import migrations


LEAN_CORE = ['pulse', 'connect', 'wiki']
TRIM = {'admin', 'id', 'scs'}
LEAN_ENV = {
    'AGENT_TEAM': 'ceo-office',
    'SHIZUHA_IDLE_HEARTBEAT_MS': '1800000',
    'SHIZUHA_HEARTBEAT_DEBOUNCE_MS': '1800000',
    'SHIZUHA_FIRST_HEARTBEAT_MS': '1800000',
    'SHIZUHA_LEAN_MCP': '1',
}


def _definition_hash(payload):
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest()


def _hash_payload(cap):
    return {
        'slug': cap.slug,
        'display_name': cap.display_name,
        'description': cap.description or '',
        'lifecycle_status': cap.lifecycle_status,
        'activation_status': cap.activation_status,
        'risk_tier': cap.risk_tier,
        'skill_refs': cap.skill_refs or [],
        'eager_skill_refs': cap.eager_skill_refs or [],
        'inline_profile_refs': cap.inline_profile_refs or [],
        'mcp_server_refs': cap.mcp_server_refs or [],
        'runtime_flags': cap.runtime_flags or {},
        'sensitive_access_flags': cap.sensitive_access_flags or [],
        'default_credential_grant_scopes': cap.default_credential_grant_scopes or [],
        'conflicts_with': cap.conflicts_with or [],
        'requires': cap.requires or [],
        'non_removable': bool(cap.non_removable),
    }


def _trim_refs(refs):
    extras = [ref for ref in (refs or []) if ref not in TRIM and ref not in LEAN_CORE]
    out = list(LEAN_CORE)
    for ref in extras:
        if ref not in out:
            out.append(ref)
    return out


def forwards(apps, schema_editor):
    CapabilityDefinition = apps.get_model('fleet', 'CapabilityDefinition')
    FleetAgent = apps.get_model('fleet', 'FleetAgent')
    for cap in CapabilityDefinition.objects.filter(slug='ceo-office'):
        trimmed = _trim_refs(cap.mcp_server_refs)
        if list(cap.mcp_server_refs or []) == trimmed:
            continue
        cap.mcp_server_refs = trimmed
        cap.eager_skill_refs = []
        new_hash = _definition_hash(_hash_payload(cap))
        if cap.definition_hash and cap.definition_hash != new_hash:
            cap.version = int(cap.version or 1) + 1
        cap.definition_hash = new_hash
        cap.updated_by = 'migration:0048_ceo_office_lean_mcp'
        cap.save(update_fields=[
            'mcp_server_refs', 'eager_skill_refs', 'definition_hash', 'version',
            'updated_by', 'updated_at',
        ])

    for agent in FleetAgent.objects.filter(agent_username__in=['hina', 'aya']):
        env = dict(agent.env or {}) if isinstance(agent.env, dict) else {}
        env.update(LEAN_ENV)
        update_fields = ['env']
        if not (agent.reasoning_effort or '').strip() or agent.reasoning_effort in ('high', 'xhigh'):
            agent.reasoning_effort = 'low'
            update_fields.append('reasoning_effort')
        agent.env = env
        agent.save(update_fields=update_fields)


def backwards(apps, schema_editor):
    CapabilityDefinition = apps.get_model('fleet', 'CapabilityDefinition')
    for cap in CapabilityDefinition.objects.filter(slug='ceo-office'):
        refs = list(cap.mcp_server_refs or [])
        for extra in ('admin', 'id'):
            if extra not in refs:
                refs.append(extra)
        cap.mcp_server_refs = refs
        cap.definition_hash = _definition_hash(_hash_payload(cap))
        cap.updated_by = 'migration:0048_ceo_office_lean_mcp:backwards'
        cap.save(update_fields=['mcp_server_refs', 'definition_hash', 'updated_by', 'updated_at'])


class Migration(migrations.Migration):
    dependencies = [
        ('fleet', '0047_repair_pending_config_objects'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
