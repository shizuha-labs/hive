"""Split review/merge skills; Review gets pulse-review + code-reviewer.

Also attaches agents-md-shipping to engineering baseline skill_refs.
"""
import hashlib
import json

from django.db import migrations


UPDATES = {
    'review': ['code-reviewer', 'pulse-review'],
    'merge': ['pulse-merge'],
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


def _apply_skills(cap, skills):
    cap.skill_refs = list(skills)
    new_hash = _definition_hash(_hash_payload(cap))
    if cap.definition_hash and cap.definition_hash != new_hash:
        cap.version = int(cap.version or 1) + 1
    cap.definition_hash = new_hash
    cap.updated_by = 'migration:0035_review_merge_skill_split_agents_md'
    cap.save(update_fields=[
        'skill_refs', 'definition_hash', 'version', 'updated_by', 'updated_at',
    ])


def forwards(apps, schema_editor):
    CapabilityDefinition = apps.get_model('fleet', 'CapabilityDefinition')
    for slug, skills in UPDATES.items():
        for cap in CapabilityDefinition.objects.filter(slug=slug):
            _apply_skills(cap, skills)
    for cap in CapabilityDefinition.objects.filter(slug='engineering'):
        refs = list(cap.skill_refs or [])
        if 'agents-md-shipping' not in refs:
            refs.append('agents-md-shipping')
            _apply_skills(cap, refs)


def backwards(apps, schema_editor):
    CapabilityDefinition = apps.get_model('fleet', 'CapabilityDefinition')
    for cap in CapabilityDefinition.objects.filter(slug='review'):
        _apply_skills(cap, ['code-reviewer', 'pulse-review-merge'])
    for cap in CapabilityDefinition.objects.filter(slug='merge'):
        _apply_skills(cap, ['pulse-review-merge', 'github', 'shipping-prs'])


class Migration(migrations.Migration):
    dependencies = [
        ('fleet', '0034_review_capability_generic_code_reviewer'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
