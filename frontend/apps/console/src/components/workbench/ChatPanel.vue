<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, nextTick, ref, watch } from 'vue'

import { useWorkbenchStore } from '../../stores/workbench'
import type { WorkbenchMessage } from '../../workbench/messages'
import QuickReplies from './QuickReplies.vue'

const wb = useWorkbenchStore()
const draft = ref('')
const sending = ref(false)
const scroller = ref<HTMLElement | null>(null)

const session = computed(() => wb.active)
// 只有接待这个会话的坐席可以回复；主管查看他人的会话时只读。
const replyable = computed(() => {
  const s = session.value
  return !!s && ['human_serving', 'transferring'].includes(s.status) && wb.isMine(s)
})

const LABEL: Record<string, string> = { customer: '客户', bot: '智能客服', system: '系统' }

function senderLabel(m: WorkbenchMessage): string {
  if (m.senderType === 'agent') return m.senderName ?? '客服'
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
watch(() => session.value?.id, scrollToBottom)

async function send(): Promise<void> {
  const text = draft.value.trim()
  if (!text || sending.value) return
  sending.value = true
  draft.value = ''
  try {
    await wb.send(text)
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    sending.value = false
  }
}

function onKeydown(event: KeyboardEvent): void {
  // Enter 发送，Shift+Enter 换行；输入法组字时不发送。
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
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
  try {
    await ElMessageBox.confirm('结束后客户会收到结束提示，再发消息时会开始新的会话。', '结束会话', {
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

function insert(text: string): void {
  draft.value = draft.value ? `${draft.value}\n${text}` : text
}
</script>

<template>
  <section class="panel">
    <template v-if="session">
      <header class="header">
        <div>
          <span class="title" data-testid="chat-title">{{ session.customer_display_name }}</span>
          <el-tag size="small" :type="session.status === 'closed' ? 'info' : 'success'" class="tag">
            {{
              session.status === 'closed'
                ? '已结束'
                : session.status === 'queued'
                  ? '排队中'
                  : '接待中'
            }}
          </el-tag>
        </div>
        <el-button
          v-if="session.status !== 'closed' && (wb.isMine(session) || wb.canManageOthers)"
          size="small"
          data-testid="close-session"
          @click="closeSession"
        >
          结束会话
        </el-button>
      </header>

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
            <div class="meta">{{ senderLabel(m) }} · {{ time(m) }}</div>
            <div class="bubble">
              <template v-if="m.text !== null">{{ m.text }}</template>
              <span v-else class="unsupported">[{{ m.contentType }}]</span>
            </div>
            <div v-if="m.status === 'pending'" class="status">发送中…</div>
            <div v-else-if="m.status === 'failed'" class="status failed">
              发送失败
              <el-button link type="primary" size="small" @click="retry(m)">重试</el-button>
            </div>
          </template>
        </div>
      </div>

      <footer v-if="replyable" class="composer">
        <div class="tools">
          <QuickReplies @pick="insert" />
        </div>
        <el-input
          v-model="draft"
          type="textarea"
          :rows="3"
          resize="none"
          placeholder="输入回复，Enter 发送，Shift+Enter 换行"
          data-testid="composer-input"
          @keydown="onKeydown"
        />
        <div class="actions">
          <el-button type="primary" :loading="sending" data-testid="send-button" @click="send">
            发送
          </el-button>
        </div>
      </footer>
      <footer v-else class="readonly">
        {{ session.status === 'closed' ? '会话已结束' : '只读：这个会话由其他坐席接待' }}
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
  padding: 10px 16px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.title {
  font-weight: 600;
}

.tag {
  margin-left: 8px;
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

.mine .bubble {
  background: var(--el-color-primary-light-8);
  border-color: var(--el-color-primary-light-7);
}

.bot .bubble {
  background: var(--el-color-success-light-9);
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

.unsupported {
  color: var(--el-text-color-secondary);
}

.composer {
  border-top: 1px solid var(--el-border-color-lighter);
  padding: 8px 12px 12px;
}

.tools {
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
