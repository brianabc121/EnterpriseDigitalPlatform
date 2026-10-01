<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, nextTick, ref, watch } from 'vue'

import { ALERT_KIND, HANDOFF_REASON, WATCHER_ROLE } from '../../labels'
import { replySubject, replyTarget } from '../../mail'
import { useAuthStore } from '../../stores/auth'
import { useWorkbenchStore } from '../../stores/workbench'
import type { ReplyOrigin, WorkbenchMessage } from '../../workbench/messages'
import MessageContent from '../chat/MessageContent.vue'
import { IMAGE_TYPES, MAX_FILE_BYTES, MAX_IMAGE_BYTES } from '../../workbench/upload'
import AssistDialog from './AssistDialog.vue'
import QuickReplies from './QuickReplies.vue'
import SessionSummaryCard from './SessionSummaryCard.vue'
import SessionTodos from './SessionTodos.vue'
import TransferDialog from './TransferDialog.vue'

const wb = useWorkbenchStore()
// AI 在这次会话里登记、等待确认的待办（客户看不到）。
const canSeeTodos = useAuthStore().can('todo:read')
const transferOpen = ref(false)
const assistOpen = ref(false)
const acting = ref(false)

const STATUS_TEXT: Record<string, string> = {
  queued: '排队中',
  human_serving: '接待中',
  transferring: '转接中',
  ai_serving: 'AI 接待',
  closed: '已结束',
}
const draft = ref('')
/** 回复框内容的来源：用了 AI 建议、知识或快捷话术时记下，用于统计采纳率。 */
const draftOrigin = ref<ReplyOrigin>('manual')
const sending = ref(false)
const uploading = ref(false)
const suggesting = ref(false)
const suggestions = ref<string[] | null>(null)
const scroller = ref<HTMLElement | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)

const session = computed(() => wb.active)
/** 邮件会话（§10.8）：回复是一封邮件，可以改主题、选择回复哪一封。 */
const isEmail = computed(() => session.value?.channel_type === 'email')
/** 选择回复的那封客户邮件；为空时回复最近的一封。 */
const emailReplyTo = ref<string | null>(null)
const emailSubject = ref('')
const emailTarget = computed(() =>
  isEmail.value ? replyTarget(wb.activeMessages, emailReplyTo.value) : null,
)
const defaultSubject = computed(() => replySubject(emailTarget.value?.email?.subject ?? ''))
const pendingTransfer = computed(() =>
  session.value ? (wb.outgoing[session.value.id] ?? null) : null,
)
const canTransfer = computed(
  () => !!session.value && (wb.isMine(session.value) || wb.canManageOthers),
)
/** 我在这个会话里的身份：assignee（接待）、monitor（旁听）、assist（协助），都不是时为空。 */
const role = computed(() => (session.value ? wb.roleIn(session.value) : null))
const inHumanService = computed(
  () => !!session.value && ['human_serving', 'transferring'].includes(session.value.status),
)
const watchers = computed(() => (session.value ? wb.watchersOf(session.value) : []))
// 主管可以旁听自己能看到的、别人接待中或 AI 接待中的会话。
const canWatch = computed(
  () =>
    !!session.value && wb.canMonitor && session.value.status !== 'closed' && role.value === null,
)

async function act(action: () => Promise<void>, done: string): Promise<void> {
  if (acting.value) return
  acting.value = true
  try {
    await action()
    ElMessage.success(done)
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    acting.value = false
  }
}

async function returnToAi(): Promise<void> {
  const s = session.value
  if (!s) return
  try {
    await ElMessageBox.confirm(
      '会话交回智能客服继续接待，您会退出这个会话；客户需要时可以再次转人工。',
      '交还 AI',
      { confirmButtonText: '交还', cancelButtonText: '取消', type: 'info' },
    )
  } catch {
    return
  }
  await act(() => wb.returnToAi(s), '已交还 AI 接待')
}

