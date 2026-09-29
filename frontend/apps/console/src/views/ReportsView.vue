<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref, watch } from 'vue'

import { api } from '../api'
import StatTile from '../components/charts/StatTile.vue'
import TrendChart, { type TrendPoint } from '../components/charts/TrendChart.vue'
import { formatDuration } from '../labels'
import { browserTimeZone, dayLabels, lastDays, percent } from '../reports'

const PRESETS = [
  { days: 7, label: '近 7 天' },
  { days: 30, label: '近 30 天' },
  { days: 90, label: '近 90 天' },
]
const STATUS: Record<string, string> = {
  online: '在线',
  busy: '忙碌',
  away: '小休',
  offline: '离线',
}

const preset = ref<number | null>(7)
const range = ref<[string, string]>(lastDays(7))
const overview = ref<Schemas['Overview'] | null>(null)
const agents = ref<Schemas['AgentStats'][]>([])
const loading = ref(false)
const tz = browserTimeZone()

async function load(): Promise<void> {
  loading.value = true
  const query = { start: range.value[0], end: range.value[1], tz }
  const [o, a] = await Promise.all([
    api.GET('/api/v1/reports/overview', { params: { query } }),
    api.GET('/api/v1/reports/agents', { params: { query } }),
  ])
  loading.value = false
  if (!o.data || !a.data) {
    ElMessage.error(errorMessage(o.error ?? a.error))
    return
  }
  overview.value = o.data
  agents.value = a.data.items
}

function choose(days: number): void {
  preset.value = days
  range.value = lastDays(days)
}

function onPick(value: [string, string] | null): void {
  if (!value) return
  preset.value = null
  range.value = value
}

watch(range, load)
onMounted(load)

const totals = computed(() => overview.value?.totals ?? null)

function points(pick: (day: Schemas['DailyStats']) => number | null): TrendPoint[] {
  return (overview.value?.days ?? []).map((day) => ({ ...dayLabels(day.day), value: pick(day) }))
}

const sessionPoints = computed(() => points((d) => d.sessions))
const responsePoints = computed(() => points((d) => d.avg_first_response_seconds ?? null))
const csat = (avg: number | null | undefined, count: number) =>
  avg === null || avg === undefined ? '—' : `${avg.toFixed(1)}（${count} 个评价）`
</script>

