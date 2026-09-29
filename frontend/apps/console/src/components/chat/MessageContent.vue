<script setup lang="ts">
import type { WorkbenchMessage } from '../../workbench/messages'

defineProps<{ message: WorkbenchMessage }>()

function formatSize(size: number | null): string {
  if (!size) return ''
  return size >= 1024 * 1024
    ? `${(size / 1024 / 1024).toFixed(1)} MB`
    : `${Math.ceil(size / 1024)} KB`
}
</script>

<template>
  <el-image
    v-if="message.contentType === 'image' && message.attachment"
    :src="message.attachment.url"
    :preview-src-list="[message.attachment.url]"
    preview-teleported
    fit="contain"
    class="image"
    data-testid="message-image"
  />
  <a
    v-else-if="message.contentType === 'file' && message.attachment"
    :href="message.attachment.url"
    target="_blank"
    rel="noopener"
    class="file"
    data-testid="message-file"
  >
    <span class="file-name">{{ message.attachment.name ?? '文件' }}</span>
    <small>{{ formatSize(message.attachment.size) }}</small>
  </a>
  <template v-else-if="message.text !== null">{{ message.text }}</template>
  <span v-else class="unsupported">[{{ message.contentType }}]</span>
</template>

<style scoped>
.image {
  display: block;
  max-width: 240px;
  max-height: 240px;
  cursor: zoom-in;
}

.file {
  display: flex;
  flex-direction: column;
  min-width: 160px;
  color: var(--el-color-primary);
  text-decoration: none;
}

.file-name {
  font-weight: 500;
  word-break: break-all;
}

.file small,
.unsupported {
  color: var(--el-text-color-secondary);
}
</style>
