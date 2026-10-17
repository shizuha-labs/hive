import { describe, expect, it } from 'vitest'
import {
  agentHealthVerdict,
  agentRuntimeHealth,
  derivePulseMeta,
  hasFreshProductiveTelemetry,
  hasStaleRuntimeTelemetry,
  hasUnprovenRunningRuntime,
  RUNTIME_TELEMETRY_STALE_MAX_AGE_S,
  liveTokenRate,
  modelLabel,
  modelSourceLabel,
} from './fleetDegradation.js'

describe('CON-176 derivePulseMeta', () => {
  it('cold snapshot failure (isError, no data) is degraded AND unavailable', () => {
    const meta = derivePulseMeta({ isError: true, data: undefined })
    expect(meta.pulseDegraded).toBe(true)
    expect(meta.pulseUnavailable).toBe(true)
    expect(meta.pulseStaleAgeSeconds).toBeNull()
  })

  it('stale-served snapshot (pulse_degraded from backend) is degraded, not unavailable', () => {
    const meta = derivePulseMeta({
      isError: false,
      data: { pulse_degraded: true, pulse_stale_age_seconds: 42, nodes: [] },
    })
    expect(meta.pulseDegraded).toBe(true)
    expect(meta.pulseUnavailable).toBe(false)
    expect(meta.pulseStaleAgeSeconds).toBe(42)
  })

  it('error while react-query still holds cached data serves that data, not unavailable', () => {
    const meta = derivePulseMeta({ isError: true, data: { nodes: [] } })
    expect(meta.pulseUnavailable).toBe(false)
    expect(meta.pulseDegraded).toBe(false)
  })

  it('healthy snapshot is neither degraded nor unavailable', () => {
    const meta = derivePulseMeta({ isError: false, data: { nodes: [] } })
    expect(meta.pulseDegraded).toBe(false)
    expect(meta.pulseUnavailable).toBe(false)
  })
})

describe('CON-176/SCLI-199 model labels', () => {
  it('no concrete model renders Unknown, never a runtime-default assertion', () => {
    expect(modelLabel({ effective_model: '', model: '', model_source: 'unknown' })).toBe('Unknown')
    expect(modelLabel({})).toBe('Unknown')
    expect(modelSourceLabel({ effective_model: '', model: '', model_source: 'unknown' })).toBe('unknown')
    expect(modelSourceLabel({})).toBe('unknown')
  })

  it('concrete models still render as before', () => {
    expect(modelLabel({ effective_model: 'deepseek-v3' })).toBe('deepseek-v3')
    expect(modelLabel({ model: 'gpt-5.5' })).toBe('gpt-5.5')
    expect(modelSourceLabel({ model: 'gpt-5.5', model_source: 'pinned' })).toBe('pinned')
    expect(modelSourceLabel({ model_source: 'runtime_daemon' })).toBe('runtime daemon')
  })
})

