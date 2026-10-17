/**
 * Merge a clicked-card snapshot with the slower detail GET.
 *
 * Detail used to omit Pulse `team_memberships`, so `live || snapshot`
 * replaced a placed card with Unassigned the moment the drawer opened.
 * Detail may deepen fields; it must not wipe placement the card already had.
 */
export function deepenFleetAgent(snapshot, live) {
  if (!live) return snapshot || null
  if (!snapshot) return live
  const liveMemberships = Array.isArray(live.team_memberships) ? live.team_memberships : null
  const snapMemberships = Array.isArray(snapshot.team_memberships) ? snapshot.team_memberships : []
  const memberships = (liveMemberships && liveMemberships.length) ? liveMemberships : snapMemberships
  const liveNames = Array.isArray(live.team_names) ? live.team_names : null
  const snapNames = Array.isArray(snapshot.team_names) ? snapshot.team_names : []
  const names = (liveNames && liveNames.length) ? liveNames : snapNames
  return {
    ...snapshot,
    ...live,
    team_memberships: memberships,
    team_names: names,
  }
}
