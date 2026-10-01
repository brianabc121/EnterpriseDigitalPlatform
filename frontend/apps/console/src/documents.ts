/**
 * 开单界面（设计文档 §25.14）的小工具：录入行、批量选择选中的商品，明细合并与合计。
 * 下单、领料单、入库单共用。
 */
import type { Schemas } from '@edp/api-client'

import { qty, qtyUnit } from './warehouse'

/** 录入行或批量选择选中的一个商品（成品或材料）。 */
export interface PickedItem {
  id: string
  code: string | null
  name: string
  spec: string
  /** 分类（品目），多级用 / 分隔。 */
  category: string
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
    category: p.category,
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
    category: i.category,
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

/** 联想（设计文档 §25.16）：按哪个字段、怎么找到的。 */
export type SuggestField = Schemas['ProductSuggestion']['field']
export type SuggestMatch = Schemas['ProductSuggestion']['match']

/** 录入行下拉里的一个候选。 */
export interface Suggestion {
  item: PickedItem
  field: SuggestField
  match: SuggestMatch
}

export function fromProductSuggestion(s: Schemas['ProductSuggestion']): Suggestion {
  return { item: fromProduct(s.product), field: s.field, match: s.match }
}

export function fromStockSuggestion(s: Schemas['StockSuggestion']): Suggestion {
  return { item: fromStockItem(s.item), field: s.field, match: s.match }
}

const FIELD_LABELS: Record<NonNullable<SuggestField>, string | null> = {
  code: '代码',
  model: '型号',
  name: null,
  alias: '俗称',
  spec: '规格',
  category: '分类',
  pinyin: '拼音',
}

/** 候选旁边的小标签：按什么找到的（按名称找到的、最近用过的不标）。 */
export function matchLabel(s: Pick<Suggestion, 'field' | 'match'>): string | null {
  if (s.match === 'recent') return null
  if (s.match === 'similar') return '相近'
  return s.field ? FIELD_LABELS[s.field] : null
}

/** 下拉的标题：没有输入时是最近用过的；只有相近的结果时说明没有完全匹配。 */
export function suggestionTitle(list: readonly Suggestion[], recent: boolean): string | null {
  if (!list.length) return null
  if (recent) return '最近用过的'
  if (list.every((s) => s.match === 'similar')) return '没有完全匹配，相近的商品'
  return null
}

/**
 * 还没出候选就回车（扫码枪扫出代码后回车）时直接加入的：代码完全一致的（"win01"也是 WIN-01），
 * 或者只有一个候选而且不只是相近；其他情况列出候选让人选。
 */
export function enterPick(list: readonly Suggestion[], text: string): PickedItem | null {
  const byCode = exactCode(list.map((s) => s.item), text)
  if (byCode) return byCode
  const [top] = list
  if (!top) return null
  if (top.match === 'exact' && (top.field === 'code' || top.field === 'model')) return top.item
  return list.length === 1 && top.match !== 'similar' && top.match !== 'recent' ? top.item : null
}

/** 候选的库存：开单时是现有和可用，下单时是可用；不管理库存的只有单位。 */
export function stockText(item: PickedItem, source: 'sales' | 'warehouse'): string {
  if (item.stock === null) return item.unit
  const available = qtyUnit(item.available, item.unit)
  return source === 'warehouse' ? `现有 ${qty(item.stock)} · 可用 ${available}` : `可用 ${available}`
}

/**
 * 把文字里和输入一致的部分标出来（按输入的每个词，不分大小写；单个字母、数字太常见，不标）。
 */
export function highlight(text: string, query: string): { text: string; hit: boolean }[] {
  const lower = text.toLowerCase()
  const words = query
    .toLowerCase()
    .split(/[\s,，、;；/|*×]+/)
    .filter((w) => w.length > 1 || w.charCodeAt(0) > 0x7f)
  if (!text || !words.length || lower.length !== text.length) return text ? [{ text, hit: false }] : []
  const marks = Array.from({ length: text.length }, () => false)
  for (const word of words) {
    for (let at = lower.indexOf(word); at >= 0; at = lower.indexOf(word, at + word.length)) {
      marks.fill(true, at, at + word.length)
    }
  }
  const parts: { text: string; hit: boolean }[] = []
  marks.forEach((hit, i) => {
    const last = parts[parts.length - 1]
    if (last && last.hit === hit) last.text += text[i]
    else parts.push({ text: text[i]!, hit })
  })
  return parts
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
