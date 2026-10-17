import React, { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useFleetAgents, useFleetActivity } from './useAgents'

const api = vi.hoisted(() => ({ cards: vi.fn(), activity: vi.fn() }))
vi.mock('../services/api', async (original) => ({ ...await original(), fleetApi: api }))
globalThis.IS_REACT_ACT_ENVIRONMENT = true
let root, container, client, latest
beforeEach(() => {
  api.cards.mockReset(); api.activity.mockReset()
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
})
afterEach(() => { act(() => root.unmount()); client.clear(); container.remove() })
const deferred = () => {
  let resolve
  const promise = new Promise((yes) => { resolve = yes })
  return { resolve, promise }
}
async function settled(assertion) {
  await vi.waitFor(async () => {
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)) })
    assertion()
  })
}
function Fleet({ org, ownerOnly = false }) { latest = useFleetAgents({ orgSlug: org, ownerOnly }); return <span>{latest.data?.[0]?.username || 'loading'}</span> }
function Activity({ id }) { latest = useFleetActivity(id); return <span>{latest.data?.messages?.[0]?.text || 'loading'}</span> }
function show(node) { act(() => root.render(<QueryClientProvider client={client}>{node}</QueryClientProvider>)) }
const cards = (name) => ({ data: { results: [{ id: name, username: name }], next: null } })
const activity = (text) => ({ data: { messages: [{ text }] } })

describe('actual fleet metric query scope boundaries', () => {
  it('does not show one org under another org, ignores late results, and reuses exact cached org', async () => {
    const second = deferred(), revisit = deferred()
    api.cards.mockResolvedValueOnce(cards('first')).mockReturnValueOnce(second.promise).mockReturnValueOnce(revisit.promise)
    show(<Fleet org="first" />)
    await settled(() => expect(latest.data?.[0]?.username).toBe('first'))
    show(<Fleet org="second" />)
    expect(latest.data).toBeUndefined()
    expect(container.textContent).toBe('loading')
    show(<Fleet org="first" />)
    expect(latest.data?.[0]?.username).toBe('first')
    await act(async () => second.resolve(cards('second')))
    expect(latest.data?.[0]?.username).toBe('first')
    await act(async () => revisit.resolve(cards('first-updated')))
    await settled(() => expect(latest.data?.[0]?.username).toBe('first-updated'))
  })

  it('does not use broad fleet data as placeholder while switching to owner-only visibility', async () => {
    api.cards.mockResolvedValueOnce(cards('org-member')).mockReturnValueOnce(new Promise(() => {}))
    show(<Fleet org="same" />)
    await settled(() => expect(latest.data?.[0]?.username).toBe('org-member'))
    show(<Fleet org="same" ownerOnly />)
    expect(latest.data).toBeUndefined()
    expect(api.cards.mock.calls[1][0].owner_only).toBe(1)
  })

  it('never shows another agent transcript while selected agent activity loads', async () => {
    api.activity.mockResolvedValueOnce(activity('first transcript')).mockReturnValueOnce(new Promise(() => {}))
    show(<Activity id="first" />)
    await settled(() => expect(latest.data?.messages?.[0]?.text).toBe('first transcript'))
    show(<Activity id="second" />)
    expect(latest.data).toBeUndefined()
    expect(container.textContent).toBe('loading')
  })

  for (const [name, method, node, valid] of [
    ['fleet', api.cards, <Fleet key="fleet" org="same" />, cards('cached')],
    ['activity', api.activity, <Activity key="activity" id="same" />, activity('cached')],
  ]) {
    it(`${name} retains same-scope data during a slow refresh`, async () => {
      const pending = deferred()
      method.mockResolvedValueOnce(valid).mockReturnValueOnce(pending.promise)
      show(node)
      await settled(() => expect(latest.isSuccess).toBe(true))
      const before = latest.data
      act(() => { latest.refetch() })
      expect(latest.data).toBe(before)
      await act(async () => pending.resolve(valid))
    })
    it.each([401, 403])(`${name} hides a retained snapshot after explicit HTTP %s denial`, async (status) => {
      method.mockResolvedValueOnce(valid).mockRejectedValueOnce({ response: { status } })
      show(node)
      await settled(() => expect(latest.isSuccess).toBe(true))
      await act(() => latest.refetch())
      await settled(() => expect(latest.isError).toBe(true))
      expect(latest.data).toBeUndefined()
    })
  }
})
