<script setup lang="ts">
import { errorMessage, formatUsage, totalHint, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'

import { api, formatDateTime } from '../../api'
import StatTile from '../charts/StatTile.vue'

/** 首屏展示的几项；其余在表格里。 */
const HEADLINE = [
  'messages_in',
  'agent_messages',
  'human_sessions',
  'seats',
  'channels',
  'file_bytes',
]

const report = ref<Schemas['UsageReport'] | null>(null)
const loading = ref(false)

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/usage')
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  report.value = data
}

const headline = computed(() =>
  (report.value?.metrics ?? []).filter((m) => HEADLINE.includes(m.key)),
)
const days = computed(() => [...(report.value?.days ?? [])].reverse())

onMounted(load)
</script>

<template>
  <div v-loading="loading" data-testid="usage-tab">
    <template v-if="report">
      <p class="hint">
        {{ report.start }} 至 {{ report.end }}（{{ report.timezone }}）。用量每 10 分钟汇总一次，
        最近一次汇总于 {{ report.updated_at ? formatDateTime(report.updated_at) : '—' }}。
      </p>
      <div class="tiles">
        <StatTile
          v-for="m in headline"
          :key="m.key"
          :label="m.label"
          :value="formatUsage(m, report.totals[m.key])"
          :hint="totalHint(m)"
          :testid="`usage-${m.key}`"
        />
      </div>
      <el-table :data="days" size="small" class="table" data-testid="usage-days">
        <el-table-column prop="day" label="日期" width="110" fixed />
        <el-table-column v-for="m in report.metrics" :key="m.key" :label="m.label" min-width="96">
          <template #default="{ row }">{{ formatUsage(m, row.values[m.key]) }}</template>
        </el-table-column>
      </el-table>
    </template>
  </div>
</template>

<style scoped>
.hint {
  margin: 0 0 12px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.tiles {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 12px;
}

.table {
  margin-top: 16px;
}
</style>
