# Merge HIVE-126 token-entry metadata with HIVE-127 token leases.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("claws", "0007_hive126_token_entry_metadata"),
        ("claws", "0007_hive127_token_lease"),
    ]

    operations = []
