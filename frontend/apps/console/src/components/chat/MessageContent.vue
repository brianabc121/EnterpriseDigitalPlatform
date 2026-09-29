<script setup lang="ts">
import type { WorkbenchMessage } from '../../workbench/messages'

defineProps<{ message: WorkbenchMessage }>()

// 浏览器能直接播放的音频（微信客服的 AMR 语音转成 MP3 后才能播放）。
function playable(mime: string | null): boolean {
  return !mime || !/amr|silk/i.test(mime)
}

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
  <div
    v-else-if="message.contentType === 'voice' && message.attachment"
    class="voice"
    data-testid="message-voice"
  >
    <audio
      v-if="playable(message.attachment.mime)"
      :src="message.attachment.url"
      controls
      preload="none"
    />
    <a v-else :href="message.attachment.url" target="_blank" rel="noopener">下载语音</a>
    <div v-if="message.transcript" class="transcript" data-testid="voice-transcript">
      {{ message.transcript }}
    </div>
  </div>
  <video
    v-else-if="message.contentType === 'video' && message.attachment"
    :src="message.attachment.url"
    controls
    preload="none"
    class="video"
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
  <template v-else-if="message.text !== null">
    {{ message.text }}
    <div v-if="message.menu?.length" class="menu" data-testid="message-menu">
      <span v-for="m in message.menu" :key="m.id" class="option">{{ m.content }}</span>
    </div>
  </template>
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

.voice audio {
  display: block;
  max-width: 260px;
  height: 36px;
}

.transcript {
  margin-top: 4px;
  font-size: 13px;
  color: var(--el-text-color-regular);
}

.video {
  display: block;
  max-width: 280px;
  max-height: 240px;
}

.menu {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 6px;
}

.option {
  padding: 1px 8px;
  font-size: 12px;
  border-radius: 10px;
  color: var(--el-color-primary);
  border: 1px solid var(--el-color-primary-light-5);
}

.file small,
.unsupported {
  color: var(--el-text-color-secondary);
}
</style>
