"""HIVE-446: tenant-tier gate for privileged capability grants.

Adds two allowlist fields to BillingPlan so a customer tenant only inherits the
sensitive_access_flags / credential_grant_scopes its plan explicitly permits.
Both default to an empty list => DEFAULT-DENY for existing/new plans (no plan
grants privileged flags until Product opts a tier in). Additive + nullable-safe:
existing rows get the empty-list default, so this is a safe forward migration.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('claws', '0020_hive356_pilot_prep'),
    ]

    operations = [
        migrations.AddField(
            model_name='billingplan',
            name='allowed_sensitive_access_flags',
            field=models.JSONField(
                default=list,
                blank=True,
                help_text=(
                    'HIVE-446 allowlist: sensitive_access_flags a customer tenant on '
                    'this plan may inherit (e.g. ["host_exec"]). Empty = default-deny.'
                ),
            ),
        ),
        migrations.AddField(
            model_name='billingplan',
            name='allowed_credential_grant_scopes',
            field=models.JSONField(
                default=list,
                blank=True,
                help_text=(
                    'HIVE-446 allowlist: credential_grant_scopes a customer tenant on '
                    'this plan may inherit (e.g. ["kubeconfig"]). Empty = default-deny.'
                ),
            ),
        ),
    ]
