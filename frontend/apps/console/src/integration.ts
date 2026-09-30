/** 企业系统对接（设计文档 §25.8）：接口密钥的权限范围、推送事件和推送状态的名称。 */
import type { Schemas } from '@edp/api-client'

export type ApiKey = Schemas['ApiKeyOut']
export type Endpoint = Schemas['WebhookEndpointOut']
export type Delivery = Schemas['WebhookDeliveryOut']
export type Scope = Schemas['ApiKeyCreate']['scopes'][number]
export type EventName = Schemas['WebhookEndpointWrite']['events'][number]

export const SCOPES: [Scope, string][] = [
  ['products:write', '同步商品和价格'],
  ['orders:read', '查询订单（含收货信息）'],
  ['orders:write', '创建订单，回传状态、物流和收款'],
  ['todos:write', '创建待办'],
]

export const SCOPE: Record<string, string> = Object.fromEntries(SCOPES)

export const EVENTS: [EventName, string][] = [
  ['order.created', '订单提交审核或创建'],
  ['order.updated', '订单内容修改'],
  ['order.confirmed', '订单确认'],
  ['order.status_changed', '开始处理、发货、完成'],
  ['order.cancelled', '订单取消'],
  ['order.payment', '收款、退款'],
  ['todo.done', '待办完成'],
]

export const EVENT: Record<string, string> = { ...Object.fromEntries(EVENTS), ping: '测试推送' }

export type DeliveryState = 'pending' | 'retrying' | 'succeeded' | 'dead'

export const DELIVERY_STATE: Record<DeliveryState, [string, 'info' | 'warning' | 'success' | 'danger']> = {
  pending: ['等待推送', 'info'],
  retrying: ['重试中', 'warning'],
  succeeded: ['成功', 'success'],
  dead: ['失败（已停止重试）', 'danger'],
}

/** 推送记录的状态：失败后等待重试的显示为"重试中"。 */
export function deliveryState(row: Pick<Delivery, 'status' | 'attempts'>): DeliveryState {
  return row.status === 'pending' && row.attempts > 0 ? 'retrying' : row.status
}

/** 推送内容排版成易读的 JSON（不是 JSON 时原样返回）。 */
export function prettyJson(text: string | null | undefined): string {
  if (!text) return ''
  try {
    return JSON.stringify(JSON.parse(text), null, 2)
  } catch {
    return text
  }
}
