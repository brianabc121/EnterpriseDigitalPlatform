/** 保存 Access Token 的地方。只放在内存里，不写入 localStorage，降低被 XSS 读取的风险。 */
export interface TokenStore {
  get(): string | null
  set(token: string | null): void
}

export function memoryTokenStore(): TokenStore {
  let token: string | null = null
  return {
    get: () => token,
    set: (value) => {
      token = value
    },
  }
}

export interface AuthFetchOptions {
  tokens: TokenStore
  /** 用刷新令牌换取新的 Access Token；会话已失效时返回 null。不传则不自动刷新。 */
  refresh?: () => Promise<string | null>
  /** 刷新之后仍然 401（会话失效）时调用，通常用于跳转登录页。 */
  onUnauthorized?: () => void
  fetch?: (request: Request) => Promise<Response>
}

/** 登录、刷新、登出本身返回 401 时不应再触发刷新（例如密码错误）。 */
function isAuthEndpoint(request: Request): boolean {
  return new URL(request.url).pathname.includes('/auth/')
}

/**
 * 给请求加上 Bearer Token；遇到 401 时刷新一次令牌并重试。
 * 并发请求同时 401 时只发起一次刷新。
 */
export function createAuthFetch(options: AuthFetchOptions): (request: Request) => Promise<Response> {
  const send = options.fetch ?? ((request: Request) => globalThis.fetch(request))
  let refreshing: Promise<string | null> | null = null

  const refreshOnce = (): Promise<string | null> => {
    if (!options.refresh) return Promise.resolve(null)
    refreshing ??= options.refresh().finally(() => {
      refreshing = null
    })
    return refreshing
  }

  return async (request: Request) => {
    const retry = request.clone()
    const token = options.tokens.get()
    if (token) request.headers.set('Authorization', `Bearer ${token}`)

    const response = await send(request)
    if (response.status !== 401 || isAuthEndpoint(retry)) return response

    const renewed = await refreshOnce()
    if (!renewed) {
      options.tokens.set(null)
      options.onUnauthorized?.()
      return response
    }
    options.tokens.set(renewed)
    retry.headers.set('Authorization', `Bearer ${renewed}`)
    const retried = await send(retry)
    if (retried.status === 401) {
      options.tokens.set(null)
      options.onUnauthorized?.()
    }
    return retried
  }
}
