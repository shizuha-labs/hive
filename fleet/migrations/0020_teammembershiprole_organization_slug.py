from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('fleet', '0019_merge_plat1251_base_mcp'),
    ]

    operations = [
        migrations.AddField(
            model_name='teammembershiprole',
            name='organization_slug',
            field=models.CharField(blank=True, db_index=True, default='', max_length=120),
        ),
        migrations.AlterUniqueTogether(
            name='teammembershiprole',
            unique_together={('agent_identifier', 'team_slug', 'organization_slug')},
        ),
        migrations.AlterModelOptions(
            name='teammembershiprole',
            options={'ordering': ['organization_slug', 'team_slug', 'agent_identifier']},
        ),
    ]