describe('fleet runtime health classification', () => {
  it('treats thinking/responding/tool as fresh productive work', () => {
    expect(hasFreshProductiveTelemetry({
      activity_phase: 'thinking',
      activity_state: 'working',
      health_ok: true,
      health_summary: { last_snapshot_age_s: 20 },
    })).toBe(true)
    expect(hasFreshProductiveTelemetry({
      activity_phase: 'responding',
      activity_state: 'working',
      tokens_per_sec: 12,
      health_summary: { last_snapshot_age_s: 10 },
    })).toBe(true)
  })

  it('keeps fresh active work alive after a transient failed turn', () => {
    expect(agentRuntimeHealth({
      health_ok: false,
      provider_unavailable: false,
      health_token_pool_unavailable: false,
      health_consecutive_errors: 1,
      activity_state: 'working',
      health_summary: { last_snapshot_age_s: 45 },
      tokens_per_sec: 54.7,
    })).toEqual({ unavailable: false, degraded: false })
  })

  it('keeps an unresolved idle runtime error visible as degraded', () => {
    expect(agentRuntimeHealth({
      health_ok: false,
      provider_unavailable: false,
      health_token_pool_unavailable: false,
      health_consecutive_errors: 1,
      activity_state: 'idle',
    })).toEqual({ unavailable: false, degraded: true })
  })

  it('never lets productive telemetry hide configured-versus-running model drift', () => {
    expect(agentRuntimeHealth({
      runtime_config_drift: true,
      health_ok: true,
      provider_unavailable: false,
      health_token_pool_unavailable: false,
      activity_state: 'working',
      health_summary: { highest_severity: 'ok', last_snapshot_age_s: 10 },
      tokens_per_sec: 42,
    })).toEqual({ unavailable: false, degraded: true })
  })

  it('reserves unavailable for explicit provider, pool, or Pulse signals', () => {
    expect(agentRuntimeHealth({ provider_unavailable: true, health_ok: false }))
      .toEqual({ unavailable: true, degraded: false })
    expect(agentRuntimeHealth({ health_token_pool_unavailable: true }))
      .toEqual({ unavailable: true, degraded: false })
    expect(agentRuntimeHealth({ availability_status: 'unavailable' }))
      .toEqual({ unavailable: true, degraded: false })
  })

  it('keeps a healthy runtime healthy', () => {
    expect(agentRuntimeHealth({ health_ok: true, provider_unavailable: false }))
      .toEqual({ unavailable: false, degraded: false })
  })

  it('requires an actual fresh timestamp instead of treating null as zero age', () => {
    expect(hasFreshProductiveTelemetry({
      activity_state: 'working',
      health_ok: true,
      health_summary: { last_snapshot_age_s: null },
    })).toBe(false)
  })

  it('lets fresh working telemetry supersede stale Pulse unavailability', () => {
    expect(agentRuntimeHealth({
      availability_status: 'unavailable',
      activity_state: 'working',
      health_ok: true,
      health_summary: { last_snapshot_age_s: 30 },
    })).toEqual({ unavailable: false, degraded: false })
  })

  it('never lets productive telemetry hide explicit provider unavailability', () => {
    expect(agentRuntimeHealth({
      availability_status: 'unavailable',
      activity_state: 'working',
      health_ok: true,
      provider_unavailable: true,
      tokens_per_sec: 20,
      health_summary: { last_snapshot_age_s: 30 },
    })).toEqual({ unavailable: true, degraded: false })
  })
})

describe('fleet full-set user-visible verdicts', () => {
  it('classifies the complete mixed-health set with current telemetry precedence', () => {
    const agents = [
      {
        agent_username: 'kai',
        status: 'running',
        needs_help: true,
        activity_state: 'working',
        health_ok: true,
        health_summary: { last_snapshot_age_s: 20 },
      },
      {
        agent_username: 'jun',
        status: 'running',
        needs_help: true,
        activity_state: 'idle',
        health_ok: true,
        health_summary: { last_snapshot_age_s: 20 },
      },
      {
        agent_username: 'nagi',
        status: 'running',
        activity_state: 'working',
        health_ok: true,
        provider_unavailable: true,
        health_summary: { last_snapshot_age_s: 20 },
      },
      {
        agent_username: 'ryo',
        status: 'running',
        activity_state: 'idle',
        health_ok: true,
      },
      { agent_username: 'sara', status: 'failed' },
      { agent_username: 'haru', status: 'stopped' },
      {
        agent_username: 'zen',
        status: 'external',
        activity_state: 'working',
        health_ok: true,
        health_summary: { last_snapshot_age_s: 15 },
      },
    ]
    const pulseAvailability = {
      kai: 'unavailable',
      jun: 'available',
      nagi: 'unavailable',
      ryo: 'available',
      sara: 'available',
      haru: 'available',
      zen: 'unavailable',
    }

    expect(Object.fromEntries(agents.map((agent) => [
      agent.agent_username,
      agentHealthVerdict(agent, pulseAvailability[agent.agent_username]),
    ]))).toEqual({
      kai: 'needs_help',
      jun: 'needs_help',
      nagi: 'unavailable',
      ryo: 'alive',
      sara: 'needs_help',
      haru: 'offline',
      zen: 'alive',
    })
  })
})

