# PLAT-187 approval — fleet/migrations/0059_remap_retired_metal_to_glm.py

**Flagged for:** `migrations.RunPython` (data remap of retired `FleetAgent.model`
pins).
**Reviewed by:** coordinator (framework-maintainer), 2026-09-19, implementing
operator direction that Vision-Metal is deprecated and every fleet agent is
pinned to GLM-5.3-Flash.

## What it does

- Rewrites `FleetAgent.model` from retired serving ids
  (`DeepSeek-V4-Flash-Vision-Metal`, `Qwen3.8-27B-Q2`, plus `cortex/`/`vllm/`
  aliases) to `cortex/GLM-5.3-Flash`.
- Touches no schema: no column is added, dropped, retyped, renamed, re-indexed
  or constrained.
- Iterates rows and writes `update_fields=['model']` only when the remapped
  value differs. Live fleet already has zero Metal/Qwen pins; the migration is
  a no-op on current production rows and a backstop for any leftover pin.
- Does **not** call k8s or Cortex. Occupancy drop (yield-homes + PVC session
  reset) is the PATCH / lease-tick path in the same change, not this migration.

## Why it is safe

- `remap_retired_fleet_model()` already rewrites these ids at `FleetAgent.save()`.
  Rows nobody writes would keep a Metal pin forever and SCLI would keep POSTing
  the deprecated ds4 slot (Nova `slot_busy` every 120s after Hive SoT said GLM).
- The target `cortex/GLM-5.3-Flash` is the live fleet pin and a registered
  runtime-lane family (`cortex-managed`).
- No primary is left empty. No KV home is deleted by the migration itself.
- Live census 2026-09-19: 0 `FleetAgent` rows on Vision-Metal or Qwen3.8-Q2;
  0 fleet Deployments with those `MODEL` env values.

## Reversible plan

- `noop_reverse` is deliberate: Metal is deprecated. Restoring a Metal pin is a
  policy decision (remove it from `RETIRED_FLEET_MODELS` and PATCH the seat),
  not a schema rollback.
- Application rollback to a pre-0059 image is safe: the column is unchanged.
  A row already rewritten to GLM stays on GLM, which pre-0059 Hive already
  served.
- If a specific seat must return to another live listing, PATCH it through Hive
  (SoT). Do not resurrect Metal.

## Who approved

Coordinator acting as framework-maintainer on the operator's 2026-09-19
directive to convert all agents off Metal onto GLM-5.3-Flash and make Hive
propagate that conversion.
