import React, { act } from 'react'
import { Simulate } from 'react-dom/test-utils'
import { createRoot } from 'react-dom/client'
import { afterEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  versions: vi.fn(),
  mutateAsync: vi.fn(),
}))

vi.mock('../hooks/useAgents', () => ({
  useHarnessVersions: mocks.versions,
  useHarnessUpgrade: () => ({ mutateAsync: mocks.mutateAsync }),
}))

import HarnessVersionsPanel from './HarnessVersionsPanel'

globalThis.IS_REACT_ACT_ENVIRONMENT = true

let container
let root

function renderPanel(data, queryState = {}) {
  mocks.versions.mockReturnValue({
    data,
    isLoading: false,
    isError: false,
    isFetching: false,
    refetch: vi.fn(),
    ...queryState,
  })
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  act(() => root.render(<HarnessVersionsPanel isStaff />))
  act(() => Simulate.click(container.querySelector('section > button')))
}

afterEach(() => {
  if (root) act(() => root.unmount())
  container?.remove()
  root = null
  container = null
  vi.clearAllMocks()
})


describe('HarnessVersionsPanel status verdict', () => {
  it('fails closed in the collapsed summary when the endpoint refuses', () => {
    renderPanel(undefined, { isError: true })

    expect(container.textContent).toContain('status unavailable')
    expect(container.textContent).toContain('Harness status unavailable')
    expect(container.textContent).not.toContain('up to date')
    expect(container.textContent).not.toContain('upgrade available')
  })

  it('shows checking instead of a verdict before first data arrives', () => {
    renderPanel(undefined, { isLoading: true })

    expect(container.textContent).toContain('checking')
    expect(container.textContent).not.toContain('up to date')
    expect(container.textContent).not.toContain('status unavailable')
  })

  it('only claims up to date after successful non-empty version data', () => {
    renderPanel({
      rollout_in_progress: false,
      harnesses: [
        { harness: 'codex', label: 'Codex', package: '@openai/codex', current: '1.1', latest: '1.1', agent_count: 3, upgrade_available: false },
      ],
    })

    expect(container.textContent).toContain('up to date')
    expect(container.textContent).not.toContain('status unavailable')
  })
})

describe('HarnessVersionsPanel rollout actions', () => {
  it('replaces every fleet-upgrade button with progress while a rollout is active', () => {
    renderPanel({
      rollout_in_progress: true,
      desired_runtime_image: 'runtime:new',
      harnesses: [
        { harness: 'codex', label: 'Codex', package: '@openai/codex', current: '1.0', latest: '1.1', agent_count: 3, upgrade_available: true },
        { harness: 'claude', label: 'Claude Code', package: '@anthropic-ai/claude-code', current: '2.0', latest: '2.1', agent_count: 2, upgrade_available: true },
      ],
    })

    expect(container.textContent).toContain('runtime rolling')
    expect(container.textContent).toContain('Runtime image rollout in progress')
    expect(container.textContent).not.toContain('Upgrade fleet')
    const actionCells = [...container.querySelectorAll('tbody tr td:last-child')]
    expect(actionCells.filter((cell) => cell.textContent.includes('in progress'))).toHaveLength(2)
  })

  it('does not call an image-only rollout a harness upgrade when package versions are current', () => {
    renderPanel({
      rollout_in_progress: true,
      agent_runtime_image: 'runtime:old',
      desired_runtime_image: 'runtime:new',
      harnesses: [
        { harness: 'codex', label: 'Codex', package: '@openai/codex', current: '1.1', latest: '1.1', agent_count: 3, upgrade_available: false },
      ],
    })

    expect(container.textContent).toContain('runtime rolling')
    expect(container.textContent).toContain('Harness package versions can already be current')
    expect(container.textContent).not.toContain('upgrade rolling')
    expect(container.textContent).toContain('current')
  })

  it('does not paint an unused harness as rolling during a fleet image roll', () => {
    renderPanel({
      rollout_in_progress: true,
      desired_runtime_image: 'runtime:new',
      harnesses: [
        { harness: 'codex', label: 'Codex', package: '@openai/codex', current: '1.0', latest: '1.1', agent_count: 3, upgrade_available: true },
        { harness: 'claude_code', label: 'Claude Code', package: '@anthropic-ai/claude-code', current: '2.1.233', latest: '2.1.300', agent_count: 0, upgrade_available: true },
      ],
    })

    expect(container.textContent).toContain('runtime rolling')
    const rows = [...container.querySelectorAll('tbody tr')]
    const claude = rows.find((row) => row.textContent.includes('Claude Code'))
    expect(claude.textContent).toContain('unused')
    expect(claude.textContent).not.toContain('in progress')
    expect(claude.textContent).not.toContain('rolling')
    const codex = rows.find((row) => row.textContent.includes('Codex') && !row.textContent.includes('Claude'))
    expect(codex.textContent).toContain('in progress')
  })

  it('offers the upgrade action when no rollout is active', () => {
    renderPanel({
      rollout_in_progress: false,
      harnesses: [
        { harness: 'codex', label: 'Codex', package: '@openai/codex', current: '1.0', latest: '1.1', agent_count: 3, upgrade_available: true },
      ],
    })

    expect(container.textContent).toContain('Upgrade fleet')
  })
})
