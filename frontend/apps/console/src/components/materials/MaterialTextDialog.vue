<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { MarkdownView } from '@edp/ui'
import { ElMessage } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import { api } from '../../api'
import { folderOptions, folderPath, folderTree, type Material, type MaterialFolder } from '../../materials'

/**
 * 写文字资料（§36.2）：名称、文件夹、标签、说明和正文（简单 Markdown，右边是排好版的预览）；保存为 OSS 上
 * 的 .md 文件。editing 不为空时只修改这份文字资料的正文。
 */
const props = defineProps<{
  modelValue: boolean
  folders: MaterialFolder[]
  /** 新建时默认放进的文件夹。 */
  folderId: string | null
  editing: { id: string; name: string; body: string } | null
  maxChars: number
  tagOptions: string[]
}>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean]; saved: [material: Material, body: string] }>()

const form = reactive({ name: '', folder: [] as string[], tags: [] as string[], description: '', body: '' })
const saving = ref(false)
const open = computed({
  get: () => props.modelValue,
  set: (value) => emit('update:modelValue', value),
})
const options = computed(() => folderOptions(folderTree(props.folders)))

watch(open, (visible) => {
  if (!visible) return
  Object.assign(form, {
    name: props.editing?.name ?? '',
    folder: folderPath(props.folders, props.folderId),
    tags: [],
    description: '',
    body: props.editing?.body ?? '',
  })
})

function cleanTags(tags: string[]): string[] {
  const seen: string[] = []
  for (const tag of tags.map((t) => t.trim().slice(0, 32)).filter(Boolean)) {
    if (!seen.includes(tag)) seen.push(tag)
  }
  return seen.slice(0, 10)
}

async function save(): Promise<void> {
  if (!props.editing && !form.name.trim()) {
    ElMessage.warning('请填写名称')
    return
  }
  if (!form.body.trim()) {
    ElMessage.warning('请填写正文')
    return
  }
  saving.value = true
  const { data, error } = props.editing
    ? await api.PUT('/api/v1/materials/{material_id}/text', {
        params: { path: { material_id: props.editing.id } },
        body: { body: form.body },
      })
    : await api.POST('/api/v1/materials/texts', {
        body: {
          name: form.name.trim(),
          body: form.body,
          folder_id: form.folder.at(-1) ?? null,
          tags: cleanTags(form.tags),
          description: form.description.trim() || null,
        },
      })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success(props.editing ? '已保存正文' : '已保存文字资料')
  emit('saved', data, form.body)
  open.value = false
}
</script>

<template>
  <el-dialog
    v-model="open"
    :title="editing ? `修改正文：${editing.name}` : '写文字资料'"
    width="960px"
    top="6vh"
    append-to-body
    :close-on-click-modal="false"
    :data-testid="editing ? 'material-text-edit-dialog' : 'material-text-dialog'"
  >
    <el-form v-if="!editing" label-width="64px" class="meta">
      <el-form-item label="名称" required>
        <el-input v-model="form.name" maxlength="200" placeholder="例如：门锁安装说明" data-testid="material-text-name" />
      </el-form-item>
      <el-form-item label="文件夹">
        <div data-testid="material-text-folder" class="wide">
          <el-cascader
            v-model="form.folder"
            :options="options"
            :props="{ checkStrictly: true }"
            clearable
            placeholder="不放进文件夹"
            class="wide"
          />
        </div>
      </el-form-item>
      <el-form-item label="标签">
        <el-select
          v-model="form.tags"
          multiple
          filterable
          allow-create
          default-first-option
          :multiple-limit="10"
          placeholder="可以输入新标签"
          class="wide"
          data-testid="material-text-tags"
        >
          <el-option v-for="tag in tagOptions" :key="tag" :label="tag" :value="tag" />
        </el-select>
      </el-form-item>
      <el-form-item label="说明">
        <el-input v-model="form.description" maxlength="2000" placeholder="选填" />
      </el-form-item>
    </el-form>
    <div class="editor">
      <div class="pane">
        <div class="pane-head">
          正文
          <span class="muted">支持 # 标题、- 列表、**加粗**、| 表格 |；{{ form.body.length }} / {{ maxChars }} 字</span>
        </div>
        <el-input
          v-model="form.body"
          type="textarea"
          :rows="18"
          :maxlength="maxChars"
          resize="none"
          placeholder="# 标题&#10;&#10;正文……"
          data-testid="material-text-body"
        />
      </div>
      <div class="pane">
        <div class="pane-head">预览</div>
        <div class="preview" data-testid="material-text-preview">
          <MarkdownView v-if="form.body.trim()" :source="form.body" />
          <p v-else class="muted">写好的正文在这里排版显示。</p>
        </div>
      </div>
    </div>
    <template #footer>
      <el-button @click="open = false">取消</el-button>
      <el-button type="primary" :loading="saving" data-testid="material-text-save" @click="save">保存</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.meta {
  display: grid;
  grid-template-columns: 1fr 1fr;
  column-gap: 16px;
}

.wide,
.wide :deep(.el-cascader) {
  width: 100%;
}

.editor {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

.pane-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 6px;
  font-size: 13px;
  font-weight: 600;
}

.preview {
  height: 398px;
  overflow: auto;
  padding: 8px 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
  background: var(--el-fill-color-blank);
}

.muted {
  font-size: 12px;
  font-weight: normal;
  color: var(--el-text-color-secondary);
}

@media (max-width: 900px) {
  .meta,
  .editor {
    grid-template-columns: 1fr;
  }
}
</style>
