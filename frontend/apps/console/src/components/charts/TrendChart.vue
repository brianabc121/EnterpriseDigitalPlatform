<script setup lang="ts">
/**
 * 单一序列的趋势图（柱状或折线），用于报表的每日数据。
 * 规格：柱宽不超过 24px、顶部 4px 圆角；折线 2px；末端点带 2px 表面色描边；网格线为 1px 细实线；
 * 只标注一个值（柱状标最大值，折线标最后一个值），其余通过悬停提示和页面上的表格查看。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import { columnPath, labelEvery, niceTicks } from './scale'

export interface TrendPoint {
  /** 横轴上显示的短标签，例如 09-28。 */
  label: string
  /** 提示里显示的完整标签，例如 2026-09-28 周一。 */
  title: string
  value: number | null
}

const props = withDefaults(
  defineProps<{
    points: TrendPoint[]
    kind: 'column' | 'line'
    format?: (value: number) => string
    height?: number
    /** 刻度的最小步长：计数、秒数传 1。 */
    minStep?: number
    label: string
  }>(),
  { format: (v: number) => v.toLocaleString('zh-CN'), height: 220, minStep: 1 },
)

const MARGIN = { top: 20, right: 16, bottom: 26, left: 48 }
const root = ref<HTMLElement | null>(null)
const width = ref(600)
const active = ref<number | null>(null)
let observer: ResizeObserver | null = null

onMounted(() => {
  if (!root.value) return
  width.value = root.value.clientWidth || 600
  observer = new ResizeObserver(([entry]) => {
    if (entry) width.value = Math.max(240, Math.floor(entry.contentRect.width))
  })
  observer.observe(root.value)
})
onBeforeUnmount(() => observer?.disconnect())

const plot = computed(() => ({
  x: MARGIN.left,
  y: MARGIN.top,
  width: width.value - MARGIN.left - MARGIN.right,
  height: props.height - MARGIN.top - MARGIN.bottom,
}))
const values = computed(() => props.points.map((p) => p.value))
const ticks = computed(() =>
  niceTicks(Math.max(0, ...values.value.map((v) => v ?? 0)), 4, props.minStep),
)
const top = computed(() => ticks.value[ticks.value.length - 1] || 1)
const band = computed(() => plot.value.width / Math.max(1, props.points.length))
const every = computed(() => labelEvery(props.points.length, plot.value.width))

function y(value: number): number {
  return plot.value.y + plot.value.height - (value / top.value) * plot.value.height
}

function center(i: number): number {
  return plot.value.x + band.value * i + band.value / 2
}

const barWidth = computed(() => Math.max(2, Math.min(24, band.value * 0.6)))

const columns = computed(() =>
  props.points.map((p, i) => {
    const v = p.value ?? 0
    const h = plot.value.y + plot.value.height - y(v)
    return columnPath(center(i) - barWidth.value / 2, y(v), barWidth.value, h)
  }),
)

/** 折线路径：没有数据的日子断开。 */
const linePath = computed(() => {
  let d = ''
  let drawing = false
  props.points.forEach((p, i) => {
    if (p.value === null) {
      drawing = false
      return
    }
    d += `${drawing ? 'L' : 'M'}${center(i)},${y(p.value)}`
    drawing = true
  })
  return d
})

/** 唯一直接标注的点：柱状取最大值，折线取最后一个有数据的点。 */
const labelled = computed(() => {
  const indexed = props.points.map((p, i) => [i, p.value] as const).filter(([, v]) => v !== null)
  if (indexed.length === 0) return null
  if (props.kind === 'line') return indexed[indexed.length - 1]![0]
  const [best] = [...indexed].sort((a, b) => (b[1] ?? 0) - (a[1] ?? 0))
  return best && (best[1] ?? 0) > 0 ? best[0] : null
})

const tooltip = computed(() => {
  if (active.value === null) return null
  const point = props.points[active.value]
  if (!point) return null
  const x = center(active.value)
  const top = point.value === null ? plot.value.y : y(point.value)
  return {
    point,
    value: point.value === null ? '无数据' : props.format(point.value),
    left: Math.min(Math.max(x, 70), width.value - 70),
    top,
    // 靠近顶部时放到点的下方，避免被卡片标题遮住。
    below: top < 64,
  }
})

function onMove(event: PointerEvent): void {
  if (props.kind !== 'line' || !root.value) return
  const rect = root.value.getBoundingClientRect()
  const i = Math.floor((event.clientX - rect.left - plot.value.x) / band.value)
  active.value = i >= 0 && i < props.points.length ? i : null
}
</script>

