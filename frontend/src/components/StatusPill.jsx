import clsx from 'clsx'
import { Loader2 } from 'lucide-react'
import { humanize } from '../lib/format'

// Maps both FleetAgent.Status and Claw.Status to a colored pill. Unknown
// statuses fall back to a neutral gray.
const STATUS_STYLES = {
  running: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400 ring-green-600/20',
  hibernated: 'bg-sky-100 text-sky-700 dark:bg-sky-900/30 dark:text-sky-300 ring-sky-600/20',
  provisioning: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400 ring-blue-600/20',
  pending: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400 ring-amber-600/20',
  offline: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400 ring-amber-600/20',
  stopped: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400 ring-red-600/20',
  deleting: 'bg-orange-100 text-orange-700 dark:bg-orange-900/30 dark:text-orange-400 ring-orange-600/20',
  deleted: 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400 ring-gray-500/20',
  failed: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400 ring-red-600/20',
  blocked: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400 ring-red-600/20',
  suspended_owner_removed: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400 ring-red-600/20',
  suspended_no_credits: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400 ring-amber-600/20',
  // Transitional (optimistic) states — lib/transientStatus.js. Spinner + tint:
  // something is actively happening (the GCloud-VM "Stopping…" feel).
  stopping: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400 ring-amber-600/20',
  starting: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400 ring-blue-600/20',
  restarting: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400 ring-blue-600/20',
}

const DOT_STYLES = {
  running: 'bg-green-500',
  hibernated: 'bg-sky-500',
  provisioning: 'bg-blue-500 animate-pulse',
  pending: 'bg-amber-500',
  offline: 'bg-amber-500',
  stopped: 'bg-red-500',
  deleting: 'bg-orange-500 animate-pulse',
  deleted: 'bg-gray-400',
  failed: 'bg-red-500',
  blocked: 'bg-red-500',
  suspended_owner_removed: 'bg-red-500',
  suspended_no_credits: 'bg-amber-500',
}

// States rendered with a spinner + ellipsis: the UI is mid-transition and will
// converge to a terminal state within seconds (fast poll while any is active).
const TRANSITIONAL = new Set(['stopping', 'starting', 'restarting', 'provisioning', 'deleting'])

export default function StatusPill({ status, className }) {
  const key = (status || '').toLowerCase()
  const transitional = TRANSITIONAL.has(key)
  return (
    <span
      className={clsx(
        'inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset',
        STATUS_STYLES[key] || 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300 ring-gray-500/20',
        className
      )}
    >
      {transitional ? (
        <Loader2 className="h-3 w-3 animate-spin" />
      ) : (
        <span className={clsx('h-1.5 w-1.5 rounded-full', DOT_STYLES[key] || 'bg-gray-400')} />
      )}
      {transitional ? `${humanize(status)}…` : (humanize(status) || 'Unknown')}
    </span>
  )
}
