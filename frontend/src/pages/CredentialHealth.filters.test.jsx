import React, { act } from 'react'
import { createRoot } from 'react-dom/client'
import { Simulate } from 'react-dom/test-utils'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import CredentialHealth from './CredentialHealth'
import { tokenPoolsApi } from '../services/api'
import { subscriptionPlan, subscriptionPlanOptions } from '../lib/subscriptionPlans'

vi.mock('../contexts/HiveContext', () => ({ useHive: () => ({ isStaff: false }) }))
vi.mock('../components/Toast', () => ({ useToast: () => ({ success: vi.fn(), error: vi.fn() }) }))

globalThis.IS_REACT_ACT_ENVIRONMENT = true
let root, container, queryClient

const account = (provider, plan, label, health = 'active') => ({
  entry_id: label, pool_id: provider, provider, label, health,
  provider_label: provider === 'openai' ? 'Codex / OpenAI' : 'Grok / xAI',
  quota: { plan }, quota_status: { status: 'ok' },
})
const accounts = [
  account('openai', 'pro', 'codex-pro'),
  account('openai', 'free', 'codex-free'),
  account('openai', 'go', 'codex-go'),
  account('xai', 'Free', 'grok-free'),
  account('xai', 'X Premium+', 'grok-paid', 'cooling'),
  account('xai', null, 'grok-unknown'),
]
const data = (rows) => ({ accounts: rows, summary: { total: rows.length, leaseable: 4, cooling: 1 } })

async function renderPage(rows = accounts) {
  vi.spyOn(tokenPoolsApi, 'credentialHealth').mockResolvedValue({ data: data(rows) })
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  queryClient.setQueryData(['credential-health', 'user'], data(rows))
  await act(async () => root.render(
    <QueryClientProvider client={queryClient}><MemoryRouter><CredentialHealth /></MemoryRouter></QueryClientProvider>,
  ))
}

const button = (text) => [...container.querySelectorAll('button')].find((node) => node.textContent === text)
const trigger = () => container.querySelector('button[aria-controls]')
const rowLabels = () => [...container.querySelectorAll('tbody tr')].map((row) => row.querySelector('td .font-medium').textContent)
const click = async (node) => { await act(async () => Simulate.click(node)) }
const refreshData = async (rows) => {
  await act(async () => {
    queryClient.setQueryData(['credential-health', 'user'], data(rows))
    // React Query batches observer notifications on its next scheduled tick.
    await new Promise((resolve) => setTimeout(resolve, 0))
  })
}

afterEach(() => {
  if (root) act(() => root.unmount())
  queryClient?.clear()
  container?.remove()
  vi.restoreAllMocks()
  root = null
})

describe('Subscriptions plan filters', () => {
  it('defaults to coding plans, keeps cooling accounts and labels scope-wide health totals', async () => {
    await renderPage()
    expect(rowLabels()).toEqual(['codex-pro', 'grok-paid'])
    expect(container.querySelector('[role="status"]').textContent).toBe('Showing 2 of 6 accounts')
    expect(container.textContent).toContain('Scope totals · all subscription plans')
    expect(trigger().textContent).toContain('Agent plans')
  })

  it('selects multiple provider-specific plans and preserves them through query refresh', async () => {
    await renderPage()
    await click(trigger())
    await click(button('Clear'))
    const freeBoxes = [...container.querySelectorAll('fieldset')].map((group) =>
      [...group.querySelectorAll('label')].find((label) => label.querySelector('.font-medium')?.textContent === 'Free').querySelector('input'),
    )
    await act(async () => Simulate.change(freeBoxes[0], { target: { checked: true } }))
    expect(rowLabels()).toEqual(['codex-free'])
    await act(async () => Simulate.change(freeBoxes[1], { target: { checked: true } }))
    expect(rowLabels()).toEqual(['codex-free', 'grok-free'])
    await refreshData([...accounts, account('openai', 'plus', 'new-plus')])
    expect(rowLabels()).toEqual(['codex-free', 'grok-free'])
    expect(container.querySelector('[role="status"]').textContent).toBe('Showing 2 of 7 accounts')
  })

  it('supports all, clear, recovery, search, unknown plans and restoring the default', async () => {
    await renderPage()
    await click(trigger())
    expect(document.activeElement.getAttribute('aria-label')).toBe('Search subscription plans')
    await click(button('All plans'))
    expect(rowLabels()).toHaveLength(6)
    await click(button('Clear'))
    expect(rowLabels()).toHaveLength(0)
    expect(container.textContent).toContain('No accounts match the selected plans')
    const search = container.querySelector('input[aria-label="Search subscription plans"]')
    await act(async () => Simulate.change(search, { target: { value: 'unknown' } }))
    const checkbox = container.querySelector('fieldset input')
    await act(async () => Simulate.change(checkbox, { target: { checked: true } }))
    expect(rowLabels()).toEqual(['grok-unknown'])
    await click(button('Agent plans'))
    expect(rowLabels()).toEqual(['codex-pro', 'grok-paid'])
    await click(button('Clear'))
    await click(button('Show all plans'))
    expect(rowLabels()).toHaveLength(6)
    expect(tokenPoolsApi.credentialHealth).toHaveBeenCalledWith({ scope: 'user' })
  })

  it('keeps the default preset live when an asynchronous quota refresh discovers new plans', async () => {
    await renderPage()
    await refreshData([...accounts, account('xai', 'SuperGrok', 'new-grok')])
    expect(rowLabels()).toEqual(['codex-pro', 'grok-paid', 'new-grok'])
  })

  it('closes on Escape, restores trigger focus and closes on an outside pointer event', async () => {
    await renderPage()
    await click(trigger())
    await act(async () => document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
    expect(trigger().getAttribute('aria-expanded')).toBe('false')
    expect(document.activeElement).toBe(trigger())
    await click(trigger())
    await act(async () => document.body.dispatchEvent(new Event('pointerdown', { bubbles: true })))
    expect(trigger().getAttribute('aria-expanded')).toBe('false')
  })
})

describe('Plan classification', () => {
  it.each(['Free', 'Go', 'X Basic', 'X Premium', 'SuperGrok Lite'])('excludes lower-tier %s from the agent preset', (plan) => {
    for (const provider of ['openai', 'xai']) expect(subscriptionPlan(account(provider, plan, 'test')).agentDefault).toBe(false)
  })

  it('normalizes case and spacing without guessing from quota, health or unknown names', () => {
    expect(subscriptionPlan(account('xai', '  SUPERGROK   HEAVY ', 'test')).key).toBe('xai:supergrok heavy')
    expect(subscriptionPlan(account('openai', ' PRO ', 'test')).agentDefault).toBe(true)
    expect(subscriptionPlan(account('xai', null, 'test')).unknown).toBe(true)
    expect(subscriptionPlan(account('xai', 'New plan', 'test')).agentDefault).toBe(false)
    expect(subscriptionPlanOptions([account('openai', 'pro', '1'), account('openai', ' PRO ', '2')])[0].count).toBe(2)
  })

  it('recognizes provider tier aliases emitted by the quota fetchers', () => {
    expect(subscriptionPlan(account('google', 'Google AI Pro (standard-tier)', 'google')).agentDefault).toBe(true)
    expect(subscriptionPlan(account('google', 'Antigravity (free-tier)', 'google')).agentDefault).toBe(false)
    expect(subscriptionPlan(account('anthropic', 'max_20x', 'claude')).key).toBe('anthropic:max 20x')
    expect(subscriptionPlan(account('anthropic', 'max_5x', 'claude')).agentDefault).toBe(true)
  })
})
