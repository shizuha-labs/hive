from django.db import migrations, models


class Migration(migrations.Migration):
    """HIVE-356: add TenantProvisioningRun.pilot_prep (design-partner pilot-prep
    intake + launch-gate state). Additive nullable-default JSON field — safe.

    Rebased (kei/aoi ruling): HIVE-331 #156 landed first, adding
    claws/0019_merge_hive346_hive331 as the single claws leaf. This migration is
    re-pointed to depend on that leaf and renumbered 0018 -> 0020 so the graph
    keeps a single leaf after merge (was dual-leaf before the rebase).
    """

    dependencies = [
        ('claws', '0019_merge_hive346_hive331'),
    ]

    operations = [
        migrations.AddField(
            model_name='tenantprovisioningrun',
            name='pilot_prep',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
