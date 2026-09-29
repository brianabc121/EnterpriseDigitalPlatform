<script setup lang="ts">
import { createImClient, type ConnectionState } from '@edp/im-client'
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'

import { fromApi, fromIm, mergeMessages, senderLabel, type WidgetMessage } from './chat'
import {
  embedOrigin,
  fetchMessages,
  fetchState,
  imageSize,
  initVisitor,
  leaveMessage,
  rate,
  requestHuman,
  upload,
  type SessionState,
  type VisitorSession,
} from './visitor'

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

const query = new URLSearchParams(location.search)
const channelKey = query.get('key')
const embedded = query.get('embed') === '1'
const parentOrigin = embedded ? embedOrigin() : ''

const im = createImClient()
const session = ref<VisitorSession | null>(null)
const service = ref<SessionState | null>(null)
const state = ref<ConnectionState>('idle')
const messages = ref<WidgetMessage[]>([])
const draft = ref('')
const sending = ref(false)
const error = ref<string | null>(channelKey ? null : '缺少渠道参数 key')
const list = ref<HTMLElement | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)
const noticeRead = ref(false)
const panel = ref<'chat' | 'leave'>('chat')
const leaveForm = ref({ content: '', contact: '', sent: false })
const csat = ref({ score: 0, comment: '', done: false })
const visible = ref(!embedded)
const unread = ref(0)
const timers: ReturnType<typeof setTimeout>[] = []

const connected = computed(() => state.value === 'connected')
const canSend = computed(() => connected.value && draft.value.trim().length > 0 && !sending.value)
const title = computed(() => session.value?.widget.title ?? '在线客服')
const banner = computed(() => {
  const s = service.value
  if (!s) return null
  if (s.status === 'queued') {
    const ahead = Math.max(0, (s.queue_position ?? 1) - 1)
    return ahead > 0 ? `正在为您转接人工客服，前面还有 ${ahead} 位` : '正在为您转接人工客服，请稍候'
  }
  if ((s.status === 'human_serving' || s.status === 'transferring') && s.assignee_name) {
    return `客服 ${s.assignee_name} 正在为您服务`
  }
  if (s.status === 'ai_serving') return '智能客服为您服务'
  return null
})
const askRating = computed(
  () =>
    service.value?.status === 'closed' &&
    service.value.session_id &&
    service.value.csat == null &&
    !csat.value.done,
)
const showPrivacy = computed(
  () =>
    !!session.value?.widget.privacy_notice &&
    !noticeRead.value &&
    !messages.value.some((m) => m.role === 'me'),
)

function isImage(m: WidgetMessage): boolean {
  return !!m.attachment && (m.attachment.width !== null || m.attachment.name === null)
}

function formatSize(size: number | null): string {
  if (!size) return ''
  return size >= 1024 * 1024
    ? `${(size / 1024 / 1024).toFixed(1)} MB`
    : `${Math.ceil(size / 1024)} KB`
}

function notifyParent(): void {
  if (embedded && parentOrigin) {
    window.parent.postMessage({ type: 'edp:unread', count: unread.value }, parentOrigin)
  }
}

