import { createPlatformApi, memoryTokenStore } from '@edp/api-client'

export const tokens = memoryTokenStore()

let unauthorizedHandler: () => void = () => {}

export function onUnauthorized(handler: () => void): void {
  unauthorizedHandler = handler
}

// 运营令牌只保存在内存中：刷新页面后需要重新登录（P1 再补充刷新机制）。
export const api = createPlatformApi({
  baseUrl: import.meta.env.VITE_API_BASE ?? '',
  tokens,
  onUnauthorized: () => unauthorizedHandler(),
})

export function formatDateTime(value: string): string {
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}
