import { createStaffApi, memoryTokenStore } from '@edp/api-client'

export const apiBase = import.meta.env.VITE_API_BASE ?? ''
/** 访客 Widget 的地址，用于"打开访客测试页"。 */
export const widgetBase = import.meta.env.VITE_WIDGET_URL ?? 'http://localhost:5175'
export const tokens = memoryTokenStore()

let unauthorizedHandler: () => void = () => {}

/** 会话失效时的处理（由路由注册：清空登录状态并跳转登录页）。 */
export function onUnauthorized(handler: () => void): void {
  unauthorizedHandler = handler
}

export const api = createStaffApi({
  baseUrl: apiBase,
  tokens,
  onUnauthorized: () => unauthorizedHandler(),
})

export function formatDateTime(value: string): string {
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}
