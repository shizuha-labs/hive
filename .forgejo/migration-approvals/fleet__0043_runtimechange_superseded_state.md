# PLAT-187 approval — fleet/migrations/0043_runtimechange_superseded_state.py

**Flagged for:** `migrations.AlterField` on `RuntimeChange.state`.
**Reviewed by:** coordinator (framework-maintainer), 2026-08-01, after live
evidence showed Hive replaying an obsolete generation-3 runtime-lane outbox to
a daemon already converged on generation 7 every heartbeat.

## What it does

- Adds `SUPERSEDED` to the application-visible choices for the existing
  `fleet_runtimechange.state` `varchar(32)` column.
- Does not change the column type, width, nullability, index, or default and
  adds no database constraint or data rewrite.
- Allows the reconnect and heartbeat convergence paths to record why an older
  undelivered lane was terminally retired instead of replaying it forever.

## Why it is safe

- The state value is written only while the corresponding outbox is made
  terminal (`delivered_at` set) in the same transaction, so it cannot suppress
  the newest desired generation or an exact acknowledgement still required by
  the daemon.
- Selection requires `change.desired_generation < current desired_generation`;
  current and future outboxes remain untouched.
- Write-time supersession prevents new orphans, while reconnect and heartbeat
  repair converge rows created before this invariant without an operator data
  mutation.

## Reversible plan

- Reversing the migration removes only the Django choice metadata; PostgreSQL
  keeps the same unconstrained `varchar(32)` representation.
- Rows already marked `SUPERSEDED` remain safe after an application rollback
  because their outboxes are terminal and therefore cannot be replayed by the
  older retry query.
- If an application rollback requires choice normalization for an admin/export
  consumer, `SUPERSEDED` may be mapped to `FAILED` after verifying
  `delivered_at IS NOT NULL`; this is cosmetic and must not clear terminal
  delivery evidence.

## Verification gates

- A production-order regression creates generation 3, advances desired truth
  to generation 7, sends two heartbeats, and proves neither heartbeat emits the
  stale frame while both change and outbox become terminal.
- A write-path regression proves accepting generation N+1 atomically
  supersedes the unacknowledged generation N but leaves N+1 pending.
- Migration graph, `makemigrations --check`, Django checks, 65 affected-module
  tests, the complete 1,424-test Hive suite, and the local primary-branch CI
  gate (1,424 backend plus 135 frontend tests) passed before release.
- Live release verification must show Nova's generation-3 orphan becomes
  `SUPERSEDED`, the repeated daemon warning stops across multiple natural
  heartbeat intervals, and Hive's operator-facing activity remains fresh.
