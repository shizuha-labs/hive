import React, { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'

const mocks = vi.hoisted(() => ({
  useFleetAgents: vi.fn(),
  useFleetEfficiency: vi.fn(),
  useHive: vi.fn(),
  orgProvisioningList: vi.fn(),
}))

vi.mock('../hooks/useAgents', () => ({
  useFleetAgents: mocks.useFleetAgents,
  useFleetEfficiency: mocks.useFleetEfficiency,
}))

vi.mock('../contexts/HiveContext', () => ({
  useHive: mocks.useHive,
}))

vi.mock('../services/api', () => ({
  orgProvisioningApi: { list: mocks.orgProvisioningList },
  meApi: { services: vi.fn() },
}))

vi.mock('../components/AgentDrawer', () => ({
  default: () => null,
}))

import Overview, { TeamRollupCard } from './Overview'
import { OrgSwitcher } from '../components/Layout'

globalThis.IS_REACT_ACT_ENVIRONMENT = true

let container
let root

function readyPreviewRun(slug) {
  return {
    org_name: 'Zen Browser Wizard 202607050401',
    org_slug: slug,
    template_slug: 'tech-company',
    status: 'ready',
    dry_run: true,
    onboarding: {
      template: { name: 'Tech Company', slug: 'tech-company' },
      summary: { team_count: 10, agent_count: 11, starter_task_count: 3, org_slug: slug },
      first_task_flow: {
        title: 'Define the first customer-visible product increment',
        assignment_group: 'product',
        workflow: 'autonomous-dev',
        href: `/pulse/tasks?org=${slug}&onboarding=first-task`,
        steps: ['Review starter task'],
      },
      surfaces: [],
    },
  }
}

function renderOverview(path) {
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  act(() => {
    root.render(
      <MemoryRouter initialEntries={[path]}>
        <Overview />
      </MemoryRouter>,
    )
  })
}

function renderTeamRollupCard({ agentCount, statusCounts, attentionCount = 0 }) {
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  act(() => {
    root.render(
      <TeamRollupCard
        name="engineering"
        agentCount={agentCount}
        statusCounts={statusCounts}
        attentionCount={attentionCount}
        onViewTeam={vi.fn()}
      />,
    )
  })
  return {
    bar: container.querySelector('[data-capacity-bar]'),
    track: container.querySelector('[data-capacity-track]'),
    summary: container.querySelector('[data-team-summary] > span'),
  }
}

function renderOrgSwitcher({ currentOrganization = null } = {}) {
  const switchOrganization = vi.fn()
  const navigate = vi.fn((event) => event.preventDefault())
  mocks.useHive.mockReturnValue({
    organizations: [
      { id: 1, slug: 'shizuha', name: 'Shizuha Global Pvt. Ltd' },
      { id: 2, slug: 'digital', name: 'Shizuha Digital LLP' },
    ],
    currentOrganization,
    switchOrganization,
    orgsLoading: false,
  })

  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  act(() => {
    root.render(
      <>
        <button type="button">Before switcher</button>
        <OrgSwitcher />
        <a href="/overview" onClick={navigate}>Overview</a>
      </>,
    )
  })
  return { switchOrganization, navigate }
}

async function flushEffects() {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
  })
}

function fleetMetricValue(label) {
  const labelNode = [...container.querySelectorAll('span')]
    .find((node) => node.textContent === label)
  return labelNode?.parentElement?.querySelector('.hv-stat-number')?.textContent
}

afterEach(() => {
  if (root) act(() => root.unmount())
  container?.remove()
  root = null
  container = null
  vi.clearAllMocks()
})

