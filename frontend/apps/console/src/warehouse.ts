/** 仓库（设计文档 §25.13）用到的名称和小工具，与后端 app/modules/warehouse 一致。 */
import type { Schemas } from '@edp/api-client'

export type StockItem = Schemas['StockItemOut']
export type WarehouseDocument = Schemas['DocumentOut']
export type DocumentLine = Schemas['DocumentLineOut']
export type DocumentBrief = Schemas['DocumentBrief']
export type DraftLine = Schemas['DraftLine']
export type DocumentKind = WarehouseDocument['kind']
export type DocumentStatus = WarehouseDocument['status']
export type ItemKind = StockItem['kind']
export type WarehouseTab = 'material' | 'goods' | 'requisition' | 'receipt' | 'movements'

export const WAREHOUSE_TABS: [WarehouseTab, string][] = [
  ['material', '材料库存'],
  ['goods', '成品库存'],
  ['requisition', '领料单'],
  ['receipt', '入库单'],
  ['movements', '库存记录'],
]

export const KIND_LABEL: Record<DocumentKind, string> = { requisition: '领料单', receipt: '入库单' }
export const ITEM_KIND_LABEL: Record<ItemKind, string> = { goods: '成品', material: '材料' }

export const STATUS_LABEL: Record<DocumentStatus, string> = {
  pending: '待确认',
  confirmed: '已确认',
  rejected: '已退回',
  voided: '已作废',
}

export const STATUS_TAG: Record<DocumentStatus, 'warning' | 'success' | 'danger' | 'info'> = {
  pending: 'warning',
  confirmed: 'success',
  rejected: 'danger',
  voided: 'info',
}

export const DOCUMENT_FILTERS: [DocumentStatus | '', string][] = [
  ['', '全部'],
  ['pending', '待确认'],
  ['rejected', '已退回'],
  ['confirmed', '已确认'],
  ['voided', '已作废'],
]

/** 数量：最多三位小数，去掉多余的 0（2.5、10）；为空时是"—"。 */
export function qty(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  const rounded = Math.round(value * 1000) / 1000
  return String(Object.is(rounded, -0) ? 0 : rounded)
}

/** 数量和单位："2.5 米"；没有单位时只有数量。 */
export function qtyUnit(value: number | null | undefined, unit: string): string {
  const text = qty(value)
  return unit && text !== '—' ? `${text} ${unit}` : text
}

/** 两个数量相减（按三位小数算，避免 10 - 2.4 = 7.6000000000000005）。 */
export function minus(a: number, b: number): number {
  return Math.round((a - b) * 1000) / 1000
}

/** 加上数量。 */
export function plus(a: number, b: number): number {
  return Math.round((a + b) * 1000) / 1000
}

/** 单据确认后的库存：领料单减少，入库单增加（原来不管理库存的从 0 开始）。 */
export function stockAfterDocument(kind: DocumentKind, stock: number | null, quantity: number): number {
  return kind === 'requisition' ? minus(stock ?? 0, quantity) : plus(stock ?? 0, quantity)
}

/** 单据的简称："领料单 LL20260930-0001 待确认"。 */
export function briefText(doc: Pick<DocumentBrief, 'kind' | 'no' | 'status'>): string {
  return `${KIND_LABEL[doc.kind]} ${doc.no} ${STATUS_LABEL[doc.status]}`
}

/** 成品只能是整数，材料最多三位小数。 */
export function precisionOf(kind: ItemKind): number {
  return kind === 'goods' ? 0 : 3
}

/** 领料单里库存不够的材料（确认后库存会是负数；只提示，可以领）。 */
export function shortMaterials(lines: { name: string; quantity: number; stock: number | null }[]): string[] {
  return lines.filter((line) => (line.stock ?? 0) < line.quantity).map((line) => line.name)
}
