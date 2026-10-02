<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'

import { FAQ_CSV_TEMPLATE } from '../../ai'
import { api, formatDateTime } from '../../api'
import {
  fileToBase64,
  IMPORT_KIND,
  IMPORT_STATUS,
  IMPORT_STATUS_TAG,
  placementOf,
  placementOptions,
} from '../../knowledge'
import { useAuthStore } from '../../stores/auth'
import { useKbSpacesStore } from '../../stores/kbSpaces'

/**
 * 批量导入知识（冷启动）：粘贴或选择 CSV 问答表（立即导入）；上传文档（PDF、Word、Markdown、网页、
 * 纯文本）或 Excel 问答表、抓取官网帮助中心（后台导入，下方查看进度和结果）。
 */
const props = defineProps<{ modelValue: boolean; placement?: string[] }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean]; imported: [] }>()

const MAX_UPLOAD = 20 * 1024 * 1024
const POLL_MS = 3000
const DOC_ACCEPT = '.pdf,.docx,.md,.markdown,.txt,.html,.htm'
const SHEET_ACCEPT = '.xlsx,.csv'
const spaces = useKbSpacesStore()
const tab = ref<'csv' | 'file' | 'crawl'>('csv')
const target = reactive({
  placement: [] as string[],
  agentOnly: false,
  publish: false,
  policy: false,
})
const upload = reactive({ kind: 'document' as 'document' | 'excel', file: null as File | null })
const crawl = reactive({ url: '', maxPages: 20 })
const submitting = ref(false)
const jobs = ref<Schemas['KbImportJobOut'][]>([])
const placements = computed(() => placementOptions(spaces.spaces))
let poller: ReturnType<typeof setInterval> | undefined

/** 已经看到完成的任务：新完成的任务出现时通知知识库刷新列表。 */
const finished = new Set<string>()
let watching = false

async function loadJobs(): Promise<void> {
  const { data } = await api.GET('/api/v1/kb/imports')
  if (!data) return
  jobs.value = data.items
  const done = data.items.filter((j) => j.status === 'done').map((j) => j.id)
  const fresh = done.some((id) => !finished.has(id))
  done.forEach((id) => finished.add(id))
  if (fresh && watching) emit('imported')
  watching = true
  const busy = data.items.some((j) => j.status === 'pending' || j.status === 'running')
  if (busy && !poller) poller = setInterval(() => void loadJobs(), POLL_MS)
  if (!busy && poller) {
    clearInterval(poller)
    poller = undefined
  }
}

function targetBody() {
  return {
    ...placementOf(target.placement),
    visibility: target.agentOnly ? ('agent' as const) : ('public' as const),
    publish: target.publish,
    // 规章制度（§33.7.1）：只用于文档和网页，问答表不适用。
    policy: target.policy && !(tab.value === 'file' && upload.kind === 'excel'),
  }
}

function pickUpload(event: Event): void {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0] ?? null
  input.value = ''
  if (file && file.size > MAX_UPLOAD) {
    ElMessage.error('文件不能超过 20 MB')
    return
  }
  upload.file = file
}

async function submitJob(): Promise<void> {
  submitting.value = true
  try {
    if (tab.value === 'file') {
      if (!upload.file) {
        ElMessage.warning('请选择文件')
        return
      }
      const { data, error } = await api.POST('/api/v1/kb/imports', {
        body: {
          kind: upload.kind,
          filename: upload.file.name,
          content_base64: await fileToBase64(upload.file),
          ...targetBody(),
        },
      })
      if (!data) throw new Error(errorMessage(error))
      upload.file = null
    } else {
      if (!/^https?:\/\/\S+$/.test(crawl.url.trim())) {
        ElMessage.warning('请填写以 http:// 或 https:// 开头的网址')
        return
      }
      const { data, error } = await api.POST('/api/v1/kb/imports/crawl', {
        body: { url: crawl.url.trim(), max_pages: crawl.maxPages, ...targetBody() },
      })
      if (!data) throw new Error(errorMessage(error))
    }
    ElMessage.success('已开始导入，完成后会收到站内信')
    await loadJobs()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    submitting.value = false
  }
}

