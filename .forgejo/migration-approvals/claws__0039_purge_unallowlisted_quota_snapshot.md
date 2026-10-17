# PLAT-187 approval — claws/migrations/0039_purge_unallowlisted_quota_snapshot.py

**Flagged for:** `migrations.RunPython` (PLAT-4125 security data cleanup).
**Reviewed under:** PLAT-4125's approved positive-schema remediation and the
canonical PLAT-4107 quota-state V1 projection.

## What it does

Projects every historical `TokenEntry.quota_snapshot` through the same bounded
`project_quota_snapshot()` allowlist used at ingestion and serialization. Rows
already matching the V1 projection are not updated. Unknown keys, nested
provider data, raw response fragments, and unbounded diagnostics are removed.

## Why it is safe

- Data-only cleanup; no schema DDL, table lock, or model-field change.
- Iterates in batches of 500 and writes only rows whose projected value differs.
- The projection is deterministic and idempotent.
- The migration depends on the current single leaf,
  `claws.0038_plat4485_usage_rollup_rating_statements`.
- Removed fields violate the PLAT-4125 security contract and must not be
  reconstructed.

## Reversible plan

The Django reverse is intentionally a no-op. A rollback preserves the sanitized
V1 snapshot instead of restoring forbidden provider-controlled data. The old
application tolerates the retained allowlisted JSON fields, so application
rollback remains compatible without reversing the cleanup.

## Verification gates

- `makemigrations --check --dry-run` and the single-leaf migration graph gate.
- Focused quota projection/persistence/API regressions plus the full Hive suite.
- Migration forward execution in the CI compatibility gate.
