<script setup lang="ts">
import { createImClient, type ChatMessage, type ConnectionState } from '@edp/im-client'
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'

import { mergeMessages, SENDER_LABEL, senderRole, type SenderRole } from './chat'
import { initVisitor, type VisitorSession } from './visitor'

const STATE_TEXT: Record<ConnectionState, string> = {
  idle: '未连接',
  connecting: '连接中…',
  connected: '在线',
  reconnecting: '重新连接中…',
  failed: '连接失败',
  kicked: '已在其他窗口打开',
  expired: '会话已过期，请刷新页面',
}

const channelKey = new URLSearchParams(location.search).get('key')
const im = createImClient()
const session = ref<VisitorSession | null>(null)
const state = ref<ConnectionState>('idle')
const messages = ref<ChatMessage[]>([])
const draft = ref('')
const sending = ref(false)
const error = ref<string | null>(channelKey ? null : '缺少渠道参数 key')
const list = ref<HTMLElement | null>(null)

const canSend = computed(
  () => state.value === 'connected' && draft.value.trim().length > 0 && !sending.value,
)

function roleOf(message: ChatMessage): SenderRole {
  return senderRole(message.sendID, session.value?.im.user_id ?? '')
}

/** 人工客服显示坐席姓名（发送时带的昵称），其他发送者显示固定称呼。 */
function senderLabel(message: ChatMessage): string {
  const role = roleOf(message)
  return role === 'agent' && message.senderNickname ? message.senderNickname : SENDER_LABEL[role]
}

function receive(incoming: ChatMessage[]): void {
  messages.value = mergeMessages(messages.value, incoming)
  void nextTick(() => list.value?.scrollTo({ top: list.value.scrollHeight }))
}

im.onState((next) => (state.value = next))
im.onMessage((message) => {
  if (message.groupID === session.value?.im.group_id) receive([message])
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
    receive(await im.history(login.conversation_id, 50))
  } catch (e) {
    error.value = e instanceof Error ? e.message : '客服暂时不可用，请稍后再试'
  }
}

async function send(): Promise<void> {
  const text = draft.value.trim()
  if (!session.value || !canSend.value) return
  sending.value = true
  try {
    receive([await im.sendText(session.value.im.group_id, text)])
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
onBeforeUnmount(() => void im.disconnect())
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
        :key="m.clientMsgID"
        class="message"
        :class="roleOf(m)"
        data-testid="message"
      >
        <span v-if="roleOf(m) !== 'me'" class="sender">{{ senderLabel(m) }}</span>
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
