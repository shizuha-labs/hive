from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0031_plat4172_needs_help'),
    ]

    operations = [
        migrations.CreateModel(
            name='CapabilityEvidenceProducerGeneration',
            fields=[
                ('generation_id', models.CharField(max_length=96, primary_key=True, serialize=False)),
                ('generation_digest', models.CharField(editable=False, max_length=71, unique=True)),
                ('source_sha', models.CharField(max_length=40)),
                ('source_tree', models.CharField(max_length=40)),
                ('image_index_digest', models.CharField(max_length=71)),
                ('schema_digest', models.CharField(max_length=71)),
                ('materializer_version', models.CharField(max_length=96)),
                ('materializer_digest', models.CharField(max_length=71)),
                ('config_digest', models.CharField(max_length=71)),
                ('service_account', models.CharField(max_length=128)),
                ('signing_kid', models.CharField(max_length=128)),
                ('signing_key_thumbprint', models.CharField(max_length=71)),
                ('activated_at', models.DateTimeField()),
                ('active', models.BooleanField(db_index=True, default=True)),
                ('retired_at', models.DateTimeField(blank=True, null=True)),
            ],
            options={'ordering': ['-activated_at']},
        ),
        migrations.AddConstraint(
            model_name='capabilityevidenceproducergeneration',
            constraint=models.UniqueConstraint(
                condition=models.Q(('active', True)), fields=('active',),
                name='fleet_one_active_cap_evidence_producer',
            ),
        ),
    ]
