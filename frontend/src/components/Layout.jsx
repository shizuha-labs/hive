import { Outlet, NavLink } from 'react-router-dom'
import { useEffect, useId, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  LayoutGrid,
  LayoutDashboard,
  Cpu,
  KeyRound,
  SlidersHorizontal,
  Users,
  Menu,
  Moon,
  Sun,
  X,
  ShieldCheck,
  Layers,
  Building2,
  ChevronDown,
  Check,
  Activity,
} from 'lucide-react'
import clsx from 'clsx'
import { AppSwitcher } from '@shizuha/ui'
import { useHive } from '../contexts/HiveContext'
import { meApi } from '../services/api'
import { useTheme } from '../hooks/useTheme'
import HiveWordmark from './HiveWordmark'
import HaneChip from './HaneChip'

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

function useEnabledServices() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['hive-me-services'],
    queryFn: () => meApi.services().then((response) => response.data),
    retry: false,
    staleTime: 60_000,
  })

  if (isLoading) return { services: null, status: 'loading' }
  if (isError) return { services: null, status: 'error' }
  return { services: data?.service_ids || [], status: 'ready' }
}

export function OrgSwitcher() {
  const { organizations, currentOrganization, switchOrganization, orgsLoading } = useHive()
  const [open, setOpen] = useState(false)
  const rootRef = useRef(null)
  const triggerRef = useRef(null)
  const menuRef = useRef(null)
  const pendingFocusRef = useRef(null)
  const menuId = useId()
  const label = currentOrganization?.name || 'All Organizations'

  const closeMenu = (focusTarget = null) => {
    pendingFocusRef.current = focusTarget
    setOpen(false)
  }

  // Move focus only after React has unmounted the popup. Focusing before the
  // close commit lets the browser move focus back to <body> when the focused
  // menu item disappears.
  useEffect(() => {
    if (open || !pendingFocusRef.current) return
    const focusTarget = pendingFocusRef.current
    pendingFocusRef.current = null
    focusTarget.focus()
  }, [open])

  useEffect(() => {
    if (!open) return undefined

    const dismissOutside = (event) => {
      if (!rootRef.current?.contains(event.target)) {
        pendingFocusRef.current = null
        setOpen(false)
      }
    }
    const dismissWithEscape = (event) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      pendingFocusRef.current = triggerRef.current
      setOpen(false)
    }

    // Capture pointerdown so the menu closes before an outside link's click
    // is dispatched. The click then reaches the link instead of a stale layer.
    document.addEventListener('pointerdown', dismissOutside, true)
    document.addEventListener('keydown', dismissWithEscape)
    return () => {
      document.removeEventListener('pointerdown', dismissOutside, true)
      document.removeEventListener('keydown', dismissWithEscape)
    }
  }, [open])

  const focusMenuItem = (position = 'selected') => {
    const items = [...(menuRef.current?.querySelectorAll('[role="menuitemradio"]') || [])]
    const target = position === 'first'
      ? items[0]
      : position === 'last'
        ? items.at(-1)
        : items.find((item) => item.getAttribute('aria-checked') === 'true') || items[0]
    target?.focus()
  }

  const openMenu = (focusPosition = 'selected') => {
    setOpen(true)
    queueMicrotask(() => focusMenuItem(focusPosition))
  }

  const handleTriggerKeyDown = (event) => {
    if (!['ArrowDown', 'ArrowUp'].includes(event.key)) return
    event.preventDefault()
    openMenu(event.key === 'ArrowUp' ? 'last' : 'first')
  }

  const moveFocusOutsideMenu = (reverse) => {
    const candidates = [...document.querySelectorAll(FOCUSABLE_SELECTOR)].filter((element) => {
      if (element.tabIndex < 0 || element.closest('[hidden], [inert]')) return false
      const style = window.getComputedStyle?.(element)
      return style?.display !== 'none' && style?.visibility !== 'hidden'
    })
    const triggerIndex = candidates.indexOf(triggerRef.current)
    const step = reverse ? -1 : 1
    let target = null
    for (let index = triggerIndex + step; index >= 0 && index < candidates.length; index += step) {
      if (!rootRef.current?.contains(candidates[index])) {
        target = candidates[index]
        break
      }
    }

    closeMenu(target || triggerRef.current)
  }

  const selectOrganization = (organization, event) => {
    switchOrganization(organization)
    // Keyboard activation emits a synthetic click with detail=0. Restore the
    // trigger so the next Tab continues from the menu button; pointer users
    // keep their natural pointer focus behavior.
    closeMenu(event.detail === 0 ? triggerRef.current : null)
  }

  const handleMenuKeyDown = (event) => {
    if (event.key === 'Tab') {
      event.preventDefault()
      moveFocusOutsideMenu(event.shiftKey)
      return
    }
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
    event.preventDefault()
    const items = [...menuRef.current.querySelectorAll('[role="menuitemradio"]')]
    if (!items.length) return
    const index = items.indexOf(document.activeElement)
    if (event.key === 'Home') items[0]?.focus()
    else if (event.key === 'End') items.at(-1)?.focus()
    else if (event.key === 'ArrowDown') items[(index + 1 + items.length) % items.length]?.focus()
    else items[(index - 1 + items.length) % items.length]?.focus()
  }

  return (
    <div ref={rootRef} className="relative px-4 py-3" data-org-switcher-state={open ? 'open' : 'closed'}>
      <button
        ref={triggerRef}
        type="button"
        aria-controls={menuId}
        aria-expanded={open}
        aria-haspopup="menu"
        onClick={() => (open ? setOpen(false) : openMenu())}
        onKeyDown={handleTriggerKeyDown}
        className="flex w-full items-center gap-2 rounded-lg border border-white/10 bg-white/[0.04] px-3 py-2 text-left text-sm text-gray-200 hover:bg-white/[0.08] transition-colors"
      >
        {currentOrganization ? (
          <Building2 className="h-4 w-4 shrink-0 text-gray-500" />
        ) : (
          <Layers className="h-4 w-4 shrink-0 text-gray-500" />
        )}
        <span className="min-w-0 flex-1 truncate">{orgsLoading ? 'Loading organizations…' : label}</span>
        <ChevronDown className="h-4 w-4 shrink-0 text-gray-400" />
      </button>

      {open && (
        <div
          ref={menuRef}
          id={menuId}
          role="menu"
          aria-label="Organizations"
          aria-orientation="vertical"
          onKeyDown={handleMenuKeyDown}
          className="absolute left-4 right-4 top-14 z-50 max-h-80 overflow-auto rounded-lg border border-gray-200 bg-white py-1 shadow-lg dark:border-gray-700 dark:bg-gray-800"
        >
          <button
            type="button"
            role="menuitemradio"
            aria-checked={!currentOrganization}
            tabIndex={-1}
            onClick={(event) => selectOrganization(null, event)}
            className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-700"
          >
            <Layers className="h-4 w-4 text-gray-500" />
            <span className="min-w-0 flex-1 truncate">All Organizations</span>
            {!currentOrganization && <Check className="h-4 w-4 text-primary-600" />}
          </button>
          {organizations.map((org) => (
            <button
              key={org.slug || org.id}
              type="button"
              role="menuitemradio"
              aria-checked={currentOrganization?.slug === org.slug}
              tabIndex={-1}
              onClick={(event) => selectOrganization(org, event)}
              className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-700"
            >
              <Building2 className="h-4 w-4 text-gray-500" />
              <span className="min-w-0 flex-1 truncate">{org.name || org.slug}</span>
              {currentOrganization?.slug === org.slug && <Check className="h-4 w-4 text-primary-600" />}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

const navigation = [
  { name: 'Overview', href: '/overview', icon: LayoutGrid },
  { name: 'Create org', href: '/create-organization', icon: Building2 },
  // Unified agents surface: agents are owned by the viewed account; org/team
  // placement is metadata, not a separate product mode.
  { name: 'Agents', href: '/agents', icon: Cpu },
  { name: 'Teams', href: '/teams', icon: Users, staffOnly: true },
  { name: 'Credential grants', href: '/credentials', icon: KeyRound, staffOnly: true },
  { name: 'Inference Key', href: '/inference-key', icon: KeyRound },
  { name: 'Token Pools', href: '/token-pools', icon: SlidersHorizontal },
  { name: 'Settings · Providers', href: '/settings/providers', icon: SlidersHorizontal },
  { name: 'Subscriptions', href: '/subscriptions', icon: Activity },
]

export default function Layout() {
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const serviceAccess = useEnabledServices()
  const { username, email, isStaff } = useHive()
  const { isDark, toggleTheme } = useTheme()
  const ThemeIcon = isDark ? Sun : Moon
  const themeLabel = isDark ? 'Switch to light mode' : 'Switch to dark mode'

  return (
    <div className="min-h-screen bg-[#f4f5f9] dark:bg-[#101017]">
      {/* Mobile backdrop */}
      {sidebarOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/50 lg:hidden"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      {/* Sidebar */}
      <aside
        className={clsx(
          // `dark` on the aside: the console sidebar is PERMANENTLY dark chrome, so
          // every dark: variant inside (wordmark, app switcher, popovers) renders its
          // dark form even when the app theme is light.
          'dark hv-sidebar fixed inset-y-0 left-0 z-50 flex w-64 flex-col transform transition-transform lg:translate-x-0',
          sidebarOpen ? 'translate-x-0' : '-translate-x-full'
        )}
      >
        {/* Logo / header */}
        <div className="h-16 flex items-center justify-between px-4 border-b border-white/[0.06]">
          <div className="flex items-center gap-2 min-w-0">
            <AppSwitcher
              currentAppId="hive"
              enabledServices={serviceAccess.services}
              accessStatus={serviceAccess.status}
              variant="compact"
              enableShortcuts
              popoverPosition="left"
            />
            <HiveWordmark size="md" showSubtitle />
          </div>
          <button
            type="button"
            title={themeLabel}
            aria-label={themeLabel}
            onClick={toggleTheme}
            className="hidden lg:inline-flex h-9 w-9 items-center justify-center rounded-lg text-gray-400 hover:bg-white/[0.06] hover:text-gray-100"
          >
            <ThemeIcon className="h-4 w-4" />
          </button>
          <button
            className="lg:hidden p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700"
            onClick={() => setSidebarOpen(false)}
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        <OrgSwitcher />

        {/* Nav */}
        <nav className="flex-1 overflow-y-auto p-4 space-y-1">
          {navigation.filter((item) => !item.staffOnly || isStaff).map((item) => (
            <NavLink
              key={item.name}
              to={item.href}
              className={({ isActive }) =>
                clsx(
                  'hv-nav-item',
                  isActive && 'hv-nav-item-active'
                )
              }
              onClick={() => setSidebarOpen(false)}
            >
              <item.icon className="h-5 w-5" />
              {item.name}
            </NavLink>
          ))}
        </nav>

        {/* User / role footer */}
        <div className="shrink-0 p-4 border-t border-white/[0.06]">
          <HaneChip className="mb-3" />
          <div className="flex items-center gap-2 px-1 mb-3">
            <div className="h-8 w-8 rounded-full bg-primary-100 dark:bg-primary-900/30 text-primary-600 flex items-center justify-center text-sm font-semibold shrink-0">
              {(username || '?').charAt(0).toUpperCase()}
            </div>
            <div className="min-w-0">
              <div className="text-sm font-medium truncate">{username}</div>
              <div className="text-xs text-gray-500 truncate">{email || '—'}</div>
            </div>
          </div>
          {isStaff && (
            <div className="flex items-center gap-1.5 px-2 py-1 mb-2 rounded-md bg-amber-50 dark:bg-amber-900/20 text-amber-700 dark:text-amber-400 text-xs">
              <ShieldCheck className="h-3.5 w-3.5" />
              Admin — lifecycle controls enabled
            </div>
          )}
          <a
            href="/id/account"
            className="block text-center text-sm text-gray-400 hover:bg-white/[0.06] hover:text-gray-100 rounded-lg py-2 transition-colors"
          >
            Back to Account
          </a>
        </div>
      </aside>

      {/* Main */}
      <div className="lg:pl-64">
        <header className="h-16 bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between px-4 lg:hidden">
          <button
            className="p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700"
            onClick={() => setSidebarOpen(true)}
          >
            <Menu className="h-5 w-5" />
          </button>
          <HiveWordmark size="sm" />
          <div className="flex items-center gap-2">
          <HaneChip compact tone="light" />
          <button
            type="button"
            title={themeLabel}
            aria-label={themeLabel}
            onClick={toggleTheme}
            className="inline-flex h-9 w-9 items-center justify-center rounded-lg text-gray-500 hover:bg-gray-100 hover:text-gray-900 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-gray-100"
          >
            <ThemeIcon className="h-4 w-4" />
          </button>
          </div>
        </header>

        <main className="hv-grid-bg min-h-screen p-4 lg:p-8"><div className="relative z-10 max-w-7xl mx-auto">
          <Outlet />
        </div></main>
      </div>
    </div>
  )
}
