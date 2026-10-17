import React, { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  list: vi.fn(),
  start: vi.fn(),
  poll: vi.fn(),
  grokStart: vi.fn(),
  grokPoll: vi.fn(),
  updateEntry: vi.fn(),
  isStaff: false,
}))

vi.mock('../contexts/HiveContext', () => ({
  useHive: () => ({ isStaff: mocks.isStaff }),
}))

vi.mock('../components/ClaudeOAuthPanel', () => ({ default: () => null }))

vi.mock('../services/api', () => ({
  tokenPoolsApi: {
    list: mocks.list,
    create: vi.fn(),
    addEntry: vi.fn(),
    updateEntry: mocks.updateEntry,
    deleteEntry: vi.fn(),
    disableEntry: vi.fn(),
    checkEntry: vi.fn(),
  },
  codexDeviceAuthApi: { start: mocks.start, poll: mocks.poll },
  grokDeviceAuthApi: { start: mocks.grokStart, poll: mocks.grokPoll },
}))

import { ToastProvider } from '../components/Toast'
import TokenPools, { deviceAuthErrorMessage, retryableDeviceAuthPollError } from './TokenPools'

globalThis.IS_REACT_ACT_ENVIRONMENT = true

let container
let root

async function flushEffects() {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
    await new Promise((resolve) => window.setTimeout(resolve, 0))
    await new Promise((resolve) => window.setTimeout(resolve, 0))
  })
}

async function flushMicrotasks() {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
  })
}

beforeEach(() => {
  mocks.isStaff = false
  window.requestAnimationFrame = (callback) => window.setTimeout(callback, 0)
  window.cancelAnimationFrame = (id) => window.clearTimeout(id)
  window.HTMLElement.prototype.scrollIntoView = vi.fn()
})

afterEach(() => {
  if (root) act(() => root.unmount())
  container?.remove()
  container = null
  root = null
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('Codex re-auth deep link', () => {
  it('keeps exact terminal account-mismatch errors instead of retrying a generic 502', () => {
    const error = {
      message: 'Request failed',
      response: {
        status: 502,
        data: {
          error: 'Authorized trading@example.com, but this re-auth was started for global@example.com.',
          retryable: false,
        },
      },
    }

    expect(retryableDeviceAuthPollError(error)).toBe(false)
    expect(deviceAuthErrorMessage(error)).toContain('started for global@example.com')
  })

  it('opens and starts the exact owned entry even before client admin claims hydrate', async () => {
    const poolId = 'f8b9cebd-ab26-4390-9033-94cbaee53763'
    const entryId = '17a5a23f-7dbe-420a-872b-9e33e40dbc22'
    mocks.list.mockResolvedValue({
      data: [{
        id: poolId,
        provider: 'openai',
        owner_kind: 'user',
        display_name: 'Personal OpenAI',
        active_entry_count: 0,
        entry_count: 1,
        entries: [{
          id: entryId,
          label: 'org-openai-shizuhatrading@gmail.com',
          codex_account_email: 'shizuhatrading@gmail.com',
          codex_account_status: 'revoked',
          active: false,
        }],
      }],
    })
    mocks.start.mockResolvedValue({
      data: {
        device_auth_id: 'device-1',
        user_code: 'USER-CODE',
        verification_url: 'https://auth.openai.com/device',
        interval: 300,
        expires_in: 900,
      },
    })

    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const route = `/credentials/codex/re-auth?scope=user&pool=${poolId}&entry=${entryId}&email=shizuhatrading%40gmail.com`

    await act(async () => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <MemoryRouter initialEntries={[route]}>
            <ToastProvider>
              <TokenPools />
            </ToastProvider>
          </MemoryRouter>
        </QueryClientProvider>,
      )
    })
    await flushEffects()

    expect(mocks.list).toHaveBeenCalledWith({ scope: 'user' })
    expect(mocks.start).toHaveBeenCalledTimes(1)
    expect(container.textContent).toContain('Re-auth requested for shizuhatrading@gmail.com')
    expect(container.textContent).toContain('private/incognito OpenAI tab')
    expect(container.textContent).toContain('USER-CODE')
    expect(container.querySelector('#codex-re-auth-panel')).not.toBeNull()
  })

  it('keeps the exact device session through a transient poll failure and completes', async () => {
    vi.useFakeTimers()
    const poolId = 'pool-transient'
    const entryId = 'entry-transient'
    mocks.list.mockResolvedValue({
      data: [{
        id: poolId,
        provider: 'openai',
        owner_kind: 'user',
        display_name: 'Personal OpenAI',
        active_entry_count: 0,
        entry_count: 1,
        entries: [{
          id: entryId,
          label: 'org-openai-shizuhaglobal@gmail.com',
          codex_account_email: 'shizuhaglobal@gmail.com',
          codex_account_status: 'revoked',
          active: false,
        }],
      }],
    })
    mocks.start.mockResolvedValue({
      data: {
        device_auth_id: 'device-transient',
        user_code: 'TRANSIENT-CODE',
        verification_url: 'https://auth.openai.com/codex/device',
        interval: 0.001,
        expires_in: 900,
      },
    })
    mocks.poll
      .mockRejectedValueOnce({ response: { status: 503, data: { detail: 'edge unavailable' } } })
      .mockResolvedValueOnce({
        data: {
          status: 'authorized',
          email: 'shizuhaglobal@gmail.com',
          created: false,
        },
      })

    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const route = `/credentials/codex/re-auth?scope=user&pool=${poolId}&entry=${entryId}&email=shizuhaglobal%40gmail.com`

    await act(async () => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <MemoryRouter initialEntries={[route]}>
            <ToastProvider>
              <TokenPools />
            </ToastProvider>
          </MemoryRouter>
        </QueryClientProvider>,
      )
    })
    await flushMicrotasks()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    await flushMicrotasks()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10)
    })
    await flushMicrotasks()

    expect(mocks.poll).toHaveBeenCalledTimes(1)
    expect(container.textContent).toContain('retrying automatically')
    expect(container.textContent).toContain('TRANSIENT-CODE')

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })
    await flushMicrotasks()

    expect(mocks.poll).toHaveBeenCalledTimes(2)
    expect(mocks.poll).toHaveBeenNthCalledWith(1, {
      device_auth_id: 'device-transient',
      user_code: 'TRANSIENT-CODE',
      target_pool_id: poolId,
      target_entry_id: entryId,
    })
    expect(mocks.poll).toHaveBeenNthCalledWith(2, {
      device_auth_id: 'device-transient',
      user_code: 'TRANSIENT-CODE',
      target_pool_id: poolId,
      target_entry_id: entryId,
    })
    expect(container.textContent).toContain('Authorized shizuhaglobal@gmail.com')
    expect(container.textContent).not.toContain('Device auth failed')
  })
})

