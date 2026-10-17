import { describe, expect, it } from 'vitest'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import {
  accountReauthRoute,
  accountReauthUrl,
  availableResetCount,
  canRedeemReset,
  CORTEX_DASHBOARD_URL,
  credentialHealthEmptyMessage,
  fmtDateTime,
  liveLeaseCount,
  SubscriptionPlan,
} from './CredentialHealth'

describe('SubscriptionPlan', () => {
  function renderPlan(provider, quota) {
    const element = document.createElement('div')
    element.innerHTML = renderToStaticMarkup(createElement(SubscriptionPlan, { provider, quota }))
    return element
  }

  it.each(['Free', 'SuperGrok', 'SuperGrok Heavy'])('shows the provider-reported %s plan even without quota windows', (plan) => {
    const element = renderPlan('xai', { plan, windows: [] })
    expect(element.textContent).toBe(`Plan${plan}`)
  })

  it.each([undefined, { plan: null }, { plan: '' }, { plan: '   ' }])('shows Unknown when xAI has no reported plan', (quota) => {
    const element = renderPlan('xai', quota)
    expect(element.textContent).toBe('PlanUnknown')
    expect(element.querySelector('[title]').title).toContain('Quota remaining does not identify the plan')
  })

  it.each([0, 100])('does not infer a free or paid plan from %s%% credits remaining', (left) => {
    expect(renderPlan('xai', { windows: [{ label: 'Credits', left_percent: left }] }).textContent).toBe('PlanUnknown')
  })

  it('preserves known plans for other providers without adding unknown badges to them', () => {
    expect(renderPlan('openai', { plan: 'pro' }).textContent).toBe('Planpro')
    expect(renderPlan('anthropic', {}).textContent).toBe('')
  })
})

describe('CORTEX_DASHBOARD_URL', () => {
  it('points at the canonical cortex host, not the shizuha.com path alias (CTX-523)', () => {
    expect(CORTEX_DASHBOARD_URL).toBe('https://cortex.shizuha.com/cortex/')
    expect(CORTEX_DASHBOARD_URL.startsWith('https://cortex.shizuha.com/')).toBe(true)
    expect(CORTEX_DASHBOARD_URL.includes('shizuha.com/cortex') && !CORTEX_DASHBOARD_URL.includes('cortex.shizuha.com')).toBe(false)
  })
})

describe('availableResetCount', () => {
  it('reads the credential-health API quota field', () => {
    expect(availableResetCount({ quota: { reset_credits: { available: 1 } } })).toBe(1)
  })

  it('does not infer a reset when quota data is missing', () => {
    expect(availableResetCount({})).toBe(0)
  })
})

describe('canRedeemReset', () => {
  it('is Codex/OpenAI only — Grok pending counts are display-only', () => {
    const quota = { reset_credits: { available: 1 } }
    expect(canRedeemReset({ provider: 'openai', quota })).toBe(true)
    expect(canRedeemReset({ provider: 'xai', quota })).toBe(false)
    expect(canRedeemReset({ provider: 'openai', quota: { reset_credits: { available: 0 } } })).toBe(false)
  })
})

describe('liveLeaseCount', () => {
  it('uses the active broker lease count instead of the legacy usage counter', () => {
    expect(liveLeaseCount({ live_leases: 4, usage_count: 0 })).toBe(4)
  })

  it('defaults to zero when lease telemetry is absent', () => {
    expect(liveLeaseCount({ usage_count: 9 })).toBe(0)
  })
})

describe('credentialHealthEmptyMessage', () => {
  it('does not direct non-staff users to admin-only Org / Mine controls', () => {
    expect(credentialHealthEmptyMessage(false)).toBe('Add tokens under Token Pools.')
    expect(credentialHealthEmptyMessage(false)).not.toMatch(/Org|Mine/)
  })

  it('keeps scope-switching guidance for staff who can use the controls', () => {
    expect(credentialHealthEmptyMessage(true)).toBe('Add tokens under Token Pools, or switch Org / Mine.')
  })
})

describe('accountReauthUrl', () => {
  it('returns the exact backend-derived OpenAI entry URL', () => {
    const url = '/hive/credentials/codex/re-auth?scope=user&pool=pool-1&entry=entry-1'
    expect(accountReauthUrl({ provider: 'openai', codex_reauth_url: url })).toBe(url)
  })

  it('does not expose a re-auth action for other providers or arbitrary URLs', () => {
    expect(accountReauthUrl({ provider: 'anthropic', codex_reauth_url: '/hive/credentials/codex/re-auth?entry=1' })).toBe('')
    expect(accountReauthUrl({ provider: 'openai', codex_reauth_url: 'https://example.com/' })).toBe('')
  })

  it('converts the public /hive URL into a basename-safe client route', () => {
    expect(accountReauthRoute({
      provider: 'openai',
      codex_reauth_url: '/hive/credentials/codex/re-auth?scope=user&entry=entry-1',
    })).toBe('/credentials/codex/re-auth?scope=user&entry=entry-1')
  })
})

describe('fmtDateTime UTC contract (HIVE-1325)', () => {
  it('renders explicit UTC (+00:00 / Z) as the same local instant', () => {
    // Live bug pair was HTTP Date 16:08:31 GMT vs naive 11:08:31 (Chicago wall).
    const utc = '2026-07-22T16:08:31.370586+00:00'
    const zulu = '2026-07-22T16:08:31.370586Z'
    expect(Date.parse(utc)).toBe(Date.parse(zulu))
    const expected = new Date(utc).toLocaleString()
    expect(fmtDateTime(utc)).toBe(expected)
    expect(fmtDateTime(zulu)).toBe(expected)
  })

  it('returns an em-dash for missing values', () => {
    expect(fmtDateTime(null)).toBe('—')
    expect(fmtDateTime('')).toBe('—')
  })
})
