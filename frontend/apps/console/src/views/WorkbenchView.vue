<script setup lang="ts">
import type { Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, nextTick, onMounted, ref, watch } from 'vue'

import KbFeedPanel from '../components/knowledge/KbFeedPanel.vue'
import KbSearchPanel from '../components/knowledge/KbSearchPanel.vue'
import ChatPanel from '../components/workbench/ChatPanel.vue'
import CustomerPanel from '../components/workbench/CustomerPanel.vue'
import CustomerOrders from '../components/orders/CustomerOrders.vue'
import CustomerTodos from '../components/todos/CustomerTodos.vue'
import IncomingTransfer from '../components/workbench/IncomingTransfer.vue'
import { stageTag } from '../intent'
import { WATCHER_ROLE } from '../labels'
import { useAuthStore } from '../stores/auth'
import { STATUS_LABEL, useWorkbenchStore, type AgentStatus } from '../stores/workbench'
import { priorityTag } from '../workbench/routing'

type ListTab = 'mine' | 'queued' | 'ongoing' | 'closed'

const wb = useWorkbenchStore()
const auth = useAuthStore()
const tab = ref<ListTab>('mine')
const sideTab = ref<'customer' | 'todos' | 'orders' | 'knowledge' | 'feed'>('customer')
const canOrders = computed(() => auth.can('order:read') && auth.me?.features?.orders !== false)
/** 待确认的必读知识数（显示在"动态"页签上）。 */
const unreadKnowledge = ref(0)
const customerOrders = ref<InstanceType<typeof CustomerOrders> | null>(null)

// 意图卡片上点了"生成订单"：右栏切到订单，打开 AI 预填（默认选中客户最近的几句话）。
watch(
  () => wb.orderPick,
  async () => {
    if (!canOrders.value) return
    sideTab.value = 'orders'
    await nextTick()
    await customerOrders.value?.pick()
  },
)

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

/** "接待中"：我接待的会话，加上我正在旁听、协助的会话（带标记）。 */
const mine = computed(() => [
  ...wb.sessions,
  ...wb.watching.filter((w) => !wb.sessions.some((s) => s.id === w.id)),
])
const list = computed(() => {
  switch (tab.value) {
    case 'queued':
      return wb.queued
    case 'ongoing':
      return wb.ongoing
    case 'closed':
      return wb.closed
    default:
      return mine.value
  }
})

