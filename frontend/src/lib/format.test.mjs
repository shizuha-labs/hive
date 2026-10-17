import { afterEach, describe, expect, it, vi } from 'vitest'
import { relativeTime } from './format.js'

describe('relativeTime UTC contract', () => {
  afterEach(() => vi.useRealTimers())

  it('treats backend naive lifecycle timestamps as UTC', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-07-15T02:08:38Z'))
    expect(relativeTime('2026-07-15T02:07:38')).toBe('1 minute ago')
  })

  it('preserves an explicit timestamp offset', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-07-15T02:08:38Z'))
    expect(relativeTime('2026-07-15T07:37:38+05:30')).toBe('1 minute ago')
  })
})
