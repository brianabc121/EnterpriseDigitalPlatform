import type { components } from './schema'

type UsageMetric = components['schemas']['UsageMetricOut']

/** 用量数值的显示：字节换算成 KB/MB/GB，其余加千分位。 */
export function formatUsage(metric: UsageMetric, value: number | undefined): string {
  const v = value ?? 0
  if (metric.unit !== '字节') return v.toLocaleString('zh-CN')
  if (v < 1024) return `${v} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let n = v / 1024
  let i = 0
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024
    i += 1
  }
  return `${n.toFixed(n >= 100 ? 0 : 1)} ${units[i]}`
}

/** 合计的说明：快照类取最后一天，活跃坐席取单日最高。 */
export function totalHint(metric: UsageMetric): string {
  if (metric.kind === 'snapshot') return '期末值'
  if (metric.kind === 'max') return '单日最高'
  return '合计'
}
