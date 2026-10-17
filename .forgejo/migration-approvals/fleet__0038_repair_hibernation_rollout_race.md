# PLAT-187 approval — fleet/migrations/0038_repair_hibernation_rollout_race.py

**Flagged for:** `migrations.RunPython`.
**Reviewed by:** coordinator (framework-maintainer), 2026-07-31.

## What it does

- Repairs the bounded set of lifecycle rows that migration `0037` marked as
  legacy auto-resumable hibernation, but an old daemon-consumer pod wrote back
  to `stopped` during the expand/roll compatibility window.
- Selects only rows with all three markers: `status='stopped'`, the exact
  `Legacy auto-resumable scale-to-zero state` reason, and a non-null
  `hibernated_at`; it changes only `status` back to `hibernated`.

## Why it is safe

- Explicit post-0037 operator stops clear both hibernation metadata fields and
  cannot match the repair predicate.
- Agent identity, configuration, workspace PVCs, tasks, credentials, runtime
  scaling, and Cortex lease ownership are untouched.
- The update restores the lifecycle intent already approved for those exact
  rows in `fleet__0037_agent_hibernation_state.md`.

## Reversible plan

- The reverse is intentionally a no-op: collapsing repaired hibernation intent
  into an explicit operator stop would be destructive.
- Application rollback remains schema-compatible because `0038` adds no schema
  and pre-hibernation code already treated the value as non-running.

## Verification gates

- A regression test proves the exact legacy marker is repaired while explicit
  stops and unrelated metadata are untouched.
- Migration graph, `makemigrations --check`, Django checks, and the full backend
  suite must pass before Origin push.
- Live verification must show no `stopped` row retaining hibernation metadata,
  while migrated rows appear as blue `hibernated`.
