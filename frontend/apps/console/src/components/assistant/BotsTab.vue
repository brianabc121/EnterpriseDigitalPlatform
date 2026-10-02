<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, ref } from 'vue'

import { api, formatDateTime } from '../../api'
import { botHealth, PROVIDER_NAME, type Bot, type ProviderSpec } from '../../assistant'
import BotDialog from './BotDialog.vue'

/** 机器人：接入企业微信、钉钉、飞书、Telegram、WhatsApp；回调地址、启停、换地址、测试发送、删除。 */
const bots = ref<Bot[]>([])
const providers = ref<ProviderSpec[]>([])
const webhookBase = ref('')
const loading = ref(false)
const busy = ref<string | null>(null)
const dialogOpen = ref(false)
const editing = ref<Bot | null>(null)

async function load(): Promise<void> {
  loading.value = true
  const [list, specs] = await Promise.all([
    api.GET('/api/v1/assistant/bots'),
    api.GET('/api/v1/assistant/providers'),
  ])
  loading.value = false
  if (!list.data || !specs.data) {
    ElMessage.error(errorMessage(list.error ?? specs.error))
    return
  }
  bots.value = list.data.items
  providers.value = specs.data.items
  webhookBase.value = specs.data.webhook_base
}

function add(): void {
  editing.value = null
  dialogOpen.value = true
}

function edit(bot: Bot): void {
  editing.value = bot
  dialogOpen.value = true
}

function replace(bot: Bot): void {
  const exists = bots.value.some((b) => b.id === bot.id)
  bots.value = exists ? bots.value.map((b) => (b.id === bot.id ? bot : b)) : [...bots.value, bot]
}

async function copy(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制')
  } catch {
    ElMessage.info(text)
  }
}

type Action = 'enable' | 'disable' | 'rotate-token'

async function act(bot: Bot, action: Action): Promise<void> {
  if (action === 'disable') {
    try {
      await ElMessageBox.confirm('停用后不再接收这个机器人的消息，也不再通过它发通知。', '停用机器人', {
        confirmButtonText: '停用',
        cancelButtonText: '取消',
        type: 'warning',
      })
    } catch {
      return
    }
  }
  if (action === 'rotate-token') {
    try {
      await ElMessageBox.confirm('旧的回调地址立即失效，需要把新地址重新配置到平台后台（Telegram 自动登记）。', '更换回调地址', {
        confirmButtonText: '更换',
        cancelButtonText: '取消',
        type: 'warning',
      })
    } catch {
      return
    }
  }
  busy.value = bot.id
  const path = {
    enable: '/api/v1/assistant/bots/{bot_id}/enable' as const,
    disable: '/api/v1/assistant/bots/{bot_id}/disable' as const,
    'rotate-token': '/api/v1/assistant/bots/{bot_id}/rotate-token' as const,
  }[action]
  const { data, error } = await api.POST(path, { params: { path: { bot_id: bot.id } } })
  busy.value = null
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  replace(data)
}

async function test(bot: Bot): Promise<void> {
  busy.value = bot.id
  const { data, error } = await api.POST('/api/v1/assistant/bots/{bot_id}/test', {
    params: { path: { bot_id: bot.id } },
  })
  busy.value = null
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  const result: Schemas['BotTestOut'] = data
  if (result.ok) ElMessage.success(`已发给 ${result.sent} 位已绑定的员工`)
  else ElMessage.error(result.error ?? '发送失败')
  await load()
}

async function remove(bot: Bot): Promise<void> {
  try {
    await ElMessageBox.confirm(`删除后员工在 ${bot.name} 上的绑定和群记录一起删除。`, '删除机器人', {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  const { error } = await api.DELETE('/api/v1/assistant/bots/{bot_id}', { params: { path: { bot_id: bot.id } } })
  if (error) {
    ElMessage.error(errorMessage(error))
    return
  }
  bots.value = bots.value.filter((b) => b.id !== bot.id)
}

onMounted(load)
</script>

<template>
  <div>
    <div class="toolbar">
      <span class="muted">把助理接到员工在用的 IM；每个机器人有自己的回调地址，密钥加密保存、不再显示。</span>
      <el-button type="primary" data-testid="bot-add" @click="add">添加机器人</el-button>
    </div>
    <el-table v-loading="loading" :data="bots" data-testid="bots-table" empty-text="还没有接入机器人">
      <el-table-column label="平台" width="110">
        <template #default="{ row }">{{ PROVIDER_NAME[row.provider] }}</template>
      </el-table-column>
      <el-table-column prop="name" label="名称" width="140" />
      <el-table-column label="状态" width="150">
        <template #default="{ row }">
          <el-tag size="small" :type="botHealth(row).type">{{ botHealth(row).label }}</el-tag>
          <div v-if="row.last_error" class="error">{{ row.last_error }}</div>
        </template>
      </el-table-column>
      <el-table-column label="回调地址" min-width="260">
        <template #default="{ row }">
          <span class="url">{{ row.webhook_url }}</span>
          <el-button link type="primary" size="small" @click="copy(row.webhook_url)">复制</el-button>
        </template>
      </el-table-column>
      <el-table-column label="绑定 / 群" width="90">
        <template #default="{ row }">{{ row.identities }} / {{ row.groups }}</template>
      </el-table-column>
      <el-table-column label="最近收到" width="160">
        <template #default="{ row }">{{ row.last_received_at ? formatDateTime(row.last_received_at) : '—' }}</template>
      </el-table-column>
      <el-table-column width="300">
        <template #default="{ row }">
          <el-button link type="primary" @click="edit(row)">修改</el-button>
          <el-button link :loading="busy === row.id" @click="test(row)">测试发送</el-button>
          <el-button link :loading="busy === row.id" @click="act(row, 'rotate-token')">换地址</el-button>
          <el-button
            v-if="row.status === 'active'"
            link
            type="warning"
            :loading="busy === row.id"
            @click="act(row, 'disable')"
          >
            停用
          </el-button>
          <el-button v-else link type="success" :loading="busy === row.id" @click="act(row, 'enable')">启用</el-button>
          <el-button link type="danger" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>
    <BotDialog v-model="dialogOpen" :bot="editing" :providers="providers" :webhook-base="webhookBase" @saved="replace" />
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.url {
  font-size: 12px;
  word-break: break-all;
}

.error {
  font-size: 12px;
  color: var(--el-color-danger);
}
</style>
