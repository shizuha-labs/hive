import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { Bot, ChevronDown, Search, SlidersHorizontal } from 'lucide-react'
import clsx from 'clsx'

export default function SubscriptionPlanFilter({ options, selected, mode, onChange, visibleCount, totalCount }) {
  const [open, setOpen] = useState(false)
  const [search, setSearch] = useState('')
  const [panelStyle, setPanelStyle] = useState({})
  const containerRef = useRef(null)
  const triggerRef = useRef(null)
  const searchRef = useRef(null)
  const panelId = useId()

  useLayoutEffect(() => {
    if (!open) return undefined
    const positionPanel = () => {
      const rect = triggerRef.current.getBoundingClientRect()
      const width = Math.min(320, window.innerWidth - 24)
      const below = window.innerHeight - rect.bottom - 20
      const above = rect.top - 20
      const upwards = below < 420 && above > below
      setPanelStyle({
        width,
        left: Math.max(12, Math.min(rect.left, window.innerWidth - width - 12)),
        maxHeight: Math.max(0, upwards ? above : below),
        ...(upwards ? { bottom: window.innerHeight - rect.top + 8 } : { top: rect.bottom + 8 }),
      })
    }
    positionPanel()
    window.addEventListener('resize', positionPanel)
    window.addEventListener('scroll', positionPanel, true)
    return () => {
      window.removeEventListener('resize', positionPanel)
      window.removeEventListener('scroll', positionPanel, true)
    }
  }, [open])

  useEffect(() => {
    if (!open) return undefined
    searchRef.current?.focus()
    const outside = (event) => {
      if (!containerRef.current?.contains(event.target)) setOpen(false)
    }
    document.addEventListener('pointerdown', outside)
    return () => document.removeEventListener('pointerdown', outside)
  }, [open])

  const selectedSet = new Set(selected)
  const visibleOptions = options.filter((option) =>
    `${option.providerLabel} ${option.label}`.toLowerCase().includes(search.trim().toLowerCase()),
  )
  const groups = [...new Set(visibleOptions.map((option) => option.provider))]
  const selectedCount = options.filter((option) => selectedSet.has(option.key)).length
  const caption = mode === 'agents' ? 'Agent plans' : mode === 'all' ? 'All plans' : `${selectedCount} plan${selectedCount === 1 ? '' : 's'} selected`
  const presetClass = (active) => clsx(
    'rounded-lg px-3 py-1.5 text-xs font-medium transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-amber-500',
    active ? 'bg-amber-500/15 text-amber-700 dark:text-amber-300' : 'text-gray-500 hover:bg-gray-500/10 dark:text-gray-400',
  )

  return (
    <div className="hv-panel flex flex-wrap items-center justify-between gap-3 px-4 py-3">
      <div className="flex min-w-0 flex-col items-start gap-3 sm:flex-row sm:flex-wrap sm:items-center">
        <div className="hv-kicker flex items-center gap-2"><SlidersHorizontal className="h-3.5 w-3.5" />Subscription plans</div>
        <div
          ref={containerRef}
          className="relative"
          onBlur={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false)
          }}
          onKeyDown={(event) => {
            if (event.key === 'Escape' && open) {
              event.preventDefault()
              event.stopPropagation()
              setOpen(false)
              triggerRef.current?.focus()
            }
          }}
        >
          <button
            ref={triggerRef}
            type="button"
            aria-label={`Subscription plans: ${caption}`}
            aria-expanded={open}
            aria-controls={panelId}
            onClick={() => setOpen((value) => !value)}
            className="inline-flex items-center gap-2 rounded-xl border border-amber-400/40 bg-amber-500/[0.06] px-3 py-2 text-sm font-medium text-amber-700 transition-colors hover:bg-amber-500/15 focus-visible:outline focus-visible:outline-2 focus-visible:outline-amber-500 dark:text-amber-300"
          >
            <Bot className="h-4 w-4" />{caption}
            <span className="rounded-md bg-amber-500/10 px-1.5 text-xs tabular-nums">{selectedCount}</span>
            <ChevronDown className={clsx('h-3.5 w-3.5 transition-transform', open && 'rotate-180')} />
          </button>
          {open && (
            <div id={panelId} style={panelStyle} className="fixed z-40 flex flex-col overflow-hidden rounded-2xl border border-gray-200/80 bg-white/95 shadow-xl backdrop-blur-xl dark:border-white/10 dark:bg-[#191921]/95">
              <div className="shrink-0 border-b border-gray-100 p-3 dark:border-white/[0.06]">
                <div className="mb-3 flex items-center gap-1">
                  <button type="button" className={presetClass(mode === 'agents')} onClick={() => onChange({ mode: 'agents' })}>Agent plans</button>
                  <button type="button" className={presetClass(mode === 'all')} onClick={() => onChange({ mode: 'all' })}>All plans</button>
                  <button type="button" className={presetClass(mode === 'custom' && !selected.length)} onClick={() => onChange({ mode: 'custom', keys: [] })}>Clear</button>
                </div>
                <label className="flex items-center gap-2 rounded-lg border border-gray-200 px-2.5 dark:border-white/10">
                  <Search className="h-3.5 w-3.5 shrink-0 text-gray-400" />
                  <input ref={searchRef} aria-label="Search subscription plans" placeholder="Find a plan or provider…" value={search} onChange={(event) => setSearch(event.target.value)} className="min-w-0 flex-1 bg-transparent py-2 text-xs outline-none" />
                </label>
              </div>
              <div className="min-h-0 max-h-80 overflow-y-auto overscroll-contain p-2">
                {groups.map((provider) => {
                  const rows = visibleOptions.filter((option) => option.provider === provider)
                  return (
                    <fieldset key={provider} className="mb-2 min-w-0 last:mb-0">
                      <legend className="px-2 py-2 font-mono text-[10px] uppercase tracking-wider text-gray-400">{rows[0].providerLabel}</legend>
                      {rows.map((option) => (
                        <label key={option.key} className="flex cursor-pointer items-center gap-3 rounded-lg px-2 py-2 transition-colors hover:bg-amber-500/[0.07]">
                          <input type="checkbox" checked={selectedSet.has(option.key)} onChange={() => onChange({ mode: 'custom', keys: selectedSet.has(option.key) ? selected.filter((key) => key !== option.key) : [...selected, option.key] })} className="h-4 w-4 shrink-0 accent-amber-500" />
                          <span className="min-w-0 flex-1">
                            <span className="block text-sm font-medium">{option.label}</span>
                            <span className="block text-[10px] text-gray-400">{option.unknown ? 'Plan not detected' : option.agentDefault ? 'Included in agent view' : 'Available to include'}</span>
                          </span>
                          <span className="hv-chip tabular-nums">{option.count}</span>
                        </label>
                      ))}
                    </fieldset>
                  )
                })}
                {!visibleOptions.length && <p className="px-2 py-5 text-center text-xs text-gray-400">No matching plans</p>}
              </div>
              <div className="shrink-0 border-t border-gray-100 px-4 py-3 text-[11px] leading-relaxed text-gray-500 dark:border-white/[0.06] dark:text-gray-400">Free, Go and unknown plans are hidden by default. Choose any plan to include it. Plan filters do not change leasing permissions.</div>
            </div>
          )}
        </div>
        <span className="text-xs text-gray-500 dark:text-gray-400">{mode === 'agents' ? 'Coding subscription tiers' : 'Choose multiple plans across providers'}</span>
      </div>
      <span role="status" className="text-xs text-gray-500 dark:text-gray-400">Showing <strong className="font-semibold text-gray-700 dark:text-gray-200">{visibleCount}</strong> of {totalCount} accounts</span>
    </div>
  )
}
