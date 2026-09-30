<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { onMounted, ref } from 'vue'

import { api, formatDateTime } from '../api'

const props = defineProps<{ tenantId: string }>()

const SENDER: Record<string, string> = { customer: '客户', agent: '坐席', bot: 'AI', system: '系统' }

const status = ref<Schemas['SupportStatus'] | null>(null)
const sessions = ref<Schemas['SupportSessionOut'][]>([])
const messages = ref<Schemas['SupportMessageOut'][]>([])
const viewing = ref<Schemas['SupportSessionOut'] | null>(null)
const loading = ref(false)

async function load(): Promise<void> {
  const { data, error } = await api.GET('/platform/v1/tenants/{tenant_id}/support', {
    params: { path: { tenant_id: props.tenantId } },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  status.value = data
}

async function loadSessions(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/platform/v1/tenants/{tenant_id}/support/sessions', {
    params: { path: { tenant_id: props.tenantId } },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  sessions.value = data.items
}

async function open(session: Schemas['SupportSessionOut']): Promise<void> {
  viewing.value = session
  messages.value = []
  const { data, error } = await api.GET(
    '/platform/v1/tenants/{tenant_id}/support/sessions/{session_id}/messages',
    { params: { path: { tenant_id: props.tenantId, session_id: session.id } } },
  )
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  messages.value = data.items
}

onMounted(load)
</script>

<template>
  <div v-if="status" data-testid="tenant-support">
    <el-alert
      v-if="!status.active"
      type="info"
      :closable="false"
      show-icon
      title="租户没有授权平台查看业务数据"
      description="需要租户管理员在控制台「设置 → 平台访问授权」里授权；有效期内的每次查看都会记入审计日志，租户可以看到。"
    />
    <template v-else-if="status.grant">
      <el-alert
        type="success"
        :closable="false"
        show-icon
        :title="`已授权至 ${formatDateTime(status.grant.expires_at)}：${status.grant.reason}`"
        description="以下查看都会记入审计日志。"
      />
      <el-button class="load" data-testid="support-sessions-button" @click="loadSessions">
        查看最近的会话
      </el-button>
      <el-table v-loading="loading" :data="sessions" size="small" data-testid="support-sessions">
        <el-table-column prop="customer_name" label="客户" />
        <el-table-column prop="channel_name" label="渠道" width="120" />
        <el-table-column prop="assignee_name" label="坐席" width="100" />
        <el-table-column prop="status" label="状态" width="110" />
        <el-table-column label="开始时间" width="170">
          <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="80">
          <template #default="{ row }">
            <el-button link type="primary" @click="open(row)">消息</el-button>
          </template>
        </el-table-column>
      </el-table>
    </template>

    <el-drawer
      :model-value="viewing !== null"
      :title="`会话消息 · ${viewing?.customer_name ?? ''}`"
      size="45%"
      @update:model-value="(v: boolean) => !v && (viewing = null)"
    >
      <div v-for="m in messages" :key="m.id" class="message" data-testid="support-message">
        <span class="who">{{ SENDER[m.sender_type] ?? m.sender_type }}</span>
        <span class="time">{{ formatDateTime(m.sent_at) }}</span>
        <div>{{ m.text ?? `[${m.content_type}]` }}</div>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.load {
  margin: 12px 0;
}

.message {
  padding: 8px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.who {
  font-weight: 600;
  margin-right: 8px;
}

.time {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
