import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import {
  Activity, AlertTriangle, Bot, Boxes, Building2, Clock, Cpu, Gauge,
  Plus, Users, RefreshCw, Sparkles, Workflow, WifiOff,
} from 'lucide-react'
import clsx from 'clsx'
import { useFleetAgents, useFleetEfficiency } from '../hooks/useAgents'
import { useHive } from '../contexts/HiveContext'
import { orgProvisioningApi } from '../services/api'
import AgentDrawer from '../components/AgentDrawer'
import AgentAvatar from '../components/AgentAvatar'
import StatusPill from '../components/StatusPill'
import { ErrorState, LoadingState } from '../components/States'
import { humanize, relativeTime } from '../lib/format'
import { isIdle, isRunning } from '../lib/agentStatus'
import { agentHealthVerdict } from '../lib/fleetDegradation'

// ── Live fleet HUD ──────────────────────────────────────────────────────────
const metricTones = {
  amber: 'bg-amber-500/10 text-amber-600 ring-amber-500/20 dark:text-amber-300',
  emerald: 'bg-emerald-500/10 text-emerald-600 ring-emerald-500/20 dark:text-emerald-300',
  red: 'bg-red-500/10 text-red-600 ring-red-500/20 dark:text-red-300',
  slate: 'bg-slate-500/10 text-slate-600 ring-slate-500/20 dark:text-slate-300',
}

const metricAccents = {
  amber: 'from-amber-500/75 via-amber-300/25',
  emerald: 'from-emerald-500/75 via-emerald-300/25',
  red: 'from-red-500/75 via-red-300/25',
  slate: 'from-slate-500/65 via-slate-300/20',
}

function FleetStatusDock({ counts, loading, ownerOnly }) {
  const metrics = [
    { label: ownerOnly ? 'Your agents' : 'Total agents', value: counts.total, icon: Boxes, tone: 'amber' },
    { label: 'Active now', value: counts.running, icon: Activity, tone: 'emerald' },
    { label: 'Needs attention', value: counts.attention, icon: AlertTriangle, tone: 'red' },
    { label: 'Idle / pending', value: counts.idle, icon: WifiOff, tone: 'slate' },
  ]

  return (
    <section className="hv-panel overflow-hidden" aria-label="Live fleet status">
      <div className="flex items-center justify-between border-b border-gray-200/70 px-4 py-2.5 dark:border-white/[0.06]">
        <div className="flex items-center gap-2">
          <span className="relative flex h-2 w-2" aria-hidden="true">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-40 motion-reduce:animate-none" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-emerald-500" />
          </span>
          <span className="font-mono text-[10px] uppercase tracking-[0.2em] text-gray-500 dark:text-gray-400">Fleet status · live</span>
        </div>
        <span className="text-[11px] text-gray-400">updates on refresh</span>
      </div>
      <div className="grid grid-cols-2 gap-3 p-3 lg:grid-cols-4">
        {metrics.map(({ label, value, icon: Icon, tone }) => (
          <div
            key={label}
            className="group relative flex min-w-0 items-center gap-3 overflow-hidden rounded-xl border border-slate-200/95 bg-gradient-to-br from-white via-white to-slate-100/80 px-4 py-4 shadow-[0_2px_3px_rgba(15,23,42,0.08),0_12px_26px_-14px_rgba(15,23,42,0.32),inset_0_1px_0_rgba(255,255,255,0.9)] transition-all duration-200 hover:-translate-y-0.5 hover:border-primary-400/45 hover:shadow-[0_3px_5px_rgba(15,23,42,0.10),0_18px_32px_-14px_rgba(99,102,241,0.28)] dark:border-white/[0.11] dark:from-white/[0.075] dark:via-white/[0.035] dark:to-white/[0.015] dark:shadow-[0_0_0_1px_rgba(99,102,241,0.05),0_18px_34px_-18px_rgba(0,0,0,0.9),inset_0_1px_0_rgba(255,255,255,0.07)]"
          >
            <span
              className={clsx('absolute inset-x-0 top-0 h-0.5 bg-gradient-to-r to-transparent', metricAccents[tone])}
              aria-hidden="true"
            />
            <span className={clsx('flex h-9 w-9 shrink-0 items-center justify-center rounded-xl ring-1', metricTones[tone])}>
              <Icon className="h-4 w-4" aria-hidden="true" />
            </span>
            <span className="min-w-0">
              <span className="block truncate font-mono text-[10px] uppercase tracking-[0.16em] text-gray-400">{label}</span>
              <span className="hv-stat-number mt-0.5 block" aria-live="polite">{loading ? '—' : value}</span>
            </span>
          </div>
        ))}
      </div>
    </section>
  )
}

