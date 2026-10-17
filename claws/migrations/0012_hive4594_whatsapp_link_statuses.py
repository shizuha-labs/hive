"""HIVE-4594 E2: dashboard-visible WhatsApp link states."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('claws', '0011_hive201_whatsapp_link'),
    ]

    operations = [
        migrations.AlterField(
            model_name='whatsapplink',
            name='status',
            field=models.CharField(
                choices=[
                    ('pending', 'Pending'),
                    ('linked', 'Linked'),
                    ('paused', 'Paused'),
                    ('rate_limited', 'Rate limited'),
                    ('expired', 'Expired'),
                ],
                db_index=True,
                default='pending',
                max_length=16,
            ),
        ),
    ]
