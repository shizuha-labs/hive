# PLAT-187 approval — fleet/migrations/0027_fleetagent_personal_owner_identity.py

**Flagged for:** `migrations.AlterField` (on `FleetAgent.owner_id`). The INV-D gate
flags every `AlterField` as potentially-semantic; this one is additive-compatible
(adds an index + confirms nullability — no type/data change). Also carries two
additive `AddField`s and one partial `AddConstraint`.
**Reviewed by:** coordinator (Shizuha), 2026-07-11, under standing break-glass +
the operator's "fix the deploy ring properly and make it autonomous" directive.
Authored by the daemon-plane consolidation work (FleetAgent owner identity); its
approval doc was missing, which fail-closed-wedged the entire hive backend deploy
ring for every commit after the last-successful anchor. This doc completes the
required artifact.

## What it does (dependency: fleet 0026_fleetagent_owner_id)
1. `AddField owner_username` — `CharField(blank=True, default='')`. Purely additive;
   non-authoritative display handle (owner_id remains the stable key). Old code
   ignores the column.
2. `AddField identity_provisioned_at` — `DateTimeField(null=True, blank=True)`.
   Purely additive; nullable.
3. `AlterField owner_id` — `BigIntegerField(null=True, blank=True, default=None,
   db_index=True)`. `owner_id` was ALREADY nullable at 0026; this only adds
   `db_index=True` (+ help text). No column type change, no rewrite of existing
   rows, no NOT-NULL tightening. Creating the index on a ~55-row table is trivial.
4. `AddConstraint fleet_one_personal_agent_per_owner` — partial
   `UniqueConstraint(owner_id) WHERE owner_id IS NOT NULL`. Enforces "≤1 personal
   agent per owner"; org/team agents have `owner_id IS NULL` and are exempt.

## Why it is safe
- Additive only: no `RemoveField`/`DeleteModel`/`RenameField`/`RunSQL`/`RunPython`;
  no existing column dropped or retyped, no row data mutated.
- The partial unique constraint was **verified against live prod data** before this
  approval: `FleetAgent.owner_id` distribution is `{NULL: 54, 329: 1}` — **zero**
  owners hold more than one non-NULL owner_id, so `CREATE UNIQUE INDEX … WHERE
  owner_id IS NOT NULL` applies without violation. (The constraint can only reject
  a *future* attempt to give one owner two personal agents — the intended invariant.)
- Old app code against the new schema is unaffected (new columns have defaults /
  are nullable; the constraint only constrains writes old code doesn't make).
  New code against the old schema is exercised by the INV-D both-ways compat run
  and confirmed healthy (the new image boots and serves `/api/health/` against a
  0026 schema — validated live, since prod currently runs this image at 0026).

## Reversible plan
- Django reverse: `migrate fleet 0026` — drops the constraint, the index, and the
  two added columns in one step. Nothing outside FleetAgent references
  `owner_username` / `identity_provisioned_at` / the constraint at the 0026
  boundary, so the reversal is clean. Only loss is the (derivable) display handle
  and the provisioned-at timestamp — nothing authoritative (owner_id is the key).

## Verification
- Prod hive DB confirmed at `fleet 0026` (0027 not yet applied) at approval time;
  owner_id distribution `{NULL:54, 329:1}` checked for constraint conflict → none.
- Migrate applied cleanly against the isolated `shizuha_hive_staging` DB.
- The R0 staging ring runs the old→new→old schema/health exercise before any prod
  rollout; deploy-fleet's `ci-migrate-hive-*` Job applies it to prod ahead of the
  image swap.
