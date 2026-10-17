# PLAT-187 approval — fleet/migrations/0050_engineering_cluster_plane_kai.py

**Flagged for:** `migrations.RunPython` (dedicated cluster-plane capability + Kai seat override data).
**Reviewed by:** san (devops), 2026-08-20, under PLAT-5591/PLAT-5584 architecture contract (kumo triage A.2/A.3) + ADR-0016 `f1b4c974` + HIVE-272 `83b6280c` + PLAT-3412 runbook `86fbc086` v6.

## What it does

1. Creates (or updates) a dedicated `engineering-cluster-plane` CapabilityDefinition:
   - `sensitive_access_flags=["kubeconfig"]`, `default_credential_grant_scopes=["kubeconfig"]` — confers ONLY the kubeconfig sensitive flag/credential scope.
   - **No `host_exec` / `credential_broker`** — plane separation per PLAT-5584 contract (host SSH is NOT the substitute).
   - `risk_tier=sensitive`, `activation_status=active`, `lifecycle_status=draft`, `security_review_required=True` with approval metadata.
2. Creates an explicit authorizing `AgentCapabilityOverride` for `kai@shizuha.com` → `action=add` (HIVE-272 §4.2 — never annotation-only, never team-slug inference), with approval metadata.

## Why it is safe

- **Data-only; no schema DDL, no credential/lease/volume mutation, no host access.**
- Scope is exactly one new capability slug plus one named seat override (`kai@shizuha.com`).
- Forward is idempotent (`update_or_create` keyed on slug / `(agent_identifier, capability)`).
- RBAC is NOT widened in this migration — the bounded `agent-kai-ops` SA / `shizuha-fleet-agent-ops` ClusterRole (broad-but-bounded, NOT cluster-admin) is enforced at the deployment/RBAC layer, analogous to Ni's bounded matrix.
- The override is an explicit, audited, approved grant (HIVE-272 §4.2) restoring Kai's historical cluster-plane contract (PLAT-3412 list: ichi, san, kai, ni); it does not grant any new plane beyond kubeconfig.

## Reversible plan

- `python manage.py migrate fleet 0049` runs `backwards()`: deletes the `kai@shizuha.com` override and the `engineering-cluster-plane` capability row. No other object depends on either at the 0049 boundary.
- Post-reverse, the daemon renderer selects the ordinary variant for Kai (no kubeconfig init/mount/env) — the pre-PLAT-5591 state.

## Verification gates

- Local: `fleet.test_hive273_capabilities` (capability confers kubeconfig-only; kai-shaped fixture surfaces kubeconfig in signed evidence).
- Live after deploy: Kai's signed effective capabilities include `kubeconfig`; daemon re-renders Kai Deployment with `serviceAccountName=agent-kai-ops` + kubeconfig init/mount/env; `kubectl auth can-i` from Kai pod is bounded (NOT cluster-admin).
