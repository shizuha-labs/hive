import clsx from 'clsx'
import {
  AlertTriangle,
  Briefcase,
  Clock,
  Gauge,
  Layers,
  Server,
  Settings2,
  Timer,
  TrendingUp,
  Zap,
  HelpCircle,
} from 'lucide-react'
import StatusPill from './StatusPill'
import AgentAvatar from './AgentAvatar'
import { humanize, relativeTime } from '../lib/format'
import { agentHealthVerdict, liveTokenRate, modelLabel, modelSourceLabel } from '../lib/fleetDegradation'
import { operatorFacingStatusMessage } from '../lib/agentStatus'
import ActivitySignal, { activityCardTone, activityPresentation, activityRingClass } from './ActivitySignal'
import { isLeaseIdleWaste } from '../lib/activityPhase'

const ATTENTION_STATUSES = new Set(['failed', 'blocked'])
const SLEEP_STATUSES = new Set(['offline', 'pending'])

function statusTone(status) {
  const key = (status || '').toLowerCase()
  if (key === 'running') return 'live'
  if (key === 'hibernated') return 'hibernate'
  if (key === 'stopped' || key === 'disabled') return 'stopped'
  if (key === 'provisioning' || key === 'deleting') return 'busy'
  if (ATTENTION_STATUSES.has(key)) return 'attention'
  if (SLEEP_STATUSES.has(key)) return 'sleep'
  return 'unknown'
}



// CON-176/SCLI-199: no concrete model → Unknown, never a default-source assertion.
const modelValue = modelLabel
const modelSource = modelSourceLabel

function workloadCounts(workload) {
  return {
    open: workload?.open || 0,
    progress: workload?.in_progress || 0,
    review: workload?.in_review || 0,
    blocked: workload?.blocked || 0,
    // 'scheduled' = open tasks due in the FUTURE (recurring/not-yet-movable). NOT
    // counted as active work, so an agent whose only tasks aren't due yet reads
    // "0 active" instead of looking like it's ignoring open work.
    scheduled: workload?.scheduled || 0,
    // 'backlog' = intentionally-parked work (decompose-on-pull when a team has
    // capacity). Like 'scheduled' it is NOT active work, so it gets its own chip
    // instead of inflating 'open' — a correctly-drained agent reads "0 open".
    backlog: workload?.backlog || 0,
    done: workload?.completed || 0,
    overdue: workload?.overdue || 0,
  }
}

