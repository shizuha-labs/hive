# PLAT-187 approval — claws/migrations/0027_tokenentry_token_hint.py

**Flagged for:** `migrations.RunPython` (backfill).
**Reviewed by:** coordinator (Shizuha), 2026-07-10, under standing break-glass +
"deployments fault-free and automatic" operator directive. Migration authored for
the HIVE subscriptions token_hint feature (hive 91756bd/746c7b5).

## What it does
1. `AddField TokenEntry.token_hint` — new `CharField(blank=True, default='')`.
   Purely additive; old code ignores the column.
2. `RunPython backfill_hints` — one-time decrypt of each existing token to compute
   the masked hint (`sk-ant-o…XYZ789` style). Populates ONLY empty hints.

## Why it is safe
- No existing column or row data is mutated; only the new column is written.
- Per-row failures (undecryptable under current KEK) are swallowed — the row is
  left blank and repopulates on its next `set_token()`. The migration cannot fail
  on bad data.
- `bulk_update` batched at 200; TokenEntry cardinality is small (tens of rows).
- Old app code running against the new schema is unaffected (column has default).
  New code against old schema is never exercised in prod ordering (migrate runs
  before rollout), and the compat gate exercises both directions in staging.

## Reversible plan
- Django reverse: `migrate claws 0026` — the RunPython reverse is a no-op and the
  `AddField` reversal drops the column. No other object depends on `token_hint`
  at the 0026 boundary.
- Data loss on reverse is limited to the derived hints, which are recomputable
  (derived from stored ciphertexts) — nothing irrecoverable.

## Verification
- Prod DB confirmed at `claws 0026` before this approval (0027 not yet applied).
- Staging ring (R0) runs the old→new→old schema/health exercise on the isolated
  `shizuha_hive_staging` DB before any prod rollout.
