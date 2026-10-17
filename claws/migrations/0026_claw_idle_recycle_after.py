from django.db import migrations, models


def move_provision_stamps(apps, schema_editor):
    """HIVE-598: provision-time idle-TTL stamps were written into recycle_after,
    colliding with the delete-retention pool. Move them to idle_recycle_after on
    every claw that is NOT in the delete flow, so recycle_after is exclusively
    delete-retention from here on."""
    Claw = apps.get_model('claws', 'Claw')
    for claw in Claw.objects.filter(recycle_after__isnull=False).exclude(status='deleting'):
        if claw.delete_requested_at is None and not claw.reclaim_now:
            claw.idle_recycle_after = claw.recycle_after
            claw.recycle_after = None
            claw.save(update_fields=['idle_recycle_after', 'recycle_after'])


class Migration(migrations.Migration):

    dependencies = [
        ('claws', '0025_tokenentry_account_email'),
    ]

    operations = [
        migrations.AddField(
            model_name='claw',
            name='idle_recycle_after',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.RunPython(move_provision_stamps, migrations.RunPython.noop),
    ]
