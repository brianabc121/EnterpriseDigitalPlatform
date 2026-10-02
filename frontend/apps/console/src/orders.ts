/** 订单与商品库（设计文档 §25）用到的名称和小工具，与后端 app/modules/orders、products 一致。 */
import type { Schemas } from '@edp/api-client'

export type Order = Schemas['OrderOut']
export type OrderDetail = Schemas['OrderDetail']
export type OrderItem = Schemas['OrderItemOut']
export type OrderEvent = Schemas['OrderEventOut']
export type OrderRevision = Schemas['OrderRevisionOut']
export type Product = Schemas['ProductOut']
export type OrderView =
  | 'all'
  | 'pending_review'
  | 'processing'
  | 'awaiting_shipment'
  | 'out_of_stock'
  | 'receivable'
  | 'modified'
export type PaymentMethod = NonNullable<Schemas['OrderConfirmRequest']['payment_method']>
export type ChangeReason = NonNullable<Schemas['OrderUpdate']['reason']>
type TagType = 'primary' | 'success' | 'info' | 'warning' | 'danger'

export const ORDER_STATUS: Record<string, string> = {
  draft: '草稿',
  pending_review: '待审核',
  confirmed: '已确认',
  fulfilling: '处理中',
  shipped: '已发货',
  completed: '已完成',
  cancelled: '已取消',
}

export const ORDER_STATUS_TAG: Record<string, TagType> = {
  draft: 'info',
  pending_review: 'warning',
  confirmed: 'primary',
  fulfilling: 'primary',
  shipped: 'primary',
  completed: 'success',
  cancelled: 'info',
}

export const ORDER_SOURCE: Record<string, string> = {
  ai_chat: 'AI 接待',
  copilot: '工作台',
  sidebar: '侧边栏',
  staff: '员工新建',
  api: '企业系统',
}

export const PAYMENT_METHODS: [PaymentMethod, string][] = [
  ['online', '在线收款'],
  ['cod', '货到付款'],
  ['deposit', '预付定金'],
  ['credit', '暂欠'],
]

export const PAYMENT_METHOD: Record<string, string> = Object.fromEntries(PAYMENT_METHODS)

export const PAYMENT_STATUS: Record<string, string> = {
  unpaid: '未收款',
  deposit: '已收定金',
  partial: '部分收款',
  paid: '已收清',
  refunded: '已退款',
}

export const PAYMENT_STATUS_TAG: Record<string, TagType> = {
  unpaid: 'info',
  deposit: 'warning',
  partial: 'warning',
  paid: 'success',
  refunded: 'info',
}

export const PAYMENT_CHANNELS: [Schemas['PaymentIn']['channel'], string][] = [
  ['wechat', '微信'],
  ['alipay', '支付宝'],
  ['bank', '银行转账'],
  ['cash', '现金'],
  ['other', '其他'],
]

export const PAYMENT_CHANNEL: Record<string, string> = Object.fromEntries(PAYMENT_CHANNELS)

/** 改价、改商品、改数量时必须选择的原因（设计文档 §25.5）。 */
export const CHANGE_REASONS: [ChangeReason, string][] = [
  ['customer_request', '客户要求'],
  ['ai_error', 'AI 识别错误'],
  ['price_adjust', '价格调整'],
  ['substitution', '缺货替换'],
  ['other', '其他'],
]

export const CHANGE_REASON: Record<string, string> = Object.fromEntries(CHANGE_REASONS)

export const REVISION_KIND: Record<string, string> = {
  created: '创建',
  edit: '修改',
  status: '状态变化',
  payment: '收款',
}

export const ORDER_VIEWS: [OrderView, string][] = [
  ['all', '全部'],
  ['pending_review', '待审核'],
  ['processing', '处理中'],
  ['awaiting_shipment', '待发货'],
  ['out_of_stock', '缺货'],
  ['receivable', '应收'],
  ['modified', '修改过的'],
]

