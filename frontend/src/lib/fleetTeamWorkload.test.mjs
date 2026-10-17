import { describe, expect, it } from 'vitest'
import { buildTeamWorkloadLookup } from './fleetTeamWorkload.js'

describe('buildTeamWorkloadLookup', () => {
  it('preserves org-scoped workload when roster ids reuse a team slug', () => {
    const scheduled = { open: 0, in_progress: 0, blocked: 0, in_review: 0, scheduled: 2, backlog: 0 }
    const idle = { open: 0, in_progress: 0, blocked: 0, in_review: 0, scheduled: 0, backlog: 0 }
    const teams = [
      { id: 1, name: 'Accounting', organization_slug: 'shizuha' },
      { id: 26, name: 'Accounting', organization_slug: 'shizuha-digital' },
      // Pulse's roster endpoint uses the slug as id. This row is processed after
      // the numeric team rows in the live combined response.
      { id: 'accounting', slug: 'accounting', organization_slug: 'shizuha' },
    ]

    const lookup = buildTeamWorkloadLookup({ 1: scheduled, 26: idle }, teams)

    expect(lookup['shizuha::accounting']).toBe(scheduled)
    expect(lookup['shizuha-digital::accounting']).toBe(idle)
  })

  it('never lets an idle same-named team in another org zero the shared key', () => {
    // Live 2026-07-27: shizuha Engineering rendered "idle · 0 active · 0 blocked
    // · 0 backlog" in the team header while its own member cards showed
    // 14/40/20, 6/71/9 and 6/5/1. Five orgs own a team named "engineering"; the
    // all-zero ones are truthy objects, so the LAST one processed overwrote
    // shizuha's real aggregate on the shared unqualified key, and a group
    // without an org slug resolves through exactly that key.
    const busy = { open: 267, in_progress: 135, blocked: 148, in_review: 1, scheduled: 3, backlog: 2, completed: 179, overdue: 1 }
    const idle = { open: 0, in_progress: 0, blocked: 0, in_review: 0, scheduled: 0, backlog: 0, completed: 0, overdue: 0 }
    const teams = [
      { id: 5, name: 'Engineering', organization_slug: 'shizuha' },
      { id: 153, name: 'Engineering', organization_slug: 'shizuha-digital' },
      { id: 'roster:engineering', name: 'engineering', organization_slug: 'nts56-qa-isolation-a' },
      { id: 'roster:engineering', name: 'engineering', organization_slug: 'nts56-qa-isolation-b' },
      // Roster rows reuse the slug as id and are appended AFTER the numeric rows.
      { id: 'engineering', slug: 'engineering', organization_slug: 'shizuha-trading' },
      { id: 'engineering', slug: 'engineering', organization_slug: 'shizuha' },
    ]

    const lookup = buildTeamWorkloadLookup({ 5: busy, 153: idle, 'roster:engineering': idle }, teams)

    // Each org keeps its OWN truth...
    expect(lookup['shizuha::engineering']).toBe(busy)
    expect(lookup['shizuha-digital::engineering']).toBe(idle)
    // ...and the shared key must report the real work, not a zero from a
    // different org — otherwise a busy team renders as "idle".
    expect(lookup['engineering']).toBe(busy)
  })

  it('maps a directory display alias onto the canonical routing team', () => {
    const workload = {
      open: 0, in_progress: 0, blocked: 2, in_review: 0,
      scheduled: 24, backlog: 0,
    }
    const teams = [
      { id: 30, name: 'Research', organization_slug: 'shizuha' },
      {
        id: 'research-analytics', slug: 'research-analytics',
        name: 'Research', organization_slug: 'shizuha',
      },
    ]

    const lookup = buildTeamWorkloadLookup({ 30: workload }, teams)

    expect(lookup['shizuha::research-analytics']).toBe(workload)
    expect(lookup['research-analytics']).toBe(workload)
  })
})
