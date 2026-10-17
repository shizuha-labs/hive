"""ADR-0080 / HIVE-2018: move token-coordinator models out of `claws`.

The token-coordinator models (TokenPool, TokenEntry, TokenEntryLeasingEvent,
TokenStickyAssignment, TokenLease) now live in `hive_platform` (migration
hive_platform/0001). This migration removes them from the `claws` app STATE only
(database_operations=[]) — the underlying `claws_*` tables are preserved and
owned by hive_platform via `db_table`, so no data is dropped.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('claws', '0040_marketplacetemplate_marketplacetemplateauditevent_and_more'),
        ('hive_platform', '0001_initial'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.DeleteModel(name='TokenPool'),
                migrations.DeleteModel(name='TokenEntry'),
                migrations.DeleteModel(name='TokenEntryLeasingEvent'),
                migrations.DeleteModel(name='TokenStickyAssignment'),
                migrations.DeleteModel(name='TokenLease'),
            ],
            database_operations=[],
        ),
    ]
