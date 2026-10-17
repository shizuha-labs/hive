import React, { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, describe, expect, it } from 'vitest'

import FleetAgentCard from './FleetAgentCard'

globalThis.IS_REACT_ACT_ENVIRONMENT = true

let container
let root

function renderCard(agent) {
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  act(() => root.render(
    <FleetAgentCard
      agent={agent}
      workload={{}}
      teamNames={['engineering']}
    />,
  ))
}

afterEach(() => {
  if (root) act(() => root.unmount())
  container?.remove()
  root = null
  container = null
})

describe('FleetAgentCard reconciled health verdict', () => {
  it('renders hibernation and lease waiting as a healthy blue scheduler state', () => {
    renderCard({
      id: 'recon',
      agent_username: 'recon',
      display_name: 'Recon',
      status: 'hibernated',
      hibernation_reason: 'does not hold the model lease',
      runtime_harness: 'Shizuha CLI',
      model_provider: 'Cortex',
      model: 'cortex/DeepSeek-V4-Flash',
      model_lease: {
        managed: true,
        state: 'waiting',
        holder: 'agent-nagi',
      },
    })

    expect(container.textContent).toContain('Hibernated')
    expect(container.textContent).toContain('Waiting')
    expect(container.textContent).toContain('behind @nagi')
    expect(container.querySelector('.hive-life-hibernate')).not.toBeNull()
  })

  it('does not keep a leftover Needs help chip on a hibernated card', () => {
    renderCard({
      id: 'sora',
      agent_username: 'sora',
      display_name: 'Sora',
      status: 'hibernated',
      hibernation_reason: 'does not hold the model lease',
      needs_help: true,
      needs_help_reason: '4 ready task(s) with no progress for 4 heartbeat(s)',
      runtime_harness: 'Shizuha CLI',
      model_provider: 'Cortex',
      model: 'cortex/GLM-5.3-Flash',
      health_summary: {
        highest_severity: 'attention',
        issues: [{
          type: 'heartbeat_needs_help',
          severity: 'attention',
          service: 'work',
          label: 'Agent needs help',
          detail: '4 ready task(s) with no progress for 4 heartbeat(s)',
        }],
      },
    })
    expect(container.textContent).toContain('Hibernated')
    expect(container.textContent).not.toContain('Agent needs help')
    expect(container.textContent).not.toContain('4 ready task(s)')
  })

  it('does not paint queue-blind Needs help when Pulse already shows assigned work', () => {
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    act(() => root.render(
      <FleetAgentCard
        agent={{
          id: 'ichi',
          agent_username: 'ichi',
          display_name: 'Ichi',
          status: 'running',
          runtime_harness: 'Shizuha CLI',
          needs_help: true,
          needs_help_reason: '3 consecutive heartbeat(s) exposed no Pulse queue snapshot',
          health_summary: {
            highest_severity: 'attention',
            issues: [{
              type: 'heartbeat_needs_help',
              severity: 'attention',
              service: 'work',
              label: 'Agent needs help',
              detail: '3 consecutive heartbeat(s) exposed no Pulse queue snapshot',
            }],
          },
        }}
        workload={{ open: 2, in_progress: 2, in_review: 1 }}
        teamNames={['devops']}
      />,
    ))
    expect(container.textContent).not.toContain('Agent needs help')
    expect(container.textContent).not.toContain('Needs help')
    expect(container.textContent).not.toContain('no Pulse queue snapshot')
  })

  it('renders Working when fresh productive telemetry supersedes stale Pulse unavailability', () => {
    renderCard({
      id: 'kai',
      agent_username: 'kai',
      display_name: 'Kai',
      status: 'running',
      availability_status: 'unavailable',
      activity_state: 'working',
      health_ok: true,
      provider_unavailable: false,
      health_token_pool_unavailable: false,
      health_summary: {
        last_snapshot_age_s: 20,
        issues: [],
      },
      runtime_harness: 'Codex',
      model_provider: 'OpenAI',
      model: 'gpt-test',
    })

    expect(container.textContent).toContain('Working')
    expect(container.textContent).not.toContain('Unavailable')
    expect(container.textContent).not.toContain('Alive')
  })

  it('animates the live phase instead of a generic Alive badge', () => {
    renderCard({
      id: 'san',
      agent_username: 'san',
      display_name: 'San',
      status: 'running',
      activity_state: 'working',
      activity_phase: 'thinking',
      activity_phase_for_ms: 12_000,
      health_ok: true,
      runtime_harness: 'Shizuha CLI',
      model: 'cortex/DeepSeek-V4-Flash',
    })
    expect(container.textContent).toContain('Thinking')
    expect(container.textContent).toContain('12s')
    expect(container.querySelector('[data-tone="thinking"]')).not.toBeNull()
  })

  it('shows a running tool name on the life signal', () => {
    renderCard({
      id: 'ichi',
      agent_username: 'ichi',
      display_name: 'Ichi',
      status: 'running',
      activity_state: 'working',
      activity_phase: 'tool',
      activity_tool_name: 'bash',
      health_ok: true,
      runtime_harness: 'Codex',
      model: 'gpt-test',
    })
    expect(container.textContent).toMatch(/Tool/i)
    expect(container.textContent).toContain('bash')
  })

  it('flags an idle lease holder as waste', () => {
    renderCard({
      id: 'hana',
      agent_username: 'hana',
      display_name: 'Hana',
      status: 'running',
      activity_state: 'idle',
      activity_phase: 'idle',
      activity_phase_for_ms: 90_000,
      health_ok: true,
      runtime_harness: 'Shizuha CLI',
      model: 'cortex/DeepSeek-V4-Flash',
      model_lease: { managed: true, state: 'holder' },
    })
    expect(container.textContent).toContain('Idle · wasting lease')
    expect(container.querySelector('[data-testid="lease-idle-waste"]')).not.toBeNull()
  })

  it('keeps explicit provider outages Unavailable despite productive telemetry', () => {
    renderCard({
      id: 'nagi',
      agent_username: 'nagi',
      display_name: 'Nagi',
      status: 'running',
      availability_status: 'unavailable',
      activity_state: 'working',
      health_ok: true,
      provider_unavailable: true,
      health_summary: {
        last_snapshot_age_s: 20,
        issues: [],
      },
      runtime_harness: 'Grok Build',
      model_provider: 'Cortex',
      model: 'grok-test',
    })

    expect(container.textContent).toContain('Unavailable')
    expect(container.textContent).not.toContain('Alive')
  })

  it('renders a healthy active runtime as Needs help when the producer episode is explicit', () => {
    renderCard({
      id: 'nagi',
      agent_username: 'nagi',
      display_name: 'Nagi',
      status: 'running',
      needs_help: true,
      needs_help_reason: '10 ready task(s) with no progress',
      heartbeat_outcome: 'needs_help',
      heartbeat_ready_task_count: 10,
      heartbeat_progress_event_count: 0,
      availability_status: 'available',
      activity_state: 'working',
      health_ok: true,
      provider_unavailable: false,
      health_token_pool_unavailable: false,
      health_summary: { last_snapshot_age_s: 10, issues: [] },
      runtime_harness: 'Codex',
      model_provider: 'OpenAI',
      model: 'gpt-test',
    })

    expect(container.textContent).toContain('Needs help')
    expect(container.textContent).not.toContain('Alive')
  })
})