onBeforeUnmount(() => clearInterval(poller))

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
    upload.file = null
    target.placement = [...(props.placement ?? [])]
    target.publish = false
    target.agentOnly = false
    target.policy = false
    void spaces.ensure()
    void loadJobs()
  } else if (poller) {
    clearInterval(poller)
    poller = undefined
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
  const blob = new Blob([`\uFEFF${FAQ_CSV_TEMPLATE}\n`], { type: 'text/csv;charset=utf-8' })
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
  <el-dialog v-model="open" title="批量导入知识" width="720px" data-testid="kb-import">
    <el-tabs v-model="tab">
      <el-tab-pane label="问答表（CSV）" name="csv">
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
      </el-tab-pane>

      <el-tab-pane label="上传文件" name="file">
        <el-radio-group v-model="upload.kind" size="small" class="block">
          <el-radio-button value="document">文档</el-radio-button>
          <el-radio-button value="excel">Excel 问答表</el-radio-button>
        </el-radio-group>
        <p class="hint">
          <template v-if="upload.kind === 'document'">
            支持 PDF、Word（.docx）、Markdown、网页和纯文本，最大 20 MB。按标题分节切片，
            每段检索结果带上所在的标题；很长的文档会拆成几条知识。扫描件请先做文字识别。
          </template>
          <template v-else>
            第一个工作表的首行为表头：标准问、答案必填，相似问用竖线 | 分隔，分类可选。
          </template>
        </p>
        <div class="file">
          <label class="el-button el-button--small">
            选择文件
            <input
              type="file"
              :accept="upload.kind === 'document' ? DOC_ACCEPT : SHEET_ACCEPT"
              hidden
              data-testid="kb-upload-file"
              @change="pickUpload"
            />
          </label>
          <span class="muted" data-testid="kb-upload-name">{{ upload.file?.name ?? '未选择' }}</span>
        </div>
      </el-tab-pane>

      <el-tab-pane label="抓取帮助中心" name="crawl">
        <p class="hint">
          从起始网址出发，抓取同一站点、同一目录下的网页（遵守 robots.txt），每页生成一条文档知识；
          再次抓取同一网页时更新原来的知识。
        </p>
        <el-form label-width="90px">
          <el-form-item label="起始网址">
            <el-input
              v-model="crawl.url"
              placeholder="https://help.example.com/docs/"
              data-testid="kb-crawl-url"
            />
          </el-form-item>
          <el-form-item label="最多网页">
            <el-input-number v-model="crawl.maxPages" :min="1" :max="100" />
          </el-form-item>
        </el-form>
      </el-tab-pane>
    </el-tabs>

    <div v-if="tab !== 'csv'" class="target">
      <el-cascader
        v-model="target.placement"
        :options="placements"
        :props="{ checkStrictly: true }"
        clearable
        placeholder="放入空间 / 分类（可不选）"
        size="small"
        data-testid="kb-import-placement"
      />
      <el-checkbox v-model="target.agentOnly">仅坐席可见</el-checkbox>
      <el-checkbox v-if="auth.can('kb:publish')" v-model="target.publish" data-testid="kb-import-publish">
        导入后立即发布
      </el-checkbox>
      <el-checkbox
        v-if="!(tab === 'file' && upload.kind === 'excel')"
        v-model="target.policy"
        data-testid="kb-import-policy"
      >
        规章制度（AI 整理知识库时作为依据）
      </el-checkbox>
    </div>

    <h4 v-if="jobs.length" class="jobs-title">导入记录</h4>
    <el-table v-if="jobs.length" :data="jobs.slice(0, 8)" size="small" data-testid="kb-import-jobs">
      <el-table-column label="来源" min-width="200">
        <template #default="{ row }">
          <el-tag size="small" type="info">{{ IMPORT_KIND[row.kind] ?? row.kind }}</el-tag>
          <span class="source">{{ row.source }}</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="IMPORT_STATUS_TAG[row.status]" data-testid="kb-import-status">
            {{ IMPORT_STATUS[row.status] ?? row.status }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="结果" min-width="180">
        <template #default="{ row }">
          <span v-if="row.status === 'failed'" class="failed">{{ row.error }}</span>
          <span v-else-if="row.status === 'done'" data-testid="kb-import-summary">
            新建 {{ row.result.created }}<template v-if="row.result.updated">、更新 {{ row.result.updated }}</template
            ><template v-if="row.result.pages"> · {{ row.result.pages }} 个网页</template
            ><template v-if="row.result.errors.length"> · {{ row.result.errors.length }} 处问题</template>
          </span>
        </template>
      </el-table-column>
      <el-table-column label="时间" width="150">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
    </el-table>

    <template #footer>
      <el-button @click="open = false">关闭</el-button>
      <el-button
        v-if="tab === 'csv'"
        type="primary"
        :loading="importing"
        data-testid="kb-import-submit"
        @click="submit"
      >
        导入
      </el-button>
      <el-button
        v-else
        type="primary"
        :loading="submitting"
        data-testid="kb-import-start"
        @click="submitJob"
      >
        开始导入
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.hint {
  margin: 0 0 12px;
  font-size: 13px;
  color: var(--el-text-color-regular);
  line-height: 1.6;
}

.block {
  margin-bottom: 8px;
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

.target {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin: 4px 0 8px;
}

.jobs-title {
  margin: 16px 0 6px;
  font-size: 14px;
}

.source {
  margin-left: 6px;
  word-break: break-all;
}

.failed {
  color: var(--el-color-danger);
  font-size: 12px;
}
</style>
