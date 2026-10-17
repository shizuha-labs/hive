import React, { act } from 'react'
import { Simulate } from 'react-dom/test-utils'
import { createRoot } from 'react-dom/client'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ToastProvider } from '../components/Toast'

const agentDrawerMocks = vi.hoisted(() => ({
  runtimeOptions: {
    runtime_methods: [{ id: 'shizuha', label: 'Shizuha CLI' }],
    runtime_environments: ['k8s'],
    tiers: ['standard'],
    skills: ['engineering', 'devops'],
    mcp_servers: [{ slug: 'pulse', name: 'Pulse' }],
  },
}))

vi.mock('../hooks/useAgents', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    useFleetRuntimeOptions: () => ({ data: agentDrawerMocks.runtimeOptions }),
  }
})

import { useTeamMutations } from '../hooks/useAgents'
import { teamsApi } from '../services/api'
import { CapabilityMappingEditor } from './Teams'
import { RuntimeConfigEditor } from '../components/AgentDrawer'

const baseCapability = {
  slug: 'engineering-tools',
  display_name: 'Engineering Tools',
  description: 'Baseline bundle',
  risk_tier: 'standard',
  skill_refs: ['engineering'],
  eager_skill_refs: [],
  inline_profile_refs: [],
  mcp_server_refs: ['pulse'],
  sensitive_access_flags: [],
  default_credential_grant_scopes: [],
  requires: [],
  conflicts_with: [],
  runtime_flags: {},
}

const runtimeOptions = {
  skills: ['engineering', 'devops'],
  mcp_servers: [{ slug: 'pulse' }, { slug: 'wiki' }],
  resource_profiles: [],
}

globalThis.IS_REACT_ACT_ENVIRONMENT = true

let container
let root

function renderEditor({ mutateImpl } = {}) {
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  const mutate = vi.fn((payload, handlers) => {
    if (mutateImpl) mutateImpl(payload, handlers)
  })
  const onClose = vi.fn()
  act(() => {
    root.render(
      <ToastProvider>
        <CapabilityMappingEditor
          capability={baseCapability}
          capabilities={[baseCapability, { slug: 'runtime-shell', display_name: 'Runtime Shell' }]}
          runtimeOptions={runtimeOptions}
          mutations={{ update: { mutate, isPending: false } }}
          onClose={onClose}
        />
      </ToastProvider>,
    )
  })
  return { mutate, onClose }
}

function renderRuntimeEditor(agent) {
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  const mutate = vi.fn()
  act(() => {
    root.render(
      <ToastProvider>
        <RuntimeConfigEditor
          agent={agent}
          mutation={{ mutate, isPending: false, isError: false }}
          canEdit
        />
      </ToastProvider>,
    )
  })
  // 20a3f14: the editor is always-editable inline — no Edit gate, and edits
  // auto-save through a 900ms debounce instead of a batch 'Save & push'.
  return { mutate }
}

function addChip(placeholder, value) {
  const input = Array.from(container.querySelectorAll('input')).find((node) => node.placeholder === placeholder)
  expect(input).toBeTruthy()
  act(() => {
    Simulate.change(input, { target: { value } })
  })
  const button = input.parentElement.querySelector('button')
  act(() => Simulate.click(button))
}

function TeamMutationHarness({ orgSlug }) {
  const mutations = useTeamMutations({ orgSlug })
  return (
    <div data-org={orgSlug}>
      <button type="button" onClick={() => mutations.update.mutate({ name: 'accounting', display_name: `${orgSlug} Accounting` })}>rename</button>
      <button type="button" onClick={() => mutations.remove.mutate('accounting')}>delete</button>
      <button type="button" onClick={() => mutations.manageMember.mutate({ name: 'accounting', action: 'add', email: `${orgSlug}@agents.shizuha.io` })}>member</button>
      <button type="button" onClick={() => mutations.setCapabilities.mutate({ name: 'accounting', capabilities: [{ capability_slug: 'books' }] })}>capability</button>
    </div>
  )
}

