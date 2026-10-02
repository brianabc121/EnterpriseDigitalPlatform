<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { Loading } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api, formatDateTime } from '../api'
import FindingList from '../components/wake/FindingList.vue'
import WakeSettingsForm from '../components/wake/WakeSettingsForm.vue'
import {
  CATEGORY_LABEL,
  FINDING_STATUS,
  kbSummary,
  llmUsage,
  nextText,
  RUN_STATUS,
  RUN_STATUS_TAG,
  RUN_TRIGGER,
  runSummary,
  SEVERITIES,
  SEVERITY_LABEL,
  type Finding,
  type FindingPage,
  type WakeOverview,
  type WakeRun,
} from '../wake'

/**
 * AI 唤醒（设计文档 §33）：AI 按每个企业的时间表醒来——工作时间每小时检查、每天巡检一次并写简报、
 * 每周对照规章制度整理知识库。这里看全部问题、唤醒记录、增量更新索引（每张表最近一次变化），
 * 改设置，也可以立即唤醒。链接可以带 tab=runs|index|settings。
 */
type Tab = 'findings' | 'runs' | 'index' | 'settings'
const TABS: Tab[] = ['findings', 'runs', 'index', 'settings']
const PAGE_SIZE = 20
const POLL_MS = 3000
const INDEX_HELP =
  '企业的每张数据表都有增量字段（change_seq）。数据新增、修改、删除时，数据库在事务提交时更新这张索引表：' +
  '每张表最近一次变化的编号和时间。AI 检查前先比对这里的编号——检查项读的表都没有新的变化时直接跳过，' +
  '不再全表查询；有变化时按增量字段只找变了的数据。'

const route = useRoute()
const router = useRouter()
const initial = route.query.tab
const tab = ref<Tab>(TABS.includes(initial as Tab) ? (initial as Tab) : 'findings')

const overview = ref<WakeOverview | null>(null)
const findings = ref<FindingPage | null>(null)
const runs = ref<WakeRun[]>([])
const runsTotal = ref(0)
const status = ref<Finding['status'] | ''>('open')
const category = ref('')
const severity = ref<Finding['severity'] | ''>('')
const page = ref(1)
const runsPage = ref(1)
const loading = ref(false)
const starting = ref<'' | 'daily' | 'kb'>('')
let poll: ReturnType<typeof setInterval> | undefined

const brief = computed(() => overview.value?.brief ?? null)
const pending = computed(() => overview.value?.pending ?? [])

async function loadOverview(): Promise<void> {
  const { data, error } = await api.GET('/api/v1/wake/overview')
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  overview.value = data
}

