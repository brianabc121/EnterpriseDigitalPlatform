<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { onBeforeUnmount, onMounted, ref } from 'vue'

import { api, formatDateTime } from '../../api'

const STATUS: Record<string, [string, 'success' | 'warning' | 'info' | 'danger']> = {
  online: ['在线', 'success'],
  busy: ['忙碌', 'danger'],
  away: ['小休', 'warning'],
  offline: ['离线', 'info'],
}
const REFRESH_MS = 15_000

const agents = ref<Schemas['AgentOut'][]>([])
const loading = ref(false)
let timer: ReturnType<typeof setInterval> | null = null

async function load(quiet = false): Promise<void> {
  if (!quiet) loading.value = true
  const { data, error } = await api.GET('/api/v1/agents')
  loading.value = false
  if (!data) {
    if (!quiet) ElMessage.error(errorMessage(error))
    return
  }
  agents.value = data.items
}

async function setConcurrency(
  agent: Schemas['AgentOut'],
  value: number | undefined,
): Promise<void> {
  if (!value || value === agent.max_concurrency) return
  const { error } = await api.PATCH('/api/v1/agents/{staff_id}', {
    params: { path: { staff_id: agent.staff_id } },
    body: { max_concurrency: value },
  })
  if (error) {
    ElMessage.error(errorMessage(error))
    await load(true)
    return
  }
  agent.max_concurrency = value
  ElMessage.success(`${agent.display_name} 最多同时接待 ${value} 个会话`)
}

onMounted(() => {
  void load()
  timer = setInterval(() => void load(true), REFRESH_MS)
})
onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
})
</script>

<template>
  <div>
    <p class="hint">坐席状态每 15 秒刷新。同时接待数达到上限的坐席不再分配新会话。</p>
    <el-table v-loading="loading" :data="agents" data-testid="agents-table">
      <el-table-column prop="display_name" label="坐席" min-width="120" />
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="STATUS[row.status]?.[1] ?? 'info'">
            {{ STATUS[row.status]?.[0] ?? row.status }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="接待中" width="90">
        <template #default="{ row }"
          >{{ row.active_sessions }} / {{ row.max_concurrency }}</template
        >
      </el-table-column>
      <el-table-column label="最多同时接待" width="170">
        <template #default="{ row }">
          <el-input-number
            :model-value="row.max_concurrency"
            :min="1"
            :max="50"
            size="small"
            :data-testid="`concurrency-${row.username}`"
            @change="(v: number | undefined) => setConcurrency(row, v)"
          />
        </template>
      </el-table-column>
      <el-table-column label="技能组" min-width="180">
        <template #default="{ row }">
          <el-tag v-for="g in row.skill_groups" :key="g.id" size="small" type="info" class="group">
            {{ g.name }}{{ g.is_lead ? '（组长）' : '' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="最近在线" width="170">
        <template #default="{ row }">
          {{ row.last_seen_at ? formatDateTime(row.last_seen_at) : '—' }}
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<style scoped>
.hint {
  margin: 0 0 12px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.group {
  margin-right: 6px;
}
</style>
