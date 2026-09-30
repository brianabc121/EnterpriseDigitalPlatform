<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'

import { api } from '../api'

/** 按租户限流：每分钟的员工接口请求、访客接口请求、OpenIM 回调和大模型调用上限。 */
type Kind = 'api' | 'visitor' | 'webhook' | 'llm'

const KINDS: { key: Kind; label: string; help: string }[] = [
  { key: 'api', label: '员工接口请求', help: '超过时返回"请求过于频繁"' },
  { key: 'visitor', label: '访客接口请求', help: '全体访客合计；每位访客另有单独的上限' },
  { key: 'webhook', label: 'OpenIM 回调', help: '超过时消息暂不入库，由对账在一分钟内补上' },
  { key: 'llm', label: '大模型调用', help: '超过时本轮 AI 接待转人工' },
]

const props = defineProps<{ tenantId: string }>()
const current = ref<Schemas['TenantRateLimits'] | null>(null)
const form = reactive<Record<Kind, number | null>>({ api: null, visitor: null, webhook: null, llm: null })
const saving = ref(false)

function fill(data: Schemas['TenantRateLimits']): void {
  current.value = data
  for (const { key } of KINDS) form[key] = data.overrides[key] ?? null
}

async function load(): Promise<void> {
  const { data, error } = await api.GET('/platform/v1/tenants/{tenant_id}/rate-limits', {
    params: { path: { tenant_id: props.tenantId } },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  fill(data)
}

async function save(): Promise<void> {
  saving.value = true
  const { data, error } = await api.PUT('/platform/v1/tenants/{tenant_id}/rate-limits', {
    params: { path: { tenant_id: props.tenantId } },
    body: { ...form },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  fill(data)
  ElMessage.success('已保存，30 秒内生效')
}

function display(value: number): string {
  return value === 0 ? '不限' : `${value.toLocaleString('zh-CN')} / 分钟`
}

onMounted(load)
</script>

<template>
  <div v-if="current" data-testid="tenant-rate-limits">
    <el-table :data="KINDS" size="small">
      <el-table-column label="类型" width="130" prop="label" />
      <el-table-column label="平台默认" width="140">
        <template #default="{ row }">{{ display(current.defaults[row.key as Kind]) }}</template>
      </el-table-column>
      <el-table-column label="单独设置" width="220">
        <template #default="{ row }">
          <el-input-number
            v-model="form[row.key as Kind]"
            :min="0"
            :max="10000000"
            :step="100"
            placeholder="用平台默认"
            controls-position="right"
            :data-testid="`rate-limit-${row.key}`"
          />
        </template>
      </el-table-column>
      <el-table-column label="生效" width="140">
        <template #default="{ row }">{{ display(current.effective[row.key as Kind]) }}</template>
      </el-table-column>
      <el-table-column label="本分钟" width="90">
        <template #default="{ row }">{{ current.usage[row.key as Kind] }}</template>
      </el-table-column>
      <el-table-column label="说明">
        <template #default="{ row }"><span class="sub">{{ row.help }}</span></template>
      </el-table-column>
    </el-table>
    <div class="actions">
      <el-button type="primary" :loading="saving" data-testid="rate-limits-save" @click="save">保存</el-button>
      <el-button @click="load">刷新计数</el-button>
      <span class="sub">
        清空表示使用平台默认，0 表示不限。每位访客每分钟最多 {{ current.visitor_per_minute }} 次请求（平台配置）。
      </span>
    </div>
  </div>
</template>

<style scoped>
.actions {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 12px;
}

.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
