"""ADR-0080 / HIVE-2018: move billing models out of `claws`.

The billing models (BillingPlan, PricingRate, TenantSubscription) now live in
`hive_platform` (migration hive_platform/0002). This migration removes them from
the `claws` app STATE only (database_operations=[]) — the underlying `claws_*`
tables are preserved and owned by hive_platform via `db_table`, so no data is
dropped.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('claws', '0043_remove_token_models'),
        ('hive_platform', '0002_billingplan_tenantsubscription_pricingrate_and_more'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.DeleteModel(name='BillingPlan'),
                migrations.DeleteModel(name='PricingRate'),
                migrations.DeleteModel(name='TenantSubscription'),
            ],
            database_operations=[],
        ),
    ]
