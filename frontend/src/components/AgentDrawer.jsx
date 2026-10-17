import { useState, useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import {
  X,
  Play,
  Square,
  RotateCcw,
  Trash2,
  Cpu,
  Server,
  Box,
  Tag,
  Activity,
  Clock,
  HardDrive,
  Layers,
  ShieldAlert,
  Pencil,
  Save,
  UserPlus,
  UserMinus,
  MessageCircle,
  PauseCircle,
  Archive,
  ArchiveRestore,
  ExternalLink,
  Users,
  Plus,
  Maximize2,
  CheckCircle2,
  AlertCircle,
  Zap,
  Braces,
} from 'lucide-react'
import StatusPill from './StatusPill'
import ActivitySignal from './ActivitySignal'
import AgentAvatar from './AgentAvatar'
import ConfirmDialog from './ConfirmDialog'
import { LoadingState } from './States'
import { useToast } from './Toast'
import { useHive } from '../contexts/HiveContext'
import { relativeTime, errorMessage, humanize, teamDisplayLabel } from '../lib/format'
import { cycleDialogFocus, inertBodySiblings } from '../lib/dialogFocus'
import { agentHealthVerdict, liveTokenRate } from '../lib/fleetDegradation'
import { operatorFacingStatusMessage } from '../lib/agentStatus'
import { deepenFleetAgent } from '../lib/agentSnapshot'
import {
  useFleetAgent,
  useFleetEvents,
  useFleetActivity,
  useFleetTeamPlacement,
  useFleetRuntimeOptions,
  useFleetMutations,
  useEffectiveCapabilities,
  useClaw,
  useClawEvents,
  useClawWhatsAppLinks,
  useClawMutations,
} from '../hooks/useAgents'

function Field({ icon: Icon, label, value, mono }) {
  return (
    <div className="flex items-start gap-2.5 py-2">
      {Icon && <Icon className="h-4 w-4 text-gray-400 mt-0.5 shrink-0" />}
      <div className="min-w-0 flex-1">
        <div className="text-xs text-gray-500 dark:text-gray-400">{label}</div>
        <div className={`text-sm text-gray-800 dark:text-gray-200 break-words ${mono ? 'font-mono' : ''}`}>
          {value || '—'}
        </div>
      </div>
    </div>
  )
}

const CHANGE_IMPACTS = {
  instant: {
    label: 'Instant',
    description: 'Saved immediately; no runtime restart.',
    className: 'bg-emerald-50 text-emerald-700 ring-emerald-200 dark:bg-emerald-900/20 dark:text-emerald-300 dark:ring-emerald-800',
    icon: Zap,
  },
  restart: {
    label: 'Restarts runtime',
    description: 'Saved immediately, then the agent runtime is gracefully reapplied.',
    className: 'bg-amber-50 text-amber-700 ring-amber-200 dark:bg-amber-900/20 dark:text-amber-300 dark:ring-amber-800',
    icon: RotateCcw,
  },
  next_restart: {
    label: 'Next restart',
    description: 'Saved immediately and used the next time the runtime starts.',
    className: 'bg-blue-50 text-blue-700 ring-blue-200 dark:bg-blue-900/20 dark:text-blue-300 dark:ring-blue-800',
    icon: Clock,
  },
}

function ImpactBadge({ type = 'instant' }) {
  const impact = CHANGE_IMPACTS[type] || CHANGE_IMPACTS.instant
  const Icon = impact.icon
  return (
    <span
      title={impact.description}
      className={`inline-flex shrink-0 items-center gap-1 rounded-full px-1.5 py-0.5 text-[10px] font-semibold normal-case tracking-normal ring-1 ${impact.className}`}
    >
      <Icon className="h-2.5 w-2.5" />
      {impact.label}
    </span>
  )
}

function FormLabel({ children, impact, hint }) {
  return (
    <span className="mb-1 flex items-start justify-between gap-2 text-xs text-gray-600 dark:text-gray-300">
      <span>
        <span className="font-medium">{children}</span>
        {hint && <span className="ml-1 font-normal text-gray-400">{hint}</span>}
      </span>
      {impact && <ImpactBadge type={impact} />}
    </span>
  )
}

// Guided edit section — each group explains its purpose and change impact so
// operators do not have to infer runtime behaviour from a raw field dump.
function EditGroup({ icon: Icon, title, description, impact, children }) {
  return (
    <section className="overflow-hidden rounded-xl border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-800/40">
      <div className="flex items-start justify-between gap-3 border-b border-gray-100 bg-gray-50/80 px-3 py-2.5 dark:border-gray-700/70 dark:bg-gray-900/30">
        <div className="flex min-w-0 items-start gap-2">
          {Icon && <Icon className="mt-0.5 h-4 w-4 shrink-0 text-primary-500" />}
          <div className="min-w-0">
            <h4 className="text-xs font-semibold text-gray-800 dark:text-gray-100">{title}</h4>
            {description && <p className="mt-0.5 text-[11px] leading-4 text-gray-500 dark:text-gray-400">{description}</p>}
          </div>
        </div>
        {impact && <ImpactBadge type={impact} />}
      </div>
      <div className="space-y-3 p-3">
        {children}
      </div>
    </section>
  )
}

function normalizePickerOptions(options = []) {
  return options.map((item) => {
    if (typeof item === 'string') return { id: item, label: item }
    const id = item?.id || item?.slug || item?.name
    return { id, label: item?.label || item?.name || item?.slug || id }
  }).filter((item) => item.id)
}

function ChipPicker({ id, label, values = [], options = [], onChange, helper, allowCustom = true, impact = 'restart' }) {
  const [draft, setDraft] = useState('')
  const normalized = normalizePickerOptions(options)
  const labelById = new Map(normalized.map((item) => [item.id, item.label]))
  const selected = Array.from(new Set(values.filter(Boolean)))
  const available = normalized.filter((item) => !selected.includes(item.id))

  const add = (raw) => {
    const next = String(raw || '').trim()
    if (!next || selected.includes(next)) return
    if (!allowCustom && !normalized.some((item) => item.id === next)) return
    onChange([...selected, next])
    setDraft('')
  }
  const remove = (value) => onChange(selected.filter((item) => item !== value))

  return (
    <div>
      <FormLabel impact={impact}>{label}</FormLabel>
      <div className="rounded-lg border border-gray-300 bg-white p-2 focus-within:border-primary-500 focus-within:ring-1 focus-within:ring-primary-500 dark:border-gray-600 dark:bg-gray-800">
        <div className="flex min-h-7 flex-wrap gap-1.5" role="list" aria-label={`Selected ${label.toLowerCase()}`}>
          {selected.length ? selected.map((value) => (
            <span key={value} role="listitem" className="inline-flex max-w-full items-center gap-1 rounded-full bg-primary-50 px-2 py-1 text-[11px] font-medium text-primary-700 ring-1 ring-primary-200 dark:bg-primary-900/20 dark:text-primary-300 dark:ring-primary-800">
              <span className="truncate">{labelById.get(value) || value}</span>
              <button type="button" onClick={() => remove(value)} aria-label={`Remove ${labelById.get(value) || value}`} className="rounded-full p-0.5 hover:bg-primary-100 dark:hover:bg-primary-800/40">
                <X className="h-2.5 w-2.5" />
              </button>
            </span>
          )) : (
            <span className="py-1 text-[11px] text-gray-400">None selected</span>
          )}
        </div>
        <div className="mt-2 flex gap-2 border-t border-gray-100 pt-2 dark:border-gray-700">
          {allowCustom ? (
            <>
              <input
                id={id}
                aria-label={`Add ${label.toLowerCase()}`}
                list={`${id}-options`}
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ',') {
                    event.preventDefault()
                    add(draft)
                  }
                }}
                className="min-w-0 flex-1 bg-transparent text-xs outline-none placeholder:text-gray-400"
                placeholder={`Add ${label.toLowerCase()}…`}
              />
              <datalist id={`${id}-options`}>
                {available.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
              </datalist>
              <button type="button" onClick={() => add(draft)} disabled={!draft.trim()} className="inline-flex items-center gap-1 rounded-md bg-gray-100 px-2 py-1 text-[11px] font-medium text-gray-600 hover:bg-gray-200 disabled:opacity-40 dark:bg-gray-700 dark:text-gray-200 dark:hover:bg-gray-600">
                <Plus className="h-3 w-3" />Add
              </button>
            </>
          ) : (
            <select id={id} aria-label={`Add ${label.toLowerCase()}`} value="" onChange={(event) => add(event.target.value)} disabled={!available.length} className="w-full bg-transparent text-xs text-gray-600 outline-none disabled:text-gray-400 dark:text-gray-300">
              <option value="">{available.length ? `Add ${label.toLowerCase()}…` : 'All available options selected'}</option>
              {available.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
            </select>
          )}
        </div>
      </div>
      {helper && <p className="mt-1 text-[10px] leading-4 text-gray-400">{helper}</p>}
    </div>
  )
}

function isPlainObject(value) {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value))
}

function allValuesAreStrings(value) {
  return Object.values(value).every((item) => typeof item === 'string')
}

const RT_FLEET_RESOURCE_FIELD = {
  label: 'Resource limits',
  key: 'resource_limits',
  payloadKey: 'resource_limits',
  impact: 'next_restart',
  schema: '{ "memory"?: string, "cpus"?: string, "pidsLimit"?: number }',
  example: { memory: '8g', cpus: '2', pidsLimit: 256 },
  validate(value) {
    const allowed = new Set(['memory', 'cpus', 'pidsLimit'])
    if (Object.keys(value).some((key) => !allowed.has(key))) return 'Only memory, cpus, and pidsLimit are supported.'
    if (value.memory != null && typeof value.memory !== 'string') return 'memory must be a Docker memory string such as "8g".'
    if (value.cpus != null && typeof value.cpus !== 'string') return 'cpus must be a Docker CPU string such as "2" or "0.5".'
    if (value.pidsLimit != null && (!Number.isInteger(value.pidsLimit) || value.pidsLimit < 1)) return 'pidsLimit must be a positive integer.'
    return ''
  },
}

const NATIVE_RESOURCE_FIELD = {
  label: 'Resource requests & limits',
  key: 'resource_limits',
  payloadKey: 'resource_overrides',
  impact: 'next_restart',
  schema: '{ "requests"?: { "cpu"?: string, "memory"?: string }, "limits"?: { "cpu"?: string, "memory"?: string } }',
  example: {
    requests: { cpu: '500m', memory: '1Gi' },
    limits: { cpu: '2', memory: '4Gi' },
  },
  validate(value) {
    const allowed = new Set(['requests', 'limits'])
    if (Object.keys(value).some((key) => !allowed.has(key))) return 'Only requests and limits are supported for native agents.'
    for (const section of ['requests', 'limits']) {
      if (value[section] == null) continue
      if (!isPlainObject(value[section])) return `${section} must be a JSON object.`
      if (Object.keys(value[section]).some((key) => !['cpu', 'memory'].includes(key))) return `${section} only supports cpu and memory.`
      if (!allValuesAreStrings(value[section])) return `${section} cpu and memory values must be Kubernetes quantity strings.`
    }
    return ''
  },
}

const COMMON_ADVANCED_FIELDS = [
  {
    label: 'Environment',
    key: 'env',
    impact: 'restart',
    schema: '{ "VARIABLE_NAME": "string value" }',
    example: { LOG_LEVEL: 'info' },
    validate: (value) => allValuesAreStrings(value) ? '' : 'Every environment value must be a string.',
  },
  {
    label: 'Token budget',
    key: 'token_budget',
    impact: 'instant',
    schema: '{ "monthlyLimit": number, "tokensUsed": number, "resetDay": 1..28 }',
    example: { monthlyLimit: 1000000, tokensUsed: 0, resetDay: 1 },
    validate(value) {
      for (const key of ['monthlyLimit', 'tokensUsed']) {
        if (value[key] != null && (typeof value[key] !== 'number' || value[key] < 0)) return `${key} must be a non-negative number.`
      }
      if (value.resetDay != null && (!Number.isInteger(value.resetDay) || value.resetDay < 1 || value.resetDay > 28)) return 'resetDay must be an integer from 1 to 28.'
      return ''
    },
  },
  {
    label: 'Personality traits',
    key: 'personality_traits',
    impact: 'instant',
    schema: '{ "trait": "value" }',
    example: { approach: 'pragmatic', focus: 'reliability' },
    validate: (value) => allValuesAreStrings(value) ? '' : 'Every personality trait value must be a string.',
  },
  {
    label: 'Work schedule',
    key: 'work_schedule',
    impact: 'instant',
    schema: '{ "days": [0..6], "startHour": 0..23, "endHour": 0..23, "timezone": "IANA zone" }',
    example: { days: [0, 1, 2, 3, 4], startHour: 9, endHour: 18, timezone: 'Asia/Kolkata' },
    validate(value) {
      if (value.days != null && (!Array.isArray(value.days) || value.days.some((day) => !Number.isInteger(day) || day < 0 || day > 6))) return 'days must contain weekday integers from 0 (Mon) to 6 (Sun).'
      for (const key of ['startHour', 'endHour']) {
        if (value[key] != null && (!Number.isInteger(value[key]) || value[key] < 0 || value[key] > 23)) return `${key} must be an integer from 0 to 23.`
      }
      if (value.timezone != null && typeof value.timezone !== 'string') return 'timezone must be an IANA timezone string.'
      return ''
    },
  },
]

