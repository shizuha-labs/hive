# PLAT-187 approval — fleet/migrations/0044_pin_agents_drop_all_fallback_chains.py

**Flagged for:** `RunPython` performing a bulk data update on
`fleet_fleetagent.model_fallbacks` and `fleet_fleetagent.failover_chain_id`.
**Reviewed by:** coordinator (framework-maintainer), 2026-08-05, implementing the
operator directive of 2026-08-04:

> perhaps there should be zero concept of a 'fallback' .. the agent needs to be
> pinned to a backend and that's it .. to make the life simpler

## What it does

- Sets `model_fallbacks = []` and `failover_chain_id = ''` on every
  `FleetAgent` row that still carries a chain.
- Touches no schema: no column is added, dropped, retyped, renamed, re-indexed
  or constrained. Column nullability and defaults are unchanged.
- Uses `.update()` deliberately, bypassing `save()`, so a migration does not
  fire per-row runtime-lane re-derivation or config-push side effects for every
  agent in the fleet.

## Why it is safe

- `runtime_lane.managed_model_fallbacks()` already returns `[]` for every lane
  as of the same change, so these rows are stale data that no longer matches
  what Hive would compute. Leaving them would keep a pod's inline failover loop
  alive with a second step that Hive no longer sanctions — the drift the
  directive removes.
- `FleetAgent.save()` recomputes `model_fallbacks` at the model chokepoint, but
  rows are only rewritten when something touches them. A pinned-and-idle agent
  could otherwise carry its chain indefinitely; this converges them in one pass.
- Clearing a fallback cannot strand an agent: the primary model is untouched,
  and an agent whose primary is unavailable now surfaces as unavailable instead
  of silently serving from a second backend. That visibility is the point of the
  directive, and the co-landed Cortex pool-dry work makes an unroutable model
  disappear from `/v1/models` so Pulse routes to a healthy peer.
- Drift detection was made chain-aware in the same change
  (`external_agents.telemetry_fields`), so an agent running a model that Hive
  itself issued is not reported as `runtime_config_drift`. With chains now empty
  that union reduces to the strict single-model check — the original contract.

## Reversible plan

- `noop_reverse` is deliberate and documented in the migration: reversing does
  not restore chains.
- Restoring failover is a POLICY decision, not a schema one. It comes back by
  changing `managed_model_fallbacks()` to return a chain again and letting the
  normal config push rewrite the rows — never by resurrecting these values from
  a backup, which would reintroduce exactly the unsanctioned per-row drift this
  migration removes.
- Because no schema changed, an application rollback to a pre-0044 image runs
  correctly against this data: empty `model_fallbacks` is a valid, previously
  supported value (it is what every non-managed lane already had), so the older
  code simply performs no inline failover.

## Verification gates

- `fleet/test_drift_respects_hive_chain.py` proves the drift rule stays correct
  for any chain that is ever configured — the fixture keeps a chain on purpose
  so the check cannot silently depend on there being none — while an
  unsanctioned model is still reported as real drift.
- Migration graph check, `makemigrations --check`, Django system checks, and the
  full Hive backend suite (1,496 tests) plus the frontend suite (146 tests)
  passed on the local CI-mirror gate before release.
- Live release verification: nagi (Grok Build, `cortex/grok-4.5` pinned) must
  show no `runtime_config_drift` banner and must not fall to
  `cortex/DeepSeek-V4-Flash`; the Agents page must show each agent on exactly
  the model Hive records as its desired state.

## Note on this doc arriving late

Migration 0044 landed in `5d2c34d` without its approval doc, which fail-closed
the PLAT-187 gate and wedged EVERY Hive deploy behind it — three commits'
worth, including an agent-credential fix — until this doc was added. The gate
worked exactly as designed (HIVE-665: fail loud and attributed rather than
silently wedge the ring); the omission was the author's. Add the approval doc in
the SAME commit as any flagged migration.
