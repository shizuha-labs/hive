import React, { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  list: vi.fn(),
  upsert: vi.fn(),
  remove: vi.fn(),
  validate: vi.fn(),
  provision: vi.fn(),
  rotate: vi.fn(),
  disable: vi.fn(),
  currentOrg: { id: 7, slug: 'acme', name: 'Acme Corp' },
  showToast: vi.fn(),
}))

vi.mock('../contexts/HiveContext', () => ({
  useHive: () => ({
    isStaff: false,
    currentOrgSlug: mocks.currentOrg.slug,
    currentOrganization: mocks.currentOrg,
  }),
}))

vi.mock('../components/Toast', () => ({
  useToast: () => ({ showToast: mocks.showToast }),
}))

vi.mock('../services/api', () => ({
  orgProviderRefsApi: {
    list: mocks.list,
    upsert: mocks.upsert,
    remove: mocks.remove,
    validate: mocks.validate,
    provision: mocks.provision,
    rotate: mocks.rotate,
    disable: mocks.disable,
  },
}))

import OrgProviders from './OrgProviders'

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

function render() {
  container = document.createElement('div')
  document.body.appendChild(container)
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  root = createRoot(container)
  act(() => {
    root.render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <OrgProviders />
        </MemoryRouter>
      </QueryClientProvider>
    )
  })
}

function setValue(el, value) {
  const isSelect = el instanceof window.HTMLSelectElement
  const proto = isSelect ? window.HTMLSelectElement.prototype : window.HTMLInputElement.prototype
  const setter = Object.getOwnPropertyDescriptor(proto, 'value').set
  act(() => setter.call(el, value))
  el.dispatchEvent(new Event(isSelect ? 'change' : 'input', { bubbles: true }))
}

function clickButton(label) {
  const btn = [...container.querySelectorAll('button')].find((b) => b.textContent.includes(label))
  if (!btn) throw new Error(`button not found: ${label}`)
  act(() => btn.click())
  return btn
}

const REF_A = {
  id: 1,
  org_id: 7,
  cortex_provider_id: '11111111-1111-1111-1111-111111111111',
  cortex_key_ref: 'cred-abc',
  default_models: ['gpt-5.6'],
  status: 'active',
  provider_type: 'openai',
  display_name: 'Acme OpenAI',
  last_validation_result: 'ok',
  per_agent_overrides: {},
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.list.mockResolvedValue({ data: { results: [] } })
  mocks.upsert.mockResolvedValue({ data: {} })
  mocks.remove.mockResolvedValue({ data: {} })
  mocks.validate.mockResolvedValue({ data: { validated: true } })
  mocks.provision.mockResolvedValue({ data: { ref: {}, validation: { validated: true } } })
  mocks.rotate.mockResolvedValue({ data: { ref: {}, rotated: true } })
  mocks.disable.mockResolvedValue({ data: { ref: {}, disabled: true } })
})

afterEach(() => {
  act(() => root?.unmount())
  container?.remove()
  root = null
})

describe('OrgProviders (HIVE-1889 + HIVE-2161 slice 2)', () => {
  it('lists org-scoped provider credentials with type and status badges', async () => {
    mocks.list.mockResolvedValue({ data: { results: [REF_A] } })
    render()
    await flushEffects()
    expect(mocks.list).toHaveBeenCalledWith(7)
    expect(container.textContent).toContain('Acme OpenAI')
    expect(container.textContent).toContain('OpenAI')
    expect(container.textContent).toContain('Active')
    expect(container.textContent).toContain('cred-abc')
    expect(container.textContent).toContain('gpt-5.6')
  })

  it('shows an empty state when no credentials exist', async () => {
    render()
    await flushEffects()
    expect(container.textContent).toContain('No provider credentials yet')
  })

  it('does not leak key material — only opaque key refs render and key inputs are password fields', async () => {
    mocks.list.mockResolvedValue({ data: { results: [REF_A] } })
    render()
    await flushEffects()
    expect(container.textContent).not.toContain('sk-cortex-')
    expect(container.textContent).not.toContain('api_key')
    clickButton('Add provider')
    await flushEffects()
    const secret = container.querySelector('#secret-key')
    expect(secret).toBeTruthy()
    expect(secret.type).toBe('password')
  })
})

