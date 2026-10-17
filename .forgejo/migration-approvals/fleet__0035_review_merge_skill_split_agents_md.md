# PLAT-187 approval — fleet/migrations/0035_review_merge_skill_split_agents_md.py

**Flagged for:** `migrations.RunPython` (capability skill_refs data update).
**Reviewed by:** coordinator (framework-maintainer), 2026-07-28.

## What it does
- `review` skill_refs → `['code-reviewer', 'pulse-review']`
- `merge` skill_refs → `['pulse-merge']`
- `engineering` skill_refs gains `agents-md-shipping` if missing
- Updates definition_hash / version when changed.

## Why it is safe
- Data-only; implements the Review vs Merge skill-pack split (SoD).
- Forwards/backwards both implemented.
- No schema DDL; no agent secret or PVC mutation.
- Engineering append is additive.

## Reversible plan
- `python manage.py migrate fleet 0034` restores prior skill lists via
  `backwards()`.
- Prefer keeping the split after fleet promote (product intent).

## Verification gates
- R0 both-ways compat on staging.
- Live: Review agents load pulse-review + code-reviewer; Merge agents load
  pulse-merge; engineering baseline includes agents-md-shipping.
