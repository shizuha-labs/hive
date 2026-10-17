// A card or team header must NEVER show less work than the agent actually owns.
//
// Operator 2026-08-05, on Shizuha Global Pvt. Ltd › Accounting:
//
//     i don't understand why banto is even running here .. ideally hive should
//     have hibernated such agents automatically since the team doesn't seem to
//     have any active tasks?
//     ...
//     note that the card and the teams headers should never under-report ..
//     fix any underlying issues
//
// banto's card read "0 open · 0 doing · 0 review", and the header read
// "0 active". Pulse held three tasks assigned to banto — BKSD-10 (todo, high),
// BKSD-8 and BKSD-9 (blocked) — and Hive's own snapshot agreed:
//
//   workload['banto@shizuha.com']                  -> open 1, blocked 2, overdue 2
//   workload_by_assignee_team['banto@...::accounting'] -> open 0, blocked 0, overdue 0
//
// The card reads the second key. It is `<email>::<assignment_group>`, so a task
// assigned to the agent whose assignment_group differs from — or is missing on —
// the card's team is silently dropped. The hibernation sweep reads the
// email-keyed totals, so the sweep correctly kept the seat warm while the page
// said there was nothing to do. Two surfaces, two answers, and the one a human
// looks at was the wrong one.
//
// The fix floors both surfaces at the truth they can see. These tests use
// banto's real numbers.
import { describe, expect, it } from 'vitest'

import {
  mergeNeverBelow,
  lookupAgentWorkload,
  workloadForTeamCard,
  workloadForTeamHeader,
} from './Fleet.jsx'

// banto's actual snapshot rows, 2026-08-05.
const BANTO_TRUTH = {
  open: 1, in_progress: 0, blocked: 2, in_review: 0,
  scheduled: 2, backlog: 0, completed: 5, overdue: 2,
}
const BANTO_TEAM_SLICE = {
  open: 0, in_progress: 0, blocked: 0, in_review: 0,
  scheduled: 2, backlog: 0, completed: 3, overdue: 0,
}

const GROUP = { key: 'accounting', teamKey: 'accounting', title: 'Accounting' }

function ctxFor(workload) {
  return {
    agent: { email: 'banto@shizuha.com', agent_id: 'banto' },
    node: null,
    workload,
  }
}

describe('mergeNeverBelow', () => {
  it('raises every bucket to the larger of the two', () => {
    const merged = mergeNeverBelow(BANTO_TEAM_SLICE, BANTO_TRUTH)
    expect(merged.open).toBe(1)
    expect(merged.blocked).toBe(2)
    expect(merged.overdue).toBe(2)
    expect(merged.completed).toBe(5)
  })

  it('never lowers a bucket the scoped view reports higher', () => {
    const merged = mergeNeverBelow({ open: 9 }, { open: 1, blocked: 2 })
    expect(merged.open).toBe(9)
    expect(merged.blocked).toBe(2)
  })

  it('survives a missing side', () => {
    expect(mergeNeverBelow(null, BANTO_TRUTH)).toEqual(BANTO_TRUTH)
    expect(mergeNeverBelow(BANTO_TRUTH, null)).toEqual(BANTO_TRUTH)
    expect(mergeNeverBelow(null, null)).toBeNull()
  })
})

describe('Pulse identity aliases on the agent card', () => {
  it('folds sara@agents.shizuha.io Pulse rows onto the sara@shizuha.com card', () => {
    const agent = {
      agent_username: 'sara',
      email: 'sara@shizuha.com',
      identity_health: { canonical_email: 'sara@agents.shizuha.io' },
    }
    const load = lookupAgentWorkload({
      'sara@agents.shizuha.io': { open: 4, in_progress: 1, blocked: 0, in_review: 0 },
    }, agent)
    expect(load.open, 'CON-450/open recurring work lives on the ID email').toBe(4)
    expect(load.in_progress).toBe(1)
  })

  it('sums disjoint buckets split across the two emails rather than taking max', () => {
    const agent = { agent_username: 'sara' }
    const load = lookupAgentWorkload({
      'sara@shizuha.com': { open: 0, in_progress: 0, in_review: 5 },
      'sara@agents.shizuha.io': { open: 4, in_progress: 1, in_review: 0 },
    }, agent)
    expect(load.open).toBe(4)
    expect(load.in_progress).toBe(1)
    expect(load.in_review).toBe(5)
  })
})

describe('agent card never under-reports', () => {
  it("shows banto's open task even though it is not in the team slice", () => {
    const load = workloadForTeamCard(ctxFor(BANTO_TRUTH), GROUP, {
      'banto@shizuha.com::accounting': BANTO_TEAM_SLICE,
    })
    expect(load.open, 'BKSD-10 is assigned to banto and must appear').toBe(1)
    expect(load.blocked, 'BKSD-8 and BKSD-9 are assigned to banto').toBe(2)
    expect(load.overdue).toBe(2)
  })

  it('falls back to the agent totals when there is no team slice at all', () => {
    // Previously returned null here — an empty card for an agent with work.
    const load = workloadForTeamCard(ctxFor(BANTO_TRUTH), GROUP, {
      'someone-else@shizuha.com::accounting': BANTO_TEAM_SLICE,
    })
    expect(load).toEqual(BANTO_TRUTH)
  })

  it('never reports fewer items than the agent owns, in any bucket', () => {
    const load = workloadForTeamCard(ctxFor(BANTO_TRUTH), GROUP, {
      'banto@shizuha.com::accounting': BANTO_TEAM_SLICE,
    })
    for (const bucket of Object.keys(BANTO_TRUTH)) {
      expect(
        Number(load[bucket] || 0),
        `bucket ${bucket} under-reported`,
      ).toBeGreaterThanOrEqual(BANTO_TRUTH[bucket])
    }
  })
})

describe('team header never under-reports', () => {
  it('cannot read idle while its own cards show work', () => {
    const header = workloadForTeamHeader(
      GROUP,
      { accounting: { open: 0, blocked: 2, scheduled: 2, backlog: 0 } },
      [BANTO_TRUTH],
    )
    expect(header.open, 'the header said 0 active while a card showed work').toBe(1)
    expect(header.blocked).toBe(2)
  })

  it('keeps a header total that legitimately exceeds the member sum', () => {
    // A team header also counts its UNASSIGNED queue, so it is expected to run
    // ahead of the sum of its members. Flooring must not clamp that down.
    const header = workloadForTeamHeader(
      GROUP,
      { accounting: { open: 40, blocked: 2 } },
      [BANTO_TRUTH],
    )
    expect(header.open).toBe(40)
    expect(header.blocked).toBe(2)
  })

  it('falls back to the member sum when the team has no entry', () => {
    const header = workloadForTeamHeader(GROUP, {}, [BANTO_TRUTH, { open: 3 }])
    expect(header.open).toBe(4)
    expect(header.blocked).toBe(2)
  })

  it('sums across every member rather than taking one', () => {
    const header = workloadForTeamHeader(
      GROUP,
      { accounting: { open: 0 } },
      [{ open: 2, blocked: 1 }, { open: 3, blocked: 4 }],
    )
    expect(header.open).toBe(5)
    expect(header.blocked).toBe(5)
  })
})
