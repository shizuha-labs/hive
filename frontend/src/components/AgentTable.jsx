import AgentAvatar from './AgentAvatar'
import { Bot } from 'lucide-react'
import StatusPill from './StatusPill'
import { SkeletonRows } from './States'
import { relativeTime } from '../lib/format'
import { activityLabel, resolveActivityPhase } from '../lib/activityPhase'

/**
 * Normalize a fleet agent OR a claw into a common row shape so one table can
 * render both. `kind` distinguishes them for the drawer + icon.
 */
export function toRow(item, kind) {
  if (kind === 'claw' || item?.kind === 'claw' || item?.runtime_source === 'claw' || String(item?.id || '').startsWith('claw:')) {
    const rawId = String(item.id || '').replace(/^claw:/, '')
    const displayName = item.display_name || item.name || item.runtime_harness
    const accountName = ['claw', 'openclaw'].includes(String(displayName || '').toLowerCase())
      ? 'Account Agent'
      : displayName || 'Account Agent'
    return {
      id: rawId,
      kind: 'claw',
      name: accountName,
      handle: item.owner_username ? `owner: ${item.owner_username}` : item.owner_email || `@${item.agent_username || 'claw'}`,
      role: 'Account agent',
      model: item.effective_model || item.model || item.tier_id || 'default tier',
      arch: item.arch || 'k3s',
      node: item.pod_name || item.namespace || 'Hosted runtime',
      status: item.status,
      lastActive: item.last_active_at,
      created: item.created_at,
      avatarUrl: item.avatar_url,
    }
  }
  return {
    id: item.id,
    kind: 'fleet',
    name: item.display_name || item.agent_username,
    handle: `@${item.agent_username}`,
    role: item.role || '—',
    model: [item.runtime_harness, item.effective_model || item.model].filter(Boolean).join(' / ') || 'unknown',
    arch: item.arch || '—',
    node: item.pod_name || item.namespace || '—',
    status: item.status,
    lastActive: item.last_active_at,
    created: item.created_at,
    contextBudget: item.context_budget,
    avatarUrl: item.avatar_url,
    activityPhase: resolveActivityPhase(item),
    activityLabel: activityLabel(resolveActivityPhase(item), {
      toolName: item.activity_tool_name,
      leaseIdleWaste: item.lease_idle_waste,
    }),
  }
}

function Avatar({ row }) {
  // Real portrait (same identity as shizuha.com home) with presence dot.
  const username = (row.handle || '').startsWith('@') ? row.handle.slice(1) : row.rawUsername
  return <AgentAvatar username={username} name={row.name} avatarUrl={row.avatarUrl} size="md" status={row.status} />
}

function kindLabel(kind) {
  if (kind === 'claw') return 'account'
  if (kind === 'fleet') return 'runtime'
  return kind
}

export default function AgentTable({ rows, loading, onSelect, showKind = false }) {
  return (
    <div className="overflow-hidden rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 shadow-sm">
      <div className="overflow-x-auto">
        <table className="w-full text-left">
          <thead>
            <tr className="border-b border-gray-200 dark:border-gray-700 text-xs uppercase tracking-wide text-gray-500 dark:text-gray-400">
              <th className="px-4 py-3 font-medium">Agent</th>
              <th className="px-4 py-3 font-medium hidden md:table-cell">Role</th>
              <th className="px-4 py-3 font-medium hidden lg:table-cell">Model / Backend</th>
              <th className="px-4 py-3 font-medium hidden xl:table-cell">Arch</th>
              <th className="px-4 py-3 font-medium hidden xl:table-cell">Node / Pod</th>
              <th className="px-4 py-3 font-medium hidden 2xl:table-cell">Context</th>
              <th className="px-4 py-3 font-medium">Status</th>
              <th className="px-4 py-3 font-medium hidden lg:table-cell">Doing</th>
              <th className="px-4 py-3 font-medium hidden sm:table-cell">Last active</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <SkeletonRows rows={6} cols={7} />
            ) : (
              rows.map((row) => (
                <tr
                  key={`${row.kind}-${row.id}`}
                  onClick={() => onSelect?.(row)}
                  className="border-b border-gray-100 dark:border-gray-700/50 last:border-0 hover:bg-gray-50 dark:hover:bg-gray-700/40 cursor-pointer transition-colors"
                >
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-3">
                      <Avatar row={row} />
                      <div className="min-w-0">
                        <div className="font-medium text-gray-900 dark:text-gray-100 truncate flex items-center gap-2">
                          {row.name}
                          {showKind && (
                            <span className="text-[10px] uppercase tracking-wide text-gray-400 border border-gray-200 dark:border-gray-600 rounded px-1 py-0.5">
                              {kindLabel(row.kind)}
                            </span>
                          )}
                        </div>
                        <div className="text-xs text-gray-500 truncate">{row.handle}</div>
                      </div>
                    </div>
                  </td>
                  <td className="px-4 py-3 hidden md:table-cell text-sm text-gray-600 dark:text-gray-300">
                    {row.role}
                  </td>
                  <td className="px-4 py-3 hidden lg:table-cell text-sm text-gray-600 dark:text-gray-300 font-mono truncate max-w-[200px]">
                    {row.model}
                  </td>
                  <td className="px-4 py-3 hidden xl:table-cell text-sm text-gray-600 dark:text-gray-300">
                    {row.arch}
                  </td>
                  <td className="px-4 py-3 hidden xl:table-cell text-sm text-gray-500 font-mono truncate max-w-[180px]">
                    {row.node}
                  </td>
                  <td className="px-4 py-3 hidden 2xl:table-cell text-sm text-gray-600 dark:text-gray-300 font-mono">
                    {row.contextBudget ? `${Math.round(row.contextBudget.total_tokens / 100) / 10}k / ${row.contextBudget.utilization_percent}%` : '—'}
                  </td>
                  <td className="px-4 py-3">
                    <StatusPill status={row.status} />
                  </td>
                  <td className="px-4 py-3 hidden lg:table-cell text-sm text-gray-600 dark:text-gray-300">
                    {row.activityLabel || '—'}
                  </td>
                  <td className="px-4 py-3 hidden sm:table-cell text-sm text-gray-500">
                    {relativeTime(row.lastActive)}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
