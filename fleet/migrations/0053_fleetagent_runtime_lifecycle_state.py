from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0052_agent_mcp_projection_state'),
        ('fleet', '0050_engineering_cluster_plane_kai'),
    ]

    operations = [
        migrations.AddField(
            model_name='fleetagent',
            name='runtime_lifecycle_state',
            field=models.CharField(
                blank=True,
                default='',
                help_text=(
                    'Latest typed lifecycle intent confirmed by the managing '
                    'runtime: enabled, hibernated, or operator_stopped.'
                ),
                max_length=32,
            ),
        ),
    ]