async function loadFindings(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/wake/findings', {
    params: {
      query: {
        view: 'all',
        status: status.value || undefined,
        category: category.value || undefined,
        severity: severity.value || undefined,
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
  findings.value = data
}

async function loadRuns(): Promise<void> {
  const { data, error } = await api.GET('/api/v1/wake/runs', {
    params: { query: { limit: PAGE_SIZE, offset: (runsPage.value - 1) * PAGE_SIZE } },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  runs.value = data.items
  runsTotal.value = data.total
}

async function refresh(): Promise<void> {
  await Promise.all([loadOverview(), loadFindings(), tab.value === 'runs' ? loadRuns() : null])
}

/** 立即唤醒：排进队列，实时消费进程几秒内执行；执行完之前每 3 秒刷新一次。 */
async function start(kind: 'daily' | 'kb'): Promise<void> {
  starting.value = kind
  const { data, error } = await api.POST('/api/v1/wake/runs', { body: { kind } })
  starting.value = ''
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success(kind === 'daily' ? 'AI 已经醒来，正在巡检' : 'AI 正在整理知识库')
  await refresh()
  watchPending()
}

function watchPending(): void {
  clearInterval(poll)
  poll = setInterval(async () => {
    await loadOverview()
    if (!pending.value.length) {
      clearInterval(poll)
      await refresh()
    }
  }, POLL_MS)
}

function onChanged(): void {
  void loadFindings()
  void loadOverview()
}

function reload(): void {
  page.value = 1
  void loadFindings()
}

watch([status, category, severity], reload)
watch(tab, (value) => {
  void router.replace({ query: { ...route.query, tab: value === 'findings' ? undefined : value } })
  if (value === 'runs') void loadRuns()
  if (value === 'index') void loadOverview()
})

onMounted(async () => {
  await refresh()
  if (tab.value === 'runs') await loadRuns()
  if (pending.value.length) watchPending()
})
onBeforeUnmount(() => clearInterval(poll))
</script>

<template>
  <div class="wake">
    <div class="page-header">
      <h2>AI 唤醒</h2>
      <div class="actions">
        <el-button
          :loading="starting === 'kb'"
          :disabled="!overview?.enabled"
          data-testid="wake-run-kb"
          @click="start('kb')"
          >立即整理知识库</el-button
        >
        <el-button
          type="primary"
          :loading="starting === 'daily'"
          :disabled="!overview?.enabled"
          data-testid="wake-run-daily"
          @click="start('daily')"
          >立即巡检</el-button
        >
      </div>
    </div>

    <el-alert
      v-if="overview && !overview.enabled"
      type="info"
      :closable="false"
      show-icon
      title="AI 唤醒已关闭：不再巡检企业数据、不再整理知识库。可以在“设置”里打开。"
      class="block"
    />

    <div v-if="overview" class="cards" data-testid="wake-overview">
      <div class="card counts">
        <div class="card-title">待处理的问题</div>
        <div class="numbers">
          <div
            v-for="level in SEVERITIES"
            :key="level"
            class="number"
            :class="level"
            :data-testid="`wake-open-${level}`"
          >
            <strong>{{ overview.open[level] }}</strong>
            <span>{{ SEVERITY_LABEL[level] }}</span>
          </div>
        </div>
      </div>
      <div class="card schedule">
        <div class="card-title">下次唤醒</div>
        <div class="line"><span>每小时检查</span>{{ nextText(overview.next_hourly) }}</div>
        <div class="line"><span>每日巡检</span>{{ nextText(overview.next_daily) }}</div>
        <div class="line"><span>知识库整理</span>{{ nextText(overview.next_kb) }}</div>
        <div v-if="pending.length" class="running" data-testid="wake-pending">
          <el-icon class="is-loading"><Loading /></el-icon>
          {{ pending.map((r) => `${r.kind_label}${RUN_STATUS[r.status]}`).join('、') }}
        </div>
      </div>
      <div class="card brief" data-testid="wake-brief">
        <div class="card-title">
          AI 简报
          <span v-if="brief?.finished_at" class="muted">{{ formatDateTime(brief.finished_at) }}</span>
        </div>
        <p v-if="brief?.summary" class="summary" data-testid="wake-brief-text">{{ brief.summary }}</p>
        <p v-else class="muted">每日巡检后 AI 会在这里写一份简报，也会发给管理员。</p>
        <div v-if="overview.kb" class="muted kb" data-testid="wake-kb-summary">
          知识库整理（{{ formatDateTime(overview.kb.finished_at ?? overview.kb.created_at) }}）：
          {{ kbSummary(overview.kb.stats) }}
        </div>
      </div>
    </div>

    <el-tabs v-model="tab" data-testid="wake-tabs">
      <el-tab-pane name="findings">
        <template #label>
          问题
          <el-badge v-if="overview?.open.total" :value="overview.open.total" class="badge" />
        </template>
        <div class="filters">
          <el-radio-group v-model="status" size="small" data-testid="wake-status-filter">
            <el-radio-button value="">全部</el-radio-button>
            <el-radio-button v-for="(label, value) in FINDING_STATUS" :key="value" :value="value">
              {{ label }}
            </el-radio-button>
          </el-radio-group>
          <el-select v-model="category" size="small" class="select" placeholder="分类" clearable>
            <el-option v-for="(label, value) in CATEGORY_LABEL" :key="value" :label="label" :value="value" />
          </el-select>
          <el-select v-model="severity" size="small" class="select" placeholder="级别" clearable>
            <el-option v-for="level in SEVERITIES" :key="level" :label="SEVERITY_LABEL[level]" :value="level" />
          </el-select>
        </div>
        <div v-loading="loading">
          <FindingList
            :items="findings?.items ?? []"
            :empty-text="status === 'open' ? 'AI 没有发现需要处理的问题' : '没有记录'"
            @changed="onChanged"
          />
        </div>
        <div class="page-footer">
          <el-pagination
            v-model:current-page="page"
            :page-size="PAGE_SIZE"
            :total="findings?.total ?? 0"
            layout="total, prev, pager, next"
            @current-change="loadFindings"
          />
        </div>
      </el-tab-pane>

      <el-tab-pane label="唤醒记录" name="runs" lazy>
        <el-table :data="runs" data-testid="wake-runs" empty-text="还没有唤醒记录">
          <el-table-column label="时间" width="170">
            <template #default="{ row }">{{ formatDateTime(row.started_at ?? row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="唤醒" width="150">
            <template #default="{ row }">
              <div>{{ row.kind_label }}</div>
              <div class="muted">
                {{ RUN_TRIGGER[row.trigger as WakeRun['trigger']] }}{{ row.created_by ? ` · ${row.created_by.name}` : '' }}
              </div>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="RUN_STATUS_TAG[row.status as WakeRun['status']]">
                {{ RUN_STATUS[row.status as WakeRun['status']] }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="结果" min-width="320">
            <template #default="{ row }">
              <div data-testid="wake-run-summary">{{ runSummary(row) }}</div>
              <div v-if="row.summary" class="muted brief-line">{{ row.summary }}</div>
              <div v-if="llmUsage(row.stats)" class="muted">{{ llmUsage(row.stats) }}</div>
            </template>
          </el-table-column>
        </el-table>
        <div class="page-footer">
          <el-pagination
            v-model:current-page="runsPage"
            :page-size="PAGE_SIZE"
            :total="runsTotal"
            layout="total, prev, pager, next"
            @current-change="loadRuns"
          />
        </div>
      </el-tab-pane>

      <el-tab-pane label="增量更新索引" name="index" lazy>
        <p class="muted explain">{{ INDEX_HELP }}</p>
        <el-table :data="overview?.data_index ?? []" data-testid="wake-data-index" empty-text="还没有数据变化">
          <el-table-column label="数据" min-width="160">
            <template #default="{ row }">
              {{ row.label }} <span v-if="row.label !== row.domain" class="muted">{{ row.domain }}</span>
            </template>
          </el-table-column>
          <el-table-column label="最近一次变化" width="190">
            <template #default="{ row }">{{ formatDateTime(row.changed_at) }}</template>
          </el-table-column>
          <el-table-column label="变化编号" width="140" align="right">
            <template #default="{ row }">{{ row.seq }}</template>
          </el-table-column>
        </el-table>
        <p class="muted">共 {{ overview?.tracked ?? 0 }} 张表有变化记录，显示最近的 12 张。</p>
      </el-tab-pane>

      <el-tab-pane label="设置" name="settings" lazy>
        <WakeSettingsForm @saved="loadOverview" />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.actions {
  display: flex;
  gap: 8px;
}

.block {
  margin-bottom: 12px;
}

.cards {
  display: grid;
  grid-template-columns: minmax(220px, 1fr) minmax(220px, 1fr) minmax(300px, 2fr);
  gap: 12px;
  margin-bottom: 12px;
}

@media (max-width: 900px) {
  .cards {
    grid-template-columns: 1fr;
  }
}

.card {
  padding: 12px 16px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-bg-color);
}

.card-title {
  display: flex;
  justify-content: space-between;
  margin-bottom: 8px;
  font-size: 13px;
  font-weight: 500;
}

.numbers {
  display: flex;
  gap: 16px;
}

.number {
  display: flex;
  flex-direction: column;
  align-items: center;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.number strong {
  font-size: 24px;
  color: var(--el-text-color-primary);
}

.number.critical strong {
  color: var(--el-color-danger);
}

.number.warning strong {
  color: var(--el-color-warning);
}

.line {
  display: flex;
  justify-content: space-between;
  font-size: 13px;
  line-height: 24px;
}

.line span {
  color: var(--el-text-color-secondary);
}

.running {
  margin-top: 6px;
  font-size: 12px;
  color: var(--el-color-primary);
}

.summary {
  margin: 0;
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
}

.kb {
  margin-top: 8px;
}

.filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}

.select {
  width: 110px;
}

.badge {
  margin-left: 4px;
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.brief-line {
  white-space: pre-wrap;
}

.explain {
  margin: 0 0 12px;
  line-height: 1.6;
}
</style>
