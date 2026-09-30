<script setup lang="ts">
import type { Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import KbSearchPanel from '../components/knowledge/KbSearchPanel.vue'
import CustomerOrders from '../components/orders/CustomerOrders.vue'
import CustomerTodos from '../components/todos/CustomerTodos.vue'
import MyTodos from '../components/todos/MyTodos.vue'
import ChatPanel from '../components/workbench/ChatPanel.vue'
import CustomerPanel from '../components/workbench/CustomerPanel.vue'
import IncomingTransfer from '../components/workbench/IncomingTransfer.vue'
import { useAuthStore } from '../stores/auth'
import { STATUS_LABEL, useWorkbenchStore, type AgentStatus } from '../stores/workbench'

/**
 * 手机版坐席工作台（设计 §6.2、§17.1）：坐席在企业微信手机端点开应用消息提醒后免登进入，
 * 查看自己接待中的会话并直接回复。与电脑版共用工作台的状态、IM 连接和聊天面板，
 * 按手机屏幕一次显示一屏：会话列表 → 聊天 → 客户资料 / 待办 / 知识检索（抽屉）。
 * "待办"页签列出等我确认的和我的待办，可以直接确认和处理（设计文档 §24.9）。
 */
const wb = useWorkbenchStore()
const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const tab = ref<'mine' | 'queued' | 'todos'>('mine')
const drawer = ref<'customer' | 'knowledge' | 'todos' | 'orders' | null>(null)
const DRAWER_TITLE = { customer: '客户资料', todos: '客户的待办', orders: '客户的订单', knowledge: '知识检索' }

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
const inChat = computed(() => !!wb.active && route.query.session === wb.active.id)

async function changeStatus(value: AgentStatus): Promise<void> {
  try {
    await wb.setStatus(value)
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  }
}

async function select(session: Schemas['SessionOut']): Promise<void> {
  try {
    await wb.open(session)
    await router.push({ query: { session: session.id } })
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  }
}

function back(): void {
  drawer.value = null
  void router.push({ query: {} })
}

function time(s: Schemas['SessionOut']): string {
  const times = [s.last_customer_message_at, s.last_agent_message_at, s.created_at]
    .filter((t): t is string => Boolean(t))
    .map((t) => Date.parse(t))
  return new Date(Math.max(...times)).toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

function insertKnowledge(text: string): void {
  wb.insertIntoComposer(text)
  drawer.value = null
}

// 从提醒链接进入时（?session=…）直接打开那个会话。
async function openFromQuery(): Promise<void> {
  const id = route.query.session
  if (typeof id !== 'string' || wb.active?.id === id) return
  const session = [...wb.sessions, ...wb.queued].find((s) => s.id === id)
  if (session) await wb.open(session)
}

watch(() => [route.query.session, wb.sessions.length], () => void openFromQuery())

onMounted(async () => {
  await wb.start()
  await openFromQuery()
})
</script>

<template>
  <div class="mobile" data-testid="mobile-workbench">
    <template v-if="!inChat">
      <header class="bar">
        <span class="title">{{ auth.me?.display_name ?? '工作台' }}</span>
        <el-select v-model="status" size="small" class="status" data-testid="mobile-status">
          <el-option
            v-for="(label, value) in STATUS_LABEL"
            :key="value"
            :label="label"
            :value="value"
          />
        </el-select>
      </header>
      <el-alert v-if="wb.error" :title="wb.error" type="error" :closable="false" show-icon />
      <el-tabs v-model="tab" stretch class="tabs">
        <el-tab-pane :label="`接待中 ${wb.sessions.length}`" name="mine" />
        <el-tab-pane v-if="wb.canSeeQueue" :label="`排队 ${wb.queued.length}`" name="queued" />
        <el-tab-pane v-if="auth.can('todo:read')" label="待办" name="todos" />
      </el-tabs>
      <MyTodos v-if="tab === 'todos'" class="list" />
      <div v-else class="list">
        <div
          v-for="s in list"
          :key="s.id"
          class="item"
          data-testid="mobile-session"
          @click="select(s)"
        >
          <div class="row">
            <span class="name">{{ s.customer_display_name }}</span>
            <span class="muted">{{ time(s) }}</span>
          </div>
          <div class="row">
            <span class="muted">{{ SESSION_LABEL[s.status] ?? s.status }}</span>
            <el-badge v-if="wb.unread[s.id]" :value="wb.unread[s.id]" />
          </div>
        </div>
        <el-empty v-if="!list.length" :image-size="60" description="暂无会话" />
      </div>
    </template>

    <template v-else>
      <header class="bar">
        <el-button link data-testid="mobile-back" @click="back">‹ 会话</el-button>
        <span class="actions">
          <el-button link type="primary" data-testid="mobile-customer" @click="drawer = 'customer'">
            客户
          </el-button>
          <el-button
            v-if="auth.can('todo:read')"
            link
            type="primary"
            data-testid="mobile-todos"
            @click="drawer = 'todos'"
          >
            待办
          </el-button>
          <el-button
            v-if="auth.can('order:read') && auth.me?.features?.orders !== false"
            link
            type="primary"
            data-testid="mobile-orders"
            @click="drawer = 'orders'"
          >
            订单
          </el-button>
          <el-button
            v-if="auth.can('kb:read')"
            link
            type="primary"
            @click="drawer = 'knowledge'"
          >
            知识
          </el-button>
        </span>
      </header>
      <ChatPanel class="chat" />
    </template>
    <IncomingTransfer />

    <el-drawer
      :model-value="drawer !== null"
      direction="btt"
      size="80%"
      :title="drawer ? DRAWER_TITLE[drawer] : ''"
      @close="drawer = null"
    >
      <CustomerPanel
        v-if="drawer === 'customer' && wb.active"
        :key="wb.active.customer_id"
        :customer-id="wb.active.customer_id"
      />
      <CustomerTodos
        v-if="drawer === 'todos' && wb.active"
        :key="wb.active.id"
        :customer-id="wb.active.customer_id"
        :customer-name="wb.active.customer_display_name"
        :session-id="wb.active.id"
        source="copilot"
      />
      <CustomerOrders
        v-if="drawer === 'orders' && wb.active"
        :key="wb.active.id"
        :customer-id="wb.active.customer_id"
        :customer-name="wb.active.customer_display_name"
        :session-id="wb.active.id"
        source="copilot"
        @insert="insertKnowledge"
      />
      <KbSearchPanel v-if="drawer === 'knowledge'" insertable @insert="insertKnowledge" />
    </el-drawer>
  </div>
</template>

<style scoped>
.mobile {
  display: flex;
  flex-direction: column;
  height: 100vh;
  height: 100dvh;
  background: var(--el-bg-color);
}

.bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.title {
  font-weight: 600;
}

.status {
  width: 96px;
}

.actions {
  display: flex;
  gap: 4px;
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

.item {
  padding: 12px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.item:active {
  background: var(--el-fill-color-light);
}

.row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.name {
  font-weight: 500;
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.chat {
  flex: 1;
  min-height: 0;
}

/* 手机屏幕上：聊天区的标题栏换行，气泡更宽。 */
.chat :deep(.header) {
  flex-wrap: wrap;
  gap: 6px;
  padding: 8px 12px;
}

.chat :deep(.bubble) {
  max-width: 85%;
}

.chat :deep(.messages) {
  padding: 8px 10px;
}
</style>