<template>
  <div ref="root" class="trend-chart" :style="{ height: `${height}px` }">
    <svg
      :width="width"
      :height="height"
      role="img"
      :aria-label="label"
      @pointermove="onMove"
      @pointerleave="active = null"
    >
      <g class="grid">
        <g v-for="t in ticks" :key="t">
          <line :x1="plot.x" :x2="plot.x + plot.width" :y1="y(t)" :y2="y(t)" />
          <text :x="plot.x - 8" :y="y(t)" dy="0.32em" text-anchor="end">{{ format(t) }}</text>
        </g>
      </g>
      <g class="x-labels">
        <template v-for="(p, i) in points" :key="p.title">
          <text
            v-if="i % every === 0 || i === points.length - 1"
            :x="center(i)"
            :y="plot.y + plot.height + 18"
            text-anchor="middle"
          >
            {{ p.label }}
          </text>
        </template>
      </g>

      <template v-if="kind === 'column'">
        <path
          v-for="(d, i) in columns"
          :key="i"
          :d="d"
          class="column"
          :class="{ active: active === i }"
        />
        <rect
          v-for="(p, i) in points"
          :key="`hit-${i}`"
          class="hit"
          :x="plot.x + band * i"
          :y="plot.y"
          :width="band"
          :height="plot.height"
          tabindex="0"
          :aria-label="`${p.title}：${p.value === null ? '无数据' : format(p.value)}`"
          @pointerenter="active = i"
          @focus="active = i"
          @blur="active = null"
        />
      </template>
      <template v-else>
        <line
          v-if="active !== null"
          class="crosshair"
          :x1="center(active)"
          :x2="center(active)"
          :y1="plot.y"
          :y2="plot.y + plot.height"
        />
        <path class="line" :d="linePath" />
        <circle
          v-if="labelled !== null"
          class="marker"
          :cx="center(labelled)"
          :cy="y(points[labelled]!.value ?? 0)"
          r="4"
        />
        <circle
          v-if="active !== null && points[active]?.value !== null"
          class="marker"
          :cx="center(active)"
          :cy="y(points[active]!.value ?? 0)"
          r="4"
        />
      </template>

      <text
        v-if="labelled !== null && active === null"
        class="direct-label"
        :x="center(labelled)"
        :y="y(points[labelled]!.value ?? 0) - 8"
        :text-anchor="kind === 'line' && labelled === points.length - 1 ? 'end' : 'middle'"
      >
        {{ format(points[labelled]!.value ?? 0) }}
      </text>
    </svg>
    <div
      v-if="tooltip"
      class="tooltip"
      :class="{ below: tooltip.below }"
      :style="{ left: `${tooltip.left}px`, top: `${tooltip.top}px` }"
      role="status"
    >
      <strong>{{ tooltip.value }}</strong>
      <span>{{ tooltip.point.title }}</span>
    </div>
  </div>
</template>

<style scoped>
.trend-chart {
  --viz-series: #2a78d6;
  --viz-surface: var(--el-bg-color);
  position: relative;
  width: 100%;
}

:global(html.dark) .trend-chart {
  --viz-series: #3987e5;
}

svg {
  display: block;
  overflow: visible;
}

.grid line {
  stroke: var(--el-border-color-lighter);
  stroke-width: 1;
}

.grid text,
.x-labels text {
  fill: var(--el-text-color-secondary);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.column {
  fill: var(--viz-series);
}

.column.active {
  fill-opacity: 0.72;
}

.hit {
  fill: transparent;
  outline: none;
}

.line {
  fill: none;
  stroke: var(--viz-series);
  stroke-width: 2;
  stroke-linejoin: round;
  stroke-linecap: round;
}

.marker {
  fill: var(--viz-series);
  stroke: var(--viz-surface);
  stroke-width: 2;
}

.crosshair {
  stroke: var(--el-border-color);
  stroke-width: 1;
}

.direct-label {
  fill: var(--el-text-color-primary);
  font-size: 12px;
  font-weight: 600;
}

.tooltip {
  position: absolute;
  transform: translate(-50%, calc(-100% - 12px));
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 6px 10px;
  background: var(--el-bg-color-overlay);
  border: 1px solid var(--el-border-color-light);
  border-radius: 6px;
  box-shadow: var(--el-box-shadow-light);
  pointer-events: none;
  white-space: nowrap;
  font-size: 12px;
}

.tooltip.below {
  transform: translate(-50%, 12px);
}

.tooltip strong {
  font-size: 14px;
  color: var(--el-text-color-primary);
}

.tooltip span {
  color: var(--el-text-color-secondary);
}
</style>
