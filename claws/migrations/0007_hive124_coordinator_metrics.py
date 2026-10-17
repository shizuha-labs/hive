# HIVE-124: Coordinator service-core metrics (load + health) on TokenEntry.
#
# NOTE (merge team): this depends on 0006 and is a PARALLEL LEAF with
# 0007_hive127_token_lease (HIVE-127 PR #51, sibling HIVE-121 slice). When both
# land, run `manage.py makemigrations --merge` to add the trivial 0008 merge node
# — both only ADD columns/models, so the merge is conflict-free.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("claws", "0006_hive123_token_pools"),
    ]

    operations = [
        migrations.AddField(
            model_name="tokenentry",
            name="active_sessions",
            field=models.PositiveIntegerField(db_index=True, default=0),
        ),
        migrations.AddField(
            model_name="tokenentry",
            name="last_used_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="tokenentry",
            name="last_status",
            field=models.CharField(
                choices=[
                    ("unknown", "Unknown"),
                    ("ok", "OK"),
                    ("rate_limited", "Rate limited"),
                    ("auth_failed", "Auth failed (dead)"),
                ],
                default="unknown",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="tokenentry",
            name="last_checked_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
