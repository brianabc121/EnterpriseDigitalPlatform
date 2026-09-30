import { createStaffApi, memoryTokenStore } from '@edp/api-client'

export const apiBase = import.meta.env.VITE_API_BASE ?? ''
// 部署时的运行时配置：容器启动时按环境变量生成 /config.js（见 frontend/deploy）。
const runtime = (globalThis as { EDP_CONFIG?: { widgetUrl?: string } }).EDP_CONFIG ?? {}
/** 访客 Widget 的地址，用于"打开访客测试页"和嵌入代码。 */
export const widgetBase =
  runtime.widgetUrl || import.meta.env.VITE_WIDGET_URL || 'http://localhost:5175'
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
