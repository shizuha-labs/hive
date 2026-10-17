# Runbook: RBAC bound-surface shrink on a CI lane identity

**Alert**: `rbac-drift-audit` job fails with exit code 2 (`RBAC SHRINKAGE
DETECTED`).
**Owner**: devops
**Severity**: high
**Origin incident**: 2026-09-29 CI deploy-lane outage — a bare, unlabeled
`hive-ci-ns-reader` ClusterRoleBinding was applied with no intended Role
wired, silently shrinking the `build:hive-ci-deployer` bound surface.

## What fired

The daily `rbac-drift-audit` CI job compared the live bound surface of each
lane identity (default: `build:hive-ci-deployer`, `build:origin-ci-deployer`)
against `k8s/rbac-drift-baseline.json` and found either:

- `removed`: a bound entry (RoleBinding / ClusterRoleBinding + role
  reference) that is no longer bound, or
- `narrowed`: an entry still bound, but with strictly fewer
  verbs/resources/resourceNames than baseline.

## Immediate actions (in order)

1. **Read the failing run log.** The `removed`/`narrowed` payload names the
   exact binding, namespace, role kind/name, and baseline rules that shrank.
2. **Identify who/what changed the RBAC.** Renewing or deleting RBAC objects
   is operator-plane work — check the broker/admin-plane actor log for
   attribution before touching anything.
3. **Reconcile the intended principal/source first.** Determine whether the
   lanes are meant to run on SA-direct tokens or the kubeconfig identity
   (PLAT-9905 chain). Do NOT blind-apply RBAC or re-run lanes to "see if it
   heals".
4. **Restore the missing grants as one atomic apply** — the complete RBAC
   contract for the identity in a single `kubectl apply -f ...`. Never wire a
   fresh SA with a partial-binding intermediate state.
5. **Verify from the live surface, not a reviewed source**: re-run the audit
   job (workflow_dispatch) and confirm exit 0, then confirm one real lane
   run's DEPLOY-stage verdict is green.

## Renewing the baseline (expected growth, not a page)

```
python -m security.rbac_drift_audit --capture k8s/rbac-drift-baseline.json
```

Run on a seat with cluster access, commit the updated baseline. Growth
(more bindings/verbs) does not fire the alert; only removal or narrowing does.

## Acceptance provenance

- Unit leg (`security.tests.test_rbac_drift_audit`) proves: a deliberately
  removed binding fires, verb narrowing fires, resource narrowing fires,
  stable surface and growth do not.
- First live fire on the cluster: run the audit once against a test SA whose
  binding is deliberately shrunk — the job must fail within one audit cycle,
  and pass again after the binding is restored.
