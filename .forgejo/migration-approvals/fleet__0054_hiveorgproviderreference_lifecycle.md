# PLAT-187 approval — fleet/migrations/0054_hiveorgproviderreference_lifecycle.py

**Flagged for:** `AlterField` (choices widening) + `AddField` ×5.
**Reviewed by:** san (devops seat, custody of HIVE-2161), 2026-09-10, against
the HIVE-336 HLD contract. Migration authored for the HIVE-2161 org provider
credential lifecycle (hive#278 heads 74750ba → be4529c).

## What it does
1. `AlterField HiveOrgProviderReference.status` — widens the `choices` set
   (adds `verifying`, `probation`, `exhausted` alongside the existing
   `active`/`disabled`/`mismatch`/`invalid`). Same `max_length=20`, same
   `default='active'`. No stored value is rewritten; existing rows keep their
   current status strings, all of which remain valid choices.
2. `AddField provider_type` — new `CharField(choices=[openai, anthropic,
   openai_compatible], default='openai_compatible', max_length=20)`.
3. `AddField display_name` — new `CharField(blank=True, default='', max_length=200)`.
4. `AddField last_validation_result` — new `CharField(blank=True, default='',
   max_length=40)`; redacted outcome code only (never a raw body), per the
   HIVE-1862 §4 redaction invariant.
5. `AddField created_by_principal` / `updated_by_principal` — new
   `CharField(blank=True, default='', max_length=200)` audit principals.

## Why it is safe
- Purely additive schema evolution on a model that has existed since
  `0050_hiveorgproviderreference` (applied on main); no `RunPython`, no data
  backfill, no column drop, no table rewrite.
- Every new column carries a default, so old app code running against the new
  schema is unaffected, and the migration cannot fail on existing data.
- The `status` choices widening is backward-compatible: every value already
  present in the column remains a legal choice; the new values are only
  written by the new lifecycle endpoints (provision/rotate/disable).
- No raw key material is stored in any new column — the secret key is relayed
  in-memory only and delegated to Cortex (HIVE-1889 thin-mirror posture).

## Reversible plan
- Django reverse: `migrate fleet 0053` — the `AddField` reversals drop the
  five new columns; the `AlterField` reversal restores the prior `choices`
  set. No other object depends on the new columns at the 0053 boundary.
- Data loss on reverse is limited to the new columns' values (provider_type,
  display_name, last_validation_result, audit principals), all of which are
  re-derivable from the live Cortex listing state or re-entered via the
  lifecycle endpoints — nothing irrecoverable.
- Rollback of the code path is the PR revert; the mirror resolver filters
  non-enabled refs, so a stale mirror never routes (documented on the
  DisableView contract).

## Verification
- Prod DB confirmed at `fleet 0053` before this approval (0054 not yet applied).
- The premerge battery runs the full fleet suite (including
  `fleet/test_hive2161_lifecycle.py`, 21 tests) against the migration state;
  the compat gate exercises old→new→old schema/health in staging before any
  prod rollout, per the standing release-lane discipline.
