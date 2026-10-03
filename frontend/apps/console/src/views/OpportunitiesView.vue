<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api } from '../api'
import { downloadBlob } from '../download'
import OpportunityBoard from '../components/opportunities/OpportunityBoard.vue'
import OpportunityCreateDialog from '../components/opportunities/OpportunityCreateDialog.vue'
import OpportunityDrawer from '../components/opportunities/OpportunityDrawer.vue'
import OpportunityList from '../components/opportunities/OpportunityList.vue'
import OpportunitySettingsDialog from '../components/opportunities/OpportunitySettingsDialog.vue'
import {
  amountShort,
  emptyFilters,
  isMode,
  isoDate,
  isView,
  LEVEL_LABEL,
  LEVELS,
  monthOptions,
  SOURCE_TEXT,
  SOURCES,
  VIEWS,
  type OpportunityBoard as Board,
  type OpportunityPage,
  type OpportunityStats,
  type OpportunityView,
  type PageMode,
  type Stage,
} from '../opportunities'
import { useAuthStore } from '../stores/auth'

/**
 * 商机页面（设计文档 §40.8）：看板（默认）和列表共用顶部数字、快捷视图和筛选；新建商机、商机设置；点卡片
 * 或一行打开详情。链接可以带 view、owner（AI 唤醒的提醒）、id（站内的链接直接打开一条）、mode=list。
 */
const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const mode = ref<PageMode>('board')
const filters = reactive(emptyFilters())
const stats = ref<OpportunityStats | null>(null)
const stages = ref<Stage[]>([])
const staff = ref<Schemas['StaffOut'][]>([])
const canEditSettings = ref(false)
const amountVisible = ref(false)
const refreshKey = ref(0)
const openId = ref<string | null>(null)
const createOpen = ref(false)
const settingsOpen = ref(false)

const canCreate = computed(() => auth.can('opportunity:manage'))
const canPickOwner = computed(() => auth.can('opportunity:read_all') && auth.can('staff:read'))
const canExport = computed(() => auth.can('opportunity:export'))
const exporting = ref(false)
const today = computed(() => isoDate(new Date()))
const months = computed(() => monthOptions(today.value))
const ownerIsMe = computed(() => !!filters.owner && filters.owner === auth.me?.id)

async function loadStats(): Promise<void> {
  const { data } = await api.GET('/api/v1/opportunities/stats')
  if (data) stats.value = data
}

async function loadMeta(): Promise<void> {
  const [settings, list] = await Promise.all([
    api.GET('/api/v1/opportunities/settings'),
    canPickOwner.value ? api.GET('/api/v1/staff') : Promise.resolve(null),
  ])
  canEditSettings.value = settings.data?.can_edit ?? false
  stages.value = settings.data?.stages ?? []
  staff.value = list?.data?.items.filter((s) => s.status === 'active') ?? []
}

function search(): void {
  refreshKey.value += 1
}

function pickView(view: OpportunityView): void {
  filters.view = view
  search()
}

function setMode(value: PageMode): void {
  mode.value = value
  void router.replace({ query: { ...route.query, mode: value === 'list' ? 'list' : undefined } })
}

/** 链接里的 view、owner、id、mode（例如 AI 唤醒的提醒和站内信）。 */
function applyQuery(): void {
  const { view, owner, id, mode: wanted } = route.query
  if (isView(view)) filters.view = view
  if (typeof owner === 'string') filters.owner = owner
  if (typeof id === 'string') openId.value = id
  if (isMode(wanted)) mode.value = wanted
  if (view || owner || id) {
    void router.replace({ query: { ...route.query, view: undefined, owner: undefined, id: undefined } })
  }
}

/** 导出查看范围内、符合当前视图和筛选条件的商机（CSV，`opportunity:export`）。 */
async function exportCsv(): Promise<void> {
  exporting.value = true
  const { data, error } = await api.GET('/api/v1/opportunities/export', {
    params: {
      query: {
        view: filters.view,
        stage_id: filters.stage || undefined,
        owner_id: filters.owner || undefined,
        q: filters.q.trim() || undefined,
      },
    },
    parseAs: 'blob',
  })
  exporting.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  downloadBlob(data as Blob, `opportunities-${today.value.replace(/-/g, '')}.csv`)
  ElMessage.success('已导出')
}

function onLoaded(data: OpportunityPage | Board): void {
  amountVisible.value = data.amount_visible
}

/** 详情里改了东西：数字和看板 / 列表都刷新。 */
function changed(): void {
  void loadStats()
  search()
}

function onSettingsSaved(): void {
  void loadMeta()
  changed()
}

watch(
  () => [route.query.view, route.query.owner, route.query.id],
  () => {
    if (route.query.view || route.query.owner || route.query.id) {
      applyQuery()
      search()
    }
  },
)

onMounted(async () => {
  applyQuery()
  await Promise.all([loadStats(), loadMeta()])
})
</script>

