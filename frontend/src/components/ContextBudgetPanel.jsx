import { useMemo, useState } from 'react'
import clsx from 'clsx'
import { AlertTriangle, Gauge, Layers } from 'lucide-react'
import { humanize, relativeTime } from '../lib/format'

const COMPONENT_COLORS = {
  agents_md: 'bg-amber-500',
  agent_config: 'bg-sky-500',
  skills: 'bg-violet-500',
  mcp_tools: 'bg-emerald-500',
  runtime_wrapper: 'bg-rose-500',
  memory: 'bg-gray-400',
  conversation_history: 'bg-slate-500',
}

function formatTokens(value) {
  const n = Number(value || 0)
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1000) return `${(n / 1000).toFixed(n >= 10_000 ? 0 : 1)}k`
  return `${n}`
}

function nonDerivedComponents(budget) {
  return (budget?.components || []).filter((c) => !['total', 'headroom'].includes(c.component))
}

function severityTone(severity) {
  if (severity === 'critical') return 'text-red-700 bg-red-50 ring-red-200 dark:bg-red-900/20 dark:text-red-300 dark:ring-red-800'
  if (severity === 'warning') return 'text-amber-700 bg-amber-50 ring-amber-200 dark:bg-amber-900/20 dark:text-amber-300 dark:ring-amber-800'
  return 'text-green-700 bg-green-50 ring-green-200 dark:bg-green-900/20 dark:text-green-300 dark:ring-green-800'
}

function CompositionBar({ budget }) {
  const components = nonDerivedComponents(budget)
  const total = Math.max(Number(budget?.total_tokens || 0), 1)
  return (
    <div className="flex h-2 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800" title="Context composition">
      {components.map((component) => {
        const pct = Math.max((Number(component.tokens || 0) / total) * 100, component.tokens ? 3 : 0)
        return (
          <span
            key={component.component}
            className={COMPONENT_COLORS[component.component] || 'bg-gray-500'}
            style={{ width: `${pct}%` }}
            title={`${humanize(component.component)}: ${formatTokens(component.tokens)} tokens`}
          />
        )
      })}
    </div>
  )
}

