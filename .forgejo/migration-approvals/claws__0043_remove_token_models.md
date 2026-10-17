# PLAT-187 approval — claws/migrations/0043_remove_token_models.py

**Flagged for:** `DeleteModel` state operations (ADR-0080 / HIVE-2018 extraction).
**Reviewed under:** ADR-0080 (Retire Claw as a product; one agent model is
FleetAgent) and the HIVE-2010 shared-infra disposition list.

## What it does

Moves the token-coordinator models (TokenPool, TokenEntry,
TokenEntryLeasingEvent, TokenStickyAssignment, TokenLease) out of the `claws`
app into `hive_platform` (hive_platform/0001). This migration removes them from
the `claws` app **STATE only** via `SeparateDatabaseAndState(database_operations=[])`.

## Why it is safe

- **No DDL, no table drop, no data loss.** The underlying `claws_*` tables are
  preserved byte-for-byte and owned by `hive_platform` via `Meta.db_table`.
- The models are re-created in hive_platform/0001 (state-only) with the same
  `db_table`, so Django's state is consistent and the tables are never touched.
- Historical data migrations (0027/0030/0034/0035/0039) keep a backward-
  compatible fallback (try `claws.TokenEntry` for fresh replay, fall back to
  `hive_platform.TokenEntry` when invoked with the current state).
- All callers updated (fleet/runtime_lane, fleet/grok_provider_metrics,
  claws/views, serializers, OAuth modules, mgmt commands, 14 test files).

## Reversible plan

Reverse is a no-op at the DB layer (no database_operations). To roll back the
app-state move, re-run the reverse of hive_platform/0001 (state-only) and this
migration; the tables remain intact in either direction. Application rollback
is compatible because the tables and data are unchanged.

## Verification gates

- `makemigrations --check --dry-run` clean; single-leaf migration graph.
- 99 token-coordinator + credential + fleet tests pass.
- Full Hive suite: only 4 pre-existing environment errors (Redis ×2, SQLite
  concurrency, FakeCursor) — confirmed failing on the base before this change.
