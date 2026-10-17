import { describe, expect, it } from 'vitest'
import {
  buildTeamGroups,
  contextMatchesTeamFilter,
  teamFilterOptions,
  teamPlacementsOf,
  teamKeyOf,
  teamSeedMatchesFilter,
} from './fleetGrouping.js'
import { humanize, teamDisplayLabel } from './format.js'

const titleFn = (key) => humanize(key)

// Helper: an agent context with the given team placements.
const ctx = (id, placements) => ({ agent: { id }, teamPlacements: placements })

describe('HIVE-443 teamPlacementsOf', () => {
  it('extracts org-tagged placements from team_memberships', () => {
    const agent = {
      team_memberships: [
        { team_slug: 'accounting', team_name: 'Accounting', organization_slug: 'digital', organization_name: 'Shizuha Digital LLP' },
      ],
    }
    expect(teamPlacementsOf(agent)).toEqual([
      { orgSlug: 'digital', orgName: 'Shizuha Digital LLP', teamKey: 'accounting', teamDisplay: 'Accounting' },
    ])
  })

  it('accepts org_slug/org_name aliases and lowercases the slug', () => {
    const agent = { team_memberships: [{ team_name: 'Review', org_slug: 'ACME', org_name: 'Acme Inc.' }] }
    expect(teamPlacementsOf(agent)).toEqual([
      { orgSlug: 'acme', orgName: 'Acme Inc.', teamKey: 'review', teamDisplay: 'Review' },
    ])
  })

  it('fails closed to Unassigned when authoritative memberships are absent', () => {
    expect(teamPlacementsOf({}, ['engineering', 'merge'])).toEqual([])
  })

  it('de-dups the same (org, team) pair', () => {
    const agent = {
      team_memberships: [
        { team_slug: 'review', organization_slug: 'a' },
        { team_slug: 'review', organization_slug: 'a' },
      ],
    }
    expect(teamPlacementsOf(agent)).toHaveLength(1)
  })

  it('renders the authoritative membership without unioning org-chart placements', () => {
    const agent = {
      team_memberships: [
        { team_slug: 'architecture', organization_slug: 'shizuha', organization_name: 'Shizuha Global' },
      ],
    }
    const pulsePlacements = [
      { teamKey: 'merge', orgSlug: 'shizuha', orgName: 'Shizuha Global' },
      { teamKey: 'accounting', orgSlug: 'shizuha-digital', orgName: 'Shizuha Digital' },
    ]
    expect(teamPlacementsOf(agent, [], { pulsePlacements })).toEqual([
      {
        orgSlug: 'shizuha', orgName: 'Shizuha Global',
        teamKey: 'architecture', teamDisplay: 'Architecture',
      },
    ])
  })

  it('treats an explicit empty membership array as authoritative unassigned', () => {
    const agent = { team_memberships: [] }
    const pulsePlacements = [
      { teamKey: 'cluster-managers', orgSlug: 'shizuha', orgName: 'Shizuha Global' },
    ]
    expect(teamPlacementsOf(agent, ['cluster-managers'], { pulsePlacements })).toEqual([])
  })
})

