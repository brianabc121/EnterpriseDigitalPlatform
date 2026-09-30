/** 加工（设计文档 §25.11）用到的名称和小工具，与后端 app/modules/orders/production.py 一致。 */
import type { Schemas } from '@edp/api-client'

export type ProductionOrder = Schemas['ProductionOrder']
export type ProductionItem = Schemas['ProductionItemOut']
export type ProductionView = 'pool' | 'mine' | 'done' | 'all'

/** 加工页的列表："全部加工中"只有能指派加工人的主管看得到。 */
export const PRODUCTION_VIEWS: [ProductionView, string][] = [
  ['pool', '待领取'],
  ['mine', '我的加工'],
  ['done', '已完成'],
  ['all', '全部加工中'],
]

export const EMPTY_TEXT: Record<ProductionView, string> = {
  pool: '暂时没有待领取的订单',
  mine: '没有加工中的订单，可以到"待领取"里领取',
  done: '最近没有加工完成的订单',
  all: '没有加工中的订单',
}

/** 商品的名称和规格，如"智能门锁 X1（黑色）"。 */
export function itemLabel(item: Pick<ProductionItem, 'name' | 'spec'>): string {
  return item.spec ? `${item.name}（${item.spec}）` : item.name
}

/** 进度："已完成 1/3"，有缺货时加上"缺货 1"。 */
export function progressText(order: Pick<ProductionOrder, 'items' | 'done_count'>): string {
  const short = order.items.filter((i) => i.work_status === 'out_of_stock').length
  const text = `已完成 ${order.done_count}/${order.items.length}`
  return short ? `${text}，缺货 ${short}` : text
}

export interface CompletePlan {
  /** 不能完成的原因（有缺货的商品）；可以完成时为 null。 */
  blocked: string | null
  /** 还有没标记的商品：完成时一并标记为已完成。 */
  markAll: boolean
  /** 确认框里的说明。 */
  message: string
}

/** 点"完成订单"时：有缺货的商品不能完成；还有没标记的商品时提示会一并标记完成。 */
export function completePlan(order: Pick<ProductionOrder, 'items'>): CompletePlan {
  const short = order.items.filter((i) => i.work_status === 'out_of_stock')
  if (short.length) {
    return {
      blocked: `有 ${short.length} 个商品缺货，到货后才能完成订单`,
      markAll: false,
      message: '',
    }
  }
  const after = '完成后订单交给客服继续处理。'
  const pending = order.items.filter((i) => i.work_status === 'pending')
  if (!pending.length) return { blocked: null, markAll: false, message: `所有商品都已完成。${after}` }
  const names = pending.map(itemLabel).join('、')
  return {
    blocked: null,
    markAll: true,
    message: `还有 ${pending.length} 个商品没有标记完成（${names}），完成订单会把它们一并标记为已完成。${after}`,
  }
}

/** 客户期望的时间：已经过了（overdue）、24 小时以内（soon）。 */
export function expectedState(
  expectedAt: string | null | undefined,
  now = new Date(),
): 'overdue' | 'soon' | null {
  if (!expectedAt) return null
  const left = new Date(expectedAt).getTime() - now.getTime()
  if (left < 0) return 'overdue'
  return left < 24 * 3600 * 1000 ? 'soon' : null
}

/** 站内信链接里的订单（/production?order=…）打开时去哪个列表：可以领取的在"待领取"，
 * 自己加工中的在"我的加工"，加工完成的在"已完成"，别人加工中的在"全部加工中"。 */
export function viewOf(order: ProductionOrder, me: string | undefined): ProductionView {
  if (order.can_claim) return 'pool'
  if (order.processed_at) return 'done'
  return order.worker_id === me ? 'mine' : 'all'
}
