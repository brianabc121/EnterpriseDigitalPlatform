<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import { api } from '../../api'
import { categoryOptions, categoryPath, categoryTree, type Category } from '../../contracts'
import { fileToBase64 } from '../../knowledge'

/**
 * 上传合同模板（§34.2）：Word、PDF、Markdown、纯文本，最大 10 MB。服务端解析成正文，"甲方：________"、
 * "（      ）"这样的空白换成填写项；原件保留，可以下载。上传后打开模板，核对正文和填写项。
 */
const open = defineModel<boolean>({ required: true })
const props = defineProps<{ categories: Category[]; categoryId: string | null }>()
const emit = defineEmits<{ uploaded: [id: string] }>()

const MAX_BYTES = 10 * 1024 * 1024
const ACCEPT = '.docx,.pdf,.md,.markdown,.txt'

const form = reactive({ name: '', category: [] as string[], description: '' })
const file = ref<File | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)
const uploading = ref(false)
const options = computed(() => categoryOptions(categoryTree(props.categories)))

watch(open, (value) => {
  if (!value) return
  form.name = ''
  form.description = ''
  form.category = categoryPath(props.categories, props.categoryId)
  file.value = null
})

function pick(event: Event): void {
  const input = event.target as HTMLInputElement
  const chosen = input.files?.[0] ?? null
  input.value = ''
  if (!chosen) return
  if (chosen.size > MAX_BYTES) {
    ElMessage.warning('模板文件最大 10 MB')
    return
  }
  file.value = chosen
  if (!form.name) form.name = chosen.name.replace(/\.[^.]+$/, '').slice(0, 128)
}

async function submit(): Promise<void> {
  if (!file.value) {
    ElMessage.warning('先选择模板文件')
    return
  }
  uploading.value = true
  const { data, error } = await api.POST('/api/v1/contracts/templates/upload', {
    body: {
      filename: file.value.name,
      content_base64: await fileToBase64(file.value),
      name: form.name.trim() || null,
      category_id: form.category.at(-1) ?? null,
      description: form.description.trim() || null,
    },
  })
  uploading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  const detected = data.detected.length
  ElMessage.success(detected ? `已上传，把 ${detected} 处空白识别成了填写项，请核对` : '已上传，请核对正文和填写项')
  open.value = false
  emit('uploaded', data.template.id)
}
</script>

<template>
  <el-dialog v-model="open" title="上传合同模板" width="520px" append-to-body data-testid="template-upload-dialog">
    <el-form label-width="72px" :disabled="uploading">
      <el-form-item label="文件" required>
        <input
          ref="fileInput"
          type="file"
          :accept="ACCEPT"
          class="file-input"
          data-testid="template-upload-input"
          @change="pick"
        />
        <el-button size="small" @click="fileInput?.click()">选择文件</el-button>
        <span v-if="file" class="file-name" data-testid="template-upload-name">{{ file.name }}</span>
        <div class="hint">
          Word（.docx）、PDF、Markdown、纯文本，最大 10 MB。"甲方：________"、"（      ）"、"【    】"这样的空白会换成
          填写项，名称取空白前面的文字。
        </div>
      </el-form-item>
      <el-form-item label="名称">
        <el-input v-model="form.name" maxlength="128" placeholder="不填时用文件名" data-testid="template-upload-title" />
      </el-form-item>
      <el-form-item label="分类">
        <el-cascader
          v-model="form.category"
          :options="options"
          :props="{ checkStrictly: true }"
          clearable
          placeholder="未分类"
          class="wide"
        />
      </el-form-item>
      <el-form-item label="说明">
        <el-input v-model="form.description" type="textarea" :rows="2" maxlength="1000" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button :disabled="uploading" @click="open = false">取消</el-button>
      <el-button type="primary" :loading="uploading" data-testid="template-upload-submit" @click="submit">上传</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.file-input {
  display: none;
}

.file-name {
  margin-left: 8px;
  font-size: 13px;
}

.hint {
  width: 100%;
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--el-text-color-secondary);
}

.wide {
  width: 100%;
}
</style>
