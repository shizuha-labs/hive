import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => {
  const clients = []
  const rawGet = vi.fn()
  const rawPost = vi.fn()
  const create = vi.fn(() => {
    const client = {
      requestInterceptor: null,
      interceptors: {
        request: { use: vi.fn((handler) => { client.requestInterceptor = handler }) },
        response: { use: vi.fn() },
      },
      get: vi.fn(),
      post: vi.fn(),
      patch: vi.fn(),
      put: vi.fn(),
      delete: vi.fn(),
    }
    clients.push(client)
    return client
  })
  return { clients, create, rawGet, rawPost }
})

vi.mock('axios', () => ({
  default: {
    create: mocks.create,
    get: mocks.rawGet,
    post: mocks.rawPost,
  },
}))

const { fleetApi } = await import('./api')

function expireCookie(name) {
  document.cookie = `${name}=; Max-Age=0; path=/`
}

describe('Hive API cookie-auth CSRF', () => {
  beforeEach(() => {
    localStorage.clear()
    expireCookie('csrftoken')
    mocks.rawGet.mockReset()
  })

  it('bootstraps Django CSRF and sends X-CSRFToken on unsafe cookie-auth requests', async () => {
    mocks.rawGet.mockImplementation(async () => {
      document.cookie = 'csrftoken=csrf-cookie-value; path=/'
      return { data: { username: 'hritik' } }
    })

    const config = await mocks.clients[0].requestInterceptor({ method: 'post', headers: {} })

    expect(mocks.rawGet).toHaveBeenCalledWith('/hive/api/v1/me/')
    expect(config.headers['X-CSRFToken']).toBe('csrf-cookie-value')
  })

  it('keeps bearer-auth requests independent of cookie CSRF', async () => {
    localStorage.setItem('shizuha_access_token', 'opaque-bearer-token')

    const config = await mocks.clients[0].requestInterceptor({ method: 'post', headers: {} })

    expect(config.headers.Authorization).toBe('Bearer opaque-bearer-token')
    expect(config.headers['X-CSRFToken']).toBeUndefined()
    expect(mocks.rawGet).not.toHaveBeenCalled()
  })
})

describe('fleet card rollout compatibility', () => {
  beforeEach(() => {
    mocks.clients[0].get.mockReset()
  })

  it('falls back to the full list while an old backend replica lacks the card projection', async () => {
    const params = { page_size: 100, page: 1 }
    const legacyResponse = { data: { results: [{ id: 'agent-1' }] } }
    mocks.clients[0].get
      .mockRejectedValueOnce({ response: { status: 404 } })
      .mockResolvedValueOnce(legacyResponse)

    await expect(fleetApi.cards(params)).resolves.toBe(legacyResponse)
    expect(mocks.clients[0].get).toHaveBeenNthCalledWith(1, '/v1/fleet/agent-cards/', { params })
    expect(mocks.clients[0].get).toHaveBeenNthCalledWith(2, '/v1/fleet/agents/', { params })
  })

  it('does not hide genuine card endpoint failures', async () => {
    const failure = { response: { status: 503 } }
    mocks.clients[0].get.mockRejectedValueOnce(failure)

    await expect(fleetApi.cards({ page: 1 })).rejects.toBe(failure)
    expect(mocks.clients[0].get).toHaveBeenCalledTimes(1)
  })
})
