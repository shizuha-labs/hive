from django.db import migrations, models
import django.utils.timezone
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0060_fleetagent_clusterplane_kubeconfig'),
    ]

    operations = [
        migrations.CreateModel(
            name='HiveOrgCredentialLease',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('grant_id', models.UUIDField(unique=True, editable=False)),
                ('organization_id', models.IntegerField(db_index=True)),
                ('provider', models.CharField(max_length=32)),
                ('runtime_class', models.CharField(blank=True, default='', max_length=80)),
                ('agent_identity', models.CharField(blank=True, db_index=True, default='', max_length=200)),
                ('consumer_token_fingerprint', models.CharField(blank=True, default='', max_length=71)),
                ('status', models.CharField(choices=[('active', 'Active'), ('expired', 'Expired'), ('revoked', 'Revoked')], default='active', max_length=16)),
                ('issued_at', models.DateTimeField(auto_now_add=True)),
                ('expires_at', models.DateTimeField(db_index=True)),
                ('reference', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='credential_leases', to='fleet.hiveorgproviderreference')),
            ],
            options={
                'verbose_name': 'hive org credential lease',
                'ordering': ['-issued_at'],
                # PLS: indexes belong in Meta (options), never a top-level
                # CreateModel kwarg — Django has no such parameter, so the
                # top-level form raised TypeError at import and the backend
                # gate died before any test ran (sara, HIVE-2622 774193).
                'indexes': [
                    # Name must equal the model Meta's auto-generated name
                    # (Index.set_name_with_model over table+fields) or
                    # makemigrations --check drifts a rename to 0060
                    # (run-2912 red lap).
                    models.Index(fields=['reference', 'agent_identity'], name='fleet_hiveo_referen_bec503_idx'),
                ],
            },
        ),
    ]
