# Merge HIVE-346 plan catalog branch with HIVE-331 migration leaf.
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('claws', '0018_hive346_plan_catalog_rates'),
        ('claws', '0018_merge_hive331'),
    ]

    operations = []
