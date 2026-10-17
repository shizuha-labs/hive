import { teamKeyOf } from './fleetGrouping.js'

function teamKey(team) {
  return teamKeyOf(team)
}

function teamName(team) {
  return team.slug || team.key || team.name || team.assignment_group || team.id
}

function teamOrgSlug(team) {
  return String(team.organization_slug || team.org_slug || team.orgSlug || '').trim().toLowerCase()
}

const BUCKETS = ['open', 'in_progress', 'blocked', 'in_review', 'scheduled', 'completed', 'overdue', 'backlog']

/** All-zero counts are a truthy OBJECT, so they win `||` and `.find(Boolean)`. */
function isEmptyWorkload(workload) {
  if (!workload) return true
  return BUCKETS.every((bucket) => !Number(workload[bucket] || 0))
}

function workloadKeysForTeam(team) {
  const key = teamKey(teamName(team))
  if (!key) return { identity: [], shared: '' }
  const orgSlug = teamOrgSlug(team)
  const idKey = team.id != null ? String(team.id) : ''
  // IDENTITY keys name exactly THIS team: the org-qualified form and its own id
  // (roster rows often use the plain team slug as their `id`). Their value is
  // authoritative for this team even when it is legitimately all zeros.
  //
  // The bare team key is SHARED by every same-named team across every org
  // ("engineering" exists in shizuha, shizuha-digital, shizuha-trading and two
  // QA-isolation orgs), so it is only ever a weak fallback — never authoritative.
  //
  // Roster rows set `id` to the team SLUG, so their idKey IS the shared key —
  // it must not be promoted to identity, or those rows write the shared key
  // unconditionally and re-introduce the zeroing bug.
  const ownId = idKey && teamKey(idKey) !== key ? idKey : ''
  return {
    identity: [orgSlug ? `${orgSlug}::${key}` : '', ownId].filter(Boolean),
    shared: key,
    orgScoped: Boolean(orgSlug),
  }
}

export function buildTeamWorkloadLookup(teamWorkload = {}, teams = []) {
  const out = {}
  const zero = { open: 0, in_progress: 0, blocked: 0, in_review: 0, scheduled: 0, completed: 0, overdue: 0, backlog: 0 }

  const read = (key) => {
    const k = teamKey(key)
    return teamWorkload[key] || teamWorkload[k] || out[k]
  }

  for (const [key, value] of Object.entries(teamWorkload)) {
    out[teamKey(key)] = value
  }

  for (const team of teams) {
    const { identity, shared, orgScoped } = workloadKeysForTeam(team)
    if (!shared) continue

    // Resolve from THIS team's own identity. An org-scoped team whose identity
    // names no workload genuinely has none — borrowing the shared key there
    // would paint one org's numbers onto another's same-named team. Only
    // legacy org-less rows may fall back to the ambiguous shared key.
    const workload = identity.map(read).find(Boolean) || (orgScoped ? null : read(shared)) || zero

    for (const key of identity) out[teamKey(key)] = workload

    // HIVE / operator 2026-07-27: never let one org's idle same-named team zero
    // out another org's real aggregate on the SHARED key. Engineering showed
    // "idle · 0 active · 0 blocked" in the team header while its own member
    // cards showed 14/40/20, 6/71/9, 6/5/1 — because shizuha's real counts
    // (open 267, in_progress 135, blocked 148) were overwritten here by a
    // zero-workload Engineering from another org, and the header falls back to
    // this shared key whenever its group carries no org slug. A zero may only
    // fill a key that is still empty, never replace real work.
    const sharedKey = teamKey(shared)
    if (!isEmptyWorkload(workload) || isEmptyWorkload(out[sharedKey])) {
      out[sharedKey] = workload
    }
  }
  return out
}
