from django.db import migrations


class Migration(migrations.Migration):
    """PLAT-1251: unify the fleet migration graph after rebasing on a main that
    merged HIVE-331/HIVE-346. Folds the base-MCP-floor data migration
    (0013_plat1251_base_team_mcp_refs) into main's current leaf
    (0018_merge_hive331) so a fresh-DB migrate/test has a single fleet leaf.
    Replaces the branch's earlier 0014_merge_20260702_1921 (dropped — main's
    newer merge nodes subsumed the leaves it linearized)."""

    dependencies = [
        ("fleet", "0018_merge_hive331"),
        ("fleet", "0013_plat1251_base_team_mcp_refs"),
    ]

    operations = []
