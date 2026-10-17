# PLAT-187 approval — claws/migrations/0030_tokenentry_agent_leasing_enabled.py

**Flagged for:** `migrations.RunPython` (preserve existing manual reserves).
**Reviewed by:** coordinator (Shizuha), 2026-07-26, while repairing the live
Token Pools defect where provider-health reports re-enabled credentials that an
owner had explicitly reserved from agent leasing.

## What it does
1. Adds `TokenEntry.agent_leasing_enabled` as an indexed Boolean with a safe
   default of `True`. Old application images ignore the additive column.
2. Backfills only rows matching the legacy manual-reserve representation:
   `active=False`, no cooldown, and a non-`auth_failed` status. Those rows get
   `agent_leasing_enabled=False`; no token material, health status, cooldown,
   lease, quota, ownership, or other existing field is changed.
3. All other rows retain the default `True`. In particular, terminal provider
   authentication failures remain health failures rather than being silently
   reclassified as owner policy.

## Live data and safety proof
- Immediately before approval, production held 21 non-deleted token entries.
  Five matched the bounded legacy-reserve predicate. The grouped health state
  was: active/ok 10, active/rate_limited 3, inactive/ok 2,
  inactive/rate_limited 3, inactive/unknown 2, inactive/auth_failed 1.
- The update is one indexed small-table statement over five rows. It only writes
  the newly added column and is idempotent.
- The default keeps every active credential leaseable until an owner explicitly
  reserves it. Existing inactive/no-cooldown rows preserve their prior
  non-leaseable behavior across the model split.
- Existing leases are untouched and expire normally. New selection filters on
  both provider health (`active`) and owner policy
  (`agent_leasing_enabled`), so a reserve takes effect without revocation.
- Codex re-auth and provider health reports update health only and do not mutate
  the new policy. The full local CI-container suite passed 1,318 tests before
  this approval, including reserve persistence, selection exclusion, re-auth,
  API count, and Grok device-auth coverage.

## Reversible plan
- Preferred application rollback keeps the additive column and restores the
  prior image. Old code ignores the field, so no data rollback is required.
- Hard schema reversal is `python manage.py migrate claws 0029`. This drops only
  `agent_leasing_enabled`; the reverse data migration is intentionally a no-op
  because the legacy `active=False` state remains unchanged on all rows that
  were backfilled.
- Before a hard reversal, reserve policy should be exported if owners have made
  new post-migration choices, because 0029 has no independent field capable of
  representing healthy-but-reserved credentials. No token, quota, or lease data
  is otherwise lost.

## Verification gates
- R0 must run the old/new compatibility exercise and apply 0030 on the isolated
  staging database before canary or production.
- Fleet rollout must apply `claws 0030` before the application image swap.
- Live verification must confirm the target OpenAI account is healthy,
  `agent_leasing_enabled=False`, excluded from token selection and available
  counts, unchanged by a subsequent `ok` health report, and rendered as
  `Personal reserve` with `Allow agents` in the authenticated Token Pools UI.
