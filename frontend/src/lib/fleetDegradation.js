import { isProductiveActivity } from './activityPhase.js'

// CON-176: honest degradation signals for the fleet view.
//
// Two distinct failure shapes must both surface visibly:
//  - STALE:       the backend served last-known-good data with pulse_degraded set.
//  - UNAVAILABLE: the pulse-snapshot fetch failed before Hive ever had a stale
//    payload (cold failure) — react-query has isError and NO data. Workload
//    counters must render as unavailable, never as confident zeros.
export function derivePulseMeta(pulse) {
  const data = pulse?.data
  const pulseUnavailable = Boolean(pulse?.isError) && !data
  return {
    pulseDegraded: Boolean(data?.pulse_degraded) || pulseUnavailable,
    pulseUnavailable,
    pulseStaleAgeSeconds: data?.pulse_stale_age_seconds ?? null,
  }
}

// CON-176/SCLI-199: never assert a default model as if it were live data.
// Until the runtime/store reports a concrete model, the answer is Unknown.
export function modelLabel(agent) {
  return agent?.effective_model || agent?.model || 'Unknown'
}

export function modelSourceLabel(agent) {
  if (agent?.model_source === 'pinned' || agent?.model) return 'pinned'
  if (agent?.model_source === 'fleet_default') return 'fleet default'
  if (agent?.model_source === 'runtime_daemon') return 'runtime daemon'
  return 'unknown'
}

// A fresh runtime can prove that an older Pulse/heartbeat availability snapshot
// is stale. Keep the proof deliberately narrow: the process must be actively
// working, telemetry must be recent, and it must expose output or a successful
// progress heartbeat. Explicit provider/token-pool unavailability remains a
// separate hard signal and is never cleared by this helper.
export function hasFreshProductiveTelemetry(agent) {
  if (!isProductiveActivity(agent)) return false
  const rawSnapshotAge = agent?.health_summary?.last_snapshot_age_s
  const snapshotAge = Number(rawSnapshotAge)
  const telemetryFresh = rawSnapshotAge != null &&
    Number.isFinite(snapshotAge) &&
    snapshotAge >= 0 &&
    snapshotAge <= 180
  const outputRate = Number(agent?.tokens_per_sec)
  const heartbeatOutcome = String(agent?.heartbeat_outcome || '').toLowerCase()
  const progressEvents = Number(agent?.heartbeat_progress_event_count)
  const productiveHeartbeat = ['worked_task', 'forwarded_task'].includes(heartbeatOutcome) ||
    (Number.isFinite(progressEvents) && progressEvents > 0)
  const explicitlyHealthy = agent?.health_ok === true &&
    agent?.provider_unavailable !== true &&
    agent?.health_token_pool_unavailable !== true
  return telemetryFresh && (
    explicitlyHealthy ||
    (Number.isFinite(outputRate) && outputRate > 0) ||
    productiveHeartbeat
  )
}

// Provider capacity is narrower than general runtime health. A bridge can report
// health_ok=false after a single failed turn (for example, a stream disconnect)
// while the provider/token pool remains available and the next turn is already
// running. Keep that transient degradation visible, but never mislabel it as a
// provider outage.
export function agentRuntimeHealth(agent) {
  const providerUnavailable = agent?.provider_unavailable === true ||
    agent?.health_token_pool_unavailable === true
  const runtimeConfigDrift = agent?.runtime_config_drift === true
  const freshWorkingEvidence = hasFreshProductiveTelemetry(agent)
  const unavailable = providerUnavailable ||
    (agent?.availability_status === 'unavailable' && !freshWorkingEvidence)
  // Productive telemetry cannot clear config drift: output from the wrong
  // model is positive evidence that desired and running state still differ.
  // Keep ordinary one-turn errors suppressible by fresh work, but make this
  // operator-facing invariant fail closed even against an older Hive backend
  // that still reports health_ok=true.
  const degraded = !unavailable && (
    runtimeConfigDrift || (
      (agent?.health_ok === false ||
        agent?.health_summary?.highest_severity === 'error') &&
      !freshWorkingEvidence
    )
  )
  return { unavailable, degraded }
}