/** 订单中心的视图名称：没有发货环节时"待发货"叫"待交付"。 */
export function viewLabel(view: OrderView, label: string, shipping: boolean): string {
  return view === 'awaiting_shipment' && !shipping ? '待交付' : label
}

/** 商品行的加工进度（设计文档 §25.11）。 */
export const WORK_STATUS: Record<string, string> = {
  pending: '待加工',
  done: '已完成',
  out_of_stock: '缺货',
}

export const WORK_STATUS_TAG: Record<string, TagType> = {
  pending: 'info',
  done: 'success',
  out_of_stock: 'danger',
}

/** 缺货的说明：缺多少、预计到货、备注。 */
export function shortageText(item: {
  quantity: number
  shortage_qty?: number | null
  shortage_note?: string | null
  restock_date?: string | null
}): string {
  const parts = [`缺 ${item.shortage_qty ?? item.quantity}/${item.quantity}`]
  if (item.restock_date) parts.push(`预计 ${item.restock_date} 到货`)
  if (item.shortage_note) parts.push(item.shortage_note)
  return parts.join('，')
}

export const RECEIVER_FIELDS: ['name' | 'phone' | 'address', string][] = [
  ['name', '收货人'],
  ['phone', '联系电话'],
  ['address', '收货地址'],
]

const RECEIVER_LABEL: Record<string, string> = Object.fromEntries(RECEIVER_FIELDS)

