<script setup lang="ts">
import type { RouteLocationRaw } from 'vue-router'

/** 可以点开的数字（首页的待处理事项）：点开后打开对应的页面并筛选好。有数字时按 tone 标色。 */
defineProps<{
  label: string
  value: number
  to: RouteLocationRaw
  hint?: string
  tone?: 'danger' | 'warning'
  testid?: string
}>()
</script>

<template>
  <router-link :to="to" class="link-tile" :data-testid="testid">
    <span class="label">{{ label }}</span>
    <span class="value" :class="value > 0 && tone ? tone : ''">{{ value }}</span>
    <span v-if="hint" class="hint">{{ hint }}</span>
  </router-link>
</template>

<style scoped>
.link-tile {
  display: flex;
  flex-direction: column;
  min-width: 150px;
  padding: 14px 16px;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  color: inherit;
  text-decoration: none;
  transition: border-color 0.15s;
}

.link-tile:hover {
  border-color: var(--el-color-primary);
}

.label {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.value {
  margin-top: 6px;
  font-size: 24px;
  font-weight: 600;
  color: var(--el-text-color-primary);
}

.value.danger {
  color: var(--el-color-danger);
}

.value.warning {
  color: var(--el-color-warning);
}

.hint {
  margin-top: 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
