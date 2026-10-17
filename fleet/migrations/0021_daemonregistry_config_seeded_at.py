from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('fleet', '0020_teammembershiprole_organization_slug'),
    ]

    operations = [
        migrations.AddField(
            model_name='daemonregistry',
            name='config_seeded_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
