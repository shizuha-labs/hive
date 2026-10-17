"""Grant the private Browser MCP to social-media capability bundles.

Hive already provisions the per-agent worker and projects its URL for social
media seats. The effective capability allowlist must name ``browser`` as well;
otherwise the reviewed daemon grant excludes the bridge from generated
runtimes even though the worker itself is healthy.
"""
import hashlib
import json

from django.db import migrations


def _definition_hash(cap):
    payload = {
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
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest()


def forwards(apps, schema_editor):
    CapabilityDefinition = apps.get_model('fleet', 'CapabilityDefinition')
    for cap in CapabilityDefinition.objects.filter(slug='social-media'):
        refs = list(cap.mcp_server_refs or [])
        if 'browser' in refs:
            continue
        refs.append('browser')
        cap.mcp_server_refs = refs
        cap.definition_hash = _definition_hash(cap)
        cap.version = int(cap.version or 1) + 1
        cap.updated_by = 'migration:0048_social_media_browser_mcp'
        cap.save(update_fields=[
            'mcp_server_refs', 'definition_hash', 'version', 'updated_by', 'updated_at',
        ])


def backwards(apps, schema_editor):
    # Do not remove an MCP grant on rollback. A later capability edit may have
    # adopted the same ref; destructive reversal would silently revoke it.
    return None


class Migration(migrations.Migration):
    dependencies = [
        ('fleet', '0047_repair_pending_config_objects'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
