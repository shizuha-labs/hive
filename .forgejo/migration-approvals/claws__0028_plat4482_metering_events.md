# PLAT-187 approval — claws/migrations/0028_plat4482_metering_events.py

**Flagged for:** `migrations.AlterField` and `migrations.RunPython` (legacy
`UsageEvent` backfill).
**Reviewed under:** PLAT-4482's accepted ADR-0032 + unified Metering HLD and
PLAT-4562's recorded migration invariant/safety review, 2026-07-13. This artifact
records the already-reviewed migration contract; it does not bypass or relax the
fail-closed compatibility gate.

## What it does
1. Widens `UsageEvent.source` from its prior bound to `max_length=512`; no values
   are truncated or rewritten.
2. Adds nullable/defaulted CloudEvents, billing-authority, reservation,
   correction, and generation fields to `UsageEvent`.
3. Creates `AdmissionAuthoritySnapshot` and `UsageEventOutbox` tables.
4. Backfills each legacy `UsageEvent` deterministically: derives stable event and
   proof IDs, writes a canonical CloudEvents envelope/hash, and creates the
   authority snapshot with `get_or_create`.
5. Adds the `(source, event_id)` partial uniqueness constraint and billing /
   correction lookup indexes after the backfill.

## Safety proof
- Production preflight on 2026-07-13 confirmed the database remains at `claws
  0027` and `UsageEvent` has **0 rows**. The `RunPython` body is therefore a no-op
  for this rollout; there is no legacy data to rewrite or conflict with the new
  uniqueness constraint.
- All schema additions are nullable or have safe defaults. Old code ignores the
  new columns/tables; widening `source` is backward-compatible.
- For a non-empty future/staging database, backfill IDs and hashes are
  deterministic, iteration is bounded in chunks of 500, snapshots use
  `get_or_create`, and the event save writes only the named new fields.
- The checked-in R0 migration gate must exercise old/new image-schema
  compatibility before any canary or fleet mutation. The gate remains fail
  closed; this document only satisfies its required reviewed-artifact input.

## Rollback and data preservation
- **Preferred rollback:** restore the prior application image while retaining the
  forward 0028 schema. The additions/widening are backward-compatible and this
  avoids discarding events or outbox rows created after migration.
- **Hard schema reversal:** first quiesce metering writes, verify no new
  `UsageEvent`, `UsageEventOutbox`, or `AdmissionAuthoritySnapshot` rows exist,
  and take a database dump. Then `migrate claws 0027` may drop the added
  tables/fields/indexes/constraint. `RunPython` has a no-op reverse because the
  derived legacy fields disappear with the reversed schema.
- If any post-migration billable events/outbox rows exist, do **not** reverse the
  schema. Preserve the forward schema or export/replay those rows before any
  destructive reversal.

## Verification gates
- Plan-only probe must recognize this exact path and advance from the missing-doc
  failure to exit 3 (`additive compatibility exercise required`):
  `MIGRATION_COMPAT_PLAN_ONLY=1 bash .forgejo/scripts/migration-compat-gate.sh
  1404c66d91e992c5bd5c4b4bf84bd4aa8e89f7ad dummy:new dummy:old`.
- Origin must then pass backend tests, the real R0 old/new compatibility exercise,
  canary, and fleet at the reviewed head.
- After rollout verify `claws 0028`, the live `shizuha-hive/hive` image and
  readiness, and that the Origin failure streak clears.
