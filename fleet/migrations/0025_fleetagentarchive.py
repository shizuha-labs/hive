from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0024_daemonregistry_harness'),
    ]

    operations = [
        migrations.CreateModel(
            name='FleetAgentArchive',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('agent_username', models.CharField(db_index=True, max_length=150, unique=True)),
                ('archived_at', models.DateTimeField(auto_now_add=True)),
                ('archived_by', models.CharField(blank=True, max_length=150)),
                ('reason', models.CharField(blank=True, max_length=300)),
            ],
            options={
                'verbose_name': 'fleet agent archive marker',
            },
        ),
    ]
