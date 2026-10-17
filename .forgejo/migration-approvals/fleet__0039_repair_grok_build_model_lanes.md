# PLAT-187 approval — fleet/migrations/0039_repair_grok_build_model_lanes.py

**Flagged for:** `migrations.RunPython`.
**Reviewed by:** coordinator (framework-maintainer), 2026-08-01.

## What it does

- Selects only `FleetAgent` rows whose authoritative execution method is
  `grok_build` while their primary model is outside the pinned Grok model set.
- Replaces that impossible pairing with the canonical
  `cortex/grok-4.5` model. If the same stale model is present in a queued
  `pending_config`, the migration repairs that field as well.
- Adds no schema and does not change lifecycle status, replicas, tasks,
  credentials, subscription policy, identity, or workspace state.

## Why it is safe

- The Grok Build runtime already accepts only `grok-4.5`,
  `cortex/grok-4.5`, or `cortex/xai/grok-4.5`. A non-Grok model is rejected by
  the runtime and silently falls back to a different harness, so the repaired
  rows cannot execute their declared lane before this migration.
- The predicate is bounded by the explicit `grok_build` operator intent. It
  leaves every Shizuha, Codex, Claude, Antigravity, and OpenClaw lane untouched.
- Production evidence before approval found exactly two matching rows, Hana and
  Nagi. Both were hibernated and both had empty `pending_config` documents.

## Reversible plan

- The reverse is intentionally a no-op because restoring an invalid
  method/model pair would recreate the defect.
- Application rollback remains schema-compatible. Older images already support
  the canonical Grok model and ignore the new model-save guard in code.
- If an operator intended one of the affected agents to use DeepSeek instead,
  that intent must be represented explicitly through Hive by changing the
  execution method to `shizuha`, not by restoring the impossible pair.

## Verification gates

- Regression tests must prove invalid Grok pairings are rejected at the model
  write chokepoint, valid Grok pairings persist, and unrelated status updates on
  legacy rows are not blocked.
- Migration graph, `makemigrations --check`, Django checks, and the full backend
  suite must pass before Origin push.
- Live verification must show every `grok_build` agent on a Grok model and no
  runtime fallback warning when a demand-driven Grok agent next wakes.
