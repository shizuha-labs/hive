# HIVE-319: merge usage-billing leaf with current claws migration graph.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('claws', '0015_hive319_usage_billing'),
        ('claws', '0016_merge_hive344_migration_graph'),
    ]

    operations = []
