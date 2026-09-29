<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, ref, watch } from 'vue'

import { api, formatDateTime } from '../../api'
import {
  ASSIGN_VIA,
  CLOSE_REASON,
  HANDOFF_REASON,
  SESSION_EVENT,
  SESSION_STATUS,
  SESSION_STATUS_TAG,
  formatDuration,
  secondsBetween,
} from '../../labels'
import { fromApi, type WorkbenchMessage } from '../../workbench/messages'
import AiOutcomeCard from '../ai/AiOutcomeCard.vue'
import MessageContent from '../chat/MessageContent.vue'

const props = defineProps<{ sessionId: string | null; staffNames: Map<string, string> }>()
const emit = defineEmits<{ close: [] }>()

const PAGE = 100
const detail = ref<Schemas['SessionDetail'] | null>(null)
const messages = ref<WorkbenchMessage[]>([])
const hasMore = ref(false)
const loading = ref(false)
const decisions = ref<Schemas['AiDecisionOut'][]>([])

const open = computed({
  get: () => props.sessionId !== null,
  set: (value) => {
    if (!value) emit('close')
  },
})

const LABEL: Record<string, string> = { bot: '智能客服', system: '系统' }
// 转接会刷新会话上的分配时间，首次响应从第一次分配算起（与报表一致）。
const firstAssigned = computed(
  () => detail.value?.events.find((e) => e.type === 'assigned')?.created_at ?? null,
)

function sender(m: WorkbenchMessage): string {
  if (m.senderType === 'customer') return detail.value?.customer_display_name ?? '客户'
  if (m.senderType === 'agent') return m.senderName ?? '客服'
  if (m.senderType === 'bot') return m.senderName ?? LABEL.bot!
  return LABEL[m.senderType] ?? ''
}

function staffName(id: unknown): string {
  return typeof id === 'string' ? (props.staffNames.get(id) ?? '') : ''
}

function eventText(event: Schemas['SessionEventOut']): string {
  const title = SESSION_EVENT[event.type] ?? event.type
  const p = event.payload as Record<string, unknown>
  if (event.type === 'assigned') {
    const via = ASSIGN_VIA[String(p.via)] ?? ''
    return [title, staffName(p.staff_id), via && `（${via}）`].filter(Boolean).join(' ')
  }
  if (event.type === 'closed') return `${title}（${CLOSE_REASON[String(p.reason)] ?? p.reason}）`
  if (event.type === 'handoff') return `${title}（${HANDOFF_REASON[String(p.reason)] ?? p.reason}）`
  if (event.type === 'csat' && typeof p.score === 'number') return `${title}：${p.score} 分`
  if (event.type === 'transferred' && p.to_staff_id) {
    return `${title}：${staffName(p.from_staff_id) || '—'} → ${staffName(p.to_staff_id) || '—'}`
  }
  return title
}

/** AI 接待过的会话：每一轮的判定（回复内容、依据、信号、转人工原因）。 */
async function loadDecisions(id: string): Promise<void> {
  const { data } = await api.GET('/api/v1/sessions/{session_id}/ai-decisions', {
    params: { path: { session_id: id } },
  })
  decisions.value = data?.items ?? []
}

