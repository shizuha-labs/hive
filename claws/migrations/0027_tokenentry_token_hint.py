"""Add TokenEntry.token_hint (masked first+last-chars credential identifier) and
backfill existing rows by decrypting each token once. The KEK is available in the
running pod's environment at migrate time, so existing OATs/JWTs get a hint without
waiting for a re-set. A row whose token can't be decrypted (rotated KEK) is left
blank and repopulates on its next set_token()."""
from django.db import migrations, models


def backfill_hints(apps, schema_editor):
    try:
        TokenEntry = apps.get_model('claws', 'TokenEntry')
    except LookupError:
        # ADR-0080 / HIVE-2018: TokenEntry moved to hive_platform. During fresh
        # replay it is still in claws at this point in the graph; when invoked
        # with the current state (after claws/0040) use the hive_platform home.
        TokenEntry = apps.get_model('hive_platform', 'TokenEntry')
    # Import the live crypto helpers (not the historical model's methods, which the
    # frozen apps registry doesn't carry).
    from claws.token_coordinator import decrypt_token, mask_token_hint

    to_update = []
    for entry in TokenEntry.objects.all().only(
        'id', 'token_ciphertext', 'wrapped_dek', 'token_hint',
    ):
        if entry.token_hint:
            continue
        try:
            raw = decrypt_token(entry.token_ciphertext, entry.wrapped_dek)
        except Exception:
            # Undecryptable under the current KEK — leave blank; a future set_token
            # repopulates it. Never fail the migration on one bad row.
            continue
        hint = mask_token_hint(raw)
        if hint:
            entry.token_hint = hint
            to_update.append(entry)
    if to_update:
        TokenEntry.objects.bulk_update(to_update, ['token_hint'], batch_size=200)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('claws', '0026_claw_idle_recycle_after'),
    ]

    operations = [
        migrations.AddField(
            model_name='tokenentry',
            name='token_hint',
            field=models.CharField(blank=True, default='', max_length=40),
        ),
        migrations.RunPython(backfill_hints, noop_reverse),
    ]
