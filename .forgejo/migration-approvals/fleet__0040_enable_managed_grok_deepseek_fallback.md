# PLAT-187 approval — fleet/migrations/0040_enable_managed_grok_deepseek_fallback.py

**Flagged for:** `migrations.RunPython`.
**Reviewed by:** coordinator (framework-maintainer), 2026-08-01, implementing the
operator's explicit Grok continuity directive.

## What it does

- Selects only the enabled named Grok agents `goro`, `hana`, and `nagi` whose
  Hive primary lane is `grok_build` on an admitted Grok model.
- Writes the single platform-managed chain: the existing Grok primary followed
  by `shizuha` on `cortex/DeepSeek-V4-Flash`.
- Writes the same chain into `pending_config`, Hive's durable daemon outbox, so
  the normal reconnect replay applies it after the CI rollout.
- Adds no schema and changes no credentials, lifecycle status, task ownership,
  identity, model primary, or workspace state.

## Why it is safe

- Scope is bounded by three exact usernames, `status='running'`, the exact
  `grok_build` method, and the admitted Grok model set.
- The primary lane remains Grok. DeepSeek is used only after the existing
  runtime's ordinary fallback-eligible error contract observes xAI auth,
  quota, rate-limit, transport, or provider failure.
- The DeepSeek target is the already-deployed Cortex model alias; no model
  workload, GPU placement, context length, admission cap, or credential is
  changed by this migration.
- Arbitrary caller-supplied fallback chains remain rejected in Hive admission.

## Reversible plan

- The reverse removes only the exact managed chain written by this migration
  and queues an explicit empty fallback list in the durable outbox so an
  already-applied daemon chain is cleared too. A chain changed after rollout is
  left untouched.
- Application rollback is schema-compatible because no columns or constraints
  are added.
- If rollback occurs after the daemon has already applied the chain, Hive's
  reverse pending state is replayed through the same authoritative config path;
  no daemon API or pod environment is patched by hand.

## Verification gates

- Regression tests prove only Grok receives the managed tail, arbitrary chains
  stay rejected, named rows are backfilled, and pending replay matches desired
  state.
- Migration graph, `makemigrations --check`, Django checks, and the full backend
  suite must pass before Origin push.
- Live verification must compare Hive desired state, daemon observed state,
  Prometheus per-agent capacity/provider series, and a real agent activity turn.