export function advancedFieldsForAgent(agent) {
  const native = agent?.runtime_source === 'k3s-native'
  const rtFleet = agent?.runtime_source === 'rt-fleet'
  const resourceField = native
    ? NATIVE_RESOURCE_FIELD
    : RT_FLEET_RESOURCE_FIELD
  // Native/Connect-backed rows persist env for the next workload start. Only
  // rt-fleet routes through the daemon restart contract.
  return [
    resourceField,
    ...COMMON_ADVANCED_FIELDS.map((field) => (
      field.key === 'env' && !rtFleet ? { ...field, impact: 'next_restart' } : field
    )),
  ]
}

export function capabilityImpactForAgent(agent) {
  // rt-fleet applies capability changes by restarting its child; external
  // Connect agents acknowledge the pushed live config. Native rows persist the
  // desired JSON config but have no live config-push path, so take effect only
  // when the native workload next starts.
  if (agent?.runtime_source === 'rt-fleet') return 'restart'
  if (agent?.runtime_source === 'external') return 'instant'
  return 'next_restart'
}

function resourceConfigForAgent(agent) {
  const overrides = isPlainObject(agent?.resource_overrides) ? agent.resource_overrides : {}
  if (Object.keys(overrides).length > 0) return overrides
  return isPlainObject(agent?.resource_profile_spec) ? agent.resource_profile_spec : {}
}

function validateJsonField(text, field) {
  const source = String(text || '').trim()
  if (!source) return { valid: true, value: {}, empty: true, error: '' }
  let value
  try {
    value = JSON.parse(source)
  } catch (error) {
    return { valid: false, value: null, empty: false, error: `Invalid JSON: ${error.message}` }
  }
  if (!isPlainObject(value)) return { valid: false, value, empty: false, error: 'Expected a JSON object, not an array or scalar.' }
  const schemaError = field?.validate?.(value) || ''
  return { valid: !schemaError, value, empty: Object.keys(value).length === 0, error: schemaError }
}

function jsonFieldIsSet(value) {
  const result = validateJsonField(value)
  return !result.empty || !result.valid
}

// Validated JSON override editor. Validation is live, field-specific, and the
// pretty-print action makes raw power fields safe without promoting them into
// the everyday path.
function JsonField({ field, value, onChange }) {
  const result = validateJsonField(value, field)
  const rows = Math.min(10, Math.max(3, String(value || '').split('\n').length))
  const StatusIcon = result.valid ? CheckCircle2 : AlertCircle
  const statusLabel = result.valid ? (result.empty ? 'Empty' : 'Valid JSON') : 'Needs attention'
  return (
    <div className="rounded-lg border border-gray-200 bg-gray-50/50 p-2.5 dark:border-gray-700 dark:bg-gray-900/20">
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="flex items-center gap-1.5 text-xs font-medium text-gray-700 dark:text-gray-200">
            {field.label}
            <ImpactBadge type={field.impact} />
          </div>
          <p className="mt-0.5 font-mono text-[10px] leading-4 text-gray-400">{field.schema}</p>
        </div>
        <span className={`inline-flex shrink-0 items-center gap-1 text-[10px] font-medium ${result.valid ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-600 dark:text-red-400'}`}>
          <StatusIcon className="h-3 w-3" />{statusLabel}
        </span>
      </div>
      <textarea
        rows={rows}
        spellCheck={false}
        aria-label={`${field.label} JSON`}
        aria-invalid={!result.valid}
        className={`mt-2 w-full rounded-lg border bg-white px-2.5 py-2 font-mono text-[11px] leading-4 outline-none focus:ring-1 dark:bg-gray-800 ${result.valid ? 'border-gray-300 focus:border-primary-500 focus:ring-primary-500 dark:border-gray-600' : 'border-red-400 focus:border-red-500 focus:ring-red-500 dark:border-red-700'}`}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={JSON.stringify(field.example, null, 2)}
      />
      <div className="mt-1.5 flex items-start justify-between gap-2">
        <p className={`text-[10px] leading-4 ${result.valid ? 'text-gray-400' : 'text-red-600 dark:text-red-400'}`}>
          {result.valid ? `Example: ${JSON.stringify(field.example)}` : result.error}
        </p>
        <button
          type="button"
          disabled={!result.valid}
          onClick={() => onChange(JSON.stringify(result.value, null, 2))}
          className="inline-flex shrink-0 items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium text-primary-600 hover:bg-primary-50 disabled:cursor-not-allowed disabled:opacity-40 dark:text-primary-300 dark:hover:bg-primary-900/20"
        >
          <Braces className="h-3 w-3" />Pretty-print
        </button>
      </div>
    </div>
  )
}

function ContextPromptEditor({ value, onChange }) {
  const [expanded, setExpanded] = useState(false)
  const expandTriggerRef = useRef(null)
  const expandedDialogRef = useRef(null)
  const expandedInitialFocusRef = useRef(null)
  const count = String(value || '').length
  const editorClass = 'w-full resize-y rounded-lg border border-gray-300 bg-white px-3 py-2 font-mono text-xs leading-5 outline-none focus:border-primary-500 focus:ring-1 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800'

  useEffect(() => {
    if (!expanded) return undefined
    const previousFocus = document.activeElement
    expandedInitialFocusRef.current?.focus()
    const onKey = (event) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        event.stopPropagation()
        setExpanded(false)
        return
      }
      if (event.key === 'Tab') {
        event.stopPropagation()
        cycleDialogFocus(event, expandedDialogRef.current)
      }
    }
    document.addEventListener('keydown', onKey, true)
    return () => {
      document.removeEventListener('keydown', onKey, true)
      if (previousFocus?.isConnected) previousFocus.focus()
    }
  }, [expanded])

  return (
    <div>
      <FormLabel impact="restart">Context prompt</FormLabel>
      <div className="relative">
        <textarea
          rows="8"
          spellCheck={false}
          aria-label="Context prompt"
          className={editorClass}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder="Standing instructions injected into every turn"
        />
        <button ref={expandTriggerRef} type="button" onClick={() => setExpanded(true)} className="absolute bottom-3 right-2 inline-flex items-center gap-1 rounded-md border border-gray-200 bg-white/95 px-2 py-1 text-[10px] font-medium text-gray-600 shadow-sm hover:bg-gray-50 dark:border-gray-600 dark:bg-gray-800/95 dark:text-gray-200 dark:hover:bg-gray-700">
          <Maximize2 className="h-3 w-3" />Full screen
        </button>
      </div>
      <div className="mt-1 flex items-center justify-between gap-2 text-[10px] text-gray-400">
        <span>Standing instructions applied to every turn.</span>
        <span className="font-mono">{count.toLocaleString()} characters</span>
      </div>
      {expanded && (
        <div ref={expandedDialogRef} data-agent-drawer-nested-modal="true" tabIndex={-1} className="fixed inset-0 z-[140] flex flex-col bg-white dark:bg-gray-900" role="dialog" aria-modal="true" aria-label="Full-screen context prompt editor">
          <div className="flex items-center justify-between gap-4 border-b border-gray-200 px-4 py-3 dark:border-gray-700 sm:px-6">
            <div>
              <h3 className="text-sm font-semibold">Context prompt</h3>
              <p className="text-xs text-gray-500">{count.toLocaleString()} characters · runtime restarts when this change is saved</p>
            </div>
            <button ref={expandedInitialFocusRef} type="button" onClick={() => setExpanded(false)} aria-label="Close full-screen editor" className="rounded-lg p-2 hover:bg-gray-100 dark:hover:bg-gray-800">
              <X className="h-5 w-5" />
            </button>
          </div>
          <div className="flex min-h-0 flex-1 p-3 sm:p-6">
            <textarea
              autoFocus
              spellCheck={false}
              aria-label="Full-screen context prompt"
              className={`${editorClass} min-h-full flex-1 resize-none`}
              value={value}
              onChange={(event) => onChange(event.target.value)}
            />
          </div>
          <div className="flex items-center justify-between border-t border-gray-200 px-4 py-3 text-xs dark:border-gray-700 sm:px-6">
            <span className="text-gray-500">Changes stay in this draft until Save &amp; push.</span>
            <button type="button" onClick={() => setExpanded(false)} className="rounded-lg bg-primary-600 px-4 py-2 font-medium text-white hover:bg-primary-700">Done</button>
          </div>
        </div>
      )}
    </div>
  )
}

// VM-instance-style detail section: a labelled group of read-only fields, the
// way a cloud console groups an instance into Machine / Boot disk / Networking /
// Identity blocks instead of one flat property dump.
function DetailSection({ icon: Icon, title, children }) {
  return (
    <div className="mb-4 rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden">
      <div className="flex items-center gap-1.5 border-b border-gray-200 bg-gray-50 px-3 py-2 text-xs font-semibold uppercase tracking-wide text-gray-600 dark:border-gray-700 dark:bg-gray-800/60 dark:text-gray-300">
        {Icon && <Icon className="h-3.5 w-3.5" />}
        {title}
      </div>
      <div className="divide-y divide-gray-100 px-3 dark:divide-gray-700/60">
        {children}
      </div>
    </div>
  )
}

// CON-176/SCLI-199: no concrete model → Unknown, never a default-source assertion.
function modelValue(agent) {
  return agent?.effective_model || agent?.model || 'Unknown'
}

function imageValue(agent) {
  return agent?.effective_image || agent?.image || 'Fleet default image'
}

function formatTokens(value) {
  const n = Number(value || 0)
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1000) return `${(n / 1000).toFixed(n >= 10_000 ? 0 : 1)}k`
  return `${n}`
}

function EventList({ events, loading }) {
  if (loading) return <LoadingState label="Loading events…" />
  if (!events?.length) {
    return <p className="text-sm text-gray-500 py-4">No events recorded yet.</p>
  }
  return (
    <ol className="relative border-l border-gray-200 dark:border-gray-700 ml-2 space-y-4 py-2">
      {events.map((e, i) => (
        <li key={e.id ?? i} className="ml-4">
          <span className="absolute -left-1.5 mt-1.5 h-3 w-3 rounded-full bg-primary-500 ring-4 ring-white dark:ring-gray-800" />
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-medium">{humanize(e.event_type)}</span>
            <span className="text-xs text-gray-400">{relativeTime(e.created_at)}</span>
          </div>
          {e.message && (
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5 break-words">{e.message}</p>
          )}
        </li>
      ))}
    </ol>
  )
}

function ActivityPayload({ children, mono = false, muted = false }) {
  if (!children) return null
  const cls = [
    mono ? 'font-mono' : '',
    muted ? 'text-gray-500 dark:text-gray-400' : 'text-gray-600 dark:text-gray-300',
    'text-[11px] mt-1 whitespace-pre-wrap break-words leading-relaxed',
  ].filter(Boolean).join(' ')
  return <pre className={cls}>{children}</pre>
}

