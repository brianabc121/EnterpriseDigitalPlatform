/**
 * 纵轴刻度：取"整"的步长（1、2、5 × 10^n），让刻度读起来是 0 / 20 / 40 这样的数。
 * minStep 为最小步长：计数、秒数这类整数量传 1，避免出现 0.5 个会话这样的刻度。
 */
export function niceTicks(max: number, count = 4, minStep = 0): number[] {
  if (!Number.isFinite(max) || max <= 0) return [0, Math.max(1, minStep)]
  const raw = Math.max(max / count, minStep)
  const magnitude = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 2, 5, 10].map((m) => m * magnitude).find((s) => s >= raw) ?? raw
  const ticks: number[] = []
  for (let v = 0; v < max + step * 0.999; v += step) ticks.push(Number(v.toPrecision(12)))
  return ticks
}

/** 顶部两角圆角（4px）、底部直角的柱子路径。 */
export function columnPath(
  x: number,
  y: number,
  width: number,
  height: number,
  radius = 4,
): string {
  if (height <= 0) return ''
  const r = Math.min(radius, width / 2, height)
  return [
    `M${x},${y + height}`,
    `V${y + r}`,
    `Q${x},${y} ${x + r},${y}`,
    `H${x + width - r}`,
    `Q${x + width},${y} ${x + width},${y + r}`,
    `V${y + height}`,
    'Z',
  ].join('')
}

/** 横轴标签太密时只显示一部分：返回每隔几个显示一个。 */
export function labelEvery(count: number, width: number, labelWidth = 44): number {
  const fit = Math.max(1, Math.floor(width / labelWidth))
  return Math.max(1, Math.ceil(count / fit))
}
