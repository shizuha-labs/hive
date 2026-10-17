"""Review capability: generic code-reviewer + pulse-review-merge only.

Retires person-specific revi/reika skills and legacy github skill from the
Review team capability baseline. MCP floor (incl. admin) unchanged.
"""
import hashlib
import json

from django.db import migrations


NEW_SKILL_REFS = ['code-reviewer', 'pulse-review-merge']
OLD_SKILL_REFS = ['pulse-review-merge', 'github', 'revi-reviewer', 'reika-reviewer']


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


def forwards(apps, schema_editor):
    CapabilityDefinition = apps.get_model('fleet', 'CapabilityDefinition')
    for cap in CapabilityDefinition.objects.filter(slug='review'):
        cap.skill_refs = list(NEW_SKILL_REFS)
        new_hash = _definition_hash(_hash_payload(cap))
        if cap.definition_hash and cap.definition_hash != new_hash:
            cap.version = int(cap.version or 1) + 1
        cap.definition_hash = new_hash
        cap.updated_by = 'migration:0034_review_capability_generic_code_reviewer'
        cap.save(update_fields=[
            'skill_refs', 'definition_hash', 'version', 'updated_by', 'updated_at',
        ])


def backwards(apps, schema_editor):
    CapabilityDefinition = apps.get_model('fleet', 'CapabilityDefinition')
    for cap in CapabilityDefinition.objects.filter(slug='review'):
        cap.skill_refs = list(OLD_SKILL_REFS)
        new_hash = _definition_hash(_hash_payload(cap))
        if cap.definition_hash and cap.definition_hash != new_hash:
            cap.version = int(cap.version or 1) + 1
        cap.definition_hash = new_hash
        cap.save(update_fields=['skill_refs', 'definition_hash', 'version', 'updated_at'])


class Migration(migrations.Migration):
    dependencies = [
        ('fleet', '0033_merge_hive803_hive1153'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
