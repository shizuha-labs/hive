import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('hive_platform', '0005_tokenentry_cooldown_reason'),
    ]

    operations = [
        migrations.CreateModel(
            name='ProviderQuotaAlert',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('org_id', models.CharField(db_index=True, max_length=128)),
                ('condition', models.CharField(choices=[
                    ('low_quota', 'Low quota'),
                    ('stale_snapshot', 'Stale snapshot'),
                    ('fetch_error', 'Fetch error'),
                    ('revoked_token', 'Revoked token'),
                    ('unexpected_cooldown', 'Unexpected cooldown'),
                    ('token_expiry', 'Token expiry'),
                ], max_length=32)),
                ('state', models.CharField(choices=[('open', 'Open'), ('resolved', 'Resolved')], default='open', max_length=16)),
                ('pulse_item_key', models.CharField(blank=True, default='', max_length=64)),
                ('snapshot_version', models.CharField(blank=True, default='', max_length=64)),
                ('diagnostic', models.CharField(blank=True, default='', max_length=64)),
                ('opened_at', models.DateTimeField(auto_now_add=True)),
                ('resolved_at', models.DateTimeField(blank=True, null=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('entry', models.ForeignKey(on_delete=models.deletion.CASCADE, related_name='quota_alerts', to='hive_platform.tokenentry')),
            ],
            options={'db_table': 'claws_providerquotaalert'},
        ),
        migrations.AddConstraint(
            model_name='providerquotaalert',
            constraint=models.UniqueConstraint(
                condition=models.Q(state='open'),
                fields=('org_id', 'entry', 'condition'),
                name='uniq_open_quota_alert_org_entry_condition',
            ),
        ),
    ]
