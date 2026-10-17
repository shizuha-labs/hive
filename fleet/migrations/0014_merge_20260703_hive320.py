# Merge existing fleet migration leaves so unrelated Hive tests can build a graph.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0013_hive296_health_models'),
        ('fleet', '0013_merge_20260702_1905'),
    ]

    operations = []
