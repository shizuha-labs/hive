# PLAT-187 approval — fleet/migrations/0049_ceo_office_full_agent_mcp.py

**Flagged for:** `migrations.RunPython` (CEO Office capability MCP + seat env/prompt data update).
**Reviewed by:** coordinator (framework-maintainer), 2026-08-16.

## What it does

- Unions `admin`, `id`, and `hive` onto the `ceo-office` CapabilityDefinition
  `mcp_server_refs` so talk seats share the platform MCP floor plus Hive.
- Writes the same MCP floor onto FleetAgent rows `hina`, `aya`, `yuna`, `ena`.
- Sets `SHIZUHA_TALK_MINIMAL_PROMPT=0` on those seats (empty `tools[]` was
  coupled to talk-minimal and made Ena narrate Pulse lookups).
- Replaces Ena/Yuna `context_prompt` so they call `pulse_get_user_tasks`
  instead of claiming they cannot look work up.

## Why it is safe

- Data-only; no schema DDL, credential, lease, or volume mutation.
- Scope is one capability slug plus four named CEO Office seats.
- Forward is idempotent: already-expanded refs are left unchanged.
- A backwards function drops `admin`/`id`/`hive` from the capability row only.

## Reversible plan

- `python manage.py migrate fleet 0048` trims `admin`/`id`/`hive` from the
  `ceo-office` capability. Seat env/prompt are not automatically reverted
  (they are additive). Restore via Hive SoT if a seat must go back to the
  2026-08-15 lean-talk contract.

## Verification gates

- Local: `fleet.test_ceo_office_lean_mcp`.
- Live after deploy: ceo-office grant includes pulse/connect/wiki/admin/id/hive;
  Ena/Yuna env has `SHIZUHA_TALK_MINIMAL_PROMPT=0`; a "what tasks are assigned
  to me?" turn calls Pulse instead of narrating a lookup.