watch(tab, (value) => {
  if (value === 'closed') void wb.loadClosed()
})
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
      <el-tabs v-model="tab" class="tabs" stretch data-testid="session-tabs">
        <el-tab-pane :label="`接待中 ${mine.length}`" name="mine" />
        <el-tab-pane v-if="wb.canSeeQueue" :label="`排队 ${wb.queued.length}`" name="queued" />
        <el-tab-pane
          v-if="wb.canSeeQueue"
          :label="`进行中 ${wb.ongoing.length}`"
          name="ongoing"
        />
        <el-tab-pane label="已结束" name="closed" />
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
            <span class="name">
              <el-tag
                v-if="s.channel_type === 'email'"
                size="small"
                type="info"
                class="channel"
                data-testid="email-tag"
              >
                邮件
              </el-tag>
              {{ s.customer_display_name }}
            </span>
            <span class="time">{{ lastActivity(s) }}</span>
          </div>
          <div v-if="s.email_subject" class="subject" data-testid="session-email-subject">
            {{ s.email_subject }}
          </div>
          <div class="row">
            <span class="meta">
              <el-tag
                v-if="s.my_role && !wb.isMine(s)"
                size="small"
                type="warning"
                class="role"
                data-testid="watch-role"
              >
                {{ WATCHER_ROLE[s.my_role] ?? s.my_role }}
              </el-tag>
              {{ SESSION_LABEL[s.status] ?? s.status }}
              <template v-if="tab !== 'mine' && s.assignee_display_name">
                · {{ s.assignee_display_name }}
              </template>
              <el-tag
                v-if="tab === 'queued' && priorityTag(s.priority)"
                size="small"
                type="danger"
                class="flag"
                data-testid="priority-tag"
              >
                {{ priorityTag(s.priority) }}
              </el-tag>
              <el-tag v-if="s.intent" size="small" class="flag" data-testid="intent-tag">
                {{ s.intent }}
              </el-tag>
              <el-tag
                v-if="stageTag(s.purchase_stage)"
                size="small"
                :type="stageTag(s.purchase_stage)!.type"
                effect="dark"
                class="flag"
                data-testid="purchase-tag"
              >
                {{ stageTag(s.purchase_stage)!.label }}
              </el-tag>
            </span>
            <el-badge v-if="wb.unread[s.id]" :value="wb.unread[s.id]" data-testid="unread" />
          </div>
        </div>
        <el-empty v-if="list.length === 0" :image-size="60" description="暂无会话" />
      </div>
    </aside>

    <ChatPanel class="chat" />
    <IncomingTransfer />

    <aside class="customer side">
      <el-tabs v-model="sideTab" class="side-tabs" stretch>
        <el-tab-pane label="客户" name="customer" />
        <el-tab-pane v-if="auth.can('todo:read')" name="todos">
          <template #label><span data-testid="todos-tab">待办</span></template>
        </el-tab-pane>
        <el-tab-pane v-if="canOrders" name="orders">
          <template #label><span data-testid="orders-tab">订单</span></template>
        </el-tab-pane>
        <el-tab-pane v-if="auth.can('kb:read')" label="知识库" name="knowledge" />
        <el-tab-pane v-if="auth.can('kb:read')" name="feed">
          <template #label>
            <span data-testid="feed-tab">
              动态
              <el-badge v-if="unreadKnowledge" :value="unreadKnowledge" class="badge" />
            </span>
          </template>
        </el-tab-pane>
      </el-tabs>
      <template v-if="sideTab === 'customer'">
        <CustomerPanel
          v-if="wb.active"
          :key="wb.active.customer_id"
          class="side-body"
          :customer-id="wb.active.customer_id"
        />
        <div v-else class="side-body placeholder">选择会话后显示客户资料</div>
      </template>
      <template v-if="sideTab === 'todos'">
        <CustomerTodos
          v-if="wb.active"
          :key="wb.active.id"
          class="side-body todos"
          :customer-id="wb.active.customer_id"
          :customer-name="wb.active.customer_display_name"
          :session-id="wb.active.id"
          source="copilot"
        />
        <div v-else class="side-body placeholder">选择会话后显示客户的待办</div>
      </template>
      <template v-if="sideTab === 'orders'">
        <CustomerOrders
          v-if="wb.active"
          ref="customerOrders"
          :key="wb.active.id"
          class="side-body todos"
          :customer-id="wb.active.customer_id"
          :customer-name="wb.active.customer_display_name"
          :session-id="wb.active.id"
          source="copilot"
          @insert="wb.insertIntoComposer"
        />
        <div v-else class="side-body placeholder">选择会话后显示客户的订单</div>
      </template>
      <KbSearchPanel
        v-if="auth.can('kb:read')"
        v-show="sideTab === 'knowledge'"
        class="side-body kb"
        insertable
        @insert="wb.insertIntoComposer"
      />
      <KbFeedPanel
        v-if="auth.can('kb:read')"
        v-show="sideTab === 'feed'"
        class="side-body kb"
        @unread="(n: number) => (unreadKnowledge = n)"
      />
    </aside>
  </div>
</template>

<style scoped>
.workbench {
  display: grid;
  grid-template-columns: 280px minmax(0, 1fr) 300px;
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

.tabs :deep(.el-tabs__item) {
  padding: 0 6px;
  font-size: 13px;
}

.flag {
  margin-left: 4px;
}

.role {
  margin-right: 4px;
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

.channel {
  margin-right: 4px;
}

.subject {
  margin: 2px 0;
  font-size: 12px;
  color: var(--el-text-color-regular);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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

.kb,
.todos {
  padding: 10px 12px;
}

.badge {
  margin-left: 2px;
}

.placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
</style>
