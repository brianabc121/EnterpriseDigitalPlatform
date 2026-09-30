<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { api, formatDateTime } from '../../api'
import { dueState, TODO_STATUS, TODO_STATUS_TAG, type Todo } from '../../todos'
import TodoDrawer from './TodoDrawer.vue'

/** 手机工作台的待办：等我确认的和我的未完成待办（点开处理）。 */
defineProps<{ drawerSize?: string }>()

const pending = ref<Todo[]>([])
const mine = ref<Todo[]>([])
const loading = ref(false)
const openId = ref<string | null>(null)

const empty = computed(() => !pending.value.length && !mine.value.length)

async function load(): Promise<void> {
  loading.value = true
  const [p, m] = await Promise.all([
    api.GET('/api/v1/todos', { params: { query: { view: 'pending', limit: 50 } } }),
    api.GET('/api/v1/todos', { params: { query: { view: 'mine', limit: 50 } } }),
  ])
  loading.value = false
  pending.value = p.data?.items ?? []
  mine.value = m.data?.items ?? []
}

defineExpose({ load })
onMounted(load)
</script>

<template>
  <div v-loading="loading" class="my-todos" data-testid="my-todos">
    <template v-if="pending.length">
      <div class="section">待确认 {{ pending.length }}</div>
      <div v-for="t in pending" :key="t.id" class="item" data-testid="my-todo-pending" @click="openId = t.id">
        <div class="line">
          <el-tag size="small" type="warning">{{ t.type_name }}</el-tag>
          <span class="name">{{ t.title }}</span>
        </div>
        <div class="muted">{{ t.customer_name ?? '—' }} · {{ formatDateTime(t.created_at) }}</div>
      </div>
    </template>
    <template v-if="mine.length">
      <div class="section">我的待办 {{ mine.length }}</div>
      <div
        v-for="t in mine"
        :key="t.id"
        class="item"
        :class="{ overdue: dueState(t) === 'overdue' }"
        data-testid="my-todo"
        @click="openId = t.id"
      >
        <div class="line">
          <el-tag size="small" :type="TODO_STATUS_TAG[t.status]">{{ TODO_STATUS[t.status] }}</el-tag>
          <span class="name">{{ t.title }}</span>
        </div>
        <div class="muted">
          {{ t.customer_name ?? '—' }}
          <template v-if="t.due_at"> · 截止 {{ formatDateTime(t.due_at) }}</template>
        </div>
      </div>
    </template>
    <el-empty v-if="empty && !loading" :image-size="60" description="没有待办" />
    <TodoDrawer :todo-id="openId" :size="drawerSize ?? '100%'" @close="openId = null" @changed="load" />
  </div>
</template>

<style scoped>
.section {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  margin: 8px 0 4px;
}

.item {
  border-bottom: 1px solid var(--el-border-color-lighter);
  padding: 8px 4px;
  cursor: pointer;
}

.item.overdue .name {
  color: var(--el-color-danger);
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
  margin-top: 2px;
}
</style>