describe('Overview preview org scoping', () => {
  function mockEmptyFleet({ currentOrgSlug = '' } = {}) {
    mocks.useHive.mockReturnValue({
      isStaff: false,
      username: 'Zen',
      currentOrgSlug,
    })
    mocks.useFleetAgents.mockReturnValue({
      data: [],
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    })
    mocks.useFleetEfficiency.mockReturnValue({ data: {} })
    mocks.orgProvisioningList.mockResolvedValue({ data: { results: [] } })
  }

  it('opens the selected organization first-task surface from an empty fleet', async () => {
    mockEmptyFleet()

    renderOverview('/overview?org=personal-11')
    await flushEffects()

    const openPulse = [...container.querySelectorAll('a')]
      .find((link) => link.textContent.includes('Open Pulse'))
    expect(openPulse?.getAttribute('href')).toBe(
      '/pulse/tasks?org=personal-11&onboarding=first-task',
    )
  })

  it('does not fetch staff-only fleet efficiency for a non-staff Overview (HIVE-1330)', async () => {
    mocks.useHive.mockReturnValue({ isStaff: false, username: 'Zen', currentOrgSlug: '' })
    mocks.useFleetAgents.mockReturnValue({ data: [], isLoading: false, isError: false, refetch: vi.fn() })
    mocks.useFleetEfficiency.mockReturnValue({ data: {} })
    mocks.orgProvisioningList.mockResolvedValue({ data: { results: [] } })

    renderOverview('/overview')
    await flushEffects()

    // The staff-only efficiency hook must be DISABLED for non-staff callers —
    // otherwise the Overview issues a deterministic 403 + console error.
    const call = mocks.useFleetEfficiency.mock.calls[0]
    expect(call).toBeTruthy()
    expect(call[0].enabled).toBe(false)
  })

  it('enables fleet efficiency for a staff Overview', async () => {
    mocks.useHive.mockReturnValue({ isStaff: true, username: 'Ryo', currentOrgSlug: '' })
    mocks.useFleetAgents.mockReturnValue({ data: [], isLoading: false, isError: false, refetch: vi.fn() })
    mocks.useFleetEfficiency.mockReturnValue({ data: {} })
    mocks.orgProvisioningList.mockResolvedValue({ data: { results: [] } })

    renderOverview('/overview')
    await flushEffects()

    const call = mocks.useFleetEfficiency.mock.calls[0]
    expect(call).toBeTruthy()
    expect(call[0].enabled).toBe(true)
  })

  it('shows no Create-org CTA for a non-staff user with org membership (HIVE-1630)', async () => {
    mocks.useHive.mockReturnValue({
      isStaff: false,
      username: 'Kumo',
      currentOrgSlug: '',
      claims: { organization_memberships: { 1: 'member', 359: 'owner' } },
      organizations: [],
    })
    mocks.useFleetAgents.mockReturnValue({ data: [], isLoading: false, isError: false, refetch: vi.fn() })
    mocks.useFleetEfficiency.mockReturnValue({ data: {} })
    mocks.orgProvisioningList.mockResolvedValue({ data: { results: [] } })

    renderOverview('/overview')
    await flushEffects()

    // A ready org with 0 agents invites creating an agent, not another org.
    expect(container.textContent).not.toContain('Create org')
    expect(container.textContent).not.toContain('Create your organization')
    expect(container.textContent).toContain('Add an agent')
    const addAgent = [...container.querySelectorAll('a')]
      .find((link) => link.textContent.includes('Add an agent'))
    expect(addAgent?.getAttribute('href')).toBe('/agents')
  })

  it('keeps the Create-org CTA when a non-staff user has no membership (HIVE-1630)', async () => {
    mocks.useHive.mockReturnValue({
      isStaff: false,
      username: 'NewUser',
      currentOrgSlug: '',
      claims: { organization_memberships: {} },
      organizations: [],
    })
    mocks.useFleetAgents.mockReturnValue({ data: [], isLoading: false, isError: false, refetch: vi.fn() })
    mocks.useFleetEfficiency.mockReturnValue({ data: {} })
    mocks.orgProvisioningList.mockResolvedValue({ data: { results: [] } })

    renderOverview('/overview')
    await flushEffects()

    expect(container.textContent).toContain('Create org')
    expect(container.textContent).toContain('Create your organization')
  })

  it('renders no Create-org CTA while the auth probe is unresolved (HIVE-2383 fail-closed)', async () => {
    mocks.useHive.mockReturnValue({
      isStaff: false,
      username: 'Mika',
      currentOrgSlug: '',
      claims: null,
      organizations: [],
      loading: true,
      hasToken: false,
    })
    mocks.useFleetAgents.mockReturnValue({ data: [], isLoading: false, isError: false, refetch: vi.fn() })
    mocks.useFleetEfficiency.mockReturnValue({ data: {} })
    mocks.orgProvisioningList.mockResolvedValue({ data: { results: [] } })

    renderOverview('/overview')
    await flushEffects()

    // Unknown membership state must never open the Create-org funnel.
    expect(container.textContent).not.toContain('Create org')
    expect(container.textContent).not.toContain('Create your organization')
  })

  it('renders no Create-org CTA when a token exists but claims fail to decode (HIVE-2383 fail-closed)', async () => {
    mocks.useHive.mockReturnValue({
      isStaff: false,
      username: 'Mika',
      currentOrgSlug: '',
      claims: null,
      organizations: [],
      loading: false,
      hasToken: true,
    })
    mocks.useFleetAgents.mockReturnValue({ data: [], isLoading: false, isError: false, refetch: vi.fn() })
    mocks.useFleetEfficiency.mockReturnValue({ data: {} })
    mocks.orgProvisioningList.mockResolvedValue({ data: { results: [] } })

    renderOverview('/overview')
    await flushEffects()

    // A present-but-undecodable token is an unresolved claims state.
    expect(container.textContent).not.toContain('Create org')
    expect(container.textContent).not.toContain('Create your organization')
  })

  it('keeps the Create-org CTA when membership is positively resolved as empty (HIVE-2383)', async () => {
    mocks.useHive.mockReturnValue({
      isStaff: false,
      username: 'NewUser',
      currentOrgSlug: '',
      claims: { organization_memberships: {} },
      organizations: [],
      loading: false,
      hasToken: true,
    })
    mocks.useFleetAgents.mockReturnValue({ data: [], isLoading: false, isError: false, refetch: vi.fn() })
    mocks.useFleetEfficiency.mockReturnValue({ data: {} })
    mocks.orgProvisioningList.mockResolvedValue({ data: { results: [] } })

    renderOverview('/overview')
    await flushEffects()

    expect(container.textContent).toContain('Create org')
    expect(container.textContent).toContain('Create your organization')
  })

  it('prefers and encodes the URL organization over the saved organization', async () => {
    mockEmptyFleet({ currentOrgSlug: 'stale-saved-org' })
    const urlOrg = 'personal/team & qa'

    renderOverview(`/overview?org=${encodeURIComponent(urlOrg)}`)
    await flushEffects()

    expect(mocks.useFleetAgents).toHaveBeenCalledWith(
      expect.objectContaining({ orgSlug: urlOrg }),
    )
    const openPulse = [...container.querySelectorAll('a')]
      .find((link) => link.textContent.includes('Open Pulse'))
    expect(openPulse?.getAttribute('href')).toBe(
      '/pulse/tasks?org=personal%2Fteam%20%26%20qa&onboarding=first-task',
    )
  })

  it('hides global fleet agents when a dry-run create-org preview is open', async () => {
    const slug = 'zen-browser-wizard-202607050401'
    mocks.useHive.mockReturnValue({
      isStaff: true,
      username: 'Zen',
      currentOrgSlug: '',
    })
    mocks.useFleetAgents.mockReturnValue({
      data: [
        { id: 1, display_name: 'Akira', agent_username: 'akira', status: 'running', created_at: '2026-07-01T00:00:00Z' },
        { id: 2, display_name: 'Kai', agent_username: 'kai', status: 'running', created_at: '2026-07-01T00:00:00Z' },
      ],
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    })
    mocks.useFleetEfficiency.mockReturnValue({
      data: { kai: { display_name: 'Kai', last_action: 'handled production work', last_active: '2026-07-05T03:00:00Z' } },
    })
    mocks.orgProvisioningList.mockResolvedValue({ data: { results: [readyPreviewRun(slug)] } })

    renderOverview(`/overview?org=${slug}`)
    await flushEffects()

    expect(mocks.useFleetAgents).toHaveBeenCalledWith(expect.objectContaining({ orgSlug: slug }))
    expect(container.textContent).toContain('Give Zen Browser Wizard 202607050401 its first task')
    expect(container.textContent).toContain('Preview-only dashboard')
    expect(container.textContent).toContain('11 planned agents')
    expect(container.textContent).not.toContain('Akira')
    expect(container.textContent).not.toContain('Kai')
    expect(container.textContent).not.toContain('Recent activity')
  })

  it('keeps long multi-team rollups shrinkable at desktop and mobile widths', async () => {
    mocks.useHive.mockReturnValue({
      isStaff: true,
      username: 'Kai',
      currentOrgSlug: '',
    })
    mocks.useFleetAgents.mockReturnValue({
      data: [
        { id: 1, display_name: 'Akira', agent_username: 'akira', status: 'running', team_names: ['customer-experience-and-reliability-engineering'], created_at: '2026-07-01T00:00:00Z' },
        { id: 2, display_name: 'Kai', agent_username: 'kai', status: 'blocked', team_names: ['customer-experience-and-reliability-engineering'], created_at: '2026-07-01T00:00:00Z' },
        { id: 3, display_name: 'Sara', agent_username: 'sara', status: 'running', team_names: ['international-securities-engineering-and-operations'], created_at: '2026-07-01T00:00:00Z' },
        { id: 4, display_name: 'Ryo', agent_username: 'ryo', status: 'idle', team_names: ['digital-accounting'], created_at: '2026-07-01T00:00:00Z' },
      ],
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    })
    mocks.useFleetEfficiency.mockReturnValue({ data: {} })

    renderOverview('/overview')
    await flushEffects()

    const rows = [...container.querySelectorAll('[data-team-rollup]')]
    expect(rows).toHaveLength(3)
    rows.forEach((row) => {
      expect(row.className).toContain('min-w-0')
      expect(row.className).toContain('overflow-hidden')
      expect(row.className).not.toMatch(/\bw-(?:20|24|40)\b/)

      const name = row.querySelector('[data-team-name]')
      expect(name.className).toContain('min-w-0')
      expect(name.className).toContain('truncate')

      const summary = row.querySelector('[data-team-summary]')
      expect(summary.className).toContain('shrink-0')
    })
  })
})

