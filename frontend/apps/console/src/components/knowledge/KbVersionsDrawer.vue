<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, ref, watch } from 'vue'

import { api, formatDateTime } from '../../api'
import { VERSION_CHANGE } from '../../knowledge'
import { useAuthStore } from '../../stores/auth'

/** 历次发布的内容（设计 §12.5）：可以把任意历史版本恢复为新版本。 */
const props = defineProps<{ item: Schemas['KbItemOut'] | null }>()
const emit = defineEmits<{ close: []; restored: [item: Schemas['KbItemOut']] }>()

const auth = useAuthStore()
const versions = ref<Schemas['KbVersionOut'][]>([])
const loading = ref(false)
const expanded = ref<number | null>(null)

const open = computed({
  get: () => props.item !== null,
  set: (value) => {
    if (!value) emit('close')
  },
})

watch(
  () => props.item?.id,
  async (id) => {
    versions.value = []
    expanded.value = null
    if (!id) return
    loading.value = true
    const { data, error } = await api.GET('/api/v1/kb/items/{item_id}/versions', {
      params: { path: { item_id: id } },
    })
    loading.value = false
    if (!data) {
      ElMessage.error(errorMessage(error))
      return
    }
    versions.value = data.items
  },
)

async function restore(version: Schemas['KbVersionOut']): Promise<void> {
  if (!props.item) return
  try {
    await ElMessageBox.confirm(
      `把 v${version.version} 的内容作为新版本发布？AI 与坐席立即使用恢复后的内容。`,
      '恢复历史版本',
      { confirmButtonText: '恢复', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  const { data, error } = await api.POST('/api/v1/kb/items/{item_id}/versions/{version}/restore', {
    params: { path: { item_id: props.item.id, version: version.version } },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success(`已恢复为 v${version.version} 的内容（新版本 v${data.version}）`)
  emit('restored', data)
}
</script>

<template>
  <el-drawer v-model="open" :title="`版本历史 · ${item?.title ?? ''}`" size="560px" append-to-body>
    <div v-loading="loading" data-testid="kb-versions">
      <el-empty v-if="!loading && versions.length === 0" description="还没有发布过" />
      <el-timeline>
        <el-timeline-item
          v-for="v in versions"
          :key="v.version"
          :timestamp="formatDateTime(v.created_at)"
          :type="v.version === item?.version ? 'primary' : undefined"
        >
          <div class="head">
            <b>v{{ v.version }}</b>
            <el-tag size="small" type="info">{{ VERSION_CHANGE[v.change] ?? v.change }}</el-tag>
            <span class="muted">{{ v.published_by_name ?? '系统' }}</span>
            <span v-if="v.note" class="muted">{{ v.note }}</span>
          </div>
          <p class="content" :class="{ clamp: expanded !== v.version }">{{ v.content }}</p>
          <div class="actions">
            <el-button
              link
              size="small"
              @click="expanded = expanded === v.version ? null : v.version"
            >
              {{ expanded === v.version ? '收起' : '展开' }}
            </el-button>
            <el-button
              v-if="auth.can('kb:publish') && v.version !== item?.version"
              link
              type="primary"
              size="small"
              data-testid="restore-version"
              @click="restore(v)"
            >
              恢复此版本
            </el-button>
          </div>
        </el-timeline-item>
      </el-timeline>
    </div>
  </el-drawer>
</template>

<style scoped>
.head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.content {
  margin: 6px 0 0;
  font-size: 13px;
  white-space: pre-wrap;
  word-break: break-word;
}

.clamp {
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
</style>
