<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { onBeforeUnmount, onMounted, ref } from 'vue'

import { api, formatDateTime } from '../../api'
import { VERSION_CHANGE } from '../../knowledge'

/** 知识动态（设计 §12.6）：待确认的必读知识和最近发布、更新的知识。每分钟刷新。 */
const emit = defineEmits<{ unread: [count: number] }>()

const REFRESH_MS = 60_000
const feed = ref<Schemas['KbFeed'] | null>(null)
const viewing = ref<Schemas['KbItemOut'] | null>(null)
let timer: ReturnType<typeof setInterval> | null = null

async function load(): Promise<void> {
  const { data } = await api.GET('/api/v1/kb/feed', { params: { query: { limit: 20 } } })
  if (!data) return
  feed.value = data
  emit('unread', data.must_read.length)
}

async function view(itemId: string): Promise<void> {
  const { data, error } = await api.GET('/api/v1/kb/items/{item_id}', {
    params: { path: { item_id: itemId } },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  viewing.value = data
}

async function confirm(itemId: string): Promise<void> {
  const { error } = await api.POST('/api/v1/kb/items/{item_id}/read', {
    params: { path: { item_id: itemId } },
  })
  if (error) {
    ElMessage.error(errorMessage(error))
    return
  }
  viewing.value = null
  await load()
}

onMounted(() => {
  void load()
  timer = setInterval(() => void load(), REFRESH_MS)
})
onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
})
</script>

<template>
  <div class="feed" data-testid="kb-feed">
    <template v-if="feed">
      <section v-if="feed.must_read.length" class="must-read">
        <h4>必读 · 请确认</h4>
        <div
          v-for="e in feed.must_read"
          :key="e.item_id"
          class="event unread"
          data-testid="must-read-item"
        >
          <a class="title" @click="view(e.item_id)">{{ e.title }}</a>
          <span class="muted">
            v{{ e.version }} {{ VERSION_CHANGE[e.change] ?? '' }} ·
            {{ formatDateTime(e.created_at) }}
          </span>
          <el-button
            size="small"
            type="primary"
            link
            data-testid="confirm-read"
            @click="confirm(e.item_id)"
          >
            确认已读
          </el-button>
        </div>
      </section>
      <h4>最近更新</h4>
      <div v-for="e in feed.events" :key="`${e.item_id}:${e.version}`" class="event">
        <a class="title" @click="view(e.item_id)">{{ e.title }}</a>
        <el-tag v-if="e.must_read" size="small" type="warning">必读</el-tag>
        <span class="muted">
          {{ e.version === 1 ? '新增' : `v${e.version} ${VERSION_CHANGE[e.change] ?? ''}` }} ·
          {{ formatDateTime(e.created_at) }}
        </span>
      </div>
      <el-empty v-if="!feed.events.length" :image-size="48" description="暂无知识动态" />
    </template>

    <el-dialog
      :model-value="viewing !== null"
      :title="viewing?.title"
      width="520px"
      append-to-body
      @update:model-value="(v: boolean) => !v && (viewing = null)"
    >
      <p class="content">{{ viewing?.content }}</p>
      <template v-if="viewing?.must_read" #footer>
        <el-button type="primary" @click="viewing && confirm(viewing.id)">确认已读</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
h4 {
  margin: 4px 0 8px;
  font-size: 13px;
}

.must-read {
  margin-bottom: 12px;
  padding: 8px;
  border-radius: 6px;
  background: var(--el-color-warning-light-9);
}

.event {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 8px;
  padding: 6px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
  font-size: 13px;
}

.event:last-child {
  border-bottom: none;
}

.title {
  cursor: pointer;
  color: var(--el-text-color-primary);
}

.title:hover {
  color: var(--el-color-primary);
}

.unread .title {
  font-weight: 600;
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.content {
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
