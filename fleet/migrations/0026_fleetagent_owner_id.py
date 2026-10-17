from django.db import migrations, models


class Migration(migrations.Migration):
    """HIVE-620/624: owner of a personal agent.

    Additive nullable BigInteger, default NULL, so every existing fleet/org agent
    stays unowned and unchanged. A non-null owner_id marks a per-user personal
    agent (the free "Shizuha" every user gets).
    """

    dependencies = [
        ('fleet', '0025_fleetagentarchive'),
    ]

    operations = [
        migrations.AddField(
            model_name='fleetagent',
            name='owner_id',
            field=models.BigIntegerField(blank=True, db_index=True, default=None, null=True),
        ),
    ]
