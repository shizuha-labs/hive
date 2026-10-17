from django.db import migrations


def project_existing_snapshots(apps, schema_editor):
    from claws.quota_projection_v1 import project_quota_snapshot

    try:
        TokenEntry = apps.get_model('claws', 'TokenEntry')
    except LookupError:
        # ADR-0080 / HIVE-2018: TokenEntry moved to hive_platform. During fresh
        # replay it is still in claws at this point in the graph; when invoked
        # with the current state (after claws/0040) use the hive_platform home.
        TokenEntry = apps.get_model('hive_platform', 'TokenEntry')
    for entry in TokenEntry.objects.only('pk', 'quota_snapshot').iterator(chunk_size=500):
        projected = project_quota_snapshot(entry.quota_snapshot)
        if projected != (entry.quota_snapshot or {}):
            TokenEntry.objects.filter(pk=entry.pk).update(quota_snapshot=projected)


class Migration(migrations.Migration):
    dependencies = [
        ('claws', '0038_plat4485_usage_rollup_rating_statements'),
    ]

    operations = [
        migrations.RunPython(project_existing_snapshots, migrations.RunPython.noop),
    ]
