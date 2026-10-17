from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0062_deprecate_grok_46'),
    ]

    operations = [
        migrations.CreateModel(
            name='LeaseCriticalTeamPolicy',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('team_slug', models.CharField(max_length=128, unique=True)),
                ('min_leases', models.PositiveSmallIntegerField(default=1)),
                ('enabled', models.BooleanField(default=True)),
                ('note', models.CharField(blank=True, default='', max_length=280)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'lease critical team policy',
                'verbose_name_plural': 'lease critical team policies',
                'ordering': ['team_slug'],
            },
        ),
    ]
