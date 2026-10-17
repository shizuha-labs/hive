# Agent fleet cutover runbook: rt-fleet (host-docker) → k3s-native pods

**Task**: PLAT-152 (Eng/DevOps) · **EPIC**: PLAT-146 · **Contract**: HLD PLAT-147 v0.2.2
(wiki `84b482e6`) **§8** (single-active-instance + blue-green), **§9** (phased cutover),
**§11 R4** (split-brain, HIGH), **§12** H2/H5. Reporter/architect: Aoi.

> **The load-bearing invariant (HLD §8, R4=HIGH):** an agent identity must be live in
> **exactly one place**. Connect is **fan-out-to-both, no kick-old** (code-confirmed,
> PLAT-154): two live connections from one identity each receive *and process* every DM.
> Connect therefore provides **zero** safety net at the seam. **The drain + hard-fail
> start-guard below is the SOLE barrier against split-brain. Do not soften the zero-overlap
> gate for convenience.**

This runbook is **additive** and reversible: `rt-fleet` (the host-docker runtime on s1) is
left **stopped-but-intact** as the green fallback until an operator-gated decommission.

---

## 0. Roles & prerequisites

- **Driver**: DevOps (ichi/ni) — has s1 host-docker + `kubectl`/cluster reach. Engineering
  (this runbook's author) pairs but cannot reach the cluster directly.
- **Per-agent k3s side** is actuated by the Hive provisioner (`shizuha-labs/hive`
  `fleet/provisioner.py`): `start_agent` (scale Deployment→1), `stop_agent` (→0),
  `get_agent_status` (pod phase + `ready_containers`). `scale_agent` hard-enforces
  replicas ∈ {0,1} (HLD §8 single-active).
- **Per-agent rt-fleet side**: the agent runs as host-docker container `shizuha-agent-<u>`
  on s1 (ns `rt-fleet`, Deployment `shizuha-runtime-fleet`), launched by
  `shizuha-beta` `src/daemon/manager.ts`.
- **Preconditions before ANY cutover** (Ph0 must be satisfied for the candidate agent):
  - The agent's `FleetAgent` row exists and is **provisioned** (identity Secret + workspace
    Longhorn PVC + Deployment + broker sidecar) — `provision_agent` succeeded, Deployment at
    `replicas=0`.
  - Candidate is a **stateless, provably-no-exec** agent for Ph1 (no inner-docker exec;
    sandbox-dependent agents are a separate cohort **blocked on R1/PLAT-153**).
  - Workspace state has been migrated/replicated to the PVC (or the agent is stateless).

---

## 1. Per-agent cutover: `drain → start-guard → start → verify` (HLD §8)

Run for **one** agent at a time. **Never** start the k3s pod until step 2 passes.

### 1.1 Drain on rt-fleet (graceful)
Goal: finish/ack in-flight work and flush state so nothing is lost or double-processed at
the seam (the claw-Baileys ~10s flush analog; `terminationGracePeriodSeconds:30` alone does
**not** guarantee this).

1. Mark the agent **draining** (stop assigning new work to the rt-fleet instance).
2. Wait for the current turn to finish and the inbox to quiesce (no busy turn, depth 0).
3. Flush durable state to the PVC/platform; confirm sessions are persisted.
4. **Clean Connect disconnect**: the rt-fleet instance closes its Connect WS (so its
   `channel_name` leaves `user_inbox_<id>`). This is the precondition that makes step 2
   meaningful — a live old WS is exactly the split-brain.
5. `docker stop --time=30 shizuha-agent-<u>` on s1 (graceful SIGTERM → SIGKILL fallback).

### 1.2 Start-guard — POSITIVE drain + `exited` assertion (HARD-FAIL, zero overlap)
> This is the only split-brain barrier (R4). It is **not** a timer and **not**
> "stop issued" optimism. It is a positive confirmation the old instance is gone.

Assert ALL of the following on s1 **before** proceeding; **abort the cutover** (do not start
the k3s pod) if any check fails or is indeterminate:

- `docker inspect -f '{{.State.Status}}' shizuha-agent-<u>` returns **`exited`**
  (not `running`, not `restarting`; a `docker stop` can time out / zombie — assert the end
  state, mirror claw §4.3.1a).
- The container's Connect WS is closed (no lingering `user_inbox_<id>` membership for the
  rt-fleet channel). If a Connect-side check isn't available, the `exited` state above is the
  proxy — but the container MUST be `exited`, never merely "stopping".
- There is **no** other running rt-fleet container for `<u>` (guard against a respawn by the
  rt-fleet Deployment — pause/scale the rt-fleet manager for `<u>` so it does not recreate
  the container mid-cutover).

