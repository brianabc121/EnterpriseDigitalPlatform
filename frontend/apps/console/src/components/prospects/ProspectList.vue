<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { formatDateTime, api } from '../../api'
import {
  dueText,
  isView,
  isoDate,
  LEVEL_LABEL,
  LEVEL_TAG,
  LEVELS,
  SOURCE_LABEL,
  STATUS_LABEL,
  STATUS_TAG,
  VIEWS,
  type ProspectLevel,
  type ProspectPage,
  type ProspectSource,
  type ProspectSummary,
  type ProspectView,
} from '../../prospects'
import { useAuthStore } from '../../stores/auth'
import ProspectCreateDialog from './ProspectCreateDialog.vue'
import ProspectDrawer from './ProspectDrawer.vue'
import ProspectSettingsDialog from './ProspectSettingsDialog.vue'

/**
 * "客户"页面的"意向客户"页签（设计文档 §35.5）：顶部数字（跟进中、今天该跟进、已逾期、待确认、本月成交），
 * 按状态、等级、来源、跟进人筛选和按客户搜索，点一行打开意向详情。AI 唤醒的提醒链接带 view、follower，
 * 站内的链接可以带 id 直接打开一条。
 */
const PAGE_SIZE = 20
const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const page = ref<ProspectPage | null>(null)
const loading = ref(false)
const current = ref(1)
const filters = reactive({
  view: 'active' as ProspectView,
  level: '' as ProspectLevel | '',
  source: '' as ProspectSource | '',
  follower: '',
  q: '',
})
const staff = ref<Schemas['StaffOut'][]>([])
const openId = ref<string | null>(null)
const createOpen = ref(false)
const settingsOpen = ref(false)
const canEditSettings = ref(false)

const canPickFollower = computed(() => auth.can('customer:assign') && auth.can('staff:read'))
const today = computed(() => isoDate(new Date()))
const items = computed<ProspectSummary[]>(() => page.value?.items ?? [])
const counts = computed(() => page.value?.counts ?? {})
const followerIsMe = computed(() => !!filters.follower && filters.follower === auth.me?.id)

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/prospects', {
    params: {
      query: {
        view: filters.view,
        level: filters.level || undefined,
        source: filters.source || undefined,
        follower_id: filters.follower || undefined,
        q: filters.q.trim() || undefined,
        limit: PAGE_SIZE,
        offset: (current.value - 1) * PAGE_SIZE,
      },
    },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  page.value = data
}

function search(): void {
  current.value = 1
  void load()
}

function pickView(view: ProspectView): void {
  filters.view = view
  search()
}

async function loadMeta(): Promise<void> {
  const [settings, list] = await Promise.all([
    api.GET('/api/v1/prospects/settings'),
    canPickFollower.value ? api.GET('/api/v1/staff') : Promise.resolve(null),
  ])
  canEditSettings.value = settings.data?.can_edit ?? false
  staff.value = list?.data?.items.filter((s) => s.status === 'active') ?? []
}

/** 链接里的 view、follower、id（例如 AI 唤醒的提醒）。 */
function applyQuery(): void {
  const { view, follower, id } = route.query
  if (isView(view)) filters.view = view
  if (typeof follower === 'string') filters.follower = follower
  if (typeof id === 'string') openId.value = id
  if (view || follower || id) {
    void router.replace({ query: { ...route.query, view: undefined, follower: undefined, id: undefined } })
  }
}

function dueClass(row: ProspectSummary): string {
  if (row.overdue) return 'overdue'
  return row.due_today ? 'today' : ''
}

function changed(): void {
  void load()
}

watch(
  () => [route.query.view, route.query.follower, route.query.id],
  () => {
    if (route.query.view || route.query.follower || route.query.id) {
      applyQuery()
      search()
    }
  },
)

onMounted(async () => {
  applyQuery()
  await Promise.all([load(), loadMeta()])
})
</script>

