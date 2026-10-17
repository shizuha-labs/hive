import { useMemo, useState } from 'react'
import {
  Activity,
  AlertTriangle,
  Briefcase,
  Cpu,
  Layers,
  LayoutGrid,
  List,
  Lock,
  PauseCircle,
  Plus,
  SlidersHorizontal,
  Users,
  X,
} from 'lucide-react'
import { useFleetAgents, usePulseOrgSnapshot, useFleetEfficiency, useDaemonIdentityHealth, useTeams } from '../hooks/useAgents'
import { useHive } from '../contexts/HiveContext'
import AgentTable, { toRow } from '../components/AgentTable'
import AgentDrawer from '../components/AgentDrawer'
import FleetAgentCard from '../components/FleetAgentCard'
import { isSharedServiceIdentity } from '../lib/serviceIdentity'
import ProvisionAgentModal from '../components/ProvisionAgentModal'
import ContextBudgetPanel from '../components/ContextBudgetPanel'
import WorkEfficiencyPanel from '../components/WorkEfficiencyPanel'
import HarnessVersionsPanel from '../components/HarnessVersionsPanel'
import Toolbar from '../components/Toolbar'
import { ErrorState, EmptyState } from '../components/States'
import { humanize } from '../lib/format'
import {
  buildTeamGroups,
  contextMatchesTeamFilter,
  teamFilterOptions,
  teamKeyOf,
  teamPlacementsOf,
  teamSeedMatchesFilter,
} from '../lib/fleetGrouping'
import { agentHealthVerdict, derivePulseMeta, modelLabel } from '../lib/fleetDegradation'
import { isLeaseIdleWaste, resolveActivityPhase } from '../lib/activityPhase'
import { buildTeamWorkloadLookup } from '../lib/fleetTeamWorkload'

const STATUS_FILTERS = ['all', 'running', 'hibernated', 'provisioning', 'offline', 'stopped', 'failed', 'blocked', 'pending']
const WORKLOAD_FILTERS = [
  { value: 'all', label: 'Any workload' },
  { value: 'loaded', label: 'Loaded' },
  { value: 'backlog', label: 'Backlog pressure' },
  { value: 'blocked', label: 'Blocked work' },
  { value: 'idle', label: 'Idle' },
]
const BACKLOG_PRESSURE_THRESHOLD = 25
const DENSITIES = [
  { value: 'comfortable', label: 'Comfort' },
  { value: 'compact', label: 'Compact' },
  { value: 'dense', label: 'Dense' },
]

export function drawerSelection(agent) {
  const id = agent?.id
  if (!id) return null
  if (agent.kind === 'claw' || agent.runtime_source === 'claw' || String(id).startsWith('claw:')) {
    // HIVE-1520: the clicked claw card already holds meaningful state — carry
    // it into the drawer so opening is immediate; the detail request only
    // deepens it (same contract as the fleet branch below).
    return { kind: 'claw', id: String(id).replace(/^claw:/, ''), snapshot: agent }
  }
  // The card already holds a useful, live-ish projection. Carry it into the
  // drawer so opening is immediate; the detail request only deepens it.
  return { kind: 'fleet', id, snapshot: agent }
}

// (Removed ROLE_TEAM_HINTS — role/profile must NOT define team membership; teams
// come solely from the admin/Pulse roster. See inferredTeams. Split-brain fix.)

function agentEmail(agent) {
  return `${agent.agent_username || ''}@shizuha.com`.toLowerCase()
}

const AGENT_EMAIL_DOMAINS = ['shizuha.com', 'agents.shizuha.io']

/** Pulse assigns to canonical ID emails (`sara@agents.shizuha.io`); Hive cards
 * historically keyed only `username@shizuha.com`. Sara 2026-09-16: card 0/0/0
 * while Pulse held CON-450 + 4 open on the agents.io identity. */
export function agentEmailAliases(agent) {
  const aliases = new Set()
  const username = String(agent?.agent_username || '').trim().toLowerCase()
  if (username) {
    for (const domain of AGENT_EMAIL_DOMAINS) aliases.add(`${username}@${domain}`)
  }
  for (const raw of [
    agent?.email,
    agent?.canonical_email,
    agent?.identity_health?.canonical_email,
    agent?.identity_health?.runtime_email,
  ]) {
    const email = String(raw || '').trim().toLowerCase()
    if (email.includes('@')) aliases.add(email)
  }
  return [...aliases]
}

function sumWorkloadRows(left, right) {
  if (!left) return right || null
  if (!right) return left
  const out = { ...left }
  for (const bucket of Object.keys(right)) {
    out[bucket] = Number(out[bucket] || 0) + Number(right[bucket] || 0)
  }
  return out
}

export function lookupAgentWorkload(workloadMap, agent) {
  if (!workloadMap || !agent) return null
  let merged = null
  for (const email of agentEmailAliases(agent)) {
    merged = sumWorkloadRows(merged, workloadMap[email] || null)
  }
  const idKey = agent.agent_id != null ? String(agent.agent_id) : ''
  if (idKey) merged = sumWorkloadRows(merged, workloadMap[idKey] || null)
  return merged
}


function activeWorkload(workload) {
  if (!workload) return 0
  return (workload.open || 0) + (workload.in_progress || 0) + (workload.in_review || 0) + (workload.blocked || 0)
}

function workloadScore(agent, workload) {
  const status = (agent.status || '').toLowerCase()
  let score = 0
  if (status === 'failed' || status === 'blocked') score += 1000
  if (status === 'provisioning' || status === 'deleting') score += 700
  if (status === 'running') score += 100
  if (workload) {
    score += (workload.blocked || 0) * 20
    score += (workload.overdue || 0) * 15
    score += (workload.in_progress || 0) * 4
    score += (workload.in_review || 0) * 3
    score += workload.open || 0
    score += Math.min(workload.backlog || 0, 100) * 0.5
  }
  return score
}

function healthFor(agent, availabilityStatus) {
  return agentHealthVerdict(agent, availabilityStatus)
}

function agentDisplayName(agent) {
  return agent.display_name || agent.agent_username || 'Agent'
}

function teamKey(team) {
  return teamKeyOf(team)
}

function mergeTeamSeeds(...lists) {
  const byKey = new Map()
  for (const list of lists) {
    for (const raw of list || []) {
      const seed = typeof raw === 'object' ? raw : teamPlacementSeed(raw)
      if (!seed?.teamKey) continue
      const key = `${seed.orgSlug || ''}::${seed.teamKey}`
      if (!byKey.has(key)) byKey.set(key, seed)
    }
  }
  return Array.from(byKey.values()).sort((a, b) => {
    const aTitle = `${a.orgName || a.orgSlug || ''} ${a.teamDisplay || a.teamKey}`.trim()
    const bTitle = `${b.orgName || b.orgSlug || ''} ${b.teamDisplay || b.teamKey}`.trim()
    return aTitle.localeCompare(bTitle)
  })
}

