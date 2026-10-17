from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('fleet', '0054_hiveorgproviderreference_lifecycle')]

    operations = [
        migrations.AddField(
            model_name='fleetagent',
            name='native_lifecycle_receipt',
            field=models.JSONField(blank=True, default=dict, editable=False),
        ),
    ]
