import createClient, { type Client } from 'openapi-fetch'

import { createAuthFetch, type TokenStore } from './auth-fetch'
import type { components, paths } from './schema'

export { createAuthFetch, memoryTokenStore, type AuthFetchOptions, type TokenStore } from './auth-fetch'
export type { components, paths }

export type Schemas = components['schemas']
export type Permission = Schemas['Permission']
export type ApiClient = Client<paths>

interface ClientOptions {
  /** 后端地址；留空表示与页面同源（开发环境由 Vite 代理转发）。 */
  baseUrl?: string
  tokens: TokenStore
  onUnauthorized?: () => void
}

/** 用 httpOnly Cookie 中的刷新令牌换取新的 Access Token。会话失效时返回 null。 */
export async function refreshAccessToken(baseUrl = ''): Promise<string | null> {
  try {
    const response = await fetch(`${baseUrl}/api/v1/auth/refresh`, {
      method: 'POST',
      credentials: 'include',
    })
    if (!response.ok) return null
    const body = (await response.json()) as Schemas['TokenResponse']
    return body.access_token
  } catch {
    return null
  }
}

/** 租户员工使用的客户端：自动携带令牌，过期时自动刷新。 */
export function createStaffApi(options: ClientOptions): ApiClient {
  const baseUrl = options.baseUrl ?? ''
  return createClient<paths>({
    baseUrl,
    credentials: 'include',
    fetch: createAuthFetch({
      tokens: options.tokens,
      refresh: () => refreshAccessToken(baseUrl),
      onUnauthorized: options.onUnauthorized,
    }),
  })
}

/** 平台运营使用的客户端。P0 不做令牌刷新，过期后重新登录。 */
export function createPlatformApi(options: ClientOptions): ApiClient {
  return createClient<paths>({
    baseUrl: options.baseUrl ?? '',
    fetch: createAuthFetch({ tokens: options.tokens, onUnauthorized: options.onUnauthorized }),
  })
}

/** 访客 Widget 使用的客户端：不需要登录，访客身份由请求体中的访客令牌表示。 */
export function createVisitorApi(baseUrl = ''): ApiClient {
  return createClient<paths>({ baseUrl })
}

/** 从后端统一错误结构 {"error": {"message": ...}} 中取出提示文案。 */
export function errorMessage(error: unknown, fallback = '请求失败，请稍后重试'): string {
  if (typeof error === 'object' && error !== null && 'error' in error) {
    const detail = (error as { error?: { message?: unknown } }).error
    if (detail && typeof detail.message === 'string') return detail.message
  }
  return fallback
}
