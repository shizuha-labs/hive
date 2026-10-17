# HIVE-2161 S3-lite (kumo refinement #3, binding): optional per-runtime-class
# default model ids on the org provider mirror. Additive/null-safe — slice-1
# rows stay NULL (no backfill) and the flat `default_models` list remains live
# in the precedence chain. No destructive change.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0054_hiveorgproviderreference_lifecycle'),
    ]

    operations = [
        migrations.AddField(
            model_name='hiveorgproviderreference',
            name='default_models_by_runtime',
            field=models.JSONField(
                blank=True, default=dict, null=True,
                help_text='Optional {runtime_class: model_id} defaults; NULL/absent '
                          'is skipped without error (flat default_models stays live).',
            ),
        ),
    ]
