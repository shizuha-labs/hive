import { Search, RefreshCw } from 'lucide-react'
import clsx from 'clsx'

const STATUS_FILTERS = ['all', 'running', 'provisioning', 'stopped', 'failed', 'pending']

export default function Toolbar({
  query,
  onQuery,
  status,
  onStatus,
  onRefresh,
  refreshing,
  right,
  statuses = STATUS_FILTERS,
}) {
  return (
    <div className="flex flex-col sm:flex-row gap-3 sm:items-center justify-between">
      <div className="flex items-center gap-2 flex-1 min-w-0">
        <div className="relative flex-1 max-w-xs">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-gray-400" />
          <input
            value={query}
            onChange={(e) => onQuery(e.target.value)}
            placeholder="Search agents…"
            className="hv-input w-full pl-9 pr-3 py-2 text-sm"
          />
        </div>
        {onStatus && (
          <div className="hidden md:flex items-center gap-1">
            {statuses.map((s) => (
              <button
                key={s}
                onClick={() => onStatus(s)}
                className={clsx(
                  'px-2.5 py-1.5 text-xs rounded-lg capitalize transition-colors',
                  status === s
                    ? 'bg-primary-600 text-white'
                    : 'text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700'
                )}
              >
                {s}
              </button>
            ))}
          </div>
        )}
      </div>
      <div className="flex items-center gap-2">
        {onRefresh && (
          <button
            onClick={onRefresh}
            className="inline-flex items-center gap-1.5 px-3 py-2 text-sm rounded-lg border border-gray-300 dark:border-gray-600 hover:bg-gray-100 dark:hover:bg-gray-700"
          >
            <RefreshCw className={clsx('h-4 w-4', refreshing && 'animate-spin')} />
            <span className="hidden sm:inline">Refresh</span>
          </button>
        )}
        {right}
      </div>
    </div>
  )
}