describe('HIVE-1599: explicit needs-help is producer-owned for eligible seats', () => {
  const nagi = {
    agent_username: 'nagi',
    status: 'running',
    needs_help: true,
    needs_help_reason: '10 known ready task(s) and 87 heartbeat(s) exposed no progress',
    heartbeat_outcome: 'needs_help',
    heartbeat_ready_task_count: 10,
    heartbeat_progress_event_count: 0,
    activity_state: 'working',
    health_ok: true,
    provider_unavailable: false,
    health_token_pool_unavailable: false,
    availability_status: 'available',
    tokens_per_sec: 42,
    health_summary: { highest_severity: 'ok', issues: [], last_snapshot_age_s: 10 },
  }

  it('keeps the exact healthy/fresh active vector in needs_help', () => {
    expect(agentHealthVerdict(nagi)).toBe('needs_help')
  })

  it('keeps the two-argument Fleet verdict on the same producer truth', () => {
    expect(agentHealthVerdict({ ...nagi, availability_status: undefined }, 'unavailable'))
      .toBe('needs_help')
  })

  it('applies the same precedence to an external seat', () => {
    expect(agentHealthVerdict({ ...nagi, status: 'external' })).toBe('needs_help')
  })

  it('gates intentional lifecycle states before a stale needs-help flag', () => {
    const stopped = { ...nagi, status: 'stopped' }
    expect(agentHealthVerdict(stopped)).toBe('offline')
    expect(agentHealthVerdict({ ...stopped, availability_status: undefined }, 'unavailable'))
      .toBe('offline')
  })

  it('clears only when a later producer payload clears the flag', () => {
    expect(agentHealthVerdict({
      ...nagi,
      needs_help: false,
      heartbeat_outcome: 'worked_task',
      heartbeat_progress_event_count: 1,
    })).toBe('alive')
  })

  it('treats an idle CEO Office empty-queue projection as alive', () => {
    // Operator 2026-08-15: Hina/Aya cards said Needs help —
    // "2 consecutive heartbeat(s) exposed no Pulse queue snapshot" — while
    // intended to sit Running with 0 assigned Pulse work. Hive retracts that
    // false-positive on the wire; the card must then paint Alive.
    expect(agentHealthVerdict({
      agent_username: 'hina',
      status: 'running',
      needs_help: false,
      needs_help_reason: '',
      heartbeat_outcome: 'queue_empty',
      heartbeat_ready_task_count: 0,
      activity_state: 'idle',
      health_ok: true,
      provider_unavailable: false,
      health_token_pool_unavailable: false,
      health_summary: { highest_severity: 'ok', ok: true, issues: [] },
    })).toBe('alive')
  })

  it('preserves provider capacity and HIVE-1288 when no explicit episode exists', () => {
    expect(agentHealthVerdict({ ...nagi, needs_help: false, provider_unavailable: true }))
      .toBe('unavailable')
    expect(agentHealthVerdict({ ...nagi, status: 'failed', needs_help: false }))
      .toBe('needs_help')
  })
})

describe('live token-rate display truth', () => {
  it('omits idle/invalid rates and preserves positive live output', () => {
    expect(liveTokenRate({ words_per_sec: 0 })).toBeNull()
    expect(liveTokenRate({ words_per_sec: '0' })).toBeNull()
    expect(liveTokenRate({ words_per_sec: null })).toBeNull()
    expect(liveTokenRate({ words_per_sec: 12.5 })).toBe(12.5)
  })
})

