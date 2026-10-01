<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api } from '../api'
import TaskDrawer from '../components/tasks/TaskDrawer.vue'
import TaskFormDialog from '../components/tasks/TaskFormDialog.vue'
import { useAuthStore } from '../stores/auth'
import {
  dueState,
  dueText,
  MINE_FILTERS,
  mineQuery,
  PRIORITY_LABEL,
  PRIORITY_TAG,
  TASK_SOURCE,
  TASK_STATUS,
  TASK_STATUS_TAG,
  TASKS_CHANGED,
  type MineFilter,
  type Task,
  type TaskView,
} from '../tasks'

/**
 * 个人待办（设计文档 §27.2）：我的（今天到期、已逾期、未完成、已完成）、我交办的、全员（管理员：每人一行
 * 的汇总，点开看这个人的事项）。站内信和助理里的链接带 id，打开后直接显示这条事项。
 */
const PAGE_SIZE = 20
const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const canReadAll = computed(() => auth.can('task:read_all'))
const canAssign = computed(() => auth.can('task:assign'))
const tab = ref<TaskView>('mine')
const filter = ref<MineFilter>('open')
const items = ref<Task[]>([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const counts = ref<Schemas['TaskCounts'] | null>(null)
const overview = ref<Schemas['TaskOverviewRow'][]>([])
const chosen = reactive({ ownerId: '' as string, ownerName: '' as string })
const q = ref('')
const openId = ref<string | null>(null)
const creating = ref(false)

const subtitle = computed(() => {
  const c = counts.value
  if (!c) return ''
  return `未完成 ${c.open} · 今天到期 ${c.due_today} · 已逾期 ${c.overdue}`
})

async function loadCounts(): Promise<void> {
  const { data } = await api.GET('/api/v1/tasks/counts')
  if (data) counts.value = data
}

async function loadOverview(): Promise<void> {
  if (!canReadAll.value) return
  const { data } = await api.GET('/api/v1/tasks/overview')
  if (data) overview.value = data.items
}

async function load(): Promise<void> {
  loading.value = true
  const base = tab.value === 'mine' ? mineQuery(filter.value) : {}
  const { data, error } = await api.GET('/api/v1/tasks', {
    params: {
      query: {
        view: tab.value,
        owner_id: tab.value === 'all' && chosen.ownerId ? chosen.ownerId : undefined,
        status: tab.value === 'mine' ? base.status : undefined,
        due: tab.value === 'mine' ? base.due : undefined,
        q: q.value.trim() || undefined,
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
  await Promise.all([load(), loadCounts(), loadOverview()])
}

function pick(row: Schemas['TaskOverviewRow']): void {
  chosen.ownerId = row.staff_id
  chosen.ownerName = row.name
  page.value = 1
  void load()
}

function clearPick(): void {
  chosen.ownerId = ''
  chosen.ownerName = ''
  page.value = 1
  void load()
}

function onChanged(): void {
  void refresh()
}

watch([tab, filter], () => {
  page.value = 1
  void load()
})

onMounted(() => {
  if (typeof route.query.id === 'string') openId.value = route.query.id
  void refresh()
  window.addEventListener(TASKS_CHANGED, onChanged)
})

function close(): void {
  openId.value = null
  if (route.query.id) void router.replace({ query: { ...route.query, id: undefined } })
}
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h2>个人待办</h2>
        <div class="muted" data-testid="task-summary">{{ subtitle }}</div>
      </div>
      <div class="header-actions">
        <router-link
          v-if="counts?.work_todos"
          :to="{ path: '/todos', query: { view: 'mine' } }"
          class="work-link"
          data-testid="work-todos-link"
        >
          另有 {{ counts.work_todos }} 条分派给我的客户待办
        </router-link>
        <el-button type="primary" data-testid="task-create" @click="creating = true">
          {{ canAssign ? '新建 / 交办' : '新建' }}
        </el-button>
      </div>
    </div>
    <el-tabs v-model="tab" data-testid="task-tabs">
      <el-tab-pane label="我的" name="mine" />
      <el-tab-pane v-if="canAssign" label="我交办的" name="assigned" />
      <el-tab-pane v-if="canReadAll" label="全员" name="all" />
    </el-tabs>
    <div class="filters">
      <el-radio-group v-if="tab === 'mine'" v-model="filter" size="small" data-testid="task-filter">
        <el-radio-button v-for="[value, label] in MINE_FILTERS" :key="value" :value="value">
          {{ label }}
        </el-radio-button>
      </el-radio-group>
      <el-tag
        v-if="tab === 'all' && chosen.ownerId"
        closable
        data-testid="task-owner-filter"
        @close="clearPick"
      >
        {{ chosen.ownerName }}
      </el-tag>
      <el-input
        v-model="q"
        clearable
        placeholder="按编号或标题搜索"
        style="width: 220px"
        data-testid="task-search"
        @change="load"
      />
    </div>
    <el-table
      v-if="tab === 'all' && !chosen.ownerId"
      :data="overview"
      data-testid="task-overview"
      class="clickable"
      @row-click="pick"
    >
      <el-table-column prop="name" label="员工" min-width="160" />
      <el-table-column prop="open" label="未完成" width="100" />
      <el-table-column prop="due_today" label="今天到期" width="100" />
      <el-table-column label="已逾期" width="100">
        <template #default="{ row }">
          <span :class="{ overdue: row.overdue }">{{ row.overdue }}</span>
        </template>
      </el-table-column>
    </el-table>
    <template v-else>
      <el-table
        v-loading="loading"
        :data="items"
        data-testid="task-table"
        class="clickable"
        empty-text="没有事项"
        @row-click="(row: Task) => (openId = row.id)"
      >
        <el-table-column label="事项" min-width="260">
          <template #default="{ row }">
            <div class="title-cell">
              <span class="no">{{ row.no }}</span>
              <span>{{ row.title }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column v-if="tab !== 'mine'" label="负责人" width="120">
          <template #default="{ row }">{{ row.owner_name ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="截止" width="140">
          <template #default="{ row }">
            <span :class="{ overdue: dueState(row) === 'overdue', soon: dueState(row) === 'soon' }">
              {{ dueText(row.due_at) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="优先级" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="PRIORITY_TAG[row.priority]" effect="plain">
              {{ PRIORITY_LABEL[row.priority] }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="TASK_STATUS_TAG[row.status]">{{ TASK_STATUS[row.status] }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="来源" width="100">
          <template #default="{ row }">{{ TASK_SOURCE[row.source] }}</template>
        </el-table-column>
      </el-table>
      <div v-if="total > PAGE_SIZE" class="page-footer">
        <el-pagination
          v-model:current-page="page"
          :page-size="PAGE_SIZE"
          :total="total"
          layout="prev, pager, next"
          @current-change="load"
        />
      </div>
    </template>
    <TaskFormDialog v-model="creating" :owner-id="tab === 'all' ? chosen.ownerId : null" @created="refresh" />
    <TaskDrawer :task-id="openId" @close="close" @changed="refresh" />
  </div>
</template>

<style scoped>
.header-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.work-link {
  font-size: 13px;
}

.filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}

.title-cell {
  display: flex;
  gap: 8px;
}

.no {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.overdue {
  color: var(--el-color-danger);
}

.soon {
  color: var(--el-color-warning);
}

.clickable :deep(.el-table__row) {
  cursor: pointer;
}
</style>