// HIVE-1690 / binding v1.1: a `running` lifecycle state is INTENTION, not
// observation. Backend attaches typed readiness issues
// (`runtime_unavailable` / `runtime_health_unknown`) from Deployment
// ready_replicas — never a last_active_at age threshold. The UI mirrors those
// issue types so summary/card agree with health_summary.ok.
export function hasUnprovenRunningRuntime(agent) {
  const types = agent?.health_summary?.issue_types
  if (Array.isArray(types)) {
    if (types.includes('runtime_unavailable') || types.includes('runtime_health_unknown')) {
      return true
    }
  }
  const issues = agent?.health_summary?.issues
  if (Array.isArray(issues)) {
    return issues.some((i) => (
      i?.type === 'runtime_unavailable' || i?.type === 'runtime_health_unknown'
    ))
  }
  // Fail closed on explicit ok=false for a claimed-running row when the
  // backend already computed the summary (issue_types may be omitted by an
  // older payload shape).
  if (agent?.health_summary?.ok === false && String(agent?.status || '').toLowerCase() === 'running') {
    const sev = agent?.health_summary?.highest_severity
    if (sev === 'error') return true
  }
  return false
}

/** @deprecated binding v1.1 — age thresholds removed; use hasUnprovenRunningRuntime */
export const RUNTIME_TELEMETRY_STALE_MAX_AGE_S = 24 * 60 * 60
export function hasStaleRuntimeTelemetry(agent) {
  return hasUnprovenRunningRuntime(agent)
}

export function agentHealthVerdict(agent, availabilityStatus = agent?.availability_status) {
  const status = String(agent?.status || '').toLowerCase()
  const effectiveAgent = availabilityStatus == null
    ? agent
    : { ...agent, availability_status: availabilityStatus }

  // HIVE-1599/HIVE-510: lifecycle intent is an admissibility gate. These
  // states are deliberately not part of the attention population, even when a
  // stale producer flag or an availability override accompanies the row.
  if (['offline', 'hibernated', 'stopped', 'pending'].includes(status)) {
    return 'offline'
  }

  // The heartbeat producer owns this episode and its ordered clear. Runtime
  // health, activity, throughput, and telemetry receipt age do not measure
  // Pulse progress, so they cannot downgrade an explicit episode to Alive.
  // Provider capacity remains visible as secondary detail in the card.
  if (agent?.needs_help === true) return 'needs_help'

  const runtimeHealth = agentRuntimeHealth(effectiveAgent)

  // HIVE-1288: liveness must not mask a fault.
  //
  // `runtimeHealth.unavailable` folds together two different things: genuine
  // CAPACITY unavailability (provider down / token pool exhausted) and a mere
  // Pulse LIVENESS reading (`availability_status === 'unavailable'` with no
  // fresh evidence). Returning 'unavailable' for both, before the
  // failed/blocked branch below, meant a persisted non-capacity fault was
  // reclassified as "not running" — and the Agents summary counts
  // `health === 'needs_help'`, so it rendered NEEDS HELP = 0 while the card
  // showed Failed + the error and /hive/overview counted the same agent as
  // needing attention. (Live: @fumi, status=failed,
  // "attempt to write a readonly database", last active 2026-06-18.)
  //
  // Capacity unavailability keeps precedence — it is a real "cannot run" and
  // is tracked as capacity, not as a fault. Liveness does not, because
  // `failed`/`blocked` IS the fault signal and an agent that is both stale and
  // failed is precisely what "needs help" means.
  //
  // Not a HIVE-510 regression: that fixed Stopped agents being falsely
  // counted, and `stopped`/`offline`/`pending` are idle statuses which never
  // reach the failed/blocked branch.
  const capacityUnavailable = effectiveAgent?.provider_unavailable === true ||
    effectiveAgent?.health_token_pool_unavailable === true

  if (capacityUnavailable) return 'unavailable'
  if (status === 'failed' || status === 'blocked') return 'needs_help'
  if (runtimeHealth.unavailable) return 'unavailable'
  if (runtimeHealth.degraded) return 'needs_help'
  if (status === 'external') {
    if (hasFreshProductiveTelemetry(effectiveAgent)) return 'alive'
    return availabilityStatus === 'available' ? 'alive' : 'offline'
  }
  if (status === 'running') {
    // HIVE-1690: lifecycle says running; Deployment readiness (via backend
    // health_summary issues) must agree before we assert Alive.
    if (hasUnprovenRunningRuntime(effectiveAgent)) return 'needs_help'
    return 'alive'
  }
  if (status === 'failed' || status === 'blocked') return 'needs_help'
  if (status === 'provisioning' || status === 'deleting') return 'changing'
  return 'unknown'
}

// Zero is an idle snapshot, not an efficiency score. Returning null lets the UI
// omit the misleading "0 tok/s" badge while retaining positive live output.
export function liveTokenRate(rate) {
  const value = Number(rate?.words_per_sec)
  return Number.isFinite(value) && value > 0 ? value : null
}
