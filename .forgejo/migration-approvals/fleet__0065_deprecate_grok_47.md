# PLAT-187 approval — fleet/migrations/0065_deprecate_grok_47.py

**Flagged for:** `migrations.RunPython` (data remap of deprecated `FleetAgent.model`
pins, grok-4.7 → GLM-5.3-Flash).
**Reviewed by:** kai (engineering lead), 2026-11-11, on live router evidence
(`/v1/models` on cortex:8040 queried 2026-11-11: only `cortex/GLM-5.3-Flash`
and `cortex/Qwen3.8-27B` are served).

## What it does

- Rewrites `FleetAgent.model` when the string contains `grok-4.7` or
  `grok-4.7-latest`, on the 0062 contract:
  `cortex/grok-4.7` → `cortex/GLM-5.3-Flash` (prefix-preserving, in-place
  substitution of the matched substring), bare `grok-4.7` →
  `GLM-5.3-Flash` (deploy-time prefix added by the router contract).
- **xai-cohort exception — wholesale rewrite:** `xai:grok-4.7` and
  `cortex/xai/grok-4.7` are rewritten wholesale to `cortex/GLM-5.3-Flash`,
  NOT prefix-preserving. Rationale: the xai lane is no longer a catalog
  listing (`/v1/models` serves no `xai:*` and no `cortex/xai/*`), and per
  `fleet/runtime_lane.py` an explicit provider prefix routes to that
  provider — an unconfigured provider fails at startup (HIVE-1937
  crash-loop datum). A prefix-preserving rewrite would recreate the exact
  wedge this migration fixes.
- Leaves `grok-voice*` and `grok-imagine*` unchanged.
- Touches no schema: no column is added, dropped, retyped, renamed, re-indexed
  or constrained.
- Uses `QuerySet.update(model=...)` only when the string changes. It does not
  call `save()`, so hibernated and deleted rows stay hibernated or deleted.
- Does not enable an agent, call Kubernetes, or call Cortex.

## Why it is safe

- Every cohort lands on `cortex/GLM-5.3-Flash`, which the live router
  actually serves; no rewritten pin points at a dead provider or a dead
  listing.
- `remap_retired_fleet_model()` performs the same class of rewrite on
  `FleetAgent.save()`; the migration closes the gap for rows nobody saves.
- Amendment-in-place of 0065 is safe: the PLAT-187 gate fail-fasted every
  deploy at the earliest stage, so **no environment ever applied 0065** —
  there are no already-migrated rows carrying the unsafe prefix-preserving
  xai outcome.
- `grok-4.6` and `grok-4.5` are different listings and are not matched.
- No lifecycle column is written. Customer seats that are hibernated until
  first use stay hibernated.

## Reversible plan

- `noop_reverse` is deliberate: grok-4.7 is deprecated. Writing the pin back
  would point seats at an id the catalog no longer serves.
- Application rollback to a pre-0065 image is safe: the column type is
  unchanged, and rows already rewritten to `cortex/GLM-5.3-Flash` remain on a
  served id — `GLM-5.3-Flash` stays a served alias across images, so a
  rollback does not strand a seat on a dead listing.
- Do not restore a `grok-4.7` pin to wake a seat.

## Who approved

kai, engineering lead, 2026-11-11, reviewing the amended migration on live
router evidence and the HIVE-1937 crash-loop datum; correction of the
original prefix-preserving xai behavior documented above. Amendment
authored by ryo (merge seat) from the reviewed spec after the PLAT-187 gate
blocked the deploy lane on the missing approval doc.