describe('OrgProviders — HIVE-2161 lifecycle', () => {
  it('provisions a canonical provider without a base URL (endpoint derived server-side)', async () => {
    render()
    await flushEffects()
    clickButton('Add provider')
    await flushEffects()

    setValue(container.querySelector('#display-name'), 'Acme OpenAI')
    setValue(container.querySelector('#models'), 'gpt-5.6, gpt-5.6-mini')
    setValue(container.querySelector('#secret-key'), 'sk-test-provider-key')
    expect(container.querySelector('#base-url')).toBeNull()

    clickButton('Add credential')
    await flushEffects()

    expect(mocks.provision).toHaveBeenCalledWith({
      org_id: 7,
      provider_type: 'openai',
      display_name: 'Acme OpenAI',
      base_url: '',
      models: ['gpt-5.6', 'gpt-5.6-mini'],
      secret_key: 'sk-test-provider-key',
    })
    expect(mocks.upsert).not.toHaveBeenCalled()
  })

  it('requires a base URL for openai_compatible and sends it', async () => {
    render()
    await flushEffects()
    clickButton('Add provider')
    await flushEffects()

    setValue(container.querySelector('#provider-type'), 'openai_compatible')
    await flushEffects()
    const baseUrl = container.querySelector('#base-url')
    expect(baseUrl).toBeTruthy()
    expect(baseUrl.required).toBe(true)

    setValue(container.querySelector('#display-name'), 'Acme internal LLM')
    setValue(container.querySelector('#base-url'), 'https://llm.internal.acme.com/v1')
    setValue(container.querySelector('#models'), 'internal-model')
    setValue(container.querySelector('#secret-key'), 'sk-internal-key')

    clickButton('Add credential')
    await flushEffects()

    expect(mocks.provision).toHaveBeenCalledWith({
      org_id: 7,
      provider_type: 'openai_compatible',
      display_name: 'Acme internal LLM',
      base_url: 'https://llm.internal.acme.com/v1',
      models: ['internal-model'],
      secret_key: 'sk-internal-key',
    })
  })

  it('surfaces a failed provision honestly and refreshes the mirror state', async () => {
    mocks.provision.mockRejectedValue({
      response: { status: 502, data: { error: 'Cortex provider request failed.', detail: { status: 'invalid' } } },
    })
    render()
    await flushEffects()
    clickButton('Add provider')
    await flushEffects()
    setValue(container.querySelector('#display-name'), 'Broken key')
    setValue(container.querySelector('#models'), 'gpt-5.6')
    setValue(container.querySelector('#secret-key'), 'sk-broken')
    clickButton('Add credential')
    await flushEffects()

    expect(container.querySelector('[role="alert"]').textContent).toContain('Cortex provider request failed.')
    // The 502 may have materialized a mirror row — the list is refreshed.
    expect(mocks.list).toHaveBeenCalledTimes(2)
  })

  it('rotates a key with the one-time secret relayed to Cortex only', async () => {
    mocks.list.mockResolvedValue({ data: { results: [REF_A] } })
    render()
    await flushEffects()
    clickButton('Rotate')
    await flushEffects()
    const rotateInput = container.querySelector('#rotate-key-1')
    expect(rotateInput).toBeTruthy()
    expect(rotateInput.type).toBe('password')
    setValue(rotateInput, 'sk-rotated-key')
    clickButton('Rotate key')
    await flushEffects()
    expect(mocks.rotate).toHaveBeenCalledWith(1, { secret_key: 'sk-rotated-key' })
  })

  it('disables a credential', async () => {
    mocks.list.mockResolvedValue({ data: { results: [REF_A] } })
    render()
    await flushEffects()
    clickButton('Disable')
    await flushEffects()
    expect(mocks.disable).toHaveBeenCalledWith(1)
  })

  it('hides Disable on an already-disabled credential', async () => {
    mocks.list.mockResolvedValue({ data: { results: [{ ...REF_A, status: 'disabled' }] } })
    render()
    await flushEffects()
    expect([...container.querySelectorAll('button')].some((b) => b.textContent.includes('Disable'))).toBe(false)
  })

  it('runs the live validation probe on Validate', async () => {
    mocks.list.mockResolvedValue({ data: { results: [{ ...REF_A, status: 'verifying' }] } })
    render()
    await flushEffects()
    clickButton('Validate')
    await flushEffects()
    expect(mocks.validate).toHaveBeenCalledWith(1)
  })

  it('removes a reference on Remove', async () => {
    mocks.list.mockResolvedValue({ data: { results: [REF_A] } })
    render()
    await flushEffects()
    clickButton('Remove')
    await flushEffects()
    expect(mocks.remove).toHaveBeenCalledWith(1)
  })
})