**If the guard fails → STOP. Leave the k3s pod at `replicas=0`. The agent stays on rt-fleet.**
Never "force ahead" — an overlap window = double real-time message processing.

### 1.3 Start the k3s pod
Only after 1.2 passes:
- `provisioner.start_agent(agent)` (scales the Deployment to 1). `scale_agent` refuses any
  value other than 0/1, preserving single-active inside k3s.

### 1.4 Per-agent verify gate (must ALL pass)
Via `get_agent_status(agent)` + platform checks:
- Pod `phase == Running` and `ready_containers == 2` (agent + broker sidecar both Ready —
  R5: the agent is readiness-gated on the sidecar).
- **Broker sidecar Ready** and it **minted the JWT** (agent received it over the pod-local
  socket; no AGENT_PASSWORD in the agent container).
- **Connect reconnect**: the k3s instance's WS is up and receiving DMs.
- **MCP/Pulse** reachable through the broker (a real `pulse_get_my_tasks` returns).
- **One real task end-to-end** (the agent picks up and completes/acks a task).

If verify fails → **roll back** (§3) and investigate; do not proceed to the next agent.

### 1.5 Next agent
Only advance when 1.4 is green. Cohort order per §9 Ph2 (sandbox-dependent cohort excluded,
blocked on R1).

---

## 2. Ph1 canary (HLD §9)

1–2 low-risk **stateless, no-exec** agents, soaked before any batch. Verify:
- Connect WS, MCP/Pulse, work execution (§1.4).
- **Restart-without-state-loss**: `kubectl delete pod` → Deployment reschedules → workspace
  PVC intact, session continues, sidecar re-mints JWT, **no manual step** (H3).
- JWT re-mint + broker sidecar readiness.
- **Node-spread**: confirm the canary lands per anti-affinity across the gx10-1..8 pool.

### 2.1 Same-identity overlap-negative seam test (R4 — MANDATORY, §11-R4/§13)
Deliberately attempt the failure the start-guard prevents, in a controlled window, and prove
the guard holds:
- With the canary live on **k3s**, attempt to start the **rt-fleet** instance for the same
  identity (or vice-versa) **without** the start-guard.
- Send a test DM to that identity and assert **exactly one** runner processes it (inspect both
  sides' logs / the single side-effect). Two processings = R4 reproduced = the guard is the
  only thing standing between us and split-brain → confirms why §1.2 is hard-fail.
- Then re-run the same scenario **with** the §1.2 start-guard and show the second instance is
  refused start (guard aborts). This is the acceptance evidence for H2's cross-cutover claim.

---

## 3. Rollback (HLD §8, H5) — proven on the canary before any batch

Reversible until rt-fleet decommission:
1. `provisioner.stop_agent(agent)` (k3s Deployment → 0). Confirm the k3s pod is **gone**
   (`get_agent_status` → no pod / phase terminal) and its Connect WS closed — same
   zero-overlap discipline in reverse.
2. Restart the agent on rt-fleet (un-pause/scale the rt-fleet manager for `<u>`; the
   `shizuha-agent-<u>` container comes back with its host state intact).
3. Verify the rt-fleet instance reconnects Connect + processes a task.

**H5 gate**: rollback must be demonstrated end-to-end on the canary **before** Ph2 batch.

---

## 4. Acceptance mapping (HLD §12)

| AC | Where proven |
|---|---|
| **H2** single-active in-cluster (`replicas=1`+`Recreate`) **and** across cutover (drain + hard-fail `exited` start-guard) | §1.2 + §2.1 |
| **H3** state carry: kill pod → reschedule, workspace/session intact, sidecar re-mints | §2 restart-without-state-loss |
| **H4** Connect + MCP/Pulse through the broker; canary does a real task e2e | §1.4 |
| **H5** blue-green rollback to rt-fleet proven on the canary before any batch | §3 |
| **M1** node-spread across gx10-1..8 under load | §2 node-spread |

---

## 5. Automation status / what's manual

This runbook is the **operator-gated procedure of record**. The drain/start-guard/verify/
rollback steps that touch the cluster + s1 host-docker are executed by DevOps (ichi/ni); the
k3s side is the existing provisioner (`start_agent`/`stop_agent`/`get_agent_status`). A
companion harness that scripts §1–§3 (incl. the §2.1 overlap-negative check) is a follow-up to
be **validated on-cluster with DevOps** before it drives a real cutover — until then these
steps are run by hand per this runbook, with the §1.2 start-guard checks performed explicitly.
The end-to-end H2/H5 proof requires the canary agent's per-agent pod (broker sidecar + image +
PVC) to be provisionable (Ph0).