// HIVE-175: live runtime activity — what the agent is actually doing right now
// (user/assistant messages, tool calls, tool results), newest first.
export function ActivityRow({ e }) {
  const ts = e.ts ? relativeTime(e.ts) : ''
  if (e.type === 'tool_call') {
    return (
      <li className="ml-4">
        <span className="absolute -left-1.5 mt-1.5 h-3 w-3 rounded-full bg-amber-500 ring-4 ring-white dark:ring-gray-800" />
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-xs font-semibold text-amber-700 dark:text-amber-400">tool call · {e.tool}</span>
          <span className="text-[11px] text-gray-400">{ts}</span>
        </div>
        <ActivityPayload muted>{e.input}</ActivityPayload>
      </li>
    )
  }
  if (e.type === 'tool_output') {
    const stream = e.stream === 'stderr' ? 'stderr' : e.stream === 'stdout' ? 'stdout' : 'output'
    return (
      <li className="ml-4">
        <span className={`absolute -left-1.5 mt-1.5 h-3 w-3 rounded-full ring-4 ring-white dark:ring-gray-800 ${stream === 'stderr' ? 'bg-red-400' : 'bg-indigo-400'}`} />
        <div className="flex items-center gap-2 flex-wrap">
          <span className={`text-xs font-medium ${stream === 'stderr' ? 'text-red-600 dark:text-red-400' : 'text-indigo-600 dark:text-indigo-400'}`}>{stream} · {e.tool || 'shell'}</span>
          <span className="text-[11px] text-gray-400">{ts}</span>
        </div>
        <ActivityPayload mono>{e.output}</ActivityPayload>
      </li>
    )
  }
  if (e.type === 'tool_result') {
    return (
      <li className="ml-4">
        <span className="absolute -left-1.5 mt-1.5 h-3 w-3 rounded-full bg-gray-400 ring-4 ring-white dark:ring-gray-800" />
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-xs font-medium text-gray-500">tool result · {e.tool || 'result'}</span>
          <span className="text-[11px] text-gray-400">{ts}</span>
        </div>
        <ActivityPayload muted>{e.output}</ActivityPayload>
      </li>
    )
  }
  if (e.type === 'reasoning') {
    return (
      <li className="ml-4">
        <span className="absolute -left-1.5 mt-1.5 h-3 w-3 rounded-full bg-violet-400 ring-4 ring-white dark:ring-gray-800" />
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-xs font-medium text-violet-600 dark:text-violet-400">reasoning</span>
          <span className="text-[11px] text-gray-400">{ts}</span>
        </div>
        <ActivityPayload>{e.text}</ActivityPayload>
      </li>
    )
  }
  if (e.type === 'turn_summary') {
    const tools = Array.isArray(e.tool_names) ? e.tool_names.filter(Boolean) : []
    const excerpt = typeof e.assistant_excerpt === 'string' ? e.assistant_excerpt.trim() : ''
    const noTools = !tools.length && !(e.tool_calls > 0)
    const highPrefill = Number(e.input_tokens) >= 80000
    return (
      <li className="ml-4">
        <span className={`absolute -left-1.5 mt-1.5 h-3 w-3 rounded-full ring-4 ring-white dark:ring-gray-800 ${noTools && highPrefill ? 'bg-amber-400' : 'bg-slate-300'}`} />
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-xs font-medium text-gray-500">turn summary</span>
          {noTools && highPrefill && (
            <span className="text-[10px] font-semibold uppercase tracking-wide text-amber-700 dark:text-amber-400">no tools</span>
          )}
          {tools.length > 0 && (
            <span className="text-[10px] font-mono text-indigo-600 dark:text-indigo-400">{tools.join(', ')}</span>
          )}
          <span className="text-[11px] text-gray-400">{ts}</span>
        </div>
        {excerpt && <ActivityPayload>{excerpt}</ActivityPayload>}
        <ActivityPayload muted>{e.text}</ActivityPayload>
      </li>
    )
  }
  const isUser = e.role === 'user'
  const isSystem = e.role === 'system'
  const label = isUser ? 'user' : isSystem ? 'system' : 'assistant'
  return (
    <li className="ml-4">
      <span className={`absolute -left-1.5 mt-1.5 h-3 w-3 rounded-full ring-4 ring-white dark:ring-gray-800 ${isUser ? 'bg-blue-400' : isSystem ? 'bg-gray-300' : 'bg-green-500'}`} />
      <div className="flex items-center gap-2 flex-wrap">
        <span className={`text-xs font-semibold ${isUser ? 'text-blue-600 dark:text-blue-400' : isSystem ? 'text-gray-500' : 'text-green-700 dark:text-green-400'}`}>{label}</span>
        <span className="text-[11px] text-gray-400">{ts}</span>
      </div>
      <ActivityPayload>{e.text}</ActivityPayload>
    </li>
  )
}

export function ActivityTimeline({ data, loading, rate, error }) {
  const eventTime = (e) => (e?.ts ? new Date(e.ts).getTime() || 0 : 0)
  const events = [...(data?.events || [])].sort((a, b) => eventTime(b) - eventTime(a))
  const tokPerSec = liveTokenRate(rate)
  const sourceState = data?.source_state
  const persisted = sourceState === 'persisted'
  const degraded = sourceState === 'degraded' || sourceState === 'unavailable' || data?.degraded
  const historyEvents = data?.history_events || []
  const fetchFailed = Boolean(error) && !events.length && !historyEvents.length
  return (
    <div className="mt-6">
      <div className="flex items-center justify-between mb-1">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold"><Activity className="h-4 w-4" />Live activity</h3>
        <div className="flex items-center gap-2 text-[11px] text-gray-500">
          <span>Newest first</span>
          {tokPerSec != null && (
            <span className="font-mono rounded bg-gray-100 dark:bg-gray-700 px-1.5 py-0.5">{tokPerSec} tok/s</span>
          )}
          {data?.total != null && <span>{data.total} events</span>}
        </div>
      </div>
      {loading && !events.length && !historyEvents.length ? (
        <LoadingState label="Loading activity…" />
      ) : persisted && events.length ? (
        <>
          <div className="rounded border border-amber-300/70 bg-amber-50 px-2.5 py-2 text-[11px] text-amber-700 dark:border-amber-700/50 dark:bg-amber-900/20 dark:text-amber-400">
            Showing last recorded activity — this history survives restart and hibernate.
            {data?.persisted_at && <span> Saved {relativeTime(data.persisted_at)}.</span>}
            {' '}Live transcript is not currently readable.
          </div>
          <ol className="relative border-l border-gray-200 dark:border-gray-700 ml-2 space-y-3 py-2 max-h-[28rem] overflow-auto pr-1 mt-2">
            {events.map((e, i) => <ActivityRow key={i} e={e} />)}
          </ol>
        </>
      ) : (degraded || fetchFailed) && !events.length ? (
        <>
          <div className="rounded border border-amber-300/70 bg-amber-50 px-2.5 py-2 text-[11px] text-amber-700 dark:border-amber-700/50 dark:bg-amber-900/20 dark:text-amber-400">
            {sourceState === 'unavailable' || fetchFailed
              ? 'Live activity source is unavailable — the runtime transcript could not be read.'
              : 'Waiting for the next turn — this session has no live events yet (reset or just started).'}
            {historyEvents.length > 0 && (
              <span> Showing {historyEvents.length} historical event{historyEvents.length === 1 ? '' : 's'} below (not live).</span>
            )}
          </div>
          {historyEvents.length > 0 && (
            <div className="mt-3">
              <h4 className="text-[11px] font-semibold text-gray-500 uppercase tracking-wide mb-1">Historical (not live)</h4>
              <ol className="relative border-l border-gray-200 dark:border-gray-700 ml-2 space-y-3 py-2 max-h-[16rem] overflow-auto pr-1 opacity-70">
                {historyEvents.map((e, i) => <ActivityRow key={`h-${i}`} e={e} />)}
              </ol>
            </div>
          )}
        </>
      ) : !events.length && historyEvents.length ? (
        <>
          <div className="rounded border border-amber-300/70 bg-amber-50 px-2.5 py-2 text-[11px] text-amber-700 dark:border-amber-700/50 dark:bg-amber-900/20 dark:text-amber-400">
            Waiting for the next turn — this session has no live events yet (reset or just started).
            <span> Showing {historyEvents.length} historical event{historyEvents.length === 1 ? '' : 's'} below (not live).</span>
          </div>
          <div className="mt-3">
            <h4 className="text-[11px] font-semibold text-gray-500 uppercase tracking-wide mb-1">Historical (not live)</h4>
            <ol className="relative border-l border-gray-200 dark:border-gray-700 ml-2 space-y-3 py-2 max-h-[16rem] overflow-auto pr-1 opacity-70">
              {historyEvents.map((e, i) => <ActivityRow key={`h-${i}`} e={e} />)}
            </ol>
          </div>
        </>
      ) : !events.length ? (
        <p className="text-sm text-gray-500 py-4">No recent activity recorded.</p>
      ) : (
        <>
          {Date.now() - eventTime(events[0]) > 60 * 60 * 1000 && (
            <div className="mb-2 rounded border border-amber-300/70 bg-amber-50 px-2.5 py-1.5 text-[11px] text-amber-700 dark:border-amber-700/50 dark:bg-amber-900/20 dark:text-amber-400">
              Newest recorded event is {relativeTime(events[0].ts)} — if this agent is running, its live session source may not be ingested yet.
            </div>
          )}
          <ol className="relative border-l border-gray-200 dark:border-gray-700 ml-2 space-y-3 py-2 max-h-[28rem] overflow-auto pr-1">
            {events.map((e, i) => <ActivityRow key={i} e={e} />)}
          </ol>
          {historyEvents.length > 0 && (
            <div className="mt-3">
              <h4 className="text-[11px] font-semibold text-gray-500 uppercase tracking-wide mb-1">Historical (not live)</h4>
              <ol className="relative border-l border-gray-200 dark:border-gray-700 ml-2 space-y-3 py-2 max-h-[16rem] overflow-auto pr-1 opacity-70">
                {historyEvents.map((e, i) => <ActivityRow key={`h-${i}`} e={e} />)}
              </ol>
            </div>
          )}
        </>
      )}
    </div>
  )
}

export function Shell({ onClose, title, subtitle, status, children, actions, avatar }) {
  const dialogRef = useRef(null)
  const closeButtonRef = useRef(null)
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose

  // WAI-ARIA modal behavior: enter focus, contain Tab, make the app behind the
  // portaled drawer inert, close the top layer on Escape, then restore focus.
  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return undefined
    const previousFocus = document.activeElement
    const restoreBackground = inertBodySiblings(dialog)
    closeButtonRef.current?.focus()
    const onKey = (event) => {
      // Nested confirmations/editors own their keyboard events until closed.
      if (dialog.querySelector('[data-agent-drawer-nested-modal="true"]')) return
      if (event.key === 'Escape') {
        event.preventDefault()
        onCloseRef.current?.()
        return
      }
      cycleDialogFocus(event, dialog)
    }
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('keydown', onKey)
      restoreBackground()
      if (previousFocus?.isConnected) previousFocus.focus()
    }
  }, [])

  // HIVE-732 (HIVE-677 blocker): portal the overlay to <body> so its
  // `fixed inset-0 z-[120]` escapes the app shell's `relative z-10` stacking
  // context (the Layout main-content wrapper). Rendered inline, the drawer's
  // z-index was capped INSIDE that z-10 context — beneath the z-50 sidebar and
  // page chrome — so "Open details" toggled `selected` state but produced no
  // visibly-on-top drawer. No overlay in the app was portaled; portaling to
  // document.body is the standard fix for a full-screen modal/drawer and makes
  // this a true top-layer surface regardless of ancestor stacking/transform.
  return createPortal(
    <div
      ref={dialogRef}
      data-agent-drawer-root="true"
      className="fixed inset-0 z-[120] flex justify-end"
      role="dialog"
      aria-modal="true"
      aria-label={title || 'Agent details'}
      tabIndex={-1}
    >
      <div data-agent-drawer-backdrop="true" aria-hidden="true" className="absolute inset-0 bg-black/40" onClick={onClose} />
      <div data-agent-drawer-panel="true" className="relative w-full max-w-md h-full bg-white dark:bg-gray-800 shadow-2xl border-l border-gray-200 dark:border-gray-700 flex flex-col">
        <div className="flex items-start justify-between p-5 border-b border-gray-200 dark:border-gray-700">
          <div className="flex min-w-0 items-start gap-3">
            {avatar}
            <div className="min-w-0">
              <h2 className="text-lg font-bold truncate">{title}</h2>
              <p className="text-sm text-gray-500 truncate">{subtitle}</p>
              <div className="mt-2">{status}</div>
            </div>
          </div>
          <button ref={closeButtonRef} type="button" aria-label="Close agent details" onClick={onClose} className="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700">
            <X className="h-5 w-5" />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto p-5">{children}</div>
        {actions && (
          <div className="p-4 border-t border-gray-200 dark:border-gray-700">{actions}</div>
        )}
      </div>
    </div>,
    document.body,
  )
}

function ActionButton({ icon: Icon, label, onClick, disabled, variant = 'default' }) {
  const styles = {
    default: 'border border-gray-300 dark:border-gray-600 hover:bg-gray-100 dark:hover:bg-gray-700',
    primary: 'bg-primary-600 text-white hover:bg-primary-700',
    danger: 'border border-red-300 dark:border-red-800 text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20',
  }
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={`inline-flex items-center justify-center gap-1.5 px-3 py-2 text-sm rounded-lg transition-colors disabled:opacity-40 disabled:cursor-not-allowed ${styles[variant]}`}
    >
      <Icon className="h-4 w-4" />
      {label}
    </button>
  )
}

