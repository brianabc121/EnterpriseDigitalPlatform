import { describe, expect, it, vi } from 'vitest'

import { createAuthFetch, memoryTokenStore } from './auth-fetch'
import { errorMessage } from './index'

const URL = 'http://localhost/api/v1/me'

function respond(status: number): Response {
  return new Response(JSON.stringify({ status }), { status })
}

describe('createAuthFetch', () => {
  it('attaches the current access token', async () => {
    const tokens = memoryTokenStore()
    tokens.set('t1')
    const send = vi.fn(async (_request: Request) => respond(200))

    await createAuthFetch({ tokens, fetch: send })(new Request(URL))

    expect(send.mock.calls[0]![0].headers.get('Authorization')).toBe('Bearer t1')
  })

  it('refreshes once and retries after a 401', async () => {
    const tokens = memoryTokenStore()
    tokens.set('expired')
    const send = vi.fn(async (request: Request) =>
      respond(request.headers.get('Authorization') === 'Bearer fresh' ? 200 : 401),
    )
    const refresh = vi.fn(async () => 'fresh')

    const response = await createAuthFetch({ tokens, refresh, fetch: send })(new Request(URL))

    expect(response.status).toBe(200)
    expect(refresh).toHaveBeenCalledTimes(1)
    expect(tokens.get()).toBe('fresh')
  })

  it('shares a single refresh between concurrent 401s', async () => {
    const tokens = memoryTokenStore()
    const send = vi.fn(async (request: Request) =>
      respond(request.headers.get('Authorization') === 'Bearer fresh' ? 200 : 401),
    )
    let resolveRefresh: (token: string) => void = () => {}
    const refresh = vi.fn(
      () =>
        new Promise<string>((resolve) => {
          resolveRefresh = resolve
        }),
    )
    const authFetch = createAuthFetch({ tokens, refresh, fetch: send })

    const pending = [authFetch(new Request(URL)), authFetch(new Request(URL))]
    await vi.waitFor(() => expect(refresh).toHaveBeenCalled())
    resolveRefresh('fresh')
    const responses = await Promise.all(pending)

    expect(responses.map((r) => r.status)).toEqual([200, 200])
    expect(refresh).toHaveBeenCalledTimes(1)
  })

  it('clears the token and reports when the session cannot be refreshed', async () => {
    const tokens = memoryTokenStore()
    tokens.set('expired')
    const onUnauthorized = vi.fn()

    const response = await createAuthFetch({
      tokens,
      refresh: async () => null,
      onUnauthorized,
      fetch: async () => respond(401),
    })(new Request(URL))

    expect(response.status).toBe(401)
    expect(tokens.get()).toBeNull()
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
  })

  it('does not refresh when an auth endpoint itself returns 401', async () => {
    const tokens = memoryTokenStore()
    const refresh = vi.fn(async () => 'fresh')
    const onUnauthorized = vi.fn()

    const response = await createAuthFetch({
      tokens,
      refresh,
      onUnauthorized,
      fetch: async () => respond(401),
    })(new Request('http://localhost/api/v1/auth/login', { method: 'POST', body: '{}' }))

    expect(response.status).toBe(401)
    expect(refresh).not.toHaveBeenCalled()
    expect(onUnauthorized).not.toHaveBeenCalled()
  })

  it('replays the request body on retry', async () => {
    const tokens = memoryTokenStore()
    const bodies: string[] = []
    const send = vi.fn(async (request: Request) => {
      bodies.push(await request.text())
      return respond(request.headers.get('Authorization') === 'Bearer fresh' ? 201 : 401)
    })

    const request = new Request(URL, { method: 'POST', body: '{"display_name":"A"}' })
    await createAuthFetch({ tokens, refresh: async () => 'fresh', fetch: send })(request)

    expect(bodies).toEqual(['{"display_name":"A"}', '{"display_name":"A"}'])
  })
})

describe('errorMessage', () => {
  it('reads the backend error message', () => {
    expect(errorMessage({ error: { code: 'conflict', message: '用户名已存在' } })).toBe('用户名已存在')
  })

  it('falls back for unknown shapes', () => {
    expect(errorMessage(undefined, '失败')).toBe('失败')
  })
})
