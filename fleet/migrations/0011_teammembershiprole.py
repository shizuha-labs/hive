# Generated for HIVE-287 on 2026-07-02.

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0010_merge_hive273_hivecredential_grants'),
    ]

    operations = [
        migrations.CreateModel(
            name='TeamMembershipRole',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('agent_identifier', models.CharField(db_index=True, max_length=255)),
                (
                    'team_slug',
                    models.CharField(
                        db_index=True,
                        max_length=80,
                        validators=[
                            django.core.validators.RegexValidator(
                                message='slug must be kebab-case lowercase alphanumeric/hyphen.',
                                regex='^[a-z0-9]([-a-z0-9]*[a-z0-9])?$',
                            )
                        ],
                    ),
                ),
                ('role_title', models.CharField(blank=True, max_length=120)),
                (
                    'membership_role',
                    models.CharField(
                        choices=[
                            ('member', 'Member'),
                            ('lead', 'Lead'),
                            ('reviewer', 'Reviewer'),
                            ('operator', 'Operator'),
                            ('observer', 'Observer'),
                            ('custom', 'Custom'),
                        ],
                        default='member',
                        max_length=24,
                    ),
                ),
                ('tier', models.CharField(blank=True, max_length=80)),
                ('enabled', models.BooleanField(db_index=True, default=True)),
                ('source', models.CharField(blank=True, default='hive', max_length=80)),
                ('reason', models.TextField(blank=True)),
                ('created_by', models.CharField(blank=True, max_length=255)),
                ('updated_by', models.CharField(blank=True, max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['team_slug', 'agent_identifier'],
                'unique_together': {('agent_identifier', 'team_slug')},
            },
        ),
    ]
