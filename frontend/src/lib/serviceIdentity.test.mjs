import { describe, expect, it } from 'vitest'
import { isSharedServiceIdentity, KNOWN_SERVICE_IDENTITIES } from './serviceIdentity.js'

describe('HIVE-412 isSharedServiceIdentity', () => {
  it('flags a known service account even when only one card resolves to it', () => {
    // The security-ci flood belonged to codex@shizuha.com (user 4). Even a single
    // card mapping to it must not render the pile as personal workload.
    const counts = new Map([['codex@shizuha.com', 1]])
    expect(isSharedServiceIdentity('codex@shizuha.com', counts)).toBe(true)
  })

  it('flags an identity that backs more than one agent card', () => {
    const counts = new Map([['bridge@shizuha.com', 3]])
    expect(isSharedServiceIdentity('bridge@shizuha.com', counts)).toBe(true)
  })

  it('does NOT flag a normal personal identity backing a single card', () => {
    const counts = new Map([['sara@shizuha.com', 1]])
    expect(isSharedServiceIdentity('sara@shizuha.com', counts)).toBe(false)
  })

  it('is case-insensitive for known service accounts', () => {
    expect(isSharedServiceIdentity('Codex@Shizuha.com', new Map())).toBe(true)
  })

  it('returns false for an empty/missing email', () => {
    expect(isSharedServiceIdentity('', new Map())).toBe(false)
    expect(isSharedServiceIdentity(undefined, new Map())).toBe(false)
  })

  it('tolerates a missing counts map', () => {
    expect(isSharedServiceIdentity('sara@shizuha.com')).toBe(false)
    expect(isSharedServiceIdentity('codex@shizuha.com')).toBe(true)
  })

  it('exposes codex@shizuha.com as a known service identity', () => {
    expect(KNOWN_SERVICE_IDENTITIES.has('codex@shizuha.com')).toBe(true)
  })
})