<template>
  <div class="reports">
    <div class="page-header">
      <h2>报表</h2>
    </div>
    <div class="filters" data-testid="report-filters">
      <el-radio-group :model-value="preset" size="small">
        <el-radio-button v-for="p in PRESETS" :key="p.days" :value="p.days" @click="choose(p.days)">
          {{ p.label }}
        </el-radio-button>
      </el-radio-group>
      <div class="range-picker">
        <el-date-picker
          :model-value="range"
          type="daterange"
          value-format="YYYY-MM-DD"
          size="small"
          range-separator="至"
          :clearable="false"
          @update:model-value="onPick"
        />
      </div>
    </div>

    <div :class="{ refreshing: loading && overview }">
      <div v-if="totals" class="tiles" data-testid="report-tiles">
        <StatTile
          label="会话"
          :value="totals.sessions.toLocaleString('zh-CN')"
          :hint="`人工接待 ${totals.human_sessions}，转留言 ${totals.missed_sessions}`"
          testid="tile-sessions"
        />
        <StatTile label="平均排队" :value="formatDuration(totals.avg_wait_seconds)" />
        <StatTile
          label="平均首次响应"
          :value="formatDuration(totals.avg_first_response_seconds)"
          hint="从分配到坐席第一条回复"
        />
        <StatTile label="平均处理时长" :value="formatDuration(totals.avg_handle_seconds)" />
        <StatTile
          label="满意度"
          :value="
            totals.csat_avg === null || totals.csat_avg === undefined
              ? '—'
              : `${totals.csat_avg.toFixed(1)} 分`
          "
          :hint="`满意率 ${percent(totals.satisfied_rate)}，${totals.csat_count} 个评价`"
          testid="tile-csat"
        />
        <StatTile
          label="消息"
          :value="(totals.messages_in + totals.agent_messages).toLocaleString('zh-CN')"
          :hint="`客户 ${totals.messages_in}，坐席 ${totals.agent_messages}`"
        />
        <StatTile label="留言" :value="String(totals.tickets)" />
        <StatTile label="转接" :value="String(totals.transfers)" />
        <template v-if="totals.ai_sessions">
          <StatTile
            label="AI 接待"
            :value="totals.ai_sessions.toLocaleString('zh-CN')"
            :hint="`转人工 ${totals.ai_handoffs}`"
            testid="tile-ai-sessions"
          />
          <StatTile
            label="AI 独立解决率"
            :value="percent(totals.ai_resolution_rate)"
            :hint="`AI 解决 ${totals.ai_resolved} 个会话`"
            testid="tile-ai-resolved"
          />
        </template>
      </div>

      <div v-if="overview" class="charts">
        <el-card shadow="never" class="chart-card">
          <template #header>每日会话数</template>
          <TrendChart kind="column" :points="sessionPoints" label="每日会话数" />
        </el-card>
        <el-card shadow="never" class="chart-card">
          <template #header>每日平均首次响应时长</template>
          <TrendChart
            kind="line"
            :points="responsePoints"
            :format="formatDuration"
            label="每日平均首次响应时长"
          />
        </el-card>
      </div>

      <el-collapse v-if="overview" class="table-view">
        <el-collapse-item title="查看每日数据" name="days">
          <el-table :data="overview.days" size="small" data-testid="report-days">
            <el-table-column prop="day" label="日期" width="120" />
            <el-table-column prop="sessions" label="会话" width="80" />
            <el-table-column prop="human_sessions" label="人工接待" width="90" />
            <el-table-column prop="closed_sessions" label="已结束" width="80" />
            <el-table-column prop="missed_sessions" label="转留言" width="80" />
            <el-table-column label="平均排队" min-width="100">
              <template #default="{ row }">{{ formatDuration(row.avg_wait_seconds) }}</template>
            </el-table-column>
            <el-table-column label="平均首次响应" min-width="110">
              <template #default="{ row }">
                {{ formatDuration(row.avg_first_response_seconds) }}
              </template>
            </el-table-column>
            <el-table-column label="满意度" min-width="120">
              <template #default="{ row }">{{ csat(row.csat_avg, row.csat_count) }}</template>
            </el-table-column>
          </el-table>
        </el-collapse-item>
      </el-collapse>

      <h3 class="section">坐席</h3>
      <el-table :data="agents" data-testid="report-agents" empty-text="这段时间没有坐席数据">
        <el-table-column prop="display_name" label="坐席" min-width="110" />
        <el-table-column label="当前状态" width="90">
          <template #default="{ row }">{{ STATUS[row.status] ?? row.status }}</template>
        </el-table-column>
        <el-table-column prop="sessions" label="接待会话" width="90" />
        <el-table-column prop="messages" label="发出消息" width="90" />
        <el-table-column label="平均首次响应" min-width="110">
          <template #default="{ row }">{{
            formatDuration(row.avg_first_response_seconds)
          }}</template>
        </el-table-column>
        <el-table-column label="平均处理时长" min-width="110">
          <template #default="{ row }">{{ formatDuration(row.avg_handle_seconds) }}</template>
        </el-table-column>
        <el-table-column label="满意度" min-width="120">
          <template #default="{ row }">{{ csat(row.csat_avg, row.csat_count) }}</template>
        </el-table-column>
        <el-table-column prop="transfers_out" label="转出" width="70" />
      </el-table>
    </div>
  </div>
</template>

<style scoped>
.filters {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}

.range-picker {
  width: 280px;
}

.refreshing {
  opacity: 0.55;
  transition: opacity 0.2s;
}

.tiles {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
  gap: 12px;
}

.charts {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
  gap: 12px;
  margin-top: 16px;
}

.chart-card :deep(.el-card__header) {
  padding: 10px 16px;
  font-size: 14px;
  font-weight: 600;
}

.table-view {
  margin-top: 12px;
}

.section {
  margin: 24px 0 12px;
  font-size: 15px;
}
</style>
