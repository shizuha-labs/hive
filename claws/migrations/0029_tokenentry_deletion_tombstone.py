from django.db import migrations, models
import django.db.models


class Migration(migrations.Migration):

    dependencies = [
        ('claws', '0028_plat4482_metering_events'),
    ]

    operations = [
        migrations.AddField(
            model_name='tokenentry',
            name='deleted_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='tokenentry',
            name='deleted_by',
            field=models.CharField(blank=True, default='', max_length=254),
        ),
        migrations.RemoveConstraint(
            model_name='tokenentry',
            name='uniq_token_entry_pool_label',
        ),
        migrations.RemoveConstraint(
            model_name='tokenentry',
            name='uniq_token_entry_pool_fingerprint',
        ),
        migrations.AddConstraint(
            model_name='tokenentry',
            constraint=models.UniqueConstraint(
                condition=django.db.models.Q(('deleted_at__isnull', True)),
                fields=('pool', 'label'),
                name='uniq_token_entry_pool_label',
            ),
        ),
        migrations.AddConstraint(
            model_name='tokenentry',
            constraint=models.UniqueConstraint(
                condition=django.db.models.Q(('deleted_at__isnull', True)),
                fields=('pool', 'token_fingerprint'),
                name='uniq_token_entry_pool_fingerprint',
            ),
        ),
    ]
