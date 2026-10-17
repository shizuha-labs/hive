import hashlib
import json
import uuid

from django.db import migrations, models
import django.utils.timezone


def _hash(payload):
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), default=str).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def backfill_legacy_usage_events(apps, schema_editor):
    UsageEvent = apps.get_model('claws', 'UsageEvent')
    AdmissionAuthoritySnapshot = apps.get_model('claws', 'AdmissionAuthoritySnapshot')
    for event in UsageEvent.objects.all().iterator(chunk_size=500):
        event_id = event.idempotency_key or str(event.id)
        billing_account_id = event.tenant_org_id or (
            f'user:{event.owner_id}' if event.owner_id is not None else 'platform'
        )
        proof_id = 'legacy-backfill:' + hashlib.sha256(
            f'{event.source}\0{event_id}'.encode('utf-8')
        ).hexdigest()[:40]
        authority_claims = {
            'billing_account_id': billing_account_id,
            'reservation_id': event_id,
            'lease_id': '',
            'payer_link_generation': 0,
            'binding_generation': 0,
            'admission_generation': 0,
        }
        proof_hash = _hash(authority_claims)
        authority = {
            **authority_claims,
            'proof_id': proof_id,
            'proof_sha256': proof_hash,
        }
        AdmissionAuthoritySnapshot.objects.get_or_create(
            proof_id=proof_id,
            defaults={
                'proof_sha256': proof_hash,
                **authority_claims,
                'snapshot': authority,
            },
        )
        payload = {
            'specversion': '1.0',
            'id': event_id,
            'source': event.source,
            'type': f'shizuha.usage.{event.metric}.v1',
            'subject': billing_account_id,
            'dataschema': 'urn:shizuha:metering:usage-event:v1',
            'datacontenttype': 'application/json',
            'time': event.occurred_at.isoformat(),
            'data': {
                'meter': event.metric,
                'quantity': str(event.quantity),
                'unit': event.unit,
                'model': event.model,
                'resource_id': event.resource_id,
                'metadata': event.metadata,
            },
            'billingaccountid': billing_account_id,
            'authorityproofid': proof_id,
            'authorityproofhash': proof_hash,
            'reservationid': event_id,
            'payerlinkgen': 0,
            'bindinggen': 0,
            'admissiongen': 0,
        }
        event.event_id = event_id
        event.specversion = '1.0'
        event.event_type = payload['type']
        event.subject = billing_account_id
        event.dataschema = payload['dataschema']
        event.datacontenttype = payload['datacontenttype']
        event.event_payload = payload
        event.payload_sha256 = _hash(payload)
        event.billing_account_id = billing_account_id
        event.authority_snapshot = authority
        event.authority_proof_id = authority['proof_id']
        event.authority_proof_sha256 = proof_hash
        event.reservation_id = event_id
        event.payer_link_generation = 0
        event.binding_generation = 0
        event.admission_generation = 0
        event.save(update_fields=[
            'event_id', 'specversion', 'event_type', 'subject', 'dataschema',
            'datacontenttype', 'event_payload', 'payload_sha256',
            'billing_account_id', 'authority_snapshot', 'authority_proof_id',
            'authority_proof_sha256', 'reservation_id',
            'payer_link_generation', 'binding_generation', 'admission_generation',
        ])


class Migration(migrations.Migration):
    dependencies = [('claws', '0027_tokenentry_token_hint')]

    operations = [
        migrations.AlterField(
            model_name='usageevent',
            name='source',
            field=models.CharField(db_index=True, max_length=512),
        ),
        migrations.AddField(
            model_name='usageevent', name='event_id',
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name='usageevent', name='specversion',
            field=models.CharField(default='1.0', max_length=16),
        ),
        migrations.AddField(
            model_name='usageevent', name='event_type',
            field=models.CharField(blank=True, db_index=True, max_length=255),
        ),
        migrations.AddField(
            model_name='usageevent', name='subject',
            field=models.CharField(blank=True, db_index=True, max_length=512),
        ),
        migrations.AddField(
            model_name='usageevent', name='dataschema',
            field=models.CharField(blank=True, max_length=512),
        ),
        migrations.AddField(
            model_name='usageevent', name='datacontenttype',
            field=models.CharField(blank=True, default='application/json', max_length=255),
        ),
        migrations.AddField(
            model_name='usageevent', name='event_payload',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='usageevent', name='payload_sha256',
            field=models.CharField(blank=True, db_index=True, max_length=64),
        ),
        migrations.AddField(
            model_name='usageevent', name='billing_account_id',
            field=models.CharField(blank=True, db_index=True, max_length=128),
        ),
        migrations.AddField(
            model_name='usageevent', name='authority_snapshot',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='usageevent', name='authority_proof_id',
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name='usageevent', name='authority_proof_sha256',
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name='usageevent', name='reservation_id',
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name='usageevent', name='lease_id',
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name='usageevent', name='payer_link_generation',
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='usageevent', name='binding_generation',
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='usageevent', name='admission_generation',
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='usageevent', name='correction_of_source',
            field=models.CharField(blank=True, max_length=512),
        ),
        migrations.AddField(
            model_name='usageevent', name='correction_of_event_id',
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.CreateModel(
            name='AdmissionAuthoritySnapshot',
            fields=[
                ('proof_id', models.CharField(max_length=255, primary_key=True, serialize=False)),
                ('proof_sha256', models.CharField(max_length=64, unique=True)),
                ('billing_account_id', models.CharField(db_index=True, max_length=128)),
                ('reservation_id', models.CharField(blank=True, max_length=255)),
                ('lease_id', models.CharField(blank=True, max_length=255)),
                ('payer_link_generation', models.BigIntegerField()),
                ('binding_generation', models.BigIntegerField()),
                ('admission_generation', models.BigIntegerField()),
                ('snapshot', models.JSONField(default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={'ordering': ['-created_at']},
        ),
        migrations.RunPython(backfill_legacy_usage_events, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='usageevent',
            constraint=models.UniqueConstraint(
                condition=models.Q(('event_id__isnull', False)),
                fields=('source', 'event_id'),
                name='uniq_usage_event_cloudevent_source_id',
            ),
        ),
        migrations.AddIndex(
            model_name='usageevent',
            index=models.Index(
                fields=['billing_account_id', 'occurred_at'],
                name='usage_event_billing_occur_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='usageevent',
            index=models.Index(
                fields=['correction_of_source', 'correction_of_event_id'],
                name='usage_event_correction_idx',
            ),
        ),
        migrations.CreateModel(
            name='UsageEventOutbox',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('source', models.CharField(max_length=512)),
                ('event_id', models.CharField(max_length=255)),
                ('envelope', models.JSONField()),
                ('payload_sha256', models.CharField(max_length=64)),
                ('status', models.CharField(
                    choices=[('pending', 'Pending'), ('delivered', 'Delivered'), ('conflict', 'Idempotency conflict')],
                    db_index=True, default='pending', max_length=16,
                )),
                ('attempts', models.PositiveIntegerField(default=0)),
                ('next_attempt_at', models.DateTimeField(default=django.utils.timezone.now, db_index=True)),
                ('last_error', models.TextField(blank=True)),
                ('delivered_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'ordering': ['next_attempt_at', 'created_at'],
                'indexes': [
                    models.Index(fields=['status', 'next_attempt_at'], name='usage_outbox_status_due_idx'),
                ],
                'constraints': [
                    models.UniqueConstraint(
                        fields=('source', 'event_id'),
                        name='uniq_usage_outbox_cloudevent_source_id',
                    ),
                ],
            },
        ),
    ]
