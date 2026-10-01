import { createPlatformApi, createTransport, memoryTokenStore, transportKey } from '@edp/api-client'

export const apiBase = import.meta.env.VITE_API_BASE ?? ''
export const tokens = memoryTokenStore()
/** 接口传输加密（设计文档 §25.15）：接口客户端和刷新令牌共用一个加密会话。 */
export const transport = createTransport({
  baseUrl: apiBase,
  publicKey: transportKey(import.meta.env.VITE_TRANSPORT_PUBLIC_KEY),
})

let unauthorizedHandler: () => void = () => {}

export function onUnauthorized(handler: () => void): void {
  unauthorizedHandler = handler
}

// Access Token 只保存在内存中；刷新页面后用 httpOnly Cookie 里的刷新令牌恢复登录（stores/auth 的 restore）。
export const api = createPlatformApi({
  baseUrl: apiBase,
  tokens,
  transport,
  onUnauthorized: () => unauthorizedHandler(),
})

export function formatDateTime(value: string): string {
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}