function renderTeamMutationHarnesses() {
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  act(() => {
    root.render(
      <QueryClientProvider client={queryClient}>
        <TeamMutationHarness orgSlug="digital" />
        <TeamMutationHarness orgSlug="global" />
      </QueryClientProvider>,
    )
  })
}

afterEach(() => {
  if (root) act(() => root.unmount())
  container?.remove()
  vi.restoreAllMocks()
  root = null
  container = null
})

describe('CapabilityMappingEditor', () => {
  it('renders skill and MCP mappings, lets admins add refs, and saves the payload', () => {
    const { mutate, onClose } = renderEditor({ mutateImpl: (_payload, handlers) => handlers.onSuccess() })

    expect(container.textContent).toContain('Edit capability bundle: Engineering Tools')
    expect(container.textContent).toContain('engineering')
    expect(container.textContent).toContain('pulse')

    addChip('Add skills…', 'devops')
    addChip('Add mcp servers…', 'wiki')

    const save = Array.from(container.querySelectorAll('button')).find((node) => node.textContent === 'Save mappings')
    act(() => Simulate.click(save))

    expect(mutate).toHaveBeenCalledTimes(1)
    expect(mutate.mock.calls[0][0]).toMatchObject({
      slug: 'engineering-tools',
      skill_refs: ['engineering', 'devops'],
      mcp_server_refs: ['pulse', 'wiki'],
      runtime_flags: {},
      reason: 'Updated capability bundle mappings from Hive Teams UI',
    })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('surfaces backend validation errors for invalid mapping refs', () => {
    const { mutate } = renderEditor({
      mutateImpl: (_payload, handlers) => handlers.onError({
        response: { data: { mcp_server_refs: ['Unknown MCP server ref(s): missing'] } },
      }),
    })

    const save = Array.from(container.querySelectorAll('button')).find((node) => node.textContent === 'Save mappings')
    act(() => Simulate.click(save))

    expect(mutate).toHaveBeenCalledTimes(1)
    expect(container.textContent).toContain('mcp_server_refs: Unknown MCP server ref(s): missing')
  })
})

describe('PLAT-4212 org-scoped team mutations', () => {
  it('issues distinct org-scoped requests for every same-named team card action', async () => {
    const update = vi.spyOn(teamsApi, 'update').mockResolvedValue({ data: {} })
    const remove = vi.spyOn(teamsApi, 'delete').mockResolvedValue({ data: {} })
    const manageMember = vi.spyOn(teamsApi, 'manageMember').mockResolvedValue({ data: {} })
    const setCapabilities = vi.spyOn(teamsApi, 'setCapabilities').mockResolvedValue({ data: {} })
    renderTeamMutationHarnesses()

    await act(async () => {
      for (const button of container.querySelectorAll('button')) Simulate.click(button)
    })
    await vi.waitFor(() => expect(setCapabilities).toHaveBeenCalledTimes(2))

    expect(update.mock.calls).toEqual([
      ['accounting', { display_name: 'digital Accounting', org_slug: 'digital' }],
      ['accounting', { display_name: 'global Accounting', org_slug: 'global' }],
    ])
    expect(remove.mock.calls).toEqual([
      ['accounting', { org_slug: 'digital' }],
      ['accounting', { org_slug: 'global' }],
    ])
    expect(manageMember.mock.calls).toEqual([
      ['accounting', { action: 'add', email: 'digital@agents.shizuha.io', org_slug: 'digital' }],
      ['accounting', { action: 'add', email: 'global@agents.shizuha.io', org_slug: 'global' }],
    ])
    expect(setCapabilities.mock.calls).toEqual([
      ['accounting', { capabilities: [{ capability_slug: 'books' }], org_slug: 'digital' }],
      ['accounting', { capabilities: [{ capability_slug: 'books' }], org_slug: 'global' }],
    ])
  })
})

describe('Agent configuration editor runtime contracts', () => {
  const nativeAgent = {
    id: 'native-agent',
    runtime_source: 'k3s-native',
    has_telemetry: true,
    role: 'Engineer',
    execution_method: 'shizuha',
    model: 'gpt-5.3-codex',
    resource_profile: 'standard',
    resource_profile_spec: {
      requests: { cpu: '500m', memory: '1Gi' },
      limits: { cpu: '2', memory: '4Gi' },
    },
    resource_overrides: {},
    skills: ['engineering'],
    eager_skills: [],
    enabled_mcp_server_ids: ['pulse'],
    runtime_environment: 'k8s',
  }

  it('keeps a populated native resource profile valid during a role-only save', () => {
    const { mutate } = renderRuntimeEditor(nativeAgent)

    expect(container.textContent).not.toContain('Needs attention')
    expect(container.textContent).toContain('Skills & MCP')
    expect(container.textContent).toContain('Instant')

    vi.useFakeTimers()
    const role = container.querySelector('input[placeholder="Engineer, Researcher, Operations…"]')
    act(() => Simulate.change(role, { target: { value: 'Lead Engineer' } }))
    act(() => { vi.advanceTimersByTime(1000) })  // 900ms auto-save debounce
    vi.useRealTimers()

    expect(mutate).toHaveBeenCalledWith(
      { role: 'Lead Engineer' },
      expect.objectContaining({ onSuccess: expect.any(Function) }),
    )
  })

  it('uses the native resource schema and PATCH field and shows source-aware impacts', () => {
    const { mutate } = renderRuntimeEditor(nativeAgent)

    vi.useFakeTimers()
    const resourceEditor = container.querySelector('textarea[aria-label="Resource requests & limits JSON"]')
    expect(resourceEditor).toBeTruthy()
    act(() => Simulate.change(resourceEditor, {
      target: { value: '{"requests":{"cpu":"1","memory":"2Gi"},"limits":{"cpu":"3","memory":"6Gi"}}' },
    }))

    const environmentCard = Array.from(container.querySelectorAll('div')).find((node) => (
      node.querySelector('textarea[aria-label="Environment JSON"]') && node.textContent.includes('Environment')
    ))
    expect(environmentCard.textContent).toContain('Next restart')

    const skillsGroup = Array.from(container.querySelectorAll('section')).find((node) => node.textContent.includes('Skills & MCP'))
    expect(skillsGroup.textContent).toContain('Next restart')

    act(() => { vi.advanceTimersByTime(1000) })  // 900ms auto-save debounce
    vi.useRealTimers()
    expect(mutate.mock.calls[0][0]).toEqual({
      resource_overrides: {
        requests: { cpu: '1', memory: '2Gi' },
        limits: { cpu: '3', memory: '6Gi' },
      },
    })
  })

  it('shows the config editor for k3s-native Hive agents without telemetry', () => {
    renderRuntimeEditor({
      ...nativeAgent,
      has_telemetry: false,
      lifecycle_managed: true,
    })
    expect(container.textContent).toContain('Agent configuration')
    expect(container.textContent).toContain('Runtime harness')
  })

  it('shows external Connect capability edits as instant and env as next restart', () => {
    renderRuntimeEditor({
      ...nativeAgent,
      id: 'external-agent',
      runtime_source: 'external',
      resource_profile_spec: undefined,
      resource_limits: {},
    })

    const skillsGroup = Array.from(container.querySelectorAll('section')).find((node) => node.textContent.includes('Skills & MCP'))
    expect(skillsGroup.textContent).toContain('Instant')
    const environmentCard = Array.from(container.querySelectorAll('div')).find((node) => (
      node.querySelector('textarea[aria-label="Environment JSON"]') && node.textContent.includes('Environment')
    ))
    expect(environmentCard.textContent).toContain('Next restart')
  })
})