async function loadMessages(before?: string): Promise<void> {
  if (!props.sessionId) return
  const { data, error } = await api.GET('/api/v1/sessions/{session_id}/messages', {
    params: { path: { session_id: props.sessionId }, query: { limit: PAGE, before } },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  // 接口按时间倒序返回，这里按时间正序显示。
  messages.value = [...data.items.map(fromApi).reverse(), ...(before ? messages.value : [])]
  hasMore.value = data.has_more
}

watch(
  () => props.sessionId,
  async (id) => {
    detail.value = null
    messages.value = []
    decisions.value = []
    if (!id) return
    loading.value = true
    const { data, error } = await api.GET('/api/v1/sessions/{session_id}', {
      params: { path: { session_id: id } },
    })
    if (!data) {
      loading.value = false
      ElMessage.error(errorMessage(error))
      return
    }
    detail.value = data
    const servedByAi = data.events.some((e) => e.type === 'ai_serving')
    await Promise.all([loadMessages(), servedByAi ? loadDecisions(id) : undefined])
    loading.value = false
  },
)
</script>

<template>
  <el-drawer v-model="open" title="会话记录" size="640px">
    <div v-loading="loading" class="body" data-testid="session-drawer">
      <template v-if="detail">
        <el-descriptions :column="2" size="small" border>
          <el-descriptions-item label="客户">{{
            detail.customer_display_name
          }}</el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag size="small" :type="SESSION_STATUS_TAG[detail.status]">
              {{ SESSION_STATUS[detail.status] ?? detail.status }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="接待坐席">
            {{ detail.assignee_display_name ?? '—' }}
          </el-descriptions-item>
          <el-descriptions-item label="开始时间">
            {{ formatDateTime(detail.created_at) }}
          </el-descriptions-item>
          <el-descriptions-item label="首次响应">
            {{ formatDuration(secondsBetween(firstAssigned, detail.first_response_at)) }}
          </el-descriptions-item>
          <el-descriptions-item label="结束">
            <template v-if="detail.closed_at">
              {{ formatDateTime(detail.closed_at) }}
              <span class="muted">{{ CLOSE_REASON[detail.close_reason ?? ''] ?? '' }}</span>
            </template>
            <template v-else>—</template>
          </el-descriptions-item>
          <el-descriptions-item v-if="detail.handoff_reason" label="转人工原因" :span="2">
            {{ HANDOFF_REASON[detail.handoff_reason] ?? detail.handoff_reason }}
          </el-descriptions-item>
          <el-descriptions-item v-if="detail.ai_summary" label="交接摘要" :span="2">
            <span class="summary" data-testid="drawer-ai-summary">{{ detail.ai_summary }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="满意度" :span="2">
            <template v-if="detail.csat">
              <el-rate :model-value="detail.csat" disabled size="small" />
              <span v-if="detail.csat_comment" class="muted">“{{ detail.csat_comment }}”</span>
            </template>
            <span v-else class="muted">未评价</span>
          </el-descriptions-item>
        </el-descriptions>

        <h4>对话</h4>
        <div class="messages" data-testid="session-transcript">
          <div v-if="hasMore" class="more">
            <el-button link size="small" @click="loadMessages(messages[0]?.id ?? undefined)">
              查看更早的消息
            </el-button>
          </div>
          <div
            v-for="m in messages"
            :key="m.key"
            class="message"
            :class="m.senderType"
            data-testid="transcript-message"
          >
            <div v-if="m.senderType === 'system'" class="notice">{{ m.text }}</div>
            <template v-else>
              <div class="meta">
                {{ sender(m) }}
                <span v-if="m.senderType === 'bot'" class="ai-badge">AI</span>
                · {{ formatDateTime(new Date(m.sentAt).toISOString()) }}
              </div>
              <div class="bubble"><MessageContent :message="m" /></div>
            </template>
          </div>
          <el-empty v-if="messages.length === 0" description="没有消息" :image-size="60" />
        </div>

        <template v-if="decisions.length">
          <h4>AI 接待</h4>
          <div class="decisions" data-testid="ai-decisions">
            <div v-for="d in decisions" :key="d.id" class="decision">
              <div class="decision-time">{{ formatDateTime(d.created_at) }}</div>
              <AiOutcomeCard :outcome="d" :question="d.question" />
            </div>
          </div>
        </template>

        <h4>过程</h4>
        <el-timeline>
          <el-timeline-item
            v-for="e in detail.events"
            :key="e.id"
            :timestamp="formatDateTime(e.created_at)"
            size="small"
          >
            {{ eventText(e) }}
          </el-timeline-item>
        </el-timeline>
      </template>
    </div>
  </el-drawer>
</template>

<style scoped>
h4 {
  margin: 20px 0 8px;
  font-size: 14px;
}

.muted {
  margin-left: 6px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.messages {
  max-height: 420px;
  overflow-y: auto;
  padding: 8px 12px;
  background: var(--el-fill-color-lighter);
  border-radius: 4px;
}

.more {
  text-align: center;
}

.message {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  margin: 6px 0;
}

.message.agent,
.message.bot {
  align-items: flex-end;
}

.message.system {
  align-items: center;
}

.meta {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin: 2px 4px;
}

.bubble {
  max-width: 80%;
  padding: 6px 10px;
  border-radius: 6px;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  white-space: pre-wrap;
  word-break: break-word;
}

.agent .bubble {
  background: var(--el-color-primary-light-8);
}

.bot .bubble {
  background: var(--el-color-success-light-9);
}

.ai-badge {
  display: inline-block;
  padding: 0 4px;
  border-radius: 3px;
  font-size: 10px;
  line-height: 14px;
  color: var(--el-color-success);
  border: 1px solid var(--el-color-success-light-5);
}

.summary {
  white-space: pre-wrap;
}

.decision {
  padding: 10px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.decision:last-child {
  border-bottom: none;
}

.decision-time {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-bottom: 4px;
}

.notice {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  background: var(--el-fill-color);
  padding: 2px 10px;
  border-radius: 10px;
}
</style>
