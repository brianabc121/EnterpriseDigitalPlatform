<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { nextTick, onMounted, ref } from 'vue'

import { api, formatDateTime } from '../../api'
import { TOOL_NAME, type ChatMessage } from '../../assistant'

/** 在控制台里直接和助理对话：与 IM 里一样，以本人的权限查询待办、订单、客户、知识库，或记一件事。 */
const messages = ref<ChatMessage[]>([])
const text = ref('')
const sending = ref(false)
const loading = ref(false)
const list = ref<HTMLElement | null>(null)

async function scrollDown(): Promise<void> {
  await nextTick()
  if (list.value) list.value.scrollTop = list.value.scrollHeight
}

async function load(): Promise<void> {
  loading.value = true
  const { data } = await api.GET('/api/v1/assistant/chat/history')
  loading.value = false
  messages.value = data?.items ?? []
  await scrollDown()
}

async function send(): Promise<void> {
  const question = text.value.trim()
  if (!question || sending.value) return
  sending.value = true
  text.value = ''
  const now = new Date().toISOString()
  messages.value = [...messages.value, { id: `u-${now}`, role: 'user', text: question, tools: [], created_at: now }]
  await scrollDown()
  const { data, error } = await api.POST('/api/v1/assistant/chat', { body: { text: question } })
  sending.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  messages.value = [
    ...messages.value,
    { id: `a-${now}`, role: 'assistant', text: data.reply, tools: data.tools, created_at: new Date().toISOString() },
  ]
  await scrollDown()
}

onMounted(load)
</script>

<template>
  <div class="chat" data-testid="assistant-chat">
    <div ref="list" v-loading="loading" class="messages">
      <el-empty
        v-if="!messages.length && !loading"
        :image-size="60"
        description="试试：我的待办有什么？订单 XX 怎么样了？记一下明天给供应商打电话"
      />
      <div v-for="m in messages" :key="m.id" class="row" :class="m.role" :data-testid="`chat-${m.role}`">
        <div class="bubble">
          <div class="text">{{ m.text }}</div>
          <div class="meta">
            <span>{{ formatDateTime(m.created_at) }}</span>
            <el-tag v-for="tool in m.tools" :key="tool" size="small" effect="plain" class="tool">
              {{ TOOL_NAME[tool] ?? tool }}
            </el-tag>
          </div>
        </div>
      </div>
    </div>
    <div class="ask">
      <el-input
        v-model="text"
        maxlength="2000"
        placeholder="问助理：我的待办、某个订单的进度、某位客户的情况，或者让它记一件事"
        data-testid="chat-input"
        @keyup.enter="send"
      />
      <el-button type="primary" :loading="sending" data-testid="chat-send" @click="send">发送</el-button>
    </div>
    <p class="muted">助理只回答你有权限查看的内容；对话会调用大模型并计入用量。</p>
  </div>
</template>

<style scoped>
.chat {
  max-width: 820px;
}

.messages {
  height: 420px;
  overflow-y: auto;
  padding: 12px;
  border: 1px solid var(--el-border-color-light);
  border-radius: 6px;
  background: var(--el-fill-color-lighter);
}

.row {
  display: flex;
  margin-bottom: 10px;
}

.row.user {
  justify-content: flex-end;
}

.bubble {
  max-width: 78%;
  padding: 8px 12px;
  border-radius: 8px;
  background: var(--el-bg-color);
}

.row.user .bubble {
  background: var(--el-color-primary-light-9);
}

.text {
  white-space: pre-wrap;
  word-break: break-word;
}

.meta {
  margin-top: 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.tool {
  margin-left: 6px;
}

.ask {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
