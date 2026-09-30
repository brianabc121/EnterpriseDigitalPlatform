<script setup lang="ts">
/**
 * 排名条形图：一类数值的分布（例如转人工原因），按数值从大到小，每行标签、条、数值。
 * 单一颜色；条高 16px、右端 4px 圆角；数值直接标在条后，悬停显示占比。
 */
import { computed } from 'vue'

export interface BarItem {
  label: string
  value: number
}

const props = withDefaults(defineProps<{ items: BarItem[]; label: string; empty?: string }>(), {
  empty: '暂无数据',
})

const total = computed(() => props.items.reduce((sum, item) => sum + item.value, 0))
const max = computed(() => Math.max(1, ...props.items.map((item) => item.value)))
const rows = computed(() =>
  [...props.items]
    .sort((a, b) => b.value - a.value)
    .map((item) => ({
      ...item,
      width: `${Math.max(2, (item.value / max.value) * 100)}%`,
      share: total.value ? Math.round((item.value / total.value) * 100) : 0,
    })),
)
</script>

<template>
  <div class="bar-list" role="table" :aria-label="label">
    <div
      v-for="row in rows"
      :key="row.label"
      class="row"
      role="row"
      :title="`${row.label}：${row.value}（${row.share}%）`"
    >
      <span class="name" role="cell">{{ row.label }}</span>
      <span class="track" role="cell">
        <span class="bar" :style="{ width: row.width }" />
      </span>
      <span class="value" role="cell">{{ row.value.toLocaleString('zh-CN') }}</span>
    </div>
    <p v-if="rows.length === 0" class="empty">{{ empty }}</p>
  </div>
</template>

<style scoped>
.bar-list {
  --viz-series: #2a78d6;
  display: grid;
  gap: 8px;
}

:global(html.dark) .bar-list {
  --viz-series: #3987e5;
}

.row {
  display: grid;
  grid-template-columns: 120px minmax(0, 1fr) 48px;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}

.name {
  color: var(--el-text-color-regular);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.track {
  display: block;
  height: 16px;
}

.bar {
  display: block;
  height: 100%;
  background: var(--viz-series);
  border-radius: 0 4px 4px 0;
}

.value {
  color: var(--el-text-color-primary);
  font-variant-numeric: tabular-nums;
}

.empty {
  margin: 0;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
</style>
