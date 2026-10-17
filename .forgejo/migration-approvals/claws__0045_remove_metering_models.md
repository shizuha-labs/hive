# PLAT-187 approval — claws/migrations/0045_remove_metering_models.py

**Flagged for:** `DeleteModel` / `RemoveField` state operations (ADR-0080 /
HIVE-2018 extraction).
**Reviewed under:** ADR-0080 (Retire Claw as a product; one agent model is
FleetAgent) and the HIVE-2010 shared-infra disposition list.

## What it does

Moves the metering models (AdmissionAuthoritySnapshot, UsageEvent,
UsageEventOutbox, UsageDailyRollup, UsageInvoiceDraft) out of the `claws` app
into `hive_platform` (hive_platform/0003). This migration removes them from the
`claws` app **STATE only** via `SeparateDatabaseAndState(database_operations=[])`,
including the state-only `RemoveField(usageevent, claw)` (the FK now references
`claws.Claw` from hive_platform).

## Why it is safe

- **No DDL, no table drop, no data loss.** The underlying `claws_*` tables are
  preserved byte-for-byte and owned by `hive_platform` via `Meta.db_table`.
- The models are re-created in hive_platform/0003 (state-only) with the same
  `db_table`, so Django's state is consistent and the tables are never touched.
- `UsageEvent.claw` FK now uses a string reference `'claws.Claw'`; the Metering
  Core models (BillingAccount etc.) remain in claws/metering_models.py.
- All callers updated (claws/metering_events, metering_statements,
  usage_metering, usage_metering_metrics, metering_views, urls, mgmt commands,
  and test files).

## Reversible plan

Reverse is a no-op at the DB layer (no database_operations). To roll back the
app-state move, re-run the reverse of hive_platform/0003 (state-only) and this
migration; the tables remain intact in either direction. Application rollback
is compatible because the tables and data are unchanged.

## Verification gates

- `makemigrations --check --dry-run` clean; single-leaf migration graph.
- 68 metering tests pass; full Hive suite (1911) only 4 pre-existing
  environment errors (Redis ×2, SQLite concurrency, FakeCursor) — confirmed
  failing on the base before this change.