function QuickActionChip({ to, icon: Icon, children, primary = false }) {
  return (
    <Link
      to={to}
      className={clsx(
        'inline-flex items-center gap-2 rounded-full border px-3.5 py-2 text-sm font-medium transition-all hover:-translate-y-px',
        primary
          ? 'border-amber-400/50 bg-amber-500 text-gray-950 shadow-[0_8px_24px_-14px_rgba(245,158,11,0.8)] hover:bg-amber-400'
          : 'border-gray-200 bg-white/70 text-gray-700 hover:border-amber-400/50 hover:text-amber-700 dark:border-white/10 dark:bg-white/[0.035] dark:text-gray-300 dark:hover:text-amber-300',
      )}
    >
      <Icon className="h-4 w-4" aria-hidden="true" />
      {children}
    </Link>
  )
}

function pulseFirstTaskHref(orgSlug = '') {
  const orgParam = orgSlug ? `org=${encodeURIComponent(orgSlug)}&` : ''
  return `/pulse/tasks?${orgParam}onboarding=first-task`
}

function EmptyFleetStage({ isStaff, orgSlug, hasOrgMembership, membershipUnresolved }) {
  // HIVE-1630: a non-staff user who already belongs to orgs must never be
  // funneled into a false "Create org" empty state. Drive the CTA from the
  // JWT membership claim (the member-scoped orgs endpoint is admin-gated and
  // 403s for customers), so a ready org with 0 agents invites creating an
  // agent / opening Pulse — not creating another org.
  // HIVE-2383: fail CLOSED while the membership state is unresolved — an
  // unknown claims state must never render the Create-org funnel (HIVE-704).
  const createOrg = !isStaff && !hasOrgMembership && !membershipUnresolved
  const steps = [
    ['01', isStaff || !createOrg ? 'Add an agent' : 'Create your organization', 'Bring the first live worker onto the board.'],
    ['02', 'Create a Pulse task', 'Give the organization one concrete outcome.'],
    ['03', 'Watch activity land', 'This theater fills as agents move work.'],
  ]

  return (
    <section className="hv-panel relative overflow-hidden p-6 sm:p-8">
      <div className="pointer-events-none absolute -right-20 -top-24 h-64 w-64 rounded-full bg-amber-400/10 blur-3xl" />
      <div className="relative grid gap-8 lg:grid-cols-[minmax(0,1.25fr)_minmax(280px,0.75fr)] lg:items-center">
        <div>
          <div className="hv-kicker mb-2">Autonomous organization · ready</div>
          <h2 className="text-2xl font-semibold tracking-tight text-gray-950 dark:text-white">Your Hive is ready. Give it something real to move.</h2>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-gray-500 dark:text-gray-400">
            The overview becomes a live operations theater as agents take ownership, report progress, and hand work across teams.
          </p>
          <div className="mt-5 flex flex-wrap gap-2">
            <QuickActionChip to={isStaff || !createOrg ? '/agents' : '/create-organization'} icon={isStaff || !createOrg ? Bot : Building2} primary>
              {isStaff || !createOrg ? 'Add an agent' : 'Create your organization'}
            </QuickActionChip>
            <a
              href={pulseFirstTaskHref(orgSlug)}
              className="inline-flex items-center gap-2 rounded-full border border-gray-200 bg-white/70 px-3.5 py-2 text-sm font-medium text-gray-700 transition-all hover:-translate-y-px hover:border-primary-400/50 dark:border-white/10 dark:bg-white/[0.035] dark:text-gray-300"
            >
              <Workflow className="h-4 w-4" aria-hidden="true" />
              Open Pulse
            </a>
          </div>
        </div>
        <ol className="space-y-2.5" aria-label="Get started">
          {steps.map(([number, title, body]) => (
            <li key={number} className="flex gap-3 rounded-xl border border-gray-200/70 bg-white/55 p-3 dark:border-white/[0.06] dark:bg-white/[0.025]">
              <span className="font-mono text-xs font-semibold text-amber-600 dark:text-amber-300">{number}</span>
              <span>
                <span className="block text-sm font-medium text-gray-900 dark:text-gray-100">{title}</span>
                <span className="mt-0.5 block text-xs leading-5 text-gray-500 dark:text-gray-400">{body}</span>
              </span>
            </li>
          ))}
        </ol>
      </div>
    </section>
  )
}

