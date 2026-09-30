/** 金额（分）显示为元：¥1,999.00。 */
export function formatMoney(fen: number | null | undefined): string {
  const value = (fen ?? 0) / 100
  return `¥${value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

/** 额度显示：为空表示不限。 */
export function formatLimit(limit: number | null | undefined, unit = ''): string {
  if (limit === null || limit === undefined) return '不限'
  return `${limit.toLocaleString('zh-CN')}${unit ? ` ${unit}` : ''}`
}

/** 用量占额度的百分比（0–100，不限时为 0）。 */
export function usagePercent(used: number, limit: number | null | undefined): number {
  if (!limit) return limit === 0 ? 100 : 0
  return Math.min(100, Math.round((used / limit) * 100))
}

export const SUBSCRIPTION_STATUS: Record<string, string> = {
  trial: '试用中',
  active: '正常',
  expired: '已到期',
  cancelled: '已取消',
}

export const INVOICE_STATUS: Record<string, string> = {
  issued: '待付款',
  paid: '已付款',
  void: '已作废',
}