function handoff(): void {
  const s = session.value
  if (s) void act(() => wb.handoff(s), '已转人工，正在分配坐席')
}

function startMonitor(): void {
  const s = session.value
  if (s) void act(() => wb.monitor(s), '已加入旁听，客户看不到您')
}

function leave(staffId?: string): void {
  const s = session.value
  if (s) void act(() => wb.leave(s, staffId), staffId ? '已请同事退出' : '已退出')
}

async function cancelTransfer(): Promise<void> {
  if (!pendingTransfer.value) return
  try {
    await wb.decideTransfer(pendingTransfer.value, 'cancel')
    ElMessage.info('已撤回转接')
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  }
}
// 坐席助手的实时提醒：只显示最近几条、没有点过"知道了"的。
const dismissed = ref(new Set<string>())
const sessionAlerts = computed(() =>
  session.value && session.value.status !== 'closed'
    ? (wb.alerts[session.value.id] ?? []).filter((a) => !dismissed.value.has(a.id)).slice(-3)
    : [],
)
function dismissAlert(id: string): void {
  dismissed.value = new Set([...dismissed.value, id])
}
// 人工接待过的会话结束后显示小结（接待坐席、主管可以确认写入客户档案）。
const showSummary = computed(
  () => !!session.value && session.value.status === 'closed' && !!session.value.assigned_at,
)

// AI 接待转人工（或 AI 优先却不能接待）时，给坐席看原因和交接摘要。
const handoffReason = computed(() => {
  const reason = session.value?.handoff_reason
  return reason ? (HANDOFF_REASON[reason] ?? reason) : null
})

// 接待这个会话的坐席和受邀协助的同事可以回复；旁听、查看他人的会话时只读。
const replyable = computed(
  () => inHumanService.value && (role.value === 'assignee' || role.value === 'assist'),
)
const readonlyText = computed(() => {
  const s = session.value
  if (!s) return ''
  if (s.status === 'closed') return '会话已结束'
  if (role.value === 'monitor') return '旁听中：只能查看，客户看不到您'
  if (s.status === 'ai_serving') return '智能客服接待中'
  if (s.status === 'queued') return '排队中，等待分配坐席'
  return '只读：这个会话由其他坐席接待'
})

// 微信客服等渠道的回复限制：客户最后一次发消息后 48 小时内最多 5 条。
const replyWindow = computed(() => {
  const w = wb.replyWindow
  return w && w.limited && session.value ? w : null
})
const windowClosed = computed(() => !!replyWindow.value && !replyWindow.value.open)
const windowText = computed(() => {
  const w = replyWindow.value
  if (!w) return ''
  if (!w.open) return w.reason ?? '暂时不能回复'
  const deadline = w.deadline ? formatDeadline(w.deadline) : ''
  return `微信客服：剩余 ${w.remaining ?? 0} 条${deadline ? ` / 截止 ${deadline}` : ''}`
})

function formatDeadline(value: string): string {
  const d = new Date(value)
  const today = new Date()
  const hm = d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
  if (d.toDateString() === today.toDateString()) return hm
  return `${d.getMonth() + 1}月${d.getDate()}日 ${hm}`
}

const LABEL: Record<string, string> = { customer: '客户', bot: '智能客服', system: '系统' }

function senderLabel(m: WorkbenchMessage): string {
  if (m.senderType === 'agent') return m.senderName ?? '客服'
  if (m.senderType === 'bot') return m.senderName ?? LABEL.bot!
  if (m.senderType === 'customer') return session.value?.customer_display_name ?? LABEL.customer!
  return LABEL[m.senderType] ?? ''
}

