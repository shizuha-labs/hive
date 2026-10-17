"""HIVE-235: DaemonCredential + DaemonRegistry models."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0004_fleetagent_behavioral_fields'),
    ]

    operations = [
        migrations.CreateModel(
            name='DaemonCredential',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('daemon_id', models.CharField(db_index=True, max_length=128)),
                ('token_hash', models.CharField(max_length=64, unique=True)),
                ('revoked', models.BooleanField(db_index=True, default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('revoked_at', models.DateTimeField(blank=True, null=True)),
            ],
            options={'ordering': ['-created_at']},
        ),
        migrations.CreateModel(
            name='DaemonRegistry',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('daemon_id', models.CharField(db_index=True, max_length=128, unique=True)),
                ('fleet_id', models.CharField(db_index=True, max_length=128)),
                ('runtime', models.CharField(
                    choices=[('docker', 'Docker'), ('k3s', 'k3s')],
                    default='docker',
                    max_length=16,
                )),
                ('cred_ref', models.CharField(blank=True, max_length=256)),
                ('last_seen', models.DateTimeField(blank=True, null=True)),
                ('last_acked_seq', models.PositiveBigIntegerField(default=0)),
                ('status', models.CharField(
                    choices=[
                        ('connected', 'Connected'),
                        ('disconnected', 'Disconnected'),
                        ('degraded', 'Degraded'),
                        ('down', 'Down'),
                    ],
                    default='disconnected',
                    max_length=16,
                )),
                ('snapshot_complete', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name_plural': 'Daemon registries',
                'ordering': ['-updated_at'],
            },
        ),
    ]