describe('Overview agent metric scoping', () => {
  const agents = [
    { id: 1, display_name: 'Akira', agent_username: 'akira', status: 'running', created_at: '2026-07-01T00:00:00Z' },
    { id: 2, display_name: 'Ryo', agent_username: 'ryo', status: 'idle', created_at: '2026-07-02T00:00:00Z' },
  ]

  it('labels the owner-scoped count as Your agents and preserves counts.total', async () => {
    mocks.useHive.mockReturnValue({ isStaff: false, username: 'Lina', currentOrgSlug: '' })
    mocks.useFleetAgents.mockReturnValue({
      data: agents,
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    })
    mocks.useFleetEfficiency.mockReturnValue({ data: {} })

    renderOverview('/overview')
    await flushEffects()

    expect(mocks.useFleetAgents).toHaveBeenCalledWith(expect.objectContaining({ ownerOnly: true }))
    expect(fleetMetricValue('Your agents')).toBe('2')
    expect(fleetMetricValue('Total agents')).toBeUndefined()
  })

  it('keeps the fleet-wide Total agents label and preserves counts.total for staff', async () => {
    mocks.useHive.mockReturnValue({ isStaff: true, username: 'Ryo', currentOrgSlug: '' })
    mocks.useFleetAgents.mockReturnValue({
      data: agents,
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    })
    mocks.useFleetEfficiency.mockReturnValue({ data: {} })

    renderOverview('/overview')
    await flushEffects()

    expect(mocks.useFleetAgents).toHaveBeenCalledWith(expect.objectContaining({ ownerOnly: false }))
    expect(fleetMetricValue('Total agents')).toBe('2')
    expect(fleetMetricValue('Your agents')).toBeUndefined()
  })
})

