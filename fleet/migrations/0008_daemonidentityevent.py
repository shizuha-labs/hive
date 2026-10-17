"""HIVE-254 P5: DaemonIdentityEvent model — persists identity_event frames."""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0007_fleetagent_managing_daemon'),
    ]

    operations = [
        migrations.CreateModel(
            name='DaemonIdentityEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('child_id', models.CharField(db_index=True, max_length=256)),
                ('username', models.CharField(blank=True, max_length=150)),
                ('verdict', models.CharField(
                    choices=[('admitted', 'Admitted'), ('repaired', 'Repaired'), ('rejected', 'Rejected')],
                    max_length=16,
                )),
                ('reason', models.TextField(blank=True)),
                ('seen_at', models.DateTimeField()),
                ('resolved_at', models.DateTimeField(blank=True, null=True)),
                ('daemon', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='identity_events',
                    to='fleet.daemonregistry',
                )),
            ],
            options={
                'ordering': ['-seen_at'],
            },
        ),
        migrations.AddIndex(
            model_name='daemonidentityevent',
            index=models.Index(fields=['daemon', 'resolved_at'], name='fleet_daemo_daemon_i_idx'),
        ),
    ]