function EffectiveCapabilitiesPanel({ agentId }) {
  const { data, isLoading, isError } = useEffectiveCapabilities(agentId, { enabled: !!agentId })
  if (isLoading) return <LoadingState label="Loading effective capabilities…" />
  if (isError) return <p className="text-xs text-red-500 py-3">Effective capabilities unavailable.</p>
  const caps = data?.capabilities || []
  return (
    <div className="mt-6 rounded-lg border border-gray-200 bg-gray-50 p-3 dark:border-gray-700 dark:bg-gray-900/30">
      <h3 className="mb-2 flex items-center gap-1.5 text-sm font-semibold"><ShieldAlert className="h-4 w-4" />Effective capabilities</h3>
      <p className="mb-2 text-xs text-gray-500">
        Runtime grants only. Human-readable role titles do not grant skills, MCP servers, profiles, flags, or credentials.
      </p>
      {!caps.length ? (
        <p className="text-sm text-gray-500">No active capabilities resolved from team membership.</p>
      ) : (
        <div className="flex flex-wrap gap-1.5">
          {caps.map((cap) => (
            <span key={cap.slug} className="rounded-full bg-primary-50 px-2 py-1 text-xs font-medium text-primary-700 dark:bg-primary-900/20 dark:text-primary-300" title={(cap.sources || []).join(', ')}>
              {cap.display_name || cap.slug}
            </span>
          ))}
        </div>
      )}
      {(data?.skills?.length || data?.enabled_mcp_servers?.length) && (
        <p className="mt-2 text-xs text-gray-500">
          Skills: {(data.skills || []).join(', ') || '—'} · MCP: {(data.enabled_mcp_servers || []).join(', ') || '—'}
        </p>
      )}
      {(data?.diagnostics || []).map((diag, i) => (
        <p key={i} className="mt-1 text-xs text-amber-600 dark:text-amber-400">{diag.message || String(diag)}</p>
      ))}
    </div>
  )
}

function teamRoleLabel(team, fallbackTitle = 'Agent') {
  return team?.role_title || fallbackTitle || 'Agent'
}

function RolesByTeamPanel({ agentId, fallbackTitle, enabled = true }) {
  const { currentOrgSlug } = useHive()
  const { data, isLoading, isError } = useFleetTeamPlacement(agentId, { enabled: !!agentId && enabled, orgSlug: currentOrgSlug })
  if (!enabled) return null
  if (isLoading) return <LoadingState label="Loading team roles…" />
  if (isError) return <p className="text-xs text-red-500 py-3">Team-scoped roles unavailable.</p>
  const roles = (data?.teams || []).filter((team) => team.selected)
  return (
    <div className="mt-6 rounded-lg border border-gray-200 bg-white p-3 dark:border-gray-700 dark:bg-gray-800/60">
      <h3 className="mb-2 flex items-center gap-1.5 text-sm font-semibold"><Users className="h-4 w-4" />Roles by team</h3>
      <p className="mb-2 text-xs text-gray-500">
        Display-only membership context. Runtime permissions are listed separately under Effective capabilities.
      </p>
      {roles.length ? (
        <div className="space-y-2">
          {roles.map((team) => (
            <div key={team.id} className="flex items-center justify-between gap-2 rounded border border-gray-100 px-2 py-1.5 text-sm dark:border-gray-700">
              <div className="min-w-0">
                <div className="truncate font-medium">{teamDisplayLabel(team)}</div>
                <div className="truncate text-xs text-gray-500">
                  {teamRoleLabel(team, fallbackTitle)} · {team.membership_role || 'member'}
                  {team.tier ? ` · tier ${team.tier}` : ''}
                </div>
              </div>
              {team.team_role_source === 'fallback' && (
                <span className="shrink-0 rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-500 dark:bg-gray-700 dark:text-gray-300">fallback</span>
              )}
            </div>
          ))}
        </div>
      ) : (
        <p className="text-sm text-gray-500">No team memberships yet.</p>
      )}
    </div>
  )
}

// HIVE-195 Phase 2: top-down config editor. Edits PATCH /v1/fleet/agents/{id}/,
// which pushes Hive -> fleet daemon -> agent. Normal runtime edits are one
// primary execution method + model pair; fallback chains are advanced/legacy
// compatibility and are not part of the everyday editor.

const RUNTIME_METHOD_OPTIONS = [
  { id: 'codex_app_server', label: 'Codex' },
  { id: 'claude_code_server', label: 'Claude Code' },
  { id: 'antigravity_server', label: 'Antigravity' },
  { id: 'openclaw_app_server', label: 'OpenClaw' },
  { id: 'grok_build', label: 'Grok Build' },
  { id: 'shizuha', label: 'Shizuha CLI' },
]

const CLAUDE_MODEL_OPTIONS = [
  { id: 'claude-opus-4-8', label: 'Opus 4.8 (claude-opus-4-8)' },
  { id: 'claude-sonnet-4-6', label: 'Sonnet 4.6 (claude-sonnet-4-6)' },
  { id: 'claude-haiku-4-5', label: 'Haiku 4.5 (claude-haiku-4-5)' },
]

const ANTIGRAVITY_MODEL_OPTIONS = [
  { id: 'claude-opus-4.6', label: 'Claude Opus 4.6 Thinking (claude-opus-4.6)' },
  { id: 'claude-opus-4.6-thinking', label: 'Claude Opus 4.6 Thinking (claude-opus-4.6-thinking)' },
  { id: 'gemini-2.5-pro', label: 'Gemini 2.5 Pro (gemini-2.5-pro)' },
  { id: 'gemini-2.5-flash', label: 'Gemini 2.5 Flash (gemini-2.5-flash)' },
]

const CODEX_MODEL_OPTIONS = [
  { id: 'gpt-5.6-sol', label: 'GPT-5.6 Sol (gpt-5.6-sol)' },
  { id: 'gpt-5.5', label: 'GPT-5.5 (gpt-5.5)' },
  { id: 'gpt-5.3-codex-spark', label: 'GPT-5.3 Codex Spark (gpt-5.3-codex-spark)' },
]

const GROK_BUILD_MODEL_OPTIONS = [
  { id: 'cortex/grok-4.7', label: 'Grok 4.7 (via Cortex SuperGrok / Grok Build OIDC)' },
  { id: 'xai:grok-4.7', label: 'Grok 4.7 (direct xAI id)' },
  { id: 'grok-4.7', label: 'Grok 4.7 (bare offer id)' },
  { id: 'cortex/grok-4.5', label: 'Grok 4.5 high (via Cortex SuperGrok / Grok Build OIDC)' },
  { id: 'xai:grok-4.5', label: 'Grok 4.5 (Hive xAI TokenPool lease)' },
  { id: 'grok-4.5', label: 'Grok 4.5 (bare offer id)' },
]

function currentRuntimeMethod(agent) {
  return agent.execution_method || agent.model_fallbacks?.[0]?.method || 'shizuha'
}

function currentRuntimeModel(agent, method) {
  const currentMethod = agent.execution_method || agent.model_fallbacks?.[0]?.method || 'shizuha'
  if (method === currentMethod && (agent.effective_model || agent.model)) return agent.effective_model || agent.model
  const overrides = agent.model_overrides || {}
  const fallback = (agent.model_fallbacks || []).find((entry) => entry.method === method)
  return overrides[method] || fallback?.model || (method === currentMethod ? (agent.effective_model || agent.model || '') : '')
}

function currentReasoningEffort(agent, method) {
  const fallback = (agent.model_fallbacks || []).find((entry) => entry.method === method && entry.model === currentRuntimeModel(agent, method))
    || (agent.model_fallbacks || []).find((entry) => entry.method === method)
  return agent.reasoning_effort
    || fallback?.reasoningEffort
    || agent.model_overrides?.[`${method}_reasoning_effort`]
    || ''
}

function modelOptionsFor(method, runtimeOptions) {
  const byMethod = runtimeOptions?.method_models || {}
  if (Array.isArray(byMethod[method]) && byMethod[method].length) return byMethod[method]
  if (method === 'claude_code_server') return runtimeOptions?.claude_models || CLAUDE_MODEL_OPTIONS
  if (method === 'antigravity_server') return runtimeOptions?.antigravity_models || ANTIGRAVITY_MODEL_OPTIONS
  if (method === 'codex_app_server' || method === 'openclaw_app_server') return runtimeOptions?.codex_models || CODEX_MODEL_OPTIONS
  if (method === 'grok_build') return runtimeOptions?.grok_build_models || GROK_BUILD_MODEL_OPTIONS
  if (method === 'shizuha') return runtimeOptions?.cortex_models || []
  return []
}

function defaultModelFor(method, currentModel) {
  if (currentModel) return currentModel
  if (method === 'antigravity_server') return 'claude-opus-4.6'
  if (method === 'codex_app_server') return 'gpt-5.6-sol'
  if (method === 'claude_code_server') return 'claude-sonnet-4-6'
  // Grok Build harness → Cortex SuperGrok (Grok Build OIDC on managed xai-grok).
  if (method === 'grok_build') return 'cortex/grok-4.7'
  // Preferred Shizuha/Cortex managed path when converting from Codex/Claude.
  if (method === 'shizuha') return 'cortex/grok-4.7'
  return ''
}

function defaultReasoningEffortFor(method) {
  if (method === 'grok_build') return 'medium'
  // High is the cost-aware default; role policy may raise the few seats that
  // benefit materially from maximum reasoning.
  if (method === 'codex_app_server') return 'high'
  return undefined
}

function jsonText(value) {
  if (value == null || value === '') return ''
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return ''
  }
}

function parseJsonField(text, field) {
  const result = validateJsonField(text, field)
  if (!result.valid) throw new Error(`${field.label}: ${result.error}`)
  return result.value
}