describe('HIVE-1599 full-row attention projection', () => {
  it('keeps Agents truth in the Overview headline, list, card tone, and team rollup', async () => {
    mocks.useHive.mockReturnValue({ isStaff: true, username: 'Kai', currentOrgSlug: '' })
    mocks.useFleetAgents.mockReturnValue({
      data: [
        {
          id: 1,
          display_name: 'Nagi',
          agent_username: 'nagi',
          status: 'running',
          needs_help: true,
          needs_help_reason: '10 ready task(s) with no progress',
          heartbeat_outcome: 'needs_help',
          heartbeat_ready_task_count: 10,
          heartbeat_progress_event_count: 0,
          activity_state: 'working',
          health_ok: true,
          provider_unavailable: false,
          availability_status: 'available',
          health_summary: { highest_severity: 'ok', issues: [], last_snapshot_age_s: 10 },
          team_names: ['engineering'],
          created_at: '2026-07-31T00:00:00Z',
        },
        {
          id: 2,
          display_name: 'Haru',
          agent_username: 'haru',
          status: 'stopped',
          needs_help: true,
          availability_status: 'unavailable',
          team_names: ['engineering'],
          created_at: '2026-07-30T00:00:00Z',
        },
      ],
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    })
    mocks.useFleetEfficiency.mockReturnValue({ data: {} })

    renderOverview('/overview')
    await flushEffects()

    expect(fleetMetricValue('Needs attention')).toBe('1')
    expect(container.textContent).toContain('1 agent needs attention')

    const nagiCard = [...container.querySelectorAll('button')]
      .find((button) => button.textContent.includes('@nagi'))
    expect(nagiCard).toBeDefined()
    expect(nagiCard.className).toContain('border-red-300')

    const teamRollup = container.querySelector('[data-team-rollup]')
    expect(teamRollup.textContent).toContain('1 needs help')
  })
})

