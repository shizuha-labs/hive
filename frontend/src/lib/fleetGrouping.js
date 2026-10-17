// HIVE-443: org-aware team grouping for the fleet view (/hive/agents).
//
// Each org on the platform has an INDEPENDENT team structure, and the same team
// NAME legitimately exists in multiple orgs (e.g. two "Accounting" teams, two
// "Review" teams id 7 & 31, two "Security" id 6 & 33). Grouping by team name
// alone collapses different orgs' same-named teams into one visual group, so the
// operator cannot tell which org a member/edit belongs to — dangerous next to the
// org-scoping fixes (HIVE-442/HIVE-445).
//
// This module groups agents by (organization, team) when "All Organizations" is
// selected and labels each group "<Org Name> › <Team>". A single-org view keeps
// the plain team header. Org identity is read from each agent's team_memberships
// (`organization_slug` / `organization_name`), which the org-scoping backend
// supplies. Missing membership data fails closed to Unassigned; team seeds,
// org-chart nodes, and lead labels never infer placement.

import { humanize, teamDisplayLabel } from './format.js'

// Separator for composite (org, team) group keys. '::' can't appear in a slug
// (teamKeyOf/org slugs are [a-z0-9-]), so the key is unambiguous.
const SEP = '::'

// Mirror Fleet.jsx teamKey() so keys line up with the rest of the view.
export function teamKeyOf(team) {
  const key = String(team || '').trim().toLowerCase().replace(/[_\s]+/g, '-')
  return {
    analytics: 'research-analytics',
    research: 'research-analytics',
    researchanalytics: 'research-analytics',
  }[key] || key
}

function applyOrgNameDirectory(placement, orgNameBySlug = {}) {
  if (!placement) return placement
  if (placement.orgName || !placement.orgSlug) return placement
  const name = orgNameBySlug[placement.orgSlug]
  return name ? { ...placement, orgName: name } : placement
}

/**
 * Per-agent team placements, each tagged with its org when known.
 * `team_memberships` is the only placement input. Missing and empty both mean
 * Unassigned; org-chart/Admin relationships and inferred team keys are display
 * metadata only and must never manufacture a second placement answer.
 *
 * @param {object} agent      fleet agent payload
 * @param {string[]} _teamKeys retained for call-site compatibility; ignored
 * @param {object}  [opts]
 * @param {Record<string,string>} [opts.orgNameBySlug] org slug → display name
 * @returns {{orgSlug:string, orgName:string, teamKey:string, teamDisplay:string}[]}
 */
export function teamPlacementsOf(agent, _teamKeys = [], opts = {}) {
  const memberships = Array.isArray(agent?.team_memberships) ? agent.team_memberships : []
  const orgNameBySlug = opts.orgNameBySlug || {}
  const placements = []
  const seen = new Set()

  const pushPlacement = (raw) => {
    let placement = normaliseTeamPlacement(raw)
    if (!placement) return
    placement = applyOrgNameDirectory(placement, orgNameBySlug)
    const dedup = `${placement.orgSlug}${SEP}${placement.teamKey}`
    if (seen.has(dedup)) return
    seen.add(dedup)
    placements.push(placement)
  }

  for (const m of memberships) {
    const key = teamKeyOf(m?.team_slug || m?.team_name || m?.team_id)
    if (!key) continue
    const orgSlug = String(m?.organization_slug || m?.org_slug || '').toLowerCase()
    const orgName = m?.organization_name || m?.org_name || ''
    pushPlacement({
      orgSlug,
      orgName,
      teamKey: key,
      teamDisplay: m?.team_name || humanize(key),
    })
  }

  return placements
}

/**
 * Build the team-grouped sections for the fleet view.
 *
 * @param {object}   opts
 * @param {object[]} opts.contexts       filtered agent contexts (each carries teamPlacements)
 * @param {(string|object)[]} opts.allTeamNames   every team in the org (to render empty groups);
 *                                             objects may carry orgSlug/orgName/teamKey
 * @param {(key:string)=>string} opts.groupTitleFn  plain team-title resolver (humanize/aliases)
 * @returns {{key:string, teamKey:string, orgSlug:string, title:string, contexts:object[]}[]}
 */
function normaliseTeamPlacement(raw) {
  if (typeof raw === 'string' || typeof raw === 'number') {
    const key = teamKeyOf(raw)
    return key ? { orgSlug: '', orgName: '', teamKey: key, teamDisplay: humanize(key) } : null
  }
  if (!raw || typeof raw !== 'object') return null
  const key = teamKeyOf(raw.teamKey || raw.team_slug || raw.teamSlug || raw.team_name || raw.name || raw.slug || raw.id)
  if (!key) return null
  const orgSlug = String(raw.orgSlug || raw.organization_slug || raw.org_slug || '').trim().toLowerCase()
  const orgName = raw.orgName || raw.organization_name || raw.org_name || ''
  return {
    orgSlug,
    orgName,
    teamKey: key,
    teamDisplay: raw.teamDisplay || raw.team_name || raw.display_name || raw.name || humanize(key),
  }
}

/**
 * Stable value used by the Agents team filter. Org-scoped placements use the
 * same composite identity as team groups; legacy orgless payloads retain the
 * plain team slug.
 */
