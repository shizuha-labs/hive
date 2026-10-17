// HIVE-536: this was a plain node:assert script; Vitest discovers *.test.mjs
// and fails with "No test suite found" when a file defines no test()/it().
// Same assertions as before (HIVE-510 regression coverage), as a real suite.
import { describe, it, expect } from 'vitest'
import {
  isNeedsAttention,
  isIdle,
  isRunning,
  ATTENTION_STATUSES,
  IDLE_STATUSES,
  operatorFacingStatusMessage,
} from './agentStatus.js'

describe('agentStatus buckets (HIVE-510)', () => {
  it('treats stopped as a deliberate, healthy state — NOT needs-attention', () => {
    expect(isNeedsAttention('stopped')).toBe(false)
    expect(isNeedsAttention('Stopped')).toBe(false) // case-insensitive
    expect(isIdle('stopped')).toBe(true)
  })

  it('treats hibernated as healthy, idle, and distinct from stopped', () => {
    expect(isNeedsAttention('hibernated')).toBe(false)
    expect(isIdle('hibernated')).toBe(true)
    expect(isRunning('hibernated')).toBe(false)
  })

  it('still flags genuinely problematic states', () => {
    expect(isNeedsAttention('failed')).toBe(true)
    expect(isNeedsAttention('blocked')).toBe(true)
    expect(isNeedsAttention('error')).toBe(true)
  })

  it('classifies not-yet-running states as idle, not attention', () => {
    for (const s of ['pending', 'offline', 'provisioning']) {
      expect(isNeedsAttention(s), `${s} not attention`).toBe(false)
      expect(isIdle(s), `${s} is idle`).toBe(true)
    }
  })

  it('classifies running as neither idle nor attention', () => {
    expect(isRunning('running')).toBe(true)
    expect(isNeedsAttention('running')).toBe(false)
    expect(isIdle('running')).toBe(false)
  })

  it('handles null/empty safely (neither bucket)', () => {
    expect(isNeedsAttention(null)).toBe(false)
    expect(isNeedsAttention('')).toBe(false)
    expect(isIdle(undefined)).toBe(false)
  })

  it('keeps the attention and idle buckets disjoint', () => {
    for (const s of ATTENTION_STATUSES) {
      expect(IDLE_STATUSES.includes(s), `${s} must not be in both buckets`).toBe(false)
    }
  })

  it('hides raw kubectl credential dumps from agent cards', () => {
    const dump = 'k8s reconcile start failed: Command failed: /usr/local/bin/kubectl get -n shizuha-fleet deployment/agent-ichi -o json\nerror: You must be logged in to the server (the server has asked for the client to provide credentials)'
    expect(operatorFacingStatusMessage(dump)).toBe('')
    expect(operatorFacingStatusMessage('attempt to write a readonly database')).toBe(
      'attempt to write a readonly database',
    )
  })

  it('hides runtime-owner park essays from agent cards', () => {
    const park = 'Runtime-owner park: deployment parked 0/0 by hive (PLAT-8181); FleetAgent row descoped from RUNNING to STOPPED to stop the phantom ShizuhaAgentDown (the PLAT-7922 recurrence). Wake path remains available on a model-lease hold.'
    expect(operatorFacingStatusMessage(park)).toBe('')
  })
})
