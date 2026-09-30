<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { onBeforeUnmount, onMounted, ref } from 'vue'

import { api, formatDateTime } from '../api'
import { HEALTH_STATUS } from '../labels'

const METRICS: Record<string, string> = {
  active_tenants: '正常租户',
  open_sessions: '进行中的会话',
  queued_sessions: '排队中',
  messages_1h: '最近 1 小时消息',
  ai_replies_1h: '最近 1 小时 AI 回复',
  llm_calls_15m: '最近 15 分钟模型调用',
  llm_errors_15m: '最近 15 分钟模型失败',
  outbox_lagging: '积压的 IM 操作',
  outbox_failed_24h: '24 小时内失败的 IM 操作',
  event_pending: '待处理事件',
  dead_letters: '死信事件',
}

// 可以在"运维"页处理的指标。
const OPS_METRICS = new Set(['outbox_lagging', 'outbox_failed_24h', 'dead_letters'])

const report = ref<Schemas['HealthReport'] | null>(null)
const loading = ref(false)
let timer: ReturnType<typeof setInterval> | undefined

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/platform/v1/health')
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  report.value = data
}

onMounted(() => {
  void load()
  timer = setInterval(() => void load(), 30_000)
})
onBeforeUnmount(() => clearInterval(timer))
</script>

<template>
  <div v-loading="loading && !report">
    <div class="page-header">
      <h2>
        系统健康
        <el-tag disable-transitions
          v-if="report"
          :type="HEALTH_STATUS[report.status]?.type as 'success' | 'warning' | 'danger'"
          data-testid="health-overall"
        >
          {{ HEALTH_STATUS[report.status]?.label }}
        </el-tag>
      </h2>
      <span class="sub">
        {{ report ? `检查于 ${formatDateTime(report.checked_at)}，每 30 秒刷新` : '' }}
        <el-button link type="primary" @click="load">立即刷新</el-button>
      </span>
    </div>
    <template v-if="report">
      <div class="grid" data-testid="health-components">
        <el-card v-for="c in report.components" :key="c.key" shadow="never" class="card">
          <div class="card-title">
            {{ c.name }}
            <el-tag disable-transitions
              size="small"
              :type="HEALTH_STATUS[c.status]?.type as 'success' | 'warning' | 'danger' | 'info'"
            >
              {{ HEALTH_STATUS[c.status]?.label }}
            </el-tag>
          </div>
          <div class="sub">{{ c.detail ?? '' }}</div>
          <div v-if="c.latency_ms !== null && c.latency_ms !== undefined" class="sub">
            {{ c.latency_ms }} ms
          </div>
        </el-card>
      </div>
      <h3>指标</h3>
      <el-descriptions :column="4" border size="small">
        <el-descriptions-item v-for="(value, key) in report.metrics" :key="key" :label="METRICS[key] ?? key">
          {{ value }}
          <router-link v-if="OPS_METRICS.has(String(key)) && value" :to="{ name: 'ops' }" class="ops-link">
            去处理
          </router-link>
        </el-descriptions-item>
      </el-descriptions>
    </template>
  </div>
</template>

<style scoped>
.ops-link {
  margin-left: 8px;
  font-size: 12px;
}

.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}

.page-header h2 {
  margin: 0;
  font-size: 18px;
  display: flex;
  gap: 8px;
  align-items: center;
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 12px;
}

.card-title {
  display: flex;
  justify-content: space-between;
  font-weight: 600;
  margin-bottom: 6px;
}

.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

h3 {
  margin: 20px 0 8px;
  font-size: 15px;
}
</style>
