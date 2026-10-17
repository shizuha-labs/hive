"""Restore full agent MCP for CEO Office talk seats.

Operator 2026-08-16: Ena/Yuna must do real Pulse/Hive work like other
platform agents. Migration 0048 stripped admin/id and forced a talk-minimal
prompt that emptied tools[]. Put the platform floor + Hive back, and turn
the talk-minimal prompt off.
"""
import hashlib
import json

from django.db import migrations


FLOOR = ['pulse', 'connect', 'wiki', 'admin', 'id', 'hive']
CEO_USERNAMES = ['hina', 'aya', 'yuna', 'ena']
ENA_PROMPT = (
    'You are Ena, Executive Assistant on the CEO Office team. Dedicated to '
    'Hritik (CEO). You are a full fleet agent with Pulse, Connect, Wiki, '
    'Admin, ID, and Hive — the same class as other platform agents. When '
    'Hritik asks what tasks are assigned to him, call pulse_get_user_tasks '
    'and report the real results. Never say you will look something up '
    'without calling the tool. Never write tool_call or ToolSearch markup '
    'as spoken text. CEO Office roster: Hina (Grok Build, stopped), Aya '
    '(Grok Build, stopped), Yuna (DeepSeek SCLI), Ena (you, grok-4.6 SCLI). '
    'Lead listed as Hina.'
)
YUNA_PROMPT = (
    'You are Yuna, Executive Assistant on the CEO Office team. Dedicated to '
    'Hritik (CEO). You are a full fleet agent with Pulse, Connect, Wiki, '
    'Admin, ID, and Hive — the same class as other platform agents. When '
    'Hritik asks what tasks are assigned to him, call pulse_get_user_tasks '
    'and report the real results. Never say you will look something up '
    'without calling the tool. Never write tool_call or ToolSearch markup '
    'as spoken text. CEO Office roster: Hina (Grok Build, stopped), Aya '
    '(Grok Build, stopped), Yuna (you, DeepSeek SCLI), Ena (grok-4.6 SCLI). '
    'Lead listed as Hina.'
)


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


def _union_floor(refs):
    out = []
    for ref in list(refs or []) + FLOOR:
        value = str(ref or '').strip()
        if value and value not in out:
            out.append(value)
    return out


def forwards(apps, schema_editor):
    CapabilityDefinition = apps.get_model('fleet', 'CapabilityDefinition')
    FleetAgent = apps.get_model('fleet', 'FleetAgent')
    for cap in CapabilityDefinition.objects.filter(slug='ceo-office'):
        expanded = _union_floor(cap.mcp_server_refs)
        if list(cap.mcp_server_refs or []) == expanded:
            continue
        cap.mcp_server_refs = expanded
        new_hash = _definition_hash(_hash_payload(cap))
        if cap.definition_hash and cap.definition_hash != new_hash:
            cap.version = int(cap.version or 1) + 1
        cap.definition_hash = new_hash
        cap.updated_by = 'migration:0049_ceo_office_full_agent_mcp'
        cap.save(update_fields=[
            'mcp_server_refs', 'definition_hash', 'version',
            'updated_by', 'updated_at',
        ])

    prompts = {'ena': ENA_PROMPT, 'yuna': YUNA_PROMPT}
    for agent in FleetAgent.objects.filter(agent_username__in=CEO_USERNAMES):
        mcp = dict(agent.mcp_config or {}) if isinstance(agent.mcp_config, dict) else {}
        ids = _union_floor(mcp.get('enabled_mcp_server_ids') or [])
        mcp['enabled_mcp_server_ids'] = ids
        mcp['mcp_servers'] = [{'name': slug, 'slug': slug} for slug in ids]
        agent.mcp_config = mcp
        env = dict(agent.env or {}) if isinstance(agent.env, dict) else {}
        env['SHIZUHA_TALK_MINIMAL_PROMPT'] = '0'
        env.pop('SHIZUHA_TALK_SUPPRESS_TOOLS', None)
        agent.env = env
        update_fields = ['mcp_config', 'env']
        prompt = prompts.get(agent.agent_username)
        if prompt:
            agent.context_prompt = prompt
            update_fields.append('context_prompt')
        agent.save(update_fields=update_fields)


def backwards(apps, schema_editor):
    CapabilityDefinition = apps.get_model('fleet', 'CapabilityDefinition')
    for cap in CapabilityDefinition.objects.filter(slug='ceo-office'):
        refs = [ref for ref in (cap.mcp_server_refs or []) if ref not in {'admin', 'id', 'hive'}]
        for core in ('pulse', 'connect', 'wiki'):
            if core not in refs:
                refs.append(core)
        cap.mcp_server_refs = refs
        cap.definition_hash = _definition_hash(_hash_payload(cap))
        cap.updated_by = 'migration:0049_ceo_office_full_agent_mcp:backwards'
        cap.save(update_fields=['mcp_server_refs', 'definition_hash', 'updated_by', 'updated_at'])


class Migration(migrations.Migration):
    dependencies = [
        ('fleet', '0048_ceo_office_lean_mcp'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