export function teamPlacementKey(raw) {
  const placement = normaliseTeamPlacement(raw)
  if (!placement) return ''
  return placement.orgSlug
    ? `${placement.orgSlug}${SEP}${placement.teamKey}`
    : placement.teamKey
}

/**
 * Build org-distinct filter options and member counts from team placements.
 * Flat teamNames are consulted only when a context has no structured placement
 * data, preserving the legacy payload contract without collapsing org teams.
 */
export function teamFilterOptions(contexts = [], teamSeeds = []) {
  const counts = new Map()
  for (const ctx of contexts) {
    const rawPlacements = ctx.teamPlacements?.length ? ctx.teamPlacements : (ctx.teamNames || [])
    const values = new Set(rawPlacements.map(teamPlacementKey).filter(Boolean))
    for (const value of values) counts.set(value, (counts.get(value) || 0) + 1)
  }

  const byValue = new Map()
  for (const raw of teamSeeds || []) {
    const placement = normaliseTeamPlacement(raw)
    if (!placement) continue
    const value = teamPlacementKey(placement)
    byValue.set(value, {
      value,
      label: placement.orgSlug
        ? teamDisplayLabel(placement, placement.teamDisplay)
        : placement.teamDisplay,
      count: counts.get(value) || 0,
    })
  }

  for (const ctx of contexts) {
    const rawPlacements = ctx.teamPlacements?.length ? ctx.teamPlacements : (ctx.teamNames || [])
    for (const raw of rawPlacements) {
      const placement = normaliseTeamPlacement(raw)
      if (!placement) continue
      const value = teamPlacementKey(placement)
      if (byValue.has(value)) continue
      byValue.set(value, {
        value,
        label: placement.orgSlug
          ? teamDisplayLabel(placement, placement.teamDisplay)
          : placement.teamDisplay,
        count: counts.get(value) || 0,
      })
    }
  }
  return Array.from(byValue.values()).sort((a, b) => a.label.localeCompare(b.label))
}

export function contextMatchesTeamFilter(ctx, value) {
  if (!value || value === 'all') return true
  const rawPlacements = ctx.teamPlacements?.length ? ctx.teamPlacements : (ctx.teamNames || [])
  return rawPlacements.some((raw) => teamPlacementKey(raw) === value)
}

export function teamSeedMatchesFilter(seed, value) {
  return !value || value === 'all' || teamPlacementKey(seed) === value
}

function titleForPlacement(p, orgMode, groupTitleFn) {
  const teamTitle = p.teamDisplay || groupTitleFn(p.teamKey)
  if (orgMode && p.orgSlug) return teamDisplayLabel(p, teamTitle)
  return teamTitle
}

function keyForPlacement(p, orgMode) {
  if (orgMode && p.orgSlug) return `${p.orgSlug}${SEP}${p.teamKey}`
  return p.teamKey
}

export function buildTeamGroups({ contexts = [], allTeamNames = [], groupTitleFn = humanize }) {
  const seedTeams = (allTeamNames || []).map(normaliseTeamPlacement).filter(Boolean)
  // CON-177: prefix with org whenever the backend supplies org context — not only
  // in the explicit All-Organizations scope. Without this, a selected single org
  // still showed bare headers like "Human Ops", which was the operator-visible
  // ambiguity this task fixes. Legacy/orgless payloads remain plain.
  const orgMode = contexts.some((ctx) => (ctx.teamPlacements || []).some((p) => p.orgSlug))
    || seedTeams.some((p) => p.orgSlug)

  const byKey = new Map()

  // Seed empty team groups so the full org structure is visible. If the team seed
  // carries org identity, seed the org-prefixed group; otherwise keep the legacy
  // plain fallback for old Pulse payloads.
  for (const p of seedTeams) {
    // In org-prefixed mode, do not fabricate anonymous empty groups from an
    // org-agnostic team list. Only concrete agent placements may fall back to
    // plain keys; empty orgless seed rows would reintroduce ambiguous headers.
    if (orgMode && !p.orgSlug) continue
    const key = keyForPlacement(p, orgMode)
    if (key && !byKey.has(key)) {
      byKey.set(key, {
        key,
        teamKey: p.teamKey,
        orgSlug: orgMode ? (p.orgSlug || '') : '',
        orgName: orgMode ? (p.orgName || '') : '',
        title: titleForPlacement(p, orgMode, groupTitleFn),
        contexts: [],
      })
    }
  }

  for (const ctx of contexts) {
    const placements = ctx.teamPlacements && ctx.teamPlacements.length
      ? ctx.teamPlacements.map(normaliseTeamPlacement).filter(Boolean)
      : [{ orgSlug: '', orgName: '', teamKey: 'unassigned', teamDisplay: 'Unassigned' }]
    for (const p of placements) {
      const key = keyForPlacement(p, orgMode)
      if (!byKey.has(key)) {
        byKey.set(key, {
          key,
          teamKey: p.teamKey,
          orgSlug: orgMode ? (p.orgSlug || '') : '',
          orgName: orgMode ? (p.orgName || '') : '',
          title: titleForPlacement(p, orgMode, groupTitleFn),
          contexts: [],
        })
      }
      byKey.get(key).contexts.push(ctx)
    }
  }

  // Sort by title so org-prefixed mode clusters each org's teams together
  // ("Digital › Accounting", "Digital › Sales", "Shizuha › Accounting", …).
  return Array.from(byKey.values()).sort((a, b) => a.title.localeCompare(b.title))
}
