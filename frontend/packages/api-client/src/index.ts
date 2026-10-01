import createClient, { type Client } from 'openapi-fetch'

import { createAuthFetch, type TokenStore } from './auth-fetch'
import { createTransport } from './transport'
import type { components, paths } from './schema'

export { createAuthFetch, memoryTokenStore, type AuthFetchOptions, type TokenStore } from './auth-fetch'
export type { components, paths }
export { formatUsage, totalHint } from './usage'
export { createTransport, SEALED_TYPE, transportKey, type TransportOptions } from './transport'
export {
  formatLimit,
  formatMoney,
  INVOICE_STATUS,
  SUBSCRIPTION_STATUS,
  usagePercent,
} from './billing'

export type Schemas = components['schemas']
export type Permission = Schemas['Permission']
/** 按岗位的控制台（§25.15）：菜单名和岗位，与后端 app/core/consoles.py 一致。 */
export type ConsoleMenu = Schemas['ConsoleMenu']
export type ConsoleProfile = Schemas['ConsoleProfile']
export type ApiClient = Client<paths>

interface ClientOptions {
  /** 后端地址；留空表示与页面同源（开发环境由 Vite 代理转发）。 */
  baseUrl?: string
  tokens: TokenStore
  onUnauthorized?: () => void
  /** 传输加密的服务器公钥（设计文档 §25.15）；为空时从后端获取。 */
  transportKey?: string | null
  /** 已经创建好的传输加密（与刷新令牌等共用一个加密会话）；不传时按 transportKey 创建。 */
  transport?: Send
}

type Send = (request: Request) => Promise<Response>

/** 用 httpOnly Cookie 中的刷新令牌换取新的 Access Token。会话失效时返回 null。 */
export async function refreshAccessToken(
  baseUrl = '',
  send: Send = (request) => globalThis.fetch(request),
): Promise<string | null> {
  try {
    const response = await send(
      new Request(`${baseUrl}/api/v1/auth/refresh`, { method: 'POST', credentials: 'include' }),
    )
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
  const secure = options.transport ?? createTransport({ baseUrl, publicKey: options.transportKey })
  return createClient<paths>({
    baseUrl,
    credentials: 'include',
    fetch: createAuthFetch({
      tokens: options.tokens,
      refresh: () => refreshAccessToken(baseUrl, secure),
      onUnauthorized: options.onUnauthorized,
      fetch: secure,
    }),
  })
}

/** 运营后台：用 httpOnly Cookie 中的刷新令牌换取新的 Access Token。会话失效时返回 null。 */
export async function refreshPlatformToken(
  baseUrl = '',
  send: Send = (request) => globalThis.fetch(request),
): Promise<string | null> {
  try {
    const response = await send(
      new Request(`${baseUrl}/platform/v1/auth/refresh`, { method: 'POST', credentials: 'include' }),
    )
    if (!response.ok) return null
    const body = (await response.json()) as Schemas['PlatformTokenResponse']
    return body.access_token
  } catch {
    return null
  }
}

/** 平台运营使用的客户端：自动携带令牌，过期时用刷新令牌 Cookie 自动刷新。 */
export function createPlatformApi(options: ClientOptions): ApiClient {
  const baseUrl = options.baseUrl ?? ''
  const secure = options.transport ?? createTransport({ baseUrl, publicKey: options.transportKey })
  return createClient<paths>({
    baseUrl,
    credentials: 'include',
    fetch: createAuthFetch({
      tokens: options.tokens,
      refresh: () => refreshPlatformToken(baseUrl, secure),
      onUnauthorized: options.onUnauthorized,
      fetch: secure,
    }),
  })
}

/** 访客 Widget 使用的客户端：不需要登录，访客身份由请求体中的访客令牌表示。 */
export function createVisitorApi(baseUrl = '', transportKey: string | null = null): ApiClient {
  return createClient<paths>({ baseUrl, fetch: createTransport({ baseUrl, publicKey: transportKey }) })
}

/** 后端统一错误结构里的错误码（如 plan_limit、mfa_required）。 */
export function errorCode(error: unknown): string | null {
  if (typeof error === 'object' && error !== null && 'error' in error) {
    const detail = (error as { error?: { code?: unknown } }).error
    if (detail && typeof detail.code === 'string') return detail.code
  }
  return null
}

/** 从后端统一错误结构 {"error": {"message": ...}} 中取出提示文案。 */
export function errorMessage(error: unknown, fallback = '请求失败，请稍后重试'): string {
  if (typeof error === 'object' && error !== null && 'error' in error) {
    const detail = (error as { error?: { message?: unknown } }).error
    if (detail && typeof detail.message === 'string') return detail.message
  }
  return fallback
}
