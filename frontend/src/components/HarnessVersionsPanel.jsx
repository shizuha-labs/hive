import { useState } from 'react'
import { Cpu, ArrowUpCircle, CheckCircle2, RefreshCw, ChevronDown, ChevronRight, Loader2, AlertCircle } from 'lucide-react'
import clsx from 'clsx'
import { useHarnessVersions, useHarnessUpgrade } from '../hooks/useAgents'

/**
 * HIVE-600: fleet harness-version tracker. Shows, per harness (Codex, Claude
 * Code, Antigravity, OpenClaw), the version the fleet currently runs vs the latest
 * published, and lets an admin request a fleet-wide upgrade when one is
 * available. The "current" version comes from the agent-runtime image's OCI
 * labels; "latest" from the npm registry.
 */
export default function HarnessVersionsPanel({ isStaff = false }) {
  // Collapsed by default: this is admin detail, not the page's hero content
  // (P1 from the 2026-07-11 design critique — it was stealing the prime viewport).
  const [open, setOpen] = useState(false)
  const { data, isLoading, isError, refetch, isFetching } = useHarnessVersions({ enabled: isStaff })
  const upgrade = useHarnessUpgrade()
  const [pending, setPending] = useState(null)
  const [note, setNote] = useState(null)

  if (!isStaff) return null

  const harnesses = data?.harnesses || []
  const versionsUnavailable = isError || (!isLoading && !data)
  const anyUpgrade = harnesses.some((h) => h.upgrade_available)
  const rolloutInProgress = Boolean(data?.rollout_in_progress)

  async function onUpgrade(h) {
    if (rolloutInProgress) return
    setPending(h.harness)
    setNote(null)
    try {
      const res = await upgrade.mutateAsync({ harness: h.harness, targetVersion: h.latest })
      setNote({
        harness: h.harness,
        text: `Upgrade to ${h.package}@${res.target_version} requested — ${res.rollout?.summary || 'rebuild + roll the agent-runtime image.'}`,
      })
    } catch (e) {
      setNote({ harness: h.harness, text: `Upgrade request failed: ${e?.response?.data?.error || e.message}` })
    } finally {
      setPending(null)
    }
  }

  return (
    <section className="mb-4 rounded-xl border border-gray-200 bg-white shadow-sm dark:border-gray-700 dark:bg-gray-800">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left"
      >
        <div className="flex items-center gap-2.5">
          {open ? <ChevronDown className="h-4 w-4 text-gray-400" /> : <ChevronRight className="h-4 w-4 text-gray-400" />}
          <Cpu className="h-4 w-4 text-indigo-500" />
          <h2 className="text-sm font-bold text-gray-900 dark:text-white">Harness versions</h2>
          {isLoading ? (
            <span className="inline-flex items-center gap-1 rounded-full bg-gray-100 px-2 py-0.5 text-[11px] font-semibold text-gray-600 ring-1 ring-gray-200 dark:bg-gray-700/60 dark:text-gray-300 dark:ring-gray-600">
              <Loader2 className="h-3 w-3 animate-spin" /> checking
            </span>
          ) : versionsUnavailable ? (
            <span className="inline-flex items-center gap-1 rounded-full bg-red-50 px-2 py-0.5 text-[11px] font-semibold text-red-700 ring-1 ring-red-200 dark:bg-red-900/20 dark:text-red-300 dark:ring-red-800">
              <AlertCircle className="h-3 w-3" /> status unavailable
            </span>
          ) : rolloutInProgress ? (
            <span className="inline-flex items-center gap-1 rounded-full bg-indigo-50 px-2 py-0.5 text-[11px] font-semibold text-indigo-700 ring-1 ring-indigo-200 dark:bg-indigo-900/20 dark:text-indigo-300 dark:ring-indigo-800">
              <Loader2 className="h-3 w-3 animate-spin" /> runtime rolling
            </span>
          ) : anyUpgrade ? (
            <span className="inline-flex items-center gap-1 rounded-full bg-amber-50 px-2 py-0.5 text-[11px] font-semibold text-amber-700 ring-1 ring-amber-200 dark:bg-amber-900/20 dark:text-amber-300 dark:ring-amber-800">
              <ArrowUpCircle className="h-3 w-3" /> upgrade available
            </span>
          ) : (
            <span className="inline-flex items-center gap-1 rounded-full bg-green-50 px-2 py-0.5 text-[11px] font-semibold text-green-700 ring-1 ring-green-200 dark:bg-green-900/20 dark:text-green-300 dark:ring-green-800">
              <CheckCircle2 className="h-3 w-3" /> up to date
            </span>
          )}
        </div>
        <span
          role="button"
          tabIndex={0}
          onClick={(e) => { e.stopPropagation(); refetch() }}
          onKeyDown={(e) => { if (e.key === 'Enter') { e.stopPropagation(); refetch() } }}
          className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-[11px] text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700"
        >
          <RefreshCw className={clsx('h-3 w-3', isFetching && 'animate-spin')} /> refresh
        </span>
      </button>

      {open && (
        <div className="border-t border-gray-100 px-4 py-3 dark:border-gray-700">
          {data?.agent_runtime_image && (
            <p className="mb-2 truncate font-mono text-[11px] text-gray-400" title={data.agent_runtime_image}>
              fleet image: {data.agent_runtime_image}
            </p>
          )}
          {rolloutInProgress && data?.desired_runtime_image && (
            <>
              <p className="mb-1 truncate font-mono text-[11px] text-indigo-500" title={data.desired_runtime_image}>
                rolling to: {data.desired_runtime_image}
              </p>
              <p className="mb-1 text-[11px] text-indigo-500">
                Runtime image rollout in progress. Harness package versions can already be current
                when the new image contains other source or runtime changes.
              </p>
              {data?.rollout_progress?.total > 0 && (
                <div className="mb-2">
                  <div className="mb-1 flex items-center justify-between text-[11px] text-indigo-500">
                    <span>
                      {data.rollout_progress.matched}/{data.rollout_progress.total} agents on the new image
                    </span>
                    {(data.rollout_progress.drifted?.length > 0) && (
                      <span
                        className="truncate pl-2 text-gray-400"
                        title={data.rollout_progress.drifted.join(', ')}
                      >
                        rolling: {data.rollout_progress.drifted.slice(0, 6).join(', ')}
                        {data.rollout_progress.drifted.length > 6
                          ? ` +${data.rollout_progress.drifted.length - 6}`
                          : ''}
                      </span>
                    )}
                  </div>
                  <div className="h-1.5 w-full overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700">
                    <div
                      className="h-full rounded-full bg-indigo-500 transition-[width] duration-500"
                      style={{
                        width: `${Math.round(
                          (data.rollout_progress.matched / data.rollout_progress.total) * 100,
                        )}%`,
                      }}
                    />
                  </div>
                </div>
              )}
            </>
          )}
          {isLoading ? (
            <div className="flex items-center gap-2 py-4 text-sm text-gray-400"><Loader2 className="h-4 w-4 animate-spin" /> loading…</div>
          ) : versionsUnavailable ? (
            <div className="flex items-start gap-2 rounded-lg bg-red-50 px-3 py-3 text-sm text-red-700 ring-1 ring-red-100 dark:bg-red-900/15 dark:text-red-300 dark:ring-red-900/40">
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
              <div>
                <p className="font-semibold">Harness status unavailable</p>
                <p className="mt-0.5 text-xs opacity-80">Version data could not be loaded. Refresh to check again.</p>
              </div>
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[520px] text-left text-sm">
                <thead>
                  <tr className="text-[11px] uppercase tracking-wide text-gray-400">
                    <th className="py-1.5 pr-3 font-semibold">Harness</th>
                    <th className="py-1.5 pr-3 font-semibold">Current</th>
                    <th className="py-1.5 pr-3 font-semibold">Latest</th>
                    <th className="py-1.5 pr-3 font-semibold">Agents</th>
                    <th className="py-1.5 pr-3 font-semibold text-right">Action</th>
                  </tr>
                </thead>
                <tbody>
                  {harnesses.map((h) => {
                    const unused = !(h.agent_count > 0)
                    const rowRolling = rolloutInProgress && h.upgrade_available && !unused
                    return (
                    <tr key={h.harness} className="border-t border-gray-50 dark:border-gray-700/50">
                      <td className="py-2 pr-3">
                        <div className="font-medium text-gray-900 dark:text-white">{h.label}</div>
                        <div className="font-mono text-[10px] text-gray-400">{h.package}</div>
                      </td>
                      <td className="py-2 pr-3 font-mono text-xs text-gray-700 dark:text-gray-200">
                        {h.current || <span className="text-gray-400">unknown</span>}
                      </td>
                      <td className="py-2 pr-3 font-mono text-xs">
                        {rowRolling ? (
                          <span className="inline-flex items-center gap-1 text-[11px] text-indigo-600 dark:text-indigo-400">
                            <Loader2 className="h-3 w-3 animate-spin" /> rolling
                          </span>
                        ) : h.upgrade_available && !unused ? (
                          <span className="font-semibold text-amber-600 dark:text-amber-400">{h.latest}</span>
                        ) : (
                          <span className="text-gray-500">{h.latest || '—'}</span>
                        )}
                      </td>
                      <td className="py-2 pr-3 text-xs text-gray-600 dark:text-gray-300" title={(h.agents || []).join(', ')}>
                        {h.agent_count}
                      </td>
                      <td className="py-2 pr-3 text-right">
                        {unused ? (
                          <span className="text-[11px] text-gray-400">unused</span>
                        ) : rowRolling ? (
                          <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-indigo-600 dark:text-indigo-400">
                            <Loader2 className="h-3 w-3 animate-spin" /> in progress
                          </span>
                        ) : h.upgrade_available ? (
                          <button
                            type="button"
                            disabled={pending === h.harness}
                            onClick={() => onUpgrade(h)}
                            className="inline-flex items-center gap-1 rounded-md bg-indigo-600 px-2.5 py-1 text-[11px] font-semibold text-white hover:bg-indigo-700 disabled:opacity-50"
                          >
                            {pending === h.harness ? <Loader2 className="h-3 w-3 animate-spin" /> : <ArrowUpCircle className="h-3 w-3" />}
                            Upgrade fleet
                          </button>
                        ) : (
                          <span className="inline-flex items-center gap-1 text-[11px] text-green-600 dark:text-green-400"><CheckCircle2 className="h-3 w-3" /> current</span>
                        )}
                      </td>
                    </tr>
                    )
                  })}
                  {harnesses.length === 0 && (
                    <tr><td colSpan={5} className="py-3 text-sm text-gray-400">No harnesses reported yet.</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          )}
          {note && (
            <p className="mt-2 rounded-md bg-indigo-50 px-3 py-2 text-[12px] text-indigo-700 dark:bg-indigo-900/20 dark:text-indigo-300">
              {note.text}
            </p>
          )}
        </div>
      )}
    </section>
  )
}