describe('Grok device auth', () => {
  it('renders device auth in the personal xAI pool with an agent-leasing choice', async () => {
    mocks.isStaff = false
    mocks.list.mockResolvedValue({
      data: [{
        id: 'xai-personal',
        provider: 'xai',
        owner_kind: 'user',
        display_name: 'Personal (xai)',
        active_entry_count: 1,
        entry_count: 2,
        entries: [{
          id: 'grok-1',
          label: 'org-xai-personal@example.com',
          account_email: 'personal@example.com',
          active: false,
          tier: '',
          priority: 100,
          quota_snapshot: {},
        }],
      }],
    })

    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    await act(async () => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <MemoryRouter initialEntries={['/token-pools']}>
            <ToastProvider>
              <TokenPools />
            </ToastProvider>
          </MemoryRouter>
        </QueryClientProvider>,
      )
    })
    await flushEffects()

    expect(container.textContent).toContain('Grok device auth')
    expect(container.textContent).toContain('Connect Grok account')
    expect(container.textContent).toContain('Available to agents immediately')
    expect(container.textContent).toContain('Advanced: add a raw xAI credential')
    expect(container.textContent).toContain('Personal reserve')
    expect(container.textContent).toContain('Allow agents')
    expect(container.textContent).toContain('Account: personal@example.com')
  })

  it('sends the personal-reserve choice through device authorization', async () => {
    mocks.isStaff = false
    mocks.list.mockResolvedValue({
      data: [{
        id: 'xai-personal',
        provider: 'xai',
        owner_kind: 'user',
        display_name: 'Personal (xai)',
        active_entry_count: 0,
        entry_count: 0,
        entries: [],
      }],
    })
    mocks.grokStart.mockResolvedValue({
      data: {
        device_code: 'grok-device-1',
        device_auth_id: 'grok-device-1',
        user_code: 'GROK-CODE',
        verification_url: 'https://accounts.x.ai/device',
        interval: 0.001,
        expires_in: 300,
      },
    })
    mocks.grokPoll.mockResolvedValue({
      data: {
        status: 'authorized',
        email: 'reserved@example.com',
        created: true,
        available_to_agents: false,
      },
    })

    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    await act(async () => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <MemoryRouter initialEntries={['/token-pools']}>
            <ToastProvider>
              <TokenPools />
            </ToastProvider>
          </MemoryRouter>
        </QueryClientProvider>,
      )
    })
    await flushEffects()

    const leasingCheckbox = container.querySelector('input[type="checkbox"]')
    const connectButton = Array.from(container.querySelectorAll('button'))
      .find((button) => button.textContent.includes('Connect Grok account'))
    vi.useFakeTimers()
    await act(async () => {
      leasingCheckbox.click()
      connectButton.click()
      await Promise.resolve()
    })
    await flushMicrotasks()
    expect(container.textContent).toContain('GROK-CODE')

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10)
    })
    await flushMicrotasks()

    expect(mocks.grokPoll).toHaveBeenCalledWith({
      device_code: 'grok-device-1',
      device_auth_id: 'grok-device-1',
      available_to_agents: false,
    })
    expect(container.textContent).toContain('personal reserve')
  })
})

describe('Persistent agent leasing policy', () => {
  it('reserves immediately through the dedicated leasing field', async () => {
    mocks.isStaff = true
    mocks.list.mockResolvedValue({
      data: [{
        id: 'openai-personal',
        provider: 'openai',
        owner_kind: 'user',
        display_name: 'Personal (openai)',
        active_entry_count: 1,
        entry_count: 1,
        entries: [{
          id: 'openai-digital',
          label: 'org-openai-shizuhadigital@gmail.com',
          account_email: 'shizuhadigital@gmail.com',
          codex_account_email: 'shizuhadigital@gmail.com',
          active: true,
          agent_leasing_enabled: true,
          tier: '',
          priority: 100,
          quota_snapshot: {},
        }],
      }],
    })
    mocks.updateEntry.mockImplementation(() => new Promise(() => {}))
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)

    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    await act(async () => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <MemoryRouter initialEntries={['/token-pools']}>
            <ToastProvider>
              <TokenPools />
            </ToastProvider>
          </MemoryRouter>
        </QueryClientProvider>,
      )
    })
    await flushEffects()

    const reserve = Array.from(container.querySelectorAll('button'))
      .find((button) => button.textContent.includes('Reserve'))
    await act(async () => reserve.click())

    expect(mocks.updateEntry).toHaveBeenCalledWith(
      'openai-personal',
      'openai-digital',
      { agent_leasing_enabled: false },
    )
    expect(container.textContent).toContain('Personal reserve')
    expect(container.textContent).toContain('Allow agents')
    confirm.mockRestore()
  })
})
