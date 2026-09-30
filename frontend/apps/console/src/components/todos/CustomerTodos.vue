<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref, watch } from 'vue'

import { api, formatDateTime } from '../../api'
import { useAuthStore } from '../../stores/auth'
import { dueState, TODO_STATUS, TODO_STATUS_TAG, type Todo } from '../../todos'
import TodoDrawer from './TodoDrawer.vue'
import TodoFormDialog from './TodoFormDialog.vue'

/**
 * 当前客户的待办（工作台右栏、企业微信侧边栏、手机工作台）：未完成的和最近完成的，
 * 可以快速新建（员工新建的直接进入待办列表），或选中几条消息让 AI 预填。
 */
const props = defineProps<{
  customerId: string
  customerName?: string | null
  sessionId?: string | null
  source?: 'staff' | 'copilot' | 'sidebar'
}>()

const auth = useAuthStore()
const canCreate = computed(() => auth.can('todo:handle') || auth.can('todo:assign'))
const items = ref<Todo[]>([])
const loading = ref(false)
const openId = ref<string | null>(null)
const creating = ref(false)
const prefill = ref<Schemas['TodoSuggestion'] | null>(null)
const picking = ref(false)
const messages = ref<Schemas['MessageOut'][]>([])
const picked = ref<string[]>([])
const extracting = ref(false)
const suggestions = ref<Schemas['TodoSuggestion'][]>([])

const unfinished = computed(() =>
  items.value.filter((t) => ['pending', 'open', 'in_progress', 'waiting'].includes(t.status)),
)
const finished = computed(() => items.value.filter((t) => t.status === 'done').slice(0, 3))

async function load(): Promise<void> {
  loading.value = true
  const query = { customer_id: props.customerId, limit: 50 }
  const [pending, rest] = await Promise.all([
    api.GET('/api/v1/todos', { params: { query: { ...query, view: 'pending' } } }),
    api.GET('/api/v1/todos', { params: { query: { ...query, view: 'all' } } }),
  ])
  loading.value = false
  items.value = [...(pending.data?.items ?? []), ...(rest.data?.items ?? [])]
}

function create(suggestion: Schemas['TodoSuggestion'] | null = null): void {
  prefill.value = suggestion
  creating.value = true
}

async function pick(): Promise<void> {
  if (!props.sessionId) return
  const { data, error } = await api.GET('/api/v1/sessions/{session_id}/messages', {
    params: { path: { session_id: props.sessionId }, query: { limit: 50 } },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  // 接口按时间倒序返回：取最近的 20 条文字消息，按时间顺序显示。
  messages.value = [...data.items]
    .reverse()
    .filter((m) => m.text_plain && m.sender_type !== 'system')
    .slice(-20)
  picked.value = messages.value.filter((m) => m.sender_type === 'customer').slice(-3).map((m) => m.id)
  suggestions.value = []
  picking.value = true
}

async function extract(): Promise<void> {
  if (!props.sessionId || !picked.value.length) {
    ElMessage.warning('请选择消息')
    return
  }
  extracting.value = true
  const { data, error } = await api.POST('/api/v1/todos/extract', {
    body: { session_id: props.sessionId, message_ids: picked.value },
  })
  extracting.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  if (!data.items.length) {
    ElMessage.info('没有识别出需要跟进的事，可以手工新建')
    return
  }
  if (data.items.length === 1) {
    picking.value = false
    create(data.items[0])
    return
  }
  suggestions.value = data.items
}

watch(() => props.customerId, load)
onMounted(load)
</script>

<template>
  <div v-loading="loading" class="customer-todos" data-testid="customer-todos">
    <div v-if="canCreate" class="toolbar">
      <el-button size="small" type="primary" data-testid="customer-todo-new" @click="create()">新建待办</el-button>
      <el-button v-if="sessionId" size="small" data-testid="customer-todo-pick" @click="pick">从消息预填</el-button>
    </div>
    <div
      v-for="t in unfinished"
      :key="t.id"
      class="item"
      :class="{ overdue: dueState(t) === 'overdue' }"
      data-testid="customer-todo"
      @click="openId = t.id"
    >
      <div class="line">
        <el-tag size="small" :type="TODO_STATUS_TAG[t.status]">{{ TODO_STATUS[t.status] }}</el-tag>
        <span class="name">{{ t.title }}</span>
      </div>
      <div class="muted">
        {{ t.type_name }} · {{ t.assignee_name ?? '待认领' }}
        <template v-if="t.due_at"> · 截止 {{ formatDateTime(t.due_at) }}</template>
      </div>
    </div>
    <template v-if="finished.length">
      <div class="muted section">最近完成</div>
      <div v-for="t in finished" :key="t.id" class="item done" @click="openId = t.id">
        <div class="line">{{ t.title }}</div>
        <div class="muted">{{ t.result }}</div>
      </div>
    </template>
    <el-empty v-if="!items.length && !loading" :image-size="40" description="这位客户没有待办" />

    <el-dialog v-model="picking" title="选择消息，AI 预填待办" width="520px" append-to-body>
      <el-checkbox-group v-model="picked" class="messages">
        <el-checkbox v-for="m in messages" :key="m.id" :value="m.id" data-testid="pick-message">
          <span class="muted">{{ m.sender_type === 'customer' ? '客户' : '客服' }}：</span>{{ m.text_plain }}
        </el-checkbox>
      </el-checkbox-group>
      <div v-if="suggestions.length" class="suggestions">
        <div class="muted">识别出多件事，选择一件：</div>
        <el-button v-for="(s, i) in suggestions" :key="i" size="small" @click="picking = false; create(s)">
          {{ s.type_name }}：{{ s.title }}
        </el-button>
      </div>
      <template #footer>
        <el-button @click="picking = false">取消</el-button>
        <el-button type="primary" :loading="extracting" data-testid="pick-extract" @click="extract"
          >AI 预填</el-button
        >
      </template>
    </el-dialog>

    <TodoFormDialog
      v-model="creating"
      :customer-id="customerId"
      :customer-name="customerName"
      :session-id="sessionId"
      :source="source ?? 'copilot'"
      :prefill="prefill"
      @created="load"
    />
    <TodoDrawer :todo-id="openId" @close="openId = null" @changed="load" />
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  gap: 8px;
  margin-bottom: 8px;
}

.item {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
  padding: 6px 8px;
  margin-bottom: 6px;
  cursor: pointer;
  font-size: 13px;
}

.item.overdue {
  border-color: var(--el-color-danger-light-5);
}

.item.done {
  opacity: 0.8;
}

.line {
  display: flex;
  align-items: center;
  gap: 6px;
}

.name {
  font-weight: 500;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.section {
  margin: 8px 0 4px;
}

.messages {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
  max-height: 320px;
  overflow-y: auto;
}

.messages :deep(.el-checkbox__label) {
  white-space: normal;
}

.suggestions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 12px;
}
</style>
