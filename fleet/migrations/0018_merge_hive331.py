from django.db import migrations


class Migration(migrations.Migration):
    """HIVE-331: merge the two dangling fleet migration leaves so a fresh-DB
    `migrate`/`test` has a single leaf. `0014_merge_20260702_1722` (from HIVE-313
    #158) was never folded into the later HIVE-344 graph-fix chain, leaving main
    with two leaves alongside `0017_plat1282_review_github_grant_scope`."""

    dependencies = [
        ("fleet", "0014_merge_20260702_1722"),
        ("fleet", "0017_plat1282_review_github_grant_scope"),
    ]

    operations = []
