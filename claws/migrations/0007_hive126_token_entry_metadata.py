# Generated for HIVE-126 token-pool configuration surface.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("claws", "0007_hive124_coordinator_metrics"),
    ]

    operations = [
        migrations.AddField(
            model_name="tokenentry",
            name="usage_count",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="tokenentry",
            name="tier",
            field=models.CharField(blank=True, default="", max_length=50),
        ),
    ]