function teamName(team) {
  return team.slug || team.key || team.name || team.assignment_group || team.id
}

function teamOrgSlug(team) {
  return String(team.organization_slug || team.org_slug || team.orgSlug || '').trim().toLowerCase()
}

function teamOrgName(team) {
  return team.organization_name || team.org_name || team.orgName || ''
}

function teamPlacementSeed(team, orgNameBySlug = {}) {
  const key = teamKey(teamName(team))
  if (!key) return null
  const orgSlug = teamOrgSlug(team)
  return {
    teamKey: key,
    teamDisplay: team.display_name || team.name || humanize(key),
    orgSlug,
    orgName: teamOrgName(team) || (orgSlug ? (orgNameBySlug[orgSlug] || '') : ''),
  }
}

function groupWorkloadLookupKey(group) {
  if (group.orgSlug && group.teamKey) return `${group.orgSlug}::${group.teamKey}`
  return group.teamKey || group.key
}

function groupWorkloadLookupKeys(group) {
  return [
    groupWorkloadLookupKey(group),
    group.teamKey,
    group.key,
  ].filter(Boolean).map(teamKey)
}

/**
 * Per-bucket floor: the result is never smaller than `base` in any bucket.
 *
 * Operator 2026-08-05: "the card and the teams headers should never
 * under-report". A view that shows FEWER items than the agent actually owns is
 * worse than one that shows too many — it reads as "nothing to do" and hides
 * real work.
 */
export function mergeNeverBelow(scoped, base) {
  if (!base) return scoped || null
  if (!scoped) return base
  const out = { ...scoped }
  for (const bucket of Object.keys(base)) {
    const b = Number(base[bucket] || 0)
    const s = Number(out[bucket] || 0)
    if (b > s) out[bucket] = b
  }
  return out
}

export function workloadForTeamCard(ctx, group, workloadByAssigneeTeam) {
  if (!group || !workloadByAssigneeTeam) return ctx.workload
  const identities = [
    ...agentEmailAliases(ctx.agent),
    ctx.agent.agent_id ? String(ctx.agent.agent_id) : '',
    ctx.node?.user_id ? String(ctx.node.user_id) : '',
  ].filter(Boolean)
  for (const identity of identities) {
    for (const team of groupWorkloadLookupKeys(group)) {
      const scoped = workloadByAssigneeTeam[`${String(identity).toLowerCase()}::${team}`]
      // The scoped bucket is keyed `<email>::<assignment_group>`, so it holds
      // only the agent's tasks whose assignment_group equals THIS card's team.
      // A task assigned to the agent with a different — or missing —
      // assignment_group is silently dropped, even though the agent owns it.
      //
      // banto, 2026-08-05: email-keyed truth was open=1 blocked=2 overdue=2;
      // `banto@shizuha.com::accounting` was open=0 blocked=0 overdue=0. The
      // card read "0 open" while Pulse held BKSD-10 (todo, high) plus BKSD-8
      // and BKSD-9 (blocked) assigned to banto. The hibernation sweep reads the
      // email-keyed totals, so it correctly kept the seat while the card said
      // there was nothing to do — the two disagreed on screen.
      //
      // Floor the team slice at the agent's own totals so a card can never
      // show less work than the agent actually has.
      if (scoped) return mergeNeverBelow(scoped, ctx.workload)
    }
  }
  // No scoped entry at all previously returned null, i.e. an EMPTY card for an
  // agent that may own plenty of work. Fall back to the agent's real totals.
  return ctx.workload || null
}

export function workloadForTeamHeader(group, teamWorkloadByName, memberWorkloads) {
  // team_workload is keyed by assignment_group, so it misses work assigned to a
  // member under a different (or empty) group — the same drop that emptied the
  // cards. The header must never read idle while its own cards show work, so
  // floor it at the per-bucket sum of its members.
  //
  // Flooring is safe in the other direction too: the header legitimately
  // EXCEEDS the member sum, because it also counts the team's UNASSIGNED queue.
  // A floor only ever raises it toward truth.
  const memberTotal = sumWorkloads(memberWorkloads)
  for (const key of groupWorkloadLookupKeys(group)) {
    const load = teamWorkloadByName?.[key]
    if (load) return mergeNeverBelow(load, memberTotal)
  }
  return memberTotal
}

function sumWorkloads(workloads) {
  if (!workloads || !workloads.length) return null
  const out = {}
  for (const load of workloads) {
    if (!load) continue
    for (const bucket of Object.keys(load)) {
      out[bucket] = Number(out[bucket] || 0) + Number(load[bucket] || 0)
    }
  }
  return Object.keys(out).length ? out : null
}

function inferredTeams(agent) {
  // BYO agents are grouped under External/BYO (separate runtime, not org-team-routed).
  if (agent.runtime_source === 'byo') return ['external-byo']
  // SINGLE SOURCE OF TRUTH: team membership comes SOLELY from Pulse TeamRoster.
  // NOT from the agent's role/profile, NOT from a runtime cohort. Role &
  // profile describe an agent's SKILLS/capabilities — what it *can* do — never
  // which team it belongs to. Inferring team from role caused the split brain
  // (ichi, a Devops "DevOps Engineer", matched /engineer/ and showed under
  // Engineering too). An agent the roster places nowhere is 'unassigned' — fix the
  // roster, never guess. The intended invariant is one primary team per agent;
  // if Pulse still returns multiple teams, render exactly that so drift is visible.
  const sourceTeams = agent.team_names || (agent.team_memberships || []).map((tm) => tm.team_name)
  const out = new Set((sourceTeams || []).filter(Boolean).map(teamKey))
  return Array.from(out).sort()
}

function optionList(contexts, pick, label = humanize) {
  const counts = new Map()
  for (const ctx of contexts) {
    const values = [].concat(pick(ctx) || []).filter(Boolean)
    for (const value of values) {
      const key = String(value)
      counts.set(key, (counts.get(key) || 0) + 1)
    }
  }
  return Array.from(counts.entries())
    .sort((a, b) => label(a[0]).localeCompare(label(b[0])))
    .map(([value, count]) => ({ value, label: label(value), count }))
}

function FilterSelect({ label, value, onChange, options }) {
  return (
    <label className="flex min-w-[150px] flex-col gap-1 text-[11px] font-semibold uppercase text-gray-500 dark:text-gray-400">
      {label}
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="h-9 rounded-lg border border-gray-300 bg-white px-2 text-sm normal-case text-gray-900 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100"
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}{option.count != null ? ` (${option.count})` : ''}
          </option>
        ))}
      </select>
    </label>
  )
}

