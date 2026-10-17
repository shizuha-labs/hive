import React, { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, describe, expect, it } from 'vitest'

import { ActivityTimeline } from './AgentDrawer'

globalThis.IS_REACT_ACT_ENVIRONMENT = true

let container
let root

function renderTimeline(data) {
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  act(() => root.render(<ActivityTimeline data={data} loading={false} rate={null} />))
}

afterEach(() => {
  if (root) act(() => root.unmount())
  container?.remove()
  root = null
  container = null
})

describe('native bridge activity contract', () => {
  it('renders a fresh normalised before/after audit pair as tool activity', () => {
    const now = new Date().toISOString()
    renderTimeline({
      total: 2,
      events: [
        { id: 'call-1', ts: now, type: 'tool_call', tool: 'mcp__pulse__get', input: 'HIVE-609' },
        { id: 'call-1', ts: now, type: 'tool_result', tool: 'mcp__pulse__get', output: 'ok' },
      ],
    })

    expect(container.textContent).toContain('tool call · mcp__pulse__get')
    expect(container.textContent).toContain('tool result · mcp__pulse__get')
    expect(container.textContent).toContain('HIVE-609')
    expect(container.textContent).toContain('ok')
    expect(container.textContent).not.toContain('live session source may not be ingested')
  })

  it('shows excerpt, tools, and a no-tools chip on high-prefill empty turns', () => {
    const now = new Date().toISOString()
    renderTimeline({
      total: 2,
      events: [
        {
          ts: now,
          type: 'turn_summary',
          text: 'LLM turn completed: DeepSeek-V4-Flash, 8.3s, 294089 input tokens, 101 output tokens, 0 tool calls — no tools',
          tool_calls: 0,
          input_tokens: 294089,
          assistant_excerpt: '',
          tool_names: [],
        },
        {
          ts: now,
          type: 'turn_summary',
          text: 'LLM turn completed: DeepSeek-V4-Flash, 49s, 157076 input tokens, 836 output tokens, 1 tool calls — tools: bash',
          tool_calls: 1,
          input_tokens: 157076,
          assistant_excerpt: 'Checking Pulse for SCLI-376',
          tool_names: ['bash'],
        },
      ],
    })

    expect(container.textContent).toContain('no tools')
    expect(container.textContent).toContain('Checking Pulse for SCLI-376')
    expect(container.textContent).toContain('bash')
  })

  it('renders a degraded receipt as not-live with segregated history (HIVE-1552)', () => {
    renderTimeline({
      source_state: 'degraded',
      source: 'legacy-history',
      resource_uid: 'deploy-uid-1',
      total: 0,
      events: [],
      history_events: [
        { ts: '2026-07-04T08:57:50Z', type: 'message', role: 'system', text: 'legacy row' },
      ],
    })

    // Empty live tail is not an outage — stale rows stay historical, not live.
    expect(container.textContent).toContain('Waiting for the next turn')
    expect(container.textContent).toContain('not live')
    expect(container.textContent).toContain('legacy row')
    expect(container.textContent).not.toContain('No recent activity recorded')
  })

  it('renders an unavailable receipt explicitly (HIVE-1552)', () => {
    renderTimeline({
      source_state: 'unavailable',
      total: 0,
      events: [],
      history_events: [],
    })

    expect(container.textContent).toContain('Live activity source is unavailable')
    expect(container.textContent).not.toContain('No recent activity recorded')
  })

  it('renders persisted history after restart instead of a blank feed', () => {
    renderTimeline({
      source_state: 'persisted',
      persisted_at: '2026-09-15T15:35:00Z',
      live_source_state: 'unavailable',
      total: 1,
      events: [
        {
          id: 'call-1',
          ts: '2026-09-15T15:35:00Z',
          type: 'tool_call',
          tool: 'mcp__pulse__get',
          input: 'PLAT-8846',
        },
      ],
      history_events: [],
    })

    expect(container.textContent).toContain('this history survives restart and hibernate')
    expect(container.textContent).toContain('tool call · mcp__pulse__get')
    expect(container.textContent).toContain('PLAT-8846')
    expect(container.textContent).not.toContain('No recent activity recorded')
  })

  it('shows persisted history under a live empty reset instead of a blank feed', () => {
    renderTimeline({
      source_state: 'live',
      total: 0,
      events: [],
      history_events: [
        {
          id: 'call-1',
          ts: '2026-09-15T15:35:00Z',
          type: 'tool_call',
          tool: 'mcp__pulse__get',
          input: 'PLAT-8846',
        },
      ],
    })

    expect(container.textContent).toContain('this session has no live events yet')
    expect(container.textContent).toContain('tool call · mcp__pulse__get')
    expect(container.textContent).not.toContain('No recent activity recorded')
  })

  it('treats a failed activity fetch as unavailable, not as empty history', () => {
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    act(() => root.render(
      <ActivityTimeline data={undefined} loading={false} rate={null} error />,
    ))

    expect(container.textContent).toContain('Live activity source is unavailable')
    expect(container.textContent).not.toContain('No recent activity recorded')
  })
})
