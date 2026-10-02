<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { Grid, List } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, useRoute, useRouter } from 'vue-router'

import { api } from '../api'
import MaterialDrawer from '../components/materials/MaterialDrawer.vue'
import MaterialFolderTree from '../components/materials/MaterialFolderTree.vue'
import MaterialList from '../components/materials/MaterialList.vue'
import MaterialTextDialog from '../components/materials/MaterialTextDialog.vue'
import {
  checkFile,
  KIND_LABEL,
  KINDS,
  sizeText,
  UploadAborted,
  uploadMaterial,
  usagePercent,
  usageText,
  xhrPut,
  type MaterialConfig,
  type MaterialFolder,
  type MaterialKind,
  type MaterialPage,
  type UploadSteps,
} from '../materials'
import { useAuthStore } from '../stores/auth'

/**
 * 企业资料（设计文档 §36）：左边是多层级文件夹，右边是资料（卡片或表格，按类型、标签、上传的人筛选，
 * 搜索名称、说明、标签和文字资料的正文）。上传时浏览器直接传到阿里云 OSS（可以一次选多个文件，大视频
 * 分片上传，显示进度，可以取消）；也可以直接写文字资料。链接可以带 id（打开这份资料）。
 */
type Sort = 'created' | 'name' | 'size' | 'views'
interface UploadItem {
  key: number
  file: File
  folderId: string | null
  loaded: number
  status: 'waiting' | 'uploading' | 'failed'
  error: string
  controller: AbortController
}

const PAGE_SIZE = 30
const VIEW_KEY = 'edp.materials.view'
const SORTS: [Sort, string][] = [
  ['created', '最新上传'],
  ['name', '名称'],
  ['size', '大小'],
  ['views', '浏览最多'],
]

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const config = ref<MaterialConfig | null>(null)
const folders = ref<MaterialFolder[]>([])
const maxDepth = ref(5)
const unfiled = ref(0)
const selected = ref<string | null>(null)
const kind = ref<MaterialKind | ''>('')
const tag = ref('')
const mine = ref(false)
const q = ref('')
const sort = ref<Sort>('created')
const page = ref(1)
const result = ref<MaterialPage | null>(null)
const loading = ref(false)
const mode = ref<'grid' | 'table'>(readMode())
const openId = ref<string | null>(null)
const writing = ref(false)
const uploads = ref<UploadItem[]>([])
const fileInput = ref<HTMLInputElement | null>(null)
let nextKey = 1
let running = false

const enabled = computed(() => config.value?.enabled ?? false)
const canManage = computed(() => config.value?.can_manage ?? auth.can('material:manage'))
/** 新上传、新写的资料放进当前选中的文件夹。 */
const targetFolder = computed(() =>
  selected.value && selected.value !== 'unfiled' ? selected.value : null,
)
const targetFolderName = computed(() => folders.value.find((f) => f.id === targetFolder.value)?.name ?? '')
const percent = computed(() =>
  result.value ? usagePercent(result.value.used_bytes, result.value.limit_bytes) : null,
)
const accept = computed(() =>
  Object.entries(config.value?.extensions ?? {})
    .filter(([name]) => name !== 'text')
    .flatMap(([, list]) => list)
    .join(','),
)
const busyUploads = computed(() => uploads.value.filter((u) => u.status !== 'failed').length)

function readMode(): 'grid' | 'table' {
  try {
    return localStorage.getItem(VIEW_KEY) === 'table' ? 'table' : 'grid'
  } catch {
    return 'grid'
  }
}

watch(mode, (value) => {
  try {
    localStorage.setItem(VIEW_KEY, value)
  } catch {
    // 只影响下次打开时的显示方式。
  }
})

async function loadConfig(): Promise<void> {
  const { data, error } = await api.GET('/api/v1/materials/config')
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  config.value = data
}

async function loadFolders(): Promise<void> {
  const { data, error } = await api.GET('/api/v1/materials/folders')
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  folders.value = data.items
  maxDepth.value = data.max_depth
  unfiled.value = data.unfiled
  if (selected.value && selected.value !== 'unfiled' && !data.items.some((f) => f.id === selected.value)) {
    selected.value = null
  }
}