export default function ContextBudgetPanel({ contexts = [], onSelect }) {
  const [sortBy, setSortBy] = useState('utilization')

  const rows = useMemo(() => {
    const list = contexts
      .map((ctx) => ({
        agent: ctx.agent,
        name: ctx.agent.display_name || ctx.agent.agent_username || 'Agent',
        budget: ctx.agent.context_budget,
      }))
      .filter((row) => row.budget)
    return [...list].sort((a, b) => {
      if (sortBy === 'name') return a.name.localeCompare(b.name)
      if (sortBy === 'headroom') return (a.budget.headroom_tokens || 0) - (b.budget.headroom_tokens || 0)
      if (sortBy === 'total') return (b.budget.total_tokens || 0) - (a.budget.total_tokens || 0)
      return (b.budget.utilization_percent || 0) - (a.budget.utilization_percent || 0)
    })
  }, [contexts, sortBy])

  const summary = useMemo(() => {
    const totals = rows.map((row) => row.budget.total_tokens || 0)
    const max = rows[0]?.budget
    return {
      measured: rows.length,
      outliers: rows.filter((row) => row.budget.outlier).length,
      avg: totals.length ? Math.round(totals.reduce((a, b) => a + b, 0) / totals.length) : 0,
      max,
    }
  }, [rows])

  if (!rows.length) return null

  return (
    <section className="hv-panel p-4">
      <div className="mb-4 flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div>
          <h2 className="flex items-center gap-2 text-base font-semibold text-gray-950 dark:text-white">
            <Gauge className="h-4 w-4 text-amber-500" />
            Context budget
          </h2>
          <p className="text-xs text-gray-500 dark:text-gray-400">
            Estimated per-agent prompt composition. Values are counts only; no raw prompt or memory content is exposed.
          </p>
        </div>
        <label className="flex min-w-[180px] flex-col gap-1 text-[11px] font-semibold uppercase text-gray-500 dark:text-gray-400">
          Sort by
          <select
            value={sortBy}
            onChange={(e) => setSortBy(e.target.value)}
            className="h-9 rounded-lg border border-gray-300 bg-white px-2 text-sm normal-case text-gray-900 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100"
          >
            <option value="utilization">Utilization / bloat</option>
            <option value="total">Total tokens</option>
            <option value="headroom">Lowest headroom</option>
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
          <div className="text-xl font-bold">{formatTokens(summary.avg)}</div>
          <div className="text-xs text-gray-500">avg tokens</div>
        </div>
        <div className="rounded-lg bg-gray-50 p-3 ring-1 ring-gray-200 dark:bg-gray-900/40 dark:ring-gray-700">
          <div className="text-xl font-bold">{formatTokens(summary.max?.total_tokens)}</div>
          <div className="text-xs text-gray-500">largest context</div>
        </div>
        <div className={clsx('rounded-lg p-3 ring-1', summary.outliers ? 'bg-amber-50 text-amber-700 ring-amber-200 dark:bg-amber-900/20 dark:text-amber-300 dark:ring-amber-800' : 'bg-green-50 text-green-700 ring-green-200 dark:bg-green-900/20 dark:text-green-300 dark:ring-green-800')}>
          <div className="text-xl font-bold">{summary.outliers}</div>
          <div className="text-xs opacity-80">outlier flags</div>
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="border-b border-gray-200 text-xs uppercase tracking-wide text-gray-500 dark:border-gray-700 dark:text-gray-400">
              <th className="py-2 pr-3 font-medium">Agent</th>
              <th className="px-3 py-2 font-medium">Total</th>
              <th className="px-3 py-2 font-medium">Window</th>
              <th className="px-3 py-2 font-medium">Utilization</th>
              <th className="px-3 py-2 font-medium">Composition</th>
              <th className="px-3 py-2 font-medium">Largest component</th>
              <th className="py-2 pl-3 font-medium">Measured</th>
            </tr>
          </thead>
          <tbody>
            {rows.slice(0, 12).map(({ agent, name, budget }) => {
              const largest = [...nonDerivedComponents(budget)].sort((a, b) => (b.tokens || 0) - (a.tokens || 0))[0]
              return (
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
                  <td className="px-3 py-2 font-mono">{formatTokens(budget.total_tokens)}</td>
                  <td className="px-3 py-2 font-mono text-gray-500">{formatTokens(budget.context_window_tokens)}</td>
                  <td className="px-3 py-2">
                    <span className={clsx('inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-semibold ring-1', severityTone(budget.severity))}>
                      {budget.outlier && <AlertTriangle className="h-3 w-3" />}
                      {budget.utilization_percent}%
                    </span>
                  </td>
                  <td className="min-w-[170px] px-3 py-2"><CompositionBar budget={budget} /></td>
                  <td className="px-3 py-2 text-xs text-gray-600 dark:text-gray-300">
                    <span className="inline-flex items-center gap-1"><Layers className="h-3 w-3" />{humanize(largest?.component)} · {formatTokens(largest?.tokens)}</span>
                  </td>
                  <td className="py-2 pl-3 text-xs text-gray-500">{relativeTime(budget.measured_at)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      {rows.length > 12 && <div className="mt-2 text-xs text-gray-500">Showing top 12 of {rows.length}; adjust sort/filter above to inspect others.</div>}
      <div className="mt-3 flex flex-wrap gap-2 text-[11px] text-gray-500 dark:text-gray-400">
        {Object.entries(COMPONENT_COLORS).map(([component, color]) => (
          <span key={component} className="inline-flex items-center gap-1"><span className={clsx('h-2 w-2 rounded-full', color)} />{humanize(component)}</span>
        ))}
      </div>
    </section>
  )
}
