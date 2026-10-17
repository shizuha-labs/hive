"""PLAT-5850: per-account model-capability allowlist on TokenEntry.

The coordinator's pick_token previously had no knowledge of which ChatGPT
account's plan supports the agent's pinned model, so an exhausted/unsupported
account (FREE or trading ChatGPT) was leased to codex agents and every turn
failed "model is not supported" — freezing activity feeds. This field lets a
probe/operator mark an account's supported models; pick_token skips entries
whose allowlist does not contain the requested model.

Canonical model moved to hive_platform (HIVE-2021); the db_table is unchanged
(claws_tokenentry), so this migration targets hive_platform.TokenEntry.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('hive_platform', '0003_admissionauthoritysnapshot_usagedailyrollup_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='tokenentry',
            name='supported_models',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
