<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api, formatDateTime } from '../api'
import WecomJoinWays from '../components/wecom/WecomJoinWays.vue'
import WecomKfAccounts from '../components/wecom/WecomKfAccounts.vue'
import WecomMembers from '../components/wecom/WecomMembers.vue'
import WecomResigned from '../components/wecom/WecomResigned.vue'
import WecomSettingsForm from '../components/wecom/WecomSettingsForm.vue'
import { SYNC_TARGETS } from '../wecom'

/**
 * 企业微信接入（设计 §7.4）：扫码授权代开发应用、同步数据、微信客服、成员绑定、离职继承、
 * 客户群活码、欢迎语与提醒、菜单消息、数据与智能专区。
 */
const route = useRoute()
const router = useRouter()
const status = ref<Schemas['WecomStatus'] | null>(null)
const loading = ref(false)
const installing = ref(false)
const syncing = ref(false)
const tab = ref('kf')
const ZONE_ROW = { key: 'zone', label: '专区分析结果' } as const

const corp = computed(() => status.value?.corp ?? null)
const syncRows = computed(() =>
  [...SYNC_TARGETS, ...(status.value?.settings?.zone_enabled ? [ZONE_ROW] : [])].map((t) => {
    const state = (status.value?.sync_state?.[t.key] ?? null) as {
      at?: string
      count?: number
      error?: string | null
    } | null
    return { ...t, at: state?.at ?? null, count: state?.count ?? null, error: state?.error ?? null }
  }),
)

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/admin/integrations/wecom')
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  status.value = data
}

async function install(): Promise<void> {
  installing.value = true
  const { data, error } = await api.POST('/api/v1/admin/integrations/wecom/install')
  installing.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  window.location.assign(data.url)
}

async function sync(): Promise<void> {
  syncing.value = true
  const { error } = await api.POST('/api/v1/admin/integrations/wecom/sync', { body: {} })
  syncing.value = false
  if (error) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已开始同步，稍后刷新查看结果')
  window.setTimeout(() => void load(), 2000)
}

