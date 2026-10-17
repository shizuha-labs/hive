# Generated manually to merge the HIVE-368 and HIVE-446 claws migration branches.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('claws', '0021_hive368_org_templates'),
        ('claws', '0021_hive446_plan_capability_allowlists'),
    ]

    operations = []
