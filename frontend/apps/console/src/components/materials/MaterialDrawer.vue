<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { MarkdownView } from '@edp/ui'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import { api, formatDateTime } from '../../api'
import { placementOf, placementOptions } from '../../knowledge'
import {
  canJoinKnowledge,
  fetchTextPreview,
  folderOptions,
  folderPath,
  folderTree,
  KIND_LABEL,
  KIND_TAG,
  previewKind,
  SCAN_LABEL,
  SHARE_DAYS,
  sizeText,
  type Material,
  type MaterialConfig,
  type MaterialFolder,
  type MaterialShare,
} from '../../materials'
import { copyText } from '../../orders'
import { useAuthStore } from '../../stores/auth'
import { useKbSpacesStore } from '../../stores/kbSpaces'
import MaterialTextDialog from './MaterialTextDialog.vue'

/**
 * 资料详情（§36.4）：在线查看（视频播放、图片、PDF、纯文本、文字资料）、下载、修改名称说明标签和文件夹、
 * 修改文字资料的正文、分享给客户（1–30 天的链接，可以停用）、加入知识库、删除。
 */
const props = defineProps<{
  materialId: string | null
  folders: MaterialFolder[]
  config: MaterialConfig | null
  tagOptions: string[]
}>()
const emit = defineEmits<{ close: []; changed: [] }>()

const auth = useAuthStore()
const spaces = useKbSpacesStore()
const material = ref<Material | null>(null)
const loading = ref(false)
const busy = ref('')
const preview = reactive({
  url: '',
  text: '',
  partial: false,
  error: '',
  loading: false,
})
const edit = reactive({ name: '', description: '', tags: [] as string[], folder: [] as string[] })
const shares = ref<MaterialShare[]>([])
const shareDays = ref(7)
const textEditing = ref(false)
const knowledge = reactive({ open: false, placement: [] as string[], agentOnly: false, publish: false })

const open = computed({
  get: () => props.materialId !== null,
  set: (value) => {
    if (!value) emit('close')
  },
})
const kind = computed(() => (material.value ? previewKind(material.value) : 'none'))
const ready = computed(() => material.value?.status === 'ready')
const options = computed(() => folderOptions(folderTree(props.folders)))
const canKnowledge = computed(
  () => !!props.config?.can_import && !!material.value && canJoinKnowledge(material.value),
)
const placements = computed(() => placementOptions(spaces.spaces))

function path() {
  return { params: { path: { material_id: props.materialId ?? '' } } }
}

/** 查看（1 小时）或下载（5 分钟）的签名地址；平台记一次浏览或下载。 */
function linkOf(purpose: 'view' | 'download') {
  return api.GET('/api/v1/materials/{material_id}/link', {
    params: { path: { material_id: props.materialId ?? '' }, query: { purpose } },
  })
}

function fill(data: Material): void {
  material.value = data
  Object.assign(edit, {
    name: data.name,
    description: data.description ?? '',
    tags: [...data.tags],
    folder: folderPath(props.folders, data.folder_id),
  })
}

async function load(): Promise<void> {
  if (!props.materialId) return
  loading.value = true
  const { data, error } = await api.GET('/api/v1/materials/{material_id}', path())
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    emit('close')
    return
  }
  fill(data)
  if (data.status !== 'ready') return
  await Promise.all([loadPreview(data), loadShares()])
}

/** 在线查看：文字资料读正文；视频、图片、PDF 用查看地址；纯文本文档读开头 200 KB。 */
async function loadPreview(data: Material): Promise<void> {
  const how = previewKind(data)
  if (how === 'none') return
  preview.loading = true
  try {
    if (how === 'markdown') {
      const { data: text, error } = await api.GET('/api/v1/materials/{material_id}/text', path())
      if (!text) throw new Error(errorMessage(error))
      preview.text = text.body
      return
    }
    const { data: link, error } = await linkOf('view')
    if (!link) throw new Error(errorMessage(error))
    if (how === 'text') {
      const result = await fetchTextPreview(link.url, data.size)
      preview.text = result.text
      preview.partial = result.partial
    } else {
      preview.url = link.url
    }
  } catch (e) {
    preview.error = e instanceof Error ? e.message : String(e)
  } finally {
    preview.loading = false
  }
}

