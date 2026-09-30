<script setup lang="ts">
import { type Schemas } from '@edp/api-client'
import { Bell } from '@element-plus/icons-vue'
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { api, formatDateTime } from '../../api'

/** 站内信：知识到期提醒、线索待确认、导入结果、知识周报等。每分钟刷新一次未读数。 */
const POLL_MS = 60_000
const router = useRouter()
const items = ref<Schemas['NotificationOut'][]>([])
const unread = ref(0)
const open = ref(false)
let timer: ReturnType<typeof setInterval> | undefined

async function load(): Promise<void> {
  const { data } = await api.GET('/api/v1/notifications', { params: { query: { limit: 20 } } })
  if (!data) return
  items.value = data.items
  unread.value = data.unread
}

async function read(item: Schemas['NotificationOut']): Promise<void> {
  open.value = false
  if (!item.read_at) {
    await api.POST('/api/v1/notifications/{notification_id}/read', {
      params: { path: { notification_id: item.id } },
    })
    await load()
  }
  if (item.link) await router.push(item.link)
}

async function readAll(): Promise<void> {
  await api.POST('/api/v1/notifications/read-all')
  await load()
}

onMounted(() => {
  void load()
  timer = setInterval(() => {
    if (document.visibilityState === 'visible') void load()
  }, POLL_MS)
})
onBeforeUnmount(() => clearInterval(timer))
</script>

<template>
  <el-popover v-model:visible="open" placement="bottom-end" :width="360" trigger="click" @show="load">
    <template #reference>
      <span class="bell" data-testid="notification-bell">
        <el-badge :value="unread" :hidden="!unread" :max="99">
          <el-icon :size="18"><Bell /></el-icon>
        </el-badge>
      </span>
    </template>
    <div class="head">
      <span>站内信</span>
      <el-button v-if="unread" link type="primary" size="small" @click="readAll">全部已读</el-button>
    </div>
    <el-scrollbar max-height="360px">
      <div
        v-for="item in items"
        :key="item.id"
        class="item"
        :class="{ unread: !item.read_at }"
        data-testid="notification"
        @click="read(item)"
      >
        <div class="title">{{ item.title }}</div>
        <div v-if="item.body" class="body">{{ item.body }}</div>
        <div class="time">{{ formatDateTime(item.created_at) }}</div>
      </div>
      <el-empty v-if="!items.length" :image-size="48" description="暂无消息" />
    </el-scrollbar>
  </el-popover>
</template>

<style scoped>
.bell {
  display: inline-flex;
  align-items: center;
  margin-right: 16px;
  cursor: pointer;
  color: var(--el-text-color-regular);
}

.head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
  font-weight: 600;
}

.item {
  padding: 8px 4px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  cursor: pointer;
}

.item:hover {
  background: var(--el-fill-color-light);
}

.item.unread .title::before {
  content: '';
  display: inline-block;
  width: 6px;
  height: 6px;
  margin-right: 6px;
  border-radius: 50%;
  background: var(--el-color-danger);
  vertical-align: middle;
}

.title {
  font-size: 13px;
}

.body {
  margin-top: 2px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.time {
  margin-top: 2px;
  font-size: 11px;
  color: var(--el-text-color-placeholder);
}
</style>
