# PLAT-187 approval — claws/migrations/0035_reassert_confirmed_xai_credit_cooldowns.py

**Flagged for:** `migrations.RunPython`.
**Reviewed by:** coordinator (framework-maintainer), 2026-08-01, after live
verification caught a stale health-writer race following migration 0034.

## What it does

- Reasserts reset-length cooldowns only for active, agent-leasable, non-deleted
  xAI entries with confirmed zero-credit `grok_xai_usage` snapshots.
- Marks those rows rate-limited and fills a missing rate-limit timestamp.
- Never reads, rotates, decrypts, or changes credential material.

## Why it is safe

- The application success/report-status chokepoints now re-read current xAI
  quota state and cannot clear or shorten a confirmed reset-length cooldown.
- Positive-capacity, uncertain/error, past-reset, inactive, reserved, non-xAI,
  and deleted entries are excluded; existing later cooldowns are not shortened.

## Reversible plan

- Reverse is intentionally a no-op because removing a still-confirmed
  zero-credit cooldown would recreate the production retry loop.
- A later positive snapshot or provider reset boundary restores normal routing.

## Verification gates

- Regression tests reproduce a stale pre-migration `ok` writer and late success
  feedback, proving neither can clear the newer zero-credit cooldown.
- Migration graph, approval gate, Django checks, and the complete CI-equivalent
  backend/ring/frontend suites must pass before Origin push.
- Live verification must prove all confirmed zero-credit cooldowns cover their
  resets after the background refresh/health writers have run.
