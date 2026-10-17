import React, { act } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const query = vi.hoisted(() => ({ current: {} }))
vi.mock('../hooks/useAgents', () => ({
  useFleetAgents: () => query.current,
  usePulseOrgSnapshot: () => ({ data: {} }),
  useFleetEfficiency: () => ({ data: {} }),
  useDaemonIdentityHealth: () => ({ data: [] }),
  useTeams: () => ({ data: [{ slug: 'engineering', name: 'Engineering', organization_slug: 'test' }] }),
}))
vi.mock('../contexts/HiveContext', () => ({ useHive: () => ({ isStaff: true, canReadFleet: true, currentOrgSlug: 'test', organizations: [] }) }))
vi.mock('../components/AgentDrawer', () => ({ default: () => null }))
vi.mock('../components/ProvisionAgentModal', () => ({ default: () => null }))
vi.mock('../components/ContextBudgetPanel', () => ({ default: () => null }))
vi.mock('../components/WorkEfficiencyPanel', () => ({ default: () => null }))
vi.mock('../components/HarnessVersionsPanel', () => ({ default: () => null }))
import Fleet from './Fleet'

globalThis.IS_REACT_ACT_ENVIRONMENT = true
let container, root
beforeEach(() => { container = document.createElement('div'); document.body.appendChild(container); root = createRoot(container) })
afterEach(() => { act(() => root.unmount()); container.remove() })
function render(state) {
  query.current = { isLoading: false, isError: false, isFetching: false, refetch: vi.fn(), ...state }
  act(() => root.render(<MemoryRouter><Fleet /></MemoryRouter>))
}
const metrics = () => [...container.querySelectorAll('.hv-stat-number')].map((node) => node.textContent)
const text = () => container.textContent

describe('Fleet page snapshot admission into displayed metrics', () => {
  // The live caller receives no data on cold/org-key change, then a snapshot;
  // query error may occur either before data or while retaining same-key data.
  it.each([
    [{ isLoading: true, isFetching: true }, 'Loading'],
    [{ isError: true }, 'Unavailable'],
  ])('does not turn a missing scoped snapshot into measured zero: %j', (state, label) => {
    render(state)
    expect(metrics()).toEqual(['—', '—', '—', '—', '—'])
    expect(text()).toContain(label)
    expect(container.querySelector('[data-testid="activity-mix"]').textContent).not.toMatch(/0\s*(thinking|responding|tool|working|idle)/i)
    expect(text()).not.toContain('Showing 0 of 0')
    expect(text()).not.toContain('empty teams')
  })

  it('distinguishes a successful empty snapshot from loading and failure', () => {
    render({ isLoading: true })
    expect(metrics()[0]).toBe('—')
    render({ data: [] })
    expect(metrics()).toEqual(['0/0', '0', '0', '0', '0'])
    expect(text()).toContain('Showing 0 of 0 unique agents')
    expect(text()).not.toContain('Loading agents')
  })

  it('retains real same-scope counts during refresh/failure and clears them for a missing new scope', () => {
    const data = [{ id: 'one', agent_username: 'one', display_name: 'One', status: 'hibernated', team_memberships: [] }]
    render({ data })
    expect(metrics()[0]).toBe('0/1')
    expect(metrics()[1]).toBe('1')
    render({ data, isFetching: true })
    expect(metrics()[0]).toBe('0/1')
    render({ data, isError: true })
    expect(metrics()[0]).toBe('0/1')
    expect(text()).toContain('Showing 1 of 1 unique agent')
    render({ isLoading: true, isFetching: true })
    expect(metrics()).toEqual(['—', '—', '—', '—', '—'])
    expect(text()).not.toContain('Showing 1 of 1')
  })
})