function time(m: WorkbenchMessage): string {
  return new Date(m.sentAt).toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

async function scrollToBottom(): Promise<void> {
  await nextTick()
  if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight
}

watch(() => wb.activeMessages.length, scrollToBottom)
watch(
  () => session.value?.id,
  () => {
    suggestions.value = null
    emailReplyTo.value = null
    emailSubject.value = ''
    void scrollToBottom()
  },
)

function chooseReply(m: WorkbenchMessage): void {
  emailReplyTo.value = m.id
  emailSubject.value = ''
}
// 知识检索面板点"插入回复框"。
watch(
  () => wb.composerInsert?.seq,
  () => {
    if (wb.composerInsert && replyable.value) insert(wb.composerInsert.text, 'knowledge')
  },
)

async function suggest(): Promise<void> {
  if (!session.value || suggesting.value) return
  suggesting.value = true
  try {
    const result = await wb.suggest(session.value)
    suggestions.value = result.suggestions
    if (!result.suggestions.length) ElMessage.info('没有找到可以参考的知识')
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    suggesting.value = false
  }
}

function useSuggestion(text: string): void {
  draft.value = text
  draftOrigin.value = 'suggestion'
  suggestions.value = null
}

watch(draft, (value) => {
  if (!value.trim()) draftOrigin.value = 'manual'
})

async function send(): Promise<void> {
  const text = draft.value.trim()
  if (!text || sending.value) return
  sending.value = true
  const origin = draftOrigin.value
  const email = isEmail.value
    ? { subject: emailSubject.value.trim() || null, replyTo: emailReplyTo.value }
    : {}
  draft.value = ''
  try {
    await wb.send({ type: 'text', text, origin, ...email })
    if (isEmail.value) {
      emailSubject.value = ''
      emailReplyTo.value = null
    }
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    sending.value = false
  }
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Enter' || event.isComposing) return
  // 邮件：Enter 换行，Ctrl+Enter（Mac 上 ⌘+Enter）发送；聊天：Enter 发送，Shift+Enter 换行。
  const sendNow = isEmail.value ? event.ctrlKey || event.metaKey : !event.shiftKey
  if (sendNow) {
    event.preventDefault()
    void send()
  }
}

async function retry(m: WorkbenchMessage): Promise<void> {
  try {
    await wb.retry(m)
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  }
}

async function closeSession(): Promise<void> {
  if (!session.value) return
  const message = isEmail.value
    ? '结束后不会给客户发邮件；客户再来信时会开始新的会话。'
    : '结束后客户会收到结束提示，再发消息时会开始新的会话。'
  try {
    await ElMessageBox.confirm(message, '结束会话', {
      confirmButtonText: '结束',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await wb.close(session.value)
    ElMessage.success('会话已结束')
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  }
}

async function onFile(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  const isImage = IMAGE_TYPES.includes(file.type)
  const limit = isImage ? MAX_IMAGE_BYTES : MAX_FILE_BYTES
  if (file.size > limit) {
    ElMessage.error(`${isImage ? '图片' : '文件'}不能超过 ${limit / 1024 / 1024} MB`)
    return
  }
  uploading.value = true
  try {
    await wb.sendFile(file)
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    uploading.value = false
  }
}

function insert(text: string, origin: ReplyOrigin = 'quick_reply'): void {
  draft.value = draft.value ? `${draft.value}\n${text}` : text
  draftOrigin.value = origin
}
</script>

<template>
  <section class="panel">
    <template v-if="session">
      <header class="header">
        <div>
          <span class="title" data-testid="chat-title">{{ session.customer_display_name }}</span>
          <el-tag size="small" :type="session.status === 'closed' ? 'info' : 'success'" class="tag">
            {{ STATUS_TEXT[session.status] ?? session.status }}
          </el-tag>
          <el-tag v-if="isEmail" size="small" type="info" class="tag" data-testid="chat-email-tag">
            邮件
          </el-tag>
          <span
            v-if="replyWindow"
            class="reply-window"
            :class="{ closed: windowClosed }"
            data-testid="reply-window"
          >
            {{ windowText }}
          </span>
          <span v-if="pendingTransfer" class="transferring" data-testid="transfer-pending">
            等待对方接受转接
            <el-button link type="primary" size="small" @click="cancelTransfer">撤回</el-button>
          </span>
          <span v-if="watchers.length" class="watchers" data-testid="session-watchers">
            <el-tag
              v-for="w in watchers"
              :key="w.staff_id"
              size="small"
              type="info"
              :closable="w.role === 'assist' && canTransfer && role !== 'assist'"
              class="watcher"
              @close="leave(w.staff_id)"
            >
              {{ WATCHER_ROLE[w.role] ?? w.role }}：{{ w.display_name }}
            </el-tag>
          </span>
        </div>
        <div class="header-actions">
          <el-button
            v-if="role === 'monitor' || role === 'assist'"
            size="small"
            :loading="acting"
            data-testid="leave-session"
            @click="leave()"
          >
            {{ role === 'monitor' ? '退出旁听' : '退出协助' }}
          </el-button>
          <el-button
            v-if="canWatch"
            size="small"
            :loading="acting"
            data-testid="monitor-session"
            @click="startMonitor"
          >
            旁听
          </el-button>
          <el-button
            v-if="session.status === 'ai_serving' && wb.canManageOthers"
            size="small"
            :loading="acting"
            data-testid="handoff-session"
            @click="handoff"
          >
            转人工
          </el-button>
          <el-button
            v-if="inHumanService && canTransfer && role !== 'assist'"
            size="small"
            data-testid="invite-assist"
            @click="assistOpen = true"
          >
            邀请协助
          </el-button>
          <el-button
            v-if="
              session.status === 'human_serving' && canTransfer && role !== 'assist' && !isEmail
            "
            size="small"
            :loading="acting"
            data-testid="return-to-ai"
            @click="returnToAi"
          >
            交还 AI
          </el-button>
          <el-button
            v-if="session.status === 'human_serving' && canTransfer && role !== 'assist'"
            size="small"
            data-testid="transfer-session"
            @click="transferOpen = true"
          >
            转接
          </el-button>
          <el-button
            v-if="session.status !== 'closed' && (wb.isMine(session) || wb.canManageOthers)"
            size="small"
            data-testid="close-session"
            @click="closeSession"
          >
            结束会话
          </el-button>
        </div>
      </header>
      <TransferDialog v-model="transferOpen" :session="session" />
      <AssistDialog v-model="assistOpen" :session="session" />
      <div v-if="handoffReason || session.ai_summary" class="handoff" data-testid="ai-summary">
        <span class="handoff-title"
          >转人工<template v-if="handoffReason">：{{ handoffReason }}</template></span
        >
        <p v-if="session.ai_summary">{{ session.ai_summary }}</p>
      </div>
      <div v-if="sessionAlerts.length" class="alerts" data-testid="copilot-alerts">
        <div
          v-for="a in sessionAlerts"
          :key="a.id"
          class="alert"
          :class="a.kind"
          data-testid="copilot-alert"
        >
          <el-tag size="small" :type="a.kind === 'promise' ? 'warning' : 'danger'" effect="plain">
            {{ ALERT_KIND[a.kind] ?? '提醒' }}
          </el-tag>
          <span class="alert-text">{{ a.text }}</span>
          <el-button link size="small" @click="dismissAlert(a.id)">知道了</el-button>
        </div>
      </div>

      <div ref="scroller" class="messages" data-testid="chat-messages">
        <div v-if="wb.hasMore[session.room_id]" class="more">
          <el-button link size="small" @click="wb.loadOlder()">查看更早的消息</el-button>
        </div>
        <div
          v-for="m in wb.activeMessages"
          :key="m.key"
          class="message"
          :class="[m.senderType, { mine: m.senderType === 'agent' }]"
          data-testid="chat-message"
        >
          <template v-if="m.senderType === 'system'">
            <div class="notice">{{ m.text }}</div>
          </template>
          <template v-else>
            <div class="meta">
              {{ senderLabel(m) }}
              <span v-if="m.senderType === 'bot'" class="ai-badge">AI</span>
              · {{ time(m) }}
            </div>
            <div class="bubble" :class="{ email: !!m.email }">
              <MessageContent :message="m" :replyable="replyable" @reply="chooseReply" />
            </div>
            <div v-if="m.status === 'pending'" class="status">发送中…</div>
            <div v-else-if="m.status === 'failed'" class="status failed" data-testid="send-failed">
              发送失败<template v-if="m.error">：{{ m.error }}</template>
              <el-button
                v-if="m.senderType === 'agent'"
                link
                type="primary"
                size="small"
                @click="retry(m)"
              >
                重试
              </el-button>
            </div>
          </template>
        </div>
      </div>

      <SessionSummaryCard v-if="showSummary" :session-id="session.id" :can-write="canTransfer" />
      <SessionTodos v-if="canSeeTodos" :key="session.id" :session-id="session.id" />
      <footer v-if="replyable" class="composer">
        <div class="tools">
          <QuickReplies @pick="(text: string) => insert(text, 'quick_reply')" />
          <el-button
            size="small"
            :loading="uploading"
            data-testid="attach-button"
            @click="fileInput?.click()"
          >
            图片/文件
          </el-button>
          <input
            ref="fileInput"
            type="file"
            hidden
            accept="image/png,image/jpeg,image/gif,image/webp,.pdf,.txt,.zip,.doc,.docx,.xls,.xlsx,.ppt,.pptx"
            data-testid="attach-input"
            @change="onFile"
          />
          <el-button
            size="small"
            :loading="suggesting"
            data-testid="suggest-button"
            @click="suggest"
          >
            AI 建议
          </el-button>
        </div>
        <div v-if="suggestions?.length" class="suggestions" data-testid="suggestions">
          <div class="suggestions-head">
            <span>点击使用，发送前可以修改</span>
            <el-button link size="small" @click="suggestions = null">收起</el-button>
          </div>
          <button
            v-for="(text, i) in suggestions"
            :key="i"
            type="button"
            class="suggestion"
            data-testid="suggestion"
            @click="useSuggestion(text)"
          >
            <span class="suggestion-text">{{ text }}</span>
          </button>
        </div>
        <div v-if="windowClosed" class="window-closed" data-testid="window-closed">
          {{ replyWindow?.reason }}
        </div>
        <div v-if="isEmail" class="email-head" data-testid="email-compose">
          <div class="email-target">
            <span v-if="emailTarget" data-testid="email-reply-target">
              回复：{{ emailTarget.email?.subject || '（无主题）' }}
              <template v-if="!emailReplyTo">（最近的一封）</template>
            </span>
            <span v-else>回复客户的邮件</span>
            <el-button v-if="emailReplyTo" link size="small" @click="emailReplyTo = null">
              改回最近的一封
            </el-button>
          </div>
          <el-input
            v-model="emailSubject"
            size="small"
            maxlength="300"
            :placeholder="defaultSubject"
            data-testid="email-subject-input"
          >
            <template #prepend>主题</template>
          </el-input>
        </div>
        <el-input
          v-model="draft"
          type="textarea"
          :rows="isEmail ? 6 : 3"
          resize="none"
          :disabled="windowClosed"
          :placeholder="
            isEmail
              ? '输入邮件正文，Ctrl+Enter 发送；签名和引用的原邮件会自动附上'
              : '输入回复，Enter 发送，Shift+Enter 换行'
          "
          data-testid="composer-input"
          @keydown="onKeydown"
        />
        <div class="actions">
          <el-button
            type="primary"
            :loading="sending"
            :disabled="windowClosed"
            data-testid="send-button"
            @click="send"
          >
            {{ isEmail ? '发送邮件' : '发送' }}
          </el-button>
        </div>
      </footer>
      <footer v-else class="readonly" data-testid="chat-readonly">
        {{ readonlyText }}
      </footer>
    </template>
    <div v-else class="empty">从左侧选择一个会话开始接待</div>
  </section>
</template>

<style scoped>
.panel {
  display: flex;
  flex-direction: column;
}

.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

/* 标题一侧可以换行，按钮保持一行。 */
.header > :first-child {
  flex: 1;
  min-width: 0;
}

.title {
  font-weight: 600;
}

.tag {
  margin-left: 8px;
}

.alerts {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 6px 16px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  background: var(--el-color-warning-light-9);
}

.alert {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}

.alert-text {
  flex: 1;
  min-width: 0;
}

.reply-window {
  margin-left: 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.reply-window.closed {
  color: var(--el-color-danger);
}

.window-closed {
  margin-bottom: 6px;
  font-size: 12px;
  color: var(--el-color-danger);
}

.transferring {
  margin-left: 8px;
  font-size: 12px;
  color: var(--el-color-warning);
}

.header-actions {
  display: flex;
  flex-shrink: 0;
  gap: 8px;
}

.header-actions .el-button + .el-button {
  margin-left: 0;
}

.watchers {
  margin-left: 8px;
}

.watcher {
  margin-right: 4px;
}

.messages {
  flex: 1;
  overflow-y: auto;
  padding: 12px 16px;
  background: var(--el-fill-color-lighter);
}

.more {
  text-align: center;
}

.message {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  margin: 8px 0;
}

.message.mine {
  align-items: flex-end;
}

.meta,
.status {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin: 2px 4px;
}

.status.failed {
  color: var(--el-color-danger);
}

.bubble {
  max-width: 70%;
  padding: 8px 12px;
  border-radius: 8px;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  white-space: pre-wrap;
  word-break: break-word;
}

.bubble.email {
  max-width: 86%;
}

.email-head {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-bottom: 6px;
}

.email-target {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.mine .bubble {
  background: var(--el-color-primary-light-8);
  border-color: var(--el-color-primary-light-7);
}

.bot .bubble {
  background: var(--el-color-success-light-9);
}

.ai-badge {
  display: inline-block;
  padding: 0 4px;
  margin-left: 2px;
  border-radius: 3px;
  font-size: 10px;
  line-height: 14px;
  color: var(--el-color-success);
  border: 1px solid var(--el-color-success-light-5);
}

.handoff {
  padding: 8px 16px;
  font-size: 13px;
  background: var(--el-color-warning-light-9);
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.handoff-title {
  font-weight: 600;
  color: var(--el-color-warning-dark-2);
}

.handoff p {
  margin: 4px 0 0;
  white-space: pre-wrap;
  color: var(--el-text-color-regular);
}

.suggestions {
  margin-bottom: 6px;
  padding: 6px 8px;
  border-radius: 6px;
  background: var(--el-fill-color-lighter);
}

.suggestions-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.suggestion {
  display: block;
  width: 100%;
  margin-top: 4px;
  padding: 6px 10px;
  text-align: left;
  font: inherit;
  font-size: 13px;
  color: var(--el-text-color-primary);
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  cursor: pointer;
}

.suggestion-text {
  white-space: pre-wrap;
}

.suggestion:hover {
  border-color: var(--el-color-primary-light-5);
}

.message.system {
  align-items: center;
}

.notice {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  background: var(--el-fill-color);
  padding: 2px 10px;
  border-radius: 10px;
}

.composer {
  border-top: 1px solid var(--el-border-color-lighter);
  padding: 8px 12px 12px;
}

.tools {
  display: flex;
  gap: 8px;
  margin-bottom: 6px;
}

.actions {
  display: flex;
  justify-content: flex-end;
  margin-top: 8px;
}

.readonly,
.empty {
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.readonly {
  padding: 16px;
  border-top: 1px solid var(--el-border-color-lighter);
}

.empty {
  flex: 1;
}
</style>
