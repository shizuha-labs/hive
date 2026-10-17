# PLAT-187 approval — fleet/migrations/0037_agent_hibernation_state.py

**Flagged for:** `migrations.RunPython` and the status-choice `AlterField`.
**Reviewed by:** coordinator (framework-maintainer), 2026-07-29.

## What it does

- Adds nullable/blank hibernation explanation fields to `FleetAgent`.
- Adds the application-level `hibernated` status choice; the existing
  `varchar(20)` database representation and index are unchanged.
- Converts legacy `stopped` rows to `hibernated` because the old controller
  treated every stopped row as automatically wakeable. New explicit operator
  stops written after the migration remain `stopped` and are never auto-woken.

## Why it is safe

- Both schema additions are nullable or have an empty-string default at the
  application boundary, so the previous image can read/write the table during
  rollout.
- The bounded data update changes only `FleetAgent.status='stopped'`. It
  preserves the exact old auto-resume behavior instead of introducing new
  wake authority.
- Agent identity, configuration, workspace PVCs, tasks, credentials, and
  Cortex leases are untouched. Runtime scaling remains owned by the normal
  Hive lifecycle reconciler after migration.

## Reversible plan

- Application rollback is safe while the additive columns remain; the old
  application treats `hibernated` as a non-running value.
- `python manage.py migrate fleet 0036` maps all `hibernated` rows back to the
  legacy auto-wakeable `stopped` representation, clears the two added metadata
  fields, and then drops those fields.
- Do not reverse if post-migration operator-stop intent must remain
  distinguishable without first exporting the affected lifecycle rows.

## Verification gates

- Migration graph and `makemigrations --check` are clean.
- Forward and reverse migration compatibility runs against isolated Postgres.
- Backend tests prove automatic hibernation remains wakeable, explicit stop is
  not wakeable by task/DM/lease demand, and rt-fleet observations preserve Hive
  hibernation intent.
- Live Agents API/UI show hibernated agents blue with the reason and lease
  state, while explicit stops remain distinct.
