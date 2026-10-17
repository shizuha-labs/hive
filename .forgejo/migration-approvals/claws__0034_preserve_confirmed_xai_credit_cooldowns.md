# PLAT-187 approval — claws/migrations/0034_preserve_confirmed_xai_credit_cooldowns.py

**Flagged for:** `migrations.RunPython`.
**Reviewed by:** coordinator (framework-maintainer), 2026-08-01, implementing the
operator's explicit xAI credit-exhaustion fail-closed directive.

## What it does

- Selects only active, agent-leasable xAI entries with a non-deleted row and a
  confirmed `grok_xai_usage` snapshot whose measured windows all have 0% left.
- Extends `cooldown_until` to the latest future provider `resets_at`, marks the
  entry rate-limited, and fills a missing rate-limit timestamp.
- Never reads, rotates, decrypts, or changes credential material.

## Why it is safe

- Exact positive-capacity, uncertain/error, stale-past-reset, inactive,
  reserved, non-xAI, and deleted entries are excluded.
- Existing later cooldowns are never shortened.
- The same predicate is enforced by the application routing/recovery
  chokepoints, so the data repair makes stored state truthful without creating
  a second policy.

## Reversible plan

- Reverse is intentionally a no-op: clearing a still-confirmed zero-credit
  cooldown before its provider reset would recreate the production failure.
- The normal quota refresh path clears eligibility through a later positive
  snapshot or the reset boundary; no schema is added.

## Verification gates

- Regression tests prove only confirmed, leasable zero-credit xAI rows receive
  the reset-length cooldown.
- Migration graph, `makemigrations --check`, Django checks, and the full backend
  suite must pass before Origin push.
- Live verification must compare every redacted zero-credit reset with its
  stored cooldown and prove the broker does not route those rows.
