from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('fleet', '0051_merge_social_media_browser_mcp')]

    operations = [
        migrations.CreateModel(
            name='AgentMcpProjectionState',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('generation', models.PositiveBigIntegerField(default=1)),
                ('fingerprint', models.CharField(max_length=64)),
                ('authority_state', models.CharField(default='authority_unknown', max_length=32)),
                ('current_high_water', models.PositiveBigIntegerField(default=1)),
                ('lease_deadline', models.DateTimeField(blank=True, null=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('agent', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='mcp_projection_state', to='fleet.fleetagent')),
            ],
        ),
    ]
