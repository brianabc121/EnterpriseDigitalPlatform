<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'

import { api, formatDateTime } from '../api'
import { EVENT_TYPE, IM_OP_TYPE } from '../labels'

/** 运维：处理失败进入死信的事件、IM 发件箱里最终失败或积压的操作（设计文档 §19.3）。 */
type DeadLetter = Schemas['DeadLetterOut']
type ImOp = Schemas['ImOpOut']

const tab = ref<'dead' | 'outbox'>('dead')
const tenants = ref<Schemas['TenantOut'][]>([])

// ---- 死信 ----
const dead = ref<DeadLetter[]>([])
const deadTotal = ref(0)
const deadNext = ref<string | null>(null)
const deadLoading = ref(false)
const deadSelected = ref<DeadLetter[]>([])
const deadFilters = reactive({ tenantId: '', type: '' })

async function loadDead(more = false): Promise<void> {
  deadLoading.value = true
  const { data, error } = await api.GET('/platform/v1/ops/dead-letters', {
    params: {
      query: {
        tenant_id: deadFilters.tenantId || undefined,
        type: deadFilters.type || undefined,
        before: more && deadNext.value ? deadNext.value : undefined,
        limit: 50,
      },
    },
  })
  deadLoading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  dead.value = more ? [...dead.value, ...data.items] : data.items
  deadTotal.value = data.total
  deadNext.value = data.next_before ?? null
}

async function actDead(action: 'retry' | 'discard', rows: DeadLetter[]): Promise<void> {
  if (!rows.length) return
  if (action === 'discard') {
    try {
      await ElMessageBox.confirm(`丢弃 ${rows.length} 个事件？丢弃后不能恢复。`, '丢弃事件', {
        type: 'warning',
        confirmButtonText: '丢弃',
      })
    } catch {
      return
    }
  }
  const path =
    action === 'retry' ? '/platform/v1/ops/dead-letters/retry' : '/platform/v1/ops/dead-letters/discard'
  const { data, error } = await api.POST(path, { body: { ids: rows.map((r) => r.id) } })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  const verb = action === 'retry' ? '重新处理' : '丢弃'
  ElMessage.success(data.skipped ? `已${verb} ${data.done} 个，${data.skipped} 个已不存在` : `已${verb} ${data.done} 个`)
  await loadDead()
}

// ---- IM 发件箱 ----
const ops = ref<ImOp[]>([])
const counts = ref<Schemas['ImOpCounts'] | null>(null)
const opsNext = ref<number | null>(null)
const opsLoading = ref(false)
const opsSelected = ref<ImOp[]>([])
const opsFilters = reactive({ status: 'failed' as 'failed' | 'stuck' | 'pending', tenantId: '', op: '' })

async function loadOps(more = false): Promise<void> {
  opsLoading.value = true
  const { data, error } = await api.GET('/platform/v1/ops/im-ops', {
    params: {
      query: {
        status: opsFilters.status,
        tenant_id: opsFilters.tenantId || undefined,
        op: opsFilters.op || undefined,
        before_id: more && opsNext.value ? opsNext.value : undefined,
        limit: 50,
      },
    },
  })
  opsLoading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ops.value = more ? [...ops.value, ...data.items] : data.items
  counts.value = data.counts
  opsNext.value = data.next_before_id ?? null
}

async function actOps(action: 'retry' | 'discard', rows: ImOp[]): Promise<void> {
  if (!rows.length) return
  if (action === 'discard') {
    try {
      await ElMessageBox.confirm(
        `放弃 ${rows.length} 个操作？同一会话后面的操作会继续执行。`,
        '放弃操作',
        { type: 'warning', confirmButtonText: '放弃' },
      )
    } catch {
      return
    }
  }
  const path = action === 'retry' ? '/platform/v1/ops/im-ops/retry' : '/platform/v1/ops/im-ops/discard'
  const { data, error } = await api.POST(path, { body: { ids: rows.map((r) => r.id) } })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  const verb = action === 'retry' ? '重试' : '放弃'
  ElMessage.success(
    data.skipped ? `已${verb} ${data.done} 个，${data.skipped} 个不能${verb}` : `已${verb} ${data.done} 个`,
  )
  await loadOps()
}

const retryableSelected = computed(() => opsSelected.value.filter((r) => r.retryable))
const pendingSelected = computed(() => opsSelected.value.filter((r) => r.status === 'pending'))

function detailText(detail: Record<string, unknown>): string {
  const text = JSON.stringify(detail)
  return text === '{}' ? '' : text
}

onMounted(async () => {
  const list = await api.GET('/platform/v1/tenants')
  tenants.value = list.data?.items ?? []
  await Promise.all([loadDead(), loadOps()])
})
</script>

