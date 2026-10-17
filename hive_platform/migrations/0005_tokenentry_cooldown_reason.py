# HIVE-1747: persist the report-status reason that stamped a cooldown so the
# pre-dry self-heal sweep cannot re-lease a provider-confirmed quota-exhausted
# credential for its requested window (even when the usage API is unreadable).
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('hive_platform', '0004_tokenentry_supported_models'),
    ]

    operations = [
        migrations.AddField(
            model_name='tokenentry',
            name='cooldown_reason',
            field=models.CharField(blank=True, default='', max_length=32),
        ),
    ]
