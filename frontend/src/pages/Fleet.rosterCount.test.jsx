// HIVE-1299: the roster must never claim a total that contradicts the cards it
// draws.
//
// Live symptom (zen, 2026-07-22, authenticated multi-org vantage): the header
// read "Showing 55 of 55 agents" while the hydrated DOM held 57
// `.hive-agent-card` nodes, because @kai and @zen each sit on two teams and team
// grouping renders one card per PLACEMENT. Approved contract (Architecture/CoS):
// keep health/workload totals scoped to unique identities, keep multi-team cards
// as placements, and label BOTH populations explicitly.
//
// This test asserts against the rendered DOM rather than against
// buildTeamGroups(), because the defect was never in the grouping helper — that
// helper was doing exactly what it was designed to do. The mismatch lived in
// what Fleet.jsx *said about* the groups, so a fleetGrouping unit test would go
// green while the page still lied.
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

const team = (slug, name, orgSlug, orgName) => ({
  team_slug: slug,
  team_name: name,
  organization_slug: orgSlug,
  organization_name: orgName,
})

// Two multi-team aliases, mirroring the live @kai / @zen case that QA asked be
// covered ("a browser regression exercises at least two multi-team aliases").
// 3 unique agents -> 5 team placements.
const AGENTS = [
  {
    id: 1,
    agent_id: 1,
    agent_username: 'kai',
    display_name: 'Kai',
    email: 'kai@shizuha.com',
    status: 'running',
    runtime_harness: 'claude',
    team_memberships: [
      team('engineering', 'Engineering', 'shizuha', 'Shizuha Global Pvt. Ltd'),
      team('review', 'Review', 'digital', 'Shizuha Digital LLP'),
    ],
  },
  {
    id: 2,
    agent_id: 2,
    agent_username: 'zen',
    display_name: 'Zen',
    email: 'zen@shizuha.com',
    status: 'running',
    runtime_harness: 'claude',
    team_memberships: [
      team('qa', 'QA', 'shizuha', 'Shizuha Global Pvt. Ltd'),
      team('security', 'Security', 'digital', 'Shizuha Digital LLP'),
    ],
  },
  {
    id: 3,
    agent_id: 3,
    agent_username: 'solo',
    display_name: 'Solo',
    email: 'solo@shizuha.com',
    status: 'running',
    runtime_harness: 'codex',
    team_memberships: [team('engineering', 'Engineering', 'shizuha', 'Shizuha Global Pvt. Ltd')],
  },
]

const UNIQUE_AGENTS = 3
const TEAM_PLACEMENTS = 5

let container
let root

beforeEach(() => {
  mocks.useHive.mockReturnValue({
    isStaff: true,
    canReadFleet: true,
    currentOrgSlug: '',
    organizations: [
      { slug: 'shizuha', name: 'Shizuha Global Pvt. Ltd' },
      { slug: 'digital', name: 'Shizuha Digital LLP' },
    ],
  })
  mocks.useFleetAgents.mockReturnValue({
    data: AGENTS,
    isLoading: false,
    isError: false,
    isFetching: false,
    refetch: vi.fn(),
  })
  mocks.usePulseOrgSnapshot.mockReturnValue({ data: {}, isError: false, isLoading: false })
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
    root.render(
      <MemoryRouter>
        <Fleet />
      </MemoryRouter>,
    )
  })
}

const cardCount = () => container.querySelectorAll('.hive-agent-card').length
const bodyText = () => container.textContent || ''

describe('HIVE-1299 roster count vs rendered cards', () => {
  it('draws one card per placement, and says so', () => {
    render()

    // The population that actually exists on screen.
    expect(cardCount()).toBe(TEAM_PLACEMENTS)

    const text = bodyText()
    // Both populations named explicitly, per the approved contract. The bug was
    // that only the first number appeared, next to TEAM_PLACEMENTS cards.
    expect(text).toContain(`Showing ${UNIQUE_AGENTS} of ${UNIQUE_AGENTS} unique agents`)
    expect(text).toContain(`${TEAM_PLACEMENTS} team placements`)

    // The exact contradictory string from the live report must not come back:
    // "Showing 3 of 3 agents" with 5 cards on the page.
    expect(text).not.toContain(`Showing ${UNIQUE_AGENTS} of ${UNIQUE_AGENTS} agents`)
  })

  it('quotes a placement count that equals the cards drawn, not a recomputation', () => {
    render()

    // The number in the header must be derived from the same array the cards
    // come from. Asserting equality with the DOM count is what makes this a
    // real guard: a placement count computed independently could drift from the
    // render and still look plausible in isolation.
    const match = bodyText().match(/(\d+) team placements/)
    expect(match).not.toBeNull()
    expect(Number(match[1])).toBe(cardCount())
  })

  it('labels team sections as placements, since one agent can appear in two', () => {
    render()

    const text = bodyText()
    // Under team grouping a section holds placements. Calling them "agents"
    // double-counts a multi-team alias across sections.
    expect(text).toMatch(/\d+ placements/)
    expect(text).not.toMatch(/\d+ agents(?!\b.*unique)/)
  })

  it('CONTROL: a roster with no multi-team agent shows no placement clause', () => {
    // Guards against the opposite failure — bolting the extra clause onto every
    // roster, including the ones that never had a contradiction. If this passed
    // regardless of the fixture, the assertions above would prove nothing about
    // the condition that triggers the label.
    mocks.useFleetAgents.mockReturnValue({
      data: [AGENTS[2]], // single team, single placement
      isLoading: false,
      isError: false,
      isFetching: false,
      refetch: vi.fn(),
    })
    render()

    expect(cardCount()).toBe(1)
    const text = bodyText()
    expect(text).toContain('Showing 1 of 1 unique agent')
    expect(text).not.toContain('team placements')
  })
})
