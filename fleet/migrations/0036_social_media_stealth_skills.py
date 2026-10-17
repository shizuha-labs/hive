"""Attach human-browser-stealth + x-account-safety to social-media (and marketing).

Operator 2026-07-29: agent skill_config was hand-patched then wiped by capability
reconcile because the team CapabilityDefinition only listed x-twitter +
connect-messaging. Hive team capabilities are SoT — put stealth skills on the
capability grant (skills + eager) so reconcile keeps them on every team seat.
"""
import hashlib
import json

from django.db import migrations


UPDATES = {
    'social-media': {
        'skill_refs': [
            'x-twitter',
            'connect-messaging',
            'human-browser-stealth',
            'x-account-safety',
        ],
        'eager_skill_refs': [
            'human-browser-stealth',
            'x-account-safety',
            'x-twitter',
        ],
    },
    'marketing': {
        'skill_refs': [
            'x-twitter',
            'connect-messaging',
            'human-browser-stealth',
            'x-account-safety',
        ],
        'eager_skill_refs': [
            'human-browser-stealth',
            'x-account-safety',
            'x-twitter',
        ],
    },
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


def _merge_unique(existing, required):
    out = list(existing or [])
    for ref in required or []:
        if ref not in out:
            out.append(ref)
    return out


def forwards(apps, schema_editor):
    CapabilityDefinition = apps.get_model('fleet', 'CapabilityDefinition')
    for slug, bundle in UPDATES.items():
        for cap in CapabilityDefinition.objects.filter(slug=slug):
            cap.skill_refs = _merge_unique(cap.skill_refs, bundle['skill_refs'])
            cap.eager_skill_refs = _merge_unique(cap.eager_skill_refs, bundle['eager_skill_refs'])
            new_hash = _definition_hash(_hash_payload(cap))
            if cap.definition_hash and cap.definition_hash != new_hash:
                cap.version = int(cap.version or 1) + 1
            cap.definition_hash = new_hash
            cap.updated_by = 'migration:0036_social_media_stealth_skills'
            cap.save(update_fields=[
                'skill_refs', 'eager_skill_refs', 'definition_hash', 'version',
                'updated_by', 'updated_at',
            ])


def backwards(apps, schema_editor):
    CapabilityDefinition = apps.get_model('fleet', 'CapabilityDefinition')
    strip = {'human-browser-stealth', 'x-account-safety'}
    for slug in UPDATES:
        for cap in CapabilityDefinition.objects.filter(slug=slug):
            cap.skill_refs = [s for s in (cap.skill_refs or []) if s not in strip]
            cap.eager_skill_refs = [s for s in (cap.eager_skill_refs or []) if s not in strip]
            cap.definition_hash = _definition_hash(_hash_payload(cap))
            cap.updated_by = 'migration:0036_social_media_stealth_skills:backwards'
            cap.save(update_fields=[
                'skill_refs', 'eager_skill_refs', 'definition_hash',
                'updated_by', 'updated_at',
            ])


class Migration(migrations.Migration):
    dependencies = [
        ('fleet', '0035_review_merge_skill_split_agents_md'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