<template>
  <div data-testid="prospect-list">
    <div v-if="page" class="tiles" data-testid="prospect-tiles">
      <button type="button" class="tile" data-testid="prospect-tile-active" @click="pickView('active')">
        <span class="label">跟进中</span>
        <span class="value">{{ counts.active ?? 0 }}</span>
      </button>
      <button type="button" class="tile warning" data-testid="prospect-tile-today" @click="pickView('today')">
        <span class="label">今天该跟进</span>
        <span class="value">{{ counts.today ?? 0 }}</span>
      </button>
      <button type="button" class="tile danger" data-testid="prospect-tile-overdue" @click="pickView('overdue')">
        <span class="label">已逾期</span>
        <span class="value">{{ counts.overdue ?? 0 }}</span>
      </button>
      <button
        type="button"
        class="tile"
        data-testid="prospect-tile-suggested"
        @click="pickView('suggested')"
      >
        <span class="label">待确认</span>
        <span class="value">{{ counts.suggested ?? 0 }}</span>
        <span class="hint">AI 建议的</span>
      </button>
      <button type="button" class="tile success" data-testid="prospect-tile-won" @click="pickView('won')">
        <span class="label">本月成交</span>
        <span class="value">{{ page.won_this_month }}</span>
      </button>
    </div>

    <div class="toolbar">
      <el-radio-group v-model="filters.view" size="small" data-testid="prospect-views" @change="search">
        <el-radio-button v-for="[value, label] in VIEWS" :key="value" :value="value">
          {{ label }}
          <span v-if="counts[value]" class="count">{{ counts[value] }}</span>
        </el-radio-button>
      </el-radio-group>
      <div class="grow" />
      <el-button data-testid="prospect-create-open" @click="createOpen = true">转入意向客户</el-button>
      <el-button v-if="canEditSettings" data-testid="prospect-settings-open" @click="settingsOpen = true">
        意向客户设置
      </el-button>
    </div>
    <div class="filters">
      <el-select
        v-model="filters.level"
        clearable
        placeholder="等级"
        size="small"
        class="filter"
        data-testid="prospect-filter-level"
        @change="search"
      >
        <el-option v-for="level in LEVELS" :key="level" :label="`意向${LEVEL_LABEL[level]}`" :value="level" />
      </el-select>
      <el-select
        v-model="filters.source"
        clearable
        placeholder="来源"
        size="small"
        class="filter"
        data-testid="prospect-filter-source"
        @change="search"
      >
        <el-option label="AI 转入" value="ai" />
        <el-option label="员工转入" value="staff" />
      </el-select>
      <el-select
        v-if="canPickFollower"
        v-model="filters.follower"
        clearable
        filterable
        placeholder="跟进人"
        size="small"
        class="filter"
        data-testid="prospect-filter-follower"
        @change="search"
      >
        <el-option v-for="s in staff" :key="s.id" :label="s.display_name" :value="s.id" />
      </el-select>
      <el-tag
        v-else-if="filters.follower"
        closable
        data-testid="prospect-filter-mine"
        @close="(filters.follower = ''), search()"
      >
        {{ followerIsMe ? '只看我跟进的' : '只看一位跟进人的' }}
      </el-tag>
      <el-input
        v-model="filters.q"
        size="small"
        clearable
        placeholder="客户名称、公司，或完整手机号"
        class="search"
        data-testid="prospect-search"
        @keyup.enter="search"
        @clear="search"
      />
    </div>

    <el-table
      v-loading="loading"
      :data="items"
      data-testid="prospect-table"
      empty-text="没有意向客户"
      row-class-name="row"
      @row-click="(row: ProspectSummary) => (openId = row.id)"
    >
      <el-table-column label="客户" min-width="150">
        <template #default="{ row }">
          <div class="name">{{ row.customer_name }}</div>
          <div v-if="row.customer_company" class="muted">{{ row.customer_company }}</div>
        </template>
      </el-table-column>
      <el-table-column label="等级" width="76">
        <template #default="{ row }">
          <el-tag :type="LEVEL_TAG[row.level as ProspectLevel]" size="small">
            {{ LEVEL_LABEL[row.level as ProspectLevel] }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="想要什么" min-width="180" show-overflow-tooltip>
        <template #default="{ row }">{{ row.interest ?? '—' }}</template>
      </el-table-column>
      <el-table-column label="顾虑" min-width="140" show-overflow-tooltip>
        <template #default="{ row }">{{ row.concerns ?? '—' }}</template>
      </el-table-column>
      <el-table-column label="来源" width="70">
        <template #default="{ row }">{{ SOURCE_LABEL[row.source as ProspectSource] }}</template>
      </el-table-column>
      <el-table-column label="跟进人" width="96">
        <template #default="{ row }">{{ row.follower_name ?? '—' }}</template>
      </el-table-column>
      <el-table-column label="最近跟进" width="150">
        <template #default="{ row }">
          {{ row.last_followed_at ? formatDateTime(row.last_followed_at) : '—' }}
        </template>
      </el-table-column>
      <el-table-column v-if="['active', 'today', 'overdue'].includes(filters.view)" label="下次跟进" width="130">
        <template #default="{ row }">
          <div v-if="row.next_follow_at" :class="['due', dueClass(row)]" :data-testid="`prospect-due-${row.id}`">
            <div>{{ dueText(row.next_follow_at, today) }}</div>
            <div class="muted">{{ row.next_follow_at }}</div>
          </div>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column v-else label="状态" width="110">
        <template #default="{ row }">
          <el-tag :type="STATUS_TAG[row.status as ProspectSummary['status']]" size="small">
            {{ STATUS_LABEL[row.status as ProspectSummary['status']] }}
          </el-tag>
          <div v-if="row.order_no" class="muted">{{ row.order_no }}</div>
        </template>
      </el-table-column>
    </el-table>
    <div class="page-footer">
      <el-pagination
        v-model:current-page="current"
        layout="total, prev, pager, next"
        :page-size="PAGE_SIZE"
        :total="page?.total ?? 0"
        @current-change="load"
      />
    </div>

    <ProspectDrawer :prospect-id="openId" @close="openId = null" @changed="changed" />
    <ProspectCreateDialog v-model="createOpen" @created="(p) => ((openId = p.id), changed())" />
    <ProspectSettingsDialog v-model="settingsOpen" />
  </div>
</template>

<style scoped>
.tiles {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 12px;
  margin-bottom: 12px;
}

.tile {
  display: flex;
  flex-direction: column;
  min-width: 0;
  padding: 12px 16px;
  text-align: left;
  font: inherit;
  cursor: pointer;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
}

.tile:hover {
  border-color: var(--el-color-primary);
}

.tile .label,
.tile .hint {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.tile .hint {
  margin-top: 2px;
  font-size: 12px;
}

.tile .value {
  margin-top: 4px;
  font-size: 22px;
  font-weight: 600;
}

.tile.warning .value {
  color: var(--el-color-warning);
}

.tile.danger .value {
  color: var(--el-color-danger);
}

.tile.success .value {
  color: var(--el-color-success);
}

.toolbar,
.filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}

.grow {
  flex: 1;
}

.count {
  margin-left: 4px;
  color: var(--el-text-color-secondary);
}

.filter {
  width: 120px;
}

.search {
  width: 240px;
}

.name {
  font-weight: 500;
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.due.today {
  color: var(--el-color-warning);
}

.due.overdue {
  color: var(--el-color-danger);
}

:deep(.row) {
  cursor: pointer;
}
</style>
