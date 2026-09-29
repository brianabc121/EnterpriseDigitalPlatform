<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { ref } from 'vue'

import { api, formatDateTime } from '../../api'

/** 微信客服账号：每个账号是一个渠道（路由策略在"设置 → 接入渠道"里调整），可以设置欢迎语。 */
const props = defineProps<{ accounts: Schemas['KfAccountOut'][] }>()
const emit = defineEmits<{ changed: [] }>()

const editing = ref<Schemas['KfAccountOut'] | null>(null)
const welcome = ref('')
const saving = ref(false)

async function edit(account: Schemas['KfAccountOut']): Promise<void> {
  const { data } = await api.GET('/api/v1/channels')
  const channel = data?.items.find((c) => c.id === account.channel_id)
  welcome.value = channel?.kf?.welcome_message ?? ''
  editing.value = account
}

async function save(): Promise<void> {
  if (!editing.value) return
  saving.value = true
  const { error } = await api.PATCH('/api/v1/channels/{channel_id}', {
    params: { path: { channel_id: editing.value.channel_id } },
    body: { kf: { welcome_message: welcome.value.trim() || null } },
  })
  saving.value = false
  if (error) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('欢迎语已保存')
  editing.value = null
  emit('changed')
}

async function copy(url: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(url)
    ElMessage.success('已复制客服链接')
  } catch {
    ElMessage.info(url)
  }
}
</script>

<template>
  <div data-testid="wecom-kf">
    <el-empty
      v-if="!props.accounts.length"
      description="还没有客服账号：请在企业微信后台把微信客服账号交给「智能客服平台」应用管理，然后点「立即同步」"
    />
    <el-table v-else :data="props.accounts" size="small">
      <el-table-column label="客服账号" min-width="160">
        <template #default="{ row }">
          <div class="account">
            <el-avatar v-if="row.avatar" :src="row.avatar" :size="24" />
            <span>{{ row.name }}</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column prop="open_kfid" label="open_kfid" min-width="150" />
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag :type="row.status === 'active' ? 'success' : 'info'" size="small">
            {{ row.status === 'active' ? '接待中' : '已收回' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="客服链接" min-width="200">
        <template #default="{ row }">
          <el-button
            v-if="row.contact_url"
            link
            type="primary"
            size="small"
            @click="copy(row.contact_url)"
          >
            复制链接
          </el-button>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="最近拉取" width="170">
        <template #default="{ row }">{{
          row.synced_at ? formatDateTime(row.synced_at) : '—'
        }}</template>
      </el-table-column>
      <el-table-column label="" width="100" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" data-testid="kf-welcome" @click="edit(row)">
            欢迎语
          </el-button>
        </template>
      </el-table-column>
    </el-table>
    <p class="muted hint">
      客户最后一次发消息后 48 小时内，最多可以回复 5 条；客户再发消息后额度重置。AI
      回复会带「【AI】」标识。
    </p>

    <el-dialog
      :model-value="editing !== null"
      :title="`欢迎语 · ${editing?.name ?? ''}`"
      width="480px"
      @update:model-value="(v: boolean) => !v && (editing = null)"
    >
      <p class="muted">客户进入会话时自动发送，不占 5 条的额度。留空表示不发送。</p>
      <el-input
        v-model="welcome"
        type="textarea"
        :rows="4"
        maxlength="500"
        show-word-limit
        data-testid="kf-welcome-input"
      />
      <template #footer>
        <el-button @click="editing = null">取消</el-button>
        <el-button type="primary" :loading="saving" data-testid="kf-welcome-save" @click="save">
          保存
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.account {
  display: flex;
  align-items: center;
  gap: 8px;
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.hint {
  margin-top: 12px;
}
</style>
