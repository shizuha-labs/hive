import { createContext, useContext, useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { meApi, organizationsApi } from '../services/api'

const HiveContext = createContext(null)
const CURRENT_ORG_KEY = 'shizuha_current_org'

/**
 * Decode the shizuha-id JWT access token (no verification — display/gating only;
 * the backend is the real authority on every mutating call).
 */
export function decodeTokenValue(token) {
  try {
    if (!token) return null
    const parts = token.split('.')
    if (parts.length !== 3) return null
    const base64 = parts[1].replace(/-/g, '+').replace(/_/g, '/')
    const padded = base64.padEnd(base64.length + ((4 - (base64.length % 4)) % 4), '=')
    return JSON.parse(atob(padded))
  } catch {
    return null
  }
}

function storedAccessToken() {
  return localStorage.getItem('shizuha_access_token') || ''
}

/**
 * HIVE-2383: the access token is NOT guaranteed to exist at mount. The login
 * flow can store it after the SPA mounted (the in-app refresh path in
 * services/api.js writes post-mount), so a mount-time-only read captures ''
 * forever and the derived claims stay null — the org-membership gate then
 * fails open (HIVE-704). Subscribe to token changes instead: the custom event
 * fired by the in-app writer plus the cross-tab `storage` event.
 */
export function useAccessToken() {
  const [token, setToken] = useState(storedAccessToken)
  useEffect(() => {
    const reread = () => setToken(storedAccessToken())
    window.addEventListener('shizuha:token-changed', reread)
    window.addEventListener('storage', reread)
    return () => {
      window.removeEventListener('shizuha:token-changed', reread)
      window.removeEventListener('storage', reread)
    }
  }, [])
  return token
}

export function HiveProvider({ children }) {
  const token = useAccessToken()
  const claims = useMemo(() => decodeTokenValue(token), [token])
  const currentPath = window.location.pathname.replace(/\/+$/, '') || '/'
  const isPublicLanding = currentPath === '/' || currentPath === '/hive'
  const [currentOrganization, setCurrentOrganization] = useState(null)

  // Owner context (entitlements, credits) — also a cheap auth probe. If this
  // 401s the axios interceptor redirects to login.
  const { data: me, isLoading } = useQuery({
    queryKey: ['hive-me'],
    queryFn: () => meApi.get().then((r) => r.data),
    retry: false,
    // Authenticated app routes may use the shared ID cookie even when this
    // origin has no localStorage token.  Probe the backend there; keep the
    // public /hive landing page anonymous when no token is present.
    enabled: !!token || !isPublicLanding,
  })

  // Lifecycle controls are admin-only. The fleet API itself is IsAdminUser, so
  // a non-staff user simply gets empty fleet data + disabled controls.
  const isStaff = Boolean(me?.is_staff || me?.is_superuser || claims?.is_staff || claims?.is_superuser)
  const isSuperuser = Boolean(me?.is_superuser || claims?.is_superuser)
  const canReadFleet = Boolean(isStaff || me?.fleet_read_access)

  const {
    data: organizations = [],
    isPending: orgsPending,
    isError: orgsError,
  } = useQuery({
    queryKey: ['hive-organizations'],
    queryFn: () => organizationsApi.list().then((r) => r.data?.organizations || r.data?.results || r.data || []),
    retry: false,
    staleTime: 60_000,
    // Cookie-auth /hive/agents has no localStorage JWT (`claims` empty) but
    // still needs the org switcher. Never block the chrome on a hung Admin
    // hop — fail-soft to All Organizations (async-frontends).
    enabled: !!claims || !isPublicLanding,
  })
  const orgsLoading = Boolean(orgsPending && !orgsError)

  useEffect(() => {
    if (!organizations.length) {
      setCurrentOrganization(null)
      return
    }
    const saved = localStorage.getItem(CURRENT_ORG_KEY)
    if (!saved || saved === '__all__') {
      setCurrentOrganization(null)
      return
    }
    setCurrentOrganization(organizations.find((org) => org.slug === saved) || null)
  }, [organizations])

  const switchOrganization = (org) => {
    setCurrentOrganization(org || null)
    localStorage.setItem(CURRENT_ORG_KEY, org?.slug || '__all__')
  }

  const value = {
    claims,
    me,
    loading: isLoading,
    hasToken: Boolean(token),
    isAuthenticated: Boolean(me || claims),
    isStaff,
    isSuperuser,
    canReadFleet,
    username: me?.username || claims?.username || claims?.preferred_username || 'You',
    email: me?.email || claims?.email || '',
    organizations,
    currentOrganization,
    currentOrgSlug: currentOrganization?.slug || '',
    switchOrganization,
    orgsLoading,
  }

  return <HiveContext.Provider value={value}>{children}</HiveContext.Provider>
}

export function useHive() {
  const ctx = useContext(HiveContext)
  if (!ctx) throw new Error('useHive must be used within a HiveProvider')
  return ctx
}

export default HiveContext
