/** 库存（设计文档 §25.12）用到的名称和小工具，与后端 app/modules/products/stock.py 一致。 */
import type { Schemas } from '@edp/api-client'

export type StockMovement = Schemas['StockMovementOut']
export type StockMode = Schemas['StockAdjustIn']['mode']
export type StockFilter = 'low' | 'tracked' | 'untracked'

type StockLevel = Pick<
  Schemas['ProductOut'],
  'stock' | 'stock_reserved' | 'stock_available' | 'stock_low'
>

/** 手动调整库存的方式。 */
export const STOCK_MODES: [StockMode, string, string][] = [
  ['add', '入库', '到货后增加库存'],
  ['remove', '出库', '损耗、自用等减少库存（不能超过现有库存）'],
  ['set', '盘点', '把现有库存改为实际清点的数量'],
  ['untrack', '不再管理', '这个商品不再管理库存（例如服务类商品）'],
]

export const STOCK_FILTERS: [StockFilter, string][] = [
  ['low', '库存不足'],
  ['tracked', '管理库存的'],
  ['untracked', '不管理库存的'],
]

/** 库存摘要，如"现有 12 · 占用 2"；不管理库存时为空。 */
export function stockDetail(level: StockLevel): string {
  if (level.stock === null) return ''
  return level.stock_reserved ? `现有 ${level.stock} · 占用 ${level.stock_reserved}` : `现有 ${level.stock}`
}

/** 调整后的现有库存（出库超过现有库存、不再管理时返回 null）。 */
export function stockAfter(current: number | null, mode: StockMode, quantity: number): number | null {
  if (mode === 'untrack') return null
  if (mode === 'set') return quantity
  if (mode === 'add') return (current ?? 0) + quantity
  if (current === null || quantity > current) return null
  return current - quantity
}

/** 库存变化量："+5"、"-3"、"0"。 */
export function deltaText(delta: number): string {
  return delta > 0 ? `+${delta}` : String(delta)
}

/** 库存记录里变化前后的数量："10 → 12"，不管理库存的写作"—"。 */
export function changeText(movement: Pick<StockMovement, 'stock_before' | 'stock_after'>): string {
  const show = (value: number | null) => (value === null ? '—' : String(value))
  return `${show(movement.stock_before)} → ${show(movement.stock_after)}`
}

/** 订单行的库存提示："可用 3"；库存不足时加上"库存不足"。不管理库存的为空。 */
export function lineStockText(item: { stock_available?: number | null; stock_short?: boolean }): string {
  if (item.stock_available === null || item.stock_available === undefined) return ''
  return item.stock_short ? `库存不足（可用 ${item.stock_available}）` : `可用 ${item.stock_available}`
}

/** 确认订单前的库存提示：库存不足的商品（只提示，不拦截）。 */
export function shortLines(
  items: { name: string; spec?: string; quantity: number; stock_available?: number | null; stock_short?: boolean }[],
): string[] {
  return items
    .filter((i) => i.stock_short)
    .map((i) => `${i.name}${i.spec ? `（${i.spec}）` : ''}：需要 ${i.quantity}，可用 ${i.stock_available ?? 0}`)
}
