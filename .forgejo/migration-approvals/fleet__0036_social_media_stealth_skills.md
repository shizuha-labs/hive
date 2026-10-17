# PLAT-187 approval — fleet/migrations/0036_social_media_stealth_skills.py

**Flagged for:** `migrations.RunPython` (capability skill_refs data update).
**Reviewed by:** coordinator (framework-maintainer), 2026-07-29.

## What it does

- Ensures the `social-media` and `marketing` capability definitions include
  `human-browser-stealth`, `x-account-safety`, `x-twitter`, and
  `connect-messaging` in their intended skill/eager-skill sets.
- Recomputes each affected definition hash and increments its version only when
  the effective capability payload changes.
- Updates at most the rows whose slug is `social-media` or `marketing`.

## Why it is safe

- Data-only; it contains no schema DDL, credential, lease, agent lifecycle, or
  persistent-volume mutation.
- The forward merge is idempotent and preserves unrelated skill references.
- Live preflight on 2026-07-29 found exactly two matching rows. Both already
  carried the intended skill sets from the reversible operator stop-bleed, so
  the forward migration is a no-op apart from normal hash/attribution
  convergence.
- A backwards function is present and the normal R0 gate exercises both schema
  directions before deployment.

## Reversible plan

- Before any later capability edits, `python manage.py migrate fleet 0035`
  removes only `human-browser-stealth` and `x-account-safety` from these two
  capability slugs.
- After later edits, do not run the automatic backwards migration because it
  cannot distinguish references added by this migration from later intentional
  additions. Restore the recorded pre-migration values explicitly instead.

## Verification gates

- R0 additive/semantic migration compatibility on staging Postgres.
- Live: both capability rows retain all intended skill refs and the affected
  social-media agents retain the skills after capability reconciliation.
