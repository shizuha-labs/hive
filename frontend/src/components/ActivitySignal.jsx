import clsx from 'clsx'
import {
  Activity,
  AlertTriangle,
  Brain,
  CheckCircle2,
  Moon,
  PauseCircle,
  Radio,
  Wrench,
  XCircle,
} from 'lucide-react'
import {
  activityIdleMs,
  activityLabel,
  formatPhaseDuration,
  isLeaseIdleWaste,
  resolveActivityPhase,
} from '../lib/activityPhase'

const FAULT_TONES = {
  attention: { Icon: XCircle, label: 'Needs help' },
  hibernate: { Icon: PauseCircle, label: 'Hibernated' },
  stopped: { Icon: XCircle, label: 'Stopped' },
  sleep: { Icon: PauseCircle, label: 'Offline' },
  unknown: { Icon: AlertTriangle, label: 'Unknown' },
}

function phaseVisual(phase, { leaseIdleWaste, toolName } = {}) {
  if (leaseIdleWaste) {
    return { tone: 'waste', Icon: AlertTriangle, label: activityLabel('idle', { leaseIdleWaste: true }) }
  }
  if (phase === 'thinking') {
    return { tone: 'thinking', Icon: Brain, label: 'Thinking' }
  }
  if (phase === 'responding') {
    return { tone: 'responding', Icon: Radio, label: 'Responding' }
  }
  if (phase === 'tool') {
    return { tone: 'tool', Icon: Wrench, label: activityLabel('tool', { toolName }) }
  }
  if (phase === 'working') {
    return { tone: 'working', Icon: Activity, label: 'Working' }
  }
  if (phase === 'idle') {
    return { tone: 'idle', Icon: Moon, label: 'Idle' }
  }
  return { tone: 'live', Icon: CheckCircle2, label: 'Alive' }
}

export function activityPresentation(agent, { availability, needsHelp } = {}) {
  const status = String(agent?.status || '').toLowerCase()
  const stopped = status === 'stopped' || status === 'disabled'
  const hibernated = status === 'hibernated'
  const offline = status === 'offline'
  const unavailable = !stopped && !hibernated && availability === 'unavailable'
  const leaseIdleWaste = isLeaseIdleWaste(agent)
  const phase = resolveActivityPhase(agent)
  const toolName = agent?.activity_tool_name

  if (needsHelp) return { ...FAULT_TONES.attention, tone: 'attention', phase, leaseIdleWaste }
  if (offline) return { ...FAULT_TONES.sleep, tone: 'sleep', label: 'Offline', phase, leaseIdleWaste }
  if (unavailable) return { ...FAULT_TONES.sleep, tone: 'sleep', label: 'Unavailable', phase, leaseIdleWaste }
  if (hibernated) return { ...FAULT_TONES.hibernate, tone: 'hibernate', phase, leaseIdleWaste }
  if (stopped) return { ...FAULT_TONES.stopped, tone: 'stopped', phase, leaseIdleWaste }
  if (['provisioning', 'deleting'].includes(status)) {
    return { tone: 'busy', Icon: Activity, label: 'Changing', phase, leaseIdleWaste }
  }
  if (status === 'running' || status === 'external') {
    return { ...phaseVisual(phase, { leaseIdleWaste, toolName }), phase, leaseIdleWaste, toolName }
  }
  return { ...FAULT_TONES.unknown, tone: 'unknown', phase, leaseIdleWaste }
}

export default function ActivitySignal({
  agent,
  availability,
  needsHelp = false,
  dense = false,
}) {
  const visual = activityPresentation(agent, { availability, needsHelp })
  const Icon = visual.Icon
  const idleMs = visual.tone === 'idle' || visual.tone === 'waste' || visual.phase
    ? activityIdleMs(agent)
    : null
  const duration = formatPhaseDuration(
    ['thinking', 'responding', 'tool', 'working', 'idle', 'waste'].includes(visual.tone)
      ? (Number(agent?.activity_phase_for_ms) >= 0 ? agent.activity_phase_for_ms : idleMs)
      : null,
  )
  const showBars = ['responding', 'working', 'live'].includes(visual.tone)
  const showThink = visual.tone === 'thinking'
  const showTool = visual.tone === 'tool'

  return (
    <div
      data-testid="activity-signal"
      data-phase={visual.phase || ''}
      data-tone={visual.tone}
      className={clsx('hive-life rounded-lg px-2 py-1.5', `hive-life-${visual.tone}`)}
      title={visual.toolName ? `${visual.label}` : visual.label}
    >
      <div className="flex items-center gap-1.5">
        <Icon className={clsx('h-3.5 w-3.5 shrink-0', showTool && 'hive-tool-spin')} />
        <div className="min-w-0">
          <div className="truncate text-[11px] font-semibold uppercase tracking-wide">
            {visual.label}
          </div>
          {duration && (
            <div className="text-[10px] font-medium opacity-70">{duration}</div>
          )}
          {!dense && showBars && (
            <div className="flex items-end gap-0.5 pt-1" aria-hidden="true">
              <span className="hive-signal-bar" />
              <span className="hive-signal-bar" />
              <span className="hive-signal-bar" />
              <span className="hive-signal-bar" />
            </div>
          )}
          {!dense && showThink && (
            <div className="hive-think-shimmer mt-1" aria-hidden="true" />
          )}
          {!dense && showTool && (
            <div className="hive-tool-track mt-1" aria-hidden="true">
              <span />
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

export function activityCardTone(visualTone, statusTone) {
  if (visualTone === 'thinking') return 'thinking'
  if (visualTone === 'responding' || visualTone === 'working' || visualTone === 'live') return 'live'
  if (visualTone === 'tool') return 'tool'
  if (visualTone === 'waste') return 'waste'
  if (visualTone === 'idle') return 'idle'
  return statusTone
}

export function activityRingClass(tone) {
  if (tone === 'thinking') return 'hive-live-ring hive-live-ring-thinking'
  if (tone === 'responding' || tone === 'working' || tone === 'live') return 'hive-live-ring'
  if (tone === 'tool') return 'hive-live-ring hive-live-ring-tool'
  if (tone === 'waste') return 'hive-live-ring hive-live-ring-waste'
  return null
}