<template>
  <div data-testid="opp-page">
    <div class="page-header">
      <h2>商机</h2>
      <div class="toolbar">
        <el-radio-group
          v-model="mode"
          size="small"
          data-testid="opp-mode"
          @change="(value: string | number | boolean | undefined) => setMode(value === 'list' ? 'list' : 'board')"
        >
          <el-radio-button value="board">看板</el-radio-button>
          <el-radio-button value="list">列表</el-radio-button>
        </el-radio-group>
        <el-button v-if="canCreate" type="primary" data-testid="opp-create-open" @click="createOpen = true">
          新建商机
        </el-button>
        <el-button v-if="canExport" :loading="exporting" data-testid="opp-export" @click="exportCsv">导出</el-button>
        <el-button v-if="canEditSettings" data-testid="opp-settings-open" @click="settingsOpen = true">
          商机设置
        </el-button>
      </div>
    </div>

    <div v-if="stats" class="tiles" data-testid="opp-tiles">
      <button type="button" class="tile" data-testid="opp-tile-active" @click="pickView('active')">
        <span class="label">进行中</span>
        <span class="value">{{ stats.active }}</span>
        <span class="hint">我负责的 {{ stats.mine }}</span>
      </button>
      <button type="button" class="tile warning" data-testid="opp-tile-today" @click="pickView('today')">
        <span class="label">今天该跟进</span>
        <span class="value">{{ stats.today }}</span>
        <span class="hint">本周 {{ stats.week }}</span>
      </button>
      <button type="button" class="tile danger" data-testid="opp-tile-overdue" @click="pickView('overdue')">
        <span class="label">已逾期</span>
        <span class="value">{{ stats.overdue }}</span>
      </button>
      <button type="button" class="tile danger" data-testid="opp-tile-stale" @click="pickView('stale')">
        <span class="label">停滞</span>
        <span class="value">{{ stats.stale }}</span>
        <span class="hint">超过阶段的停滞天数</span>
      </button>
      <button type="button" class="tile" data-testid="opp-tile-suggested" @click="pickView('suggested')">
        <span class="label">待确认</span>
        <span class="value">{{ stats.suggested }}</span>
        <span class="hint">AI 建议的</span>
      </button>
      <button type="button" class="tile success" data-testid="opp-tile-won" @click="pickView('won')">
        <span class="label">本月赢单</span>
        <span class="value">{{ stats.won_this_month }}</span>
        <span v-if="stats.won_amount_this_month !== null && stats.won_amount_this_month !== undefined" class="hint">
          {{ amountShort(stats.won_amount_this_month) }}
        </span>
      </button>
    </div>

    <div class="views">
      <el-radio-group v-model="filters.view" size="small" data-testid="opp-views" @change="search">
        <el-radio-button v-for="[value, label] in VIEWS" :key="value" :value="value">{{ label }}</el-radio-button>
      </el-radio-group>
    </div>
    <div class="filters">
      <el-select
        v-model="filters.level"
        clearable
        placeholder="等级"
        size="small"
        class="filter"
        data-testid="opp-filter-level"
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
        data-testid="opp-filter-source"
        @change="search"
      >
        <el-option v-for="source in SOURCES" :key="source" :label="SOURCE_TEXT[source]" :value="source" />
      </el-select>
      <el-select
        v-if="canPickOwner"
        v-model="filters.owner"
        clearable
        filterable
        placeholder="负责人"
        size="small"
        class="filter"
        data-testid="opp-filter-owner"
        @change="search"
      >
        <el-option v-for="s in staff" :key="s.id" :label="s.display_name" :value="s.id" />
      </el-select>
      <el-tag v-else-if="filters.owner" closable data-testid="opp-filter-mine" @close="(filters.owner = ''), search()">
        {{ ownerIsMe ? '只看我负责的' : '只看一位负责人的' }}
      </el-tag>
      <template v-if="mode === 'list'">
        <el-select
          v-model="filters.stage"
          clearable
          placeholder="阶段"
          size="small"
          class="filter"
          data-testid="opp-filter-stage"
          @change="search"
        >
          <el-option v-for="s in stages" :key="s.id" :label="s.name" :value="s.id" />
        </el-select>
        <el-select
          v-model="filters.closeMonth"
          clearable
          placeholder="预计成交月份"
          size="small"
          class="filter wide"
          data-testid="opp-filter-month"
          @change="search"
        >
          <el-option v-for="[value, label] in months" :key="value" :label="label" :value="value" />
        </el-select>
        <el-input
          v-model="filters.amountMin"
          size="small"
          clearable
          placeholder="金额从"
          class="amount"
          data-testid="opp-filter-amount-min"
          @change="search"
        />
        <el-input
          v-model="filters.amountMax"
          size="small"
          clearable
          placeholder="到"
          class="amount"
          data-testid="opp-filter-amount-max"
          @change="search"
        />
        <el-checkbox v-model="filters.stale" size="small" data-testid="opp-filter-stale" @change="search">
          只看停滞的
        </el-checkbox>
      </template>
      <el-input
        v-model="filters.q"
        size="small"
        clearable
        placeholder="商机名称、客户、公司，或完整手机号"
        class="search"
        data-testid="opp-search"
        @keyup.enter="search"
        @clear="search"
      />
    </div>

    <OpportunityBoard
      v-if="mode === 'board'"
      :filters="filters"
      :today="today"
      :refresh-key="refreshKey"
      @open="(id) => (openId = id)"
      @changed="loadStats"
      @loaded="onLoaded"
    />
    <OpportunityList
      v-else
      :filters="filters"
      :today="today"
      :refresh-key="refreshKey"
      @open="(id) => (openId = id)"
      @loaded="onLoaded"
    />

    <OpportunityDrawer :opportunity-id="openId" @close="openId = null" @changed="changed" />
    <OpportunityCreateDialog v-model="createOpen" @created="(o) => ((openId = o.id), changed())" />
    <OpportunitySettingsDialog
      v-model="settingsOpen"
      @saved="onSettingsSaved"
      @stages-changed="(list) => (stages = list)"
    />
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

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

.views,
.filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}

.filter {
  width: 120px;
}

.filter.wide {
  width: 150px;
}

.amount {
  width: 100px;
}

.search {
  width: 260px;
}
</style>
