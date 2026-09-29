<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { ref } from 'vue'

import { api } from '../../api'

/** 把重复的客户档案并入目标客户。 */
const open = defineModel<boolean>({ required: true })
const props = defineProps<{ target: Schemas['CustomerOut'] | null }>()
const emit = defineEmits<{ done: [] }>()

const q = ref('')
const candidates = ref<Schemas['CustomerOut'][]>([])
const sources = ref<string[]>([])
const saving = ref(false)

async function search(): Promise<void> {
  const { data } = await api.GET('/api/v1/customers', {
    params: { query: { q: q.value.trim() || undefined, limit: 20 } },
  })
  candidates.value = (data?.items ?? []).filter((c) => c.id !== props.target?.id)
}

function reset(): void {
  q.value = props.target?.display_name ?? ''
  sources.value = []
  void search()
}

async function merge(): Promise<void> {
  if (!props.target || sources.value.length === 0) {
    ElMessage.warning('请选择要并入的客户')
    return
  }
  saving.value = true
  const { data, error } = await api.POST('/api/v1/customers/{customer_id}/merge', {
    params: { path: { customer_id: props.target.id } },
    body: { source_ids: sources.value },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success(`已合并 ${sources.value.length} 个客户`)
  open.value = false
  emit('done')
}
</script>

<template>
  <el-dialog v-model="open" title="合并客户" width="560px" data-testid="merge-dialog" @open="reset">
    <p class="hint">
      选中的客户并入「{{ target?.display_name }}」：渠道身份、会话、留言和归属记录一并迁移，
      标签合并，然后删除选中的客户。
    </p>
    <el-input
      v-model="q"
      placeholder="按名称、公司，或完整的手机号、邮箱查找"
      clearable
      data-testid="merge-search"
      @keyup.enter="search"
      @clear="search"
    >
      <template #append>
        <el-button @click="search">查找</el-button>
      </template>
    </el-input>
    <el-checkbox-group v-model="sources" class="list">
      <el-checkbox v-for="c in candidates" :key="c.id" :value="c.id">
        {{ c.display_name }}
        <span class="muted">{{ [c.phone, c.email, c.company].filter(Boolean).join(' · ') }}</span>
      </el-checkbox>
      <el-empty v-if="candidates.length === 0" :image-size="40" description="没有找到其他客户" />
    </el-checkbox-group>
    <template #footer>
      <el-button @click="open = false">取消</el-button>
      <el-button type="primary" :loading="saving" data-testid="merge-confirm" @click="merge">
        合并
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.hint {
  margin: 0 0 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.6;
}

.list {
  display: flex;
  flex-direction: column;
  margin-top: 12px;
  max-height: 280px;
  overflow: auto;
}

.muted {
  margin-left: 6px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
