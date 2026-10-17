# PLAT-187 approval — fleet/migrations/0062_deprecate_grok_46.py

**Flagged for:** `migrations.RunPython` (data remap of deprecated `FleetAgent.model`
pins).
**Reviewed by:** coordinator (framework-maintainer), 2026-09-29, implementing
operator direction that Grok 4.7 is the fleet pin and Grok 4.6 is deprecated.

## What it does

- Rewrites `FleetAgent.model` when the string contains `grok-4.6` or
  `grok-4.6-latest`, preserving the prefix:
  `cortex/grok-4.6` → `cortex/grok-4.7`, `xai:grok-4.6` → `xai:grok-4.7`,
  bare `grok-4.6` → `grok-4.7`, and the same for `cortex/xai/`.
- Leaves `grok-voice*` and `grok-imagine*` unchanged.
- Touches no schema: no column is added, dropped, retyped, renamed, re-indexed
  or constrained.
- Uses `QuerySet.update(model=...)` only when the string changes. It does not
  call `save()`, so hibernated and deleted rows stay hibernated or deleted.
- Does not enable an agent, call Kubernetes, or call Cortex.

## Why it is safe

- `remap_retired_fleet_model()` performs the same rewrite on `FleetAgent.save()`.
  A row nobody saves would otherwise keep posting `grok-4.6` after the catalog
  stops offering it as its own upstream.
- The dedicated rewrite runs before the generic retired-fleet map. That generic
  map sends unknown retired ids to `cortex/GLM-5.3-Flash`. Grok 4.6 must not
  take that path.
- `grok-4.5` is a different listing and is not matched.
- No lifecycle column is written. Customer seats that are hibernated until
  first use stay hibernated.

## Reversible plan

- `noop_reverse` is deliberate: Grok 4.6 is deprecated. Writing the pin back
  would point seats at an id the catalog no longer serves as its own upstream.
- Application rollback to a pre-0062 image is safe: the column type is
  unchanged. A row already rewritten to `grok-4.7` stays on `grok-4.7`, which
  the previous image does not admit. Roll back only together with the Cortex
  alias that still routes a `grok-4.6` offer id onto the `grok-4.7` upstream,
  or PATCH a seat to another live listing through Hive.
- Do not restore a `grok-4.6` pin to wake a seat.

## Who approved

Coordinator acting as framework-maintainer on the operator's 2026-09-29
directive to admit Grok 4.7 for Shizuha CLI and Grok Build and deprecate
Grok 4.6 everywhere.
