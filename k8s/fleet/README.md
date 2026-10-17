# Fleet networking & DevOps isolation (PLAT-151, EPIC PLAT-146)

Static, cluster-singleton manifests for the k3s-native agent runtime. Grounded in
HLD **PLAT-147 v0.2** (wiki `84b482e6`) §3 (Q1/Q1b), §7 (Networking), §11 R3.

These are **additive** — they do not touch the existing host-docker `rt-fleet`.
Per-agent objects (identity Secret, workspace PVC, Deployment, broker sidecar) are
emitted imperatively by the generalized Hive provisioner; the objects here are
namespace-wide singletons applied once.

## Files (apply in order)

| File | Contents |
|---|---|
| `00-namespaces.yaml` | `shizuha-fleet` (shared, ordinary agents) + `shizuha-fleet-devops` (isolated ichi/ni). Baseline Pod Security. |
| `10-networkpolicy-fleet.yaml` | `shizuha-fleet` egress allowlist: default-deny, DNS, platform (ns `shizuha`), external LLM HTTPS, registry note. |
| `20-devops-isolation.yaml` | `shizuha-fleet-devops` SA + zero-privilege RBAC + default-deny + DNS/platform egress + **finalized** SSH egress (PLAT-156/PLAT-174). |

## Security model (why shared-ns is safe)

The fleet is **single-tenant**, so the namespace is not the security boundary
(unlike HIVE-1/2 multi-tenant). Three structural facts make it safe:

1. **`automountServiceAccountToken: false`** on every agent pod (set by the
   provisioner) **and on each namespace's `default` ServiceAccount** (pinned in
   `00-namespaces.yaml`, so a pod that omits `serviceAccountName` still gets no
   token) → no in-cluster kube-API path, so one pod cannot enumerate siblings' Secrets.
2. **Default-deny NetworkPolicy** in both namespaces → no agent-to-agent traffic,
   no ingress; only the explicit egress allowlist is reachable.
3. **DevOps SSH-bearing agents are isolated** in `shizuha-fleet-devops`; the
   fleet-ssh Secret lives only there and no `shizuha-fleet` ServiceAccount has a
   RoleBinding into that namespace → ordinary agents cannot read it by any API path.

Per-agent RWO PVCs (provisioner-emitted) close volume-sharing (PLAT-86); the
namespace was never that lever.

## Egress allowlist (`shizuha-fleet`)

- **DNS** → kube-system `k8s-app=kube-dns` :53.
- **Platform** → ns `shizuha` Services (shizuha-id :8000, pulse :8002, wiki :8013,
  connect :8019). Add ports here as needed (kept an explicit allowlist for least
  privilege; can be tightened to per-Service further).
- **Remote LLM (external)** → TCP 443 to `0.0.0.0/0` **except** RFC1918 + link-local
  (incl. `169.254.169.254` metadata) + `100.64.0.0/10` (Tailscale/CGNAT). This is
  the load-bearing property: external LLM reachable, internal hosts not.
- **In-cluster Cortex/vLLM** → ns `shizuha-cortex` :8040 (repo default
  `VLLM_BASE_URL`). Required separately because the Cortex ClusterIP is RFC1918
  (excluded by the 443 rule) and not in ns `shizuha`.
- **Registry** (`gx10-1:30500`) → image pulls are kubelet-level (not pod-NetPol),
  so no rule needed for startup. A commented node-CIDR block covers any in-pod
  registry API use (DevOps to confirm the gx10-1 address).

The `shizuha-fleet-devops` ns carries the **same** DNS / platform / external-LLM /
Cortex egress allowlist (ichi/ni are still LLM-driven agents). It also includes the
finalized host-plane SSH egress (see Fleet-SSH section below).

## Probes

Keep agent readiness/liveness as `exec` or `tcpSocket`, not `httpGet` — default-deny
ingress can drop kubelet httpGet probes depending on CNI source-IP handling.

## Fleet-SSH posture (PLAT-156 decided, finalized in PLAT-174 — wiki `74b498f3`)

R3 is decided. The `allow-hostplane-ssh-egress` NetworkPolicy is now live in
`20-devops-isolation.yaml`:

- **Allowed egress**: `10.200.10.0/24:22` (gx10-1..12 cluster management LAN)
  and `100.64.0.3/32:22` (s1 Tailscale/tailnet IP). Scoped to pods with
  label `shizuha.io/agent-role: devops-ssh`.
- **Tailscale-ACL enforcement**: NetworkPolicy grants the k8s-level path;
  Headscale per-identity ACLs (ichi vs ni, default-deny) enforce node-level
  access control on top. Both layers must allow a connection.
- **Auth (axis iii)**: Tailscale SSH preferred (no key at rest). Fallback:
  short-TTL SSH certs minted from the broker CA (CA key stays in control-plane,
  never in the agent pod). Raw long-lived bearer key is REJECTED as standing posture.
- **Per-agent Secret shape**: documented in `20-devops-isolation.yaml` section (B).
  Provisioner emits the correct Secret type per the deployed mechanism.
- **Headscale ACL config**: operational (not a manifest); apply per PLAT-174
  instructions. Separate ichi and ni identities; scope each to only the nodes/ports
  they need; default-deny all else.

## Validation

```sh
kubectl apply --dry-run=server -f k8s/fleet/00-namespaces.yaml
kubectl apply --dry-run=server -f k8s/fleet/10-networkpolicy-fleet.yaml
kubectl apply --dry-run=server -f k8s/fleet/20-devops-isolation.yaml
```

During canary (Ph1, HLD §9): confirm an agent pod resolves DNS, reaches the
platform Services and its LLM endpoint, and **cannot** reach a sibling agent pod
or an internal host on 443.