<template>
  <div data-testid="ops-view">
    <div class="page-header">
      <h2>运维</h2>
      <span class="hint">事件处理重试 3 次仍失败会进入死信；IM 操作重试 12 次仍失败为最终失败。</span>
    </div>
    <el-tabs v-model="tab">
      <el-tab-pane :label="`事件死信（${deadTotal}）`" name="dead">
        <div class="toolbar">
          <div class="filters">
            <el-select v-model="deadFilters.tenantId" placeholder="全部租户" clearable filterable @change="loadDead()">
              <el-option v-for="t in tenants" :key="t.id" :label="`${t.name}（${t.code}）`" :value="t.id" />
            </el-select>
            <el-select v-model="deadFilters.type" placeholder="全部事件" clearable @change="loadDead()">
              <el-option v-for="(label, key) in EVENT_TYPE" :key="key" :label="label" :value="key" />
            </el-select>
          </div>
          <div>
            <el-button
              type="primary"
              :disabled="!deadSelected.length"
              data-testid="dead-retry"
              @click="actDead('retry', deadSelected)"
              >重新处理</el-button
            >
            <el-button :disabled="!deadSelected.length" data-testid="dead-discard" @click="actDead('discard', deadSelected)"
              >丢弃</el-button
            >
          </div>
        </div>
        <el-table
          v-loading="deadLoading"
          :data="dead"
          row-key="id"
          data-testid="dead-table"
          empty-text="没有死信"
          @selection-change="(rows: DeadLetter[]) => (deadSelected = rows)"
        >
          <el-table-column type="selection" width="40" />
          <el-table-column label="失败时间" width="170">
            <template #default="{ row }">{{ formatDateTime(row.failed_at) }}</template>
          </el-table-column>
          <el-table-column label="事件" width="160">
            <template #default="{ row }">
              {{ EVENT_TYPE[row.type] ?? row.type }}
              <div class="sub mono">{{ row.type }}</div>
            </template>
          </el-table-column>
          <el-table-column label="租户" width="110">
            <template #default="{ row }">{{ row.tenant_code ?? '-' }}</template>
          </el-table-column>
          <el-table-column label="错误" min-width="240">
            <template #default="{ row }">
              <code class="detail">{{ row.error }}</code>
            </template>
          </el-table-column>
          <el-table-column label="内容" min-width="200">
            <template #default="{ row }">
              <span class="sub mono">{{ row.key }}</span>
              <code class="detail">{{ JSON.stringify(row.data) }}</code>
            </template>
          </el-table-column>
        </el-table>
        <div class="more">
          <el-button v-if="deadNext" :loading="deadLoading" @click="loadDead(true)">加载更多</el-button>
        </div>
      </el-tab-pane>

      <el-tab-pane label="IM 发件箱" name="outbox">
        <div class="toolbar">
          <div class="filters">
            <el-radio-group v-model="opsFilters.status" data-testid="ops-status" @change="loadOps()">
              <el-radio-button value="failed">最终失败（{{ counts?.failed ?? 0 }}）</el-radio-button>
              <el-radio-button value="stuck">积压（{{ counts?.stuck ?? 0 }}）</el-radio-button>
              <el-radio-button value="pending">待执行（{{ counts?.pending ?? 0 }}）</el-radio-button>
            </el-radio-group>
            <el-select v-model="opsFilters.tenantId" placeholder="全部租户" clearable filterable @change="loadOps()">
              <el-option v-for="t in tenants" :key="t.id" :label="`${t.name}（${t.code}）`" :value="t.id" />
            </el-select>
            <el-select v-model="opsFilters.op" placeholder="全部操作" clearable @change="loadOps()">
              <el-option v-for="(label, key) in IM_OP_TYPE" :key="key" :label="label" :value="key" />
            </el-select>
          </div>
          <div>
            <el-button
              type="primary"
              :disabled="!retryableSelected.length"
              data-testid="ops-retry"
              @click="actOps('retry', retryableSelected)"
              >重试</el-button
            >
            <el-button
              :disabled="!pendingSelected.length"
              data-testid="ops-discard"
              @click="actOps('discard', pendingSelected)"
              >放弃</el-button
            >
          </div>
        </div>
        <el-table
          v-loading="opsLoading"
          :data="ops"
          row-key="id"
          data-testid="ops-table"
          empty-text="没有记录"
          @selection-change="(rows: ImOp[]) => (opsSelected = rows)"
        >
          <el-table-column type="selection" width="40" />
          <el-table-column label="创建时间" width="170">
            <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="130">
            <template #default="{ row }">
              {{ IM_OP_TYPE[row.op] ?? row.op }}
              <el-tag v-if="!row.retryable" size="small" type="info" disable-transitions>在线信令</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="租户" width="110" prop="tenant_code" />
          <el-table-column label="重试" width="70" prop="attempts" />
          <el-table-column label="下次执行" width="170">
            <template #default="{ row }">{{ row.status === 'pending' ? formatDateTime(row.next_attempt_at) : '-' }}</template>
          </el-table-column>
          <el-table-column label="错误" min-width="220">
            <template #default="{ row }">
              <code class="detail">{{ row.last_error }}</code>
            </template>
          </el-table-column>
          <el-table-column label="参数" min-width="200">
            <template #default="{ row }">
              <span class="sub mono">会话群 {{ row.room_id }}</span>
              <code class="detail">{{ detailText(row.detail) }}</code>
            </template>
          </el-table-column>
        </el-table>
        <div class="more">
          <el-button v-if="opsNext" :loading="opsLoading" @click="loadOps(true)">加载更多</el-button>
        </div>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.page-header {
  display: flex;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 8px;
}

.page-header h2 {
  margin: 0;
  font-size: 18px;
}

.hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.filters {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.detail {
  display: block;
  font-size: 12px;
  word-break: break-all;
}

.mono {
  font-family: monospace;
}

.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.more {
  margin-top: 12px;
  text-align: center;
}
</style>
