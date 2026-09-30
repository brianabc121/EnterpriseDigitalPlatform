<script setup lang="ts">
import { formatSize, playableAudio } from './format'
import TextWithLinks from './TextWithLinks.vue'
import type { MessageView } from './types'

/** 一条消息的内容：文本（链接可点击）、图片、文件、语音（含转写）、视频，以及被删除的附件。
 *
 * 插槽：image（替换默认的图片展示，如控制台的大图预览）、默认插槽（文本下方的附加内容，如菜单）。
 */
defineProps<{ message: MessageView }>()
</script>

<template>
  <span v-if="message.removed" class="edp-removed" data-testid="message-removed">
    {{ message.removed.reason === 'blocked' ? '文件含有病毒，已被拦截' : '文件已超过保留期'
    }}{{ message.removed.name ? `：${message.removed.name}` : '' }}
  </span>
  <template v-else-if="message.contentType === 'image' && message.attachment">
    <slot name="image" :url="message.attachment.url">
      <a :href="message.attachment.url" target="_blank" rel="noopener" class="edp-image">
        <img :src="message.attachment.url" alt="图片" data-testid="message-image" />
      </a>
    </slot>
  </template>
  <div
    v-else-if="message.contentType === 'voice' && message.attachment"
    class="edp-voice"
    data-testid="message-voice"
  >
    <audio
      v-if="playableAudio(message.attachment.mime)"
      :src="message.attachment.url"
      controls
      preload="none"
    />
    <a v-else :href="message.attachment.url" target="_blank" rel="noopener">下载语音</a>
    <div v-if="message.transcript" class="edp-transcript" data-testid="voice-transcript">
      {{ message.transcript }}
    </div>
  </div>
  <video
    v-else-if="message.contentType === 'video' && message.attachment"
    :src="message.attachment.url"
    controls
    preload="none"
    class="edp-video"
  />
  <a
    v-else-if="message.attachment"
    :href="message.attachment.url"
    target="_blank"
    rel="noopener"
    class="edp-file"
    data-testid="message-file"
  >
    <span class="edp-file-name">📎 {{ message.attachment.name ?? '文件' }}</span>
    <small>{{ formatSize(message.attachment.size) }}</small>
  </a>
  <template v-else-if="message.text !== null">
    <TextWithLinks :text="message.text" />
    <slot />
  </template>
  <span v-else class="edp-unsupported">[{{ message.contentType }}]</span>
</template>

<style scoped>
.edp-image img {
  display: block;
  max-width: 220px;
  max-height: 220px;
  border-radius: 6px;
}

.edp-file {
  display: inline-flex;
  flex-direction: column;
  min-width: 140px;
  color: inherit;
  text-decoration: none;
}

.edp-file-name {
  font-weight: 500;
  word-break: break-all;
}

.edp-file small,
.edp-unsupported,
.edp-removed {
  color: var(--edp-muted, #909399);
}

.edp-removed {
  font-style: italic;
}

.edp-voice audio {
  display: block;
  max-width: 260px;
  height: 36px;
}

.edp-transcript {
  margin-top: 4px;
  font-size: 13px;
}

.edp-video {
  display: block;
  max-width: 280px;
  max-height: 240px;
}
</style>
