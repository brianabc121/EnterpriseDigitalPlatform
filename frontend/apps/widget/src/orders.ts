/** 订单跟踪页和"我的订单"的小工具。 */

/** 金额显示为"¥1,299.00"；为空时显示"待确认"（待定价）。 */
export function money(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return '待确认'
  const number = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(number)) return '待确认'
  return `¥${number.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

/** 时间显示为"9月30日 14:05"。 */
export function shortTime(value: string | null | undefined): string {
  if (!value) return ''
  const date = new Date(value)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getMonth() + 1}月${date.getDate()}日 ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/**
 * 页面地址里的跟踪令牌（?track=…）：有这个参数时 Widget 只显示订单跟踪页。没有参数返回 null；
 * 格式不对（链接被截断或改动过）返回空字符串，页面直接提示链接失效。
 */
export function trackToken(search: string): string | null {
  const token = new URLSearchParams(search).get('track')
  if (token === null) return null
  return /^[\w-]{16,64}$/.test(token) ? token : ''
}
