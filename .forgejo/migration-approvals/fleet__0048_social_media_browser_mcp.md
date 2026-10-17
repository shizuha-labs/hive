# PLAT-187 approval — fleet/migrations/0048_social_media_browser_mcp.py

**Flagged for:** `migrations.RunPython` (capability-definition data update).
**Reviewed under:** SM-14's recorded Browser MCP least-privilege contract and the
accepted Browser Human-Stealth HLD. This artifact documents the bounded grant;
it does not bypass the fail-closed migration compatibility gate.

## What it does

- Selects only `CapabilityDefinition` rows whose slug is exactly
  `social-media`.
- Appends the private per-agent `browser` MCP ref when it is absent, recomputes
  the canonical definition hash, and increments the capability version.
- Adds no schema and changes no agent lifecycle state, credential material,
  worker URL, pod, task ownership, or unrelated capability definition.
- Rows already containing `browser` are unchanged, so the forward operation is
  idempotent.

## Why it is safe

- Hive already provisions an isolated Browser worker and projects
  `SHIZUHA_BROWSER_MCP_URL` for social-media seats. This migration only aligns
  the effective allowlist with that existing private endpoint; it neither
  introduces a shared browser nor broadens any other team.
- The application default for `social-media` carries the same `browser` ref, so
  new/reconciled definitions and migrated definitions converge on one source of
  truth.
- Runtime validation explicitly recognizes `browser` as a private MCP ref. The
  generated grant remains bounded by the social-media capability and per-agent
  URL/JWT projection.
- The table is small and the update touches only the named capability payload,
  version/hash, and provenance fields.

## Reversible plan

- Preferred rollback is an application-image rollback while retaining the
  forward capability row. The prior image ignores the additional allowed ref,
  and the already-provisioned private worker remains isolated.
- The Django reverse is intentionally a no-op. A later reviewed capability edit
  may independently retain the same ref, so blindly removing it during schema
  rollback would silently revoke an adopted grant.
- A deliberate grant rollback, if required, must be a new bounded capability
  migration that removes `browser` only after verifying no later capability
  version adopted it; the normal Hive reconcile then withdraws it.

## Verification gates

- The focused capability tests must prove the default bundle and the effective
  grant include `browser`, while the existing stealth-skill/eager-skill checks
  remain green.
- Python compile/import checks, Django system checks, migration graph and
  `makemigrations --check`, the affected suite, and the full backend suite must
  pass in the repository CI recipe before push.
- After Origin CI deploys the reviewed head, a fresh Nori runtime must list
  callable `browser`, `mouse`, and `keyboard`; a non-sensitive human-mode input
  smoke must pass, and the same tools must remain present after an ordinary
  reconcile/restart boundary. The reconnect-failure gauge must return to zero.