async function loadShares(): Promise<void> {
  const { data } = await api.GET('/api/v1/materials/{material_id}/shares', path())
  shares.value = data?.items ?? []
}

async function save(): Promise<void> {
  const current = material.value
  if (!current) return
  if (!edit.name.trim()) {
    ElMessage.warning('请填写名称')
    return
  }
  const folderId = edit.folder.at(-1) ?? null
  const tags = edit.tags.map((t) => t.trim().slice(0, 32)).filter(Boolean)
  busy.value = 'save'
  const { data, error } = await api.PATCH('/api/v1/materials/{material_id}', {
    ...path(),
    body: {
      name: edit.name.trim(),
      description: edit.description.trim() || null,
      tags: [...new Set(tags)].slice(0, 10),
      folder_id: folderId,
    },
  })
  busy.value = ''
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  fill(data)
  ElMessage.success('已保存')
  emit('changed')
}

async function download(): Promise<void> {
  busy.value = 'download'
  const { data, error } = await linkOf('download')
  busy.value = ''
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  // 签名地址带"附件"的文件名，浏览器直接下载。
  const anchor = document.createElement('a')
  anchor.href = data.url
  anchor.download = data.filename
  anchor.rel = 'noopener'
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  if (material.value) material.value = { ...material.value, downloads: material.value.downloads + 1 }
}

async function createShare(): Promise<void> {
  busy.value = 'share'
  const { data, error } = await api.POST('/api/v1/materials/{material_id}/shares', {
    ...path(),
    body: { days: shareDays.value },
  })
  busy.value = ''
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  shares.value = [data, ...shares.value]
  ElMessage.success((await copyText(data.url)) ? '已生成分享链接并复制' : '已生成分享链接')
  emit('changed')
}

async function copyShare(share: MaterialShare): Promise<void> {
  if (await copyText(share.url)) ElMessage.success('已复制分享链接')
  else ElMessage.warning('复制失败，请手动复制')
}

