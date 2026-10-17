# HIVE-344: merge parallel fleet migration leaves after the 2026-07-02 merge burst.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0014_merge_20260703_hive320'),
        ('fleet', '0015_hive332_recovery_action'),
    ]

    operations = []
