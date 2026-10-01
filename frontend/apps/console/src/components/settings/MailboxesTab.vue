<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, ref } from 'vue'

import { api } from '../../api'
import { mailboxStatus, shortTime, type MailAccount, type MailProvider } from '../../mail'
import MailboxDialog from './MailboxDialog.vue'

/**
 * 设置 → 邮箱（设计文档 §10.8）：接入 163、QQ、Gmail、企业邮箱等。客户发来的新邮件直接进入人工排队，
 * 分配给客服后是未读会话；客服在工作台回复邮件。
 */
const accounts = ref<MailAccount[]>([])
const providers = ref<MailProvider[]>([])
const allowInsecure = ref(false)
const loading = ref(false)
const dialogOpen = ref(false)
const editing = ref<MailAccount | null>(null)
/** 正在收取或启停的邮箱。 */
const busy = ref<string | null>(null)

async function load(): Promise<void> {
  loading.value = true
  const [list, presets] = await Promise.all([
    api.GET('/api/v1/mail/accounts'),
    api.GET('/api/v1/mail/providers'),
  ])
  loading.value = false
  if (!list.data || !presets.data) {
    ElMessage.error(errorMessage(list.error ?? presets.error))
    return
  }
  accounts.value = list.data.items
  providers.value = presets.data.items
  allowInsecure.value = presets.data.allow_insecure
}

function providerName(key: string): string {
  return providers.value.find((p) => p.key === key)?.name ?? key
}

function add(): void {
  editing.value = null
  dialogOpen.value = true
}

function edit(account: MailAccount): void {
  editing.value = account
  dialogOpen.value = true
}

function replace(account: MailAccount): void {
  const exists = accounts.value.some((a) => a.id === account.id)
  accounts.value = exists
    ? accounts.value.map((a) => (a.id === account.id ? account : a))
    : [...accounts.value, account]
}

async function fetchNow(account: MailAccount): Promise<void> {
  busy.value = account.id
  const { data, error } = await api.POST('/api/v1/mail/accounts/{account_id}/fetch', {
    params: { path: { account_id: account.id } },
  })
  busy.value = null
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  replace(data.account)
  if (data.error) ElMessage.error(`收取失败：${data.error}`)
  else if (data.imported) ElMessage.success(`收到 ${data.imported} 封新邮件，已进入客服排队`)
  else ElMessage.info(data.ignored ? `没有新邮件（忽略了 ${data.ignored} 封）` : '没有新邮件')
}

async function toggle(account: MailAccount): Promise<void> {
  const enable = account.status === 'disabled'
  if (!enable) {
    try {
      await ElMessageBox.confirm(
        `停用后不再收取 ${account.address} 的新邮件，进行中的邮件会话也不能再回复。`,
        '停用邮箱',
        { confirmButtonText: '停用', cancelButtonText: '取消', type: 'warning' },
      )
    } catch {
      return
    }
  }
  busy.value = account.id
  const path = enable
    ? ('/api/v1/mail/accounts/{account_id}/enable' as const)
    : ('/api/v1/mail/accounts/{account_id}/disable' as const)
  const { data, error } = await api.POST(path, { params: { path: { account_id: account.id } } })
  busy.value = null
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  replace(data)
  ElMessage.success(enable ? '已启用，马上开始收信' : '已停用')
}

onMounted(load)
</script>

<template>
  <div>
    <el-alert
      type="info"
      :closable="false"
      show-icon
      class="tip"
      title="客户发到这些邮箱的新邮件直接进入客服排队（不经过 AI、不受工作时间限制），分配给客服后是未读会话，客服在工作台回复邮件。"
      description="添加时只记下收件箱的当前位置，之前的邮件不导入；只读取收件箱，不改变邮件的已读状态，邮箱客户端里照常可以看。"
    />
    <div class="bar">
      <el-button type="primary" data-testid="add-mailbox" @click="add">添加邮箱</el-button>
    </div>
    <el-table
      v-loading="loading"
      :data="accounts"
      data-testid="mailbox-table"
      empty-text="还没有接入邮箱"
    >
      <el-table-column label="邮箱" min-width="220">
        <template #default="{ row }">
          <div class="address" data-testid="mailbox-address">{{ row.address }}</div>
          <small v-if="row.name !== row.address" class="muted">{{ row.name }}</small>
        </template>
      </el-table-column>
      <el-table-column label="类型" width="110">
        <template #default="{ row }">{{ providerName(row.provider) }}</template>
      </el-table-column>
      <el-table-column label="状态" min-width="160">
        <template #default="{ row }">
          <el-tag :type="mailboxStatus(row).type" data-testid="mailbox-status">
            {{ mailboxStatus(row).label }}
          </el-tag>
          <div v-if="row.last_error" class="error" data-testid="mailbox-error">
            {{ row.last_error }}
          </div>
        </template>
      </el-table-column>
      <el-table-column label="最近收信" width="110">
        <template #default="{ row }">{{ shortTime(row.last_polled_at) }}</template>
      </el-table-column>
      <el-table-column label="最近来信" width="110">
        <template #default="{ row }">{{ shortTime(row.last_received_at) }}</template>
      </el-table-column>
      <el-table-column label="已忽略" width="80">
        <template #default="{ row }">
          <el-tooltip content="自动回复、退信、群发邮件和忽略的发件人不会导入" placement="top">
            <span>{{ row.ignored }}</span>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="200" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" :data-testid="`edit-mailbox-${row.address}`" @click="edit(row)">
            修改
          </el-button>
          <el-button
            link
            type="primary"
            :disabled="row.status === 'disabled'"
            :loading="busy === row.id"
            :data-testid="`fetch-mailbox-${row.address}`"
            @click="fetchNow(row)"
          >
            立即收取
          </el-button>
          <el-button
            link
            :type="row.status === 'disabled' ? 'primary' : 'danger'"
            :data-testid="`toggle-mailbox-${row.address}`"
            @click="toggle(row)"
          >
            {{ row.status === 'disabled' ? '启用' : '停用' }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>
    <MailboxDialog
      v-model="dialogOpen"
      :account="editing"
      :providers="providers"
      :allow-insecure="allowInsecure"
      @saved="replace"
    />
  </div>
</template>

<style scoped>
.tip {
  margin-bottom: 12px;
}

.bar {
  margin-bottom: 12px;
}

.address {
  font-weight: 500;
}

.muted {
  color: var(--el-text-color-secondary);
}

.error {
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.4;
  color: var(--el-color-danger);
}
</style>
