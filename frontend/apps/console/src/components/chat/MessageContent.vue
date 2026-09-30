<script setup lang="ts">
import { MessageBody } from '@edp/ui'

import type { WorkbenchMessage } from '../../workbench/messages'

/** 工作台里的一条消息：内容由共享组件展示，图片用 Element Plus 的大图预览，微信客服菜单显示可点选的按钮。 */
defineProps<{ message: WorkbenchMessage }>()
</script>

<template>
  <MessageBody :message="message">
    <template #image="{ url }">
      <el-image
        :src="url"
        :preview-src-list="[url]"
        preview-teleported
        fit="contain"
        class="image"
        data-testid="message-image"
      />
    </template>
    <div v-if="message.menu?.length" class="menu" data-testid="message-menu">
      <span v-for="m in message.menu" :key="m.id" class="option">{{ m.content }}</span>
    </div>
  </MessageBody>
</template>

<style scoped>
.image {
  display: block;
  max-width: 240px;
  max-height: 240px;
  cursor: zoom-in;
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
</style>