async function load(): Promise<void> {
  loading.value = true
  const folder = selected.value
  const { data, error } = await api.GET('/api/v1/materials', {
    params: {
      query: {
        folder_id: folder && folder !== 'unfiled' ? folder : undefined,
        unfiled: folder === 'unfiled' ? true : undefined,
        kind: kind.value || undefined,
        tag: tag.value || undefined,
        created_by: mine.value ? auth.me?.id : undefined,
        q: q.value.trim() || undefined,
        sort: sort.value,
        limit: PAGE_SIZE,
        offset: (page.value - 1) * PAGE_SIZE,
      },
    },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  result.value = data
  if (tag.value && !data.tags.includes(tag.value)) tag.value = ''
}

function search(): void {
  if (page.value === 1) void load()
  else page.value = 1
}

function refresh(): void {
  void load()
  void loadFolders()
}

watch([selected, kind, tag, mine, sort], search)
watch(page, load)

// ---- 上传 ----

function stepsFor(item: UploadItem): UploadSteps {
  return {
    async start(file) {
      const { data, error } = await api.POST('/api/v1/materials/uploads', {
        body: { filename: file.name, size: file.size, folder_id: item.folderId },
      })
      if (!data) throw new Error(errorMessage(error))
      return data
    },
    async parts(id, numbers) {
      const { data, error } = await api.POST('/api/v1/materials/{material_id}/parts', {
        params: { path: { material_id: id } },
        body: { part_numbers: numbers },
      })
      if (!data) throw new Error(errorMessage(error))
      return data.parts
    },
    async complete(id, parts) {
      const { data, error } = await api.POST('/api/v1/materials/{material_id}/complete', {
        params: { path: { material_id: id } },
        body: { parts },
      })
      if (!data) throw new Error(errorMessage(error))
      return data
    },
    async abort(id) {
      await api.DELETE('/api/v1/materials/{material_id}/upload', { params: { path: { material_id: id } } })
    },
    put: xhrPut,
  }
}

function pickFiles(): void {
  fileInput.value?.click()
}

function onFiles(event: Event): void {
  const input = event.target as HTMLInputElement
  const files = [...(input.files ?? [])]
  input.value = ''
  if (!config.value) return
  const problems: string[] = []
  for (const file of files) {
    const problem = checkFile(file, config.value)
    if (problem) {
      problems.push(problem)
      continue
    }
    uploads.value.push({
      key: nextKey++,
      file,
      folderId: targetFolder.value,
      loaded: 0,
      status: 'waiting',
      error: '',
      controller: new AbortController(),
    })
  }
  if (problems.length) ElMessage({ type: 'error', message: problems.join('；'), duration: 8000, showClose: true })
  void runQueue()
}

/** 一个一个地上传（大文件自己同时传 3 个分片）。 */
async function runQueue(): Promise<void> {
  if (running) return
  running = true
  try {
    for (let item = next(); item; item = next()) {
      const current = item
      current.status = 'uploading'
      try {
        const material = await uploadMaterial(
          current.file,
          stepsFor(current),
          (loaded) => {
            current.loaded = loaded
          },
          current.controller.signal,
        )
        uploads.value = uploads.value.filter((u) => u.key !== current.key)
        ElMessage.success(
          material.scan_status === 'pending' ? `已上传「${material.name}」，正在做病毒扫描` : `已上传「${material.name}」`,
        )
        refresh()
      } catch (e) {
        if (e instanceof UploadAborted) {
          uploads.value = uploads.value.filter((u) => u.key !== current.key)
        } else {
          current.status = 'failed'
          current.error = e instanceof Error ? e.message : String(e)
        }
      }
    }
  } finally {
    running = false
  }
}

function next(): UploadItem | undefined {
  return uploads.value.find((u) => u.status === 'waiting')
}

function cancel(item: UploadItem): void {
  if (item.status === 'uploading') item.controller.abort()
  else uploads.value = uploads.value.filter((u) => u.key !== item.key)
}

function retry(item: UploadItem): void {
  Object.assign(item, { status: 'waiting', error: '', loaded: 0, controller: new AbortController() })
  void runQueue()
}

function progress(item: UploadItem): number {
  return Math.min(100, Math.floor((item.loaded / Math.max(1, item.file.size)) * 100))
}

function warnUnload(event: BeforeUnloadEvent): void {
  if (busyUploads.value === 0) return
  event.preventDefault()
  event.returnValue = ''
}

onBeforeRouteLeave(async () => {
  if (busyUploads.value === 0) return true
  try {
    await ElMessageBox.confirm(`还有 ${busyUploads.value} 个文件没有上传完，离开页面会取消上传。`, '离开页面', {
      confirmButtonText: '离开',
      cancelButtonText: '留下',
      type: 'warning',
    })
  } catch {
    return false
  }
  for (const item of uploads.value) item.controller.abort()
  uploads.value = []
  return true
})

// ---- 文字资料、详情 ----

function onWritten(id: string): void {
  refresh()
  openId.value = id
}

/** 链接带的 id：打开这份资料；用过就从地址里去掉。 */
function fromQuery(): void {
  const { id } = route.query
  if (typeof id === 'string' && id) {
    openId.value = id
    void router.replace({ query: { ...route.query, id: undefined } })
  }
}

watch(() => route.query.id, fromQuery)

onMounted(async () => {
  window.addEventListener('beforeunload', warnUnload)
  await Promise.all([loadConfig(), loadFolders(), load()])
  fromQuery()
})

onBeforeUnmount(() => window.removeEventListener('beforeunload', warnUnload))
</script>

<template>
  <div>
    <div class="page-header">
      <h2>资料</h2>
      <span>
        <el-button :disabled="!enabled" data-testid="material-write" @click="writing = true">写文字资料</el-button>
        <el-button type="primary" :disabled="!enabled" data-testid="material-upload" @click="pickFiles">
          上传资料
        </el-button>
        <input
          ref="fileInput"
          type="file"
          multiple
          :accept="accept"
          class="hidden"
          data-testid="material-file-input"
          @change="onFiles"
        />
      </span>
    </div>

    <el-alert
      v-if="config && !enabled"
      type="warning"
      :closable="false"
      show-icon
      class="notice"
      title="还没有配置企业资料存储（阿里云 OSS），暂时不能上传和查看资料，请联系平台。"
      data-testid="material-not-configured"
    />

    <div class="layout">
      <aside class="side">
        <MaterialFolderTree
          :folders="folders"
          :max-depth="maxDepth"
          :unfiled="unfiled"
          :selected="selected"
          :can-manage="canManage"
          @select="selected = $event"
          @changed="refresh"
        />
        <div v-if="result" class="usage" data-testid="material-usage">
          <div class="usage-text">企业资料{{ usageText(result.used_bytes, result.limit_bytes) }}</div>
          <el-progress
            v-if="percent !== null"
            :percentage="percent"
            :stroke-width="6"
            :show-text="false"
            :status="percent >= 100 ? 'exception' : percent >= 90 ? 'warning' : undefined"
          />
        </div>
      </aside>
      <section class="main">
        <div v-if="uploads.length" class="uploads" data-testid="material-uploads">
          <div v-for="item in uploads" :key="item.key" class="upload" data-testid="material-upload-item">
            <div class="upload-line">
              <span class="upload-name" :title="item.file.name">{{ item.file.name }}</span>
              <span class="muted">{{ sizeText(item.file.size) }}</span>
              <span v-if="item.status === 'waiting'" class="muted">等待上传</span>
              <span v-else-if="item.status === 'failed'" class="error" data-testid="material-upload-error">
                {{ item.error }}
              </span>
              <el-button v-if="item.status === 'failed'" link type="primary" size="small" @click="retry(item)">
                重试
              </el-button>
              <el-button link size="small" data-testid="material-upload-cancel" @click="cancel(item)">
                {{ item.status === 'failed' ? '移除' : '取消' }}
              </el-button>
            </div>
            <el-progress
              v-if="item.status === 'uploading'"
              :percentage="progress(item)"
              :stroke-width="6"
              data-testid="material-upload-progress"
            />
          </div>
          <div v-if="targetFolderName" class="muted small">上传到文件夹「{{ targetFolderName }}」</div>
        </div>

        <div class="filters">
          <el-radio-group v-model="kind" size="small" data-testid="material-kinds">
            <el-radio-button value="">全部</el-radio-button>
            <el-radio-button v-for="k in KINDS" :key="k" :value="k" :data-testid="`material-kind-${k}`">
              {{ KIND_LABEL[k] }}
            </el-radio-button>
          </el-radio-group>
          <el-select
            v-model="tag"
            clearable
            filterable
            placeholder="标签"
            size="small"
            class="tag"
            data-testid="material-tag-filter"
          >
            <el-option v-for="t in result?.tags ?? []" :key="t" :label="t" :value="t" />
          </el-select>
          <el-checkbox v-model="mine" size="small" data-testid="material-mine">只看我上传的</el-checkbox>
          <el-input
            v-model="q"
            clearable
            placeholder="名称、说明、标签、正文"
            size="small"
            class="search"
            data-testid="material-search"
            @keyup.enter="search"
            @clear="search"
          />
          <el-select v-model="sort" size="small" class="sort" data-testid="material-sort">
            <el-option v-for="[value, label] in SORTS" :key="value" :label="label" :value="value" />
          </el-select>
          <el-radio-group v-model="mode" size="small" class="mode">
            <el-radio-button value="grid" title="卡片" aria-label="卡片" data-testid="material-mode-grid">
              <el-icon><Grid /></el-icon>
            </el-radio-button>
            <el-radio-button value="table" title="列表" aria-label="列表" data-testid="material-mode-table">
              <el-icon><List /></el-icon>
            </el-radio-button>
          </el-radio-group>
        </div>

        <MaterialList
          :items="result?.items ?? []"
          :mode="mode"
          :loading="loading"
          @open="openId = $event"
        />
        <el-pagination
          v-if="(result?.total ?? 0) > PAGE_SIZE"
          v-model:current-page="page"
          :page-size="PAGE_SIZE"
          :total="result?.total ?? 0"
          layout="total, prev, pager, next"
          class="pager"
        />
      </section>
    </div>

    <MaterialDrawer
      :material-id="openId"
      :folders="folders"
      :config="config"
      :tag-options="result?.tags ?? []"
      @close="openId = null"
      @changed="refresh"
    />
    <MaterialTextDialog
      v-model="writing"
      :folders="folders"
      :folder-id="targetFolder"
      :editing="null"
      :max-chars="config?.text_max_chars ?? 200000"
      :tag-options="result?.tags ?? []"
      @saved="(material) => onWritten(material.id)"
    />
  </div>
</template>

<style scoped>
.hidden {
  display: none;
}

.notice {
  margin-bottom: 12px;
}

.layout {
  display: grid;
  grid-template-columns: 240px minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}

.side {
  position: sticky;
  top: 0;
}

.usage {
  margin-top: 12px;
  padding: 10px 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-bg-color);
}

.usage-text {
  margin-bottom: 6px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.uploads {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 12px;
  padding: 10px 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-bg-color);
}

.upload-line {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}

.upload-name {
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.upload-line .el-button + .el-button {
  margin-left: 0;
}

.error {
  color: var(--el-color-danger);
}

.filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}

.tag {
  width: 110px;
}

.search {
  width: 180px;
}

.sort {
  width: 110px;
}

.mode {
  margin-left: auto;
}

.pager {
  justify-content: flex-end;
  margin-top: 12px;
}

.muted {
  color: var(--el-text-color-secondary);
}

.small {
  font-size: 12px;
}

@media (max-width: 900px) {
  .layout {
    grid-template-columns: minmax(0, 1fr);
  }

  .side {
    position: static;
  }

  .mode {
    margin-left: 0;
  }
}
</style>
