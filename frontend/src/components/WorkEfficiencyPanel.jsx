import { useMemo, useState } from 'react'
import clsx from 'clsx'
import { Activity, AlertTriangle } from 'lucide-react'

// HIVE-172: per-agent work-efficiency (throughput, cycle time, WIP, blocked,
// overdue, cost/task) sourced from Prometheus via the fleet API (agent.efficiency).
function hours(seconds) {
  if (!seconds && seconds !== 0) return '—'
  const h = seconds / 3600
  return h >= 1 ? `${h.toFixed(1)}h` : `${Math.round(seconds / 60)}m`
}
function num(v) { return v === undefined || v === null ? '—' : Math.round(v) }
function money(v) { return v === undefined || v === null ? '—' : `$${v.toFixed(2)}` }

export default function WorkEfficiencyPanel({ contexts = [], onSelect }) {
  const [sortBy, setSortBy] = useState('throughput')

  const rows = useMemo(() => {
    const list = contexts
      .map((ctx) => ({
        agent: ctx.agent,
        name: ctx.agent.display_name || ctx.agent.agent_username || 'Agent',
        eff: ctx.agent.efficiency,
      }))
      .filter((row) => row.eff && Object.keys(row.eff).length)
    return [...list].sort((a, b) => {
      if (sortBy === 'name') return a.name.localeCompare(b.name)
      if (sortBy === 'cycle') return (b.eff.cycle_seconds || 0) - (a.eff.cycle_seconds || 0)
      if (sortBy === 'overdue') return (b.eff.overdue || 0) - (a.eff.overdue || 0)
      if (sortBy === 'wip') return (b.eff.wip || 0) - (a.eff.wip || 0)
      return (b.eff.completed_24h || 0) - (a.eff.completed_24h || 0)
    })
  }, [contexts, sortBy])

  const summary = useMemo(() => {
    const done = rows.reduce((a, r) => a + (r.eff.completed_24h || 0), 0)
    const cyc = rows.map((r) => r.eff.cycle_seconds).filter((v) => v)
    const overdue = rows.reduce((a, r) => a + (r.eff.overdue || 0), 0)
    return {
      measured: rows.length,
      completed: done,
      avgCycle: cyc.length ? cyc.reduce((a, b) => a + b, 0) / cyc.length : null,
      overdue,
    }
  }, [rows])

  if (!rows.length) return null

  return (
    <section className="hv-panel p-4">
      <div className="mb-4 flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div>
          <h2 className="flex items-center gap-2 text-base font-semibold text-gray-950 dark:text-white">
            <Activity className="h-4 w-4 text-amber-500" />
            Work efficiency
          </h2>
          <p className="text-xs text-gray-500 dark:text-gray-400">
            Per-agent productivity (last 24h): throughput, cycle time, WIP, blocked, overdue, cost per task.
          </p>
        </div>
        <label className="flex min-w-[180px] flex-col gap-1 text-[11px] font-semibold uppercase text-gray-500 dark:text-gray-400">
          Sort by
          <select
            value={sortBy}
            onChange={(e) => setSortBy(e.target.value)}
            className="h-9 rounded-lg border border-gray-300 bg-white px-2 text-sm normal-case text-gray-900 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100"
          >
            <option value="throughput">Throughput (24h)</option>
            <option value="cycle">Cycle time</option>
            <option value="wip">WIP</option>
            <option value="overdue">Overdue</option>
            <option value="name">Agent name</option>
          </select>
        </label>
      </div>

      <div className="mb-4 grid gap-2 sm:grid-cols-4">
        <div className="rounded-lg bg-gray-50 p-3 ring-1 ring-gray-200 dark:bg-gray-900/40 dark:ring-gray-700">
          <div className="text-xl font-bold">{summary.measured}</div>
          <div className="text-xs text-gray-500">agents measured</div>
        </div>
        <div className="rounded-lg bg-gray-50 p-3 ring-1 ring-gray-200 dark:bg-gray-900/40 dark:ring-gray-700">
          <div className="text-xl font-bold">{summary.completed}</div>
          <div className="text-xs text-gray-500">tasks done / 24h</div>
        </div>
        <div className="rounded-lg bg-gray-50 p-3 ring-1 ring-gray-200 dark:bg-gray-900/40 dark:ring-gray-700">
          <div className="text-xl font-bold">{hours(summary.avgCycle)}</div>
          <div className="text-xs text-gray-500">avg cycle time</div>
        </div>
        <div className={clsx('rounded-lg p-3 ring-1', summary.overdue ? 'bg-amber-50 text-amber-700 ring-amber-200 dark:bg-amber-900/20 dark:text-amber-300 dark:ring-amber-800' : 'bg-green-50 text-green-700 ring-green-200 dark:bg-green-900/20 dark:text-green-300 dark:ring-green-800')}>
          <div className="text-xl font-bold">{summary.overdue}</div>
          <div className="text-xs opacity-80">overdue tasks</div>
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="border-b border-gray-200 text-xs uppercase tracking-wide text-gray-500 dark:border-gray-700 dark:text-gray-400">
              <th className="py-2 pr-3 font-medium">Agent</th>
              <th className="px-3 py-2 font-medium">Done 24h</th>
              <th className="px-3 py-2 font-medium">Cycle</th>
              <th className="px-3 py-2 font-medium">WIP</th>
              <th className="px-3 py-2 font-medium">Blocked</th>
              <th className="px-3 py-2 font-medium">Overdue</th>
              <th className="py-2 pl-3 font-medium">$/task</th>
            </tr>
          </thead>
          <tbody>
            {rows.slice(0, 15).map(({ agent, name, eff }) => (
              <tr
                key={agent.id || name}
                className="border-b border-gray-100 last:border-0 hover:bg-gray-50 dark:border-gray-700/50 dark:hover:bg-gray-700/40"
              >
                <td className="py-2 pr-3">
                  <button type="button" onClick={() => onSelect?.(agent)} className="text-left font-medium text-gray-950 hover:text-primary-600 dark:text-white">
                    {name}
                    <div className="font-normal text-xs text-gray-500">@{agent.agent_username}</div>
                  </button>
                </td>
                <td className="px-3 py-2 font-mono">{num(eff.completed_24h)}</td>
                <td className="px-3 py-2 font-mono text-gray-500">{hours(eff.cycle_seconds)}</td>
                <td className="px-3 py-2 font-mono">{num(eff.wip)}</td>
                <td className="px-3 py-2 font-mono text-gray-500">{num(eff.blocked)}</td>
                <td className="px-3 py-2">
                  {eff.overdue ? (
                    <span className="inline-flex items-center gap-1 rounded-full bg-amber-50 px-2 py-0.5 text-xs font-semibold text-amber-700 ring-1 ring-amber-200 dark:bg-amber-900/20 dark:text-amber-300 dark:ring-amber-800">
                      <AlertTriangle className="h-3 w-3" />{num(eff.overdue)}
                    </span>
                  ) : <span className="font-mono text-gray-400">0</span>}
                </td>
                <td className="py-2 pl-3 font-mono text-gray-500">{money(eff.cost_per_completed_task)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {rows.length > 15 && <div className="mt-2 text-xs text-gray-500">Showing top 15 of {rows.length}; adjust sort above to inspect others.</div>}
    </section>
  )
}
