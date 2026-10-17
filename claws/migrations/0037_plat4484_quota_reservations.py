# Generated manually for PLAT-4484 Wave 1 — atomic reservations / leases / proofs / writer authority

import claws.metering_models
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('claws', '0036_plat4481_plat4483_metering_core'),
    ]

    operations = [
        migrations.CreateModel(
            name='AdmissionWriterAuthority',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('product', models.CharField(db_index=True, max_length=64)),
                ('generation', models.BigIntegerField()),
                ('state', models.CharField(choices=[('active', 'Active'), ('freezing', 'Freezing'), ('importing', 'Importing'), ('retired', 'Retired')], db_index=True, default='active', max_length=16)),
                ('source_watermark', models.CharField(blank=True, default='', max_length=255)),
                ('cutover_manifest_sha256', models.CharField(blank=True, default='', max_length=64)),
                ('activated_at', models.DateTimeField(blank=True, null=True)),
                ('retired_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('metadata', models.JSONField(blank=True, default=claws.metering_models._json_dict)),
            ],
            options={
                'ordering': ['-generation'],
            },
        ),
        migrations.CreateModel(
            name='ProducerHolder',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('holder_key', models.CharField(max_length=255, unique=True)),
                ('epoch', models.BigIntegerField(default=1)),
                ('incarnation_id', models.CharField(max_length=64)),
                ('last_heartbeat_at', models.DateTimeField(auto_now=True)),
                ('metadata', models.JSONField(blank=True, default=claws.metering_models._json_dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['holder_key'],
            },
        ),
        migrations.CreateModel(
            name='QuotaReservation',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('decision_id', models.CharField(db_index=True, max_length=64, unique=True)),
                ('reserve_idempotency_key', models.CharField(db_index=True, max_length=255)),
                ('reserve_payload_sha256', models.CharField(max_length=64)),
                ('start_idempotency_key', models.CharField(blank=True, default='', max_length=255)),
                ('start_payload_sha256', models.CharField(blank=True, default='', max_length=64)),
                ('terminal_idempotency_key', models.CharField(blank=True, default='', max_length=255)),
                ('terminal_payload_sha256', models.CharField(blank=True, default='', max_length=64)),
                ('meter_key', models.CharField(db_index=True, max_length=128)),
                ('meter_version', models.PositiveIntegerField(default=1)),
                ('unit', models.CharField(max_length=64)),
                ('resource_uid', models.CharField(db_index=True, max_length=255)),
                ('producer_principal', models.CharField(blank=True, default='', max_length=255)),
                ('dimensions', models.JSONField(blank=True, default=claws.metering_models._json_dict)),
                ('original_reserved', models.DecimalField(decimal_places=6, max_digits=28)),
                ('committed_actual', models.DecimalField(decimal_places=6, default=0, max_digits=28)),
                ('released_unused', models.DecimalField(decimal_places=6, default=0, max_digits=28)),
                ('held_unknown', models.DecimalField(decimal_places=6, default=0, max_digits=28)),
                ('server_rated_spend_reserved_inr_micros', models.BigIntegerField(default=0)),
                ('status', models.CharField(choices=[('reserved', 'Reserved'), ('started', 'Started'), ('committed', 'Committed'), ('released', 'Released'), ('expired_unstarted', 'Expired unstarted'), ('orphaned_hold', 'Orphaned hold')], db_index=True, default='reserved', max_length=32)),
                ('allowed', models.BooleanField(default=True)),
                ('deny_reason', models.CharField(blank=True, default='', max_length=255)),
                ('entitlement_generation', models.BigIntegerField(default=0)),
                ('balance_generation', models.BigIntegerField(default=0)),
                ('rate_version_id', models.CharField(blank=True, default='', max_length=128)),
                ('admission_epoch', models.BigIntegerField()),
                ('occurrence_ref', models.CharField(blank=True, default='', max_length=255)),
                ('outbox_ref', models.CharField(blank=True, default='', max_length=255)),
                ('no_start_proof_sha256', models.CharField(blank=True, default='', max_length=64)),
                ('terminal_proof_sha256', models.CharField(blank=True, default='', max_length=64)),
                ('expires_at', models.DateTimeField(db_index=True)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('terminal_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('response_snapshot', models.JSONField(blank=True, default=claws.metering_models._json_dict)),
                ('authority_proof', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='reservations', to='claws.payerauthorityproof')),
                ('billing_account', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='quota_reservations', to='claws.billingaccount')),
                ('entitlement_balance', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='reservations', to='claws.entitlementbalance')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='SpendLease',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('lease_id', models.CharField(db_index=True, max_length=64, unique=True)),
                ('resource_uid', models.CharField(db_index=True, max_length=255)),
                ('producer_principal', models.CharField(max_length=255)),
                ('producer_holder_epoch', models.BigIntegerField()),
                ('meter_key', models.CharField(max_length=128)),
                ('authorized_quantity', models.DecimalField(decimal_places=6, max_digits=28)),
                ('debited_quantity', models.DecimalField(decimal_places=6, default=0, max_digits=28)),
                ('status', models.CharField(choices=[('active', 'Active'), ('closed', 'Closed'), ('expired', 'Expired'), ('fenced', 'Fenced by holder epoch')], db_index=True, default='active', max_length=16)),
                ('lease_kid', models.CharField(blank=True, default='', max_length=128)),
                ('lease_jws', models.TextField(blank=True, default='')),
                ('close_kid', models.CharField(blank=True, default='', max_length=128)),
                ('close_jws', models.TextField(blank=True, default='')),
                ('issue_idempotency_key', models.CharField(db_index=True, max_length=255)),
                ('issue_payload_sha256', models.CharField(max_length=64)),
                ('close_idempotency_key', models.CharField(blank=True, default='', max_length=255)),
                ('close_payload_sha256', models.CharField(blank=True, default='', max_length=64)),
                ('expires_at', models.DateTimeField(db_index=True)),
                ('closed_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('response_snapshot', models.JSONField(blank=True, default=claws.metering_models._json_dict)),
                ('billing_account', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='spend_leases', to='claws.billingaccount')),
                ('parent_reservation', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='spend_leases', to='claws.quotareservation')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='AdmissionProofReceipt',
            fields=[
                ('receipt_id', models.CharField(max_length=64, primary_key=True, serialize=False)),
                ('profile', models.CharField(choices=[('prepared_operation', 'PreparedOperationReceipt'), ('start_authorization', 'StartAuthorizationReceipt'), ('no_side_effect', 'NoSideEffectProof'), ('terminal_operation', 'TerminalOperationReceipt'), ('spend_lease_issue', 'SpendLeaseIssue'), ('spend_lease_close', 'SpendLeaseClose')], db_index=True, max_length=32)),
                ('payload_sha256', models.CharField(max_length=64)),
                ('jws_compact', models.TextField(blank=True, default='')),
                ('kid', models.CharField(blank=True, default='', max_length=128)),
                ('algorithm', models.CharField(default='EdDSA', max_length=16)),
                ('audience', models.CharField(default='metering:admission', max_length=128)),
                ('payload', models.JSONField(default=claws.metering_models._json_dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('lease', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='proof_receipts', to='claws.spendlease')),
                ('reservation', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='proof_receipts', to='claws.quotareservation')),
            ],
            options={
                'ordering': ['-created_at'],
            },
            bases=(models.Model,),
        ),
        migrations.AddConstraint(
            model_name='admissionwriterauthority',
            constraint=models.UniqueConstraint(fields=('product', 'generation'), name='uniq_admission_writer_product_generation'),
        ),
        migrations.AddConstraint(
            model_name='admissionwriterauthority',
            constraint=models.UniqueConstraint(condition=models.Q(('state', 'active')), fields=('product',), name='uniq_admission_writer_one_active_per_product'),
        ),
        migrations.AddIndex(
            model_name='quotareservation',
            index=models.Index(fields=['billing_account', 'status'], name='claws_quota_billing_0c0a0a_idx'),
        ),
        migrations.AddIndex(
            model_name='quotareservation',
            index=models.Index(fields=['resource_uid', 'status'], name='claws_quota_resourc_1b1b1b_idx'),
        ),
        migrations.AddIndex(
            model_name='quotareservation',
            index=models.Index(fields=['status', 'expires_at'], name='claws_quota_status_2c2c2c_idx'),
        ),
        migrations.AddConstraint(
            model_name='quotareservation',
            constraint=models.UniqueConstraint(fields=('reserve_idempotency_key', 'producer_principal'), name='uniq_quota_reservation_reserve_key_principal'),
        ),
        migrations.AddIndex(
            model_name='spendlease',
            index=models.Index(fields=['billing_account', 'status'], name='claws_spend_billing_3d3d3d_idx'),
        ),
        migrations.AddIndex(
            model_name='spendlease',
            index=models.Index(fields=['resource_uid', 'status'], name='claws_spend_resourc_4e4e4e_idx'),
        ),
        migrations.AddConstraint(
            model_name='spendlease',
            constraint=models.UniqueConstraint(fields=('issue_idempotency_key', 'producer_principal'), name='uniq_spend_lease_issue_key_principal'),
        ),
        migrations.AddIndex(
            model_name='admissionproofreceipt',
            index=models.Index(fields=['profile', 'created_at'], name='claws_admis_profile_5f5f5f_idx'),
        ),
        migrations.AddIndex(
            model_name='admissionproofreceipt',
            index=models.Index(fields=['payload_sha256'], name='claws_admis_payload_6a6a6a_idx'),
        ),
    ]
