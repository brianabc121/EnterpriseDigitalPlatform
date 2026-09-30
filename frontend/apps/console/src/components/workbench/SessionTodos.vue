<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { onMounted, ref, watch } from 'vue'

import { api } from '../../api'
import TodoDrawer from '../todos/TodoDrawer.vue'
import { TODO_SOURCE, todosChanged, type Todo } from '../../todos'

/**
 * 这次会话里 AI 生成、等待确认的待办（设计文档 §24.9）：显示在会话小结下面，可以直接确认或驳回；
 * 需要修改时打开详情。
 */
const props = defineProps<{ sessionId: string }>()

const items = ref<Todo[]>([])
const acting = ref<string | null>(null)
const openId = ref<string | null>(null)

async function load(): Promise<void> {
  const { data } = await api.GET('/api/v1/todos', {
    params: { query: { view: 'pending', session_id: props.sessionId, limit: 20 } },
  })
  items.value = data?.items ?? []
}

async function decide(todo: Todo, confirm: boolean): Promise<void> {
  acting.value = todo.id
  const path = { params: { path: { todo_id: todo.id } } }
  const { data, error } = confirm
    ? await api.POST('/api/v1/todos/{todo_id}/confirm', { ...path, body: {} })
    : await api.POST('/api/v1/todos/{todo_id}/discard', {
        ...path,
        body: { reason: 'not_real', note: null },
      })
  acting.value = null
  if (!data) {
    ElMessage.error(errorMessage(error))
    // 例如缺少必填信息：打开详情，在确认对话框里补全。
    if (confirm) openId.value = todo.id
    return
  }
  ElMessage.success(confirm ? '已确认，进入待办列表' : '已驳回')
  todosChanged()
  await load()
}

watch(() => props.sessionId, load)
onMounted(load)
</script>

<template>
  <div v-if="items.length" class="card" data-testid="session-todos">
    <div class="head">AI 生成的待办 <span class="muted">确认后进入待办列表</span></div>
    <div v-for="t in items" :key="t.id" class="item">
      <div class="text" @click="openId = t.id">
        <el-tag size="small" type="warning">{{ t.type_name }}</el-tag>
        {{ t.title }}
        <span class="muted">（{{ TODO_SOURCE[t.source] }}）</span>
      </div>
      <div class="actions">
        <el-button size="small" :disabled="acting === t.id" @click="decide(t, false)">驳回</el-button>
        <el-button
          size="small"
          type="primary"
          :loading="acting === t.id"
          data-testid="session-todo-confirm"
          @click="decide(t, true)"
          >确认</el-button
        >
      </div>
    </div>
    <TodoDrawer :todo-id="openId" @close="openId = null" @changed="load" />
  </div>
</template>

<style scoped>
.card {
  border: 1px solid var(--el-color-warning-light-5);
  background: var(--el-color-warning-light-9);
  border-radius: 6px;
  padding: 8px 10px;
  margin: 8px 12px;
}

.head {
  font-weight: 600;
  font-size: 13px;
  margin-bottom: 6px;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  font-weight: normal;
}

.item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 4px 0;
}

.text {
  cursor: pointer;
  font-size: 13px;
}

.actions {
  flex-shrink: 0;
}
</style>
