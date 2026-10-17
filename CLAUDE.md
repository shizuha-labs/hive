# CLAUDE.md - Shizuha Hive

## Overview

**Shizuha Hive** is the agent control-plane. It provisions and manages **FleetAgents** (native k3s org seats and isolated personal talk seats) on Kubernetes. A personal assistant is an owned isolated `FleetAgent` (`shizuha-{user_id}`), parked hibernated until first use.

**Claw** (isolated OpenClaw/Hermes/SCLI tenant) is a **retired customer product** (ADR-0080 / HIVE-2007). Do not auto-provision new Claw rows. Existing Claw rows stay readable/stoppable until drained. Shared infra that still lives under `claws/` (token coordinator, metering, org templates, OAuth) is not the Claw product — do not delete the Django app.

| Setting | Value |
|---------|-------|
| Backend Port | 8030 |
| Database | shizuha_hive |
| URL Path | `/hive/` |

## Agent API (first-class)

```
GET    /hive/api/v1/fleet/agents/                       # FleetAgent list
POST   /hive/api/v1/fleet/agents/                       # Provision a FleetAgent
GET    /hive/api/v1/fleet/personal-agent                # Caller's default personal seat
POST   /hive/api/v1/fleet/internal/ensure-personal-agent/  # ID activation hook (hibernated)
POST   /hive/api/v1/fleet/internal/connect-wake/        # Wake on real owner DM
```

## Remaining Claw API (drain only)

```
GET    /hive/api/v1/claws/                          # List caller's remaining claws
POST   /hive/api/v1/claws/                          # 410 Gone — provisioning retired
GET    /hive/api/v1/claws/mine                      # Existing claw if any; never creates or auto-starts
GET    /hive/api/v1/claws/{claw_id}/                # Claw detail
PATCH  /hive/api/v1/claws/{claw_id}/                # Rename display name
POST   /hive/api/v1/claws/{claw_id}/start/          # Scale 0 -> 1 (explicit)
POST   /hive/api/v1/claws/{claw_id}/stop/           # Scale 1 -> 0
DELETE /hive/api/v1/claws/{claw_id}/?confirm=<name> # Two-phase delete
```

Idle running Claws are scaled to zero by `finalize_claw_deletions` (existing CronJob) via `lifecycle.idle_stop_candidates()`. PVCs are retained.

## Personal default seats

Eligible humans get one isolated FleetAgent, status `hibernated`, `hibernation_reason=default_until_first_use`. Ensure must not start a pod. The on-demand sweep must not wake these seats; connect-wake / explicit enable does.
