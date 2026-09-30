<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { onMounted, ref } from 'vue'

import { api, formatDateTime, widgetBase } from '../../api'
import ChannelEditor from './ChannelEditor.vue'

type Channel = Schemas['ChannelOut']

const CHANNEL_TYPES: Record<string, string> = {
  web: '网页',
  wecom_kf: '微信客服',
  wecom_contact: '客户联系',
}

const channels = ref<Channel[]>([])
const policies = ref<Schemas['RoutingPolicyOut'][]>([])
const loading = ref(false)
const editing = ref<Channel | null>(null)

async function loadPolicies(): Promise<void> {
  const { data } = await api.GET('/api/v1/routing-policies')
  policies.value = data?.items ?? []
}

async function load(): Promise<void> {
  loading.value = true
  const [channelRes] = await Promise.all([api.GET('/api/v1/channels'), loadPolicies()])
  loading.value = false
  if (!channelRes.data) {
    ElMessage.error(errorMessage(channelRes.error))
    return
  }
  channels.value = channelRes.data.items
}

/** 打开设置时刷新路由策略：可能刚在"路由策略"页签里新建过。 */
async function edit(channel: Channel): Promise<void> {
  await loadPolicies()
  editing.value = channel
}

function policyName(channel: Channel): string {
  const policy = policies.value.find((p) =>
    channel.routing_policy_id ? p.id === channel.routing_policy_id : p.is_default,
  )
  return policy?.name ?? '默认策略'
}

function widgetUrl(channel: Channel): string {
  return `${widgetBase}/?key=${encodeURIComponent(channel.public_key)}`
}

function onSaved(channel: Channel): void {
  channels.value = channels.value.map((c) => (c.id === channel.id ? channel : c))
  if (editing.value?.id === channel.id) editing.value = channel
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
    <el-alert
      type="info"
      :closable="false"
      show-icon
      title="渠道 key 用于在官网等页面嵌入访客聊天窗口，它是公开标识，不是密码。"
      class="tip"
    />
    <el-table
      v-loading="loading"
      :data="channels"
      data-testid="channel-table"
      empty-text="暂无渠道"
    >
      <el-table-column prop="name" label="名称" min-width="120" />
      <el-table-column label="类型" width="90">
        <template #default="{ row }">{{ CHANNEL_TYPES[row.type] ?? row.type }}</template>
      </el-table-column>
      <el-table-column label="渠道 key" min-width="300">
        <template #default="{ row }">
          <code class="key">{{ row.public_key }}</code>
          <el-button link type="primary" @click="copy(row.public_key)">复制</el-button>
        </template>
      </el-table-column>
      <el-table-column label="路由策略" min-width="110">
        <template #default="{ row }">{{ policyName(row) }}</template>
      </el-table-column>
      <el-table-column label="实名访客" width="90">
        <template #default="{ row }">
          <template v-if="row.type === 'web'">{{
            row.identity_secret ? '已启用' : '未启用'
          }}</template>
          <span v-else>—</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="80">
        <template #default="{ row }">
          <el-tag :type="row.status === 'active' ? 'success' : 'info'">
            {{ row.status === 'active' ? '启用' : '停用' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="创建时间" width="170">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="190" fixed="right">
        <template #default="{ row }">
          <el-button
            link
            type="primary"
            :data-testid="`edit-channel-${row.name}`"
            @click="edit(row)"
          >
            设置
          </el-button>
          <el-link
            v-if="row.type === 'web'"
            :href="widgetUrl(row)"
            target="_blank"
            type="primary"
            class="link"
          >
            打开访客测试页
          </el-link>
        </template>
      </el-table-column>
    </el-table>
    <ChannelEditor
      :channel="editing"
      :policies="policies"
      @saved="onSaved"
      @close="editing = null"
    />
  </div>
</template>

<style scoped>
.tip {
  margin-bottom: 16px;
}

.key {
  margin-right: 8px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
}

.link {
  margin-left: 12px;
}
</style>
