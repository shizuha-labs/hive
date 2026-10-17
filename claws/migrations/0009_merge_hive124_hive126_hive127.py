# Merge hive124 coordinator-metrics leaf with the hive126+hive127 merge migration.
# Required because 0008_merge_hive126_hive127 covered hive126+hive127 but left
# 0007_hive124_coordinator_metrics as an un-merged leaf node.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('claws', '0008_merge_hive126_hive127'),
        ('claws', '0007_hive124_coordinator_metrics'),
    ]

    operations = []
