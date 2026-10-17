from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('claws', '0024_tokenentry_quota_snapshot'),
    ]

    operations = [
        migrations.AddField(
            model_name='tokenentry',
            name='account_email',
            field=models.CharField(blank=True, default='', max_length=254),
        ),
    ]
