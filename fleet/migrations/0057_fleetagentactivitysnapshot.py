from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0056_fleetruntimeoutbox'),
    ]

    operations = [
        migrations.CreateModel(
            name='FleetAgentActivitySnapshot',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('events', models.JSONField(blank=True, default=list)),
                ('newest_event_at', models.DateTimeField(blank=True, null=True)),
                ('captured_at', models.DateTimeField(auto_now=True)),
                ('source', models.CharField(blank=True, max_length=64)),
                ('resource_uid', models.CharField(blank=True, max_length=128)),
                ('pod_name', models.CharField(blank=True, max_length=200)),
                ('source_state_at_capture', models.CharField(blank=True, max_length=32)),
                ('agent', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='activity_snapshot',
                    to='fleet.fleetagent',
                )),
            ],
        ),
    ]
