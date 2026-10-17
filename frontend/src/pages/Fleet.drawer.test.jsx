import { describe, expect, it } from 'vitest'

import AgentDrawerSource from '../components/AgentDrawer.jsx?raw'
import { drawerSelection } from './Fleet'

describe('fleet drawer selection', () => {
  it('carries the visible agent snapshot into the drawer for immediate paint', () => {
    const agent = {
      id: 'runtime-agent-id',
      agent_username: 'jun',
      display_name: 'Jun',
      status: 'running',
    }

    expect(drawerSelection(agent)).toEqual({
      kind: 'fleet',
      id: 'runtime-agent-id',
      snapshot: agent,
    })
  })

  it('carries the claw card snapshot into the drawer for immediate paint (HIVE-1520)', () => {
    const claw = {
      id: 'claw:abc123',
      kind: 'claw',
      name: 'My Assistant',
      status: 'running',
      handle: 'owner: mika',
    }

    expect(drawerSelection(claw)).toEqual({
      kind: 'claw',
      id: 'abc123',
      snapshot: claw,
    })
  })

  it('does not gate snapshot-backed drawer content on the background detail request', () => {
    expect(AgentDrawerSource).toContain('{!agent ? (')
    expect(AgentDrawerSource).not.toContain('{isLoading || !agent ? (')
  })

  it('does not gate the claw drawer on the background detail request either (HIVE-1520)', () => {
    expect(AgentDrawerSource).toContain('{!claw ? (')
    expect(AgentDrawerSource).not.toContain('{isLoading || !claw ? (')
  })

  it('deepens the card snapshot instead of replacing Pulse placement', () => {
    expect(AgentDrawerSource).toContain('deepenFleetAgent(initialAgent, liveAgent)')
    expect(AgentDrawerSource).not.toContain('liveAgent || initialAgent')
  })
})
