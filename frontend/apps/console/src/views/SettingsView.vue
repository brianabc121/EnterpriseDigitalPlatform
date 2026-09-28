<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { onMounted, ref } from 'vue'

import { api, formatDateTime, widgetBase } from '../api'

const CHANNEL_TYPES: Record<string, string> = { web: '网页' }

const channels = ref<Schemas['ChannelOut'][]>([])
const loading = ref(false)

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/channels')
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  channels.value = data.items
}

function widgetUrl(channel: Schemas['ChannelOut']): string {
  return `${widgetBase}/?key=${encodeURIComponent(channel.public_key)}`
}

async function copy(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制')
  } catch {
    ElMessage.warning('浏览器不允许自动复制，请手动选择复制')
  }
}

onMounted(load)
</script>

<template>
  <div>
    <div class="page-header">
      <h2>接入渠道</h2>
    </div>
    <el-alert
      type="info"
      :closable="false"
      show-icon
      title="渠道 key 用于在官网等页面嵌入访客聊天窗口，它是公开标识，不是密码。"
      class="tip"
    />
    <el-table v-loading="loading" :data="channels" data-testid="channel-table" empty-text="暂无渠道">
      <el-table-column prop="name" label="名称" width="140" />
      <el-table-column label="类型" width="90">
        <template #default="{ row }">{{ CHANNEL_TYPES[row.type] ?? row.type }}</template>
      </el-table-column>
      <el-table-column label="渠道 key" min-width="320">
        <template #default="{ row }">
          <code class="key">{{ row.public_key }}</code>
          <el-button link type="primary" @click="copy(row.public_key)">复制</el-button>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag :type="row.status === 'active' ? 'success' : 'info'">
            {{ row.status === 'active' ? '启用' : '停用' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="创建时间" width="180">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="130">
        <template #default="{ row }">
          <el-link v-if="row.type === 'web'" :href="widgetUrl(row)" target="_blank" type="primary">
            打开访客测试页
          </el-link>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<style scoped>
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}

.page-header h2 {
  margin: 0;
  font-size: 18px;
}

.tip {
  margin-bottom: 16px;
}

.key {
  margin-right: 8px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
}
</style>
