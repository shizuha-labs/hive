# PLAT-187 approval — fleet/migrations/0064_seed_critical_team_policy.py

**Flagged for:** `migrations.RunPython` (seeds default
`LeaseCriticalTeamPolicy` rows: merge/review/devops `min_leases=1`).
**Reviewed by:** aoi (architect), 2026-10-08, as reviewer of HIVE-2862
(shizuha-labs/hive#485). Implements the HIVE-2862 operator directive: each
critical team's lease reservation is an admin-editable Hive setting with a
DEPLOYED default, so a fresh fleet reserves merge/review/devops from the
first tick.

## What it does

- Creates up to three rows in `fleet_leasecriticalteampolicy`:
  `merge=1`, `review=1`, `devops=1`, all `enabled=True`, each carrying the
  marker note `default floor (HIVE-2862): fair reservation for every
  critical team`.
- Idempotent: skips any team_slug that already has an enabled policy row,
  so operator-created floors are never overwritten or duplicated.
- Touches no schema: no column is added, dropped, retyped, renamed,
  re-indexed or constrained. The table itself is created by 0063
  (`fleet_leasecriticalteampolicy`), already on main.
- Uses `objects.create()` only. It does not update, enable/disable, or
  delete any pre-existing row, does not call Kubernetes or Cortex, and
  does not touch `FleetAgent` lifecycle state.

## Why it is safe

- The floor values are the policy the operator already directed
  (HIVE-2862): one reserved lease per critical team (merge, review,
  devops). Without the seed, a fresh fleet has an empty policy table and
  reserves nothing — the exact starvation shape observed on 2026-10-08
  (devops zero holders while urgent work waited).
- Rows are data, not code: floors stay adjustable in the Hive admin with
  no redeploy, which is the point of the migration.
- The scheduler reads the policy per tick; adding rows changes only
  floor-reservation arithmetic, never lease ownership of an existing seat.

## Reversible plan

- `backwards()` deletes exactly the rows this migration created, matched
  by `team_slug` in `{merge, review, devops}` AND the distinctive
  `default floor (HIVE-2862)` note marker — so a floor an operator later
  edited (different note) or created independently is preserved. The
  forward pass is row-for-row symmetric with the backward pass.
- Application rollback to a pre-0064 image is safe: the table (0063)
  remains, the table shape is unchanged, and an un-migrated database
  simply has no default floor rows — the scheduler treats a missing
  policy as floor 0, the pre-HIVE-2862 behavior. No schema or code
  coupling breaks on either direction.
- Roll back together with reverting hive#485 if the yield-ordering change
  itself is ever found defective; the seed rows alone are harmless.
