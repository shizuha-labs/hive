# PLAT-187 approval — fleet/migrations/0047_repair_pending_config_objects.py

**Flagged for:** `RunPython` repairing malformed JSON values and an
`AlterField` that records the application-level object validator.
**Risk tier:** safe corrective data migration; no destructive schema change.
**Review gate:** PR #151, authored by Sara with distinct Rui approval and a
distinct merger required before release.

## What it does

- Reads only `FleetAgent.id` and `FleetAgent.pending_config`.
- Replaces every value that is not a JSON object (dictionary in Python) with
  the valid empty outbox `{}`. This includes booleans, arrays, strings, numbers,
  and JSON null if any are present.
- Preserves every existing object byte-for-byte at the logical JSON-value
  boundary, including non-empty queued configuration updates.
- Records the new mapping-only field validator in migration state. It adds,
  drops, renames, or retypes no column and creates no index or database trigger.
- Changes no desired model, execution method, lifecycle state, identity,
  credential, lease, deployment, workspace, or task data.

## Why it is safe

- `pending_config` is the durable key/value outbox consumed with mapping
  operations (`items`, `keys`, and per-key fingerprints). A scalar has no valid
  meaning and cannot represent recoverable queued configuration.
- The incident snapshot found exactly four malformed rows — Banto, Kai, Nao,
  and Saki — each containing the JSON boolean `true`. On every rt-fleet
  heartbeat, the shared DaemonLink reader attempted `dict(True)`, raised
  `TypeError`, and disconnected the authoritative runtime projection.
- Empty `{}` is the existing default and means “no receiver-unconfirmed config
  keys.” It is therefore the only truthful repair for a scalar that contains no
  key/value payload.
- The strict model/write boundary and fail-loud defensive readers land in the
  same release. New ordinary writers cannot recreate a scalar, while a legacy
  or bypassed row cannot take down the shared socket during version skew.
- The update is bounded to the malformed primary keys discovered by a streamed
  two-column scan; it does not rewrite valid outbox objects.

## Reversible plan

- **Data preflight:** before forward migration, record the affected primary keys
  and JSON types from the canonical database, and assert that no affected value
  is an object. This count is the forward migration's exact repair set and the
  live verification baseline.
- **Forward:** repair only that invalid set to `{}`, then add the application
  validator state. Valid objects remain unchanged.
- **Schema reverse:** the `AlterField` reverses cleanly because it changes only
  Django's validator state; the PostgreSQL JSONB column is unchanged.
- **Data reverse is intentionally a no-op and explicitly lossy.** The original
  invalid scalar values are not restored. A boolean/array/string contains no
  recoverable pending key/value intent, and restoring it would recreate the
  fleet-wide disconnect. This is not claimed as a lossless rollback.
- Application rollback remains schema-compatible: older Hive images already
  accept `{}` and treat it as an empty pending outbox. The validator exists in
  application/migration state only; the PostgreSQL JSONB column is unchanged.
- If an affected agent still requires a configuration update, Hive remains the
  source of truth: submit the desired fields through the normal Hive config API
  so they are written as a fresh object and acknowledged by the daemon. Do not
  restore the malformed value.
- **Application rollback:** deploy the prior reviewed image through CI. It reads
  repaired `{}` values safely; do not reverse the data repair or reinsert the
  captured scalars. Verify the prior image is healthy and the affected rows
  remain `{}`. If an operator needs config on one of those agents, reissue it
  through Hive's normal desired-config API.

## Verification gates

- Migration regression seeds booleans, arrays, strings, integers, and floats,
  proves every non-object becomes `{}`, and proves a valid non-empty object is
  unchanged.
- Model regression rejects scalar saves; runtime writer regression proves a
  legacy scalar is replaced by a valid pending object; serializer regression
  proves a scalar is exposed as no pending keys while logging loudly.
- Production-order DaemonLink regression seeds `true`, receives heartbeat
  sequence 3 acknowledgement, lets all retry readers finish, then receives
  sequence 4 acknowledgement on the same socket.
- PR #151 also carries #149's transactional current-`ws_channel` ownership
  fence, with A-register → B-supersede → late-A retry proof that the stale
  connection cannot mutate the runtime-lane outbox.
- Migration graph, `makemigrations --check`, Django checks, standalone deploy
  guards, and the full backend suite must pass on the exact PR head.
- Live release proof must show all non-object `pending_config` rows repaired,
  stable connected rt-fleet registry/desired-runtime projection, no recurrence
  of `TypeError: 'bool' object is not iterable`, and consecutive heartbeat
  traffic without DaemonLink reconnect churn after the deployment cutoff.