export function RuntimeConfigEditor({ agent, mutation, canEdit }) {
  // Hive is SoT for k3s-native fleet rows AND rt-fleet/external telemetry
  // seats. Gating on rt-fleet/has_telemetry hid the editor for native
  // agents (Aoi: k3s-native, hibernated) so staff could not change harness.
  const isRuntime = agent.runtime_source === 'rt-fleet'
    || agent.runtime_source === 'k3s-native'
    || agent.lifecycle_managed === true
    || agent.has_telemetry === true
  const { data: options } = useFleetRuntimeOptions({ enabled: isRuntime && canEdit })
  const method = currentRuntimeMethod(agent)
  const advancedFields = advancedFieldsForAgent(agent)
  const capabilityImpact = capabilityImpactForAgent(agent)
  const currentResourceConfig = resourceConfigForAgent(agent)
  const [form, setForm] = useState(() => ({
    role: agent.role || '',
    execution_method: method,
    model: currentRuntimeModel(agent, method),
    reasoning_effort: currentReasoningEffort(agent, method),
    context_prompt: agent.context_prompt || '',
    skills: agent.skills || agent.skill_config?.skills || [],
    eager_skills: agent.eager_skills || agent.skill_config?.eager_skills || [],
    enabled_mcp_server_ids: agent.enabled_mcp_server_ids
      || agent.mcp_config?.enabled_mcp_server_ids
      || (agent.mcp_servers || agent.mcp_config?.mcp_servers || []).map((s) => s.slug || s.name).filter(Boolean),
    runtime_environment: agent.runtime_environment || 'container',
    tier: agent.tier || agent.resource_profile || 'normal',
    resource_limits: jsonText(currentResourceConfig),
    env: jsonText(agent.env || {}),
    personality_traits: jsonText(agent.personality_traits || {}),
    token_budget: jsonText(agent.token_budget || {}),
    work_schedule: jsonText(agent.work_schedule || {}),
    agent_memory: agent.agent_memory || '',
    warm_pool_size: agent.warm_pool_size || 0,
  }))
  const [saveState, setSaveState] = useState('idle') // idle | saving | saved
  const saveTimer = useRef(null)
  const firstRun = useRef(true)
  // Hive is the source of truth; every accepted edit persists immediately (the
  // impact badge on each field states the runtime consequence). Diff the form
  // against the agent and PATCH only what changed — no batch "Save & push", no
  // separate Edit mode, the config is always live.
  const autoSave = () => {
    const invalid = advancedFields.filter((field) => !validateJsonField(form[field.key], field).valid).length
    if (invalid > 0) return // wait until the JSON overrides are valid
    const payload = {}
    try {
      if (form.role !== (agent.role || '')) payload.role = form.role
      if (form.execution_method !== method) payload.execution_method = form.execution_method
      const nextModel = defaultModelFor(form.execution_method, form.model)
      const currentEffort = currentReasoningEffort(agent, method)
      const primaryChanged = nextModel !== currentRuntimeModel(agent, form.execution_method)
        || form.execution_method !== method
        || form.reasoning_effort !== currentEffort
      if (primaryChanged) {
        payload.model = nextModel
        payload.reasoning_effort = form.reasoning_effort
      }
      if (form.context_prompt !== (agent.context_prompt || '')) payload.context_prompt = form.context_prompt
      const skills = form.skills || []
      if (skills.join(',') !== (agent.skills || agent.skill_config?.skills || []).join(',')) payload.skills = skills
      const eager = form.eager_skills || []
      if (eager.join(',') !== (agent.eager_skills || agent.skill_config?.eager_skills || []).join(',')) payload.eager_skills = eager
      const mcp = form.enabled_mcp_server_ids || []
      const currentMcp = agent.enabled_mcp_server_ids
        || agent.mcp_config?.enabled_mcp_server_ids
        || (agent.mcp_servers || agent.mcp_config?.mcp_servers || []).map((s) => s.slug || s.name).filter(Boolean)
      if (mcp.join(',') !== currentMcp.join(',')) payload.enabled_mcp_server_ids = mcp
      if (form.runtime_environment !== (agent.runtime_environment || 'container')) payload.runtime_environment = form.runtime_environment
      if (form.tier !== (agent.tier || agent.resource_profile || 'normal')) payload.tier = form.tier
      const resourceField = advancedFields.find((field) => field.key === 'resource_limits')
      const resourceLimits = parseJsonField(form.resource_limits, resourceField)
      if (JSON.stringify(resourceLimits) !== JSON.stringify(currentResourceConfig)) payload[resourceField.payloadKey] = resourceLimits
      const env = parseJsonField(form.env, advancedFields.find((field) => field.key === 'env'))
      if (JSON.stringify(env) !== JSON.stringify(agent.env || {})) payload.env = env
      const personality = parseJsonField(form.personality_traits, advancedFields.find((field) => field.key === 'personality_traits'))
      if (JSON.stringify(personality) !== JSON.stringify(agent.personality_traits || {})) payload.personality_traits = personality
      const tokenBudget = parseJsonField(form.token_budget, advancedFields.find((field) => field.key === 'token_budget'))
      if (JSON.stringify(tokenBudget) !== JSON.stringify(agent.token_budget || {})) payload.token_budget = tokenBudget
      const workSchedule = parseJsonField(form.work_schedule, advancedFields.find((field) => field.key === 'work_schedule'))
      if (JSON.stringify(workSchedule) !== JSON.stringify(agent.work_schedule || {})) payload.work_schedule = workSchedule
      if (form.agent_memory !== (agent.agent_memory || '')) payload.agent_memory = form.agent_memory
      if (Number(form.warm_pool_size || 0) !== Number(agent.warm_pool_size || 0)) payload.warm_pool_size = Number(form.warm_pool_size || 0)
    } catch (e) {
      return // invalid intermediate state — skip until it settles
    }
    if (Object.keys(payload).length === 0) return
    setSaveState('saving')
    mutation.mutate(payload, {
      onSuccess: () => setSaveState('saved'),
      onError: () => setSaveState('idle'),
    })
  }
  useEffect(() => {
    if (firstRun.current) { firstRun.current = false; return }
    if (saveTimer.current) clearTimeout(saveTimer.current)
    saveTimer.current = setTimeout(autoSave, 900)
    return () => { if (saveTimer.current) clearTimeout(saveTimer.current) }
  }, [form])
  if (!isRuntime || !canEdit) return null
  const cls = 'w-full rounded-lg border border-gray-300 bg-white px-2.5 py-2 text-sm outline-none focus:border-primary-500 focus:ring-1 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800'
  const modelOptions = form ? modelOptionsFor(form.execution_method, options) : []
  const pendingKeys = Array.isArray(agent.pending_config_keys) ? agent.pending_config_keys : []
  const advancedSetCount = form ? advancedFields.filter((field) => jsonFieldIsSet(form[field.key])).length : 0
  const invalidJsonCount = form ? advancedFields.filter((field) => !validateJsonField(form[field.key], field).valid).length : 0
  return (
    <div className="mt-6 overflow-hidden rounded-xl border border-gray-200 bg-gray-50/50 dark:border-gray-700 dark:bg-gray-900/20">
      <div className="flex items-start justify-between gap-3 border-b border-gray-200 bg-white px-3 py-3 dark:border-gray-700 dark:bg-gray-800/70">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="flex items-center gap-1.5 text-sm font-semibold"><Pencil className="h-4 w-4" />Agent configuration</h3>
            {pendingKeys.length > 0 && (
              <span title={`Waiting for runtime confirmation: ${pendingKeys.join(', ')}`} className="animate-pulse rounded-full bg-blue-50 px-2 py-0.5 text-[10px] font-semibold text-blue-600 ring-1 ring-blue-200 dark:bg-blue-900/20 dark:text-blue-300 dark:ring-blue-800">syncing {pendingKeys.length}</span>
            )}
          </div>
          <p className="mt-0.5 text-[11px] text-gray-500">Hive saves every accepted edit immediately; impact badges show what happens to the runtime.</p>
        </div>
        <span className="shrink-0 text-[11px] font-medium text-gray-400" aria-live="polite">
          {saveState === 'saving' ? 'Saving…' : saveState === 'saved' ? 'Saved ✓' : ''}
        </span>
      </div>
      <div className="space-y-3 p-3">
          <div className="flex flex-wrap gap-2 rounded-lg border border-gray-200 bg-white p-2.5 dark:border-gray-700 dark:bg-gray-800/50">
            {Object.entries(CHANGE_IMPACTS).map(([key, impact]) => (
              <span key={key} className="inline-flex items-center gap-1.5 text-[10px] text-gray-500"><ImpactBadge type={key} />{impact.description}</span>
            ))}
          </div>

          <EditGroup icon={Tag} title="Identity" description="The role people see across Hive and team surfaces." impact="instant">
            <label className="block">
              <FormLabel impact="instant">Default display role</FormLabel>
              <input className={cls} value={form.role} onChange={(event) => setForm({ ...form, role: event.target.value })} placeholder="Engineer, Researcher, Operations…" />
            </label>
          </EditGroup>

          <EditGroup icon={Cpu} title="Model & compute" description="Choose the harness, model, and reasoning level used for the next turn." impact="restart">
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <label className="block">
                <FormLabel impact="restart">Runtime harness</FormLabel>
              <select
                className={cls}
                value={form.execution_method}
                  onChange={(event) => {
                    const nextMethod = event.target.value
                    setForm({
                      ...form,
                      execution_method: nextMethod,
                      model: defaultModelFor(nextMethod, currentRuntimeModel(agent, nextMethod)),
                      reasoning_effort: currentReasoningEffort(agent, nextMethod) || defaultReasoningEffortFor(nextMethod) || '',
                    })
                }}
              >
                {(options?.runtime_methods || RUNTIME_METHOD_OPTIONS).map((m) => <option key={m.id} value={m.id}>{m.label} ({m.id})</option>)}
                {form.execution_method && !(options?.runtime_methods || RUNTIME_METHOD_OPTIONS).some((m) => m.id === form.execution_method) && (
                  <option value={form.execution_method}>{form.execution_method} (current)</option>
                )}
              </select>
              </label>
              <label className="block">
                <FormLabel impact="restart">Model</FormLabel>
                {modelOptions.length ? (
                  <select className={cls} value={form.model} onChange={(event) => setForm({ ...form, model: event.target.value })}>
                    <option value="">Runtime default</option>
                    {modelOptions.map((model) => {
                      const unavailable = model.available === false || model.status === 'unavailable' || model.status === 'maintenance'
                      return <option key={model.id} value={model.id} disabled={unavailable}>{model.label}{unavailable ? ' (unavailable)' : ''}</option>
                    })}
                    {form.model && !modelOptions.some((model) => model.id === form.model) && (
                      <option value={form.model}>{form.model} (current)</option>
                    )}
                  </select>
                ) : (
                  <input className={cls} value={form.model} onChange={(event) => setForm({ ...form, model: event.target.value })} placeholder="Runtime default" />
                )}
              </label>
              <label className="block sm:col-span-2">
                <FormLabel impact="restart" hint="Leave Auto to use the harness default.">Reasoning effort</FormLabel>
                <select className={cls} value={form.reasoning_effort} onChange={(event) => setForm({ ...form, reasoning_effort: event.target.value })}>
                  <option value="">Auto</option>
                  {Array.from(new Set(['low', 'medium', 'high', 'xhigh', 'ultra', 'max', form.reasoning_effort])).filter(Boolean).map((effort) => <option key={effort} value={effort}>{humanize(effort)}</option>)}
                </select>
              </label>
            </div>
            {agent.effective_model && (
              <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-emerald-200 bg-emerald-50 px-2.5 py-2 text-[11px] text-emerald-700 dark:border-emerald-800 dark:bg-emerald-900/20 dark:text-emerald-300">
                <span className="inline-flex items-center gap-1.5"><Activity className="h-3.5 w-3.5" />Running now</span>
                <span className="font-mono">{agent.effective_model}</span>
              </div>
            )}
          </EditGroup>

          <EditGroup icon={Server} title="Runtime" description="Placement and capacity controls. Each field shows when it takes effect.">
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <label className="block">
                <FormLabel impact="instant">Runtime environment</FormLabel>
                {/* The fleet daemon, agent runtime and every agent run entirely on
                    k3s — the runtime environment is fixed, not a per-agent choice,
                    so this is read-only rather than a dropdown offering non-k3s
                    (bare_metal/…) options that can't apply. */}
                <div className={`${cls} flex items-center text-white/60 cursor-default`} title="All fleet agents run as k3s-native pods">
                  k3s-native{agent.sandbox_required ? ' · sandbox' : agent.isolated ? ' · restricted' : ' · container'}
                </div>
              </label>
              <label className="block">
                <FormLabel impact="instant">Profile / tier</FormLabel>
                <select className={cls} value={form.tier} onChange={(event) => setForm({ ...form, tier: event.target.value })}>
                  {Array.from(new Set([...(options?.tiers || []), form.tier || 'normal'])).filter(Boolean).map((item) => <option key={item} value={item}>{humanize(item)}</option>)}
                </select>
              </label>
              <label className="block sm:col-span-2">
                <FormLabel impact="instant" hint="Pre-warmed runtime instances (0–16).">Warm pool size</FormLabel>
                <input type="number" min="0" max="16" className={cls} value={form.warm_pool_size} onChange={(event) => setForm({ ...form, warm_pool_size: event.target.value })} />
              </label>
            </div>
          </EditGroup>

          <EditGroup icon={Layers} title="Skills & MCP" description="Select capabilities as chips instead of editing comma-separated identifiers." impact={capabilityImpact}>
            <ChipPicker id="agent-skills" label="Skills" values={form.skills} options={options?.skills || []} onChange={(skills) => setForm({ ...form, skills })} helper="Start typing to choose from the runtime catalog, or add an installed skill by name." impact={capabilityImpact} />
            <ChipPicker id="agent-eager-skills" label="Eager skills" values={form.eager_skills} options={options?.skills || []} onChange={(eager_skills) => setForm({ ...form, eager_skills })} helper="Eager skills are always preloaded into the agent context." impact={capabilityImpact} />
            <ChipPicker id="agent-mcp-servers" label="MCP servers" values={form.enabled_mcp_server_ids || []} options={options?.mcp_servers || []} onChange={(enabled_mcp_server_ids) => setForm({ ...form, enabled_mcp_server_ids })} helper="Only servers discovered from the managed runtime catalog can be selected." allowCustom={false} impact={capabilityImpact} />
          </EditGroup>

          <EditGroup icon={MessageCircle} title="Agent brain" description="Standing instructions and durable memory, with room to write comfortably.">
            <ContextPromptEditor value={form.context_prompt} onChange={(context_prompt) => setForm({ ...form, context_prompt })} />
            <label className="block">
              <FormLabel impact="instant">Agent memory</FormLabel>
              <textarea rows="4" className={`${cls} resize-y`} value={form.agent_memory} onChange={(event) => setForm({ ...form, agent_memory: event.target.value })} placeholder="Durable notes the agent carries across sessions" />
            </label>
          </EditGroup>

          <details className="group overflow-hidden rounded-xl border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-800/40">
            <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-3 py-3 marker:hidden">
              <span className="flex min-w-0 items-start gap-2">
                <Braces className="mt-0.5 h-4 w-4 shrink-0 text-gray-400" />
                <span>
                  <span className="block text-xs font-semibold text-gray-700 dark:text-gray-200">Advanced overrides</span>
                  <span className="mt-0.5 block text-[11px] leading-4 text-gray-500">Validated JSON for rare runtime and policy overrides.</span>
                </span>
              </span>
              <span className="shrink-0 rounded-full bg-gray-100 px-2 py-0.5 text-[10px] text-gray-500 dark:bg-gray-700 dark:text-gray-300">
                {invalidJsonCount ? `${invalidJsonCount} invalid` : advancedSetCount ? `${advancedSetCount} set` : 'Collapsed'}
              </span>
            </summary>
            <div className="space-y-2.5 border-t border-gray-100 p-3 dark:border-gray-700">
              {advancedFields.map((field) => (
                <JsonField key={field.key} field={field} value={form[field.key]} onChange={(value) => setForm({ ...form, [field.key]: value })} />
              ))}
            </div>
          </details>

          {invalidJsonCount > 0 && (
            <p className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700 dark:border-amber-800 dark:bg-amber-900/20 dark:text-amber-300">
              {invalidJsonCount} advanced JSON field{invalidJsonCount === 1 ? '' : 's'} invalid — fix to auto-save.
            </p>
          )}
          {mutation.isError && (
            <p className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-600 dark:border-red-800 dark:bg-red-900/20 dark:text-red-300">
              Save failed — {mutation.error?.response?.data?.error || mutation.error?.message || 'see backend logs for details'}.
            </p>
          )}
        </div>
    </div>
  )
}