function WorkloadStrip({ workload, dense, unknown = false, sharedServiceIdentity = false, serviceIdentityEmail = '' }) {
  // CON-176: cold pulse-snapshot failure — there is no workload data at all.
  // Render an explicit unavailable state instead of confident zeros.
  if (unknown) {
    return (
      <div
        data-testid="workload-unavailable"
        className="flex items-center gap-1.5 text-xs text-amber-700 dark:text-amber-400"
      >
        <Briefcase className="h-3.5 w-3.5" />
        Workload unavailable — Pulse snapshot fetch failed
      </div>
    )
  }
  const counts = workloadCounts(workload)
  const active = counts.open + counts.progress + counts.review + counts.blocked
  const max = Math.max(active + counts.scheduled + counts.backlog, 1)
  // HIVE-412: when the card's Pulse identity is a shared SERVICE account, the
  // pile is NOT this person's personal work — badge it unmistakably so a human
  // never reads a service-account queue (e.g. codex@shizuha.com's 671 doing) as
  // one engineer's workload or an Engineering bottleneck.
  const serviceHandle = serviceIdentityEmail ? serviceIdentityEmail.replace(/@.*$/, '') : ''
  const serviceBadge = sharedServiceIdentity ? (
    <span
      data-testid="shared-service-identity-badge"
      className="inline-flex items-center gap-1 rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-semibold text-amber-700 ring-1 ring-amber-200 dark:bg-amber-900/30 dark:text-amber-300 dark:ring-amber-800"
      title={`This queue belongs to the shared service account${serviceIdentityEmail ? ` ${serviceIdentityEmail}` : ''}, not to this agent personally.`}
    >
      shared service identity{serviceHandle ? ` · @${serviceHandle}` : ''}
    </span>
  ) : null
  const segments = [
    ['open', counts.open, 'bg-primary-500'],
    ['doing', counts.progress, 'bg-amber-500'],
    ['review', counts.review, 'bg-violet-500'],
    ['blocked', counts.blocked, 'bg-red-500'],
    ['scheduled', counts.scheduled, 'bg-gray-400'],
    ['backlog', counts.backlog, 'bg-slate-300'],
  ]

  if (dense) {
    return (
      <div className="flex items-center justify-between gap-2 text-xs">
        <span className="inline-flex items-center gap-1 text-gray-500 dark:text-gray-400">
          <Briefcase className="h-3.5 w-3.5" />
          {`${active} active`}
        </span>
        {serviceBadge}
        {!sharedServiceIdentity && counts.backlog > 0 && <span className="font-semibold text-slate-600 dark:text-slate-300">{counts.backlog} backlog</span>}
        {!sharedServiceIdentity && counts.blocked > 0 && <span className="font-semibold text-red-600 dark:text-red-400">{counts.blocked} blocked</span>}
      </div>
    )
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2 text-xs">
        <span className="inline-flex items-center gap-1 font-semibold text-gray-700 dark:text-gray-200">
          <Briefcase className="h-3.5 w-3.5 text-gray-400" />
          Workload
        </span>
        {serviceBadge}
      </div>
      <div className="flex h-2 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
        {segments.map(([key, value, color]) => (
          <span
            key={key}
            className={color}
            style={{ width: `${Math.max((value / max) * 100, value ? 8 : 0)}%` }}
          />
        ))}
      </div>
      <div className="grid grid-cols-6 gap-1 text-center">
        {[
          ['open', counts.open, 'text-primary-600 dark:text-primary-300'],
          ['doing', counts.progress, 'text-amber-600 dark:text-amber-400'],
          ['review', counts.review, 'text-violet-600 dark:text-violet-400'],
          ['blocked', counts.blocked, 'text-red-600 dark:text-red-400'],
          ['scheduled', counts.scheduled, 'text-gray-500 dark:text-gray-400'],
          ['backlog', counts.backlog, 'text-slate-500 dark:text-slate-400'],
        ].map(([label, value, color]) => (
          <div key={label} className="min-w-0">
            <div className={clsx('text-base font-bold leading-none', color)}>{value}</div>
            <div className="mt-0.5 truncate text-[10px] text-gray-500 dark:text-gray-400">{label}</div>
          </div>
        ))}
      </div>
    </div>
  )
}

function fmtCycleTime(seconds) {
  if (seconds == null || isNaN(seconds)) return null
  const h = seconds / 3600
  return h < 1 ? `${Math.round(seconds / 60)}m` : h < 24 ? `${h.toFixed(1)}h` : `${(h / 24).toFixed(1)}d`
}

function EfficiencyStrip({ efficiency, rate }) {
  const eff = efficiency || {}
  const done24h = eff.tasks_completed_24h
  const cycleTime = fmtCycleTime(eff.avg_lead_time_seconds)
  const overdue = eff.overdue_tasks
  // HIVE-175: recent output throughput (≈ tok/s) — live "is it producing now?"
  const tokPerSec = liveTokenRate(rate)

  if (done24h == null && cycleTime == null && overdue == null && tokPerSec == null) return null

  return (
    <div className="flex items-center gap-3 border-t border-gray-100 pt-2 text-xs dark:border-gray-700/60">
      <span className="shrink-0 text-[10px] font-semibold uppercase text-gray-400 dark:text-gray-500">Efficiency</span>
      {tokPerSec != null && (
        <span className="inline-flex items-center gap-1 text-gray-600 dark:text-gray-300" title="Recent output rate (words≈tokens) over the last 10 min">
          <Zap className="h-3 w-3 text-amber-500" />
          <span className="font-semibold">{tokPerSec}</span>
          <span className="text-gray-400">tok/s</span>
        </span>
      )}
      {done24h != null && (
        <span className="inline-flex items-center gap-1 text-gray-600 dark:text-gray-300">
          <TrendingUp className="h-3 w-3 text-green-500" />
          <span className="font-semibold">{done24h}</span>
          <span className="text-gray-400">/24h</span>
        </span>
      )}
      {cycleTime != null && (
        <span className="inline-flex items-center gap-1 text-gray-600 dark:text-gray-300">
          <Timer className="h-3 w-3 text-sky-500" />
          <span className="font-semibold">{cycleTime}</span>
          <span className="text-gray-400">avg</span>
        </span>
      )}
      {overdue != null && overdue > 0 && (
        <span className="inline-flex items-center gap-1 font-semibold text-red-600 dark:text-red-400">
          <AlertTriangle className="h-3 w-3" />
          {overdue} overdue
        </span>
      )}
    </div>
  )
}

