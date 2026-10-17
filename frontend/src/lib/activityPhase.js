// Live operator-facing activity for Agents cards.
// Coarse activity_state stays idle|working for older runtimes. Fine
// activity_phase (thinking / responding / tool / idle) is what we animate.

export const CANONICAL_PHASES = ['idle', 'thinking', 'responding', 'tool']

const PRODUCTIVE = new Set([
  'thinking',
  'responding',
  'tool',
  'working',
  'busy',
  'running_turn',
  'running-tool',
  'running_tool',
])

const ALIASES = {
  waiting_first_model_event: 'thinking',
  running: 'working',
  'running-tool': 'tool',
  running_tool: 'tool',
  busy: 'working',
  running_turn: 'working',
  sleeping: 'idle',
}

const LEASE_IDLE_WASTE_MIN_MS = 30_000

function norm(value) {
  return String(value || '').trim().toLowerCase().replace(/\s+/g, '_')
}

export function normalizeActivityPhase(value) {
  const raw = ALIASES[norm(value)] || norm(value)
  if (!raw) return null
  if (CANONICAL_PHASES.includes(raw) || raw === 'working') return raw
  if (PRODUCTIVE.has(raw)) return 'working'
  if (raw === 'idle') return 'idle'
  return null
}

export function resolveActivityPhase(agent) {
  const phase = normalizeActivityPhase(agent?.activity_phase)
  if (CANONICAL_PHASES.includes(phase)) return phase
  const state = normalizeActivityPhase(agent?.activity_state)
  if (CANONICAL_PHASES.includes(state)) return state
  if (state === 'working' || PRODUCTIVE.has(state)) {
    return agent?.activity_tool_name ? 'tool' : 'working'
  }
  if (state === 'idle') return 'idle'
  return state
}

export function isProductivePhase(phase) {
  return PRODUCTIVE.has(phase) || phase === 'thinking' || phase === 'responding' || phase === 'tool'
}

export function isProductiveActivity(agent) {
  return isProductivePhase(resolveActivityPhase(agent))
}

export function activityIdleMs(agent) {
  const phaseMs = Number(agent?.activity_phase_for_ms)
  if (Number.isFinite(phaseMs) && phaseMs >= 0) return phaseMs
  const age = Number(agent?.last_activity_ms_ago)
  if (Number.isFinite(age) && age >= 0) return age
  return null
}

export function isLeaseIdleWaste(agent, minIdleMs = LEASE_IDLE_WASTE_MIN_MS) {
  if (agent?.model_lease?.state !== 'holder') return false
  if (String(agent?.status || '').toLowerCase() !== 'running') return false
  if (resolveActivityPhase(agent) !== 'idle') return false
  const idleMs = activityIdleMs(agent)
  return idleMs != null && idleMs >= minIdleMs
}

export function agentHasLiveActivity(agent) {
  if (String(agent?.status || '').toLowerCase() !== 'running') return false
  return isProductiveActivity(agent)
}

export function formatPhaseDuration(ms) {
  if (ms == null || !Number.isFinite(ms) || ms < 1000) return ''
  const s = Math.floor(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return s % 60 ? `${m}m ${s % 60}s` : `${m}m`
  const h = Math.floor(m / 60)
  return m % 60 ? `${h}h ${m % 60}m` : `${h}h`
}

export function activityLabel(phase, { toolName, leaseIdleWaste } = {}) {
  if (leaseIdleWaste) return 'Idle · wasting lease'
  if (phase === 'thinking') return 'Thinking'
  if (phase === 'responding') return 'Responding'
  if (phase === 'tool') return toolName ? `Tool · ${toolName}` : 'Running tool'
  if (phase === 'working') return 'Working'
  if (phase === 'idle') return 'Idle'
  return null
}
