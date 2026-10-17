# PLAT-187 approval — fleet/migrations/0034_review_capability_generic_code_reviewer.py

**Flagged for:** `migrations.RunPython` (capability skill_refs data update).
**Reviewed by:** coordinator (framework-maintainer), 2026-07-28.

## What it does
For `CapabilityDefinition` rows with `slug='review'` only:
- Sets `skill_refs` to `['code-reviewer', 'pulse-review-merge']`
  (retires person-specific revi/reika and legacy github from the Review
  baseline).
- Bumps `version` / `definition_hash` when the payload changes.
- Does **not** touch MCP server refs, credentials, or agent pods directly.

## Why it is safe
- Data-only update of a small config table (capability definitions).
- Forwards and backwards both provided; reverse restores the previous skill
  list.
- MCP floor (including admin) is unchanged per migration docstring.
- No schema DDL.

## Reversible plan
- `python manage.py migrate fleet 0033` runs `backwards()` and restores
  `['pulse-review-merge', 'github', 'revi-reviewer', 'reika-reviewer']`.
- Application image rollback without reverse-migrating leaves skill_refs at
  the new list (desired SoD: generic code-reviewer).

## Verification gates
- R0 both-ways compat on staging.
- After fleet migrate: Review capability skill_refs match the new list;
  Review agents still have pulse + wiki MCP.
