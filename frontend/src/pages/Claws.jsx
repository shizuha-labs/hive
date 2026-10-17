import { useMemo, useState } from 'react'
import { Bot } from 'lucide-react'
import { useClaws } from '../hooks/useAgents'
import AgentTable, { toRow } from '../components/AgentTable'
import AgentDrawer from '../components/AgentDrawer'
import Toolbar from '../components/Toolbar'
import { ErrorState, EmptyState } from '../components/States'

const CLAW_STATUSES = ['all', 'running', 'provisioning', 'stopped', 'deleting', 'failed']

export default function Claws({ embedded = false }) {
  const { data, isLoading, isError, isFetching, refetch } = useClaws()
  const [query, setQuery] = useState('')
  const [status, setStatus] = useState('all')
  const [selected, setSelected] = useState(null)

  const rows = useMemo(() => {
    let list = (data || []).map((c) => toRow(c, 'claw'))
    if (status !== 'all') list = list.filter((r) => r.status === status)
    const q = query.trim().toLowerCase()
    if (q) {
      list = list.filter(
        (r) =>
          r.name.toLowerCase().includes(q) ||
          (r.handle || '').toLowerCase().includes(q) ||
          (r.node || '').toLowerCase().includes(q)
      )
    }
    return list
  }, [data, query, status])

  return (
    <div className="space-y-5">
      {!embedded && (
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <Bot className="h-6 w-6 text-fuchsia-500" />
            Agents
          </h1>
          <p className="text-gray-500 dark:text-gray-400">
            Agents provisioned under your account. They use the same Hive lifecycle surface as every other agent.
          </p>
        </div>
      )}

      <Toolbar
        query={query}
        onQuery={setQuery}
        status={status}
        onStatus={setStatus}
        statuses={CLAW_STATUSES}
        onRefresh={() => refetch()}
        refreshing={isFetching}
      />

      {isError ? (
        <ErrorState message="Could not load agents. The HIVE API may be unreachable." onRetry={() => refetch()} />
      ) : rows.length === 0 && !isLoading ? (
        query || status !== 'all' ? (
          <EmptyState icon={Bot} title="No matching agents" message="Try a different search or status filter." />
        ) : (
          <EmptyState
            icon={Bot}
            title="No agents yet"
            message="Agents provisioned under this account will appear here."
          />
        )
      ) : (
        <AgentTable rows={rows} loading={isLoading} onSelect={setSelected} />
      )}

      <AgentDrawer
        kind="claw"
        id={selected?.id}
        initialAgent={selected}
        onClose={() => setSelected(null)}
      />
    </div>
  )
}