describe('team rollup capacity tones', () => {
  it.each([
    {
      label: 'full capacity',
      agentCount: 2,
      statusCounts: { running: 2 },
      attentionCount: 0,
      width: '100%',
      barTone: 'from-emerald-500',
      summaryTone: 'text-emerald-600',
      trackTone: 'bg-gray-200/70',
    },
    {
      label: 'partial capacity',
      agentCount: 2,
      statusCounts: { running: 1, idle: 1 },
      attentionCount: 0,
      width: '50%',
      barTone: 'from-amber-500',
      summaryTone: 'text-amber-600',
      trackTone: 'bg-gray-200/70',
    },
    {
      label: 'attention-bearing capacity',
      agentCount: 2,
      statusCounts: { running: 1, blocked: 1 },
      attentionCount: 1,
      width: '50%',
      barTone: 'from-red-500',
      summaryTone: 'text-red-600',
      trackTone: 'bg-red-100',
    },
    {
      label: 'zero-percent critical capacity',
      agentCount: 2,
      statusCounts: { idle: 2 },
      attentionCount: 0,
      width: '0%',
      barTone: 'from-red-500',
      summaryTone: 'text-red-600',
      trackTone: 'bg-red-100',
    },
    {
      label: 'zero-agent capacity',
      agentCount: 0,
      statusCounts: {},
      attentionCount: 0,
      width: '0%',
      barTone: 'from-gray-400',
      summaryTone: 'text-gray-500',
      trackTone: 'bg-gray-200/70',
    },
  ])('uses the $label semantic tone', ({ agentCount, statusCounts, attentionCount, width, barTone, summaryTone, trackTone }) => {
    const { bar, track, summary } = renderTeamRollupCard({ agentCount, statusCounts, attentionCount })

    expect(bar.style.width).toBe(width)
    expect(bar.className).toContain(barTone)
    expect(track.className).toContain(trackTone)
    expect(summary.className).toContain(summaryTone)
  })
})

