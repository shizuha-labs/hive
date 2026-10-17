# Runbook: Hive Claw/Hermes customer data storage on Longhorn

Pulse: PLAT-1229

Customer OpenClaw/Hermes namespaces (`claw-<owner>-<id>`) must keep `claw-data`
on Longhorn/HA storage, not `local-path`. A `local-path` PV pins the dashboard to
one worker through PV nodeAffinity; if that worker is cordoned or rebooted, the pod
stays Pending and the customer dashboard returns 502.

## Inventory check

```bash
kubectl get pvc -A -o wide \
  | awk '$1 ~ /^claw-/ {print $1, $2, $6, $8}'

# This must print no rows for active customer data PVCs:
kubectl get pvc -A -o wide \
  | awk '$1 ~ /^claw-/ && $2 == "claw-data" && $6 == "local-path" {print}'
```

Expected: every active `claw-data` PVC is `longhorn`, `longhorn-3x`, or another
approved HA storage class.

## Worker-cordon regression check

Pick any running claw namespace and its current node:

```bash
NS=<claw-namespace>
NODE=$(kubectl -n "$NS" get pod -l app=claw -o jsonpath='{.items[0].spec.nodeName}')
kubectl cordon "$NODE"
kubectl -n "$NS" rollout restart deploy/claw
kubectl -n "$NS" rollout status deploy/claw --timeout=300s
kubectl -n "$NS" get pod -l app=claw -o wide
kubectl uncordon "$NODE"
```

Pass: the replacement pod schedules on another Longhorn-ready node and the
dashboard answers HTTP 200 through `kubectl port-forward pod/<pod> 18789:18789`
(or the public dashboard route). Fail: pod remains Pending with PV nodeAffinity.

## Migration pattern used for PLAT-1229

For an active local-path PVC, scale the claw deployment to zero, copy data to a
temporary Longhorn PVC, patch the old PV reclaim policy to `Retain`, replace the
`claw-data` claim with a Longhorn-backed claim of the same name, copy data back,
then scale up and verify gateway `ready` logs plus HTTP 200. Keep the old local
PV retained as a short rollback point until the post-migration soak is complete.
