"""PLAT-4235 P1: per-user fleet daemon ownership on DaemonRegistry.

Each user's Hive has exactly one independent k3s fleet daemon managing that user's
entire fleet. `owner_subject` NULL = the shared org/team daemon (backward-compatible
with the existing single global daemon), set = a per-user daemon scoped to that
user's owned agents (INV-OWNER-SCOPE).
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0028_fleetagent_exact_profile_binding'),
    ]

    operations = [
        migrations.AddField(
            model_name='daemonregistry',
            name='owner_subject',
            field=models.BigIntegerField(
                null=True, blank=True, db_index=True,
                help_text='Shizuha-ID user subject this daemon serves; NULL = shared org/team fleet daemon.',
            ),
        ),
        migrations.AddField(
            model_name='daemonregistry',
            name='owner_username',
            field=models.CharField(
                max_length=150, blank=True, default='',
                help_text='Non-authoritative owner display handle; owner_subject is the stable key.',
            ),
        ),
    ]
