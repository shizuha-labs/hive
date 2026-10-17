"""HIVE-201 E1: WhatsAppLink — bind a WA phone_jid to a Claw via a minted code."""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('claws', '0010_claw_agent_type_claw_model'),
    ]

    operations = [
        migrations.CreateModel(
            name='WhatsAppLink',
            fields=[
                ('code', models.CharField(max_length=64, primary_key=True, serialize=False)),
                ('owner_id', models.IntegerField(db_index=True)),
                ('wa_phone_jid', models.CharField(blank=True, db_index=True, default='', max_length=64)),
                ('status', models.CharField(
                    choices=[('pending', 'Pending'), ('linked', 'Linked'), ('expired', 'Expired')],
                    db_index=True, default='pending', max_length=16,
                )),
                ('expires_at', models.DateTimeField(db_index=True)),
                ('linked_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('claw', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='wa_links', to='claws.claw',
                )),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
    ]
