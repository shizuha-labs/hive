# HIVE-127: lease binding for Coordinator report-status anti-poisoning.

from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ("claws", "0006_hive123_token_pools"),
    ]

    operations = [
        migrations.CreateModel(
            name="TokenLease",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("broker_identity", models.CharField(db_index=True, max_length=200)),
                ("owner_kind", models.CharField(db_index=True, max_length=16)),
                ("owner_id", models.CharField(db_index=True, max_length=128)),
                ("provider", models.CharField(max_length=32)),
                ("issued_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField(db_index=True)),
                (
                    "entry",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="leases",
                        to="claws.tokenentry",
                    ),
                ),
            ],
            options={
                "ordering": ["-issued_at"],
            },
        ),
        migrations.AddIndex(
            model_name="tokenlease",
            index=models.Index(
                fields=["entry", "broker_identity"],
                name="claws_token_entry_i_607713_idx",
            ),
        ),
    ]
