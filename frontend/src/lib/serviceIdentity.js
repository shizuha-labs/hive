// HIVE-412: shared service-account identity detection for the fleet view.
//
// Some fleet runtimes authenticate to Pulse as a SHARED service account rather
// than their own personal identity — the codex-bridge legacy uses
// codex@shizuha.com (user 4, the security-ci PULSE token's identity). Their
// Pulse workload is therefore the SERVICE account's queue, not the named
// agent's. Attributing that shared pile to a person's card made "Sara" read as
// 671 doing / an Engineering bottleneck when the items belonged to the codex
// service account.
//
// An identity is "shared" when it is either (a) a known service account, or
// (b) the same Pulse email backs more than one agent card (definitionally, an
// identity behind multiple runtimes is not any one person's personal queue).

export const KNOWN_SERVICE_IDENTITIES = new Set(['codex@shizuha.com'])

/**
 * @param {string} email     lowercased Pulse identity email for a card
 * @param {Map<string,number>} emailCounts  email -> number of cards resolving to it
 * @returns {boolean}
 */
export function isSharedServiceIdentity(email, emailCounts) {
  if (!email) return false
  const normalized = String(email).toLowerCase()
  if (KNOWN_SERVICE_IDENTITIES.has(normalized)) return true
  return (emailCounts?.get(normalized) || 0) > 1
}
