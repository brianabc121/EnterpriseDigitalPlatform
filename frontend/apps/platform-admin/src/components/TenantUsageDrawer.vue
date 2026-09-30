<script setup lang="ts">
import { errorMessage, formatUsage, totalHint, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, ref, watch } from 'vue'

import { api, formatDateTime } from '../api'

const props = defineProps<{ tenant: Schemas['TenantOut'] | null }>()
const emit = defineEmits<{ close: [] }>()

const report = ref<Schemas['UsageReport'] | null>(null)
const loading = ref(false)
const open = computed({
  get: () => props.tenant !== null,
  set: (value) => {
    if (!value) emit('close')
  },
})
const days = computed(() => [...(report.value?.days ?? [])].reverse())

watch(
  () => props.tenant,
  async (tenant) => {
    report.value = null
    if (!tenant) return
    loading.value = true
    const { data, error } = await api.GET('/platform/v1/tenants/{tenant_id}/usage', {
      params: { path: { tenant_id: tenant.id } },
    })
    loading.value = false
    if (!data) {
      ElMessage.error(errorMessage(error))
      return
    }
    report.value = data
  },
)
</script>

<template>
  <el-drawer v-model="open" :title="`用量 · ${tenant?.name ?? ''}`" size="80%">
    <div v-loading="loading" data-testid="tenant-usage">
      <template v-if="report">
        <p class="hint">
          {{ report.start }} 至 {{ report.end }}（{{ report.timezone }}），最近一次汇总于
          {{ report.updated_at ? formatDateTime(report.updated_at) : '—' }}。
        </p>
        <el-descriptions :column="5" border size="small">
          <el-descriptions-item v-for="m in report.metrics" :key="m.key" :label="m.label">
            {{ formatUsage(m, report.totals[m.key]) }}
            <span class="hint">{{ totalHint(m) }}</span>
          </el-descriptions-item>
        </el-descriptions>
        <el-table :data="days" size="small" class="table">
          <el-table-column prop="day" label="日期" width="110" fixed />
          <el-table-column v-for="m in report.metrics" :key="m.key" :label="m.label" min-width="96">
            <template #default="{ row }">{{ formatUsage(m, row.values[m.key]) }}</template>
          </el-table-column>
        </el-table>
      </template>
    </div>
  </el-drawer>
</template>

<style scoped>
.hint {
  margin: 0 0 12px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.table {
  margin-top: 16px;
}
</style>
