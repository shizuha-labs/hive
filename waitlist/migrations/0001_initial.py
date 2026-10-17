import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='WaitlistEntry',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('email', models.EmailField(db_index=True, max_length=254, unique=True)),
                ('owner_id', models.IntegerField(blank=True, db_index=True, null=True)),
                ('owner_username', models.CharField(blank=True, max_length=150)),
                ('preferred_channel', models.CharField(choices=[('web', 'Web'), ('app', 'App'), ('whatsapp', 'WhatsApp')], default='web', max_length=20)),
                ('top_use_case', models.CharField(blank=True, max_length=500)),
                ('source', models.CharField(blank=True, default='hive-free-claw-landing', max_length=100)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'waitlist entry',
                'verbose_name_plural': 'waitlist entries',
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='WaitlistEvent',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('event_type', models.CharField(choices=[('visit', 'Visit'), ('start', 'Start (form interaction)'), ('submit', 'Submit'), ('channel_select', 'Channel selected')], db_index=True, max_length=20)),
                ('session_key', models.CharField(blank=True, db_index=True, max_length=64)),
                ('channel', models.CharField(blank=True, max_length=20)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('entry', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='events', to='waitlist.waitlistentry')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='waitlistevent',
            index=models.Index(fields=['event_type', 'created_at'], name='waitlist_wa_event_t_04f8af_idx'),
        ),
    ]