async function unbind(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '解除后微信客服渠道停用，已同步的客户和聊天记录保留。企业微信里的授权需要企业管理员在企业微信后台另行取消。',
      '解除绑定',
      { confirmButtonText: '解除', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  const { error } = await api.DELETE('/api/v1/admin/integrations/wecom')
  if (error) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已解除绑定')
  await load()
}

onMounted(async () => {
  // 授权完成后企业微信把浏览器跳回这里（installed=1 或 error=原因）。
  const { installed, error } = route.query
  if (installed) ElMessage.success('企业微信授权成功，正在同步成员、客户和客服账号')
  if (typeof error === 'string' && error) ElMessage.error(`授权没有完成：${error}`)
  if (installed || error) await router.replace({ query: {} })
  await load()
})
</script>

<template>
  <div v-loading="loading" data-testid="wecom-page">
    <div class="page-header">
      <h2>企业微信</h2>
      <el-button v-if="corp" size="small" @click="load">刷新</el-button>
    </div>

    <template v-if="status">
      <el-alert
        v-if="!status.enabled"
        type="warning"
        :closable="false"
        show-icon
        title="平台还没有配置企业微信服务商"
        description="需要平台运营配置代开发应用模板（EDP_WECOM_SUITE_ID 等）后才能授权，请联系平台运营。"
      />

      <el-card v-else-if="!corp" shadow="never" class="intro" data-testid="wecom-unbound">
        <h3>接入企业微信</h3>
        <ol>
          <li>用企业微信管理员账号扫码，授权「智能客服平台」代开发应用。</li>
          <li>在企业微信后台把微信客服账号交给这个应用管理，客户在微信里的咨询会进入工作台。</li>
          <li>
            授权后自动同步成员、客户、标签和客户群；把成员绑定到平台员工，员工就能扫码登录、接收提醒。
          </li>
        </ol>
        <el-button
          type="primary"
          :loading="installing"
          data-testid="wecom-install"
          @click="install"
        >
          扫码授权企业微信
        </el-button>
      </el-card>

      <template v-else>
        <el-card shadow="never" class="corp" data-testid="wecom-corp">
          <div class="corp-head">
            <div>
              <div class="corp-name">{{ corp.corp_name }}</div>
              <div class="muted">
                CorpID {{ corp.corp_id }} · 应用 {{ corp.agent_id ?? '—' }} · 授权于
                {{ formatDateTime(corp.authorized_at) }}
                <template v-if="corp.auth_user_id"> · 授权人 {{ corp.auth_user_id }}</template>
              </div>
            </div>
            <div class="corp-actions">
              <el-button size="small" :loading="syncing" data-testid="wecom-sync" @click="sync">
                立即同步
              </el-button>
              <el-button size="small" type="danger" plain @click="unbind">解除绑定</el-button>
            </div>
          </div>
          <div v-if="status.counts" class="counts">
            <div class="count">
              <b>{{ status.counts.members_bound }}</b> / {{ status.counts.members }}
              <span>成员已绑定员工</span>
            </div>
            <div class="count" data-testid="wecom-contacts">
              <b>{{ status.counts.contacts }}</b
              ><span>位客户</span>
            </div>
            <div class="count">
              <b>{{ status.counts.group_chats }}</b
              ><span>个客户群</span>
            </div>
            <div class="count">
              <b>{{ status.counts.tags }}</b
              ><span>个企业标签</span>
            </div>
            <div class="count">
              <b>{{ status.counts.transfers_waiting }}</b
              ><span>位客户等待接替</span>
            </div>
          </div>
          <el-table :data="syncRows" size="small" class="sync">
            <el-table-column prop="label" label="数据" width="120" />
            <el-table-column label="最近同步" width="180">
              <template #default="{ row }">{{ row.at ? formatDateTime(row.at) : '—' }}</template>
            </el-table-column>
            <el-table-column label="数量" width="90">
              <template #default="{ row }">{{ row.count ?? '—' }}</template>
            </el-table-column>
            <el-table-column label="结果" min-width="200">
              <template #default="{ row }">
                <span v-if="row.error" class="error">{{ row.error }}</span>
                <span v-else-if="row.at" class="ok">成功</span>
                <span v-else class="muted">尚未同步</span>
              </template>
            </el-table-column>
          </el-table>
        </el-card>

        <el-tabs v-model="tab" data-testid="wecom-tabs">
          <el-tab-pane label="微信客服" name="kf">
            <WecomKfAccounts :accounts="status.kf_accounts" @changed="load" />
          </el-tab-pane>
          <el-tab-pane label="成员绑定" name="members" lazy>
            <WecomMembers @changed="load" />
          </el-tab-pane>
          <el-tab-pane label="离职继承" name="resigned" lazy>
            <WecomResigned @changed="load" />
          </el-tab-pane>
          <el-tab-pane label="客户群活码" name="join-ways" lazy>
            <WecomJoinWays />
          </el-tab-pane>
          <el-tab-pane label="设置" name="settings" lazy>
            <WecomSettingsForm
              v-if="status.settings"
              :settings="status.settings"
              :accounts="status.kf_accounts"
              @saved="load"
            />
          </el-tab-pane>
        </el-tabs>
      </template>
    </template>
  </div>
</template>

<style scoped>
.intro h3 {
  margin: 0 0 8px;
}

.intro ol {
  margin: 0 0 16px;
  padding-left: 20px;
  line-height: 1.9;
  color: var(--el-text-color-regular);
}

.corp {
  margin-bottom: 16px;
}

.corp-head {
  display: flex;
  justify-content: space-between;
  gap: 16px;
}

.corp-name {
  font-size: 16px;
  font-weight: 600;
}

.corp-actions {
  display: flex;
  gap: 8px;
  align-items: flex-start;
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.counts {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 32px;
  margin: 16px 0 8px;
}

.count b {
  font-size: 20px;
  margin-right: 4px;
}

.count span {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.sync {
  margin-top: 8px;
}

.error {
  color: var(--el-color-danger);
}

.ok {
  color: var(--el-color-success);
}
</style>