function receive(incoming: WidgetMessage[]): void {
  const known = new Set(messages.value.map((m) => m.key))
  const fresh = incoming.filter((m) => !known.has(m.key) && m.role !== 'me')
  messages.value = mergeMessages(messages.value, incoming)
  if (fresh.length && !visible.value) {
    unread.value += fresh.length
    notifyParent()
  }
  if (fresh.length || incoming.some((m) => m.role === 'me')) {
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

async function refreshState(): Promise<void> {
  if (!session.value) return
  try {
    service.value = await fetchState(session.value.visitor_token)
  } catch {
    // 下一轮再试。
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
    // 系统提示和智能客服的消息往往伴随服务状态变化（开始接待、转人工），刷新一次横幅。
    if (message.sendID.endsWith('_sys') || message.sendID.endsWith('_bot')) void refreshState()
  }
})

window.addEventListener('message', (event) => {
  if (!embedded || event.origin !== parentOrigin) return
  const type = (event.data as { type?: string } | null)?.type
  if (type === 'edp:open') {
    visible.value = true
    unread.value = 0
    void nextTick(() => list.value?.scrollTo({ top: list.value.scrollHeight }))
  } else if (type === 'edp:hidden') {
    visible.value = false
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
    await Promise.all([syncFromApi(), refreshState()])
    timers.push(setTimeout(() => void syncFromApi(), SYNC_AFTER_CONNECT_MS))
    timers.push(
      setInterval(() => {
        if (document.visibilityState === 'visible') {
          void syncFromApi()
          void refreshState()
        }
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
    noticeRead.value = true
    timers.push(setTimeout(() => void refreshState(), 1500))
  } catch {
    error.value = '发送失败，请重试'
  } finally {
    sending.value = false
  }
}

async function sendFile(event: Event): Promise<void> {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (fileInput.value) fileInput.value.value = ''
  if (!file || !session.value || !connected.value) return
  sending.value = true
  try {
    const uploaded = await upload(session.value.visitor_token, file)
    const group = session.value.im.group_id
    const sent =
      uploaded.kind === 'image'
        ? await im.sendImage(group, {
            url: uploaded.file_url,
            type: file.type,
            size: file.size,
            ...(await imageSize(file)),
          })
        : await im.sendFile(group, {
            url: uploaded.file_url,
            name: file.name,
            size: file.size,
            type: file.type,
          })
    receive([fromIm(sent, session.value.im.user_id)])
    error.value = null
  } catch (e) {
    error.value = e instanceof Error ? e.message : '发送失败，请重试'
  } finally {
    sending.value = false
  }
}

async function askHuman(): Promise<void> {
  if (!session.value) return
  try {
    service.value = await requestHuman(session.value.visitor_token)
  } catch (e) {
    error.value = e instanceof Error ? e.message : '请稍后再试'
  }
}

async function submitRating(): Promise<void> {
  const s = service.value
  if (!session.value || !s?.session_id || !csat.value.score) return
  try {
    await rate(session.value.visitor_token, s.session_id, csat.value.score, csat.value.comment)
    csat.value.done = true
  } catch (e) {
    error.value = e instanceof Error ? e.message : '评价失败'
  }
}

async function submitLeave(): Promise<void> {
  if (!session.value || !leaveForm.value.content.trim()) return
  try {
    await leaveMessage(
      session.value.visitor_token,
      leaveForm.value.content.trim(),
      leaveForm.value.contact.trim(),
    )
    leaveForm.value = { content: '', contact: '', sent: true }
    error.value = null
  } catch (e) {
    error.value = e instanceof Error ? e.message : '留言失败，请稍后再试'
  }
}

function close(): void {
  if (embedded && parentOrigin) window.parent.postMessage({ type: 'edp:close' }, parentOrigin)
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
      <span class="title" data-testid="widget-title">{{ title }}</span>
      <span class="header-actions">
        <span class="state" :class="state" data-testid="widget-state">{{ STATE_TEXT[state] }}</span>
        <button
          type="button"
          class="link"
          data-testid="leave-message-tab"
          @click="panel = panel === 'chat' ? 'leave' : 'chat'"
        >
          {{ panel === 'chat' ? '留言' : '返回' }}
        </button>
        <button v-if="embedded" type="button" class="link" aria-label="收起" @click="close">
          ✕
        </button>
      </span>
    </header>
    <p v-if="banner && panel === 'chat'" class="banner" data-testid="service-banner">
      {{ banner }}
      <button
        v-if="service?.status === 'ai_serving'"
        type="button"
        class="link"
        data-testid="ask-human"
        @click="askHuman"
      >
        转人工
      </button>
    </p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>

    <template v-if="panel === 'chat'">
      <ol ref="list" class="messages" data-testid="message-list">
        <li
          v-if="session?.widget.welcome_message"
          class="message bot welcome"
          data-testid="welcome"
        >
          <span class="sender">{{ title }}</span>
          <span class="bubble">{{ session.widget.welcome_message }}</span>
        </li>
        <li
          v-for="m in messages"
          :key="m.key"
          class="message"
          :class="m.role"
          data-testid="message"
        >
          <span v-if="m.role !== 'me'" class="sender">
            {{ senderLabel(m) }}
            <span v-if="m.role === 'bot'" class="ai-badge" data-testid="ai-badge">AI</span>
          </span>
          <a
            v-if="m.attachment && isImage(m)"
            :href="m.attachment.url"
            target="_blank"
            rel="noopener"
            class="bubble image"
          >
            <img :src="m.attachment.url" alt="图片" data-testid="message-image" />
          </a>
          <a
            v-else-if="m.attachment"
            :href="m.attachment.url"
            target="_blank"
            rel="noopener"
            class="bubble file"
            data-testid="message-file"
          >
            📎 {{ m.attachment.name ?? '文件' }}
            <small>{{ formatSize(m.attachment.size) }}</small>
          </a>
          <span v-else class="bubble">{{ m.text ?? '[暂不支持显示的消息]' }}</span>
        </li>
      </ol>

      <div v-if="askRating" class="rating" data-testid="csat">
        <p>本次服务您满意吗？</p>
        <div class="stars">
          <button
            v-for="n in 5"
            :key="n"
            type="button"
            :class="{ on: n <= csat.score }"
            :aria-label="`${n} 分`"
            :data-testid="`csat-${n}`"
            @click="csat.score = n"
          >
            ★
          </button>
        </div>
        <input v-model="csat.comment" maxlength="500" placeholder="说说您的建议（选填）" />
        <button
          type="button"
          :disabled="!csat.score"
          data-testid="csat-submit"
          @click="submitRating"
        >
          提交评价
        </button>
      </div>
      <p v-else-if="csat.done" class="thanks" data-testid="csat-thanks">感谢您的评价！</p>

      <p v-if="showPrivacy" class="privacy" data-testid="privacy-notice">
        {{ session?.widget.privacy_notice }}
        <button type="button" class="link" @click="noticeRead = true">知道了</button>
      </p>

      <form class="composer" @submit.prevent="send">
        <button
          type="button"
          class="attach"
          :disabled="!connected || sending"
          aria-label="发送图片或文件"
          @click="fileInput?.click()"
        >
          ＋
        </button>
        <input
          ref="fileInput"
          type="file"
          hidden
          accept="image/png,image/jpeg,image/gif,image/webp,.pdf,.txt,.zip,.doc,.docx,.xls,.xlsx,.ppt,.pptx"
          data-testid="file-input"
          @change="sendFile"
        />
        <textarea
          v-model="draft"
          rows="2"
          placeholder="请输入您的问题"
          data-testid="message-input"
          @keydown.enter.exact.prevent="send"
        />
        <button type="submit" :disabled="!canSend" data-testid="send-button">发送</button>
      </form>
    </template>

    <form v-else class="leave" data-testid="leave-message" @submit.prevent="submitLeave">
      <p v-if="leaveForm.sent" class="thanks" data-testid="leave-thanks">
        留言已提交，我们会尽快联系您。
      </p>
      <label>
        留言内容
        <textarea v-model="leaveForm.content" rows="5" maxlength="2000" required />
      </label>
      <label>
        联系方式（选填）
        <input v-model="leaveForm.contact" maxlength="128" placeholder="手机号或邮箱" />
      </label>
      <button type="submit" :disabled="!leaveForm.content.trim()">提交留言</button>
    </form>
  </div>
</template>