// ── Attention banner ─────────────────────────────────────────────────────────
function AttentionBanner({ count, onViewAll }) {
  if (count === 0) return null
  return (
    <div className="rounded-xl border border-red-200 dark:border-red-900 bg-red-50 dark:bg-red-900/20 p-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="p-2 rounded-lg bg-red-100 dark:bg-red-900/40">
            <AlertTriangle className="h-5 w-5 text-red-600 dark:text-red-400" />
          </div>
          <div>
            <p className="font-semibold text-red-800 dark:text-red-200">
              {count} agent{count === 1 ? '' : 's'} {count === 1 ? 'needs' : 'need'} attention
            </p>
            <p className="text-sm text-red-600 dark:text-red-300">
              Agents flagged for intervention — failed, blocked, errored, or flagged by the heartbeat producer — require attention.
            </p>
          </div>
        </div>
        {onViewAll && (
          <button
            type="button"
            onClick={onViewAll}
            className="text-sm font-medium text-red-700 dark:text-red-300 hover:underline shrink-0"
          >
            View all
          </button>
        )}
      </div>
    </div>
  )
}

// ── Agent card ───────────────────────────────────────────────────────────────
function toAgentCard(item, source) {
  return {
    id: item.id,
    kind: 'fleet',
    snapshot: item,
    source,
    name: item.display_name || item.agent_username,
    handle: `@${item.agent_username}`,
    avatarUrl: item.avatar_url,
    role: item.role || 'Agent',
    runtime: [item.runtime_harness, item.runtime_provider, item.effective_model || item.model].filter(Boolean).join(' / ') || 'Unknown',
    settings: item.pvc_size ? `Workspace ${item.pvc_size}` : `${humanize(item.resource_profile)} profile`,
    status: item.status,
    created: item.created_at,
    lastActive: item.last_active_at,
    team: item.team_names?.[0] || item.team_memberships?.[0] || null,
    healthVerdict: agentHealthVerdict(item),
  }
}

