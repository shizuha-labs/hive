# PLAT-187 approval — fleet/migrations/0041_reassert_managed_grok_deepseek_fallback.py

**Flagged for:** `migrations.RunPython`.
**Reviewed by:** coordinator (framework-maintainer), 2026-08-01, repairing the
observed expand/roll compatibility race after migration `0040`.

## What it does

- Reasserts the exact Grok-to-DeepSeek V4 Flash managed chain for only `goro`,
  `hana`, and `nagi` when their primary lane is Grok Build on an admitted Grok
  model and their lifecycle is running or auto-resumable hibernated.
- Writes the same chain to Hive's durable `pending_config` outbox for daemon
  replay.
- Adds no schema and changes no credential, primary model, lifecycle, task,
  identity, workspace, or resource placement.

## Why it is safe

- Production evidence showed `0040` applied before the rolling deployment, then
  an old pod's former save chokepoint cleared the fields. The predecessor image
  for this follow-up already contains the new preserving save chokepoint, so the
  old-writer window is closed.
- The scope remains three exact usernames, two enabled lifecycle states, the
  exact execution method, and admitted Grok models.
- Hibernated is included because it is an enabled, auto-resumable state and must
  carry the fallback before its next demand-driven wake.

## Reversible plan

- Reverse is a no-op at the `0041` boundary because migration `0040` defines the
  same desired state. Rolling back below `0040` invokes `0040`'s bounded reverse,
  which queues an explicit empty chain for the daemon.
- Application rollback remains schema-compatible.

## Verification gates

- Regression tests prove running and hibernated named agents are repaired and
  receive matching pending replay.
- Migration graph, `makemigrations --check`, Django checks, and the full backend
  suite must pass before Origin push.
- Live verification must show exact Hive desired/daemon observed chains for all
  three agents and activity continuing through the DeepSeek fallback.
