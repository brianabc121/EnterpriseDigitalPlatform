<script setup lang="ts">
import { createImClient, type ConnectionState } from '@edp/im-client'
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'

import { fromApi, fromIm, mergeMessages, SENDER_LABEL, type WidgetMessage } from './chat'
import { fetchMessages, initVisitor, type VisitorSession } from './visitor'

const STATE_TEXT: Record<ConnectionState, string> = {
  idle: '未连接',
  connecting: '连接中…',
  connected: '在线',
  reconnecting: '重新连接中…',
  failed: '连接失败',
  kicked: '已在其他窗口打开',
  expired: '会话已过期，请刷新页面',
}

/**
 * 实时消息走 IM；OpenIM 在用户刚连上的一两秒内可能还没把他当作在线用户推送（实施计划 §10.1），
 * 所以连上后、之后每隔一段时间（页面可见时）以及重连后，都用平台的历史接口补齐一次。
 */
const SYNC_AFTER_CONNECT_MS = 3000
const SYNC_INTERVAL_MS = 10000

const channelKey = new URLSearchParams(location.search).get('key')
const im = createImClient()
const session = ref<VisitorSession | null>(null)
const state = ref<ConnectionState>('idle')
const messages = ref<WidgetMessage[]>([])
const draft = ref('')
const sending = ref(false)
const error = ref<string | null>(channelKey ? null : '缺少渠道参数 key')
const list = ref<HTMLElement | null>(null)
const timers: ReturnType<typeof setTimeout>[] = []

const canSend = computed(
  () => state.value === 'connected' && draft.value.trim().length > 0 && !sending.value,
)

/** 人工客服显示坐席姓名，其他发送者显示固定称呼。 */
function senderLabel(message: WidgetMessage): string {
  return message.role === 'agent' && message.senderName
    ? message.senderName
    : SENDER_LABEL[message.role]
}

function receive(incoming: WidgetMessage[]): void {
  const before = messages.value.length
  messages.value = mergeMessages(messages.value, incoming)
  if (messages.value.length !== before) {
    void nextTick(() => list.value?.scrollTo({ top: list.value.scrollHeight }))
  }
}

async function syncFromApi(): Promise<void> {
  if (!session.value) return
  try {
    receive((await fetchMessages(session.value.visitor_token)).map(fromApi))
  } catch {
    // 补齐失败不影响实时消息，下一轮再试。
  }
}

im.onState((next) => {
  const recovered = next === 'connected' && state.value !== 'connecting'
  state.value = next
  if (recovered) void syncFromApi()
})
im.onMessage((message) => {
  if (message.groupID === session.value?.im.group_id) {
    receive([fromIm(message, session.value.im.user_id)])
  }
})

async function start(key: string): Promise<void> {
  try {
    session.value = await initVisitor(key)
    const login = session.value.im
    await im.connect({
      userID: login.user_id,
      token: login.token,
      apiAddr: login.api_url,
      wsAddr: login.ws_url,
      platformID: login.platform_id,
    })
    await syncFromApi()
    timers.push(setTimeout(() => void syncFromApi(), SYNC_AFTER_CONNECT_MS))
    timers.push(
      setInterval(() => {
        if (document.visibilityState === 'visible') void syncFromApi()
      }, SYNC_INTERVAL_MS),
    )
  } catch (e) {
    error.value = e instanceof Error ? e.message : '客服暂时不可用，请稍后再试'
  }
}

async function send(): Promise<void> {
  const text = draft.value.trim()
  if (!session.value || !canSend.value) return
  sending.value = true
  try {
    const sent = await im.sendText(session.value.im.group_id, text)
    receive([fromIm(sent, session.value.im.user_id)])
    draft.value = ''
    error.value = null
  } catch {
    error.value = '发送失败，请重试'
  } finally {
    sending.value = false
  }
}

onMounted(() => {
  if (channelKey) void start(channelKey)
})
onBeforeUnmount(() => {
  timers.forEach((t) => clearTimeout(t))
  void im.disconnect()
})
</script>

<template>
  <div class="widget">
    <header class="header">
      <span class="title">在线客服</span>
      <span class="state" :class="state" data-testid="widget-state">{{ STATE_TEXT[state] }}</span>
    </header>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <ol ref="list" class="messages" data-testid="message-list">
      <li
        v-for="m in messages"
        :key="m.key"
        class="message"
        :class="m.role"
        data-testid="message"
      >
        <span v-if="m.role !== 'me'" class="sender">{{ senderLabel(m) }}</span>
        <span class="bubble">{{ m.text ?? '[暂不支持显示的消息]' }}</span>
      </li>
    </ol>
    <form class="composer" @submit.prevent="send">
      <textarea
        v-model="draft"
        rows="2"
        placeholder="请输入您的问题"
        data-testid="message-input"
        @keydown.enter.exact.prevent="send"
      />
      <button type="submit" :disabled="!canSend" data-testid="send-button">发送</button>
    </form>
  </div>
</template>
