<script setup lang="ts">
import type { Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'

import KbSearchPanel from '../components/knowledge/KbSearchPanel.vue'
import ChatPanel from '../components/workbench/ChatPanel.vue'
import CustomerPanel from '../components/workbench/CustomerPanel.vue'
import IncomingTransfer from '../components/workbench/IncomingTransfer.vue'
import { useAuthStore } from '../stores/auth'
import { STATUS_LABEL, useWorkbenchStore, type AgentStatus } from '../stores/workbench'

const wb = useWorkbenchStore()
const auth = useAuthStore()
const tab = ref<'mine' | 'queued'>('mine')
const sideTab = ref<'customer' | 'knowledge'>('customer')

const IM_LABEL: Record<string, string> = {
  idle: '未连接',
  connecting: '连接中',
  connected: '已连接',
  reconnecting: '重连中',
  failed: '连接失败',
  kicked: '已在别处登录',
  expired: '登录已过期',
}
const SESSION_LABEL: Record<string, string> = {
  queued: '排队中',
  human_serving: '接待中',
  transferring: '转接中',
  ai_serving: 'AI 接待',
  closed: '已结束',
}

const list = computed(() => (tab.value === 'mine' ? wb.sessions : wb.queued))
const status = computed({
  get: () => wb.agent?.status ?? 'offline',
  set: (value: AgentStatus) => void changeStatus(value),
})

async function changeStatus(value: AgentStatus): Promise<void> {
  try {
    await wb.setStatus(value)
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  }
}

function lastActivity(s: Schemas['SessionOut']): string {
  const times = [s.last_customer_message_at, s.last_agent_message_at, s.created_at]
    .filter((t): t is string => Boolean(t))
    .map((t) => Date.parse(t))
  return new Date(Math.max(...times)).toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

async function select(session: Schemas['SessionOut']): Promise<void> {
  try {
    await wb.open(session)
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  }
}

onMounted(() => void wb.start())
</script>

<template>
  <div class="workbench">
    <aside class="sessions">
      <div class="agent-bar">
        <el-select v-model="status" size="small" class="status" data-testid="agent-status">
          <el-option
            v-for="(label, value) in STATUS_LABEL"
            :key="value"
            :label="label"
            :value="value"
          />
        </el-select>
        <span class="load" v-if="wb.agent">
          {{ wb.agent.active_sessions }} / {{ wb.agent.max_concurrency }}
        </span>
        <el-tag
          size="small"
          :type="wb.imState === 'connected' ? 'success' : 'warning'"
          data-testid="im-state"
        >
          {{ IM_LABEL[wb.imState] ?? wb.imState }}
        </el-tag>
      </div>
      <el-alert v-if="wb.error" :title="wb.error" type="error" :closable="false" show-icon />
      <el-tabs v-model="tab" class="tabs" stretch>
        <el-tab-pane :label="`接待中 ${wb.sessions.length}`" name="mine" />
        <el-tab-pane v-if="wb.canSeeQueue" :label="`排队 ${wb.queued.length}`" name="queued" />
      </el-tabs>
      <div class="list">
        <div
          v-for="s in list"
          :key="s.id"
          class="session"
          :class="{ active: wb.active?.id === s.id }"
          data-testid="session-item"
          @click="select(s)"
        >
          <div class="row">
            <span class="name">{{ s.customer_display_name }}</span>
            <span class="time">{{ lastActivity(s) }}</span>
          </div>
          <div class="row">
            <span class="meta">
              {{ SESSION_LABEL[s.status] ?? s.status }}
              <template v-if="tab === 'queued' && s.assignee_display_name">
                · {{ s.assignee_display_name }}
              </template>
            </span>
            <el-badge v-if="wb.unread[s.id]" :value="wb.unread[s.id]" data-testid="unread" />
          </div>
        </div>
        <el-empty v-if="list.length === 0" :image-size="60" description="暂无会话" />
      </div>
    </aside>

    <ChatPanel class="chat" />
    <IncomingTransfer />

    <aside v-if="wb.active" class="customer side">
      <el-tabs v-model="sideTab" class="side-tabs" stretch>
        <el-tab-pane label="客户" name="customer" />
        <el-tab-pane v-if="auth.can('kb:read')" label="知识库" name="knowledge" />
      </el-tabs>
      <CustomerPanel
        v-show="sideTab === 'customer'"
        :key="wb.active.customer_id"
        class="side-body"
        :customer-id="wb.active.customer_id"
      />
      <KbSearchPanel
        v-if="auth.can('kb:read')"
        v-show="sideTab === 'knowledge'"
        class="side-body kb"
        insertable
        @insert="wb.insertIntoComposer"
      />
    </aside>
    <aside v-else class="customer placeholder">选择会话后显示客户资料</aside>
  </div>
</template>

<style scoped>
.workbench {
  display: grid;
  grid-template-columns: 260px minmax(0, 1fr) 300px;
  gap: 12px;
  height: calc(100vh - 60px - 40px);
  min-height: 480px;
}

.sessions,
.customer,
.chat {
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-light);
  border-radius: 6px;
  min-height: 0;
}

.sessions {
  display: flex;
  flex-direction: column;
}

.agent-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.status {
  width: 88px;
}

.load {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-right: auto;
}

.tabs {
  padding: 0 12px;
}

.tabs :deep(.el-tabs__header) {
  margin-bottom: 0;
}

.list {
  flex: 1;
  overflow-y: auto;
}

.session {
  padding: 10px 12px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  cursor: pointer;
}

.session:hover {
  background: var(--el-fill-color-light);
}

.session.active {
  background: var(--el-color-primary-light-9);
}

.row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.name {
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.time,
.meta {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.side {
  display: flex;
  flex-direction: column;
}

.side-tabs {
  padding: 0 12px;
}

.side-tabs :deep(.el-tabs__header) {
  margin-bottom: 0;
}

.side-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}

.kb {
  padding: 10px 12px;
}

.placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
</style>