describe('OrgProviders — slice-2 editors', () => {
  it('edits default models via the mirror upsert, preserving status', async () => {
    mocks.list.mockResolvedValue({ data: { results: [REF_A] } })
    render()
    await flushEffects()
    clickButton('Default models')
    await flushEffects()
    const input = container.querySelector('#models-1')
    expect(input.value).toBe('gpt-5.6')
    setValue(input, 'gpt-5.6, gpt-5.6-mini')
    clickButton('Save')
    await flushEffects()
    expect(mocks.upsert).toHaveBeenCalledWith({
      org_id: 7,
      cortex_provider_id: '11111111-1111-1111-1111-111111111111',
      status: 'active',
      default_models: ['gpt-5.6', 'gpt-5.6-mini'],
      per_agent_overrides: {},
    })
  })

  it('adds a same-org agent override with provider + optional model', async () => {
    mocks.list.mockResolvedValue({
      data: { results: [REF_A, { ...REF_A, id: 2, cortex_provider_id: '22222222-2222-2222-2222-222222222222', display_name: 'Acme Anthropic', provider_type: 'anthropic' }] },
    })
    render()
    await flushEffects()
    clickButton('Agent overrides (0)')
    await flushEffects()
    expect(container.textContent).toContain('No agent overrides')

    setValue(container.querySelector('#ov-agent-1'), 'agent@shizuha.com')
    setValue(container.querySelector('#ov-provider-1'), '22222222-2222-2222-2222-222222222222')
    setValue(container.querySelector('#ov-model-1'), 'claude-sonnet-4')
    clickButton('Add override')
    await flushEffects()

    expect(mocks.upsert).toHaveBeenCalledWith({
      org_id: 7,
      cortex_provider_id: '11111111-1111-1111-1111-111111111111',
      status: 'active',
      default_models: ['gpt-5.6'],
      per_agent_overrides: {
        'agent@shizuha.com': {
          provider_credential_id: '22222222-2222-2222-2222-222222222222',
          default_model_id: 'claude-sonnet-4',
        },
      },
    })
  })

  it('renders existing overrides with provider display names and removes one', async () => {
    mocks.list.mockResolvedValue({
      data: {
        results: [
          REF_A,
          { ...REF_A, id: 2, cortex_provider_id: '22222222-2222-2222-2222-222222222222', display_name: 'Acme Anthropic', provider_type: 'anthropic' },
          {
            ...REF_A,
            id: 3,
            cortex_provider_id: '33333333-3333-3333-3333-333333333333',
            display_name: 'Acme internal',
            provider_type: 'openai_compatible',
            per_agent_overrides: {
              'agent@shizuha.com': { provider_credential_id: '22222222-2222-2222-2222-222222222222', default_model_id: 'claude-sonnet-4' },
            },
          },
        ],
      },
    })
    render()
    await flushEffects()
    clickButton('Agent overrides (1)')
    await flushEffects()
    expect(container.textContent).toContain('agent@shizuha.com')
    expect(container.textContent).toContain('Acme Anthropic')
    expect(container.textContent).toContain('claude-sonnet-4')

    // Scope to the overrides editor of the card that owns the override —
    // other cards also render a top-level Remove action.
    const editor = [...container.querySelectorAll('[data-testid="overrides-editor"]')]
      .find((el) => el.textContent.includes('agent@shizuha.com'))
    const rowRemove = [...editor.querySelectorAll('li button')].find((b) => b.textContent === 'Remove')
    act(() => rowRemove.click())
    await flushEffects()
    // Override removal is a mirror upsert (the Cortex credential itself is
    // untouched) — the ref's own status and default models are preserved.
    expect(mocks.upsert).toHaveBeenCalledWith({
      org_id: 7,
      cortex_provider_id: '33333333-3333-3333-3333-333333333333',
      status: 'active',
      default_models: ['gpt-5.6'],
      per_agent_overrides: {},
    })
    expect(mocks.remove).not.toHaveBeenCalled()
  })

  it('removes an override via the mirror upsert', async () => {
    const overrides = {
      'a@shizuha.com': { provider_credential_id: '11111111-1111-1111-1111-111111111111' },
      'b@shizuha.com': { provider_credential_id: '11111111-1111-1111-1111-111111111111' },
    }
    mocks.list.mockResolvedValue({ data: { results: [{ ...REF_A, per_agent_overrides: overrides }] } })
    render()
    await flushEffects()
    clickButton('Agent overrides (2)')
    await flushEffects()
    // Two override rows, each with a Remove button — click the first.
    const removeBtns = [...container.querySelectorAll('li button')].filter((b) => b.textContent === 'Remove')
    expect(removeBtns.length).toBe(2)
    act(() => removeBtns[0].click())
    await flushEffects()
    expect(mocks.upsert).toHaveBeenCalledWith({
      org_id: 7,
      cortex_provider_id: '11111111-1111-1111-1111-111111111111',
      status: 'active',
      default_models: ['gpt-5.6'],
      per_agent_overrides: {
        'b@shizuha.com': { provider_credential_id: '11111111-1111-1111-1111-111111111111' },
      },
    })
  })

  it('offers only same-org credentials in the override provider select', async () => {
    // The list endpoint is org-scoped server-side, so every option is a
    // same-org credential by construction — assert the select is populated
    // from the org-scoped list only.
    mocks.list.mockResolvedValue({ data: { results: [REF_A] } })
    render()
    await flushEffects()
    clickButton('Agent overrides (0)')
    await flushEffects()
    const options = [...container.querySelectorAll('#ov-provider-1 option')].map((o) => o.value)
    expect(options).toEqual(['', '11111111-1111-1111-1111-111111111111'])
  })
})
