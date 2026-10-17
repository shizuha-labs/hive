// The banto card, rendered from the LIVE payload, must show its open task.
//
// Operator 2026-08-05, three times, on Shizuha Global Pvt. Ltd › Accounting:
//
//     still don't see the active tasks assigned to banto nor on the team header
//     .. and it's still in needs help .. really poor user experience here by you
//     ...
//     you can obviously use a browser to see .. look at this crap
//
// Pulse holds BKSD-10 (todo, high) plus BKSD-8/BKSD-9 (blocked) assigned to
// banto, and the snapshot endpoint returns exactly that. The card renders
// "0 open · 0 doing · 0 review · 2 blocked".
//
// This mounts the REAL Fleet page with the REAL snapshot payload captured from
// the live endpoint, instead of unit-testing the helper in isolation — the
// helper's own tests pass, so whatever is wrong lives between the payload and
// the DOM.
import React, { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'

const mocks = vi.hoisted(() => ({
  useFleetAgents: vi.fn(),
  usePulseOrgSnapshot: vi.fn(),
  useFleetEfficiency: vi.fn(),
  useDaemonIdentityHealth: vi.fn(),
  useTeams: vi.fn(),
  useHive: vi.fn(),
}))

vi.mock('../hooks/useAgents', () => ({
  useFleetAgents: mocks.useFleetAgents,
  usePulseOrgSnapshot: mocks.usePulseOrgSnapshot,
  useFleetEfficiency: mocks.useFleetEfficiency,
  useDaemonIdentityHealth: mocks.useDaemonIdentityHealth,
  useTeams: mocks.useTeams,
}))
vi.mock('../contexts/HiveContext', () => ({ useHive: mocks.useHive }))
vi.mock('../components/AgentDrawer', () => ({ default: () => null }))
vi.mock('../components/ProvisionAgentModal', () => ({ default: () => null }))
vi.mock('../components/ContextBudgetPanel', () => ({ default: () => null }))
vi.mock('../components/WorkEfficiencyPanel', () => ({ default: () => null }))
vi.mock('../components/HarnessVersionsPanel', () => ({ default: () => null }))

import Fleet from './Fleet'

globalThis.IS_REACT_ACT_ENVIRONMENT = true

// Captured live from GET /v1/fleet/pulse-snapshot/ (HTTP 200).
const LIVE_WORKLOAD = {
  'banto@shizuha.com': {
    open: 1, in_progress: 0, blocked: 2, in_review: 0,
    scheduled: 2, backlog: 0, completed: 5, overdue: 2,
  },
}
const LIVE_WORKLOAD_BY_ASSIGNEE_TEAM = {
  'banto@shizuha.com::accounting': {
    open: 0, in_progress: 0, blocked: 0, in_review: 0,
    scheduled: 2, backlog: 0, completed: 3, overdue: 0,
  },
}

const AGENTS = [{
  id: 1,
  agent_id: 1,
  agent_username: 'banto',
  display_name: 'Banto',
  email: 'banto@shizuha.com',
  status: 'running',
  runtime_harness: 'shizuha',
  team_memberships: [{
    team_slug: 'accounting',
    team_name: 'Accounting',
    organization_slug: 'shizuha',
    organization_name: 'Shizuha Global Pvt. Ltd',
  }],
}]

let container
let root

beforeEach(() => {
  mocks.useHive.mockReturnValue({
    isStaff: true,
    canReadFleet: true,
    currentOrgSlug: '',
    organizations: [{ slug: 'shizuha', name: 'Shizuha Global Pvt. Ltd' }],
  })
  mocks.useFleetAgents.mockReturnValue({
    data: AGENTS, isLoading: false, isError: false, isFetching: false, refetch: vi.fn(),
  })
  mocks.usePulseOrgSnapshot.mockReturnValue({
    data: {
      workload: LIVE_WORKLOAD,
      workload_by_assignee_team: LIVE_WORKLOAD_BY_ASSIGNEE_TEAM,
      team_workload: {},
      teams: [],
      nodes: [],
    },
    isError: false,
    isLoading: false,
  })
  mocks.useTeams.mockReturnValue({ data: [] })
  mocks.useFleetEfficiency.mockReturnValue({ data: {} })
  mocks.useDaemonIdentityHealth.mockReturnValue({ data: [] })

  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(() => {
  act(() => root.unmount())
  container.remove()
  vi.clearAllMocks()
})

function render() {
  act(() => {
    root.render(<MemoryRouter><Fleet /></MemoryRouter>)
  })
}

/** The rendered "N open" / "N blocked" pairs on the card. */
function workloadPairs() {
  const card = container.querySelector('.hive-agent-card')
  if (!card) return {}
  const text = (card.textContent || '').replace(/\s+/g, ' ')
  const out = {}
  for (const bucket of ['open', 'doing', 'review', 'blocked', 'scheduled', 'backlog']) {
    const m = text.match(new RegExp(`(\\d+)\\s*${bucket}`, 'i'))
    if (m) out[bucket] = Number(m[1])
  }
  return out
}

describe('banto card renders the work Pulse says it owns', () => {
  it('shows 1 open, not 0', () => {
    render()
    const pairs = workloadPairs()
    expect(
      pairs.open,
      `card rendered ${JSON.stringify(pairs)} — Pulse and the snapshot both say `
        + 'banto has 1 open (BKSD-10, todo, high)',
    ).toBe(1)
  })

  it('shows the 2 blocked items too', () => {
    render()
    expect(workloadPairs().blocked).toBe(2)
  })

  it('never renders a bucket lower than the agent\'s own totals', () => {
    render()
    const pairs = workloadPairs()
    for (const [bucket, key] of [['open', 'open'], ['blocked', 'blocked'], ['scheduled', 'scheduled']]) {
      expect(
        pairs[bucket] ?? 0,
        `bucket ${bucket} under-reported`,
      ).toBeGreaterThanOrEqual(LIVE_WORKLOAD['banto@shizuha.com'][key])
    }
  })
})