async function disableShare(share: MaterialShare): Promise<void> {
  try {
    await ElMessageBox.confirm('停用后客户打开这个链接会提示已失效。', '停用分享链接', {
      confirmButtonText: '停用',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  const { error } = await api.DELETE('/api/v1/materials/shares/{material_share_id}', {
    params: { path: { material_share_id: share.id } },
  })
  if (error) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已停用')
  await loadShares()
  emit('changed')
}

function shareState(share: MaterialShare): { label: string; type: 'success' | 'info' | 'warning' } {
  if (share.active) return { label: '有效', type: 'success' }
  if (share.disabled_at) return { label: '已停用', type: 'info' }
  return { label: '已过期', type: 'warning' }
}

async function openKnowledge(): Promise<void> {
  await spaces.ensure()
  Object.assign(knowledge, { open: true, placement: [], agentOnly: false, publish: false })
}

async function addToKnowledge(): Promise<void> {
  busy.value = 'knowledge'
  const { data, error } = await api.POST('/api/v1/materials/{material_id}/knowledge', {
    ...path(),
    body: {
      ...placementOf(knowledge.placement),
      visibility: knowledge.agentOnly ? 'agent' : 'public',
      publish: knowledge.publish,
      policy: false,
    },
  })
  busy.value = ''
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  knowledge.open = false
  ElMessage.success('已开始加入知识库，完成后会收到站内信')
}

async function remove(): Promise<void> {
  const current = material.value
  if (!current) return
  const uploading = current.status === 'uploading'
  try {
    await ElMessageBox.confirm(
      uploading
        ? '取消这次没有完成的上传？已经传上去的部分会一起删除。'
        : `删除「${current.name}」？存储里的文件会一起删除，分享链接随之失效，不能恢复。`,
      uploading ? '取消上传' : '删除资料',
      { confirmButtonText: uploading ? '取消上传' : '删除', cancelButtonText: '返回', type: 'warning' },
    )
  } catch {
    return
  }
  busy.value = 'delete'
  const { error } = uploading
    ? await api.DELETE('/api/v1/materials/{material_id}/upload', path())
    : await api.DELETE('/api/v1/materials/{material_id}', path())
  busy.value = ''
  if (error) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success(uploading ? '已取消上传' : '已删除')
  emit('changed')
  emit('close')
}

/** 修改正文：对话框从当前的正文开始，保存后预览换成新的正文。 */
const textBody = ref('')
function editText(): void {
  textBody.value = preview.text
  textEditing.value = true
}

function onTextSaved(data: Material, body: string): void {
  fill(data)
  preview.text = body
  emit('changed')
}

watch(
  () => props.materialId,
  (id) => {
    material.value = null
    shares.value = []
    Object.assign(preview, { url: '', text: '', partial: false, error: '', loading: false })
    if (id) void load()
  },
  { immediate: true },
)
</script>

<template>
  <el-drawer
    v-model="open"
    :title="material?.name ?? '资料'"
    size="720px"
    append-to-body
    data-testid="material-drawer"
  >
    <div v-loading="loading" class="body">
      <template v-if="material">
        <div class="head">
          <el-tag :type="KIND_TAG[material.kind]" data-testid="material-kind">{{ KIND_LABEL[material.kind] }}</el-tag>
          <el-tag v-if="material.status === 'uploading'" type="warning" effect="plain">上传没有完成</el-tag>
          <el-tag v-if="material.status === 'blocked'" type="danger" data-testid="material-blocked">
            含有病毒，已拦截
          </el-tag>
          <span class="muted">{{ material.ext.replace('.', '').toUpperCase() }} · {{ sizeText(material.size) }}</span>
          <span v-if="material.folder_path" class="muted">· {{ material.folder_path }}</span>
        </div>

        <section class="block">
          <el-alert
            v-if="material.status === 'blocked'"
            type="error"
            :closable="false"
            show-icon
            title="病毒扫描发现这个文件含有病毒，文件已经从存储里删除，不能查看、下载或分享。"
          />
          <el-alert
            v-else-if="material.status === 'uploading'"
            type="warning"
            :closable="false"
            show-icon
            title="这个文件的上传没有完成（例如上传时关闭了页面）。可以取消后重新上传；24 小时后会自动清理。"
          />
          <div v-else-if="preview.loading" v-loading="true" class="preview-box" />
          <el-alert v-else-if="preview.error" type="warning" :closable="false" :title="`不能在线查看：${preview.error}`" />
          <template v-else-if="kind === 'video'">
            <video
              :src="preview.url"
              :poster="material.cover_url ?? undefined"
              controls
              preload="metadata"
              class="video"
              data-testid="material-video"
            />
          </template>
          <template v-else-if="kind === 'image'">
            <el-image
              :src="preview.url"
              :preview-src-list="[preview.url]"
              fit="contain"
              class="image"
              data-testid="material-image"
            />
          </template>
          <template v-else-if="kind === 'pdf'">
            <iframe :src="preview.url" class="pdf" :title="material.name" data-testid="material-pdf" />
          </template>
          <template v-else-if="kind === 'markdown'">
            <div class="text-box" data-testid="material-markdown">
              <MarkdownView :source="preview.text" />
            </div>
          </template>
          <template v-else-if="kind === 'text'">
            <div class="text-box" data-testid="material-text-preview">
              <MarkdownView v-if="material.ext === '.md' || material.ext === '.markdown'" :source="preview.text" />
              <pre v-else class="plain">{{ preview.text }}</pre>
            </div>
            <p v-if="preview.partial" class="muted small">只显示开头 200 KB，完整内容请下载。</p>
          </template>
          <el-empty
            v-else
            :image-size="56"
            description="Word、Excel、PPT 和压缩包不能在线查看，请下载后打开"
            data-testid="material-no-preview"
          />
        </section>

        <div class="actions">
          <el-button
            v-if="ready && material.kind !== 'text'"
            type="primary"
            :loading="busy === 'download'"
            data-testid="material-download"
            @click="download"
          >
            下载
          </el-button>
          <el-button
            v-if="ready && material.kind === 'text' && material.can_edit"
            type="primary"
            data-testid="material-edit-text"
            @click="editText"
          >
            修改正文
          </el-button>
          <el-button v-if="ready && material.kind === 'text'" :loading="busy === 'download'" @click="download">
            下载 .md
          </el-button>
          <el-button v-if="canKnowledge" data-testid="material-knowledge" @click="openKnowledge">加入知识库</el-button>
          <el-button
            v-if="material.can_edit"
            type="danger"
            plain
            :loading="busy === 'delete'"
            data-testid="material-delete"
            @click="remove"
          >
            {{ material.status === 'uploading' ? '取消上传' : '删除' }}
          </el-button>
        </div>

        <section class="block">
          <h4>资料信息</h4>
          <el-form v-if="material.can_edit && material.status !== 'uploading'" label-width="64px" size="small">
            <el-form-item label="名称">
              <el-input v-model="edit.name" maxlength="200" data-testid="material-edit-name" />
            </el-form-item>
            <el-form-item label="说明">
              <el-input
                v-model="edit.description"
                type="textarea"
                :autosize="{ minRows: 2, maxRows: 5 }"
                maxlength="2000"
                placeholder="选填：这份资料讲什么、什么时候用"
                data-testid="material-edit-description"
              />
            </el-form-item>
            <el-form-item label="标签">
              <el-select
                v-model="edit.tags"
                multiple
                filterable
                allow-create
                default-first-option
                :multiple-limit="10"
                placeholder="可以输入新标签"
                class="wide"
                data-testid="material-edit-tags"
              >
                <el-option v-for="tag in tagOptions" :key="tag" :label="tag" :value="tag" />
              </el-select>
            </el-form-item>
            <el-form-item label="文件夹">
              <div data-testid="material-edit-folder" class="wide">
                <el-cascader
                  v-model="edit.folder"
                  :options="options"
                  :props="{ checkStrictly: true }"
                  clearable
                  placeholder="不放进文件夹"
                  class="wide"
                />
              </div>
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="busy === 'save'" data-testid="material-save" @click="save">
                保存修改
              </el-button>
            </el-form-item>
          </el-form>
          <template v-else>
            <p v-if="material.description" class="description">{{ material.description }}</p>
            <div v-if="material.tags.length" class="tags">
              <el-tag v-for="tag in material.tags" :key="tag" size="small" effect="plain">{{ tag }}</el-tag>
            </div>
          </template>
          <dl class="facts" data-testid="material-facts">
            <dt>文件名</dt>
            <dd>{{ material.file_name }}</dd>
            <dt>上传人</dt>
            <dd>{{ material.created_by_name ?? '—' }} · {{ formatDateTime(material.uploaded_at ?? material.created_at) }}</dd>
            <dt>浏览 / 下载</dt>
            <dd data-testid="material-counts">{{ material.views }} 次 / {{ material.downloads }} 次</dd>
            <template v-if="material.scan_status">
              <dt>病毒扫描</dt>
              <dd :class="{ danger: material.scan_status === 'infected' }" data-testid="material-scan">
                {{ SCAN_LABEL[material.scan_status] }}
              </dd>
            </template>
          </dl>
        </section>

        <section v-if="ready" class="block" data-testid="material-shares">
          <h4>
            分享给客户
            <span class="muted small">客户打开链接就能查看和下载，不用登录；到期或停用后失效</span>
          </h4>
          <div class="share-new">
            <el-select v-model="shareDays" class="days" data-testid="material-share-days">
              <el-option v-for="d in SHARE_DAYS" :key="d" :label="`${d} 天有效`" :value="d" />
            </el-select>
            <el-button :loading="busy === 'share'" data-testid="material-share-create" @click="createShare">
              生成分享链接
            </el-button>
          </div>
          <div v-for="share in shares" :key="share.id" class="share" data-testid="material-share">
            <div class="share-line">
              <el-tag size="small" :type="shareState(share).type">{{ shareState(share).label }}</el-tag>
              <el-input :model-value="share.url" readonly size="small" class="share-url" data-testid="material-share-url" />
              <el-button size="small" @click="copyShare(share)">复制</el-button>
              <el-button
                v-if="share.active && share.can_disable"
                size="small"
                type="danger"
                link
                data-testid="material-share-disable"
                @click="disableShare(share)"
              >
                停用
              </el-button>
            </div>
            <div class="muted small">
              {{ share.created_by_name ?? '—' }} {{ formatDateTime(share.created_at) }} 生成 ·
              {{ formatDateTime(share.expires_at) }} 到期 · 打开 {{ share.opens }} 次
              <template v-if="share.last_opened_at">，最近 {{ formatDateTime(share.last_opened_at) }}</template>
            </div>
          </div>
        </section>
      </template>
    </div>

    <el-dialog v-model="knowledge.open" title="加入知识库" width="460px" append-to-body>
      <p class="muted small">
        文件留在企业资料里，知识库导入一份副本，按标题切成知识条目；默认导入为草稿，由知识管理员检查后发布。
      </p>
      <el-form label-width="76px">
        <el-form-item label="放入">
          <div data-testid="material-knowledge-placement" class="wide">
            <el-cascader
              v-model="knowledge.placement"
              :options="placements"
              :props="{ checkStrictly: true }"
              clearable
              placeholder="不选空间和分类"
              class="wide"
            />
          </div>
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="knowledge.agentOnly">仅坐席可见</el-checkbox>
          <el-checkbox v-if="auth.can('kb:publish')" v-model="knowledge.publish">导入后立即发布</el-checkbox>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="knowledge.open = false">取消</el-button>
        <el-button
          type="primary"
          :loading="busy === 'knowledge'"
          data-testid="material-knowledge-save"
          @click="addToKnowledge"
        >
          加入知识库
        </el-button>
      </template>
    </el-dialog>
    <MaterialTextDialog
      v-if="material"
      v-model="textEditing"
      :folders="folders"
      :folder-id="material.folder_id"
      :editing="{ id: material.id, name: material.name, body: textBody }"
      :max-chars="config?.text_max_chars ?? 200000"
      :tag-options="tagOptions"
      @saved="onTextSaved"
    />
  </el-drawer>
</template>

<style scoped>
.body {
  min-height: 200px;
}

.head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.block {
  margin-top: 16px;
}

h4 {
  margin: 0 0 10px;
  font-size: 14px;
}

.preview-box {
  height: 240px;
}

.video {
  display: block;
  width: 100%;
  max-height: 420px;
  border-radius: 6px;
  background: #000;
}

.image {
  display: block;
  width: 100%;
  height: 360px;
  border-radius: 6px;
  background: var(--el-fill-color-light);
}

.pdf {
  display: block;
  width: 100%;
  height: 520px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
}

.text-box {
  max-height: 460px;
  overflow: auto;
  padding: 10px 14px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  background: var(--el-fill-color-blank);
}

.plain {
  margin: 0;
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}

.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
}

.actions .el-button + .el-button {
  margin-left: 0;
}

.wide,
.wide :deep(.el-cascader) {
  width: 100%;
}

.description {
  margin: 0 0 8px;
  white-space: pre-wrap;
}

.tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}

.facts {
  display: grid;
  grid-template-columns: 76px 1fr;
  gap: 6px 12px;
  margin: 8px 0 0;
  font-size: 13px;
}

.facts dt {
  color: var(--el-text-color-secondary);
  text-align: right;
}

.facts dd {
  margin: 0;
  word-break: break-all;
}

.facts dd.danger {
  color: var(--el-color-danger);
}

.share-new {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
}

.days {
  width: 130px;
}

.share {
  padding: 8px 0;
  border-top: 1px solid var(--el-border-color-lighter);
}

.share-line {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}

.share-url {
  flex: 1;
}

.muted {
  color: var(--el-text-color-secondary);
}

.small {
  font-size: 12px;
  font-weight: normal;
}
</style>