/** 金额显示为"¥1,299.00"；为空时显示"—"（待定价）。 */
export function money(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return '—'
  const number = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(number)) return '—'
  return `¥${number.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function cents(value: number): number {
  return Math.round(value * 100) / 100
}

/** 表单里的一行商品。 */
export interface FormLine {
  product_id: string | null
  name: string
  spec: string
  raw_text: string | null
  quantity: number
  list_price: string | null
  unit_price: string | null
}

/** 新建、修改订单时的金额预览：商品金额、优惠后的合计、是否有待定价的商品。 */
export function formTotals(
  lines: readonly Pick<FormLine, 'quantity' | 'unit_price'>[],
  discount: string | number | null,
): { items: number; total: number; pending: boolean } {
  let items = 0
  let pending = false
  for (const line of lines) {
    if (line.unit_price === null || line.unit_price === '') {
      pending = true
      continue
    }
    items += Number(line.unit_price) * line.quantity
  }
  items = cents(items)
  const off = Math.min(Number(discount || 0), items)
  return { items, total: cents(items - off), pending }
}

/** 相对建议零售价的优惠比例（百分比），用来提示是否超过折扣上限。 */
export function discountRate(
  lines: readonly Pick<FormLine, 'quantity' | 'list_price'>[],
  total: number,
): number {
  const listed = lines.reduce(
    (sum, line) => (line.list_price === null ? sum : sum + Number(line.list_price) * line.quantity),
    0,
  )
  if (listed <= 0) return 0
  return Math.max(0, ((listed - total) / listed) * 100)
}

const SCALAR_LABEL: Record<string, string> = {
  status: '状态',
  discount: '优惠',
  total: '合计',
  payment_method: '收款方式',
  deposit_amount: '定金',
  credit_due_date: '约定付款日期',
  expected_at: '期望时间',
  customer_note: '客户要求',
  internal_note: '内部备注',
  payment_status: '收款状态',
  shipping_company: '物流公司',
  tracking_no: '物流单号',
}
const MONEY_FIELDS = new Set(['discount', 'total', 'deposit_amount'])

function scalar(key: string, value: unknown): string {
  if (value === null || value === undefined || value === '') return '空'
  if (key === 'status') return ORDER_STATUS[String(value)] ?? String(value)
  if (key === 'payment_method') return PAYMENT_METHOD[String(value)] ?? String(value)
  if (key === 'payment_status') return PAYMENT_STATUS[String(value)] ?? String(value)
  if (MONEY_FIELDS.has(key)) return money(String(value))
  if (key === 'expected_at') return String(value).slice(0, 16).replace('T', ' ')
  return String(value)
}

interface LineChange {
  name?: string
  spec?: string
  quantity?: number | { from: number; to: number }
  unit_price?: { from: string | null; to: string | null }
}

interface PaymentChange {
  kind?: string
  amount?: string
  channel?: string
}

function item(line: LineChange): string {
  const label = [line.name, line.spec].filter(Boolean).join(' ')
  return typeof line.quantity === 'number' ? `${label} × ${line.quantity}` : label
}

/** 修改记录里一个版本的差异，逐条写成中文（"数量 2 → 3"、"新增 智能门锁 × 1"……）。 */
export function describeChanges(changes: Record<string, unknown>): string[] {
  const lines: string[] = []
  for (const [key, label] of Object.entries(SCALAR_LABEL)) {
    const change = changes[key] as { from?: unknown; to?: unknown } | undefined
    if (change && typeof change === 'object' && 'to' in change) {
      lines.push(`${label}：${scalar(key, change.from)} → ${scalar(key, change.to)}`)
    }
  }
  const items = changes.items as
    | { added?: LineChange[]; removed?: LineChange[]; changed?: LineChange[] }
    | undefined
  if (items) {
    for (const line of items.added ?? []) lines.push(`新增商品：${item(line)}`)
    for (const line of items.removed ?? []) lines.push(`删除商品：${item(line)}`)
    for (const line of items.changed ?? []) {
      const parts: string[] = []
      if (line.quantity && typeof line.quantity === 'object') {
        parts.push(`数量 ${line.quantity.from} → ${line.quantity.to}`)
      }
      if (line.unit_price) {
        parts.push(`单价 ${money(line.unit_price.from)} → ${money(line.unit_price.to)}`)
      }
      lines.push(`${item(line)}：${parts.join('，')}`)
    }
  }
  const receiver = changes.receiver
  if (Array.isArray(receiver) && receiver.length) {
    lines.push(`修改了${receiver.map((k) => RECEIVER_LABEL[String(k)] ?? String(k)).join('、')}`)
  }
  const payments = changes.payments as
    | { added?: PaymentChange[]; voided?: PaymentChange[] }
    | undefined
  if (payments) {
    for (const p of payments.added ?? []) {
      const kind = p.kind === 'refund' ? '登记退款' : '登记收款'
      lines.push(`${kind} ${money(p.amount)}（${PAYMENT_CHANNEL[p.channel ?? ''] ?? p.channel ?? ''}）`)
    }
    for (const p of payments.voided ?? []) lines.push(`作废收款 ${money(p.amount)}`)
  }
  return lines
}

const EVENT: Record<string, string> = {
  created: '创建订单',
  submitted: '提交审核',
  confirmed: '确认订单',
  started: '开始处理',
  shipped: '发货',
  completed: '完成',
  cancelled: '取消订单',
  updated: '修改订单',
  paid: '登记收款',
  refunded: '登记退款',
  payment_voided: '作废收款',
  customer_notified: '通知客户',
  assigned: '转交',
  link_regenerated: '重新生成跟踪链接',
  change_requested: '客户要求修改',
  collection_due: '暂欠到期未收清',
  collection_followup: '应收跟进',
  collection_manual: '发起催收',
  followup_created: '客户没有完成下单，生成跟进待办',
  claimed: '领取加工',
  released: '退回待领取',
  worker_assigned: '指派加工人',
  item_done: '标记完成',
  item_reopened: '撤销完成',
  shortage: '登记缺货',
  restocked: '登记到货',
  processed: '完成加工',
  reprocess: '订单修改后需要重新加工',
  requisition: '开领料单',
  printed: '打印加工单',
  receipt: '开入库单',
  document_confirmed: '仓管确认',
  document_rejected: '仓管退回',
  document_voided: '作废单据',
}

const NOTICE: Record<string, string> = {
  sent: '已发送',
  manual: '待员工发送',
  unreachable: '未能通知',
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

/** 订单动态的一行说明（"谁 做了什么：补充信息"）。names 是员工 ID 到姓名的映射。 */
export function describeOrderEvent(event: OrderEvent, names: Map<string, string>): string {
  const payload = event.payload
  let action = EVENT[event.type] ?? event.type
  let extra = ''
  switch (event.type) {
    case 'created':
      extra = ORDER_SOURCE[text(payload.source)] ?? ''
      break
    case 'confirmed':
      extra = [PAYMENT_METHOD[text(payload.payment_method)], text(payload.note)]
        .filter(Boolean)
        .join('，')
      break
    case 'shipped':
      extra = `${text(payload.shipping_company)} ${text(payload.tracking_no)}`.trim()
      break
    case 'cancelled':
      extra = text(payload.reason)
      break
    case 'updated':
      extra = CHANGE_REASON[text(payload.reason)] ?? ''
      break
    case 'paid':
    case 'refunded':
      extra = `${money(text(payload.amount))}（${PAYMENT_CHANNEL[text(payload.channel)] ?? ''}）`
      break
    case 'payment_voided':
      extra = `${money(text(payload.amount))}：${text(payload.reason)}`
      break
    case 'customer_notified':
      extra = [NOTICE[text(payload.status)], text(payload.reason) || text(payload.text)]
        .filter(Boolean)
        .join('：')
      break
    case 'assigned': {
      const to = text(payload.to)
      extra = to ? `交给 ${names.get(to) ?? '其他员工'}` : '交给技能组待认领'
      break
    }
    case 'change_requested':
      action = payload.kind === 'cancel' ? '客户要求取消' : '客户要求修改'
      extra = text(payload.request)
      break
    case 'collection_due':
    case 'collection_manual':
      extra = `还有 ${money(text(payload.outstanding))} 未收`
      break
    case 'collection_followup': {
      const promise = text(payload.promise_date)
      extra = [promise ? `客户承诺 ${promise} 付款` : '', text(payload.note)]
        .filter(Boolean)
        .join('，')
      break
    }
    case 'worker_assigned':
      extra = text(payload.worker)
      break
    case 'printed': {
      const printers = Array.isArray(payload.printers) ? payload.printers.map(String).join('、') : ''
      extra = [`第 ${String(payload.seq ?? 1)} 次`, printers].filter(Boolean).join('，')
      break
    }
    case 'item_done':
    case 'item_reopened':
    case 'restocked':
      extra = text(payload.name)
      break
    case 'requisition':
    case 'receipt':
      extra = text(payload.no)
      break
    case 'document_confirmed':
    case 'document_rejected':
    case 'document_voided':
      extra = [
        `${payload.kind === 'receipt' ? '入库单' : '领料单'} ${text(payload.no)}`,
        text(payload.reason),
      ]
        .filter(Boolean)
        .join('：')
      break
    case 'shortage':
      action = payload.edited ? '修改缺货' : '登记缺货'
      extra = [
        text(payload.name),
        typeof payload.quantity === 'number' ? `缺 ${payload.quantity}` : '',
        text(payload.restock_date) ? `预计 ${text(payload.restock_date)} 到货` : '',
        text(payload.note),
      ]
        .filter(Boolean)
        .join('，')
      break
  }
  const actor =
    event.actor_type === 'ai'
      ? 'AI'
      : event.actor_type === 'system'
        ? '系统'
        : event.actor_type === 'api'
          ? '企业系统'
          : (event.actor_name ?? '员工')
  return extra ? `${actor} ${action}：${extra}` : `${actor} ${action}`
}

/** 订单有了变化（新建、审核、收款等）时发出的窗口事件：菜单角标据此立即刷新。 */
export const ORDERS_CHANGED = 'edp:orders-changed'

export function ordersChanged(): void {
  window.dispatchEvent(new Event(ORDERS_CHANGED))
}

/** 复制文字到剪贴板（跟踪链接）。 */
export async function copyText(value: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(value)
    return true
  } catch {
    return false
  }
}
