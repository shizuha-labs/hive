from django.db import migrations, models


def preserve_manual_reservations(apps, schema_editor):
    try:
        TokenEntry = apps.get_model('claws', 'TokenEntry')
    except LookupError:
        # ADR-0080 / HIVE-2018: TokenEntry moved to hive_platform. During fresh
        # replay it is still in claws at this point in the graph; when invoked
        # with the current state (after claws/0040) use the hive_platform home.
        TokenEntry = apps.get_model('hive_platform', 'TokenEntry')
    (
        TokenEntry.objects.filter(
            active=False,
            cooldown_until__isnull=True,
        )
        .exclude(last_status="auth_failed")
        .update(agent_leasing_enabled=False)
    )


class Migration(migrations.Migration):
    dependencies = [
        ("claws", "0029_tokenentry_deletion_tombstone"),
    ]

    operations = [
        migrations.AddField(
            model_name="tokenentry",
            name="agent_leasing_enabled",
            field=models.BooleanField(db_index=True, default=True),
        ),
        migrations.RunPython(
            preserve_manual_reservations,
            migrations.RunPython.noop,
        ),
    ]
