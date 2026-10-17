from django.db import migrations, models


class Migration(migrations.Migration):
    """HIVE-620: persist exact ID user/profile rows, never mutable-name joins."""

    dependencies = [('fleet', '0027_fleetagent_personal_owner_identity')]

    operations = [
        migrations.AddField(
            model_name='fleetagent',
            name='owner_profile_id',
            field=models.BigIntegerField(
                blank=True,
                db_index=True,
                default=None,
                help_text='Exact immutable Shizuha-ID UserProfile row for owner_id.',
                null=True,
            ),
        ),
        migrations.AddField(
            model_name='fleetagent',
            name='identity_user_id',
            field=models.BigIntegerField(
                blank=True,
                default=None,
                help_text='Exact dedicated Shizuha-ID user row for this agent identity.',
                null=True,
                unique=True,
            ),
        ),
        migrations.AddField(
            model_name='fleetagent',
            name='identity_profile_id',
            field=models.BigIntegerField(
                blank=True,
                default=None,
                help_text='Exact dedicated Shizuha-ID UserProfile row for this agent identity.',
                null=True,
                unique=True,
            ),
        ),
    ]
