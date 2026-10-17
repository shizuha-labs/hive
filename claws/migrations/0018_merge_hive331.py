from django.db import migrations


class Migration(migrations.Migration):
    """HIVE-331: merge the two dangling claws migration leaves
    (`0015_merge_20260702_1722` from HIVE-313 #158 and
    `0017_merge_hive319_hive344`) into a single leaf for fresh-DB migrate/test."""

    dependencies = [
        ("claws", "0015_merge_20260702_1722"),
        ("claws", "0017_merge_hive319_hive344"),
    ]

    operations = []
