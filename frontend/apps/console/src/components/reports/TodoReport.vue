<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref, watch } from 'vue'

import { api } from '../../api'
import { formatDuration } from '../../labels'
import { dayLabels } from '../../reports'
import StatTile from '../charts/StatTile.vue'
import TrendChart from '../charts/TrendChart.vue'

/** 待办报表（设计文档 §24.10）：数量、时效、AI 生成的质量和每日趋势。 */
const props = defineProps<{ range: [string, string]; tz?: string }>()

const report = ref<Schemas['TodoReport'] | null>(null)
const loading = ref(false)

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/reports/todos', {
    params: { query: { start: props.range[0], end: props.range[1], tz: props.tz } },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  report.value = data
}

function minutes(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : formatDuration(value * 60)
}

function pct(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : `${value}%`
}

const createdPoints = computed(() =>
  (report.value?.daily ?? []).map((d) => ({ ...dayLabels(d.day), value: d.created })),
)
const rejectHint = computed(() =>
  (report.value?.ai.reject_reasons ?? []).map((r) => `${r.label} ${r.count}`).join('，'),
)

watch(() => props.range, load)
onMounted(load)
</script>

<template>
  <div v-loading="loading && !report" data-testid="todo-report">
    <template v-if="report">
      <div class="tiles">
        <StatTile
          label="新建待办"
          :value="String(report.totals.created)"
          :hint="report.by_source.map((b) => `${b.label} ${b.count}`).join('，')"
          testid="todo-tile-created"
        />
        <StatTile label="已完成" :value="String(report.totals.done)" :hint="`取消 ${report.totals.cancelled}`" />
        <StatTile label="待确认" :value="String(report.totals.pending_now)" hint="当前" testid="todo-tile-pending" />
        <StatTile label="已逾期" :value="String(report.totals.overdue_now)" hint="当前未完成" />
        <StatTile
          label="待认领"
          :value="String(report.totals.unclaimed_now)"
          :hint="
            report.totals.oldest_unclaimed_minutes === null || report.totals.oldest_unclaimed_minutes === undefined
              ? '当前'
              : `最久 ${minutes(report.totals.oldest_unclaimed_minutes)}`
          "
        />
        <StatTile label="平均确认时长" :value="minutes(report.timeliness.avg_confirm_minutes)" hint="AI 生成到确认" />
        <StatTile label="平均首次响应" :value="minutes(report.timeliness.avg_first_response_minutes)" />
        <StatTile label="平均完成时长" :value="minutes(report.timeliness.avg_resolve_minutes)" />
        <StatTile label="按时完成率" :value="pct(report.timeliness.on_time_rate)" testid="todo-tile-on-time" />
      </div>

      <h3 class="section">AI 生成的待办</h3>
      <div class="tiles">
        <StatTile label="AI 生成" :value="String(report.ai.ai_created)" testid="todo-tile-ai" />
        <StatTile label="直接确认" :value="String(report.ai.confirmed_direct)" />
        <StatTile label="修改后确认" :value="String(report.ai.confirmed_modified)" />
        <StatTile label="驳回" :value="String(report.ai.rejected)" :hint="rejectHint" />
        <StatTile label="确认后又取消" :value="String(report.ai.cancelled_after_confirm)" />
      </div>

      <div class="charts">
        <el-card shadow="never" class="chart-card">
          <template #header>每日新建待办</template>
          <TrendChart kind="column" :points="createdPoints" label="每日新建待办" />
        </el-card>
        <el-card shadow="never" class="chart-card">
          <template #header>按类型</template>
          <el-table :data="report.by_type" size="small" empty-text="这段时间没有待办">
            <el-table-column prop="label" label="类型" />
            <el-table-column prop="count" label="数量" width="90" align="right" />
          </el-table>
        </el-card>
      </div>
    </template>
  </div>
</template>

<style scoped>
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

.section {
  margin: 24px 0 12px;
  font-size: 15px;
}
</style>