describe('work-stuck chips say WHY inline', () => {
  // Operator 2026-08-05: "i despise all of these needs help issues". The reason
  // string existed in the payload but was hover-only, so the card read as
  // "agent broken" with no explanation — when the actual message is that the
  // TASKS need a routing decision, not that the agent needs a restart.
  it('renders the needs_help detail next to the label', () => {
    renderCard({
      id: 'banto',
      agent_username: 'banto',
      display_name: 'Banto',
      status: 'running',
      runtime_harness: 'Shizuha CLI',
      health_summary: {
        highest_severity: 'attention',
        issues: [{
          type: 'heartbeat_needs_help',
          severity: 'attention',
          service: 'work',
          label: 'Agent needs help',
          detail: '2 ready task(s) with no progress for 38 heartbeat(s)',
        }],
      },
    })
    expect(container.textContent).toContain('Agent needs help')
    expect(container.textContent).toContain('2 ready task(s) with no progress')
  })

  it('error chips keep the detail on hover only', () => {
    renderCard({
      id: 'rei',
      agent_username: 'rei',
      display_name: 'Rei',
      status: 'running',
      runtime_harness: 'Shizuha CLI',
      health_summary: {
        highest_severity: 'error',
        issues: [{
          type: 'runtime_unhealthy',
          severity: 'error',
          service: 'runtime',
          label: 'Runtime unhealthy',
          detail: 'vLLM error 401 (x40)',
        }],
      },
    })
    expect(container.textContent).toContain('Runtime unhealthy')
    expect(container.textContent).not.toContain('vLLM error 401')
  })
})
