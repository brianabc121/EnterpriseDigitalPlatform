<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import { KB_KIND, KB_SOURCE, KB_STATUS, KB_STATUS_TAG, KB_VISIBILITY } from '../../ai'
import { api, formatDateTime } from '../../api'
import { useAuthStore } from '../../stores/auth'

type Item = Schemas['KbItemOut']
type Kind = 'faq' | 'doc'

/** 知识条目的查看与编辑。item 为空时新建（kind 决定问答还是文档）。 */
const props = defineProps<{ modelValue: boolean; item: Item | null; kind: Kind }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean]; saved: [item: Item] }>()

const auth = useAuthStore()
const canManage = computed(() => auth.can('kb:manage'))
const canPublish = computed(() => auth.can('kb:publish'))
const saving = ref(false)

const form = reactive({
  title: '',
  content: '',
  questions: [] as string[],
  category: '',
  tags: [] as string[],
  visibility: 'public' as Schemas['KbItemCreate']['visibility'],
  validity: null as [string, string] | null,
})

const open = computed({
  get: () => props.modelValue,
  set: (value) => emit('update:modelValue', value),
})
const kind = computed<Kind>(() => (props.item?.kind as Kind | undefined) ?? props.kind)
const isFaq = computed(() => kind.value === 'faq')
const title = computed(() => {
  const noun = KB_KIND[kind.value]
  if (!props.item) return `新建${noun}`
  return canManage.value ? `编辑${noun}` : noun
})

watch(
  () => [props.modelValue, props.item] as const,
  ([visible, item]) => {
    if (!visible) return
    form.title = item?.title ?? ''
    form.content = item?.content ?? ''
    form.questions = [...(item?.questions ?? [])]
    form.category = item?.category ?? ''
    form.tags = [...(item?.tags ?? [])]
    form.visibility = (item?.visibility ?? 'public') as typeof form.visibility
    form.validity = item?.valid_from && item.valid_to ? [item.valid_from, item.valid_to] : null
  },
  { immediate: true },
)

function body(): Schemas['KbItemUpdate'] {
  return {
    title: form.title.trim(),
    content: form.content.trim(),
    questions: isFaq.value ? form.questions.map((q) => q.trim()).filter(Boolean) : [],
    category: form.category.trim(),
    tags: form.tags,
    visibility: form.visibility,
    valid_from: form.validity?.[0] ?? null,
    valid_to: form.validity?.[1] ?? null,
  }
}

async function save(publish: boolean): Promise<void> {
  if (!form.title.trim() || !form.content.trim()) {
    ElMessage.warning(isFaq.value ? '请填写标准问和答案' : '请填写标题和正文')
    return
  }
  saving.value = true
  try {
    let item: Item
    if (!props.item) {
      const { data, error } = await api.POST('/api/v1/kb/items', {
        body: { ...body(), kind: kind.value, publish } as Schemas['KbItemCreate'],
      })
      if (!data) throw new Error(errorMessage(error))
      item = data
    } else {
      const { data, error } = await api.PATCH('/api/v1/kb/items/{item_id}', {
        params: { path: { item_id: props.item.id } },
        body: body(),
      })
      if (!data) throw new Error(errorMessage(error))
      item = data
      if (publish && item.status !== 'published') {
        const published = await api.POST('/api/v1/kb/items/{item_id}/publish', {
          params: { path: { item_id: item.id } },
        })
        if (!published.data) throw new Error(errorMessage(published.error))
        item = published.data
      }
    }
    ElMessage.success(publish || item.status === 'published' ? '已保存并生效' : '已保存为草稿')
    emit('saved', item)
    open.value = false
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <el-drawer v-model="open" :title="title" size="560px" data-testid="kb-editor">
    <el-alert
      v-if="item?.status === 'published' && canManage"
      type="info"
      :closable="false"
      show-icon
      class="hint"
      title="这条知识已发布：修改内容后立即生效，版本号加一。"
    />
    <el-form label-position="top" :disabled="!canManage">
      <el-form-item :label="isFaq ? '标准问' : '标题'" required>
        <el-input v-model="form.title" maxlength="500" data-testid="kb-title" />
      </el-form-item>
      <el-form-item v-if="isFaq" label="相似问法">
        <el-input-tag
          v-model="form.questions"
          :max="50"
          placeholder="输入客户的其他问法，回车添加"
          data-testid="kb-questions"
        />
      </el-form-item>
      <el-form-item :label="isFaq ? '答案' : '正文'" required>
        <el-input
          v-model="form.content"
          type="textarea"
          :rows="isFaq ? 5 : 14"
          maxlength="50000"
          :show-word-limit="!isFaq"
          data-testid="kb-content"
        />
      </el-form-item>
      <div class="row">
        <el-form-item label="分类" class="grow">
          <el-input v-model="form.category" maxlength="64" placeholder="例如：物流、售后" />
        </el-form-item>
        <el-form-item label="可见范围" class="grow">
          <el-select v-model="form.visibility" data-testid="kb-visibility">
            <el-option
              v-for="(label, value) in KB_VISIBILITY"
              :key="value"
              :label="label"
              :value="value"
            />
          </el-select>
        </el-form-item>
      </div>
      <el-form-item label="标签">
        <el-input-tag v-model="form.tags" :max="20" placeholder="回车添加" />
      </el-form-item>
      <el-form-item label="有效期（不填为长期有效）">
        <el-date-picker
          v-model="form.validity"
          type="datetimerange"
          start-placeholder="生效时间"
          end-placeholder="失效时间"
          value-format="YYYY-MM-DDTHH:mm:ssZ"
        />
      </el-form-item>
    </el-form>
    <el-descriptions v-if="item" :column="2" size="small" class="meta">
      <el-descriptions-item label="状态">
        <el-tag size="small" :type="KB_STATUS_TAG[item.status]">
          {{ KB_STATUS[item.status] ?? item.status }}
        </el-tag>
      </el-descriptions-item>
      <el-descriptions-item label="版本">v{{ item.version }}</el-descriptions-item>
      <el-descriptions-item label="来源">{{
        KB_SOURCE[item.source] ?? item.source
      }}</el-descriptions-item>
      <el-descriptions-item label="被引用">{{ item.hits }} 次</el-descriptions-item>
      <el-descriptions-item label="更新时间" :span="2">
        {{ formatDateTime(item.updated_at) }}
      </el-descriptions-item>
    </el-descriptions>
    <template v-if="canManage" #footer>
      <el-button @click="open = false">取消</el-button>
      <el-button
        v-if="item?.status !== 'published'"
        :loading="saving"
        data-testid="kb-save-draft"
        @click="save(false)"
      >
        保存草稿
      </el-button>
      <el-button
        v-if="canPublish || item?.status === 'published'"
        type="primary"
        :loading="saving"
        data-testid="kb-save-publish"
        @click="save(item?.status !== 'published')"
      >
        {{ item?.status === 'published' ? '保存' : '保存并发布' }}
      </el-button>
    </template>
  </el-drawer>
</template>

<style scoped>
.hint {
  margin-bottom: 12px;
}

.row {
  display: flex;
  gap: 12px;
}

.grow {
  flex: 1;
}

.meta {
  margin-top: 8px;
}
</style>
