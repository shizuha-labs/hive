from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('fleet', '0026_fleetagent_owner_id')]

    operations = [
        migrations.AddField(
            model_name='fleetagent',
            name='identity_provisioned_at',
            field=models.DateTimeField(
                blank=True,
                help_text='When the dedicated @agents.shizuha.io identity was confirmed.',
                null=True,
            ),
        ),
        migrations.AddField(
            model_name='fleetagent',
            name='owner_username',
            field=models.CharField(
                blank=True,
                default='',
                help_text='Non-authoritative owner display handle; owner_id is the stable key.',
                max_length=150,
            ),
        ),
        migrations.AlterField(
            model_name='fleetagent',
            name='owner_id',
            field=models.BigIntegerField(
                blank=True,
                db_index=True,
                default=None,
                help_text='Immutable Shizuha-ID owner subject; NULL means an org/team fleet agent.',
                null=True,
            ),
        ),
        migrations.AddConstraint(
            model_name='fleetagent',
            constraint=models.UniqueConstraint(
                condition=models.Q(owner_id__isnull=False),
                fields=('owner_id',),
                name='fleet_one_personal_agent_per_owner',
            ),
        ),
    ]
