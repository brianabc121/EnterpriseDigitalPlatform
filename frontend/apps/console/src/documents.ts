/**
 * 开单界面（设计文档 §25.14）的小工具：录入行、批量选择选中的商品，明细合并与合计。
 * 下单、领料单、入库单共用。
 */
import type { Schemas } from '@edp/api-client'

/** 录入行或批量选择选中的一个商品（成品或材料）。 */
export interface PickedItem {
  id: string
  code: string | null
  name: string
  spec: string
  unit: string
  kind: 'goods' | 'material'
  /** 现有库存；为空表示不管理库存。 */
  stock: number | null
  /** 可用库存（成品：现有减去占用；材料：现有减去待领）。 */
  available: number | null
  /** 建议零售价（只有下单时有）。 */
  price: string | null
  /** 数量（批量选择时填的，录入行默认 1）。 */
  quantity: number
}

export function fromProduct(p: Schemas['ProductOut'], quantity = 1): PickedItem {
  return {
    id: p.id,
    code: p.code,
    name: p.name,
    spec: [p.model, p.spec].filter(Boolean).join(' '),
    unit: p.unit,
    kind: p.kind,
    stock: p.stock,
    available: p.stock_available,
    price: p.retail_price,
    quantity,
  }
}

export function fromStockItem(i: Schemas['StockItemOut'], quantity = 1): PickedItem {
  return {
    id: i.id,
    code: i.code,
    name: i.name,
    spec: [i.model, i.spec].filter(Boolean).join(' '),
    unit: i.unit,
    kind: i.kind,
    stock: i.stock,
    available: i.stock_available,
    price: null,
    quantity,
  }
}

/** 数量：成品只能是整数，材料最多三位小数。 */
export function roundQuantity(value: number, kind: 'goods' | 'material'): number {
  if (!Number.isFinite(value)) return 0
  return kind === 'goods' ? Math.round(value) : Math.round(value * 1000) / 1000
}

/**
 * 把选中的商品加到明细里：已经在明细里的不加新行，而是加上数量（扫码两次就是 2 件）。
 * 返回最后一个加入或累加的行号（录入后光标跳到这一行的数量）。
 */
export function mergeInto<T extends { quantity: number }>(
  lines: T[],
  picked: PickedItem[],
  keyOf: (line: T) => string | null,
  create: (item: PickedItem) => T,
): number {
  let last = -1
  for (const item of picked) {
    const index = lines.findIndex((line) => keyOf(line) === item.id)
    if (index >= 0) {
      const line = lines[index]!
      line.quantity = roundQuantity(line.quantity + item.quantity, item.kind)
      last = index
    } else {
      lines.push(create(item))
      last = lines.length - 1
    }
  }
  return last
}

/** 数量合计：单位都相同时是"12.5 米"，单位不同时为空（只显示项数）。 */
export function quantityTotal(lines: readonly { quantity: number; unit: string }[]): string {
  if (!lines.length) return ''
  const units = new Set(lines.map((l) => l.unit))
  if (units.size !== 1) return ''
  const sum = Math.round(lines.reduce((s, l) => s + (Number(l.quantity) || 0), 0) * 1000) / 1000
  const [unit] = units
  return unit ? `${sum} ${unit}` : String(sum)
}

/** 录入行输入的是不是某个商品的代码（扫码枪扫出代码后回车直接加入）。 */
export function exactCode(items: readonly PickedItem[], text: string): PickedItem | null {
  const code = text.trim().toLowerCase()
  if (!code) return null
  return items.find((i) => (i.code ?? '').toLowerCase() === code) ?? null
}

/** 今天的日期："2026-10-01"（新单据的开单日期）。 */
export function today(now: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
}

/**
 * 把光标放到明细某一格（数量、单价）并选中里面的内容，直接输入就是替换（录入后跳到数量时用）。
 * target 是输入框组件（el-input、el-input-number）或者 input 元素。
 */
export function focusField(target: unknown): void {
  const root = (target as { $el?: unknown } | null)?.$el ?? target
  const input =
    root instanceof HTMLInputElement
      ? root
      : root instanceof Element
        ? root.querySelector('input')
        : null
  if (!input) return
  input.focus()
  input.select()
}