function AgentHomeCard({ agent, onSelect }) {
  const needsHelp = agent.healthVerdict === 'needs_help'
  return (
    <button
      type="button"
      onClick={() => onSelect(agent)}
      className={clsx(
        'hv-panel hv-panel-hover group p-4 text-left',
        needsHelp ? 'border-red-300 dark:border-red-800' : 'border-gray-200 dark:border-gray-700'
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <AgentAvatar
            username={(agent.handle || '').startsWith('@') ? agent.handle.slice(1) : undefined}
            name={agent.name}
            avatarUrl={agent.avatarUrl}
            size="md"
            status={agent.status}
          />
          <div className="min-w-0">
            <h3 className="truncate font-semibold text-gray-950 dark:text-white">{agent.name}</h3>
            <p className="truncate text-xs text-gray-500 dark:text-gray-400">{agent.handle}</p>
          </div>
        </div>
        <StatusPill status={agent.status} />
      </div>

      <div className="mt-4 grid gap-2">
        <div className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
          <Cpu className="h-4 w-4 text-gray-400" />
          <span className="truncate">{agent.role}</span>
        </div>
        <div className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
          <Gauge className="h-4 w-4 text-gray-400" />
          <span className="truncate">{agent.settings}</span>
        </div>
        {agent.team && (
          <div className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
            <Users className="h-4 w-4 text-gray-400" />
            <span className="truncate">{agent.team}</span>
          </div>
        )}
        <div className="rounded-lg bg-gray-50 px-3 py-2 font-mono text-xs text-gray-600 dark:bg-gray-900/50 dark:text-gray-300">
          <span className="block truncate">{agent.runtime}</span>
        </div>
      </div>

      <div className="mt-4 flex items-center justify-between gap-3 text-xs text-gray-500 dark:text-gray-400">
        <span className="inline-flex items-center gap-1">
          <Clock className="h-3.5 w-3.5" />
          {relativeTime(agent.lastActive)}
        </span>
        <span className="rounded-full bg-gray-100 px-2 py-0.5 dark:bg-gray-700">
          {agent.source === 'runtime' ? 'runtime' : 'account'}
        </span>
      </div>
    </button>
  )
}

// ── Team rollup card ─────────────────────────────────────────────────────────
function FirstRunOnboardingPanel({ run }) {
  const onboarding = run?.onboarding
  if (!onboarding) return null
  const surfaces = onboarding.surfaces || []
  const firstTask = onboarding.first_task_flow || {}
  const summary = onboarding.summary || {}

  return (
    <div className="rounded-3xl border border-primary-100 bg-gradient-to-br from-primary-50 via-white to-amber-50 p-6 shadow-sm dark:border-primary-900 dark:from-primary-950/40 dark:via-gray-900 dark:to-amber-950/20">
      <div className="flex flex-col gap-5 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <div className="mb-3 inline-flex items-center gap-2 rounded-full bg-white/80 px-3 py-1 text-xs font-medium text-primary-700 shadow-sm dark:bg-gray-800 dark:text-primary-300">
            <Sparkles className="h-3.5 w-3.5" />
            First-run guide · {onboarding.template?.name || run.template_slug}
          </div>
          <h2 className="text-2xl font-bold text-gray-950 dark:text-white">Give {run.org_name} its first task</h2>
          <p className="mt-2 max-w-3xl text-gray-600 dark:text-gray-300">
            Your template created {summary.team_count || 0} teams and {summary.starter_task_count || 0} starter tasks. Start with one real Pulse task, then explore the agent fleet and Admin controls when you need them.
          </p>
        </div>
        <a
          href={firstTask.href || pulseFirstTaskHref(run.org_slug)}
          className="inline-flex shrink-0 items-center gap-2 rounded-xl bg-primary-600 px-4 py-2.5 text-sm font-semibold text-white hover:bg-primary-700"
        >
          <Workflow className="h-4 w-4" />
          Create first task
        </a>
      </div>

      <div className="mt-5 grid gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
        <div className="grid gap-3 md:grid-cols-2">
          {surfaces.map((surface) => (
            <a
              key={surface.id}
              href={surface.href}
              className="rounded-2xl border border-white/80 bg-white/85 p-4 shadow-sm transition hover:-translate-y-0.5 hover:shadow-md dark:border-gray-700 dark:bg-gray-800/80"
            >
              <div className="text-xs font-semibold uppercase tracking-wide text-primary-600 dark:text-primary-300">{surface.label}</div>
              <div className="mt-1 font-semibold text-gray-950 dark:text-white">{surface.headline}</div>
              <p className="mt-1 text-sm text-gray-600 dark:text-gray-300">{surface.body}</p>
              <div className="mt-3 text-sm font-medium text-primary-700 dark:text-primary-300">{surface.cta} →</div>
            </a>
          ))}
        </div>

        <div className="rounded-2xl border border-amber-200 bg-white/90 p-4 shadow-sm dark:border-amber-900 dark:bg-gray-800/90">
          <div className="text-xs font-semibold uppercase tracking-wide text-amber-700 dark:text-amber-300">Recommended starter task</div>
          <h3 className="mt-1 font-semibold text-gray-950 dark:text-white">{firstTask.title}</h3>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">Assigned to {firstTask.assignment_group || 'the right team'} · {firstTask.workflow || 'autonomous-dev'}</p>
          <ol className="mt-4 space-y-2 text-sm text-gray-700 dark:text-gray-300">
            {(firstTask.steps || []).map((step, index) => (
              <li key={step} className="flex gap-2">
                <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-amber-100 text-xs font-semibold text-amber-700 dark:bg-amber-900/40 dark:text-amber-300">{index + 1}</span>
                <span>{step}</span>
              </li>
            ))}
          </ol>
        </div>
      </div>
    </div>
  )
}

function PreviewScopedDashboardNotice({ run }) {
  if (!run?.dry_run && !run?.is_preview) return null
  const summary = run.onboarding?.summary || {}
  return (
    <div className="rounded-2xl border border-amber-200 bg-amber-50 p-5 shadow-sm dark:border-amber-900 dark:bg-amber-900/20">
      <div className="flex items-start gap-3">
        <Sparkles className="mt-0.5 h-5 w-5 text-amber-600 dark:text-amber-300" />
        <div>
          <h3 className="font-semibold text-amber-900 dark:text-amber-100">Preview-only dashboard</h3>
          <p className="mt-1 text-sm text-amber-800 dark:text-amber-200">
            This safe preview is scoped to {run.org_name}. Live Shizuha fleet agents and global activity are hidden until real provisioning adapters are enabled.
          </p>
          <div className="mt-3 flex flex-wrap gap-2 text-xs font-medium text-amber-800 dark:text-amber-200">
            <span className="rounded-full bg-white px-2.5 py-1 dark:bg-gray-800">{summary.team_count || 0} planned teams</span>
            <span className="rounded-full bg-white px-2.5 py-1 dark:bg-gray-800">{summary.agent_count || 0} planned agents</span>
            <span className="rounded-full bg-white px-2.5 py-1 dark:bg-gray-800">{summary.starter_task_count || 0} starter tasks</span>
          </div>
        </div>
      </div>
    </div>
  )
}

export function TeamRollupCard({ name, agentCount, statusCounts, attentionCount = 0, onViewTeam }) {
  const running = statusCounts.running || 0
  const attention = attentionCount
  const pct = agentCount ? Math.round((running / agentCount) * 100) : 0
  const capacityTone = agentCount === 0
    ? {
        bar: 'from-gray-400 to-gray-300 dark:from-gray-600 dark:to-gray-500',
        summary: 'text-gray-500 dark:text-gray-400',
        track: 'bg-gray-200/70 dark:bg-white/[0.06]',
      }
    : attention > 0 || pct === 0
      ? {
          bar: 'from-red-500 to-red-400',
          summary: 'text-red-600 dark:text-red-400',
          track: 'bg-red-100 dark:bg-red-950/40',
        }
      : pct >= 100
        ? {
            bar: 'from-emerald-500 to-emerald-400',
            summary: 'text-emerald-600 dark:text-emerald-400',
            track: 'bg-gray-200/70 dark:bg-white/[0.06]',
          }
        : {
            bar: 'from-amber-500 to-amber-400',
            summary: 'text-amber-600 dark:text-amber-400',
            track: 'bg-gray-200/70 dark:bg-white/[0.06]',
          }
  // Compact home-style row (design critique 2026-07-11): the old full cards made
  // 18 near-identical boxes fill the viewport with almost no signal. One dense
  // row per team — mono name, running/total, activity bar — reads like home's
  // "PROJECTS MOVING" list.
  return (
    <button
      type="button"
      onClick={onViewTeam}
      aria-label={`View ${name} team: ${running} of ${agentCount} running`}
      data-team-rollup
      className="hv-panel hv-panel-hover flex min-w-0 w-full items-center gap-3 overflow-hidden px-3.5 py-2.5 text-left"
    >
      <span className="min-w-0 flex-1">
        <span className="flex min-w-0 items-baseline justify-between gap-2">
          <span data-team-name className="min-w-0 truncate font-mono text-xs font-semibold uppercase tracking-wide text-gray-700 dark:text-gray-200">{name}</span>
          <span className="hidden shrink-0 text-[11px] text-gray-400 sm:block">{agentCount} agent{agentCount !== 1 ? 's' : ''}</span>
        </span>
        <span data-capacity-track className={`mt-2 block h-1.5 min-w-0 overflow-hidden rounded-full ${capacityTone.track}`}>
          <span
            data-capacity-bar
            className={`block h-full rounded-full bg-gradient-to-r ${capacityTone.bar} transition-all duration-500`}
            style={{ width: `${pct}%` }}
          />
        </span>
      </span>
      <span data-team-summary className="w-14 shrink-0 text-right">
        <span className={`block text-xs font-semibold tabular-nums ${capacityTone.summary}`}>{running}/{agentCount}</span>
        <span className="block font-mono text-[9px] uppercase tracking-[0.14em] text-gray-400">live</span>
        {attention > 0 && (
          <span className="mt-1 inline-flex items-center gap-1 text-[10px] text-red-500">
            <span className="h-1.5 w-1.5 rounded-full bg-red-500" />
            {attention} {attention === 1 ? 'needs help' : 'need help'}
          </span>
        )}
      </span>
    </button>
  )
}

// ── Activity timeline item ───────────────────────────────────────────────────
function ActivityItem({ agent, time, action }) {
  return (
    <div className="flex items-start gap-3 py-2">
      <div className="h-2 w-2 rounded-full bg-gray-300 dark:bg-gray-600 mt-1.5 shrink-0" />
      <div className="min-w-0">
        <p className="text-sm text-gray-700 dark:text-gray-300">
          <span className="font-medium">{agent}</span> {action}
        </p>
        <p className="text-xs text-gray-400">{time}</p>
      </div>
    </div>
  )
}

// ── Main overview component ──────────────────────────────────────────────────
export default function Overview() {
  const { isStaff, username, currentOrgSlug, claims, organizations, loading: authLoading, hasToken } = useHive()
  const [searchParams] = useSearchParams()
  const urlOrg = searchParams.get('org') || ''
  const requestedOrg = urlOrg || currentOrgSlug || ''
  const ownerOnly = !isStaff
  // HIVE-1630: a customer's org membership lives in the JWT claim, NOT in the
  // member-scoped orgs endpoint (admin-gated → 403 for non-staff). A non-staff
  // user with ≥1 membership has a ready org and must not be shown Create-org.
  // HIVE-2383: fail CLOSED while the membership state is unresolved — the auth
  // probe still loading, or a token present whose claims failed to decode.
  // "Unknown" must never render the Create-org funnel (HIVE-704 class).
  const membershipUnresolved = Boolean(authLoading) || Boolean(hasToken && !claims)
  const hasOrgMembership = Boolean(
    claims?.organization_memberships &&
      (typeof claims.organization_memberships === 'object'
        ? Object.keys(claims.organization_memberships).length > 0
        : claims.organization_memberships.length > 0),
  ) || (organizations?.length || 0) > 0
  const fleet = useFleetAgents({ enabled: true, ownerOnly, orgSlug: requestedOrg })
  // HIVE-1330: /fleet/efficiency/ is staff-only — a non-staff Overview must not
  // issue a fetch it is categorically forbidden to use (deterministic 403 +
  // console noise). Gate on isStaff like Fleet.jsx does; the recent-activity
  // section renders an honest "no recent activity" state when data is absent.
  const efficiency = useFleetEfficiency({ enabled: isStaff && !requestedOrg })
  const [selected, setSelected] = useState(null)
  const [showAllAttention, setShowAllAttention] = useState(false)
  const [onboardingRun, setOnboardingRun] = useState(null)
  const [onboardingLoading, setOnboardingLoading] = useState(Boolean(urlOrg))
  const previewDashboard = Boolean(onboardingRun?.dry_run || onboardingRun?.is_preview)
  const suppressFleetData = previewDashboard || (Boolean(urlOrg) && onboardingLoading)

  const agents = suppressFleetData ? [] : (fleet.data || [])
  const loading = suppressFleetData ? false : fleet.isLoading

  useEffect(() => {
    let mounted = true
    if (!requestedOrg) {
      setOnboardingLoading(false)
      setOnboardingRun(null)
      return () => { mounted = false }
    }
    setOnboardingLoading(true)
    orgProvisioningApi.list()
      .then((response) => {
        if (!mounted) return
        const runs = response.data?.results || []
        const match = runs.find((run) => run.org_slug === requestedOrg && run.status === 'ready')
        setOnboardingRun(match || null)
      })
      .catch(() => mounted && setOnboardingRun(null))
      .finally(() => mounted && setOnboardingLoading(false))
    return () => { mounted = false }
  }, [requestedOrg])

  // ── Derived counts ───────────────────────────────────────────────────────
  const counts = useMemo(() => {
    return {
      total: agents.length,
      running: agents.filter((a) => isRunning(a.status)).length,
      attention: agents.filter((a) => agentHealthVerdict(a) === 'needs_help').length,
      idle: agents.filter((a) => isIdle(a.status)).length,
    }
  }, [agents])

  // ── Attention agents (surfaced FIRST) ────────────────────────────────────
  const attentionAgents = useMemo(() => {
    return agents
      .filter((a) => agentHealthVerdict(a) === 'needs_help')
      .map((a) => toAgentCard(a, a.runtime_source || 'agent'))
      .slice(0, showAllAttention ? undefined : 3)
  }, [agents, showAllAttention])

  // ── Recent agents (newest first, excluding attention) ────────────────────
  const recentCards = useMemo(() => {
    const attentionIds = new Set(attentionAgents.map((a) => a.id))
    const items = agents
      .filter((a) => !attentionIds.has(a.id))
      .map((a) => toAgentCard(a, a.runtime_source || 'agent'))
    items.sort((a, b) => new Date(b.created || 0) - new Date(a.created || 0))
    return items.slice(0, 9)
  }, [agents, attentionAgents])

  // ── Team rollups ─────────────────────────────────────────────────────────
  const teamRollups = useMemo(() => {
    const map = {}
    agents.forEach((a) => {
      const teamName = a.team_names?.[0] || a.team_memberships?.[0]
      if (!teamName) return
      if (!map[teamName]) map[teamName] = { agents: [] }
      map[teamName].agents.push(a)
    })
    return Object.entries(map).map(([name, { agents: members }]) => {
      const statusCounts = {}
      members.forEach((a) => {
        statusCounts[a.status] = (statusCounts[a.status] || 0) + 1
      })
      const attentionCount = members.filter((a) => agentHealthVerdict(a) === 'needs_help').length
      return { name, agentCount: members.length, statusCounts, attentionCount }
    })
  }, [agents])

  // ── Recent activity (from efficiency data) ───────────────────────────────
  const recentActivity = useMemo(() => {
    if (suppressFleetData || requestedOrg) return []
    if (!efficiency.data) return []
    return Object.entries(efficiency.data)
      .slice(0, 5)
      .map(([id, data]) => ({
        agent: data.display_name || id,
        action: data.last_action || 'active',
        time: data.last_active ? relativeTime(data.last_active) : 'recently',
      }))
  }, [efficiency.data, requestedOrg, suppressFleetData])

  // ── Render ───────────────────────────────────────────────────────────────
  if (loading && agents.length === 0) {
    return <LoadingState label="Loading fleet overview…" />
  }

  if (fleet.isError && !agents.length) {
    return (
      <ErrorState
        message="Could not load agents. Check that the HIVE API is reachable."
        onRetry={() => fleet.refetch()}
      />
    )
  }

  return (
    <div className="space-y-6">
      {/* ── Header ─────────────────────────────────────────────────── */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <div><div className="hv-kicker mb-1">Hive · overview</div><h1 className="hv-page-title">Fleet Command Center</h1></div>
          <p className="mt-1 text-gray-500 dark:text-gray-400">
            {counts.running > 0
              ? `${counts.running} agent${counts.running === 1 ? '' : 's'} working now · your organization is moving.`
              : `Your organization is ready, ${username}. Live work will appear here.`}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {!isStaff && !hasOrgMembership && !membershipUnresolved && (
            <QuickActionChip to="/create-organization" icon={Building2}>Create org</QuickActionChip>
          )}
          {!suppressFleetData && (
            <button
              type="button"
              onClick={() => fleet.refetch()}
              className="inline-flex items-center gap-2 rounded-full border border-gray-200 bg-white/70 px-3.5 py-2 text-sm font-medium text-gray-700 transition-all hover:-translate-y-px hover:border-primary-400/50 dark:border-white/10 dark:bg-white/[0.035] dark:text-gray-300"
            >
              <RefreshCw className="h-4 w-4" />
              Refresh
            </button>
          )}
          {isStaff && !suppressFleetData && (
            <QuickActionChip to="/agents" icon={Plus} primary>Add agent</QuickActionChip>
          )}
        </div>
      </div>

      {onboardingRun && <FirstRunOnboardingPanel run={onboardingRun} />}
      {previewDashboard && <PreviewScopedDashboardNotice run={onboardingRun} />}

      {/* ── Compact status dock — one live HUD, not four dead cards ── */}
      <FleetStatusDock counts={counts} loading={loading} ownerOnly={ownerOnly} />

      {/* ── Attention banner ───────────────────────────────────────── */}
      <AttentionBanner count={counts.attention} onViewAll={() => setShowAllAttention((v) => !v)} />

      {/* ── Attention agents (surfaced FIRST) ──────────────────────── */}
      {attentionAgents.length > 0 && (
        <div>
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-lg font-semibold flex items-center gap-2">
              <AlertTriangle className="h-4 w-4 text-red-500" />
              Needs attention
            </h2>
            {attentionAgents.length < counts.attention && (
              <button
                type="button"
                onClick={() => setShowAllAttention(true)}
                className="text-sm text-primary-600 hover:underline"
              >
                Show all {counts.attention}
              </button>
            )}
          </div>
          <div className="grid gap-4 md:grid-cols-2 2xl:grid-cols-3">
            {attentionAgents.map((agent) => (
              <AgentHomeCard key={`attention-${agent.kind}-${agent.id}`} agent={agent} onSelect={setSelected} />
            ))}
          </div>
        </div>
      )}

      {/* ── Operations theater ─────────────────────────────────────── */}
      {(recentActivity.length > 0 || teamRollups.length > 0) && (
        <section className="grid min-w-0 gap-4 xl:grid-cols-[minmax(0,1.25fr)_minmax(360px,0.75fr)]">
          <div className="hv-panel min-w-0 overflow-hidden">
            <div className="flex items-center justify-between border-b border-gray-200/70 px-4 py-3 dark:border-white/[0.06]">
              <div>
                <div className="hv-kicker">Operations theater</div>
                <h2 className="mt-0.5 font-semibold text-gray-900 dark:text-gray-100">Recent activity</h2>
              </div>
              <span className="hv-chip"><Activity className="h-3 w-3" /> live</span>
            </div>
            <div className="divide-y divide-gray-200/60 px-4 py-1 dark:divide-white/[0.05]" aria-live="polite">
              {recentActivity.length > 0 ? recentActivity.map((item, i) => (
                <ActivityItem key={`${item.agent}-${item.time}-${i}`} agent={item.agent} time={item.time} action={item.action} />
              )) : (
                // HIVE-1661: restored the HIVE-726/HIVE-1566 empty-state contract —
                // icon, explanatory subtitle, and actionable CTA. A bare paragraph
                // hid the theater's intent and left ops users with nothing to do.
                <div className="flex flex-col items-center gap-3 py-8 text-center" data-testid="operations-empty-state">
                  <span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-gray-100 ring-1 ring-gray-200/50 dark:bg-white/[0.04] dark:ring-white/[0.06]">
                    <Activity className="h-6 w-6 text-gray-400 dark:text-gray-500" aria-hidden="true" />
                  </span>
                  <div className="max-w-lg">
                    <p className="text-sm font-medium text-gray-700 dark:text-gray-300">No recent activity</p>
                    <p className="mt-1 text-xs leading-relaxed text-gray-500 dark:text-gray-400">
                      This feed records agent task progress, status changes, and team handoffs.
                      New events will appear here as work moves. Current team state remains visible.
                    </p>
                  </div>
                  <a
                    href="/pulse/"
                    className="inline-flex items-center gap-1.5 rounded-full border border-gray-200 bg-white/70 px-3.5 py-1.5 text-xs font-medium text-gray-700 transition-all hover:-translate-y-px hover:border-primary-400/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/50 dark:border-white/10 dark:bg-white/[0.035] dark:text-gray-300"
                  >
                    <Workflow className="h-3.5 w-3.5" aria-hidden="true" />
                    Open Pulse
                  </a>
                </div>
              )}
            </div>
          </div>

          {teamRollups.length > 0 && (
            <div className="min-w-0">
              <div className="mb-2 flex items-end justify-between">
                <div>
                  <div className="hv-kicker">Organization</div>
                  <h2 className="mt-0.5 font-semibold text-gray-900 dark:text-gray-100">Teams moving</h2>
                </div>
                <Link to="/teams" className="text-xs font-medium text-primary-600 hover:underline dark:text-primary-300">Manage</Link>
              </div>
              <div className="grid gap-1.5">
                {teamRollups.slice(0, 8).map((team) => (
                  <TeamRollupCard
                    key={team.name}
                    name={team.name}
                    agentCount={team.agentCount}
                    statusCounts={team.statusCounts}
                    attentionCount={team.attentionCount}
                    onViewTeam={() => window.location.href = `/teams?name=${encodeURIComponent(team.name)}`}
                  />
                ))}
              </div>
            </div>
          )}
        </section>
      )}

      {/* ── Agents at a glance ─────────────────────────────────────── */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold">Agents at a glance</h2>
          <Link to="/agents" className="text-sm text-primary-600 hover:underline">
            View all agents
          </Link>
        </div>

        {recentCards.length === 0 && !loading ? (
          <EmptyFleetStage isStaff={isStaff} orgSlug={requestedOrg} hasOrgMembership={hasOrgMembership} membershipUnresolved={membershipUnresolved} />
        ) : (
          <div className="grid gap-4 md:grid-cols-2 2xl:grid-cols-3">
            {recentCards.map((agent) => (
              <AgentHomeCard key={`${agent.kind}-${agent.id}`} agent={agent} onSelect={setSelected} />
            ))}
          </div>
        )}
      </div>

      <AgentDrawer
        kind={selected?.kind}
        id={selected?.id}
        initialAgent={selected?.snapshot}
        onClose={() => setSelected(null)}
      />
    </div>
  )
}
