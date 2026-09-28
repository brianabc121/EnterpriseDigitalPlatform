<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, reactive, ref } from 'vue'

import { api } from '../../api'
import { useAuthStore } from '../../stores/auth'

const emit = defineEmits<{ pick: [text: string] }>()

const auth = useAuthStore()
const visible = ref(false)
const items = ref<Schemas['QuickReplyOut'][]>([])
const keyword = ref('')
const loaded = ref(false)
const adding = ref(false)
const form = reactive({ title: '', content: '', shared: false })

const canShare = computed(() => auth.can('quick_reply:manage'))
const filtered = computed(() => {
  const k = keyword.value.trim()
  return k ? items.value.filter((r) => r.title.includes(k) || r.content.includes(k)) : items.value
})

async function load(): Promise<void> {
  const { data, error } = await api.GET('/api/v1/quick-replies')
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  items.value = data.items
  loaded.value = true
}

function onShow(): void {
  if (!loaded.value) void load()
}

function pick(reply: Schemas['QuickReplyOut']): void {
  emit('pick', reply.content)
  visible.value = false
}

async function save(): Promise<void> {
  if (!form.title.trim() || !form.content.trim()) {
    ElMessage.warning('请填写标题和内容')
    return
  }
  const { data, error } = await api.POST('/api/v1/quick-replies', {
    body: {
      title: form.title.trim(),
      content: form.content.trim(),
      shared: form.shared,
      category: '',
      sort: 0,
    },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  Object.assign(form, { title: '', content: '', shared: false })
  adding.value = false
  await load()
}

async function remove(reply: Schemas['QuickReplyOut']): Promise<void> {
  const { error } = await api.DELETE('/api/v1/quick-replies/{reply_id}', {
    params: { path: { reply_id: reply.id } },
  })
  if (error) {
    ElMessage.error(errorMessage(error))
    return
  }
  await load()
}
</script>

<template>
  <el-popover
    v-model:visible="visible"
    placement="top-start"
    :width="360"
    trigger="click"
    @show="onShow"
  >
    <template #reference>
      <el-button size="small" data-testid="quick-replies-button">快捷话术</el-button>
    </template>
    <div class="quick-replies">
      <el-input v-model="keyword" size="small" placeholder="搜索话术" clearable />
      <div class="items">
        <div
          v-for="r in filtered"
          :key="r.id"
          class="item"
          data-testid="quick-reply-item"
          @click="pick(r)"
        >
          <div class="title">
            {{ r.title }}
            <el-tag v-if="r.shared" size="small" type="info">共享</el-tag>
            <el-button
              v-if="!r.shared || canShare"
              link
              size="small"
              class="remove"
              @click.stop="remove(r)"
            >
              删除
            </el-button>
          </div>
          <div class="content">{{ r.content }}</div>
        </div>
        <el-empty v-if="loaded && filtered.length === 0" :image-size="40" description="没有话术" />
      </div>
      <div v-if="adding" class="form">
        <el-input v-model="form.title" size="small" placeholder="标题" maxlength="64" />
        <el-input
          v-model="form.content"
          type="textarea"
          :rows="3"
          placeholder="内容"
          maxlength="2000"
        />
        <div class="form-actions">
          <el-checkbox v-if="canShare" v-model="form.shared" size="small">全员共享</el-checkbox>
          <el-button size="small" @click="adding = false">取消</el-button>
          <el-button size="small" type="primary" @click="save">保存</el-button>
        </div>
      </div>
      <el-button v-else link type="primary" size="small" @click="adding = true">新增话术</el-button>
    </div>
  </el-popover>
</template>

<style scoped>
.quick-replies {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.items {
  max-height: 260px;
  overflow-y: auto;
}

.item {
  padding: 6px 4px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  cursor: pointer;
}

.item:hover {
  background: var(--el-fill-color-light);
}

.title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 500;
}

.remove {
  margin-left: auto;
}

.content {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  white-space: pre-wrap;
}

.form {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.form-actions {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 6px;
}
</style>
