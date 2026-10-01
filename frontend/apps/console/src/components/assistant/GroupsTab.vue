<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, ref } from 'vue'

import { api, formatDateTime } from '../../api'
import { effectiveReplyMode, PROVIDER_NAME, REPLY_MODE, type AssistantSettings, type Group } from '../../assistant'

/** 群组：助理所在的群，记录与提炼的开关、群内回复方式、立即提炼、查看记录、清空。 */
const groups = ref<Group[]>([])
const settings = ref<AssistantSettings | null>(null)
const loading = ref(false)
const busy = ref<string | null>(null)
const viewing = ref<Group | null>(null)
const messages = ref<Schemas['GroupMessageOut'][]>([])
const messagesTotal = ref(0)

async function load(): Promise<void> {
  loading.value = true
  const [list, conf] = await Promise.all([
    api.GET('/api/v1/assistant/groups'),
    api.GET('/api/v1/assistant/settings'),
  ])
  loading.value = false
  if (!list.data) {
    ElMessage.error(errorMessage(list.error))
    return
  }
  groups.value = list.data.items
  settings.value = conf.data ?? null
}

function replace(group: Group): void {
  groups.value = groups.value.map((g) => (g.id === group.id ? group : g))
}

async function patch(group: Group, body: Partial<Schemas['GroupUpdate']>): Promise<void> {
  busy.value = group.id
  const { data, error } = await api.PATCH('/api/v1/assistant/groups/{assistant_group_id}', {
    params: { path: { assistant_group_id: group.id } },
    body: { clear_reply_mode: false, ...body },
  })
  busy.value = null
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  replace(data)
}

function setMode(group: Group, mode: string): void {
  if (mode === 'default') void patch(group, { clear_reply_mode: true })
  else void patch(group, { reply_mode: mode as 'silent' | 'mentioned' })
}

async function extract(group: Group): Promise<void> {
  busy.value = group.id
  const { data, error } = await api.POST('/api/v1/assistant/groups/{assistant_group_id}/extract', {
    params: { path: { assistant_group_id: group.id } },
  })
  busy.value = null
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  if (data.error) ElMessage.error(`提炼失败：${data.error}`)
  else if (!data.messages) ElMessage.info('没有新的消息')
  else ElMessage.success(`提炼了 ${data.messages} 条消息，记录了 ${data.candidates} 条候选，请到知识库审核台查看`)
  await load()
}

async function clear(group: Group): Promise<void> {
  try {
    await ElMessageBox.confirm(`清空 ${group.name || group.external_chat_id} 记录的 ${group.message_count} 条消息？`, '清空记录', {
      confirmButtonText: '清空',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  busy.value = group.id
  const { data, error } = await api.POST('/api/v1/assistant/groups/{assistant_group_id}/clear', {
    params: { path: { assistant_group_id: group.id } },
  })
  busy.value = null
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  replace(data)
}

async function view(group: Group): Promise<void> {
  viewing.value = group
  const { data } = await api.GET('/api/v1/assistant/groups/{assistant_group_id}/messages', {
    params: { path: { assistant_group_id: group.id }, query: { limit: 100 } },
  })
  messages.value = data?.items ?? []
  messagesTotal.value = data?.total ?? 0
}

onMounted(load)
</script>

<template>
  <div>
    <p class="muted">
      把助理拉进群后自动登记。默认只记录不说话；企业微信和钉钉的机器人在群里只收到 @ 它的消息，飞书需要申请"获取群组中
      所有消息"权限，Telegram 需要关闭隐私模式或设为群管理员。
    </p>
    <el-table v-loading="loading" :data="groups" data-testid="groups-table" empty-text="助理还没有被拉进任何群">
      <el-table-column label="群" min-width="200">
        <template #default="{ row }">
          <div>{{ row.name || row.external_chat_id }}</div>
          <div class="muted">{{ PROVIDER_NAME[row.provider] }} · {{ row.bot_name }}</div>
        </template>
      </el-table-column>
      <el-table-column label="记录" width="90">
        <template #default="{ row }">
          <el-switch :model-value="row.recording" :loading="busy === row.id" @change="patch(row, { recording: !row.recording })" />
        </template>
      </el-table-column>
      <el-table-column label="群内回复" width="170">
        <template #default="{ row }">
          <el-select :model-value="row.reply_mode ?? 'default'" size="small" @change="(v: string) => setMode(row, v)">
            <el-option value="default" :label="`按企业设置（${REPLY_MODE[effectiveReplyMode({ reply_mode: null }, settings)]}）`" />
            <el-option v-for="(label, value) in REPLY_MODE" :key="value" :value="value" :label="label" />
          </el-select>
        </template>
      </el-table-column>
      <el-table-column label="提炼" width="90">
        <template #default="{ row }">
          <el-switch :model-value="row.extract" :loading="busy === row.id" @change="patch(row, { extract: !row.extract })" />
        </template>
      </el-table-column>
      <el-table-column label="消息" width="120">
        <template #default="{ row }">
          {{ row.message_count }}
          <span v-if="row.unextracted" class="muted">（{{ row.unextracted }} 条待提炼）</span>
        </template>
      </el-table-column>
      <el-table-column label="最近消息" width="160">
        <template #default="{ row }">{{ row.last_message_at ? formatDateTime(row.last_message_at) : '—' }}</template>
      </el-table-column>
      <el-table-column label="提炼" width="180">
        <template #default="{ row }">
          <template v-if="row.last_extracted_at">
            {{ formatDateTime(row.last_extracted_at) }} · {{ row.extracted_candidates }} 条候选
          </template>
          <span v-else class="muted">还没提炼</span>
        </template>
      </el-table-column>
      <el-table-column width="220">
        <template #default="{ row }">
          <el-button link type="primary" @click="view(row)">记录</el-button>
          <el-button link :loading="busy === row.id" data-testid="group-extract" @click="extract(row)">立即提炼</el-button>
          <el-button link type="danger" @click="clear(row)">清空</el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-drawer :model-value="viewing !== null" size="560px" :title="viewing?.name || '群记录'" @close="viewing = null">
      <p class="muted">共 {{ messagesTotal }} 条，显示最近 100 条。</p>
      <div v-for="m in messages" :key="m.id" class="message">
        <div class="meta">
          <strong>{{ m.staff_name ?? m.sender_name }}</strong>
          <span>{{ formatDateTime(m.sent_at) }}</span>
          <el-tag v-if="m.extracted" size="small" effect="plain">已提炼</el-tag>
        </div>
        <div class="text">{{ m.text }}</div>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.message {
  padding: 8px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.meta {
  display: flex;
  gap: 8px;
  align-items: center;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.text {
  white-space: pre-wrap;
}
</style>