function TeamPlacementEditor({ agentId, mutation, canEdit }) {
  const toast = useToast()
  const { currentOrgSlug } = useHive()
  const { data, isLoading, refetch } = useFleetTeamPlacement(agentId, { enabled: canEdit, orgSlug: currentOrgSlug })
  const teams = data?.teams || []
  const current = teams.filter((team) => team.selected)
  const available = teams.filter((team) => !team.selected)
  const [teamId, setTeamId] = useState('')
  const [newTeamName, setNewTeamName] = useState('')
  const selected = teamId || available[0]?.id || ''

  if (!canEdit) return null

  const apply = (payload, okMsg) => {
    mutation.mutate(payload, {
      onSuccess: () => {
        toast.success(okMsg)
        setTeamId('')
        refetch()
      },
      onError: (e) => toast.error(errorMessage(e)),
    })
  }

  const createTeam = () => {
    const name = newTeamName.trim()
    if (!name) return
    mutation.createTeam.mutate({ team_name: name, org_slug: currentOrgSlug }, {
      onSuccess: () => {
        toast.success('Team created')
        setNewTeamName('')
        refetch()
      },
      onError: (e) => toast.error(errorMessage(e)),
    })
  }

  const editRole = (team) => {
    const nextTitle = window.prompt('Role/title for this team membership', team.role_title || '')
    if (nextTitle === null) return
    const nextMembershipRole = window.prompt('Membership role (member, lead, reviewer, operator, observer, custom)', team.membership_role || 'member')
    if (nextMembershipRole === null) return
    apply(
      {
        action: 'update_role',
        team_id: team.id,
        role_title: nextTitle.trim(),
        membership_role: (nextMembershipRole.trim() || 'member'),
      },
      'Team role updated'
    )
  }

  return (
    <div className="mt-6 rounded-lg border border-gray-200 dark:border-gray-700 p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold"><Users className="h-4 w-4" />Team placement</h3>
        {isLoading && <span className="text-xs text-gray-400">Loading…</span>}
      </div>
      <div className="space-y-2">
        {current.length ? current.map((team) => (
          <div key={team.id} className="flex items-center justify-between gap-2 rounded border border-gray-200 px-2 py-1.5 text-sm dark:border-gray-700">
            <div className="min-w-0">
              <span className="block truncate">{teamDisplayLabel(team)}</span>
              <span className="block truncate text-xs text-gray-500">
                {teamRoleLabel(team, data?.agent?.display_title)} · {team.membership_role || 'member'}
              </span>
            </div>
            <div className="flex shrink-0 items-center gap-1">
              <button
                type="button"
                disabled={mutation.isPending}
                onClick={() => editRole(team)}
                className="rounded px-2 py-1 text-xs text-primary-600 hover:bg-primary-50 disabled:opacity-50 dark:text-primary-300 dark:hover:bg-primary-900/20"
              >
                Edit role
              </button>
              <button
                type="button"
                disabled={mutation.isPending}
                onClick={() => apply({ action: 'remove', team_id: team.id }, 'Removed from team')}
                className="rounded px-2 py-1 text-xs text-red-600 hover:bg-red-50 disabled:opacity-50 dark:text-red-400 dark:hover:bg-red-900/20"
              >
                Remove
              </button>
            </div>
          </div>
        )) : (
          <p className="text-sm text-gray-500">Unassigned</p>
        )}
        <div className="flex gap-2 pt-1">
          <select
            value={selected}
            onChange={(e) => setTeamId(e.target.value)}
            disabled={!available.length || mutation.isPending}
            className="min-w-0 flex-1 rounded border border-gray-300 bg-white px-2 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800"
          >
            {available.map((team) => <option key={team.id} value={team.id}>{teamDisplayLabel(team)}</option>)}
          </select>
          <button
            type="button"
            disabled={!selected || mutation.isPending}
            onClick={() => apply({ action: 'add', team_id: selected, membership_role: 'member' }, 'Added to team')}
            className="rounded bg-primary-600 px-3 py-1.5 text-sm text-white hover:bg-primary-700 disabled:opacity-50"
          >
            Add
          </button>
        </div>
        <div className="flex gap-2 pt-1">
          <input
            value={newTeamName}
            onChange={(e) => setNewTeamName(e.target.value)}
            disabled={mutation.createTeam.isPending}
            placeholder="New team"
            className="min-w-0 flex-1 rounded border border-gray-300 bg-white px-2 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800"
          />
          <button
            type="button"
            disabled={!newTeamName.trim() || mutation.createTeam.isPending}
            onClick={createTeam}
            className="inline-flex items-center gap-1 rounded bg-gray-100 px-3 py-1.5 text-sm hover:bg-gray-200 disabled:opacity-50 dark:bg-gray-700 dark:hover:bg-gray-600"
          >
            <Plus className="h-3.5 w-3.5" />Create
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Fleet drawer ────────────────────────────────────────────────────────────
function FleetDrawer({ id, initialAgent, onClose }) {
  const toast = useToast()
  const { isStaff, isSuperuser, currentOrgSlug } = useHive()
  const { data: liveAgent } = useFleetAgent(id)
  // A clicked card is already meaningful state. Render it immediately and let
  // the detail request enrich it in the background instead of blocking the
  // drawer (or painting an endless spinner after a transient request failure).
  // Never let a membership-less detail payload wipe Pulse placement.
  const agent = deepenFleetAgent(initialAgent, liveAgent)
  const { data: events, isLoading: eventsLoading } = useFleetEvents(id)
  const { data: activity, isLoading: activityLoading, isError: activityError } = useFleetActivity(id, { enabled: !!id })
  const m = useFleetMutations(id, { orgSlug: currentOrgSlug })
  const [confirm, setConfirm] = useState(null) // 'deprovision' | 'delete' | 'stop' | null

  const run = (mut, okMsg, payload) => {
    mut.mutate(payload, {
      onSuccess: () => toast.success(okMsg),
      onError: (e) => toast.error(errorMessage(e)),
    })
  }

  const healthVerdict = agent ? agentHealthVerdict(agent) : null
  const status = agent ? (
    <span className="inline-flex items-center gap-2">
      <StatusPill status={agent.status} />
      <ActivitySignal
        agent={agent}
        availability={healthVerdict === 'unavailable' ? 'unavailable' : 'available'}
        needsHelp={healthVerdict === 'needs_help'}
        dense
      />
      {agent.archived && (
        <span className="inline-flex items-center gap-1 rounded-full bg-slate-200 px-2 py-0.5 text-[11px] font-semibold text-slate-600 dark:bg-slate-700 dark:text-slate-300">
          <Archive className="h-3 w-3" />Archived
        </span>
      )}
    </span>
  ) : null
  const lifecycleManaged = agent?.lifecycle_managed !== false
  const provisioned = lifecycleManaged && agent && agent.provisioned_at
  const running = agent?.status === 'running'
  const hibernated = agent?.status === 'hibernated'
  const externalByo = agent?.is_external_byo === true
  const canClaim = externalByo && !agent?.owner_id
  const orphanCleanup = agent?.health_summary?.issues?.find(
    (issue) => issue.type === 'identity_orphan_cleanup'
  )

  return (
    <Shell
      onClose={onClose}
      title={agent?.display_name || agent?.agent_username || 'Agent'}
      subtitle={agent ? `@${agent.agent_username} · Runtime agent` : 'Runtime agent'}
      avatar={agent ? <AgentAvatar username={agent.agent_username} name={agent.display_name} avatarUrl={agent.avatar_url} size="lg" status={agent.status} /> : null}
      status={status}
      actions={
        agent && externalByo ? (
          <div className="space-y-2">
            {canClaim ? (
              <ActionButton
                icon={UserPlus}
                label={m.claim.isPending ? 'Claiming…' : 'Claim agent'}
                variant="primary"
                disabled={m.claim.isPending}
                onClick={() => run(m.claim, 'Agent claimed')}
              />
            ) : isStaff ? (
              <ActionButton
                icon={UserMinus}
                label="Unclaim"
                disabled={m.claim.isPending || !agent?.owner_id}
                onClick={() => run(m.claim, 'Agent unclaimed', { action: 'unclaim' })}
              />
            ) : (
              <p className="text-xs text-gray-500 text-center">Owned by {agent.owner || 'another user'}.</p>
            )}
            <p className="text-xs text-gray-500 text-center">Owner-scoped runtime connected to Hive; lifecycle is controlled by its owning runtime.</p>
          </div>
        ) : isStaff && agent && lifecycleManaged ? (
          <div className="grid grid-cols-2 gap-2">
            <ActionButton
              icon={Play}
              label="Provision"
              variant="primary"
              disabled={m.provision.isPending || running}
              onClick={() => run(m.provision, 'Provision requested')}
            />
            <ActionButton
              icon={Square}
              label="Hibernate"
              disabled={m.deprovision.isPending || !provisioned}
              onClick={() => setConfirm('deprovision')}
            />
            {agent.archived ? (
              <ActionButton
                icon={ArchiveRestore}
                label="Unarchive"
                disabled={m.unarchive.isPending}
                onClick={() => run(m.unarchive, 'Agent unarchived — start it when ready')}
              />
            ) : (
              <ActionButton
                icon={Archive}
                label="Archive"
                disabled={m.archive.isPending}
                onClick={() => setConfirm('archive')}
              />
            )}
            <ActionButton
              icon={Trash2}
              label="Delete agent"
              variant="danger"
              disabled={m.remove.isPending}
              onClick={() => setConfirm('delete')}
            />
          </div>
        ) : isStaff && agent && !lifecycleManaged ? (
          // HIVE-171: host-docker (rt-fleet) agents — Start/Stop via the daemon
          // toggle (proxied by the Hive backend). No provision/delete (the
          // daemon owns that lifecycle), just availability control.
          <div className="grid grid-cols-2 gap-2">
            <ActionButton
              icon={Play}
              label="Start"
              variant="primary"
              disabled={m.provision.isPending || running}
              onClick={() => run(m.provision, 'Start requested')}
            />
            <ActionButton
              icon={Square}
              label="Hibernate"
              disabled={m.deprovision.isPending || !running}
              onClick={() => setConfirm('stop')}
            />
            {isSuperuser ? (
              <ActionButton
                icon={Square}
                label="Hard stop"
                variant="danger"
                disabled={m.stop?.isPending || !running}
                onClick={() => setConfirm('hard-stop')}
              />
            ) : null}
            <ActionButton
              icon={RotateCcw}
              label="Restart"
              disabled={m.restart.isPending || !running}
              onClick={() => run(m.restart, 'Restart requested')}
            />
            {agent.archived ? (
              <ActionButton
                icon={ArchiveRestore}
                label="Unarchive"
                disabled={m.unarchive.isPending}
                onClick={() => run(m.unarchive, 'Agent unarchived — start it when ready')}
              />
            ) : (
              <ActionButton
                icon={Archive}
                label="Archive"
                disabled={m.archive.isPending}
                onClick={() => setConfirm('archive')}
              />
            )}
            {/* Operator 2026-07-12: daemon/rt-fleet agents must be deletable too —
                the backend force-deletes non-running rows (marks DELETED +
                best-effort daemon disable) so a dead seat is never undeletable. */}
            <ActionButton
              icon={Trash2}
              label="Delete agent"
              variant="danger"
              disabled={m.remove.isPending}
              onClick={() => setConfirm('delete')}
            />
          </div>
        ) : agent && !lifecycleManaged ? (
          <p className="text-xs text-gray-500 text-center">
            Managed by {agent.runtime_source || 'external runtime'}; lifecycle controls require admin privileges.
          </p>
        ) : (
          <p className="text-xs text-gray-500 text-center">
            Lifecycle controls require admin privileges.
          </p>
        )
      }
    >
      {!agent ? (
        <LoadingState />
      ) : (
        <>
          {hibernated && (
            <div className="mb-4 rounded-lg border border-sky-200 bg-sky-50 p-3 text-sm text-sky-800 dark:border-sky-800 dark:bg-sky-900/20 dark:text-sky-200">
              <div className="flex gap-2">
                <PauseCircle className="mt-0.5 h-4 w-4 shrink-0" />
                <div>
                  <div className="font-semibold">Ready to resume when Hive grants capacity</div>
                  <p className="mt-1 text-xs">
                    {agent.hibernation_reason || 'Automatically scaled to zero while waiting for work or a model lease.'}
                    {agent.hibernated_at ? ` · ${relativeTime(agent.hibernated_at)}` : ''}
                  </p>
                </div>
              </div>
            </div>
          )}
          {orphanCleanup && (
            <div className="mb-4 rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-900/20 dark:text-amber-200">
              <div className="flex gap-2">
                <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
                <div className="min-w-0 flex-1">
                  <div className="font-semibold">Incomplete identity — retiring</div>
                  <p className="mt-1 text-xs">This stopped, never-provisioned Hive row has no Shizuha-ID user. Hive automatically retires stale rows after the safety grace period.</p>
                  {isStaff && lifecycleManaged && (
                    <button
                      type="button"
                      className="mt-3 inline-flex items-center gap-1 rounded bg-red-600 px-2.5 py-1.5 text-xs font-semibold text-white hover:bg-red-700"
                      onClick={() => setConfirm('delete')}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                      Delete orphan agent
                    </button>
                  )}
                </div>
              </div>
            </div>
          )}
          {operatorFacingStatusMessage(agent.status_message) && (
            <div className="mb-4 rounded-lg bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-900 p-3 text-sm text-amber-800 dark:text-amber-300 flex gap-2">
              <ShieldAlert className="h-4 w-4 shrink-0 mt-0.5" />
              <span className="break-words">{operatorFacingStatusMessage(agent.status_message)}</span>
            </div>
          )}

          <DetailSection icon={Tag} title="Identity">
            <Field icon={Tag} label="Display role" value={agent.display_title || agent.role} />
            <Field icon={Activity} label="Enabled" value={agent.enabled === true ? 'Yes' : agent.enabled === false ? 'No' : ''} />
          </DetailSection>

          <DetailSection icon={Cpu} title="Model & compute">
            <Field icon={Cpu} label="Model" value={modelValue(agent)} mono />
            <Field icon={Cpu} label="Provider" value={agent.model_provider || agent.runtime_provider} />
            {agent.model_lease?.managed && (
              <>
                <Field icon={Zap} label="Lease state" value={humanize(agent.model_lease.state)} />
                <Field
                  icon={Users}
                  label="Current holder"
                  value={agent.model_lease.holder
                    ? agent.model_lease.holder.replace(/^agent-/, '@')
                    : 'No active holder'}
                  mono
                />
                {agent.model_lease.reason && (
                  <Field icon={Clock} label="Lease reason" value={agent.model_lease.reason} />
                )}
              </>
            )}
            <Field icon={Activity} label="Runtime harness" value={agent.runtime_harness} />
            <Field icon={Activity} label="Execution method" value={agent.execution_method} mono />
            <Field icon={Cpu} label="Reasoning effort" value={agent.reasoning_effort} />
            <Field icon={Layers} label="Resource profile" value={humanize(agent.resource_profile)} />
            {agent.resource_profile_spec && Object.keys(agent.resource_profile_spec).length > 0 && (
              <Field icon={Layers} label="Resource spec" value={JSON.stringify(agent.resource_profile_spec)} mono />
            )}
          </DetailSection>

          <DetailSection icon={Box} title="Image">
            <Field icon={Box} label="Runtime image" value={imageValue(agent)} mono />
            {agent.runtime_image_tag && <Field icon={Tag} label="Image tag" value={agent.runtime_image_tag} mono />}
            <Field icon={Box} label="Architecture" value={agent.arch} />
          </DetailSection>

          <DetailSection icon={Server} title="Runtime & placement">
            <Field icon={Activity} label="Runtime source" value={agent.runtime_source || 'k3s-native'} />
            <Field icon={Server} label="Namespace" value={agent.namespace} mono />
            <Field icon={Server} label="Pod" value={agent.pod_name} mono />
            <Field icon={Server} label="Node" value={agent.node_name} mono />
            {(agent.sidecar_set || []).length > 0 && <Field icon={Activity} label="Sidecars" value={(agent.sidecar_set || []).join(', ')} />}
          </DetailSection>

          <DetailSection icon={HardDrive} title="Storage">
            <Field icon={HardDrive} label="Workspace PVC" value={agent.workspace_pvc} mono />
            <Field icon={HardDrive} label="PVC capacity" value={agent.pvc_size} />
            <Field icon={HardDrive} label="Workspace path" value={agent.workspace_mount_path} mono />
          </DetailSection>

          <DetailSection icon={Clock} title="Lifecycle">
            <Field icon={Clock} label="Provisioned" value={relativeTime(agent.provisioned_at)} />
            <Field icon={Clock} label="Last active" value={relativeTime(agent.last_active_at)} />
            <Field icon={Clock} label="Created" value={relativeTime(agent.created_at)} />
          </DetailSection>

          <RolesByTeamPanel agentId={id} fallbackTitle={agent.display_title || agent.role || 'Agent'} enabled={isStaff} />

          <TeamPlacementEditor agentId={id} mutation={{ ...m.setTeamPlacement, createTeam: m.createTeam }} canEdit={isStaff} />

          <EffectiveCapabilitiesPanel agentId={id} />

          <RuntimeConfigEditor agent={agent} mutation={m.update} canEdit={isStaff} />

          {agent.context_budget && (
            <div className="mt-6 rounded-lg border border-gray-200 bg-gray-50 p-3 dark:border-gray-700 dark:bg-gray-900/30">
              <h3 className="mb-2 flex items-center gap-1.5 text-sm font-semibold"><Activity className="h-4 w-4" />Context budget</h3>
              <div className="grid grid-cols-3 gap-2 text-center text-xs">
                <div><div className="font-mono text-base font-bold">{formatTokens(agent.context_budget.total_tokens)}</div><div className="text-gray-500">total</div></div>
                <div><div className="font-mono text-base font-bold">{formatTokens(agent.context_budget.headroom_tokens)}</div><div className="text-gray-500">headroom</div></div>
                <div><div className="font-mono text-base font-bold">{agent.context_budget.utilization_percent}%</div><div className="text-gray-500">utilized</div></div>
              </div>
              <div className="mt-3 divide-y divide-gray-200 dark:divide-gray-700">
                {(agent.context_budget.components || []).filter((c) => !['total', 'headroom'].includes(c.component)).map((component) => (
                  <div key={component.component} className="flex items-center justify-between gap-2 py-1.5 text-xs">
                    <span className="truncate">{humanize(component.component)} <span className="text-gray-400">({component.status})</span></span>
                    <span className="font-mono text-gray-700 dark:text-gray-200">{formatTokens(component.tokens)}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          <ActivityTimeline data={activity} loading={activityLoading} rate={agent.activity_rate} error={activityError} />

          {(eventsLoading || (events?.length ?? 0) > 0) && (
            <>
              <h3 className="text-sm font-semibold mt-6 mb-1">Recent events</h3>
              <EventList events={events} loading={eventsLoading} />
            </>
          )}
        </>
      )}

      <ConfirmDialog
        open={confirm === 'deprovision'}
        title="Hibernate agent?"
        message="This scales the agent's k8s workload to zero. Hive can wake it automatically when there is work. The workspace PVC is retained."
        confirmLabel="Hibernate"
        loading={m.deprovision.isPending}
        onCancel={() => setConfirm(null)}
        onConfirm={() => {
          setConfirm(null)
          run(m.deprovision, 'Hibernate requested')
        }}
      />
      <ConfirmDialog
        open={confirm === 'stop'}
        title="Hibernate agent?"
        message="Hibernates this rt-fleet agent. Hive can wake it automatically when there is work. Only the operator can hard-stop."
        confirmLabel="Hibernate"
        loading={m.deprovision.isPending}
        onCancel={() => setConfirm(null)}
        onConfirm={() => {
          setConfirm(null)
          run(m.deprovision, 'Hibernate requested')
        }}
      />
      <ConfirmDialog
        open={confirm === 'hard-stop'}
        title="Hard-stop agent?"
        message="Operator-only kill switch. The agent will not auto-wake until you start it. Prefer Hibernate unless you intend to take this seat offline."
        confirmLabel="Hard stop"
        loading={m.stop?.isPending}
        onCancel={() => setConfirm(null)}
        onConfirm={() => {
          setConfirm(null)
          run(m.stop, 'Hard stop requested')
        }}
      />
      <ConfirmDialog
        open={confirm === 'archive'}
        title="Archive agent?"
        message="Stops the agent and parks it durably: no periodic watch, reconciler, or start button will run it again until you unarchive. Nothing is deleted — workspace and configuration are kept."
        confirmLabel="Archive"
        loading={m.archive.isPending}
        onCancel={() => setConfirm(null)}
        onConfirm={() => {
          setConfirm(null)
          run(m.archive, 'Agent archived — it will stay down until unarchived')
        }}
      />
      <ConfirmDialog
        open={confirm === 'delete'}
        destructive
        title="Delete agent permanently?"
        message="This deprovisions the workload and removes the agent record. This cannot be undone."
        confirmLabel="Delete"
        requireText={agent?.agent_username}
        loading={m.remove.isPending}
        onCancel={() => setConfirm(null)}
        onConfirm={() => {
          // Instant CRUD (operator 2026-07-12): the optimistic paint already
          // removed the card, so close NOW — don't hold the drawer on the
          // network. On error the revert refetches (card returns) + toast.
          m.remove.mutate(undefined, {
            onError: (e) => toast.error(`Delete failed — agent restored: ${errorMessage(e)}`),
          })
          toast.success('Agent deleted')
          setConfirm(null)
          onClose()
        }}
      />
    </Shell>
  )
}


function WhatsAppLinksPanel({ links, loading, mutations, toast }) {
  const statusClass = (value) => {
    if (value === 'active') return 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-300'
    if (value === 'paused') return 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300'
    if (value === 'rate_limited') return 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300'
    return 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300'
  }
  const setStatus = (link, next) => {
    mutations.setWhatsappStatus.mutate({ code: link.code, status: next }, {
      onSuccess: () => toast.success(next === 'paused' ? 'WhatsApp paused' : 'WhatsApp resumed'),
      onError: (e) => toast.error(errorMessage(e)),
    })
  }
  const unlink = (link) => {
    mutations.unlinkWhatsapp.mutate(link.code, {
      onSuccess: () => toast.success('WhatsApp unlinked; confirmation queued'),
      onError: (e) => toast.error(errorMessage(e)),
    })
  }
  if (loading) return <LoadingState label="Loading WhatsApp links…" />
  if (!links?.length) return <p className="text-sm text-gray-500 py-3">No linked WhatsApp numbers yet.</p>
  return (
    <div className="space-y-2 mt-2">
      {links.map((link) => (
        <div key={link.code} className="rounded-lg border border-gray-200 dark:border-gray-700 p-3 space-y-2">
          <div className="flex items-center justify-between gap-2">
            <div className="min-w-0">
              <div className="text-sm font-medium text-gray-900 dark:text-gray-100">{link.masked_number || 'Pending number'}</div>
              <div className="text-xs text-gray-500">Linked {relativeTime(link.verified_at || link.created_at)}</div>
            </div>
            <span className={`text-xs px-2 py-1 rounded-full ${statusClass(link.status)}`}>{humanize(link.status)}</span>
          </div>
          {link.status === 'rate_limited' && (
            <p className="text-xs text-red-600 dark:text-red-400">This WhatsApp channel is rate-limited; messages are not currently routed.</p>
          )}
          {link.status === 'paused' && (
            <p className="text-xs text-amber-600 dark:text-amber-400">Paused: inbound messages are acknowledged but not routed to this Claw.</p>
          )}
          <div className="flex flex-wrap gap-2">
            {link.status === 'paused' ? (
              <ActionButton icon={Play} label="Resume" disabled={mutations.setWhatsappStatus.isPending} onClick={() => setStatus(link, 'active')} />
            ) : (
              <ActionButton icon={PauseCircle} label="Pause" disabled={mutations.setWhatsappStatus.isPending || link.status !== 'active'} onClick={() => setStatus(link, 'paused')} />
            )}
            <ActionButton icon={Trash2} label="Unlink" variant="danger" disabled={mutations.unlinkWhatsapp.isPending} onClick={() => unlink(link)} />
          </div>
        </div>
      ))}
    </div>
  )
}

// ── Claw drawer ─────────────────────────────────────────────────────────────
function ClawDrawer({ id, onClose, initialClaw }) {
  const toast = useToast()
  const { isStaff } = useHive()
  const { data: liveClaw } = useClaw(id)
  // HIVE-1520 (game-engine, mirroring deepenFleetAgent): the clicked claw card
  // is already meaningful, live-ish state — normalize it, render it
  // immediately, and let the detail request DEEPEN it in the background. A
  // slow/failed deepening must never leave a blank "Loading…" blocking the
  // drawer, and must not wipe card-only fields the fresh detail omits.
  const snapshotClaw = (() => {
    if (!initialClaw) return null
    if (String(initialClaw.id || '').replace(/^claw:/, '') !== id) return null
    if (
      initialClaw.owner_username != null ||
      initialClaw.agent_username != null ||
      initialClaw.runtime_image != null
    ) {
      // Already a full claw-shaped object (Overview card path).
      return { ...initialClaw, id }
    }
    // Row-projected card snapshot (Agents table path) → claw-shaped partial.
    return {
      id,
      name: initialClaw.name || initialClaw.display_name,
      status: initialClaw.status,
      owner_email: initialClaw.owner_email || initialClaw.handle,
      owner_username:
        initialClaw.owner_username || String(initialClaw.handle || '').replace(/^owner: /, ''),
      owner_id: initialClaw.owner_id,
      avatar_url: initialClaw.avatar_url || initialClaw.avatarUrl,
      agent_type: initialClaw.agent_type,
      effective_model: initialClaw.effective_model || initialClaw.model,
      namespace: initialClaw.namespace,
      runtime_image: initialClaw.runtime_image,
      tier_id: initialClaw.tier_id,
      pvc_size: initialClaw.pvc_size,
      provisioned_at: initialClaw.provisioned_at,
      last_active_at: initialClaw.last_active_at || initialClaw.lastActive,
      created_at: initialClaw.created_at || initialClaw.created,
      legal_hold: initialClaw.legal_hold,
      runtime_compat_mode: initialClaw.runtime_compat_mode,
    }
  })()
  const claw = liveClaw ? (snapshotClaw ? { ...snapshotClaw, ...liveClaw } : liveClaw) : snapshotClaw
  const { data: events, isLoading: eventsLoading } = useClawEvents(id)
  const { data: waLinks, isLoading: waLinksLoading } = useClawWhatsAppLinks(id)
  const m = useClawMutations(id)
  const [confirm, setConfirm] = useState(null) // 'stop' | 'delete' | null
  const [waPairing, setWaPairing] = useState(null)
  const [renameDraft, setRenameDraft] = useState('')
  const [isRenaming, setIsRenaming] = useState(false)

  const run = (mut, okMsg) =>
    mut.mutate(undefined, {
      onSuccess: () => toast.success(okMsg),
      onError: (e) => toast.error(errorMessage(e)),
    })

  const running = claw?.status === 'running'
  const status = claw ? <StatusPill status={claw.status} /> : null
  const isOpenClawAgent = claw?.agent_type === 'claw'
  const runtimeKind = claw?.runtime_compat_mode ? 'OpenClaw compatibility mode' : humanize(claw?.agent_type || 'runtime')
  const dashboardHref = isOpenClawAgent && claw?.id ? `/hive/assistants/${claw.id}/dash` : ''
  const beginRename = () => {
    setRenameDraft(claw?.name || '')
    setIsRenaming(true)
  }
  const cancelRename = () => {
    setRenameDraft('')
    setIsRenaming(false)
  }
  const submitRename = (e) => {
    e.preventDefault()
    const nextName = renameDraft.trim()
    if (!nextName) {
      toast.error('Name is required')
      return
    }
    if (nextName === claw?.name) {
      cancelRename()
      return
    }
    m.rename.mutate(nextName, {
      onSuccess: () => {
        toast.success('Agent renamed')
        cancelRename()
      },
      onError: (err) => toast.error(errorMessage(err)),
    })
  }
  const requestWhatsAppPairing = () => {
    m.whatsappPairing.mutate(undefined, {
      onSuccess: (data) => setWaPairing(data),
      onError: (e) => toast.error(errorMessage(e)),
    })
  }

  return (
    <Shell
      onClose={onClose}
      title={claw?.name || 'Agent'}
      subtitle={claw ? `${claw.owner_email || claw.owner_username} · Account agent` : 'Account agent'}
      status={status}
      actions={
        isStaff && claw ? (
          <div className="grid grid-cols-2 gap-2">
            <ActionButton
              icon={Play}
              label="Start"
              variant="primary"
              disabled={m.start.isPending || running}
              onClick={() => run(m.start, 'Start requested')}
            />
            <ActionButton
              icon={Square}
              label="Stop"
              disabled={m.stop.isPending || !running}
              onClick={() => setConfirm('stop')}
            />
            <ActionButton
              icon={Trash2}
              label="Delete agent"
              variant="danger"
              disabled={m.remove.isPending || claw.legal_hold}
              onClick={() => setConfirm('delete')}
            />
          </div>
        ) : (
          <p className="text-xs text-gray-500 text-center">
            Lifecycle controls require admin privileges.
          </p>
        )
      }
    >
      {!claw ? (
        <LoadingState />
      ) : (
        <>
          {claw.legal_hold && (
            <div className="mb-4 rounded-lg bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-900 p-3 text-sm text-red-800 dark:text-red-300 flex gap-2">
              <ShieldAlert className="h-4 w-4 shrink-0 mt-0.5" />
              <span>Legal hold active — this agent cannot be renamed or deleted.</span>
            </div>
          )}
          {claw.runtime_compat_mode && (
            <div className="mb-4 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-900/20 dark:text-amber-200">
              <div className="mb-1 flex items-center gap-1.5 font-semibold">
                <Activity className="h-4 w-4" /> OpenClaw compatibility mode
              </div>
              <p>
                This Hermes assistant is intentionally running through the OpenClaw-compatible gateway image.
                Hermes-native dashboard and WhatsApp actions stay hidden until a dedicated Hermes runtime is deployed.
              </p>
            </div>
          )}
          <div className="hv-panel mb-4 p-3 text-sm">
            <div className="mb-2 flex items-center gap-1.5 font-semibold text-gray-900 dark:text-gray-100">
              <Pencil className="h-4 w-4" /> Name
            </div>
            {isRenaming ? (
              <form onSubmit={submitRename} className="flex flex-col gap-2 sm:flex-row">
                <input
                  className="min-w-0 flex-1 rounded border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-900 outline-none focus:border-primary-500 focus:ring-1 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100"
                  value={renameDraft}
                  onChange={(e) => setRenameDraft(e.target.value)}
                  maxLength={100}
                  autoFocus
                />
                <div className="flex gap-2">
                  <button
                    type="submit"
                    disabled={m.rename.isPending}
                    className="inline-flex items-center gap-1.5 rounded bg-primary-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-primary-700 disabled:opacity-50"
                  >
                    <Save className="h-3.5 w-3.5" />
                    {m.rename.isPending ? 'Saving…' : 'Save'}
                  </button>
                  <button
                    type="button"
                    onClick={cancelRename}
                    disabled={m.rename.isPending}
                    className="rounded border border-gray-300 px-3 py-1.5 text-xs font-medium hover:bg-gray-100 disabled:opacity-50 dark:border-gray-600 dark:hover:bg-gray-700"
                  >
                    Cancel
                  </button>
                </div>
              </form>
            ) : (
              <div className="flex items-center justify-between gap-3">
                <div className="min-w-0 break-words text-gray-800 dark:text-gray-200">{claw.name || 'Agent'}</div>
                <button
                  type="button"
                  onClick={beginRename}
                  disabled={claw.legal_hold}
                  className="inline-flex shrink-0 items-center gap-1.5 rounded border border-gray-300 px-3 py-1.5 text-xs font-medium hover:bg-gray-100 disabled:opacity-40 disabled:cursor-not-allowed dark:border-gray-600 dark:hover:bg-gray-700"
                >
                  <Pencil className="h-3.5 w-3.5" /> Edit
                </button>
              </div>
            )}
          </div>
          {dashboardHref && (
            <div className="hv-panel mb-4 p-3 text-sm">
              <div className="mb-2 flex items-center gap-1.5 font-semibold text-gray-900 dark:text-gray-100">
                <ExternalLink className="h-4 w-4" /> Dashboard
              </div>
              <a
                className={`inline-flex items-center gap-1.5 rounded px-3 py-1.5 text-xs font-medium text-white ${running ? 'bg-primary-600 hover:bg-primary-700' : 'pointer-events-none bg-gray-400 opacity-60'}`}
                href={dashboardHref}
                target="_blank"
                rel="noreferrer"
                aria-disabled={!running}
              >
                Open dashboard <ExternalLink className="h-3.5 w-3.5" />
              </a>
            </div>
          )}
          {isOpenClawAgent && (
            <div className="mb-4 rounded-lg border border-green-200 bg-green-50 p-3 text-sm dark:border-green-900 dark:bg-green-900/20">
              <div className="mb-1 flex items-center gap-1.5 font-semibold text-green-900 dark:text-green-200">
                <MessageCircle className="h-4 w-4" /> WhatsApp pairing
              </div>
              <p className="mb-3 text-green-800 dark:text-green-300">
                Pair WhatsApp through this assistant's native OpenClaw channel.
              </p>
              <button
                type="button"
                onClick={requestWhatsAppPairing}
                disabled={m.whatsappPairing.isPending}
                className="inline-flex items-center gap-1.5 rounded bg-green-700 px-3 py-1.5 text-xs font-medium text-white hover:bg-green-800 disabled:opacity-50"
              >
                <MessageCircle className="h-3.5 w-3.5" />
                {m.whatsappPairing.isPending ? 'Preparing…' : 'Link WhatsApp'}
              </button>
              {waPairing && (
                <div className="mt-3 space-y-2 text-xs text-green-900 dark:text-green-100">
                  <ol className="list-decimal space-y-1 pl-4">
                    {(waPairing.steps || []).map((step) => <li key={step}>{step}</li>)}
                  </ol>
                  <a
                    className="inline-flex items-center gap-1 font-medium underline"
                    href={waPairing.dashboard_api_path || waPairing.dashboard_url || waPairing.dashboard_path}
                    target={(waPairing.dashboard_api_path || waPairing.dashboard_url) ? '_blank' : undefined}
                    rel={(waPairing.dashboard_api_path || waPairing.dashboard_url) ? 'noreferrer' : undefined}
                  >
                    Open assistant dashboard <ExternalLink className="h-3 w-3" />
                  </a>
                </div>
              )}
            </div>
          )}

          <div className="divide-y divide-gray-100 dark:divide-gray-700/60">
            <Field
              icon={Tag}
              label="Owner"
              value={
                claw.owner_id != null
                  ? `${claw.owner_username || ''} (#${claw.owner_id})`
                  : claw.owner_username || claw.owner_email
              }
            />
            <Field icon={Tag} label="Owner email" value={claw.owner_email} />
            <Field icon={Activity} label="Runtime kind" value={runtimeKind} />
            <Field icon={Tag} label="Backing image" value={claw.runtime_image} mono />
            <Field icon={Layers} label="Tier" value={claw.tier_id || 'default'} />
            <Field icon={Server} label="Namespace" value={claw.namespace} mono />
            <Field icon={HardDrive} label="Data PVC" value={claw.pvc_size} />
            <Field icon={Clock} label="Provisioned" value={relativeTime(claw.provisioned_at)} />
            <Field icon={Clock} label="Last active" value={relativeTime(claw.last_active_at)} />
            <Field icon={Clock} label="Created" value={relativeTime(claw.created_at)} />
          </div>

          {isOpenClawAgent && (
            <>
              <h3 className="text-sm font-semibold mt-6 mb-1 flex items-center gap-2"><MessageCircle className="h-4 w-4" /> WhatsApp</h3>
              <WhatsAppLinksPanel links={waLinks} loading={waLinksLoading} mutations={m} toast={toast} />
            </>
          )}

          {(eventsLoading || (events?.length ?? 0) > 0) && (
            <>
              <h3 className="text-sm font-semibold mt-6 mb-1">Recent events</h3>
              <EventList events={events} loading={eventsLoading} />
            </>
          )}
        </>
      )}

      <ConfirmDialog
        open={confirm === 'stop'}
        title="Stop agent?"
        message="Scales this agent workload to zero. Data is retained; start it again anytime."
        confirmLabel="Stop"
        loading={m.stop.isPending}
        onCancel={() => setConfirm(null)}
        onConfirm={() => {
          setConfirm(null)
          run(m.stop, 'Stop requested')
        }}
      />
      <ConfirmDialog
        open={confirm === 'delete'}
        destructive
        title="Delete agent?"
        message="The backend requires the agent name as confirmation. This enters a 60-second undo window, then tears down the workload."
        confirmLabel="Delete"
        requireText={claw?.name}
        loading={m.remove.isPending}
        onCancel={() => setConfirm(null)}
        onConfirm={() => {
          m.remove.mutate(claw.name, {
            onSuccess: () => {
              toast.success('Claw scheduled for deletion (60s undo window)')
              setConfirm(null)
              onClose()
            },
            onError: (e) => toast.error(errorMessage(e)),
          })
        }}
      />
    </Shell>
  )
}

/** Unified drawer — dispatches on kind. */
export default function AgentDrawer({ kind, id, initialAgent, onClose }) {
  if (!id) return null
  if (kind === 'claw') return <ClawDrawer id={id} initialClaw={initialAgent} onClose={onClose} />
  return <FleetDrawer id={id} initialAgent={initialAgent} onClose={onClose} />
}
