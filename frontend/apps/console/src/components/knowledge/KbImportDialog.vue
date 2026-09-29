<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, ref, watch } from 'vue'

import { FAQ_CSV_TEMPLATE } from '../../ai'
import { api } from '../../api'
import { useAuthStore } from '../../stores/auth'

/** 从 CSV 批量导入问答（冷启动）。文件在浏览器里读取为文本后提交。 */
const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean]; imported: [] }>()

const MAX_BYTES = 2_000_000
const auth = useAuthStore()
const csv = ref('')
const fileName = ref('')
const publish = ref(false)
const importing = ref(false)
const result = ref<Schemas['KbImportResult'] | null>(null)

const open = computed({
  get: () => props.modelValue,
  set: (value) => emit('update:modelValue', value),
})

watch(open, (visible) => {
  if (visible) {
    csv.value = ''
    fileName.value = ''
    result.value = null
    publish.value = false
  }
})

async function pick(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  if (file.size > MAX_BYTES) {
    ElMessage.error('文件不能超过 2 MB，请分批导入')
    return
  }
  csv.value = await file.text()
  fileName.value = file.name
}

function downloadTemplate(): void {
  // 带 BOM，Excel 打开时按 UTF-8 识别中文。
  const blob = new Blob([`﻿${FAQ_CSV_TEMPLATE}\n`], { type: 'text/csv;charset=utf-8' })
  const link = document.createElement('a')
  link.href = URL.createObjectURL(blob)
  link.download = '问答导入模板.csv'
  link.click()
  URL.revokeObjectURL(link.href)
}

async function submit(): Promise<void> {
  if (!csv.value.trim()) {
    ElMessage.warning('请选择 CSV 文件或粘贴内容')
    return
  }
  importing.value = true
  const { data, error } = await api.POST('/api/v1/kb/import', {
    body: { csv: csv.value, publish: publish.value },
  })
  importing.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  result.value = data
  if (data.created) {
    ElMessage.success(`已导入 ${data.created} 条`)
    emit('imported')
  }
}
</script>

<template>
  <el-dialog v-model="open" title="批量导入问答" width="600px" data-testid="kb-import">
    <p class="hint">
      CSV 文件首行为表头：<b>标准问</b>、<b>答案</b>必填，<b>相似问</b>用竖线 |
      分隔，<b>分类</b>可选。
      <el-button link type="primary" @click="downloadTemplate">下载模板</el-button>
    </p>
    <div class="file">
      <label class="el-button el-button--small">
        选择文件
        <input
          type="file"
          accept=".csv,text/csv"
          hidden
          data-testid="kb-import-file"
          @change="pick"
        />
      </label>
      <span class="muted">{{ fileName || '也可以直接粘贴到下面' }}</span>
    </div>
    <el-input
      v-model="csv"
      type="textarea"
      :rows="8"
      :placeholder="FAQ_CSV_TEMPLATE"
      data-testid="kb-import-text"
    />
    <el-checkbox v-if="auth.can('kb:publish')" v-model="publish" class="publish">
      导入后立即发布（AI 和坐席马上可以用）
    </el-checkbox>
    <el-alert
      v-if="result"
      :type="result.errors.length ? 'warning' : 'success'"
      :closable="false"
      class="result"
      data-testid="kb-import-result"
    >
      <template #title>
        导入 {{ result.created }} 条<template v-if="result.errors.length"
          >， {{ result.errors.length }} 行未导入</template
        >
      </template>
      <ul v-if="result.errors.length" class="errors">
        <li v-for="e in result.errors.slice(0, 20)" :key="e">{{ e }}</li>
      </ul>
    </el-alert>
    <template #footer>
      <el-button @click="open = false">关闭</el-button>
      <el-button type="primary" :loading="importing" data-testid="kb-import-submit" @click="submit">
        导入
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.hint {
  margin: 0 0 12px;
  font-size: 13px;
  color: var(--el-text-color-regular);
}

.file {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.publish {
  margin-top: 8px;
}

.result {
  margin-top: 12px;
}

.errors {
  margin: 4px 0 0;
  padding-left: 18px;
}
</style>
