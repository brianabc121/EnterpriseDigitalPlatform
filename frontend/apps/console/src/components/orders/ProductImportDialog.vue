<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, ref, watch } from 'vue'

import { api } from '../../api'
import { downloadBlob } from '../../download'

/**
 * 用 Excel 批量导入商品（设计文档 §25.2）：下载模板 → 上传 → 逐行校验并预览（有问题的行标出原因，
 * 将跳过）→ 确认后才写入商品库。有代码的按代码更新，没有代码的按"名称 + 型号 + 规格"匹配，
 * 留空的列保持原值。导入结果可以下载。
 */
const open = defineModel<boolean>({ required: true })
const props = defineProps<{ importId?: string | null }>()
const emit = defineEmits<{ imported: [] }>()

type ImportOut = Schemas['ProductImportOut']
const preview = ref<ImportOut | null>(null)
const uploading = ref(false)
const confirming = ref(false)
const onlyProblems = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)

const ACTION: Record<string, [string, 'success' | 'primary' | 'info']> = {
  create: ['新增', 'success'],
  update: ['更新', 'primary'],
  skip: ['跳过', 'info'],
}

const rows = computed(() =>
  (preview.value?.rows ?? []).filter((r) => !onlyProblems.value || r.problems.length),
)
const done = computed(() => preview.value?.status === 'done')

watch(open, async (value) => {
  if (!value) return
  preview.value = null
  onlyProblems.value = false
  if (props.importId) {
    const { data } = await api.GET('/api/v1/products/imports/{import_id}', {
      params: { path: { import_id: props.importId } },
    })
    if (data) preview.value = data
  }
})

async function template(): Promise<void> {
  const { data, error } = await api.GET('/api/v1/products/template', { parseAs: 'blob' })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  downloadBlob(data as Blob, '商品导入模板.xlsx')
}

function base64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result).split(',', 2)[1] ?? '')
    reader.onerror = () => reject(reader.error)
    reader.readAsDataURL(file)
  })
}

async function upload(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  if (file.size > 10 * 1024 * 1024) {
    ElMessage.warning('文件不能超过 10 MB')
    return
  }
  uploading.value = true
  const { data, error } = await api.POST('/api/v1/products/imports', {
    body: { filename: file.name, content_base64: await base64(file) },
  })
  uploading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  preview.value = data
}

async function confirm(): Promise<void> {
  const current = preview.value
  if (!current) return
  confirming.value = true
  const { data, error } = await api.POST('/api/v1/products/imports/{import_id}/confirm', {
    params: { path: { import_id: current.id } },
  })
  confirming.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  preview.value = data
  ElMessage.success(`已导入：新增 ${data.created}，更新 ${data.updated}，跳过 ${data.skipped}`)
  emit('imported')
}

async function discard(): Promise<void> {
  const current = preview.value
  if (current && current.status === 'preview') {
    await api.POST('/api/v1/products/imports/{import_id}/cancel', {
      params: { path: { import_id: current.id } },
    })
  }
  preview.value = null
}

async function result(): Promise<void> {
  const current = preview.value
  if (!current) return
  const { data, error } = await api.GET('/api/v1/products/imports/{import_id}/result', {
    params: { path: { import_id: current.id } },
    parseAs: 'blob',
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  downloadBlob(data as Blob, `导入结果-${current.file_name.replace(/\.[^.]+$/, '')}.xlsx`)
}
</script>

<template>
  <el-dialog v-model="open" title="导入商品" width="860px" append-to-body data-testid="product-import">
    <div v-if="!preview" class="start">
      <ol class="steps">
        <li>
          下载表格模板，按说明填写（只有"名称"必填，价格列只能填数字）。
          <el-button link type="primary" data-testid="product-template" @click="template">下载模板</el-button>
        </li>
        <li>上传填好的表格（.xlsx 或 .csv，最多 5000 行、10 MB）。</li>
        <li>核对预览：有问题的行会标出原因并跳过；确认后才写入商品库。</li>
      </ol>
      <input
        ref="fileInput"
        type="file"
        accept=".xlsx,.csv"
        class="file"
        data-testid="product-import-file"
        @change="upload"
      />
      <el-button type="primary" :loading="uploading" @click="fileInput?.click()">选择文件上传</el-button>
    </div>

    <template v-else>
      <div class="summary" data-testid="product-import-summary">
        <span>{{ preview.file_name }}：共 {{ preview.total }} 行</span>
        <template v-if="done">
          <el-tag type="success">新增 {{ preview.created }}</el-tag>
          <el-tag>更新 {{ preview.updated }}</el-tag>
          <el-tag type="info">跳过 {{ preview.skipped }}</el-tag>
        </template>
        <template v-else>
          <el-tag type="success">将新增 {{ preview.will_create }}</el-tag>
          <el-tag>将更新 {{ preview.will_update }}</el-tag>
          <el-tag :type="preview.invalid ? 'warning' : 'info'">有问题（跳过） {{ preview.invalid }}</el-tag>
        </template>
        <el-checkbox v-model="onlyProblems">只看有问题的行</el-checkbox>
      </div>
      <el-table :data="rows" size="small" max-height="420" data-testid="product-import-rows">
        <el-table-column prop="row" label="行号" width="64" />
        <el-table-column label="名称" min-width="160">
          <template #default="{ row }">{{ row.values.name ?? '' }}</template>
        </el-table-column>
        <el-table-column label="代码" width="120">
          <template #default="{ row }">{{ row.values.code ?? '' }}</template>
        </el-table-column>
        <el-table-column label="建议零售价" width="100" align="right">
          <template #default="{ row }">{{ row.values.retail_price ?? '' }}</template>
        </el-table-column>
        <el-table-column :label="done ? '结果' : '操作'" width="80">
          <template #default="{ row }">
            <el-tag size="small" :type="ACTION[row.result ?? row.action]?.[1] ?? 'info'">{{
              ACTION[row.result ?? row.action]?.[0] ?? row.action
            }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="问题" min-width="220">
          <template #default="{ row }">
            <span class="problem" data-testid="product-import-problem">{{ row.problems.join('；') }}</span>
          </template>
        </el-table-column>
      </el-table>
    </template>

    <template #footer>
      <template v-if="preview && !done">
        <el-button @click="discard">重新上传</el-button>
        <el-button
          type="primary"
          :loading="confirming"
          :disabled="!preview.will_create && !preview.will_update"
          data-testid="product-import-confirm"
          @click="confirm"
          >确认导入</el-button
        >
      </template>
      <template v-else-if="preview">
        <el-button data-testid="product-import-result" @click="result">下载导入结果</el-button>
        <el-button type="primary" @click="open = false">完成</el-button>
      </template>
      <el-button v-else @click="open = false">关闭</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.steps {
  margin: 0 0 16px;
  padding-left: 20px;
  line-height: 2;
}

.file {
  display: none;
}

.summary {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}

.problem {
  color: var(--el-color-danger);
  font-size: 12px;
}
</style>
