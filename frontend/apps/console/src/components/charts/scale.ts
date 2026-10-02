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

/** 有正有负的刻度（盈亏）：一定包含 0，步长同样取"整"的。 */
export function signedTicks(min: number, max: number, count = 4): number[] {
  const lo = Math.min(0, min)
  const hi = Math.max(0, max)
  if (lo === 0) return niceTicks(hi, count)
  if (hi === 0) {
    return niceTicks(-lo, count)
      .map((t) => (t === 0 ? 0 : -t))
      .reverse()
  }
  const raw = (hi - lo) / count
  const magnitude = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 2, 5, 10].map((m) => m * magnitude).find((s) => s >= raw) ?? raw
  const ticks: number[] = []
  for (let v = Math.floor(lo / step) * step; v < hi + step * 0.999; v += step) {
    ticks.push(Number(v.toPrecision(12)) || 0)
  }
  return ticks
}

/**
 * 从基线 y0 长出的柱子：值在基线上方（yValue < y0）向上、下方向下；离开基线的一端 4px 圆角，
 * 贴着基线的一端是直角。
 */
export function signedColumnPath(
  x: number,
  y0: number,
  width: number,
  yValue: number,
  radius = 4,
): string {
  if (yValue === y0) return ''
  if (yValue < y0) return columnPath(x, yValue, width, y0 - yValue, radius)
  const bottom = yValue
  const r = Math.min(radius, width / 2, bottom - y0)
  return [
    `M${x},${y0}`,
    `V${bottom - r}`,
    `Q${x},${bottom} ${x + r},${bottom}`,
    `H${x + width - r}`,
    `Q${x + width},${bottom} ${x + width},${bottom - r}`,
    `V${y0}`,
    'Z',
  ].join('')
}
