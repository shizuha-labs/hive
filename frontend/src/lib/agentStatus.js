/**
 * Agent status classification for the Hive overview (HIVE-510).
 *
 * Single source of truth for which agent states count as "needs attention"
 * vs. "idle/healthy". A **Stopped** agent is intentionally off — a deliberate,
 * healthy state — so it must NOT be flagged as needing intervention (the bug:
 * the overview counted Stopped alongside Failed/Blocked).
 */

/** States that genuinely need human intervention. */
export const ATTENTION_STATUSES = ['failed', 'blocked', 'error']

/**
 * Not-actively-running but healthy/deliberate states. `stopped` belongs here
 * (intentionally off), alongside the not-yet-running states.
 */
export const IDLE_STATUSES = ['pending', 'offline', 'provisioning', 'hibernated', 'stopped']

export function normalizeStatus(status) {
  return (status || '').toLowerCase()
}

/** True when the agent is in a genuinely problematic state needing intervention. */
export function isNeedsAttention(status) {
  return ATTENTION_STATUSES.includes(normalizeStatus(status))
}

/** True when the agent is intentionally not running (idle/stopped) — healthy. */
export function isIdle(status) {
  return IDLE_STATUSES.includes(normalizeStatus(status))
}

/** True when the agent is actively running. */
export function isRunning(status) {
  return normalizeStatus(status) === 'running'
}

const K8S_CONTROL_PLANE_NOISE = [
  'you must be logged in to the server',
  'the server has asked for the client to provide credentials',
  "couldn't get current server api group list",
  'k8s reconcile start failed',
  'cluster api rejected fleet-daemon credentials',
  'cluster api unreachable from fleet daemon',
]

const PARKED_SEAT_NOISE = [
  'runtime-owner park',
  'plat-8181',
  'phantom shizuhaagentdown',
  'fleetagent row descoped',
]

/** Hide host kubectl dumps — they are not the agent's fault (PLAT-5276). */
export function operatorFacingStatusMessage(message) {
  const text = String(message || '')
  if (!text) return ''
  const low = text.toLowerCase()
  if (K8S_CONTROL_PLANE_NOISE.some((needle) => low.includes(needle))) return ''
  if (PARKED_SEAT_NOISE.some((needle) => low.includes(needle))) return ''
  return text
}
