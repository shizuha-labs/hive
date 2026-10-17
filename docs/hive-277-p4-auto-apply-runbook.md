# HIVE-277 P4 Recovery Auto-Apply Runbook

Pulse: HIVE-277

## Scope

Auto-apply is default-off. It may only be enabled for the HLD v3 §8.1 bounded, daemon-owned actions after Security/CoS sign-off:

- `reconcile_identity_alias`
- `refresh_credential_grants`
- `apply_effective_runtime_config`
- `refresh_team_roster_cache`
- `pull_desired_state_now`
- scoped single-agent `restart_agent`

Never allow cross-org identity changes, user create/delete, external/billing credentials, TeamRoster mutation, disable/delete, broad daemon/fleet restart, or privilege expansion without an approved desired-state generation.

## Enable

Set both controls; empty allowlist means dry-run only.
Wildcards are intentionally unsupported: each action must be named explicitly.

```bash
HIVE_RECOVERY_AUTO_APPLY_ENABLED=true
HIVE_RECOVERY_AUTO_APPLY_ACTIONS=reconcile_identity_alias,refresh_credential_grants
# Optional emergency rollback of one action even while global flag stays on:
HIVE_RECOVERY_AUTO_APPLY_DENY_ACTIONS=restart_agent
```

Rollback is immediate: set `HIVE_RECOVERY_AUTO_APPLY_ENABLED=false` or put the action in `HIVE_RECOVERY_AUTO_APPLY_DENY_ACTIONS`. No schema rollback is required; new actions return to dry-run/denied by policy.

## Verify

1. Create/observe a dry-run `AgentRecoveryAction` for the target issue and confirm audit fields contain no secret values.
2. Enable only the intended action in `HIVE_RECOVERY_AUTO_APPLY_ACTIONS`.
3. Create an apply-mode action and confirm the daemon returns a `recovery_result` with `status=applied`, redacted `before_summary`/`after_summary`, and matching `correlation_id`.
4. Check metrics:

```bash
curl -fsS https://hive.shizuha.com/v1/fleet/recovery-actions/metrics/
```

Expected signals:

- `hive_recovery_auto_apply_enabled 1` only during the rollout.
- `hive_recovery_actions_total{action_type=...,mode="apply",status="applied"}` increments for successful actions.
- `hive_recovery_actions_total{...,status="denied"}` or `{...,status="failed"}` should page the owning team if sustained.
- `hive_recovery_actions_stale_total` should stay `0`; non-zero means actions expired without daemon results.
- `hive_recovery_secret_rejections_total` increments when Hive rejects recovery payloads containing plaintext secret material.

Alert rules live in `k8s/hive-recovery-actions-alerts.yaml`:

- `HiveRecoveryActionDeniedOrFailed` for apply-mode denied/failed actions;
- `HiveRecoveryActionStale` for pending/sent actions past TTL;
- `HiveRecoverySecretRejection` for secret-redaction/rejection events.

## Alerts / stop conditions

Stop and roll back to dry-run if any of these occur:

- stale pending/sent actions > 0 for more than one action TTL;
- denied/failed apply results for the same action type repeat;
- daemon result contains attempted secret material (Hive redacts it, but the daemon path is misbehaving);
- desired-state generation/hash cannot be proven current;
- target runtime id does not match the affected agent.

For `apply_effective_runtime_config` apply-mode actions, Hive rejects the row
unless `params.desired_generation` and `params.desired_config_hash` match the
current `AgentRuntimeDesiredState` row for that agent. A daemon-provided hash is
not enough provenance.

## Audit

Every action must retain action id, correlation id, actor/source, target runtime id, issue ids, action type, mode, before/after summaries, daemon result, and timestamps. Do not paste raw secrets into Pulse/wiki; refer to redacted audit row ids.
