# HIVE-2862: default critical-team floor config ships as policy rows, not a
# code constant. Operator directive: each critical team's reservation is a
# Hive setting (admin-editable) with a DEPLOYED default; this migration seeds
# it so a fresh fleet reserves merge/review/devops from the first tick, and
# later edits happen in the admin with no redeploy.

from django.db import migrations

DEFAULT_FLOOR = {
    'merge': 1,
    'review': 1,
    'devops': 1,
}


def forwards(apps, schema_editor):
    LeaseCriticalTeamPolicy = apps.get_model('fleet', 'LeaseCriticalTeamPolicy')
    existing = {
        row.team_slug
        for row in LeaseCriticalTeamPolicy.objects.filter(enabled=True)
    }
    for slug, min_leases in DEFAULT_FLOOR.items():
        if slug in existing:
            continue
        LeaseCriticalTeamPolicy.objects.create(
            team_slug=slug,
            min_leases=min_leases,
            enabled=True,
            note='default floor (HIVE-2862): fair reservation for every critical team',
        )


def backwards(apps, schema_editor):
    LeaseCriticalTeamPolicy = apps.get_model('fleet', 'LeaseCriticalTeamPolicy')
    LeaseCriticalTeamPolicy.objects.filter(
        team_slug__in=DEFAULT_FLOOR.keys(),
        note__contains='default floor (HIVE-2862)',
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0063_leasecriticalteampolicy'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
