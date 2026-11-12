from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('hive_platform', '0006_providerquotaalert'),
    ]

    operations = [
        migrations.AddField(
            model_name='providerquotaalert',
            name='actor',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='providerquotaalert',
            name='service',
            field=models.CharField(blank=True, default='', max_length=32),
        ),
        migrations.AddField(
            model_name='providerquotaalert',
            name='last_transition',
            field=models.CharField(blank=True, default='', max_length=16),
        ),
    ]
