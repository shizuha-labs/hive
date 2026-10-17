import React, { act } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createRoot } from 'react-dom/client'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { HiveProvider, decodeTokenValue, useHive } from './HiveContext'

const mocks = vi.hoisted(() => ({
  meGet: vi.fn(),
  organizationsList: vi.fn(),
}))

vi.mock('../services/api', () => ({
  meApi: { get: mocks.meGet },
  organizationsApi: { list: mocks.organizationsList },
}))

globalThis.IS_REACT_ACT_ENVIRONMENT = true

let container
let root

afterEach(() => {
  if (root) act(() => root.unmount())
  container?.remove()
  root = null
  container = null
  localStorage.clear()
  mocks.meGet.mockReset()
  mocks.organizationsList.mockReset()
  window.history.replaceState({}, '', '/')
})

describe('decodeTokenValue', () => {
  it('decodes unpadded base64url JWT payloads used by Shizuha ID', () => {
    // The '-' is the URL-safe form of '+'; raw atob(payload) rejects it.
    const payload = 'eyJpc19zdGFmZiI6dHJ1ZSwidXNlcm5hbWUiOiI-In0'

    expect(decodeTokenValue(`header.${payload}.signature`)).toEqual({
      is_staff: true,
      username: '>',
    })
  })

  it('fails closed for malformed tokens', () => {
    expect(decodeTokenValue('not-a-jwt')).toBeNull()
  })
})

function ContextProbe() {
  const hive = useHive()
  return <div data-testid="auth-state">{JSON.stringify({
    loading: hive.loading,
    authenticated: hive.isAuthenticated,
    staff: hive.isStaff,
    fleetRead: hive.canReadFleet,
    username: hive.username,
    orgsLoading: hive.orgsLoading,
    hasToken: hive.hasToken,
    memberships: hive.claims?.organization_memberships ?? null,
  })}</div>
}

describe('HiveProvider cookie authentication', () => {
  it('uses the authenticated /me response when no localStorage JWT exists', async () => {
    window.history.replaceState({}, '', '/hive/agents')
    mocks.meGet.mockResolvedValue({
      data: { username: 'hritik', is_staff: true, is_superuser: false },
    })
    mocks.organizationsList.mockResolvedValue({ data: { organizations: [] } })
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    })

    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    act(() => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <HiveProvider><ContextProbe /></HiveProvider>
        </QueryClientProvider>,
      )
    })
    for (let attempt = 0; attempt < 10; attempt += 1) {
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0))
      })
      if (container.textContent.includes('"authenticated":true')) break
    }

    expect(mocks.meGet).toHaveBeenCalledTimes(1)
    expect(mocks.organizationsList).toHaveBeenCalled()
    expect(container.querySelector('[data-testid="auth-state"]').textContent).toContain(
      '"authenticated":true,"staff":true,"fleetRead":true,"username":"hritik"',
    )
    expect(container.querySelector('[data-testid="auth-state"]').textContent).toContain(
      '"orgsLoading":false',
    )
  })

  it('fails-soft to All Organizations when the org list errors', async () => {
    window.history.replaceState({}, '', '/hive/agents')
    mocks.meGet.mockResolvedValue({
      data: { username: 'hritik', is_staff: true, is_superuser: false },
    })
    mocks.organizationsList.mockRejectedValue(new Error('Admin API unreachable'))
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    })
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    act(() => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <HiveProvider><ContextProbe /></HiveProvider>
        </QueryClientProvider>,
      )
    })
    for (let attempt = 0; attempt < 10; attempt += 1) {
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0))
      })
      if (container.textContent.includes('"orgsLoading":false')) break
    }
    expect(container.querySelector('[data-testid="auth-state"]').textContent).toContain(
      '"orgsLoading":false',
    )
  })

  it('exposes JIT fleet-read capability without fabricating staff authority', async () => {
    window.history.replaceState({}, '', '/hive/agents')
    mocks.meGet.mockResolvedValue({
      data: {
        username: 'mika', is_staff: false, is_superuser: false,
        fleet_read_access: true, fleet_read_access_source: 'jit',
      },
    })
    mocks.organizationsList.mockResolvedValue({ data: { organizations: [] } })
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    })

    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    act(() => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <HiveProvider><ContextProbe /></HiveProvider>
        </QueryClientProvider>,
      )
    })
    for (let attempt = 0; attempt < 10; attempt += 1) {
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)) })
      if (container.textContent.includes('"authenticated":true')) break
    }

    expect(container.querySelector('[data-testid="auth-state"]').textContent).toContain(
      '"authenticated":true,"staff":false,"fleetRead":true,"username":"mika"',
    )
  })
})

describe('HIVE-2383 reactive access token', () => {
  function makeJwt(payload) {
    const b64 = (obj) => btoa(JSON.stringify(obj)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
    return `header.${b64(payload)}.signature`
  }

  function renderProbe(path) {
    window.history.replaceState({}, '', path)
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    })
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    act(() => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <HiveProvider><ContextProbe /></HiveProvider>
        </QueryClientProvider>,
      )
    })
  }

  async function settle() {
    for (let attempt = 0; attempt < 10; attempt += 1) {
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0))
      })
    }
  }

  it('re-decodes claims when the token is stored after mount (post-mount login/refresh path)', async () => {
    mocks.meGet.mockResolvedValue({ data: { username: 'mika', is_staff: false } })
    mocks.organizationsList.mockResolvedValue({ data: { organizations: [] } })

    // Mount with NO token — the HIVE-704 failing sequence: the login flow
    // stores the token only after the SPA mounted.
    renderProbe('/hive/overview')
    await settle()
    const probe = () => container.querySelector('[data-testid="auth-state"]').textContent
    expect(probe()).toContain('"hasToken":false')
    expect(probe()).toContain('"memberships":null')

    // The token arrives post-mount (in-app refresh write / login completion).
    act(() => {
      localStorage.setItem('shizuha_access_token', makeJwt({
        username: 'mika',
        organization_memberships: { 1: 'member', 3: 'member', 2: 'member', 15: 'owner' },
      }))
      window.dispatchEvent(new CustomEvent('shizuha:token-changed'))
    })
    await settle()

    // The claims re-decode — the gate source is no longer stale/null.
    expect(probe()).toContain('"hasToken":true')
    const state = JSON.parse(probe())
    expect(state.memberships).toEqual({ 1: 'member', 3: 'member', 2: 'member', 15: 'owner' })
  })

  it('re-reads the token on cross-tab storage events', async () => {
    mocks.meGet.mockResolvedValue({ data: { username: 'mika', is_staff: false } })
    mocks.organizationsList.mockResolvedValue({ data: { organizations: [] } })

    renderProbe('/hive/overview')
    await settle()
    const probe = () => container.querySelector('[data-testid="auth-state"]').textContent
    expect(probe()).toContain('"hasToken":false')

    act(() => {
      localStorage.setItem('shizuha_access_token', makeJwt({
        organization_memberships: { 1: 'member' },
      }))
      window.dispatchEvent(new StorageEvent('storage', { key: 'shizuha_access_token' }))
    })
    await settle()

    expect(probe()).toContain('"hasToken":true')
    expect(probe()).toContain('"memberships":{"1":"member"}')
  })
})
