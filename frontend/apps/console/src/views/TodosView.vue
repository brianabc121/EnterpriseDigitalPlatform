<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api, formatDateTime } from '../api'
import PasswordExportDialog from '../components/shared/PasswordExportDialog.vue'
import TodoDrawer from '../components/todos/TodoDrawer.vue'
import TodoFormDialog from '../components/todos/TodoFormDialog.vue'
import { useAuthStore } from '../stores/auth'
import {
  dueState,
  PRIORITY,
  PRIORITY_TAG,
  REJECT_REASONS,
  TODO_SOURCE,
  TODO_STATUS,
  TODO_STATUS_TAG,
  todosChanged,
  VIEWS,
  type Todo,
  type TodoType,
  type View,
} from '../todos'

/**
 * 待办中心（设计文档 §24.9）：待确认（AI 生成，需要人工确认）、我的待办、待认领、我分派的、全部。
 * 站内信和企业微信应用消息里的链接带 view 和 id，打开后直接显示这条待办。
 */
const PAGE_SIZE = 20
const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const view = ref<View>('mine')
const items = ref<Todo[]>([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const counts = ref<Schemas['TodoCounts'] | null>(null)
const types = ref<TodoType[]>([])
const selected = ref<Todo[]>([])
const openId = ref<string | null>(null)
const creating = ref(false)
const batching = ref(false)
const batchReason = ref<Schemas['RejectReason']>('not_real')
const filters = reactive({
  typeId: '',
  status: '' as Schemas['TodoStatus'] | '',
  priority: '' as Schemas['Priority'] | '',
  source: '' as Schemas['TodoSource'] | '',
  due: '' as 'overdue' | 'today' | 'soon' | '',
  q: '',
})

const canCreate = computed(() => auth.can('todo:handle') || auth.can('todo:assign'))
const canExport = computed(() => auth.can('todo:export'))
const exporting = ref(false)

function exporter(password: string) {
  return api.POST('/api/v1/todos/export', {
    body: {
      password,
      view: view.value,
      status: statusFilter.value ? filters.status || null : null,
      type_id: filters.typeId || null,
      priority: filters.priority || null,
      source: filters.source || null,
      due: filters.due || null,
      q: filters.q.trim() || null,
    },
    parseAs: 'blob',
  })
}
const pending = computed(() => view.value === 'pending')
const statusFilter = computed(() => view.value === 'all' || view.value === 'assigned')

function badge(name: View): number {
  const c = counts.value
  if (!c) return 0
  if (name === 'pending') return c.pending
  if (name === 'mine') return c.mine
  if (name === 'pool') return c.pool
  return 0
}

async function loadCounts(): Promise<void> {
  const { data } = await api.GET('/api/v1/todos/counts')
  if (data) counts.value = data
}

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/todos', {
    params: {
      query: {
        view: view.value,
        status: statusFilter.value ? filters.status || undefined : undefined,
        type_id: filters.typeId || undefined,
        priority: filters.priority || undefined,
        source: filters.source || undefined,
        due: filters.due || undefined,
        q: filters.q.trim() || undefined,
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

async function refresh(): Promise<void> {
  await Promise.all([load(), loadCounts()])
}

function open(todo: Todo, column?: { type?: string }): void {
  // 勾选框所在的列只用于批量选择。
  if (column?.type === 'selection') return
  openId.value = todo.id
}

function close(): void {
  openId.value = null
  if (route.query.id) void router.replace({ query: { ...route.query, id: undefined } })
}

async function batch(action: 'confirm' | 'reject'): Promise<void> {
  if (!selected.value.length) return
  batching.value = true
  const { data, error } = await api.POST('/api/v1/todos/batch', {
    body: {
      ids: selected.value.map((t) => t.id),
      action,
      reason: action === 'reject' ? batchReason.value : null,
    },
  })
  batching.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  const verb = action === 'confirm' ? '确认' : '驳回'
  if (data.failed.length) {
    ElMessage.warning(`已${verb} ${data.done.length} 条，${data.failed.length} 条未能处理：${data.failed[0]?.error}`)
  } else {
    ElMessage.success(`已${verb} ${data.done.length} 条`)
  }
  if (data.done.length) todosChanged()
  await refresh()
}

function applyQuery(): void {
  const v = route.query.view
  if (typeof v === 'string' && VIEWS.some(([name]) => name === v)) view.value = v as View
  const id = route.query.id
  openId.value = typeof id === 'string' && id ? id : openId.value
  // 首页的数字和快捷入口：/todos?due=overdue（已逾期、今天到期）、/todos?new=1（新建待办）。
  // 只用一次，用过后从地址里去掉（之后可以在页面上改筛选条件）。
  const { due, new: create } = route.query
  if (due === undefined && create === undefined) return
  if (due === 'overdue' || due === 'today' || due === 'soon') filters.due = due
  if (create) creating.value = canCreate.value
  void router.replace({ query: { ...route.query, due: undefined, new: undefined } })
}

watch(view, () => {
  page.value = 1
  selected.value = []
  void load()
})
watch(
  () => [filters.typeId, filters.status, filters.priority, filters.source, filters.due],
  () => {
    page.value = 1
    void load()
  },
)
watch(() => route.query, applyQuery)

onMounted(async () => {
  applyQuery()
  const { data } = await api.GET('/api/v1/todo-types')
  types.value = data?.items ?? []
  await refresh()
})
</script>

<template>
  <div>
    <div class="page-header">
      <h2>待办</h2>
      <span>
        <el-button v-if="canExport" data-testid="todos-export" @click="exporting = true">导出</el-button>
        <el-button v-if="canCreate" type="primary" data-testid="new-todo" @click="creating = true"
          >新建待办</el-button
        >
      </span>
    </div>

    <el-tabs v-model="view" data-testid="todo-views">
      <el-tab-pane v-for="[name, label] in VIEWS" :key="name" :name="name">
        <template #label>
          <span :data-testid="`todo-view-${name}`">
            {{ label }}
            <el-badge
              v-if="badge(name)"
              :value="badge(name)"
              :type="name === 'pending' ? 'warning' : 'primary'"
              class="badge"
            />
          </span>
        </template>
      </el-tab-pane>
    </el-tabs>

    <el-alert
      v-if="pending"
      type="info"
      :closable="false"
      show-icon
      class="tip"
      title="AI 从对话中生成的待办需要人工确认：确认后进入待办列表并提醒处理人；不是真实需求的请驳回，驳回原因用于改进 AI。"
    />
    <div v-if="counts && view === 'mine' && (counts.overdue || counts.due_today)" class="summary">
      <el-tag v-if="counts.overdue" type="danger" effect="plain" @click="filters.due = 'overdue'"
        >已逾期 {{ counts.overdue }}</el-tag
      >
      <el-tag v-if="counts.due_today" type="warning" effect="plain" @click="filters.due = 'today'"
        >今日到期 {{ counts.due_today }}</el-tag
      >
    </div>

    <div class="filters">
      <el-select v-model="filters.typeId" clearable placeholder="类型" class="filter">
        <el-option v-for="t in types" :key="t.id" :label="t.name" :value="t.id" />
      </el-select>
      <el-select
        v-if="statusFilter"
        v-model="filters.status"
        clearable
        placeholder="状态"
        class="filter"
        data-testid="todo-status-filter"
      >
        <el-option
          v-for="(label, key) in TODO_STATUS"
          :key="key"
          :label="label"
          :value="key"
          :data-testid="`todo-status-${key}`"
        />
      </el-select>
      <el-select v-model="filters.priority" clearable placeholder="优先级" class="filter">
        <el-option v-for="(label, key) in PRIORITY" :key="key" :label="label" :value="key" />
      </el-select>
      <el-select v-model="filters.source" clearable placeholder="来源" class="filter">
        <el-option v-for="(label, key) in TODO_SOURCE" :key="key" :label="label" :value="key" />
      </el-select>
      <el-select v-if="!pending" v-model="filters.due" clearable placeholder="截止" class="filter">
        <el-option label="已逾期" value="overdue" />
        <el-option label="今日到期" value="today" />
        <el-option label="24 小时内到期" value="soon" />
      </el-select>
      <el-input
        v-model="filters.q"
        clearable
        placeholder="编号或标题"
        class="search"
        @keyup.enter="load"
        @clear="load"
      />
    </div>

    <div v-if="pending && selected.length" class="batch">
      <span>已选 {{ selected.length }} 条</span>
      <el-button type="primary" size="small" :loading="batching" data-testid="batch-confirm" @click="batch('confirm')"
        >批量确认</el-button
      >
      <el-select v-model="batchReason" size="small" class="reason">
        <el-option v-for="[value, label] in REJECT_REASONS" :key="value" :label="label" :value="value" />
      </el-select>
      <el-button size="small" :loading="batching" @click="batch('reject')">批量驳回</el-button>
    </div>

    <el-table
      v-loading="loading"
      :data="items"
      row-key="id"
      data-testid="todos-table"
      empty-text="暂无待办"
      class="table"
      @row-click="open"
      @selection-change="(rows: Todo[]) => (selected = rows)"
    >
      <el-table-column v-if="pending" type="selection" width="40" />
      <el-table-column prop="no" label="编号" width="150" />
      <el-table-column label="类型" width="120">
        <template #default="{ row }">
          <el-tag size="small" type="info">{{ row.type_name }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="事项" min-width="260">
        <template #default="{ row }">
          <div class="title">
            <el-tag
              v-if="row.priority !== 'normal'"
              size="small"
              :type="PRIORITY_TAG[row.priority]"
              effect="plain"
              >{{ PRIORITY[row.priority] }}</el-tag
            >
            {{ row.title }}
          </div>
          <div class="detail">{{ row.detail }}</div>
        </template>
      </el-table-column>
      <el-table-column label="客户" min-width="110">
        <template #default="{ row }">{{ row.customer_name ?? '—' }}</template>
      </el-table-column>
      <el-table-column v-if="pending" label="来源" width="120">
        <template #default="{ row }">
          {{ TODO_SOURCE[row.source] ?? row.source }}
          <div v-if="row.confidence !== null" class="muted">
            置信度 {{ Math.round(row.confidence * 100) }}%
          </div>
        </template>
      </el-table-column>
      <el-table-column :label="pending ? '确认人' : '处理人'" width="130">
        <template #default="{ row }">
          <span v-if="row.assignee_name">{{ row.assignee_name }}</span>
          <span v-else class="muted">{{ row.skill_group_name ?? '公共' }}待认领</span>
        </template>
      </el-table-column>
      <el-table-column :label="pending ? '建议截止' : '截止'" width="170">
        <template #default="{ row }">
          <span v-if="row.due_at" :class="dueState(row) ?? ''">{{ formatDateTime(row.due_at) }}</span>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column v-if="!pending" label="状态" width="100">
        <template #default="{ row }">
          <el-tag size="small" :type="TODO_STATUS_TAG[row.status]" data-testid="todo-row-status">{{
            TODO_STATUS[row.status]
          }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="创建" width="170">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
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

    <TodoDrawer :todo-id="openId" @close="close" @changed="refresh" />
    <TodoFormDialog v-model="creating" @created="refresh" />
    <PasswordExportDialog
      v-model="exporting"
      title="导出待办"
      :hint="`导出当前视图和筛选条件下的待办（CSV）。敏感字段${
        auth.can('customer:view_sensitive') ? '导出完整内容' : '导出掩码'
      }；导出操作会记入操作日志。`"
      :filename="`todos-${new Date().toISOString().slice(0, 10)}.csv`"
      :exporter="exporter"
    />
  </div>
</template>

<style scoped>
.tip {
  margin-bottom: 12px;
}

.summary {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}

.summary .el-tag {
  cursor: pointer;
}

.filters {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}

.filter {
  width: 140px;
}

.search {
  width: 200px;
}

.batch {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.reason {
  width: 140px;
}

.badge {
  margin-left: 4px;
}

.table :deep(.el-table__row) {
  cursor: pointer;
}

.title {
  font-weight: 500;
}

.detail {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.overdue {
  color: var(--el-color-danger);
  font-weight: 500;
}

.soon {
  color: var(--el-color-warning);
}
</style>
