import { describe, expect, it } from 'vitest'
import { deepenFleetAgent } from './agentSnapshot.js'

describe('deepenFleetAgent', () => {
  it('keeps card Pulse placement when detail omits team_memberships', () => {
    const snapshot = {
      id: 'hina',
      runtime_harness: 'Grok',
      team_names: ['ceo-decisions'],
      team_memberships: [{ team_slug: 'ceo-decisions', team_name: 'CEO Decisions' }],
    }
    const live = {
      id: 'hina',
      runtime_harness: 'Shizuha CLI',
      context_budget: { total_tokens: 5053 },
    }
    expect(deepenFleetAgent(snapshot, live)).toEqual({
      id: 'hina',
      runtime_harness: 'Shizuha CLI',
      context_budget: { total_tokens: 5053 },
      team_names: ['ceo-decisions'],
      team_memberships: [{ team_slug: 'ceo-decisions', team_name: 'CEO Decisions' }],
    })
  })

  it('lets a live membership list replace the snapshot', () => {
    const snapshot = { team_memberships: [], team_names: [] }
    const live = {
      team_memberships: [{ team_slug: 'ceo-decisions' }],
      team_names: ['ceo-decisions'],
    }
    expect(deepenFleetAgent(snapshot, live).team_names).toEqual(['ceo-decisions'])
  })
})