describe('HIVE-443 buildTeamGroups — All-Organizations mode', () => {
  it('renders two same-named teams across orgs as two distinct "<Org> › <Team>" groups', () => {
    // Acceptance: two orgs each with an Accounting team -> two org-labeled groups.
    const contexts = [
      ctx('a', [{ orgSlug: 'digital', orgName: 'Shizuha Digital LLP', teamKey: 'accounting', teamDisplay: 'Accounting' }]),
      ctx('b', [{ orgSlug: 'shizuha', orgName: 'Shizuha Global Pvt. Ltd.', teamKey: 'accounting', teamDisplay: 'Accounting' }]),
    ]
    const groups = buildTeamGroups({ contexts, allOrgs: true, groupTitleFn: titleFn })
    expect(groups.map((g) => g.title)).toEqual([
      'Shizuha Digital LLP › Accounting',
      'Shizuha Global Pvt. Ltd. › Accounting',
    ])
    // Each group carries exactly its own org's member and the bare teamKey (for the load chip).
    expect(groups.every((g) => g.contexts.length === 1)).toBe(true)
    expect(groups.every((g) => g.teamKey === 'accounting')).toBe(true)
    expect(new Set(groups.map((g) => g.orgSlug))).toEqual(new Set(['digital', 'shizuha']))
  })

  it('clusters each org\'s teams together (sorted by "<Org> › <Team>" title)', () => {
    const contexts = [
      ctx('a', [{ orgSlug: 'digital', orgName: 'Digital', teamKey: 'sales', teamDisplay: 'Sales' }]),
      ctx('b', [{ orgSlug: 'digital', orgName: 'Digital', teamKey: 'accounting', teamDisplay: 'Accounting' }]),
      ctx('c', [{ orgSlug: 'shizuha', orgName: 'Shizuha', teamKey: 'accounting', teamDisplay: 'Accounting' }]),
    ]
    const groups = buildTeamGroups({ contexts, allOrgs: true, groupTitleFn: titleFn })
    expect(groups.map((g) => g.title)).toEqual([
      'Digital › Accounting',
      'Digital › Sales',
      'Shizuha › Accounting',
    ])
  })

  it('does NOT seed org-labeled empty groups from the org-agnostic team list', () => {
    const contexts = [
      ctx('a', [{ orgSlug: 'digital', orgName: 'Digital', teamKey: 'accounting', teamDisplay: 'Accounting' }]),
    ]
    const groups = buildTeamGroups({ contexts, allTeamNames: ['engineering', 'merge'], allOrgs: true, groupTitleFn: titleFn })
    expect(groups.map((g) => g.title)).toEqual(['Digital › Accounting'])
  })

  it('falls back to the org label slug when organization_name is missing', () => {
    const contexts = [ctx('a', [{ orgSlug: 'digital', orgName: '', teamKey: 'accounting', teamDisplay: 'Accounting' }])]
    const groups = buildTeamGroups({ contexts, allOrgs: true, groupTitleFn: titleFn })
    expect(groups[0].title).toBe('digital › Accounting')
  })

  it('BACKWARD-COMPAT: All-Orgs but no org info -> plain team grouping (collapses, no regression)', () => {
    const contexts = [
      ctx('a', [{ orgSlug: '', orgName: '', teamKey: 'review', teamDisplay: 'Review' }]),
      ctx('b', [{ orgSlug: '', orgName: '', teamKey: 'review', teamDisplay: 'Review' }]),
    ]
    const groups = buildTeamGroups({ contexts, allOrgs: true, groupTitleFn: titleFn })
    expect(groups).toHaveLength(1)
    expect(groups[0].title).toBe('Review')
    expect(groups[0].contexts).toHaveLength(2)
  })
})

describe('CON-177 buildTeamGroups — org-prefixed team headers', () => {
  it('prefixes team headers with the org even in single-org mode when org info is present', () => {
    const contexts = [
      ctx('a', [{ orgSlug: 'digital', orgName: 'Digital', teamKey: 'accounting', teamDisplay: 'Accounting' }]),
      ctx('b', [{ orgSlug: 'digital', orgName: 'Digital', teamKey: 'accounting', teamDisplay: 'Accounting' }]),
    ]
    const groups = buildTeamGroups({ contexts, allOrgs: false, groupTitleFn: titleFn })
    expect(groups).toHaveLength(1)
    expect(groups[0].title).toBe('Digital › Accounting')
    expect(groups[0].key).toBe('digital::accounting')
    expect(groups[0].contexts).toHaveLength(2)
  })

  it('seeds every org-scoped team (incl. empty/unstaffed) with org-prefixed headers', () => {
    const contexts = [ctx('a', [{ orgSlug: 'shizuha', orgName: 'Shizuha Global Pvt. Ltd', teamKey: 'engineering', teamDisplay: 'Engineering' }])]
    const groups = buildTeamGroups({
      contexts,
      allTeamNames: [
        { orgSlug: 'shizuha', orgName: 'Shizuha Global Pvt. Ltd', teamKey: 'engineering', teamDisplay: 'Engineering' },
        { orgSlug: 'shizuha', orgName: 'Shizuha Global Pvt. Ltd', teamKey: 'human-ops', teamDisplay: 'Human Ops' },
      ],
      allOrgs: false,
      groupTitleFn: titleFn,
    })
    const humanOps = groups.find((g) => g.teamKey === 'human-ops')
    expect(humanOps).toBeTruthy()
    expect(humanOps.title).toBe('Shizuha Global Pvt. Ltd › Human Ops')
    expect(humanOps.contexts).toHaveLength(0)
  })

  it('keeps legacy plain empty teams when no org identity is available', () => {
    const contexts = [ctx('a', [{ orgSlug: '', orgName: '', teamKey: 'engineering', teamDisplay: 'Engineering' }])]
    const groups = buildTeamGroups({ contexts, allTeamNames: ['engineering', 'merge', 'design'], allOrgs: false, groupTitleFn: titleFn })
    const merge = groups.find((g) => g.teamKey === 'merge')
    expect(merge).toBeTruthy()
    expect(merge.title).toBe('Merge')
    expect(merge.contexts).toHaveLength(0)
  })

  it('places a multi-team agent under each of its teams', () => {
    const contexts = [
      ctx('aoi', [
        { orgSlug: '', orgName: '', teamKey: 'architecture', teamDisplay: 'Architecture' },
        { orgSlug: '', orgName: '', teamKey: 'merge', teamDisplay: 'Merge' },
      ]),
    ]
    const groups = buildTeamGroups({ contexts, allOrgs: false, groupTitleFn: titleFn })
    expect(groups.map((g) => g.teamKey).sort()).toEqual(['architecture', 'merge'])
    expect(groups.every((g) => g.contexts.length === 1)).toBe(true)
  })

  it('buckets an agent with no placements under "unassigned"', () => {
    const groups = buildTeamGroups({ contexts: [{ agent: { id: 'x' }, teamPlacements: [] }], allOrgs: false, groupTitleFn: titleFn })
    expect(groups).toHaveLength(1)
    expect(groups[0].teamKey).toBe('unassigned')
  })
})

