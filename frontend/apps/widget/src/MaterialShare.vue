<script setup lang="ts">
import { MarkdownView } from '@edp/ui'
import { computed, onMounted, ref } from 'vue'

import { expiryText, shareView, sizeText } from './materials'
import { fetchSharedMaterial, type SharedMaterial } from './visitor'

/**
 * 企业资料的分享页（设计文档 §36.4）：客户凭分享链接查看企业发来的视频、文档、图片或文字资料，不需要
 * 登录，在微信里打开同样可用。能在线看的直接显示，其他的下载后打开；链接到期或被停用后提示失效。
 */
const props = defineProps<{ token: string }>()

const TEXT_LIMIT = 200 * 1024
const material = ref<SharedMaterial | null>(null)
const error = ref<string | null>(null)
const loading = ref(true)
const body = ref('')
const partial = ref(false)
const view = computed(() => (material.value ? shareView(material.value) : 'download'))

/** Markdown、TXT、CSV 文档：取开头 200 KB 显示；取不到时只提供下载。 */
async function loadText(data: SharedMaterial): Promise<void> {
  if (data.text !== null) {
    body.value = data.text
    return
  }
  if (!data.view_url) return
  try {
    partial.value = data.size > TEXT_LIMIT
    const response = await fetch(
      data.view_url,
      partial.value ? { headers: { Range: `bytes=0-${TEXT_LIMIT - 1}` } } : undefined,
    )
    if (!response.ok) throw new Error(String(response.status))
    const bytes = new Uint8Array(await response.arrayBuffer()).slice(0, TEXT_LIMIT)
    body.value = new TextDecoder('utf-8').decode(bytes).replace(/�+$/, '')
  } catch {
    body.value = ''
  }
}

onMounted(async () => {
  document.title = '资料'
  if (!props.token) {
    error.value = '分享链接已失效，请联系发给您链接的人'
    loading.value = false
    return
  }
  try {
    const data = await fetchSharedMaterial(props.token)
    material.value = data
    document.title = data.name
    if (['markdown', 'text'].includes(shareView(data))) await loadText(data)
  } catch (e) {
    error.value = e instanceof Error ? e.message : '分享链接已失效，请联系发给您链接的人'
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <div class="widget share" data-testid="material-share-page">
    <header class="header">
      <span class="title">{{ material?.company ?? '资料' }}</span>
    </header>
    <p v-if="loading" class="empty">正在加载…</p>
    <p v-else-if="error" class="error" role="alert" data-testid="share-error">{{ error }}</p>
    <div v-else-if="material" class="body">
      <section class="card">
        <h1 class="name" data-testid="share-name">{{ material.name }}</h1>
        <p v-if="material.description" class="description">{{ material.description }}</p>
      </section>

      <section class="card viewer">
        <video
          v-if="view === 'video'"
          :src="material.view_url ?? undefined"
          controls
          playsinline
          preload="metadata"
          class="video"
          data-testid="share-video"
        />
        <img
          v-else-if="view === 'image'"
          :src="material.view_url ?? undefined"
          :alt="material.name"
          class="image"
          data-testid="share-image"
        />
        <template v-else-if="view === 'pdf'">
          <iframe :src="material.view_url ?? undefined" :title="material.name" class="pdf" data-testid="share-pdf" />
          <a :href="material.view_url ?? undefined" target="_blank" rel="noopener" class="open">
            看不到内容？在新页面打开 PDF
          </a>
        </template>
        <div v-else-if="body && view === 'markdown'" class="text" data-testid="share-text">
          <MarkdownView :source="body" />
        </div>
        <pre v-else-if="body && view === 'text'" class="text plain" data-testid="share-text">{{ body }}</pre>
        <p v-else class="hint" data-testid="share-download-hint">这个文件需要下载后打开。</p>
        <p v-if="body && partial" class="muted">只显示开头的一部分，完整内容请下载。</p>
      </section>

      <section class="card file">
        <div class="file-info">
          <div class="file-name">{{ material.file_name }}</div>
          <div class="muted">{{ sizeText(material.size) }} · 链接 {{ expiryText(material.expires_at) }} 到期</div>
        </div>
        <a
          v-if="material.download_url"
          :href="material.download_url"
          :download="material.file_name"
          class="download"
          data-testid="share-download"
        >
          下载
        </a>
      </section>
    </div>
  </div>
</template>

<style scoped>
.body {
  flex: 1;
  overflow-y: auto;
  padding: 12px;
}

.card {
  margin-bottom: 12px;
  padding: 12px;
  border-radius: 8px;
  background: #fff;
}

.name {
  margin: 0;
  font-size: 17px;
  line-height: 1.4;
}

.description {
  margin: 8px 0 0;
  color: #606266;
  white-space: pre-wrap;
}

.viewer {
  padding: 0;
  overflow: hidden;
}

.video {
  display: block;
  width: 100%;
  max-height: 70vh;
  background: #000;
}

.image {
  display: block;
  width: 100%;
  height: auto;
}

.pdf {
  display: block;
  width: 100%;
  height: 70vh;
  border: 0;
}

.open {
  display: block;
  padding: 8px 12px;
  font-size: 13px;
  color: var(--primary);
  text-align: center;
}

.text {
  max-height: 70vh;
  overflow-y: auto;
  padding: 12px;
}

.plain {
  margin: 0;
  font-family: inherit;
  font-size: 13px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}

.hint {
  margin: 0;
  padding: 24px 12px;
  color: var(--muted);
  text-align: center;
}

.file {
  display: flex;
  align-items: center;
  gap: 12px;
}

.file-info {
  flex: 1;
  min-width: 0;
}

.file-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.download {
  flex: none;
  padding: 8px 18px;
  border-radius: 6px;
  color: #fff;
  text-decoration: none;
  background: var(--primary);
}

.muted {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--muted);
}

.viewer .muted {
  padding: 0 12px 10px;
}

.empty,
.error {
  padding: 24px 16px;
  text-align: center;
}
</style>
