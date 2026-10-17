# HIVE-344: merge parallel claws migration leaves after the 2026-07-02 merge burst.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('claws', '0015_hive320_tenant_provisioning_run'),
        ('claws', '0015_merge_20260702_1840'),
    ]

    operations = []
