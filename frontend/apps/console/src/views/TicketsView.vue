<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { onMounted, ref, watch } from 'vue'

import { api, formatDateTime } from '../api'
import SessionDrawer from '../components/sessions/SessionDrawer.vue'
import { TICKET_SOURCE } from '../labels'

type Status = Schemas['TicketStatus']

const PAGE_SIZE = 20
const items = ref<Schemas['TicketOut'][]>([])
const total = ref(0)
const page = ref(1)
const status = ref<Status | ''>('open')
const loading = ref(false)
const completing = ref<string | null>(null)
const viewing = ref<string | null>(null)

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/tickets', {
    params: {
      query: {
        status: status.value || undefined,
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

async function complete(ticket: Schemas['TicketOut']): Promise<void> {
  completing.value = ticket.id
  const { data, error } = await api.POST('/api/v1/tickets/{ticket_id}/done', {
    params: { path: { ticket_id: ticket.id } },
  })
  completing.value = null
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已标记为已处理')
  await load()
}

watch(status, () => {
  page.value = 1
  void load()
})

onMounted(load)
</script>

<template>
  <div>
    <div class="page-header">
      <h2>留言</h2>
      <el-radio-group v-model="status" size="small" data-testid="ticket-status-filter">
        <el-radio-button value="open">待处理</el-radio-button>
        <el-radio-button value="done">已处理</el-radio-button>
        <el-radio-button value="">全部</el-radio-button>
      </el-radio-group>
    </div>
    <el-alert
      type="info"
      :closable="false"
      show-icon
      class="tip"
      title="访客在 Widget 里留言、排队超时或非工作时间来访时，会在这里生成留言。联系客户后标记为已处理。"
    />
    <el-table v-loading="loading" :data="items" data-testid="tickets-table" empty-text="暂无留言">
      <el-table-column prop="customer_display_name" label="客户" min-width="120" />
      <el-table-column label="来源" width="110">
        <template #default="{ row }">
          <el-tag size="small" type="info">{{ TICKET_SOURCE[row.source] ?? row.source }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="内容" min-width="260">
        <template #default="{ row }">
          <div class="content">{{ row.content }}</div>
        </template>
      </el-table-column>
      <el-table-column label="联系方式" min-width="130">
        <template #default="{ row }">{{ row.contact ?? '—' }}</template>
      </el-table-column>
      <el-table-column label="时间" width="170">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="" width="170">
        <template #default="{ row }">
          <el-button
            v-if="row.session_id"
            link
            type="primary"
            size="small"
            @click="viewing = row.session_id"
          >
            会话
          </el-button>
          <el-button
            v-if="row.status === 'open'"
            link
            type="primary"
            size="small"
            :loading="completing === row.id"
            data-testid="complete-ticket"
            @click="complete(row)"
          >
            标记已处理
          </el-button>
          <span v-else class="muted"
            >已处理 {{ row.closed_at ? formatDateTime(row.closed_at) : '' }}</span
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
    <SessionDrawer :session-id="viewing" :staff-names="new Map()" @close="viewing = null" />
  </div>
</template>

<style scoped>
.tip {
  margin-bottom: 16px;
}

.content {
  white-space: pre-wrap;
  word-break: break-word;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
