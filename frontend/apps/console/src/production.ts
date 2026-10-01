/** 加工（设计文档 §25.11、§25.13）用到的名称和小工具，与后端 app/modules/orders/production.py
 * 一致。 */
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

/** 需要加工的商品（现货直接从成品库存发货，不需要加工）。 */
export function madeItems(order: Pick<ProductionOrder, 'items'>): ProductionItem[] {
  return order.items.filter((i) => !i.ready_made)
}

/** 进度："已完成 1/3"（只算需要加工的商品），有缺货时加上"缺货 1"。 */
export function progressText(order: Pick<ProductionOrder, 'items' | 'done_count'>): string {
  const short = order.items.filter((i) => i.work_status === 'out_of_stock').length
  const text = `已完成 ${order.done_count}/${madeItems(order).length}`
  return short ? `${text}，缺货 ${short}` : text
}

/** 还没开领料单、不能开始加工（商品有配方时要先按配方领料）。 */
export function needsRequisition(
  order: Pick<ProductionOrder, 'requisition_required' | 'requisition_ready'>,
): boolean {
  return order.requisition_required && !order.requisition_ready
}

type RequisitionState = Pick<
  ProductionOrder,
  | 'documents'
  | 'requisition_required'
  | 'requisition_ready'
  | 'requisition_estimated'
  | 'requisition_todo'
  | 'requisition_rejected'
>

/**
 * 订单卡片上领料的按钮（设计文档 §25.17）：被退回的领料单 → "修改领料单"；开过领料单 → "补领材料"
 * （订单改了数量、按配方还有没领的时候是主按钮）；还没开 → "开领料单"（有配方或者能按以往领料估算
 * 时是主按钮）。
 */
export function requisitionAction(order: RequisitionState): {
  action: 'open' | 'fix'
  label: string
  primary: boolean
} {
  if (order.requisition_rejected) return { action: 'fix', label: '修改领料单', primary: true }
  if (order.documents.some((d) => d.kind === 'requisition')) {
    return { action: 'open', label: '补领材料', primary: order.requisition_todo.length > 0 }
  }
  return {
    action: 'open',
    label: '开领料单',
    primary: needsRequisition(order) || order.requisition_estimated,
  }
}

/** 领取订单后自动打开领料单：有配方，或者能按以往领料估算（有可以自动填的内容）。 */
export function opensRequisition(
  order: Pick<ProductionOrder, 'requisition_required' | 'requisition_estimated'>,
): boolean {
  return order.requisition_required || order.requisition_estimated
}

export interface CompletePlan {
  /** 不能完成的原因（有缺货的商品、还没开领料单）；可以完成时为 null。 */
  blocked: string | null
  /** 还有没标记的商品：完成时一并标记为已完成。 */
  markAll: boolean
  /** 要开入库单（生产好的成品由仓管确认入库）。 */
  receipt: boolean
  /** 确认框（或入库单）里的说明。 */
  message: string
}

type Completable = Pick<
  ProductionOrder,
  'items' | 'needs_receipt' | 'requisition_required' | 'requisition_ready'
>

/** 点"完成加工"时：有缺货的商品、还没开领料单时不能完成；还有没标记的商品时提示会一并标记
 * 完成；有要入库的成品时开入库单。 */
export function completePlan(order: Completable): CompletePlan {
  const short = order.items.filter((i) => i.work_status === 'out_of_stock')
  const receipt = order.needs_receipt
  if (short.length) {
    return {
      blocked: `有 ${short.length} 个商品缺货，到货后才能完成订单`,
      markAll: false,
      receipt,
      message: '',
    }
  }
  if (needsRequisition(order)) {
    return { blocked: '请先开领料单', markAll: false, receipt, message: '' }
  }
  const after = receipt
    ? '完成后开入库单，仓管确认入库后订单交给客服发货。'
    : '完成后订单交给客服继续处理。'
  const pending = madeItems(order).filter((i) => i.work_status === 'pending')
  if (!pending.length) {
    return { blocked: null, markAll: false, receipt, message: `所有商品都已完成。${after}` }
  }
  const names = pending.map(itemLabel).join('、')
  return {
    blocked: null,
    markAll: true,
    receipt,
    message: `还有 ${pending.length} 个商品没有标记完成（${names}），完成加工会把它们一并标记为已完成。${after}`,
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
