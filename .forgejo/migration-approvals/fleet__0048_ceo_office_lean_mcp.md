# PLAT-187 approval — fleet/migrations/0048_ceo_office_lean_mcp.py

**Flagged for:** `migrations.RunPython` (CEO Office capability MCP + seat env data update).
**Reviewed by:** coordinator (framework-maintainer), 2026-08-15.

## What it does

- Trims the `ceo-office` CapabilityDefinition `mcp_server_refs` to the lean
  conversational set (`pulse`, `connect`, `wiki`) and drops historical
  PLAT-1251 `admin` / `id` / `scs` floor extras. Operator-added extras that
  are not those three slugs are preserved.
- Clears `eager_skill_refs` on that capability so CEO Office skills stay
  reference-only.
- Writes `AGENT_TEAM=ceo-office`, `SHIZUHA_LEAN_MCP=1`, and 30-minute
  idle-heartbeat env onto FleetAgent rows `hina` and `aya`, and pins
  `reasoning_effort=low` when it is blank or `high`/`xhigh`.

## Why it is safe

- Data-only; no schema DDL, credential, lease, or volume mutation.
- Scope is one capability slug plus two named seats.
- Forward is idempotent: already-lean refs are left unchanged.
- A backwards function restores `admin`/`id` on the capability row only.

## Reversible plan

- `python manage.py migrate fleet 0047` re-adds `admin`/`id` to the
  `ceo-office` capability. Seat env keys are not automatically removed
  (they are additive and harmless if left). Delete them via Hive SoT if
  a seat must leave the lean conversational profile.

## Verification gates

- Local: `fleet.test_ceo_office_lean_mcp` plus the PLAT-1251 floor suite.
- Live after deploy: ceo-office grant is pulse/connect/wiki; Hina/Aya env
  includes `SHIZUHA_IDLE_HEARTBEAT_MS=1800000` and `SHIZUHA_LEAN_MCP=1`;
  `reasoning_effort=low`.
