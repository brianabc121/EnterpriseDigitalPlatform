<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { ref } from 'vue'

import { api, formatDateTime } from '../../api'

const open = defineModel<boolean>({ required: true })
const items = ref<Schemas['PrivacyRequestOut'][]>([])
const loading = ref(false)

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/customers/privacy-requests')
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  items.value = data.items
}

const SUMMARY: [string, string][] = [
  ['sessions', '会话'],
  ['messages', '消息'],
  ['files', '文件'],
]

function summary(detail: Record<string, unknown>): string {
  return SUMMARY.filter(([key]) => typeof detail[key] === 'number')
    .map(([key, label]) => `${label} ${String(detail[key])}`)
    .join('，')
}
</script>

<template>
  <el-drawer v-model="open" title="个人信息请求记录" size="640px" @open="load">
    <el-table v-loading="loading" :data="items" data-testid="privacy-requests" empty-text="暂无记录">
      <el-table-column label="时间" width="170">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="类型" width="80">
        <template #default="{ row }">
          <el-tag :type="row.kind === 'erase' ? 'danger' : 'info'" disable-transitions>
            {{ row.kind === 'erase' ? '删除' : '查询' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="customer_name" label="客户" width="90" />
      <el-table-column label="处理人" width="90">
        <template #default="{ row }">{{ row.requested_by_name ?? '—' }}</template>
      </el-table-column>
      <el-table-column label="来源与结果" min-width="200">
        <template #default="{ row }">
          <div>{{ row.reason }}</div>
          <small class="muted">{{ summary(row.detail) }}</small>
        </template>
      </el-table-column>
    </el-table>
  </el-drawer>
</template>

<style scoped>
.muted {
  color: var(--el-text-color-secondary);
}
</style>
