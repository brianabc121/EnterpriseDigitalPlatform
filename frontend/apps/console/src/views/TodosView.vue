<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { onMounted, ref, watch } from 'vue'

import { api, formatDateTime } from '../api'
import SessionDrawer from '../components/sessions/SessionDrawer.vue'
import { TODO_SOURCE } from '../labels'

type Status = Schemas['TodoStatus']

const PAGE_SIZE = 20
const items = ref<Schemas['TodoOut'][]>([])
const total = ref(0)
const page = ref(1)
const status = ref<Status | ''>('open')
const loading = ref(false)
const completing = ref<string | null>(null)
const viewing = ref<string | null>(null)

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/todos', {
    params: {
      query: {
        view: 'all',
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

function contact(todo: Schemas['TodoOut']): string {
  return todo.fields.find((f) => f.key === 'contact' || f.key === 'phone')?.value ?? '—'
}

async function complete(todo: Schemas['TodoOut']): Promise<void> {
  completing.value = todo.id
  const { data, error } = await api.POST('/api/v1/todos/{todo_id}/done', {
    params: { path: { todo_id: todo.id } },
    body: { result: '已处理', notify_customer: false },
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
      <h2>待办</h2>
      <el-radio-group v-model="status" size="small" data-testid="todo-status-filter">
        <el-radio-button value="open">待处理</el-radio-button>
        <el-radio-button value="done">已完成</el-radio-button>
        <el-radio-button value="">全部</el-radio-button>
      </el-radio-group>
    </div>
    <el-alert
      type="info"
      :closable="false"
      show-icon
      class="tip"
      title="访客留言、排队超时或非工作时间来访、员工新建的事项会在这里生成待办。处理后标记为已完成。"
    />
    <el-table v-loading="loading" :data="items" data-testid="todos-table" empty-text="暂无待办">
      <el-table-column prop="no" label="编号" width="150" />
      <el-table-column label="客户" min-width="110">
        <template #default="{ row }">{{ row.customer_name ?? '—' }}</template>
      </el-table-column>
      <el-table-column label="类型" width="110">
        <template #default="{ row }">
          <el-tag size="small" type="info">{{ row.type_name }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="内容" min-width="260">
        <template #default="{ row }">
          <div class="title">{{ row.title }}</div>
          <div class="content">{{ row.detail }}</div>
        </template>
      </el-table-column>
      <el-table-column label="来源" width="100">
        <template #default="{ row }">{{ TODO_SOURCE[row.source] ?? row.source }}</template>
      </el-table-column>
      <el-table-column label="联系方式" min-width="120">
        <template #default="{ row }">{{ contact(row) }}</template>
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
            v-if="row.status === 'open' || row.status === 'in_progress'"
            link
            type="primary"
            size="small"
            :loading="completing === row.id"
            data-testid="complete-todo"
            @click="complete(row)"
          >
            标记已处理
          </el-button>
          <span v-else-if="row.closed_at" class="muted"
            >已结束 {{ formatDateTime(row.closed_at) }}</span
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

.title {
  font-weight: 500;
}

.content {
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--el-text-color-regular);
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
