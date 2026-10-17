# PLAT-187 approval — fleet/migrations/0042_backfill_all_managed_grok_fallbacks.py

**Flagged for:** `migrations.RunPython`.
**Reviewed by:** coordinator (framework-maintainer), 2026-08-01, after live
verification found a managed Grok agent outside migrations `0040`/`0041`'s
frozen username allowlist without its DeepSeek V4 Flash fallback.

## What it does

- Selects every running or auto-resumable hibernated agent whose primary model
  is an admitted Grok 4.5 spelling and whose execution method is `grok_build`.
- Writes the exact Grok-to-DeepSeek V4 Flash managed chain to both durable
  `model_fallbacks` and Hive's `pending_config` replay outbox.
- Adds no schema and changes no credential, primary model, lifecycle, task,
  identity, workspace, or resource placement.

## Why it is safe

- Selection is by the managed execution lane and admitted primary model rather
  than username, so the repair covers Kumo and future Grok agents without
  changing non-Grok or non-managed agents.
- The current application save chokepoint owns the same managed fallback chain,
  making this a bounded repair of desired state rather than a second policy.
- Rows already carrying the exact chain in both fields are skipped.

## Reversible plan

- Reverse is intentionally a no-op because removing the live fallback would
  restore the confirmed Grok-auth outage for affected agents.
- A rollback below migration `0040` retains its bounded reverse path, while an
  application rollback remains schema-compatible with these JSON fields.
- Operational rollback is to change an agent's primary lane through Hive; the
  application save chokepoint then recalculates the managed chain.

## Verification gates

- Regression tests prove a running Kumo-shaped row and a future hibernated Grok
  row are repaired, with matching durable and pending chains.
- Migration graph, `makemigrations --check`, Django checks, standalone canary,
  selector and rollout-ring contracts, all 1,422 backend tests, and all 135
  frontend tests passed before Origin acceptance.
- Live verification must show Kumo's exact desired/observed chain, healthy
  DeepSeek fallback, and continued activity; all enabled Grok agents must remain
  healthy or have a separately proven root cause.

## Release recovery evidence

- Origin run `1643` proved staging and canary for source `6cf9085`, then failed
  closed at fleet while the preceding partially claimed fleet generation was
  still in recovery.
- The corrected executor completed that older recovery at fencing token `19`;
  canary source `6cf9085` completed independently at fencing token `31`.
- Origin run `1645` then exercised the exact-SHA fleet-dispatch guard and was
  rejected before submission because break-glass fleet dispatches cannot carry
  unreleased migrations. This normal push is therefore the policy-required
  staging-to-canary-to-fleet path for migration `0042`.