describe('HIVE-443 teamKeyOf', () => {
  it('normalizes spaces/underscores to a lowercase slug', () => {
    expect(teamKeyOf('Trading Ops')).toBe('trading-ops')
    expect(teamKeyOf('risk_and_validation')).toBe('risk-and-validation')
    expect(teamKeyOf('  Review ')).toBe('review')
  })

  it('collapses legacy Research and Analytics labels into one visible team key', () => {
    expect(teamKeyOf('Research')).toBe('research-analytics')
    expect(teamKeyOf('Analytics')).toBe('research-analytics')
    expect(teamKeyOf('Research Analytics')).toBe('research-analytics')
  })

  it('does not seed a second empty group for a legacy Research label', () => {
    const groups = buildTeamGroups({
      contexts: [ctx('hana', [{
        orgSlug: 'shizuha',
        orgName: 'Shizuha Global Pvt. Ltd',
        teamKey: 'research-analytics',
        teamDisplay: 'Research',
      }])],
      allTeamNames: [
        { orgSlug: 'shizuha', orgName: 'Shizuha Global Pvt. Ltd', teamKey: 'research-analytics', teamDisplay: 'Research' },
        { orgSlug: 'shizuha', orgName: 'Shizuha Global Pvt. Ltd', teamKey: 'analytics', teamDisplay: 'Analytics' },
      ],
      groupTitleFn: titleFn,
    })
    expect(groups.filter((group) => group.teamKey === 'research-analytics')).toHaveLength(1)
    expect(groups.find((group) => group.teamKey === 'research-analytics')?.title)
      .toBe('Shizuha Global Pvt. Ltd › Research')
  })
})

