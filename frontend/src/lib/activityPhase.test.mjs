import { describe, expect, it } from 'vitest'
import {
  activityLabel,
  formatPhaseDuration,
  isLeaseIdleWaste,
  isProductiveActivity,
  resolveActivityPhase,
} from './activityPhase.js'

describe('resolveActivityPhase', () => {
  it('prefers the fine phase over the coarse working latch', () => {
    expect(resolveActivityPhase({ activity_state: 'working', activity_phase: 'thinking' })).toBe('thinking')
    expect(resolveActivityPhase({ activity_state: 'working', activity_phase: 'responding' })).toBe('responding')
    expect(resolveActivityPhase({ activity_state: 'working', activity_phase: 'tool' })).toBe('tool')
  })

  it('maps older idle/working telemetry and Codex wait-for-first-event', () => {
    expect(resolveActivityPhase({ activity_state: 'idle' })).toBe('idle')
    expect(resolveActivityPhase({ activity_state: 'working' })).toBe('working')
    expect(resolveActivityPhase({ activity_phase: 'waiting_first_model_event' })).toBe('thinking')
  })
})

describe('lease idle waste', () => {
  const holder = {
    status: 'running',
    activity_state: 'idle',
    activity_phase: 'idle',
    activity_phase_for_ms: 45_000,
    model_lease: { state: 'holder', managed: true },
  }

  it('flags a running lease holder sitting idle', () => {
    expect(isLeaseIdleWaste(holder)).toBe(true)
    expect(activityLabel('idle', { leaseIdleWaste: true })).toBe('Idle · wasting lease')
  })

  it('does not flag brief idle, non-holders, or productive phases', () => {
    expect(isLeaseIdleWaste({ ...holder, activity_phase_for_ms: 5_000 })).toBe(false)
    expect(isLeaseIdleWaste({ ...holder, model_lease: { state: 'waiting' } })).toBe(false)
    expect(isLeaseIdleWaste({ ...holder, activity_phase: 'thinking', activity_state: 'working' })).toBe(false)
  })
})

describe('productive activity', () => {
  it('treats the four live phases as work', () => {
    expect(isProductiveActivity({ activity_phase: 'thinking' })).toBe(true)
    expect(isProductiveActivity({ activity_phase: 'responding' })).toBe(true)
    expect(isProductiveActivity({ activity_phase: 'tool' })).toBe(true)
    expect(isProductiveActivity({ activity_state: 'working' })).toBe(true)
    expect(isProductiveActivity({ activity_state: 'idle' })).toBe(false)
  })
})

describe('formatPhaseDuration', () => {
  it('hides sub-second and compact-prints longer spans', () => {
    expect(formatPhaseDuration(400)).toBe('')
    expect(formatPhaseDuration(12_000)).toBe('12s')
    expect(formatPhaseDuration(65_000)).toBe('1m 5s')
  })
})
