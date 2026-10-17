# PLAT-187 approval — claws/migrations/0031_alter_tokenpool_provider.py

**Flagged for:** `migrations.AlterField` (provider choices list).
**Reviewed by:** coordinator (framework-maintainer), 2026-07-28, unblocking
Hive ring R0 after the live fleet lagged ~33 commits behind Origin HEAD.

## What it does
Extends `TokenPool.provider` CharField **choices** to include
`("google", "Google Antigravity")` alongside anthropic/openai/xai.
- `max_length` stays 32; `db_index` stays True.
- No column type change, no data rewrite, no drop.

## Why it is safe
- Django `choices` are validation metadata; the underlying VARCHAR column is
  unchanged. Existing rows keep their provider values.
- New value is additive only (Google Antigravity token pools already exist in
  product; this makes the ORM choice set match runtime).
- Older app images ignore unknown choice labels; they still read/write the
  string column.

## Reversible plan
- Prefer image rollback without reverse-migrating: old code still reads the
  column; any `google` rows remain valid strings.
- Hard reverse: `python manage.py migrate claws 0030` rewrites only the
  choices metadata in Django’s migration state — no row DELETE/UPDATE
  of pool secrets.

## Verification gates
- R0 migration-compat (old/new both directions) on staging Postgres.
- Fleet migrate job applies `claws 0031` before or with the app image roll.
- Live: Token Pools page lists Google Antigravity provider; existing non-google
  pools unchanged.
