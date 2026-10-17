from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('claws', '0023_alter_tokenpool_provider'),
    ]

    operations = [
        migrations.AddField(
            model_name='tokenentry',
            name='quota_snapshot',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
