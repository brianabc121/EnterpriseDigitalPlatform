<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref, watch } from 'vue'

import { api, formatDateTime } from '../api'
import SessionDrawer from '../components/sessions/SessionDrawer.vue'
import { CLOSE_REASON, SESSION_STATUS, SESSION_STATUS_TAG } from '../labels'
import { useAuthStore } from '../stores/auth'

type Status = Schemas['SessionStatus'] | 'open'

const PAGE_SIZE = 20
const FILTERS: { value: Status | ''; label: string }[] = [
  { value: '', label: '全部' },
  { value: 'open', label: '未结束' },
  { value: 'ai_serving', label: 'AI 接待' },
  { value: 'queued', label: '排队中' },
  { value: 'human_serving', label: '接待中' },
  { value: 'closed', label: '已结束' },
]

const auth = useAuthStore()
const items = ref<Schemas['SessionOut'][]>([])
const total = ref(0)
const page = ref(1)
const status = ref<Status | ''>('')
const mine = ref(false)
const loading = ref(false)
const viewing = ref<string | null>(null)
const staffNames = ref(new Map<string, string>())

const seesOthers = computed(() => auth.can('session:read_all') || auth.can('session:read_team'))

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/sessions', {
    params: {
      query: {
        status: status.value || undefined,
        mine: mine.value,
        limit: PAGE_SIZE,
        offset: (page.value - 1) * PAGE_SIZE,
      },
    },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  items.value = data.items
  total.value = data.total
}

async function loadStaff(): Promise<void> {
  if (!auth.can('staff:read')) return
  const { data } = await api.GET('/api/v1/staff')
  if (data) staffNames.value = new Map(data.items.map((s) => [s.id, s.display_name]))
}

watch([status, mine], () => {
  page.value = 1
  void load()
})

onMounted(() => {
  void load()
  void loadStaff()
})
</script>

<template>
  <div>
    <div class="page-header">
      <h2>会话记录</h2>
      <div class="filters">
        <el-radio-group v-model="status" size="small" data-testid="session-status-filter">
          <el-radio-button v-for="f in FILTERS" :key="f.value" :value="f.value">
            {{ f.label }}
          </el-radio-button>
        </el-radio-group>
        <el-checkbox v-if="seesOthers" v-model="mine" class="mine">只看我接待的</el-checkbox>
      </div>
    </div>
    <el-table
      v-loading="loading"
      :data="items"
      data-testid="sessions-table"
      empty-text="暂无会话"
      class="clickable"
      @row-click="(row: Schemas['SessionOut']) => (viewing = row.id)"
    >
      <el-table-column prop="customer_display_name" label="客户" min-width="140" />
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag size="small" :type="SESSION_STATUS_TAG[row.status]">
            {{ SESSION_STATUS[row.status] ?? row.status }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="接待坐席" min-width="110">
        <template #default="{ row }">{{ row.assignee_display_name ?? '—' }}</template>
      </el-table-column>
      <el-table-column label="开始时间" width="170">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="结束" min-width="170">
        <template #default="{ row }">
          <template v-if="row.closed_at">
            {{ formatDateTime(row.closed_at) }}
            <span class="muted">{{ CLOSE_REASON[row.close_reason ?? ''] ?? '' }}</span>
          </template>
          <template v-else>—</template>
        </template>
      </el-table-column>
      <el-table-column label="满意度" width="150">
        <template #default="{ row }">
          <el-rate v-if="row.csat" :model-value="row.csat" disabled size="small" />
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="" width="70">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click.stop="viewing = row.id"
            >查看</el-button
          >
        </template>
      </el-table-column>
    </el-table>
    <div class="page-footer">
      <el-pagination
        v-model:current-page="page"
        :page-size="PAGE_SIZE"
        :total="total"
        layout="total, prev, pager, next"
        @current-change="load"
      />
    </div>
    <SessionDrawer :session-id="viewing" :staff-names="staffNames" @close="viewing = null" />
  </div>
</template>

<style scoped>
.filters {
  display: flex;
  align-items: center;
  gap: 16px;
}

.muted {
  margin-left: 6px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.clickable :deep(.el-table__row) {
  cursor: pointer;
}
</style>