function DetailLine({ icon: Icon, label, value, mono }) {
  return (
    <div className="flex min-w-0 items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400">
      <Icon className="h-3.5 w-3.5 shrink-0" />
      <span className="shrink-0">{label}</span>
      <span className={clsx('min-w-0 truncate text-gray-800 dark:text-gray-200', mono && 'font-mono')}>
        {value || '-'}
      </span>
    </div>
  )
}

export default function FleetAgentCard({ agent, workload, workloadUnknown = false, sharedServiceIdentity = false, serviceIdentityEmail = '', pulseNode, efficiency, teamNames = [], density = 'comfortable', onSelect }) {
  const status = (agent.status || '').toLowerCase()
  // Availability wins over container status for the card's color: a "running"
  // agent that Pulse/provider reports UNAVAILABLE (rate-limited, wedged, provider
  // down) is NOT healthy and must never read green. Surface it as 'attention'
  // (red) so a running-but-not-working agent is unmmissable. Stopped/disabled
  // keep their own (sleep) tone via statusTone.
  const notRunning = ['offline', 'hibernated', 'stopped', 'disabled'].includes(status)
  const counts = workloadCounts(workload)
  const assigned = counts.open + counts.progress + counts.review
  const queueBlindReason = /no pulse queue snapshot/i.test(
    `${agent?.needs_help_reason || ''}\n${(agent.health_summary?.issues || []).map((issue) => issue?.detail || '').join('\n')}`,
  )
  // Queue-blind heartbeat while Pulse already shows assigned work is a
  // snapshot miss, not a stuck seat (Aoi/Ichi/San 2026-09-16). Retract the
  // chip AND the activity pill — filtering issues alone left the red
  // "Needs help" signal from agent.needs_help === true.
  const retractQueueBlindNeedsHelp = assigned > 0 && queueBlindReason
  const healthIssues = (agent.health_summary?.issues || []).filter((issue) => {
    // Hibernated/stopped cards already say Hibernated. A leftover
    // heartbeat_needs_help chip from the last tenure ("4 ready / no
    // progress") made parked seats look like they were asking for help.
    if (notRunning && issue?.type === 'heartbeat_needs_help') return false
    if (issue?.type === 'heartbeat_needs_help' && retractQueueBlindNeedsHelp) return false
    return true
  })
  const healthVerdict = agentHealthVerdict(
    retractQueueBlindNeedsHelp ? { ...agent, needs_help: false } : agent,
  )
  const unavailable = healthVerdict === 'unavailable'
  const degraded = healthVerdict === 'needs_help'
  const activityVisual = activityPresentation(agent, {
    availability: unavailable ? 'unavailable' : 'available',
    needsHelp: degraded,
  })
  const tone = (!notRunning && (unavailable || degraded))
    ? 'attention'
    : activityCardTone(activityVisual.tone, statusTone(status))
  const ringClass = activityRingClass(activityVisual.tone)
  const leaseWaste = isLeaseIdleWaste(agent)
  const compact = density === 'compact'
  const dense = density === 'dense'
  const teams = teamNames.length ? teamNames : ['unassigned']
  const model = modelValue(agent)
  const harness = agent.runtime_harness || 'Runtime'
  const provider = agent.model_provider || agent.runtime_provider || 'provider'
  const imageTag = agent.runtime_image_tag || ''
  const placement = agent.pod_name || agent.namespace || '-'
  const runtimeSource = agent.runtime_source === 'rt-fleet' ? 'rt-fleet' : 'k3s'
  // Show the EFFECTIVE effort for every agent (not only explicit overrides):
  // Codex/Grok default to 'high', Claude Code / Shizuha run at model default.
  // Falls back to a local compute if an older backend omits the field.
  const effortValue = agent.effective_reasoning_effort
    || agent.reasoning_effort
    || (['codex_app_server', 'openclaw_app_server', 'grok_build'].includes(agent.execution_method) ? 'high' : 'default')
  const effortIsDefault = agent.reasoning_effort_is_default != null
    ? agent.reasoning_effort_is_default
    : !agent.reasoning_effort
  // Avoid the redundant "default (default)": when the effective value is itself
  // 'default' (model default, no discrete level) show it bare; a concrete
  // inferred level like 'high' still gets the "(default)" qualifier.
  const effort = effortIsDefault
    ? (effortValue === 'default' ? 'default' : `${effortValue} (default)`)
    : effortValue
  const displayTitle = agent.display_title || agent.role || 'Agent'
  const teamRoles = Array.isArray(agent.team_roles_summary) ? agent.team_roles_summary : []

  return (
    <button
      type="button"
      onClick={() => onSelect?.(agent)}
      className={clsx(
        'hive-agent-card hv-panel hv-panel-hover group relative flex flex-col overflow-hidden text-left',
        dense ? 'min-h-[190px] p-3' : compact ? 'min-h-[255px] p-3.5' : 'min-h-[315px] p-4',
        tone === 'live' && 'hive-agent-card-live border-emerald-300/60 dark:border-emerald-500/25',
        tone === 'thinking' && 'hive-agent-card-thinking border-violet-300/70 dark:border-violet-500/30',
        tone === 'tool' && 'hive-agent-card-tool border-amber-300/70 dark:border-amber-500/30',
        tone === 'waste' && 'hive-agent-card-waste border-amber-400 dark:border-amber-600',
        tone === 'idle' && 'border-gray-200 dark:border-gray-700',
        tone === 'busy' && 'border-primary-200 dark:border-primary-500/25',
        tone === 'attention' && 'hive-agent-card-attention border-red-300 dark:border-red-800',
        tone === 'hibernate' && 'border-sky-300/80 bg-sky-50/30 dark:border-sky-700/70 dark:bg-sky-950/10',
        tone === 'stopped' && 'border-red-300/70 dark:border-red-900/80',
        tone === 'sleep' && 'border-gray-200 dark:border-gray-700',
        tone === 'unknown' && 'border-amber-200 dark:border-amber-900/70'
      )}
    >
      <div className="mb-3 flex items-start justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2.5">
          {/* Real agent portrait — same identity as shizuha.com home (HIVE-617). */}
          <div className="relative shrink-0">
            <AgentAvatar
              username={agent.agent_username}
              name={agent.display_name}
              avatarUrl={agent.avatar_url}
              size={dense ? 'sm' : 'md'}
              status={agent.status}
            />
            {ringClass && <span className={ringClass} />}
          </div>
          <div className="min-w-0">
            <div className="flex min-w-0 items-center gap-2">
              <h2 className={clsx('truncate font-bold text-gray-950 dark:text-white', dense ? 'text-sm' : 'text-base')}>
                {agent.display_name || agent.agent_username}
              </h2>
              {agent.sandbox_required && (
                <span className="rounded bg-red-50 px-1.5 py-0.5 text-[10px] font-semibold text-red-600 ring-1 ring-red-200 dark:bg-red-900/20 dark:text-red-300 dark:ring-red-800">
                  sandbox
                </span>
              )}
              {Array.isArray(agent.pending_config_keys) && agent.pending_config_keys.length > 0 && (
                <span
                  title={`Config change accepted; syncing to the agent: ${agent.pending_config_keys.join(', ')}`}
                  className="animate-pulse rounded bg-blue-50 px-1.5 py-0.5 text-[10px] font-semibold text-blue-600 ring-1 ring-blue-200 dark:bg-blue-900/20 dark:text-blue-300 dark:ring-blue-800"
                >
                  syncing
                </span>
              )}
              {agent.is_external_byo && !agent.owner_id && (
                <span
                  className="rounded bg-amber-50 px-1.5 py-0.5 text-[10px] font-semibold text-amber-700 ring-1 ring-amber-200 dark:bg-amber-900/20 dark:text-amber-300 dark:ring-amber-800"
                  title="Unclaimed personal runtime connected to Hive"
                >
                  Unclaimed
                </span>
              )}
            </div>
            <div className="truncate text-xs text-gray-500 dark:text-gray-400">@{agent.agent_username}</div>
          </div>
        </div>
        <StatusPill status={agent.status} />
      </div>

      {!dense && (
        <div className="mb-3 flex flex-wrap gap-1.5">
          <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] font-medium text-gray-700 dark:bg-gray-700/70 dark:text-gray-200">
            {displayTitle}
          </span>
          {teamRoles.length > 1 && (
            <span
              className="rounded-full bg-purple-50 px-2 py-0.5 text-[11px] font-medium text-purple-700 ring-1 ring-purple-100 dark:bg-purple-900/20 dark:text-purple-300 dark:ring-purple-800"
              title={teamRoles.map((r) => `${humanize(r.team_slug)}: ${r.role_title || r.membership_role}`).join(' · ')}
            >
              {teamRoles.length} team roles
            </span>
          )}
          {teams.slice(0, compact ? 1 : 2).map((team) => (
            <span
              key={team}
              className="rounded-full bg-primary-50 px-2 py-0.5 text-[11px] font-medium text-primary-700 ring-1 ring-primary-100 dark:bg-primary-500/10 dark:text-primary-200 dark:ring-primary-500/30"
            >
              {humanize(team)}
            </span>
          ))}
        </div>
      )}

      {agent.model_lease?.managed && (
        <div className="mb-3 flex flex-wrap items-center gap-1.5 text-[11px]">
          <span
            className={clsx(
              'inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-semibold ring-1',
              agent.model_lease.state === 'holder'
                ? 'bg-emerald-50 text-emerald-700 ring-emerald-200 dark:bg-emerald-900/20 dark:text-emerald-300 dark:ring-emerald-800'
                : agent.model_lease.state === 'disabled'
                  ? 'bg-gray-100 text-gray-600 ring-gray-200 dark:bg-gray-800 dark:text-gray-300 dark:ring-gray-700'
                : agent.model_lease.state === 'unknown'
                  ? 'bg-amber-50 text-amber-700 ring-amber-200 dark:bg-amber-900/20 dark:text-amber-300 dark:ring-amber-800'
                  : 'bg-sky-50 text-sky-700 ring-sky-200 dark:bg-sky-900/20 dark:text-sky-300 dark:ring-sky-800'
            )}
            title={agent.model_lease.holder ? `Current holder: ${agent.model_lease.holder}` : 'No current holder'}
          >
            <Zap className="h-3 w-3" />
            {agent.model_lease.state === 'holder' ? 'Lease holder' : humanize(agent.model_lease.state)}
          </span>
          {leaseWaste && (
            <span
              data-testid="lease-idle-waste"
              className="inline-flex items-center gap-1 rounded-full bg-amber-50 px-2 py-0.5 font-semibold text-amber-800 ring-1 ring-amber-300 dark:bg-amber-900/30 dark:text-amber-200 dark:ring-amber-700"
              title="This agent holds a scarce model lease but is not in a turn"
            >
              <AlertTriangle className="h-3 w-3" />
              idle lease
            </span>
          )}
          {agent.model_lease.state === 'waiting' && agent.model_lease.holder && (
            <span className="truncate text-gray-500 dark:text-gray-400">
              behind {agent.model_lease.holder.replace(/^agent-/, '@')}
            </span>
          )}
        </div>
      )}

      <div className="mb-3 grid grid-cols-[minmax(0,1fr)_auto] gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase text-gray-500 dark:text-gray-400">
            <Settings2 className="h-3.5 w-3.5" />
            {harness}
            <span className="text-gray-300 dark:text-gray-600">/</span>
            {provider}
          </div>
          <div className={clsx('mt-1 truncate font-mono font-semibold text-gray-950 dark:text-white', dense ? 'text-sm' : 'text-base')}>
            {model}
          </div>
          <div className="mt-1 flex min-w-0 items-center gap-2 text-[11px] text-gray-500 dark:text-gray-400">
            <span className="truncate">{modelSource(agent)}</span>
            {/* Meta chips crush into "pin… effort… k… image fleetf…" on narrow stopped
                cards (design-loop P3) — running cards keep full meta; stopped show source only. */}
            {tone !== 'sleep' && <span className="hidden truncate sm:inline">effort {effort}</span>}
            {tone !== 'sleep' && <span className="hidden truncate md:inline">{runtimeSource}</span>}
            {tone !== 'sleep' && imageTag && <span className="hidden truncate xl:inline">image {imageTag}</span>}
          </div>
        </div>
        <ActivitySignal
          agent={agent}
          availability={unavailable ? 'unavailable' : 'available'}
          needsHelp={degraded}
          dense={dense}
        />
      </div>

      {operatorFacingStatusMessage(agent.status_message) && (
        <div className="mb-3 flex gap-2 rounded-lg border border-red-200 bg-red-50 px-2.5 py-2 text-xs text-red-700 dark:border-red-900 dark:bg-red-900/20 dark:text-red-300">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span className="line-clamp-2 break-words">{operatorFacingStatusMessage(agent.status_message)}</span>
        </div>
      )}

      {healthIssues.length > 0 && (
        <div className="mb-3 flex flex-wrap gap-1">
          {healthIssues.map((issue) => (
            <span
              key={issue.type}
              title={issue.detail ? `${issue.service}: ${issue.label} — ${issue.detail}` : `${issue.service}: ${issue.label}`}
              className={clsx(
                'inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-semibold ring-1',
                issue.severity === 'error'
                  ? 'bg-red-50 text-red-700 ring-red-200 dark:bg-red-900/20 dark:text-red-300 dark:ring-red-800'
                  // 'attention' = the runtime is fine, the agent's WORK is
                  // stuck. Blue keeps it visible without reading as a fault;
                  // in red it was indistinguishable from an auth-broken agent.
                  : issue.severity === 'attention'
                    ? 'bg-sky-50 text-sky-700 ring-sky-200 dark:bg-sky-900/20 dark:text-sky-300 dark:ring-sky-800'
                    : 'bg-amber-50 text-amber-700 ring-amber-200 dark:bg-amber-900/20 dark:text-amber-300 dark:ring-amber-800'
              )}
            >
              {issue.severity === 'attention'
                ? <HelpCircle className="h-3 w-3 shrink-0" />
                : <AlertTriangle className="h-3 w-3 shrink-0" />}
              {issue.label}
              {/* Operator 2026-08-05 ("i despise all of these needs help
                  issues"): the reason was hover-only, so the chip read as
                  "agent broken" with no explanation. For work-stuck chips the
                  WHY is the whole message — "2 ready task(s) with no progress
                  for 38 heartbeat(s)" tells you it is the TASKS that need a
                  routing decision, not the agent that needs a restart. Render
                  it inline, truncated; full text stays on hover. */}
              {issue.severity === 'attention' && issue.detail && (
                <span className="max-w-[180px] truncate font-normal opacity-80">
                  — {issue.detail}
                </span>
              )}
            </span>
          ))}
        </div>
      )}

      <div className="mt-auto space-y-3">
        <WorkloadStrip workload={workload} dense={dense} unknown={workloadUnknown} sharedServiceIdentity={sharedServiceIdentity} serviceIdentityEmail={serviceIdentityEmail} />

        {agent.context_budget && (
          <div className={clsx('rounded-lg px-2.5 py-2 text-xs ring-1', agent.context_budget.outlier ? 'bg-amber-50 text-amber-700 ring-amber-200 dark:bg-amber-900/20 dark:text-amber-300 dark:ring-amber-800' : 'bg-gray-50 text-gray-600 ring-gray-200 dark:bg-gray-900/30 dark:text-gray-300 dark:ring-gray-700')}>
            <div className="flex items-center justify-between gap-2">
              <span className="inline-flex items-center gap-1 font-semibold"><Gauge className="h-3.5 w-3.5" /> Context</span>
              <span className="font-mono">{Math.round(agent.context_budget.total_tokens / 100) / 10}k · {agent.context_budget.utilization_percent}%</span>
            </div>
          </div>
        )}

        {!dense && (
          <div className="grid gap-1.5">
            <DetailLine icon={Gauge} label="Profile" value={humanize(agent.resource_profile)} />
            {!compact && <DetailLine icon={Server} label="Pod" value={placement} mono />}
            {!compact && <DetailLine icon={Layers} label="Arch" value={agent.arch} />}
          </div>
        )}

        {!dense && <EfficiencyStrip efficiency={efficiency} rate={agent.activity_rate} />}

        <div className="flex items-center justify-between gap-3 text-xs text-gray-500 dark:text-gray-400">
          <span className="inline-flex min-w-0 items-center gap-1">
            <Clock className="h-3.5 w-3.5 shrink-0" />
            <span className="truncate">{relativeTime(agent.last_active_at)}</span>
          </span>
          {!dense && <span className="shrink-0 text-gray-400">open details</span>}
        </div>
      </div>
    </button>
  )
}