function Stat({ icon: Icon, label, value, tone = 'gray', hint, active, onClick }) {
  // Glassy, uniform stat tile (HIVE-617): every card is the same hv-panel; the
  // semantic tone lives ONLY in a thin top-accent bar + the icon chip, so the
  // four read as a consistent set instead of four differently-bordered boxes.
  const accent = {
    green: { bar: 'from-transparent via-emerald-400 to-transparent', chip: 'text-emerald-300 bg-emerald-500/[0.14]' },
    amber: { bar: 'from-transparent via-amber-400 to-transparent', chip: 'text-amber-300 bg-amber-500/[0.14]' },
    red: { bar: 'from-transparent via-rose-400 to-transparent', chip: 'text-rose-300 bg-rose-500/[0.14]' },
    blue: { bar: 'from-transparent via-primary-400 to-transparent', chip: 'text-primary-300 bg-primary-500/[0.16]' },
    gray: { bar: 'from-transparent via-slate-500/60 to-transparent', chip: 'text-gray-400 bg-white/[0.06]' },
  }[tone] || {}
  return (
    <button
      type="button"
      onClick={onClick}
      className={`hv-panel hv-panel-hover relative overflow-hidden px-4 py-3.5 text-left ${active ? 'ring-2 ring-primary-500/70' : ''}`}
    >
      <span className={`pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r ${accent.bar}`} />
      <div className="flex items-center gap-2">
        <span className={`grid h-6 w-6 shrink-0 place-items-center rounded-md ${accent.chip}`}>
          <Icon className="h-3.5 w-3.5" />
        </span>
        <span className="hv-kicker truncate">{label}</span>
      </div>
      <div className="mt-2 flex items-end justify-between gap-2">
        <div className="hv-stat-number leading-none">{value}</div>
        {hint && <div className="truncate pb-0.5 font-mono text-[11px] text-gray-500">{hint}</div>}
      </div>
    </button>
  )
}

function groupTitle(groupBy, key) {
  if (groupBy === 'none') return 'All agents'
  if (!key) return 'Unassigned'
  if (key === 'external-byo') return 'Remote'
  return groupBy === 'model' || groupBy === 'runtime' ? key : humanize(key)
}

// Team bottleneck signal from team-assigned Pulse tasks (NOT summed agent load —
// tasks are assigned to a team/assignment_group, and agents are multi-team).
// `active` = open+in_progress+in_review = the team's working queue (the real
// pile-up signal); blocked = waiting on deps; scheduled = future work that must
// remain visible without falsely keeping agents warm; done = recent throughput.
function teamLoadOf(w) {
  if (!w) return null
  const active = (w.open || 0) + (w.in_progress || 0) + (w.in_review || 0)
  const blocked = w.blocked || 0
  const scheduled = w.scheduled || 0
  const backlog = w.backlog || 0
  const done = w.completed || 0
  let tone = 'gray'
  let label = 'light'
  if (active >= 25 || backlog >= 75) { tone = 'red'; label = 'bottleneck' }
  else if (backlog >= BACKLOG_PRESSURE_THRESHOLD) { tone = 'amber'; label = 'backlog' }
  else if (active >= 10) { tone = 'amber'; label = 'busy' }
  else if (active > 0) { tone = 'green'; label = 'flowing' }
  else {
    tone = 'gray'
    label = blocked ? 'blocked-only' : scheduled ? 'scheduled' : 'idle'
  }
  return { active, blocked, scheduled, backlog, done, tone, label }
}

function TeamLoadChip({ load }) {
  if (!load) return null
  const tones = {
    red: 'bg-red-100 text-red-700 ring-red-300 dark:bg-red-900/30 dark:text-red-300 dark:ring-red-800',
    amber: 'bg-amber-100 text-amber-700 ring-amber-300 dark:bg-amber-900/30 dark:text-amber-300 dark:ring-amber-800',
    green: 'bg-green-100 text-green-700 ring-green-300 dark:bg-green-900/30 dark:text-green-300 dark:ring-green-800',
    gray: 'bg-gray-100 text-gray-600 ring-gray-300 dark:bg-gray-700 dark:text-gray-300 dark:ring-gray-600',
  }
  return (
    <span className="flex items-center gap-2 text-[11px]">
      <span className={`rounded-full px-2 py-0.5 font-semibold uppercase ring-1 ${tones[load.tone]}`}>{load.label}</span>
      <span className="font-mono text-gray-600 dark:text-gray-300" title="live team-assigned Pulse workload">
        {load.active} active · {load.blocked} blocked · {load.scheduled} scheduled · {load.backlog} backlog
      </span>
    </span>
  )
}

// ── Daemon identity-health banner (HIVE-254 P5) ─────────────────────────────
// Shows a compact warning when any connected daemon has unresolved INV-ID
// violations. Collapses to nothing when all daemons report identity_ok=true.
function DaemonIdentityBanner({ daemons }) {
  const unhealthy = daemons.filter((d) => !d.identity_ok)
  if (!unhealthy.length) return null

  const total = unhealthy.reduce((n, d) => n + d.violations.length, 0)

  return (
    <div className="rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 dark:border-amber-700 dark:bg-amber-950/40">
      <div className="flex items-start gap-3">
        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400" />
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-amber-800 dark:text-amber-300">
            Identity violations detected — {total} unresolved rejection{total !== 1 ? 's' : ''} across{' '}
            {unhealthy.length} daemon{unhealthy.length !== 1 ? 's' : ''}
          </p>
          <ul className="mt-1 space-y-1">
            {unhealthy.flatMap((d) =>
              d.violations.map((v) => (
                <li key={`${d.daemon_id}/${v.child_id}`} className="text-xs text-amber-700 dark:text-amber-400">
                  <span className="font-mono">[{d.daemon_id}]</span>{' '}
                  {v.username || v.child_id} — {v.reason}
                </li>
              ))
            )}
          </ul>
        </div>
      </div>
    </div>
  )
}


function FleetDegradedBanner({ meta }) {
  const degraded = meta?.rosterDegraded || meta?.pulseDegraded
  if (!degraded) return null
  const age = meta.rosterStaleAgeSeconds ?? meta.pulseStaleAgeSeconds
  const ageLabel = age != null ? `${Math.max(0, Math.round(age))}s old` : 'age unknown'
  // CON-176: cold failure (no stale payload at all) gets its own honest wording —
  // there is nothing last-known-good to show, so workload reads as unavailable.
  const unavailable = meta?.pulseUnavailable
  return (
    <div data-testid="fleet-degraded-banner" className="rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 dark:border-amber-700 dark:bg-amber-950/40">
      <div className="flex items-start gap-3">
        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400" />
        <div>
          <p className="text-sm font-medium text-amber-800 dark:text-amber-300">
            {unavailable
              ? 'Workload/team snapshot is unavailable — counters are hidden until Pulse recovers.'
              : `Team roster/workload data is degraded — showing last-known-good data (${ageLabel}).`}
          </p>
          <p className="mt-1 text-xs text-amber-700 dark:text-amber-400">
            {unavailable
              ? 'The Pulse snapshot fetch failed before any last-known-good copy existed; Hive will not paint agents as idle/Unassigned from missing data.'
              : 'A Pulse/Admin fetch failed; Hive is serving stale data rather than silently marking agents Unassigned or idle.'}
          </p>
        </div>
      </div>
    </div>
  )
}