describe('organization switcher composite-menu keyboard behavior', () => {
  it('uses one composite focus stop and supports arrows, Home, End, Escape, and keyboard selection', async () => {
    const currentOrganization = { id: 1, slug: 'shizuha', name: 'Shizuha Global Pvt. Ltd' }
    const { switchOrganization } = renderOrgSwitcher({ currentOrganization })
    const trigger = container.querySelector('button[aria-haspopup="menu"]')

    act(() => trigger.click())
    await flushEffects()
    const items = [...container.querySelectorAll('[role="menuitemradio"]')]
    expect(items).toHaveLength(3)
    expect(items.every((item) => item.tabIndex === -1)).toBe(true)
    expect(document.activeElement.textContent).toContain('Shizuha Global Pvt. Ltd')

    act(() => document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true })))
    expect(document.activeElement.textContent).toContain('Shizuha Digital LLP')
    act(() => document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: 'Home', bubbles: true })))
    expect(document.activeElement.textContent).toContain('All Organizations')
    act(() => document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true })))
    expect(document.activeElement.textContent).toContain('Shizuha Digital LLP')
    act(() => document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowUp', bubbles: true })))
    expect(document.activeElement.textContent).toContain('Shizuha Global Pvt. Ltd')

    const digital = items.find((item) => item.textContent.includes('Shizuha Digital LLP'))
    act(() => digital.click()) // HTMLElement.click() is keyboard-like: MouseEvent.detail === 0
    expect(switchOrganization).toHaveBeenCalledWith(expect.objectContaining({ slug: 'digital' }))
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
    expect(document.activeElement).toBe(trigger)

    act(() => trigger.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowUp', bubbles: true })))
    await flushEffects()
    expect(document.activeElement.textContent).toContain('Shizuha Digital LLP')
    act(() => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
    expect(document.activeElement).toBe(trigger)
  })

  it('closes on Tab/Shift+Tab and moves focus to the adjacent external stop', async () => {
    const { navigate } = renderOrgSwitcher()
    const before = [...container.querySelectorAll('button')].find((button) => button.textContent === 'Before switcher')
    const trigger = container.querySelector('button[aria-haspopup="menu"]')
    const nav = container.querySelector('a[href="/overview"]')

    act(() => trigger.click())
    await flushEffects()
    act(() => document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true })))
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
    expect(document.activeElement).toBe(nav)
    act(() => nav.click())
    expect(navigate).toHaveBeenCalledTimes(1)

    act(() => trigger.click())
    await flushEffects()
    act(() => document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true })))
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
    expect(document.activeElement).toBe(before)
  })

  it('restores focus after keyboard-style selection and dismisses on an outside pointer', async () => {
    const { switchOrganization, navigate } = renderOrgSwitcher()
    const trigger = container.querySelector('button[aria-haspopup="menu"]')
    const nav = container.querySelector('a[href="/overview"]')

    act(() => trigger.click())
    await flushEffects()
    const firstItem = container.querySelector('[role="menuitemradio"]')
    act(() => firstItem.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })))
    act(() => firstItem.click())
    expect(switchOrganization).toHaveBeenCalledWith(null)
    expect(document.activeElement).toBe(trigger)

    act(() => trigger.click())
    await flushEffects()
    const digital = [...container.querySelectorAll('[role="menuitemradio"]')]
      .find((item) => item.textContent.includes('Shizuha Digital LLP'))
    act(() => digital.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', bubbles: true })))
    act(() => digital.click())
    expect(switchOrganization).toHaveBeenLastCalledWith(expect.objectContaining({ slug: 'digital' }))

    act(() => trigger.click())
    await flushEffects()
    act(() => nav.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true })))
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
    act(() => nav.click())
    expect(navigate).toHaveBeenCalledTimes(1)
  })
})
