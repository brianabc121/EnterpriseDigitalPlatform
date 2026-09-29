<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, reactive, ref } from 'vue'

import { api, formatDateTime } from '../../api'
import { TRANSFER_REQUEST_STATUS } from '../../labels'
import { useAuthStore } from '../../stores/auth'

type Request = Schemas['TransferRequestOut']
type Status = 'pending' | 'approved' | 'rejected' | 'cancelled'

const open = defineModel<boolean>({ required: true })
const emit = defineEmits<{ changed: [] }>()

const auth = useAuthStore()
const canDecide = computed(() => auth.can('customer:assign'))
const items = ref<Request[]>([])
const status = ref<Status | ''>('pending')
const loading = ref(false)
const busy = ref<string | null>(null)

const STATUS_TAG: Record<string, 'warning' | 'success' | 'danger' | 'info'> = {
  pending: 'warning',
  approved: 'success',
  rejected: 'danger',
  cancelled: 'info',
}

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/customers/transfer-requests', {
    params: { query: { status: status.value || undefined } },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  items.value = data.items
}

/** 正在审批的申请：填写说明，批准时可以同步企业微信的添加人。 */
const deciding = ref<{ row: Request; decision: 'approve' | 'reject' } | null>(null)
const decision = reactive({ note: '', syncWecom: false })
const deciderOpen = computed({
  get: () => deciding.value !== null,
  set: (value: boolean) => {
    if (!value) deciding.value = null
  },
})

function decide(row: Request, kind: 'approve' | 'reject'): void {
  Object.assign(decision, { note: '', syncWecom: false })
  deciding.value = { row, decision: kind }
}

async function confirmDecision(): Promise<void> {
  const current = deciding.value
  if (!current) return
  const done = await act(current.row, current.decision, {
    note: decision.note.trim() || null,
    sync_wecom: current.decision === 'approve' && decision.syncWecom,
  })
  if (done) deciding.value = null
}

async function cancel(row: Request): Promise<void> {
  await act(row, 'cancel')
}

async function act(
  row: Request,
  action: 'approve' | 'reject' | 'cancel',
  body?: Schemas['TransferDecision'],
): Promise<boolean> {
  busy.value = row.id
  const params = { path: { request_id: row.id } }
  const { data, error } =
    action === 'cancel'
      ? await api.POST('/api/v1/customers/transfer-requests/{request_id}/cancel', { params })
      : await api.POST(`/api/v1/customers/transfer-requests/{request_id}/${action}`, {
          params,
          body: body ?? { sync_wecom: false },
        })
  busy.value = null
  if (!data) {
    ElMessage.error(errorMessage(error))
    return false
  }
  ElMessage.success(
    { approve: '已批准，客户已转移', reject: '已驳回', cancel: '已撤回申请' }[action],
  )
  emit('changed')
  await load()
  return true
}
</script>

<template>
  <el-drawer v-model="open" title="客户转移申请" size="820px" @open="load">
    <div class="toolbar">
      <el-radio-group
        v-model="status"
        size="small"
        data-testid="transfer-request-status"
        @change="load"
      >
        <el-radio-button value="pending">待审批</el-radio-button>
        <el-radio-button value="approved">已批准</el-radio-button>
        <el-radio-button value="rejected">已驳回</el-radio-button>
        <el-radio-button value="">全部</el-radio-button>
      </el-radio-group>
      <span class="muted">{{ canDecide ? '坐席提交的申请由您审批' : '只显示您提交的申请' }}</span>
    </div>
    <el-table
      v-loading="loading"
      :data="items"
      data-testid="transfer-requests"
      empty-text="暂无申请"
    >
      <el-table-column label="时间" width="150">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column prop="customer_name" label="客户" min-width="80" />
      <el-table-column label="归属变更" min-width="110">
        <template #default="{ row }">
          {{ row.from_owner_name ?? '无' }} → {{ row.to_owner_name ?? '—' }}
        </template>
      </el-table-column>
      <el-table-column label="申请人与原因" min-width="150">
        <template #default="{ row }">
          <div>{{ row.requested_by_name ?? '—' }}</div>
          <small class="muted">{{ row.reason }}</small>
        </template>
      </el-table-column>
      <el-table-column label="状态" min-width="90">
        <template #default="{ row }">
          <el-tag :type="STATUS_TAG[row.status] ?? 'info'" size="small" disable-transitions>
            {{ TRANSFER_REQUEST_STATUS[row.status] ?? row.status }}
          </el-tag>
          <div v-if="row.decided_by_name" class="muted">
            {{ row.decided_by_name
            }}<template v-if="row.decision_note">：{{ row.decision_note }}</template>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="" width="110" fixed="right">
        <template #default="{ row }">
          <template v-if="row.status === 'pending'">
            <template v-if="canDecide">
              <el-button
                link
                type="primary"
                size="small"
                :loading="busy === row.id"
                data-testid="approve-transfer-request"
                @click="decide(row, 'approve')"
              >
                批准
              </el-button>
              <el-button
                link
                type="danger"
                size="small"
                data-testid="reject-transfer-request"
                @click="decide(row, 'reject')"
              >
                驳回
              </el-button>
            </template>
            <el-button
              v-else-if="row.requested_by === auth.me?.id"
              link
              size="small"
              :loading="busy === row.id"
              data-testid="cancel-transfer-request"
              @click="cancel(row)"
            >
              撤回
            </el-button>
          </template>
        </template>
      </el-table-column>
    </el-table>
    <el-dialog
      v-model="deciderOpen"
      :title="deciding?.decision === 'approve' ? '批准转移' : '驳回申请'"
      width="420px"
      append-to-body
      data-testid="transfer-decision-dialog"
    >
      <p v-if="deciding" class="summary">
        {{ deciding.row.customer_name }}：{{ deciding.row.from_owner_name ?? '无' }} →
        {{ deciding.row.to_owner_name ?? '—' }}
      </p>
      <el-form label-width="72px" @submit.prevent="confirmDecision">
        <el-form-item label="说明">
          <el-input
            v-model="decision.note"
            maxlength="500"
            :placeholder="deciding?.decision === 'approve' ? '可选' : '驳回原因，申请人可以看到'"
            data-testid="transfer-decision-note"
          />
        </el-form-item>
        <el-form-item v-if="deciding?.decision === 'approve'" label="企业微信">
          <el-checkbox v-model="decision.syncWecom">
            同时变更企业微信里的添加人（在职继承）
          </el-checkbox>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="deciding = null">取消</el-button>
        <el-button
          :type="deciding?.decision === 'approve' ? 'primary' : 'danger'"
          :loading="busy !== null"
          data-testid="transfer-decision-submit"
          @click="confirmDecision"
        >
          {{ deciding?.decision === 'approve' ? '批准' : '驳回' }}
        </el-button>
      </template>
    </el-dialog>
  </el-drawer>
</template>

<style scoped>
.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}

.summary {
  margin-top: 0;
  color: var(--el-text-color-secondary);
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
