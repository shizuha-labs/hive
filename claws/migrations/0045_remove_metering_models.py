"""ADR-0080 / HIVE-2018: move metering models out of `claws`.

The metering models (AdmissionAuthoritySnapshot, UsageEvent, UsageEventOutbox,
UsageDailyRollup, UsageInvoiceDraft) now live in `hive_platform` (migration
hive_platform/0003). This migration removes them from the `claws` app STATE only
(database_operations=[]) — the underlying `claws_*` tables are preserved and
owned by hive_platform via `db_table`, so no data is dropped.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('claws', '0044_remove_billing_models'),
        ('hive_platform', '0003_admissionauthoritysnapshot_usagedailyrollup_and_more'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(
                    model_name='usageevent',
                    name='claw',
                ),
                migrations.DeleteModel(name='AdmissionAuthoritySnapshot'),
                migrations.DeleteModel(name='UsageEvent'),
                migrations.DeleteModel(name='UsageEventOutbox'),
                migrations.DeleteModel(name='UsageDailyRollup'),
                migrations.DeleteModel(name='UsageInvoiceDraft'),
            ],
            database_operations=[],
        ),
    ]