export default function Fleet({ embedded = false }) {
  const { isStaff, canReadFleet, currentOrgSlug, organizations } = useHive()
  const hasFleetVantage = Boolean(isStaff || canReadFleet)
  const ownerOnly = !hasFleetVantage
  const { data, isLoading, isError, isFetching, refetch } = useFleetAgents({
    ownerOnly,
    fleetRead: hasFleetVantage && !isStaff,
    orgSlug: currentOrgSlug,
  })
  // An absent scoped snapshot is unknown; a successful [] is measured empty.
  // React Query keeps same-key snapshots during refresh/transient failure.
  const hasAgentSnapshot = Array.isArray(data)
  const snapshotHint = hasAgentSnapshot ? undefined : (isError ? 'Unavailable' : 'Loading')
  const pulse = usePulseOrgSnapshot({ enabled: hasFleetVantage && !ownerOnly, orgSlug: currentOrgSlug })
  // HIVE-617: team STRUCTURE is member-visible — a non-staff member sees the
  // teams of the org(s) they belong to (backend scopes to their verified orgs),
  // so the full org chart (incl. empty teams) renders, not just their own agents.
  // The operational snapshot above stays staff-only.
  const teams = useTeams({ enabled: true, orgSlug: currentOrgSlug })
  const orgNameBySlug = useMemo(() => {
    const out = {}
    for (const org of organizations || []) {
      const slug = String(org?.slug || '').trim().toLowerCase()
      if (!slug) continue
      out[slug] = org.display_name || org.name || org.slug || slug
    }
    return out
  }, [organizations])
  const dataMeta = {
    ...(data?._hiveMeta || {}),
    // CON-176: a cold snapshot failure (query error before any stale payload
    // exists) must trip the degraded banner too — never silent zero workload.
    ...derivePulseMeta(pulse),
  }
  const efficiency = useFleetEfficiency({ enabled: isStaff && !ownerOnly })
  const daemonHealth = useDaemonIdentityHealth({ enabled: isStaff && !ownerOnly })
  const [query, setQuery] = useState('')
  const [status, setStatus] = useState('all')
  const [filters, setFilters] = useState({
    health: 'all',
    model: 'all',
    role: 'all',
    team: 'all',
    runtime: 'all',
    workload: 'all',
  })
  const [view, setView] = useState('cards')
  const [density, setDensity] = useState('compact')
  // P1 (design critique 2026-07-11): the two always-open filter rows pushed every
  // agent below the fold. Collapsed by default; opens automatically when any
  // filter is active so state is never hidden.
  const [filtersOpen, setFiltersOpen] = useState(false)
  const [groupBy, setGroupBy] = useState('team')
  // HIVE-600: ~30 of ~40 teams are empty and dominated the page as dead space.
  // Collapse them by default; a toggle reveals them on demand.
  const [showEmptyTeams, setShowEmptyTeams] = useState(false)
  const [selected, setSelected] = useState(null)
  const [provisionOpen, setProvisionOpen] = useState(false)
  // Context-budget breakdown is a diagnostic detail, not needed in the default
  // view (operator 2026-06-27) — collapsed by default, revealed on demand.
  const [showContextBudget, setShowContextBudget] = useState(false)

  const setFilter = (key, value) => setFilters((prev) => ({ ...prev, [key]: value }))
  const resetFilters = () => {
    setQuery('')
    setStatus('all')
    setFilters({ health: 'all', model: 'all', role: 'all', team: 'all', runtime: 'all', workload: 'all' })
  }

  const pulseLookups = useMemo(() => {
    const snapshot = pulse.data || {}
    const rosterTeams = teams.data || []
    const sourceTeams = [...(snapshot.teams || []), ...rosterTeams]
    // Prefer live membership org display names when the org directory is sparse.
    const nameBySlug = { ...orgNameBySlug }
    for (const team of sourceTeams) {
      const slug = teamOrgSlug(team)
      const name = teamOrgName(team)
      if (slug && name) nameBySlug[slug] = name
    }
    const nodesByEmail = new Map()
    const nodesById = new Map()
    for (const node of snapshot.nodes || []) {
      if (node.email) nodesByEmail.set(node.email.toLowerCase(), node)
      if (node.user_id != null) nodesById.set(String(node.user_id), node)
    }
    return {
      nodesByEmail,
      nodesById,
      orgNameBySlug: nameBySlug,
      workload: snapshot.workload || {},
      // Top-level availability map (email -> available/unavailable/unknown).
      // Authoritative even when admin rosters/nodes are empty.
      availability: snapshot.availability || {},
      // All teams that exist in the org (admin/Pulse), lowercased — so the
      // fleet view can render EVERY team as a group, including empty ones
      // (e.g. Merge, Design) that currently have no agent grouped under them.
      allTeamNames: mergeTeamSeeds(
        rosterTeams.map((t) => teamPlacementSeed(t, nameBySlug)).filter(Boolean),
        (snapshot.teams || []).map((t) => teamPlacementSeed(t, nameBySlug)).filter(Boolean),
      ),
      // Per-team load (tasks by assignment_group) keyed by lowercased team name,
      // for the bottleneck chip in each team header.
      teamWorkloadByName: buildTeamWorkloadLookup(snapshot.team_workload || {}, sourceTeams),
      workloadByAssigneeTeam: snapshot.workload_by_assignee_team || snapshot.workload_by_email_team || null,
    }
  }, [pulse.data, teams.data, orgNameBySlug])

  const contexts = useMemo(() => {
    const nameCounts = new Map()
    // HIVE-412: count how many cards resolve to each Pulse identity so a shared
    // service account (one email backing multiple runtimes) can be detected.
    const emailCounts = new Map()
    // Learn friendly org names from authoritative Pulse memberships so cards
    // render a human organization label without consulting another placement source.
    const membershipOrgNames = { ...pulseLookups.orgNameBySlug }
    for (const agent of data || []) {
      const key = agentDisplayName(agent).trim().toLowerCase()
      nameCounts.set(key, (nameCounts.get(key) || 0) + 1)
      const em = agentEmail(agent)
      if (em && em !== '@shizuha.com') emailCounts.set(em, (emailCounts.get(em) || 0) + 1)
      for (const m of agent.team_memberships || []) {
        const slug = String(m?.organization_slug || m?.org_slug || '').trim().toLowerCase()
        const oname = m?.organization_name || m?.org_name || ''
        if (slug && oname) membershipOrgNames[slug] = oname
      }
    }

    return (data || []).map((rawAgent) => {
      const name = agentDisplayName(rawAgent)
      const duplicateName = (nameCounts.get(name.trim().toLowerCase()) || 0) > 1
      const agent = duplicateName
        ? { ...rawAgent, display_name: `${name} (@${rawAgent.agent_username})` }
        : rawAgent
      const email = agentEmail(agent)
      const idKey = agent.agent_id ? String(agent.agent_id) : ''
      const aliases = agentEmailAliases(agent)
      const node = aliases.map((item) => pulseLookups.nodesByEmail.get(item)).find(Boolean)
        || pulseLookups.nodesById.get(idKey)
      const workload = lookupAgentWorkload(pulseLookups.workload, agent)
      const teamNames = inferredTeams(agent)
      // Prefer the top-level availability map (covers all agents); fall back to a
      // node's availability_status if present.
      const availStatus = aliases.map((item) => pulseLookups.availability[item]).find(Boolean)
        || node?.availability_status
      // HIVE-412: if this card's Pulse identity is a shared SERVICE account, the
      // workload is NOT this person's personal queue. Do not let it drive the
      // card's personal active/loaded/score signals (which feed sorting and the
      // "bottleneck" read); the card badges it as a shared service identity so a
      // human never misreads a service-account pile as one engineer's workload.
      const sharedServiceIdentity = isSharedServiceIdentity(email, emailCounts)
      const active = sharedServiceIdentity ? 0 : activeWorkload(workload)
      const backlog = sharedServiceIdentity ? 0 : (workload?.backlog || 0)
      const row = { ...toRow(agent, 'fleet'), _raw: agent }
      const model = modelLabel(agent)
      const runtime = agent.runtime_harness || 'Runtime'
      const role = agent.display_title || agent.role || 'Agent'
      const teamRoleTerms = (agent.team_roles_summary || []).flatMap((item) => [
        item.role_title,
        item.membership_role,
        item.team_slug,
      ])
      const blocked = sharedServiceIdentity ? 0 : (workload?.blocked || 0)
      return {
        agent: { ...agent, availability_status: availStatus },
        row,
        node,
        workload,
        sharedServiceIdentity,
        serviceIdentityEmail: sharedServiceIdentity ? email : '',
        // Score used for sort/bottleneck ordering: a shared service pile scores 0
        // so it can never sort a person's card to the top as "most loaded".
        score: sharedServiceIdentity ? 0 : workloadScore(agent, workload),
        teamNames,
        // Pulse-authored `team_memberships` is the complete placement answer.
        // Snapshot nodes, team seeds, and lead metadata remain presentation and
        // workload inputs only; they cannot create membership.
        teamPlacements: teamPlacementsOf(agent, teamNames, {
          orgNameBySlug: membershipOrgNames,
        }),
        active,
        backlog,
        health: healthFor(agent, availStatus),
        backlogPressure: backlog >= BACKLOG_PRESSURE_THRESHOLD,
        loaded: active >= 10 || blocked > 0 || backlog >= BACKLOG_PRESSURE_THRESHOLD,
        model,
        runtime,
        role,
        teamRoleTerms,
      }
    })
  }, [data, pulseLookups])

  const filteredContexts = useMemo(() => {
    let list = contexts
    if (status !== 'all') list = list.filter((ctx) => (ctx.agent.status || '').toLowerCase() === status)
    if (filters.health !== 'all') list = list.filter((ctx) => ctx.health === filters.health)
    if (filters.model !== 'all') list = list.filter((ctx) => ctx.model === filters.model)
    if (filters.role !== 'all') list = list.filter((ctx) => ctx.role === filters.role)
    if (filters.runtime !== 'all') list = list.filter((ctx) => ctx.runtime === filters.runtime)
    if (filters.team !== 'all') list = list.filter((ctx) => contextMatchesTeamFilter(ctx, filters.team))
    if (filters.workload !== 'all') {
      list = list.filter((ctx) => {
        if (filters.workload === 'loaded') return ctx.loaded
        if (filters.workload === 'backlog') return ctx.backlogPressure
        if (filters.workload === 'blocked') return !ctx.sharedServiceIdentity && (ctx.workload?.blocked || 0) > 0
        if (filters.workload === 'idle') return ctx.active === 0
        return true
      })
    }
    const q = query.trim().toLowerCase()
    if (q) {
      list = list.filter((ctx) => {
        const image = ctx.agent.effective_image || ctx.agent.image || ''
        return [
          ctx.row.name,
          ctx.row.handle,
          ctx.role,
          ctx.model,
          ctx.runtime,
          ctx.agent.model_provider,
          image,
          ...ctx.teamRoleTerms,
          ...ctx.teamNames,
        ].some((value) => String(value || '').toLowerCase().includes(q))
      })
    }
    return [...list].sort((a, b) => {
      return (b.score - a.score) || a.row.name.localeCompare(b.row.name)
    })
  }, [contexts, query, status, filters])

  const stats = useMemo(() => {
    const activity = { thinking: 0, responding: 0, tool: 0, working: 0, idle: 0, waste: 0 }
    for (const ctx of contexts) {
      if (String(ctx.agent?.status || '').toLowerCase() !== 'running') continue
      if (isLeaseIdleWaste(ctx.agent)) activity.waste += 1
      const phase = resolveActivityPhase(ctx.agent)
      if (phase && Object.prototype.hasOwnProperty.call(activity, phase)) {
        activity[phase] += 1
      }
    }
    return {
      running: contexts.filter((ctx) => ctx.health === 'alive').length,
      hibernated: contexts.filter((ctx) => (ctx.agent.status || '').toLowerCase() === 'hibernated').length,
      attention: contexts.filter((ctx) => ctx.health === 'needs_help').length,
      heavy: contexts.filter((ctx) => ctx.loaded).length,
      backlog: contexts.filter((ctx) => ctx.backlogPressure).length,
      total: contexts.length,
      activity,
    }
  }, [contexts])

  const filterOptions = useMemo(() => {
    return {
      models: [{ value: 'all', label: 'Any model' }, ...optionList(contexts, (ctx) => ctx.model, (v) => v)],
      roles: [{ value: 'all', label: 'Any role' }, ...optionList(contexts, (ctx) => ctx.role)],
      teams: [{ value: 'all', label: 'Any team' }, ...teamFilterOptions(contexts, pulseLookups.allTeamNames)],
      runtimes: [{ value: 'all', label: 'Any runtime' }, ...optionList(contexts, (ctx) => ctx.runtime, (v) => v)],
      workloads: WORKLOAD_FILTERS,
    }
  }, [contexts, pulseLookups.allTeamNames])

  const teamGroupSeeds = useMemo(() => {
    const seeds = pulseLookups.allTeamNames || []
    if (filters.team === 'all') return seeds
    return seeds.filter((seed) => teamSeedMatchesFilter(seed, filters.team))
  }, [filters.team, pulseLookups.allTeamNames])

  const groups = useMemo(() => {
    if (groupBy === 'none') return [{ key: 'all', title: 'All agents', contexts: filteredContexts }]
    if (groupBy === 'team') {
      // HIVE-443: group by (org, team) when "All Organizations" is selected so
      // same-named teams across orgs don't collapse — header "<Org> › <Team>".
      // A single-org view keeps the plain team header and the empty-team seeding
      // (full org structure, incl. unstaffed teams like Merge/Design).
      //
      // HIVE-1299: the line that used to sit here — "Each agent appears under at
      // most one authoritative placement" — was false and is removed rather than
      // reworded. buildTeamGroups() pushes a context into EVERY team it belongs
      // to, on purpose. The stale comment described the invariant the reader
      // expects instead of the one the code has, so the 55-vs-57 mismatch read as
      // a data bug for days before anyone traced the renderer.
      return buildTeamGroups({
        contexts: filteredContexts,
        allTeamNames: teamGroupSeeds,
        allOrgs: !currentOrgSlug,
        groupTitleFn: (key) => groupTitle('team', key),
      })
    }
    const pick = {
      model: (ctx) => ctx.model,
      runtime: (ctx) => ctx.runtime,
      status: (ctx) => ctx.agent.status || 'unknown',
      role: (ctx) => ctx.role,
    }[groupBy]
    const byKey = new Map()
    for (const ctx of filteredContexts) {
      const key = pick ? pick(ctx) : 'all'
      if (!byKey.has(key)) byKey.set(key, [])
      byKey.get(key).push(ctx)
    }
    return Array.from(byKey.entries())
      .sort(([a], [b]) => groupTitle(groupBy, a).localeCompare(groupTitle(groupBy, b)))
      .map(([key, groupContexts]) => ({ key, title: groupTitle(groupBy, key), contexts: groupContexts }))
  }, [filteredContexts, groupBy, teamGroupSeeds, currentOrgSlug])

  // HIVE-600: hide empty teams by default (only relevant when grouping by team).
  const emptyTeamCount = useMemo(
    () => (groupBy === 'team' ? groups.filter((g) => g.contexts.length === 0).length : 0),
    [groups, groupBy],
  )
  const visibleGroups = useMemo(() => {
    if (groupBy !== 'team' || showEmptyTeams) return groups
    return groups.filter((g) => g.contexts.length > 0)
  }, [groups, groupBy, showEmptyTeams])

  // HIVE-1299: team grouping renders one card per PLACEMENT, and a multi-team
  // agent is deliberately placed under each of its teams (see the regression
  // "places a multi-team agent under each of its teams"). So the rendered card
  // population is not the unique-agent population: a 55-agent roster with two
  // dual-team agents draws 57 cards. The page used to report only
  // "Showing 55 of 55 agents" next to those 57 cards, so it contradicted itself.
  //
  // Derive this by summing what is actually rendered rather than recomputing
  // placements from the contexts. A count computed independently of the render
  // can drift from it — which is the whole defect — so the number quoted to the
  // user comes from the same array the cards come from.
  const placementCount = useMemo(
    () => visibleGroups.reduce((sum, group) => sum + group.contexts.length, 0),
    [visibleGroups],
  )
  // Only worth showing when the two populations actually differ; on a roster
  // with no multi-team agents the extra clause would be noise.
  const showPlacementCount = groupBy === 'team' && placementCount !== filteredContexts.length

  const gridClass = {
    comfortable: 'grid gap-4 xl:grid-cols-2 2xl:grid-cols-3',
    compact: 'grid gap-3 md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4',
    dense: 'grid gap-2 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5',
  }[density]

  const hasFilters = query || status !== 'all' || Object.values(filters).some((value) => value !== 'all')
  const hasRenderableContent = groupBy === 'team' ? groups.length > 0 : filteredContexts.length > 0

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        {!embedded && (
          <div>
            <h1 className="flex items-center gap-2 text-2xl font-bold">
              <Cpu className="h-6 w-6 text-amber-500" />
              Agents
            </h1>
            <p className="text-gray-500 dark:text-gray-400">
              Every agent owned by this account, grouped by placement.
            </p>
          </div>
        )}
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-5 lg:min-w-[650px]">
          <Stat
            icon={Activity}
            label="healthy"
            value={hasAgentSnapshot ? `${stats.running}/${stats.total}` : '—'}
            hint={snapshotHint}
            tone="green"
            active={filters.health === 'alive'}
            onClick={() => setFilter('health', filters.health === 'alive' ? 'all' : 'alive')}
          />
          <Stat
            icon={PauseCircle}
            label="hibernated"
            value={hasAgentSnapshot ? stats.hibernated : '—'}
            hint={snapshotHint}
            tone="blue"
            active={status === 'hibernated'}
            onClick={() => setStatus(status === 'hibernated' ? 'all' : 'hibernated')}
          />
          <Stat
            icon={AlertTriangle}
            label="needs help"
            value={hasAgentSnapshot ? stats.attention : '—'}
            hint={snapshotHint}
            tone={stats.attention ? 'red' : 'gray'}
            active={filters.health === 'needs_help'}
            onClick={() => setFilter('health', filters.health === 'needs_help' ? 'all' : 'needs_help')}
          />
          <Stat
            icon={Briefcase}
            label="loaded"
            value={hasAgentSnapshot ? stats.heavy : '—'}
            hint={snapshotHint}
            tone={stats.heavy ? 'amber' : 'gray'}
            active={filters.workload === 'loaded'}
            onClick={() => setFilter('workload', filters.workload === 'loaded' ? 'all' : 'loaded')}
          />
          <Stat
            icon={Layers}
            label="backlog pressure"
            value={hasAgentSnapshot ? stats.backlog : '—'}
            hint={snapshotHint}
            tone={stats.backlog ? 'amber' : 'gray'}
            active={filters.workload === 'backlog'}
            onClick={() => setFilter('workload', filters.workload === 'backlog' ? 'all' : 'backlog')}
          />
        </div>
      </div>
      <div
        data-testid="activity-mix"
        className="flex flex-wrap items-center gap-2 text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400"
      >
        <span className="normal-case tracking-normal text-gray-400">Doing now</span>
        {!hasAgentSnapshot && <span className="normal-case tracking-normal">{isError ? 'Agent activity unavailable' : 'Loading agent activity…'}</span>}
        {hasAgentSnapshot && [
          ['thinking', stats.activity.thinking, 'text-violet-600 dark:text-violet-300'],
          ['responding', stats.activity.responding, 'text-emerald-600 dark:text-emerald-300'],
          ['tool', stats.activity.tool, 'text-amber-600 dark:text-amber-300'],
          ['working', stats.activity.working, 'text-emerald-700 dark:text-emerald-200'],
          ['idle', stats.activity.idle, 'text-gray-600 dark:text-gray-300'],
        ].map(([label, value, color]) => (
          <span key={label} className={`inline-flex items-center gap-1 rounded-full bg-gray-100 px-2 py-0.5 dark:bg-white/[0.06] ${color}`}>
            <span className="tabular-nums">{value}</span> {label}
          </span>
        ))}
        {stats.activity.waste > 0 && (
          <span className="inline-flex items-center gap-1 rounded-full bg-amber-50 px-2 py-0.5 text-amber-800 ring-1 ring-amber-200 dark:bg-amber-900/30 dark:text-amber-200 dark:ring-amber-800">
            <AlertTriangle className="h-3 w-3" />
            <span className="tabular-nums">{stats.activity.waste}</span> idle leases
          </span>
        )}
      </div>

      <DaemonIdentityBanner daemons={daemonHealth.data ?? []} />
      <FleetDegradedBanner meta={dataMeta} />

      <HarnessVersionsPanel isStaff={isStaff && !ownerOnly} />

      <Toolbar
        query={query}
        onQuery={setQuery}
        status={status}
        onStatus={setStatus}
        statuses={STATUS_FILTERS}
        onRefresh={() => refetch()}
        refreshing={isFetching || pulse.isFetching}
        right={
          <div className="flex items-center gap-2">
            <div className="inline-flex rounded-lg border border-gray-300 bg-white p-0.5 dark:border-gray-600 dark:bg-gray-800">
              <button
                type="button"
                title="Card view"
                aria-label="Card view"
                onClick={() => setView('cards')}
                className={`inline-flex h-8 w-8 items-center justify-center rounded-md ${view === 'cards' ? 'bg-primary-600 text-white' : 'text-gray-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-700'}`}
              >
                <LayoutGrid className="h-4 w-4" />
              </button>
              <button
                type="button"
                title="Dense table"
                aria-label="Dense table"
                onClick={() => setView('table')}
                className={`inline-flex h-8 w-8 items-center justify-center rounded-md ${view === 'table' ? 'bg-primary-600 text-white' : 'text-gray-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-700'}`}
              >
                <List className="h-4 w-4" />
              </button>
            </div>
            {isStaff && (
              <button
                onClick={() => setProvisionOpen(true)}
                className="inline-flex items-center gap-1.5 rounded-lg bg-gradient-to-b from-primary-500 to-primary-600 px-3 py-2 text-sm font-semibold text-white shadow-[0_10px_24px_-10px_rgba(79,70,229,0.7)] ring-1 ring-inset ring-white/15 transition-transform hover:-translate-y-px hover:from-primary-400 hover:to-primary-500"
              >
                <Plus className="h-4 w-4" />
                <span className="hidden sm:inline">New agent</span>
              </button>
            )}
          </div>
        }
      />

      <div className="space-y-3 border-y border-gray-200/70 py-2.5 dark:border-white/[0.06]">
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => setFiltersOpen((v) => !v)}
            className={`inline-flex h-8 items-center gap-1.5 rounded-lg border px-3 text-xs font-medium transition-colors ${filtersOpen || hasFilters ? 'border-amber-400/50 bg-amber-400/10 text-amber-600 dark:text-amber-300' : 'border-gray-300 text-gray-600 hover:bg-gray-100 dark:border-white/10 dark:text-gray-300 dark:hover:bg-white/[0.05]'}`}
          >
            <SlidersHorizontal className="h-3.5 w-3.5" />
            Filters{hasFilters ? ' · on' : ''}
          </button>
          <span className="text-xs text-gray-500 dark:text-gray-400">
            {!hasAgentSnapshot ? (isError ? 'Agent counts unavailable' : 'Loading agents…') : <>
            Showing {filteredContexts.length} of {contexts.length} unique {filteredContexts.length === 1 ? 'agent' : 'agents'}
            {showPlacementCount && ` · ${placementCount} team placement${placementCount === 1 ? '' : 's'}`}
            {groupBy === 'team' && ` · ${visibleGroups.length} of ${groups.length} team${visibleGroups.length === 1 ? '' : 's'}`}
            {groupBy === 'team' && emptyTeamCount > 0 && (
              <>
                {' · '}
                <button
                  type="button"
                  onClick={() => setShowEmptyTeams((v) => !v)}
                  className="ml-1 rounded px-1 py-0.5 font-medium text-amber-600 hover:bg-amber-50 dark:text-amber-400 dark:hover:bg-amber-900/20"
                >
                  {showEmptyTeams ? `hide ${emptyTeamCount} empty team${emptyTeamCount === 1 ? '' : 's'}` : `show ${emptyTeamCount} empty team${emptyTeamCount === 1 ? '' : 's'}`}
                </button>
              </>
            )}
            </>}
          </span>
        </div>
        {(filtersOpen || hasFilters) && (
        <div className="flex flex-wrap items-end gap-2">
          <FilterSelect label="Model" value={filters.model} onChange={(value) => setFilter('model', value)} options={filterOptions.models} />
          <FilterSelect label="Runtime" value={filters.runtime} onChange={(value) => setFilter('runtime', value)} options={filterOptions.runtimes} />
          <FilterSelect label="Team" value={filters.team} onChange={(value) => setFilter('team', value)} options={filterOptions.teams} />
          <FilterSelect label="Role" value={filters.role} onChange={(value) => setFilter('role', value)} options={filterOptions.roles} />
          <FilterSelect label="Workload" value={filters.workload} onChange={(value) => setFilter('workload', value)} options={filterOptions.workloads} />
          <FilterSelect
            label="Group by"
            value={groupBy}
            onChange={setGroupBy}
            options={[
              { value: 'team', label: 'Team' },
              { value: 'model', label: 'Model' },
              { value: 'runtime', label: 'Runtime' },
              { value: 'status', label: 'Status' },
              { value: 'role', label: 'Role' },
              { value: 'none', label: 'None' },
            ]}
          />
          <div className="flex min-w-[190px] flex-col gap-1 text-[11px] font-semibold uppercase text-gray-500 dark:text-gray-400">
            Density
            <div className="inline-flex h-9 rounded-lg border border-gray-300 bg-white p-0.5 dark:border-gray-600 dark:bg-gray-800">
              {DENSITIES.map((item) => (
                <button
                  key={item.value}
                  type="button"
                  onClick={() => setDensity(item.value)}
                  className={`rounded-md px-2.5 text-xs normal-case ${density === item.value ? 'bg-primary-600 text-white' : 'text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-700'}`}
                >
                  {item.label}
                </button>
              ))}
            </div>
          </div>
          {hasFilters && (
            <button
              type="button"
              onClick={resetFilters}
              className="inline-flex h-9 items-center gap-1.5 rounded-lg border border-gray-300 px-3 text-sm text-gray-600 hover:bg-gray-100 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800"
            >
              <X className="h-4 w-4" />
              Clear
            </button>
          )}
        </div>
        )}
        <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-gray-500 dark:text-gray-400">
          <span />
          {groupBy === 'team' && (
            <span className="inline-flex items-center gap-1.5">
              <Users className="h-3.5 w-3.5" />
              Teams are synced from your organization roster.
            </span>
          )}
        </div>
      </div>

      <WorkEfficiencyPanel contexts={filteredContexts} onSelect={(agent) => setSelected(drawerSelection(agent))} />
      <div>
        <button
          type="button"
          onClick={() => setShowContextBudget((v) => !v)}
          className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-gray-300 px-3 text-xs text-gray-500 hover:bg-gray-100 dark:border-gray-600 dark:text-gray-400 dark:hover:bg-gray-800"
          aria-expanded={showContextBudget}
        >
          <SlidersHorizontal className="h-3.5 w-3.5" />
          {showContextBudget ? 'Hide context budget' : 'Show context budget'}
        </button>
        {showContextBudget && (
          <div className="mt-3">
            <ContextBudgetPanel contexts={filteredContexts} onSelect={(agent) => setSelected(drawerSelection(agent))} />
          </div>
        )}
      </div>

      {isError && !data ? (
        <ErrorState
          message="Could not load agents. The HIVE API may be unreachable."
          onRetry={() => refetch()}
        />
      ) : !hasRenderableContent && !isLoading ? (
        hasFilters ? (
          <EmptyState icon={Cpu} title="No matching agents" message="Try a different filter set." />
        ) : (
          <EmptyState
            icon={isStaff ? Cpu : Lock}
            title="No agents yet"
            message={
              isStaff
                ? 'Provision your first agent to get started.'
                : 'Agents visible to this account will appear here.'
            }
            action={
              isStaff && (
                <button
                  onClick={() => setProvisionOpen(true)}
                  className="inline-flex items-center gap-1.5 rounded-lg bg-primary-600 px-4 py-2 text-sm text-white hover:bg-primary-700"
                >
                  <Plus className="h-4 w-4" /> Provision agent
                </button>
              )
            }
          />
        )
      ) : view === 'table' ? (
        <AgentTable
          rows={filteredContexts.map((ctx) => ctx.row)}
          loading={isLoading}
          onSelect={(row) => setSelected(drawerSelection(row._raw || row))}
        />
      ) : (
        <div className="space-y-5">
          {isLoading
            ? [
                <div key="loading" className={gridClass}>
                  {Array.from({ length: density === 'dense' ? 10 : 6 }).map((_, idx) => (
                    <div
                      key={idx}
                      className={`rounded-lg border border-gray-200 bg-white p-4 shadow-sm dark:border-gray-700 dark:bg-gray-800 ${density === 'dense' ? 'min-h-[190px]' : 'min-h-[260px]'}`}
                    >
                      <div className="h-full animate-pulse space-y-4">
                        <div className="flex gap-3">
                          <div className="h-10 w-10 rounded-lg bg-gray-200 dark:bg-gray-700" />
                          <div className="flex-1 space-y-2">
                            <div className="h-5 w-1/2 rounded bg-gray-200 dark:bg-gray-700" />
                            <div className="h-4 w-1/3 rounded bg-gray-200 dark:bg-gray-700" />
                          </div>
                        </div>
                        <div className="h-16 rounded-lg bg-gray-200 dark:bg-gray-700" />
                        <div className="h-12 rounded-lg bg-gray-200 dark:bg-gray-700" />
                      </div>
                    </div>
                  ))}
                </div>,
              ]
            : visibleGroups.map((group) => (
                <section key={group.key} className="space-y-3">
                  {groupBy !== 'none' && (
                    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-gray-200 pb-2 dark:border-gray-700">
                      <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-600 dark:text-gray-300">
                        {group.title}
                      </h2>
                      <div className="flex items-center gap-3">
                        {groupBy === 'team' && <TeamLoadChip load={teamLoadOf(workloadForTeamHeader(
                          group,
                          pulseLookups.teamWorkloadByName,
                          // The header floors at its own cards' totals, so it can
                          // never read idle while the cards below it show work.
                          group.contexts.map((c) => workloadForTeamCard(c, group, pulseLookups.workloadByAssigneeTeam)),
                        ))} />}
                        {/* HIVE-1299: under team grouping a section holds placements,
                            not distinct agents — the same agent legitimately appears
                            in two sections. Saying "agents" here double-counts it. */}
                        <span className="text-xs text-gray-500 dark:text-gray-400">
                          {group.contexts.length} {groupBy === 'team'
                            ? (group.contexts.length === 1 ? 'placement' : 'placements')
                            : (group.contexts.length === 1 ? 'agent' : 'agents')}
                        </span>
                        {(() => {
                          const holders = group.contexts.filter((c) => c.agent?.model_lease?.state === 'holder')
                          const idleHolders = holders.filter((c) => isLeaseIdleWaste(c.agent))
                          if (!idleHolders.length) return null
                          return (
                            <span
                              data-testid="team-lease-idle-waste"
                              className="inline-flex items-center gap-1 rounded-full bg-amber-50 px-2 py-0.5 text-[11px] font-semibold text-amber-800 ring-1 ring-amber-200 dark:bg-amber-900/30 dark:text-amber-200 dark:ring-amber-800"
                            >
                              <AlertTriangle className="h-3 w-3" />
                              {idleHolders.length}/{holders.length} holders idle
                            </span>
                          )
                        })()}
                      </div>
                    </div>
                  )}
                  <div className={gridClass}>
                    {group.contexts.map((ctx) => (
                      <FleetAgentCard
                        key={ctx.agent.id}
                        agent={ctx.agent}
                        workload={groupBy === 'team' ? workloadForTeamCard(ctx, group, pulseLookups.workloadByAssigneeTeam) : ctx.workload}
                        workloadUnknown={dataMeta.pulseUnavailable}
                        sharedServiceIdentity={ctx.sharedServiceIdentity}
                        serviceIdentityEmail={ctx.serviceIdentityEmail}
                        pulseNode={ctx.node}
                        efficiency={(efficiency.data || {})[agentEmail(ctx.agent)] ?? null}
                        teamNames={ctx.teamNames}
                        density={density}
                        onSelect={(agent) => setSelected(drawerSelection(agent))}
                      />
                    ))}
                  </div>
                </section>
              ))}
        </div>
      )}

      <AgentDrawer
        kind={selected?.kind || 'fleet'}
        id={selected?.id}
        initialAgent={selected?.snapshot}
        onClose={() => setSelected(null)}
      />
      <ProvisionAgentModal
        open={provisionOpen}
        onClose={(created) => {
          setProvisionOpen(false)
          if (created) refetch()
        }}
      />
    </div>
  )
}
