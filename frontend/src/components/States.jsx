import { Loader2, AlertCircle, Inbox } from 'lucide-react'

export function LoadingState({ label = 'Loading…' }) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-gray-500">
      <Loader2 className="h-8 w-8 animate-spin mb-3" />
      <p className="text-sm">{label}</p>
    </div>
  )
}

export function ErrorState({ message = 'Failed to load data.', onRetry }) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      <div className="p-3 rounded-full bg-red-100 dark:bg-red-900/30 mb-3">
        <AlertCircle className="h-7 w-7 text-red-600 dark:text-red-400" />
      </div>
      <p className="text-sm text-gray-600 dark:text-gray-300 max-w-md">{message}</p>
      {onRetry && (
        <button
          onClick={onRetry}
          className="mt-4 px-4 py-2 text-sm rounded-lg bg-primary-600 text-white hover:bg-primary-700"
        >
          Retry
        </button>
      )}
    </div>
  )
}

export function EmptyState({ icon: Icon = Inbox, title = 'Nothing here yet', message, action }) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      <div className="p-3 rounded-full bg-gray-100 dark:bg-gray-700 mb-3">
        <Icon className="h-7 w-7 text-gray-400" />
      </div>
      <h3 className="text-base font-semibold text-gray-700 dark:text-gray-200">{title}</h3>
      {message && <p className="mt-1 text-sm text-gray-500 max-w-md">{message}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  )
}

/** Skeleton rows for a loading table. */
export function SkeletonRows({ rows = 5, cols = 5 }) {
  return (
    <>
      {Array.from({ length: rows }).map((_, r) => (
        <tr key={r} className="border-b border-gray-100 dark:border-gray-700/50">
          {Array.from({ length: cols }).map((__, c) => (
            <td key={c} className="px-4 py-3">
              <div className="h-4 rounded bg-gray-200 dark:bg-gray-700 animate-pulse" style={{ width: `${50 + ((r + c) % 4) * 12}%` }} />
            </td>
          ))}
        </tr>
      ))}
    </>
  )
}
