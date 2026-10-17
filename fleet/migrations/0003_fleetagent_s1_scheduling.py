# Generated for PLAT-497: per-agent fleet scheduling/storage knobs.
from django.db import migrations, models

import fleet.models


class Migration(migrations.Migration):
    dependencies = [
        ('fleet', '0002_fleetagent_model'),
    ]

    operations = [
        migrations.AddField(
            model_name='fleetagent',
            name='node_pool',
            field=models.CharField(blank=True, help_text='Optional node-pool selector value; emits shizuha.io/node-pool=<value>.', max_length=100),
        ),
        migrations.AddField(
            model_name='fleetagent',
            name='node_name',
            field=models.CharField(blank=True, help_text='Optional hard node pin; emits kubernetes.io/hostname=<value> (e.g. s1).', max_length=100),
        ),
        migrations.AddField(
            model_name='fleetagent',
            name='tolerations',
            field=models.JSONField(blank=True, default=fleet.models._default_tolerations, help_text='Optional Kubernetes tolerations list for this agent (dicts with key/operator/value/effect).'),
        ),
        migrations.AddField(
            model_name='fleetagent',
            name='storage_class',
            field=models.CharField(blank=True, help_text='Optional per-agent PVC StorageClass; blank → settings.RUNTIME_STORAGE_CLASS.', max_length=100),
        ),
    ]