describe('HIVE-1288: a failed agent with a non-capacity error is needs_help, not unavailable', () => {
  // Live repro (zen, 2026-07-21 22:55 UTC, shizuha.com/hive/agents): @fumi
  // rendered Failed + "attempt to write a readonly database" + UNAVAILABLE,
  // /hive/overview counted 1 needing attention, and the Agents summary tile
  // read NEEDS HELP = 0. The summary counts `health === 'needs_help'`, so the
  // verdict returning 'unavailable' first is what zeroed the tile.
  const fumi = {
    agent_username: 'fumi',
    status: 'failed',
    status_message: 'attempt to write a readonly database',
    availability_status: 'unavailable',
    last_active_at: '2026-06-18T03:50:12.412035',
  }

  it('counts a stale failed agent as needs_help', () => {
    expect(agentHealthVerdict(fumi)).toBe('needs_help')
  })

  it('counts a failed agent as needs_help even when availability is passed explicitly', () => {
    // Fleet.jsx calls agentHealthVerdict(agent, availStatus) with the Pulse
    // availability as a second argument, so the override path must agree with
    // the card's single-argument call or the two surfaces diverge again.
    expect(agentHealthVerdict({ ...fumi, availability_status: undefined }, 'unavailable'))
      .toBe('needs_help')
  })

  it('CAPACITY unavailability still wins — a token-pool outage is not a fault', () => {
    // The carve-out this fix deliberately preserves. Without it, every agent
    // parked by an exhausted pool would be reported as needing help.
    expect(agentHealthVerdict({ ...fumi, health_token_pool_unavailable: true }))
      .toBe('unavailable')
    expect(agentHealthVerdict({ ...fumi, provider_unavailable: true }))
      .toBe('unavailable')
  })

  it('HIVE-510 stays fixed — a Stopped agent is NOT needs_help', () => {
    // Regression guard in the opposite direction: HIVE-510 fixed Stopped
    // agents being falsely counted. `stopped` is an idle status and must never
    // reach the failed/blocked branch this fix reordered.
    expect(agentHealthVerdict({ ...fumi, status: 'stopped' })).toBe('offline')
  })

  it('NEGATIVE CONTROL: an unavailable agent that is NOT failed stays unavailable', () => {
    // Without this the fix could have been "count unavailable as needs_help",
    // which would satisfy the first assertion and silently reclassify every
    // idle-but-live agent. Only the failed/blocked fault flips.
    expect(agentHealthVerdict({ ...fumi, status: 'running' })).toBe('unavailable')
  })
})

// HIVE-1690 — Deployment readiness issues must refute asserted `running`.
//
// Binding v1.1: UI mirrors backend typed issues (runtime_unavailable /
// runtime_health_unknown), never a last_active_at age threshold.
describe('HIVE-1690 unproven running runtime downgrades an asserted-running row', () => {
  const mikaFixture = {
    agent_username: 'claw:1d176064-4499-4ee6-a107-5c6e03a3fb35',
    status: 'running',
    health_ok: true,
    needs_help: false,
    availability_status: 'available',
    health_summary: {
      ok: false,
      highest_severity: 'error',
      issue_types: ['runtime_unavailable'],
      issues: [{ type: 'runtime_unavailable', severity: 'error', service: 'runtime' }],
      last_snapshot_age_s: 30 * 24 * 60 * 60,
    },
  }

  it('the exact reported class of fixture is no longer ALIVE', () => {
    expect(agentHealthVerdict(mikaFixture)).toBe('needs_help')
  })

  it('a ready running agent is still ALIVE', () => {
    expect(agentHealthVerdict({
      ...mikaFixture,
      health_summary: { ok: true, highest_severity: 'ok', issue_types: [], last_snapshot_age_s: 30 },
    })).toBe('alive')
  })

  it('month-old last_snapshot_age alone does NOT fault when readiness is proven', () => {
    // Binding: no last_active_at / age threshold.
    expect(agentHealthVerdict({
      ...mikaFixture,
      health_summary: { ok: true, highest_severity: 'ok', issue_types: [], last_snapshot_age_s: 30 * 24 * 60 * 60 },
    })).toBe('alive')
  })

  it('missing readiness issues does not manufacture a fault', () => {
    expect(agentHealthVerdict({ ...mikaFixture, health_summary: { ok: true } })).toBe('alive')
    expect(agentHealthVerdict({ ...mikaFixture, health_summary: {} })).toBe('alive')
  })

  it('lifecycle-idle statuses are unaffected', () => {
    for (const status of ['stopped', 'hibernated', 'offline', 'pending']) {
      expect(agentHealthVerdict({ ...mikaFixture, status })).toBe('offline')
    }
  })

  it('hasUnprovenRunningRuntime is the boundary', () => {
    expect(hasUnprovenRunningRuntime(mikaFixture)).toBe(true)
    expect(hasUnprovenRunningRuntime({
      status: 'running',
      health_summary: { ok: true, issue_types: [] },
    })).toBe(false)
    expect(hasUnprovenRunningRuntime({
      status: 'running',
      health_summary: { ok: false, highest_severity: 'error', issue_types: ['runtime_health_unknown'] },
    })).toBe(true)
  })
})
