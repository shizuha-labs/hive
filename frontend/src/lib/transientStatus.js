/**
 * Optimistic transitional agent states — the "game loop" feel (operator
 * 2026-07-11): clicking Stop must IMMEDIATELY show "Stopping…" (GCloud-VM
 * style), not keep saying "running" until the backend roster catches up.
 *
 * A transient is set the moment a lifecycle mutation fires and stays STICKY
 * across poll refetches (which would otherwise overwrite it with the stale
 * backend status) until either:
 *   - the backend reports one of the expected terminal statuses, or
 *   - the TTL expires (safety valve so a failed action can't wedge the UI).
 *
 * Pure module-level store: no context/provider needed; the data hooks map
 * agents through `applyTransient` on every (re)fetch, and mutations paint the
 * query cache immediately, so every surface (list, cards, drawer, overview)
 * shows the transitional state within one render.
 */

const TTL_MS = 120_000

/** agentId -> { status, done: Set<string>, since: number, minMs: number } */
const store = new Map()

function prune() {
  const now = Date.now()
  for (const [id, t] of store) {
    if (now - t.since > TTL_MS) store.delete(id)
  }
}

/**
 * Mark an agent as transitioning.
 * @param {string} agentId
 * @param {'stopping'|'starting'|'restarting'} status
 * @param {string[]} doneStatuses backend statuses that complete the transition
 * @param {number} minMs minimum time to show the transition even if the backend
 *   already reports a done status (restart: running -> running would otherwise
 *   clear instantly and show nothing happened)
 */
export function setTransient(agentId, status, doneStatuses, minMs = 0) {
  store.set(String(agentId), {
    status,
    done: new Set(doneStatuses.map((s) => s.toLowerCase())),
    since: Date.now(),
    minMs,
  })
}

export function clearTransient(agentId) {
  store.delete(String(agentId))
}

/** Effective status for an agent object (backend status + transient overlay). */
export function transientStatusFor(agent) {
  if (!agent) return null
  const t = store.get(String(agent.id))
  if (!t) return null
  const backend = (agent.status || '').toLowerCase()
  const age = Date.now() - t.since
  if (age > TTL_MS) {
    store.delete(String(agent.id))
    return null
  }
  if (t.done.has(backend) && age >= t.minMs) {
    store.delete(String(agent.id))
    return null
  }
  return t.status
}

/** Map an agent through the transient overlay (returns a new object only when
 *  a transition is active, so referential equality is preserved otherwise). */
export function applyTransient(agent) {
  const status = transientStatusFor(agent)
  return status ? { ...agent, status } : agent
}

/** True while any agent is mid-transition — used to poll faster (game tick). */
export function hasActiveTransient() {
  prune()
  return store.size > 0
}
