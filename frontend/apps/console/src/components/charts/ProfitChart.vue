<script setup lang="ts">
/**
 * 每月净利润（设计文档 §30.4）：单一序列的盈亏柱状图。零线上方是盈利（蓝），下方是亏损（红）——
 * 位置本身就说明盈亏，颜色是第二个通道；柱宽不超过 24px，离开零线的一端 4px 圆角；只直接标注最后
 * 一个月，其余通过悬停提示和下面的每月明细查看。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import { labelEvery, signedColumnPath, signedTicks } from './scale'

export interface ProfitPoint {
  /** 横轴上的短标签，例如 10 月。 */
  label: string
  /** 提示里的完整标签，例如 2026 年 10 月。 */
  title: string
  value: number
  /** 提示里另外显示的几行，例如销售收入、毛利。 */
  details?: string[]
}

const props = withDefaults(
  defineProps<{
    points: ProfitPoint[]
    format: (value: number) => string
    axisFormat?: (value: number) => string
    height?: number
    label: string
  }>(),
  { height: 240, axisFormat: undefined },
)

const MARGIN = { top: 22, right: 16, bottom: 26, left: 64 }
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
  signedTicks(Math.min(0, ...values.value), Math.max(0, ...values.value), 4),
)
const low = computed(() => ticks.value[0] ?? 0)
const high = computed(() => ticks.value[ticks.value.length - 1] ?? 1)
const band = computed(() => plot.value.width / Math.max(1, props.points.length))
const every = computed(() => labelEvery(props.points.length, plot.value.width))
const barWidth = computed(() => Math.max(2, Math.min(24, band.value * 0.6)))
const axis = computed(() => props.axisFormat ?? props.format)

function y(value: number): number {
  const span = high.value - low.value || 1
  return plot.value.y + plot.value.height - ((value - low.value) / span) * plot.value.height
}

function center(i: number): number {
  return plot.value.x + band.value * i + band.value / 2
}

const zero = computed(() => y(0))
const bars = computed(() =>
  props.points.map((p, i) => ({
    d: signedColumnPath(center(i) - barWidth.value / 2, zero.value, barWidth.value, y(p.value)),
    loss: p.value < 0,
  })),
)

/** 只标注最后一个月（本期）；为 0 时不标。 */
const labelled = computed(() => {
  const i = props.points.length - 1
  const point = props.points[i]
  if (!point || point.value === 0) return null
  // 盈利标在柱顶上方；亏损标在柱子末端下方，那里放不下（会压住横轴的月份）时标在零线上方。
  let top = y(point.value) - 8
  if (point.value < 0) {
    top = y(point.value) + 14
    if (top > plot.value.y + plot.value.height - 2) top = zero.value - 8
  }
  // 右对齐到柱子的右边，避免最右边的数字超出图表。
  return { i, x: center(i) + barWidth.value / 2, y: top }
})

const tooltip = computed(() => {
  if (active.value === null) return null
  const point = props.points[active.value]
  if (!point) return null
  const top = Math.min(y(point.value), zero.value)
  return {
    point,
    value: `${point.value < 0 ? '亏损' : '盈利'} ${props.format(Math.abs(point.value))}`,
    left: Math.min(Math.max(center(active.value), 80), width.value - 80),
    top,
    below: top < 72,
  }
})
</script>

<template>
  <div ref="root" class="profit-chart" :style="{ height: `${height}px` }">
    <svg :width="width" :height="height" role="img" :aria-label="label" @pointerleave="active = null">
      <g class="grid">
        <g v-for="t in ticks" :key="t">
          <line
            :class="{ zero: t === 0 }"
            :x1="plot.x"
            :x2="plot.x + plot.width"
            :y1="y(t)"
            :y2="y(t)"
          />
          <text :x="plot.x - 8" :y="y(t)" dy="0.32em" text-anchor="end">{{ axis(t) }}</text>
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
      <path
        v-for="(bar, i) in bars"
        :key="i"
        :d="bar.d"
        class="column"
        :class="{ loss: bar.loss, active: active === i }"
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
        :aria-label="`${p.title}：${p.value < 0 ? '亏损' : '盈利'} ${format(Math.abs(p.value))}`"
        @pointerenter="active = i"
        @focus="active = i"
        @blur="active = null"
      />
      <text
        v-if="labelled && active === null"
        class="direct-label"
        :x="labelled.x"
        :y="labelled.y"
        text-anchor="end"
      >
        {{ format(points[labelled.i]!.value) }}
      </text>
    </svg>
    <div
      v-if="tooltip"
      class="tooltip"
      :class="{ below: tooltip.below }"
      :style="{ left: `${tooltip.left}px`, top: `${tooltip.top}px` }"
      role="status"
    >
      <span>{{ tooltip.point.title }}</span>
      <strong>{{ tooltip.value }}</strong>
      <span v-for="line in tooltip.point.details ?? []" :key="line">{{ line }}</span>
    </div>
  </div>
</template>

<style scoped>
/* 盈利、亏损两种颜色：调色板的蓝、红两端（已用 validate_palette 校验浅色和深色两种表面）。 */
.profit-chart {
  --viz-gain: #2a78d6;
  --viz-loss: #e34948;
  --viz-surface: var(--el-bg-color);
  position: relative;
  width: 100%;
}

:global(html.dark) .profit-chart {
  --viz-gain: #3987e5;
  --viz-loss: #e66767;
}

svg {
  display: block;
  overflow: visible;
}

.grid line {
  stroke: var(--el-border-color-lighter);
  stroke-width: 1;
}

.grid line.zero {
  stroke: var(--el-border-color-darker);
}

.grid text,
.x-labels text {
  fill: var(--el-text-color-secondary);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.column {
  fill: var(--viz-gain);
}

.column.loss {
  fill: var(--viz-loss);
}

.column.active {
  fill-opacity: 0.72;
}

.hit {
  fill: transparent;
  outline: none;
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