describe('authoritative membership projection', () => {
  it('does not turn org-chart lead metadata into membership', () => {
    const agent = {}
    const pulsePlacements = [
      { orgSlug: 'shizuha', orgName: 'Shizuha Global Pvt. Ltd', teamKey: 'engineering', teamDisplay: 'Engineering' },
    ]
    expect(teamPlacementsOf(agent, [], { pulsePlacements })).toEqual([])
  })

  it('does not infer membership from a unique org-scoped team seed', () => {
    const agent = {}
    const teamSeeds = [
      { orgSlug: 'shizuha', orgName: 'Shizuha Global Pvt. Ltd', teamKey: 'engineering', teamDisplay: 'Engineering' },
      { orgSlug: 'shizuha', orgName: 'Shizuha Global Pvt. Ltd', teamKey: 'merge', teamDisplay: 'Merge' },
    ]
    expect(teamPlacementsOf(agent, ['engineering'], { teamSeeds })).toEqual([])
  })

  it('does NOT invent an org when the same team name exists in multiple orgs', () => {
    const agent = {}
    const teamSeeds = [
      { orgSlug: 'shizuha', orgName: 'Shizuha', teamKey: 'accounting', teamDisplay: 'Accounting' },
      { orgSlug: 'shizuha-digital', orgName: 'Digital', teamKey: 'accounting', teamDisplay: 'Accounting' },
    ]
    expect(teamPlacementsOf(agent, ['accounting'], { teamSeeds })).toEqual([])
  })

  it('groups a lead-only agent under Org › Team with the roster members', () => {
    const contexts = [
      ctx('jun', [{ orgSlug: 'shizuha', orgName: 'Shizuha Global Pvt. Ltd', teamKey: 'engineering', teamDisplay: 'Engineering' }]),
      // Ryo: lead_email path only — previously rendered a bare "Engineering" group.
      ctx('ryo', [{ orgSlug: 'shizuha', orgName: 'Shizuha Global Pvt. Ltd', teamKey: 'engineering', teamDisplay: 'Engineering' }]),
    ]
    const groups = buildTeamGroups({ contexts, groupTitleFn: titleFn })
    expect(groups).toHaveLength(1)
    expect(groups[0].title).toBe('Shizuha Global Pvt. Ltd › Engineering')
    expect(groups[0].contexts.map((c) => c.agent.id).sort()).toEqual(['jun', 'ryo'])
  })

  it('fills organization display name from orgNameBySlug when memberships only carry the slug', () => {
    const agent = {
      team_memberships: [
        { team_slug: 'engineering', organization_slug: 'shizuha' },
      ],
    }
    expect(teamPlacementsOf(agent, [], {
      orgNameBySlug: { shizuha: 'Shizuha Global Pvt. Ltd' },
    })).toEqual([
      { orgSlug: 'shizuha', orgName: 'Shizuha Global Pvt. Ltd', teamKey: 'engineering', teamDisplay: 'Engineering' },
    ])
  })
})

describe('PLAT-4212 organization-qualified team labels', () => {
  it('renders the organization name and team display name as a single identity', () => {
    expect(teamDisplayLabel({
      organization_name: 'Shizuha Digital LLP',
      organization_slug: 'digital',
      display_name: 'Accounting',
    })).toBe('Shizuha Digital LLP › Accounting')
  })

  it('falls back to the organization slug and preserves legacy plain labels', () => {
    expect(teamDisplayLabel({ organization_slug: 'personal', team_name: 'Personal Accounting' }))
      .toBe('personal › Personal Accounting')
    expect(teamDisplayLabel({ team_name: 'Engineering' })).toBe('Engineering')
  })

  it('keeps same-named team filter options distinct by organization', () => {
    const digital = { orgSlug: 'digital', orgName: 'Shizuha Digital LLP', teamKey: 'accounting', teamDisplay: 'Accounting' }
    const global = { orgSlug: 'global', orgName: 'Shizuha Global Pvt. Ltd.', teamKey: 'accounting', teamDisplay: 'Accounting' }
    const contexts = [
      { teamPlacements: [digital], teamNames: ['accounting'] },
      { teamPlacements: [global], teamNames: ['accounting'] },
    ]

    expect(teamFilterOptions(contexts, [digital, global])).toEqual([
      { value: 'digital::accounting', label: 'Shizuha Digital LLP › Accounting', count: 1 },
      { value: 'global::accounting', label: 'Shizuha Global Pvt. Ltd. › Accounting', count: 1 },
    ])
    expect(contextMatchesTeamFilter(contexts[0], 'digital::accounting')).toBe(true)
    expect(contextMatchesTeamFilter(contexts[1], 'digital::accounting')).toBe(false)
    expect(teamSeedMatchesFilter(digital, 'digital::accounting')).toBe(true)
    expect(teamSeedMatchesFilter(global, 'digital::accounting')).toBe(false)
  })

  it('retains the plain team filter contract for orgless legacy payloads', () => {
    const context = { teamPlacements: [], teamNames: ['engineering'] }
    expect(teamFilterOptions([context])).toEqual([
      { value: 'engineering', label: 'Engineering', count: 1 },
    ])
    expect(contextMatchesTeamFilter(context, 'engineering')).toBe(true)
    expect(teamSeedMatchesFilter('engineering', 'engineering')).toBe(true)
  })
})
