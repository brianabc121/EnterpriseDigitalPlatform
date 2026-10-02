<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import { api, formatDateTime } from '../../api'
import { downloadBlob } from '../../download'
import { feeText, tokenText } from '../../tokens'
import SessionDrawer from '../sessions/SessionDrawer.vue'

/**
 * 近 7 天的 token 明细（设计文档 §37.4）：每次大模型调用一行——时间、场景、模型、输入和输出 tokens、费用、耗时、
 * 状态、触发的员工、关联的会话；按场景、状态、员工筛选，导出 CSV。
 */
type Call = Schemas['TokenCall']

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()

const PAGE_SIZE = 50
const filters = reactive({ scene: '', status: '' as '' | 'ok' | 'failed', staff: '' })
const page = ref(1)
const result = ref<Schemas['TokenCallPage'] | null>(null)
const loading = ref(false)
const exporting = ref(false)
const viewingSession = ref<string | null>(null)

const open = computed({
  get: () => props.modelValue,
  set: (value) => emit('update:modelValue', value),
})
const staffNames = computed(
  () => new Map((result.value?.staff ?? []).filter((s) => s.id !== 'system').map((s) => [s.id, s.name])),
)

function query() {
  return {
    scene: filters.scene || undefined,
    status: filters.status || undefined,
    staff_id: filters.staff || undefined,
  }
}

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/tokens/calls', {
    params: { query: { ...query(), limit: PAGE_SIZE, offset: (page.value - 1) * PAGE_SIZE } },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  result.value = data
}

function search(): void {
  if (page.value === 1) void load()
  else page.value = 1
}

async function exportCsv(): Promise<void> {
  exporting.value = true
  const { data, error } = await api.GET('/api/v1/tokens/calls/export', {
    params: { query: query() },
    parseAs: 'blob',
  })
  exporting.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  downloadBlob(data as Blob, `token明细-近7天.csv`)
}

function statusText(call: Call): string {
  if (call.status === 'ok') return '成功'
  if (call.status === 'busy') return '并发已满'
  return '失败'
}

watch([() => filters.scene, () => filters.status, () => filters.staff], search)
watch(page, load)
watch(open, (visible) => {
  if (!visible) return
  page.value = 1
  void load()
})
</script>

<template>
  <el-drawer v-model="open" title="近 7 天的 token 明细" size="1180px" append-to-body data-testid="token-details">
    <div class="filters">
      <el-select v-model="filters.scene" clearable placeholder="全部场景" class="select" data-testid="token-calls-scene">
        <el-option v-for="s in result?.scenes ?? []" :key="s.key" :label="s.label" :value="s.key" />
      </el-select>
      <el-select v-model="filters.status" clearable placeholder="全部状态" class="select narrow" data-testid="token-calls-status">
        <el-option label="成功" value="ok" />
        <el-option label="失败" value="failed" />
      </el-select>
      <el-select v-model="filters.staff" clearable filterable placeholder="全部员工" class="select" data-testid="token-calls-staff">
        <el-option v-for="s in result?.staff ?? []" :key="s.id" :label="s.name" :value="s.id" />
      </el-select>
      <el-button :loading="exporting" data-testid="token-calls-export" @click="exportCsv">导出 CSV</el-button>
    </div>
    <p v-if="result" class="summary" data-testid="token-calls-summary">
      {{ formatDateTime(result.since) }} 起：{{ result.total.toLocaleString('zh-CN') }} 次调用，tokens
      {{ tokenText(result.tokens) }}，费用 {{ feeText(result.cost) }}
    </p>
    <el-table
      v-loading="loading"
      :data="result?.items ?? []"
      size="small"
      row-key="id"
      empty-text="近 7 天没有大模型调用"
      data-testid="token-calls-table"
    >
      <el-table-column label="时间" width="158">
        <template #default="{ row }: { row: Call }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column prop="scene_label" label="场景" min-width="130" />
      <el-table-column label="模型" min-width="150">
        <template #default="{ row }: { row: Call }">
          <span class="model">{{ row.model }}</span>
        </template>
      </el-table-column>
      <el-table-column label="输入" width="86" align="right">
        <template #default="{ row }: { row: Call }">{{ row.prompt_tokens.toLocaleString('zh-CN') }}</template>
      </el-table-column>
      <el-table-column label="输出" width="80" align="right">
        <template #default="{ row }: { row: Call }">{{ row.completion_tokens.toLocaleString('zh-CN') }}</template>
      </el-table-column>
      <el-table-column label="合计" width="90" align="right">
        <template #default="{ row }: { row: Call }">
          <strong>{{ row.tokens.toLocaleString('zh-CN') }}</strong>
        </template>
      </el-table-column>
      <el-table-column label="费用" width="96" align="right">
        <template #default="{ row }: { row: Call }">{{ row.own_key ? '自带密钥' : feeText(row.cost) }}</template>
      </el-table-column>
      <el-table-column label="耗时" width="76" align="right">
        <template #default="{ row }: { row: Call }">{{ row.latency_ms ? `${(row.latency_ms / 1000).toFixed(1)}s` : '—' }}</template>
      </el-table-column>
      <el-table-column label="状态" width="92">
        <template #default="{ row }: { row: Call }">
          <el-tooltip v-if="row.status !== 'ok'" :content="row.error ?? ''" placement="top" :disabled="!row.error">
            <el-tag size="small" type="danger" disable-transitions>{{ statusText(row) }}</el-tag>
          </el-tooltip>
          <el-tag v-else size="small" type="success" disable-transitions>{{ statusText(row) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="员工" width="96">
        <template #default="{ row }: { row: Call }">{{ row.staff_name ?? '系统' }}</template>
      </el-table-column>
      <el-table-column label="会话" width="70">
        <template #default="{ row }: { row: Call }">
          <el-button
            v-if="row.session_id"
            link
            type="primary"
            size="small"
            data-testid="token-call-session"
            @click="viewingSession = row.session_id"
          >
            查看
          </el-button>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
    </el-table>
    <el-pagination
      v-if="(result?.total ?? 0) > PAGE_SIZE"
      v-model:current-page="page"
      :page-size="PAGE_SIZE"
      :total="result?.total ?? 0"
      layout="total, prev, pager, next"
      class="pager"
    />
    <SessionDrawer :session-id="viewingSession" :staff-names="staffNames" @close="viewingSession = null" />
  </el-drawer>
</template>

<style scoped>
.filters {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
}

.select {
  width: 180px;
}

.select.narrow {
  width: 120px;
}

.summary {
  margin: 0 0 10px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.model {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
}

.muted {
  color: var(--el-text-color-secondary);
}

.pager {
  justify-content: flex-end;
  margin-top: 12px;
}
</style>
