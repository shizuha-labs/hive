import { Cpu } from 'lucide-react'
import Fleet from './Fleet'
import AgentAvatar from '../components/AgentAvatar'
import { useFleetAgents } from '../hooks/useAgents'
import { useHive } from '../contexts/HiveContext'

/** Home-style hero live line: "N agents on the job right now" + a cluster of
 *  running agents' portraits with a presence ping. Reads the same cached fleet
 *  query Fleet uses (identical queryKey → zero extra requests). */
function LiveLine() {
  const { isStaff, currentOrgSlug } = useHive()
  // Same params as Fleet's query → identical queryKey → shared cache, no extra request.
  const { data: agents } = useFleetAgents({ ownerOnly: !isStaff, orgSlug: currentOrgSlug })
  const running = (agents || []).filter((a) => (a.status || '').toLowerCase() === 'running')
  if (!running.length) {
    return (
      <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
        Every agent owned by this account, with team and organization placements.
      </p>
    )
  }
  const shown = running.slice(0, 10)
  return (
    <div className="mt-2 flex flex-wrap items-center gap-3">
      <span className="inline-flex items-center gap-1.5 text-sm text-gray-600 dark:text-gray-300">
        <span className="relative flex h-2 w-2">
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-70" />
          <span className="relative inline-flex h-2 w-2 rounded-full bg-emerald-500" />
        </span>
        <span className="font-semibold tabular-nums">{running.length}</span>
        &nbsp;{running.length === 1 ? 'agent' : 'agents'} on the job right now
      </span>
      <span className="flex -space-x-2">
        {shown.map((a) => (
          <span key={a.id} className="rounded-lg ring-2 ring-[#f4f5f9] dark:ring-[#101017]">
            <AgentAvatar username={a.agent_username} name={a.display_name} avatarUrl={a.avatar_url} size="sm" />
          </span>
        ))}
        {running.length > shown.length && (
          <span className="flex h-6 w-6 items-center justify-center rounded-lg bg-gray-200 text-[9px] font-bold text-gray-600 ring-2 ring-[#f4f5f9] dark:bg-white/10 dark:text-gray-300 dark:ring-[#101017]">
            +{running.length - shown.length}
          </span>
        )}
      </span>
    </div>
  )
}

export default function Agents() {
  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <div className="hv-kicker mb-1">Fleet · live</div>
          <h1 className="hv-page-title flex items-center gap-2.5">
            <Cpu className="h-6 w-6 text-primary-400" />
            Agents
          </h1>
          <LiveLine />
        </div>
      </div>

      <Fleet embedded />
    </div>
  )
}
