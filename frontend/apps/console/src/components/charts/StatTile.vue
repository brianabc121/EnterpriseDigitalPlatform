<script setup lang="ts">
defineProps<{ label: string; value: string; hint?: string; testid?: string }>()
</script>

<template>
  <div class="stat-tile" :data-testid="testid">
    <div class="label">{{ label }}</div>
    <div class="value">{{ value }}</div>
    <div v-if="hint" class="hint">{{ hint }}</div>
  </div>
</template>

<style scoped>
/* 宽度由外面的网格决定（首页在手机上是两列）。 */
.stat-tile {
  min-width: 0;
  padding: 14px 16px;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
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

.hint {
  margin-top: 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
